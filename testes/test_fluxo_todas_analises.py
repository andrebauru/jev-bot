"""Fluxo 'todas as análises ao JEv', bloqueio de risco e histórico de 7 dias."""

import os
import tempfile
import time
from pathlib import Path
from unittest.mock import Mock, patch
import unittest

from apoio import FonteFake, LoggerSilencioso, candles_deterministicos, estado_mt5, janela
from jev_bot import candidatos, configuracao, mercados, padroes
from jev_bot.cache_m1 import CacheM1
from jev_bot.execution.paper import Book
from jev_bot.orquestrador import Orquestrador
from jev_bot.types import Decision


class TestHistorico7Dias(unittest.TestCase):
    """Janela padrão de 7 dias, configurável e coerente no cache."""

    def test_padrao_um_dia(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(configuracao.dias_historico(), 1)

    def test_env_configuravel_e_limitado(self):
        with patch.dict(os.environ, {"JEV_HISTORY_DAYS": "10"}, clear=True):
            self.assertEqual(configuracao.dias_historico(), 10)
        with patch.dict(os.environ, {"JEV_HISTORY_DAYS": "99"}, clear=True):
            with self.assertRaises(RuntimeError):
                configuracao.dias_historico()

    def test_cache_recorta_a_um_dia(self):
        with tempfile.TemporaryDirectory() as pasta:
            cache = CacheM1(Path(pasta))
            self.assertEqual(cache.max_dias, 1)
            arquivo = Path(pasta) / "mercados.json"
            mercados.salvar({"selecao": {"C": {"TESTE": {"monitorar": True,
                                                         "operar": True}}}}, arquivo)
            orch = Orquestrador(banco=Path(pasta) / "db.json",
                                pasta_dados=Path(pasta) / "padroes",
                                caminho_selecao=arquivo)
            self.assertEqual(orch.dias_historico, 1)
            self.assertEqual(orch.cache_m1.max_dias, 1)


class TestTodasAnalisesAoJev(unittest.TestCase):
    """Análise calculada é enviada; Python não veta por qualidade estatística."""

    def _orquestrador(self, pasta):
        arquivo = Path(pasta) / "mercados.json"
        mercados.salvar({"selecao": {"C": {"TESTE": {"monitorar": True, "operar": True}}}},
                        arquivo)
        orch = Orquestrador(banco=Path(pasta) / "db.json",
                            pasta_dados=Path(pasta) / "padroes",
                            caminho_selecao=arquivo, relogio=time.time)
        orch.logger = LoggerSilencioso()
        orch.book = Book.load(Path(pasta) / "db.json", "USD")
        orch.book.active = True
        orch.book.prices_valid = True
        orch.jev_configurado = True
        orch.repositorio = padroes.RepositorioPadroes(Path(pasta) / "padroes")
        orch.eventos = []
        orch.publicar = lambda canal, registro: orch.eventos.append((canal, registro))
        candles = candles_deterministicos()
        closes = [c["close"] for c in candles[-100:]]
        orch.fonte = FonteFake(estados=[estado_mt5("TESTE", closes=closes)],
                               cotacoes={"TESTE": (99.995, 100.005, time.time())},
                               janelas={"TESTE": janela(candles)})
        orch.fonte.janela_m1 = lambda simbolo, **_k: janela(candles)
        self.addCleanup(orch.encerrar.set)
        self.addCleanup(lambda: orch.pool_matematico.encerrar())
        return orch

    def _drenar(self, orch):
        limite = time.monotonic() + 3
        while (orch.analises_pendentes or orch.pendentes_jev) and time.monotonic() < limite:
            orch._drenar_analises(f"ciclo{orch.ciclo}")
            orch._processar_resultados_jev()
            time.sleep(.005)
        orch._processar_resultados_jev()

    def test_expectativa_negativa_ainda_propoe_e_jev_aprova(self):
        """Evidência fraca não impede o Python de propor; quem reprova é o JEv/risco."""
        regra = {"tipo_regra": "quedas_consecutivas", "parametros": {"n": 3},
                 "direcao": "BUY", "horizonte_minutos": 5,
                 "strategy_id": "conditional_sequence"}
        with tempfile.TemporaryDirectory() as pasta:
            orch = self._orquestrador(pasta)
            negativo = {"status": "rejeitado", "motivo": "OOS ruim",
                        "reason_code": "OOS_EDGE_NEGATIVE", "reason_codes": ["OOS_EDGE_NEGATIVE"],
                        "total": {"eventos": 100, "acertos": 40, "taxa": .4, "media_pct": -0.5,
                                  "mediana_pct": -0.5, "desvio_pct": .1},
                        "amostra": {"eventos": 70, "taxa": .4, "media_pct": -0.5},
                        "oos": {"eventos": 30, "taxa": .4, "media_pct": -0.6},
                        "estabilidade": {"wilson_inferior_oos_ajustado": .1},
                        "custo_pct": .1, "cost_basis": "spread_only",
                        "wilson_inferior": .1, "familias_testadas": 1}
            with patch("jev_bot.orquestrador.candidatos.regras_candidatas",
                       return_value=[regra]), \
                    patch.object(Orquestrador, "_validar_candidato_m1", return_value=negativo), \
                    patch("jev_bot.orquestrador.jev.avaliar_entrada",
                          return_value={"approved": True, "reason": "ok"}) as jev:
                orch._executar_ciclo("c1")
                self._drenar(orch)
            jev.assert_called_once()
            self.assertEqual(len(orch.book.fills), 1)
            self.assertEqual(orch.analises_memoria["TESTE"]["buy"]["expectancy_net_pct"], -0.5)

    def test_risco_pre_jev_nao_chama_e_nao_abre(self):
        """Veto de risco pré-JEv: nenhuma chamada, nenhuma ordem, motivo claro."""
        with tempfile.TemporaryDirectory() as pasta:
            orch = self._orquestrador(pasta)
            gate = Mock(verdict="SKIP", reason="Limite de drawdown", volume=0,
                        entry=0, stop=0, target=0, risk_amount=0, margin=0)
            with patch("jev_bot.orquestrador.risk.pre_check", return_value=gate), \
                    patch("jev_bot.orquestrador.jev.avaliar_entrada") as jev:
                orch._executar_ciclo("c1")
                self._drenar(orch)
            jev.assert_not_called()
            self.assertEqual(len(orch.book.fills), 0)
            self.assertEqual(orch.contadores_sessao.get("pre_risk_reject", 0), 1)
            motivos = {r.get("motivo") for c, r in orch.eventos if c == "oportunidade"}
            self.assertIn("RISK_REJECTED_PRE_JEV", motivos)

    def test_dados_insuficientes_nao_enviam_payload(self):
        poucos = [{"time": i * 60, "open": 100.0, "high": 100.5, "low": 99.5,
                   "close": 100.0, "tick_volume": 1} for i in range(10)]
        with tempfile.TemporaryDirectory() as pasta:
            arquivo = Path(pasta) / "mercados.json"
            mercados.salvar({"selecao": {"C": {"TESTE": {"monitorar": True, "operar": True}}}},
                            arquivo)
            orch = Orquestrador(banco=Path(pasta) / "db.json",
                                pasta_dados=Path(pasta) / "padroes",
                                caminho_selecao=arquivo, relogio=time.time)
            orch.logger = LoggerSilencioso()
            orch.book = Book.load(Path(pasta) / "db.json", "USD")
            orch.book.active = True
            orch.jev_configurado = True
            orch.repositorio = padroes.RepositorioPadroes(Path(pasta) / "padroes")
            orch.eventos = []
            orch.publicar = lambda canal, registro: orch.eventos.append((canal, registro))
            orch.fonte = FonteFake(estados=[estado_mt5("TESTE", closes=[100.0] * 10)],
                                   cotacoes={"TESTE": (99.995, 100.005, time.time())},
                                   janelas={"TESTE": janela(poucos)})
            orch.fonte.janela_m1 = lambda simbolo, **_k: janela(poucos)
            with patch("jev_bot.orquestrador.jev.avaliar_entrada") as jev:
                orch._executar_ciclo("c1")
                self._drenar(orch)
            jev.assert_not_called()
            motivos = {r.get("motivo") for c, r in orch.eventos if c == "oportunidade"}
            self.assertIn("DADOS_INSUFICIENTES", motivos)

    def test_proposta_envia_payload_imutavel(self):
        """A proposta leva symbol/side/entry/stop/alvo/risco/ids e evidências."""
        with tempfile.TemporaryDirectory() as pasta:
            orch = self._orquestrador(pasta)
            with patch("jev_bot.orquestrador.jev.avaliar_entrada",
                       return_value={"approved": True, "reason": "ok"}) as jev:
                orch._executar_ciclo("c1")
                self._drenar(orch)
            jev.assert_called_once()
            payload = jev.call_args[0][0]
            for campo in ("symbol", "side_proposed", "entry_reference", "stop_loss",
                          "take_profit", "volume", "risk_amount", "risk_pct", "spread",
                          "candidate_id", "request_id", "run_id", "candle_ref", "expires_at",
                          "evidencias"):
                self.assertIn(campo, payload)
            self.assertIn("evento", orch.analises_memoria["TESTE"])
            self.assertIn("history", orch.analises_memoria["TESTE"])

    def test_deduplicacao_por_simbolo_e_candle(self):
        regra = {"tipo_regra": "quedas_consecutivas", "parametros": {"n": 3},
                 "direcao": "BUY", "horizonte_minutos": 5}
        validacao = {"status": "validado", "motivo": "ok", "reason_code": "QUANT_VALIDATED",
                     "reason_codes": ["QUANT_VALIDATED"],
                     "total": {"eventos": 10, "acertos": 7, "taxa": .7, "media_pct": .5},
                     "amostra": {"eventos": 7, "taxa": .7, "media_pct": .5},
                     "oos": {"eventos": 3, "taxa": .7, "media_pct": .5},
                     "estabilidade": {}, "custo_pct": .01, "cost_basis": "spread_only"}
        with tempfile.TemporaryDirectory() as pasta:
            orch = self._orquestrador(pasta)
            estado = estado_mt5("TESTE", closes=[100.0] * 30)
            dados = {"candle_ref": estado.candles[-1]["time"], "custo_pct": .01,
                     "dias_janela": 1, "evento": {"status": "OK", "bars_available": 100},
                     "candidatos": [(regra, validacao)], "revalidacoes": [],
                     "buy_evaluated": 1, "sell_evaluated": 0, "stats": {}}
            with patch("jev_bot.orquestrador.jev.avaliar_entrada",
                       return_value={"approved": True, "reason": "ok"}) as jev:
                primeiro = orch._despachar_analise(estado, dados, "id1")
                segundo = orch._despachar_analise(estado, dados, "id2")
                self._drenar(orch)
            self.assertEqual(primeiro, "enviado_jev")
            self.assertEqual(segundo, "candle_ja_enviado")
            self.assertEqual(orch.contadores_ciclo.get("enviados_jev"), 1)
            jev.assert_called_once()

    def test_jev_no_nao_gera_ordem(self):
        with tempfile.TemporaryDirectory() as pasta:
            orch = self._orquestrador(pasta)
            with patch("jev_bot.orquestrador.jev.avaliar_entrada",
                       return_value={"approved": False, "reason": "sem suporte"}):
                orch._executar_ciclo("c1")
                self._drenar(orch)
            self.assertEqual(len(orch.book.fills), 0)
            naos = [r for c, r in orch.eventos if c == "decisao" and r.get("decisao") == "NAO"]
            self.assertTrue(naos)


if __name__ == "__main__":
    unittest.main(verbosity=2)
