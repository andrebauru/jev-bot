"""Matemática local M1: elegibilidade sem padrão persistido, BUY/SELL bilaterais."""

import tempfile
import time
from pathlib import Path
from unittest.mock import patch
import unittest

from apoio import FonteFake, LoggerSilencioso, candles_deterministicos, estado_mt5, janela
from jev_bot import candidatos, mercados, padroes
from jev_bot.execution.paper import Book
from jev_bot.orquestrador import Orquestrador
from jev_bot.types import Decision


class TestMatematicaLocal(unittest.TestCase):
    """O cálculo M1 qualifica a oportunidade; o repositório não decide."""

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
        orch.repositorio = padroes.RepositorioPadroes(Path(pasta) / "padroes", relogio=time.time)
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

    def test_oportunidade_por_matematica_chega_ao_jev_sem_padrao_persistido(self):
        with tempfile.TemporaryDirectory() as pasta:
            orch = self._orquestrador(pasta)
            self.assertEqual(orch.repositorio.validados("TESTE"), [])
            proposta = {"side_proposed": "BUY", "strategy_id": "s", "candidate_id": "s",
                        "status": "validado", "expectancy_net_pct": 0.4,
                        "sample_size": 10, "oos_n": 3, "oos_rate": 0.7, "reason_codes": []}
            with patch("jev_bot.deepseek.analisar",
                       side_effect=AssertionError("DeepSeek proibido")) as ds, \
                    patch.object(Orquestrador, "_propor_entrada",
                                 return_value=proposta), \
                    patch("jev_bot.orquestrador.jev.avaliar_entrada",
                          return_value={"approved": True, "reason": "ok"}) as jev:
                orch._executar_ciclo("c1")
                _drenar(orch)
            ds.assert_not_called()
            jev.assert_called_once()
            self.assertGreaterEqual(orch.contadores_ciclo.get("candidatos_locais", 0), 1)
            self.assertEqual(len(orch.book.fills), 1)
            self.assertEqual(orch.book.fills[0].action, "BUY")

    def test_buy_e_sell_avaliados_bilateralmente(self):
        regras = [
            {"tipo_regra": "quedas_consecutivas", "parametros": {"n": 3},
             "direcao": "BUY", "horizonte_minutos": 5, "strategy_id": "conditional_sequence"},
            {"tipo_regra": "altas_consecutivas", "parametros": {"n": 2},
             "direcao": "SELL", "horizonte_minutos": 5, "strategy_id": "conditional_sequence"},
        ]
        with tempfile.TemporaryDirectory() as pasta:
            orch = self._orquestrador(pasta)
            with patch("jev_bot.orquestrador.candidatos.regras_candidatas",
                       return_value=regras), \
                    patch("jev_bot.orquestrador.jev.avaliar_entrada",
                          return_value={"approved": False, "reason": "sem evidencia"}):
                orch._executar_ciclo("c1")
                _drenar(orch)
            self.assertEqual(orch.contadores_ciclo.get("buy_evaluated"), 1)
            self.assertEqual(orch.contadores_ciclo.get("sell_evaluated"), 1)
            direcoes = {r.get("direcao") for c, r in orch.eventos if c == "calc"
                        and r.get("etapa") == "validacao_local"}
            self.assertEqual(direcoes, {"BUY", "SELL"})

    def test_sem_hipotese_direcional_nao_chama_jev(self):
        """Sem direção do Python, o JEv não é chamado e nada é fabricado."""
        candles = [{"time": i * 60, "open": 100.0, "high": 100.01, "low": 99.99,
                    "close": 100.0, "tick_volume": 1} for i in range(3000)]
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
            orch.fonte = FonteFake(estados=[estado_mt5("TESTE", closes=[100.0] * 100)],
                                   cotacoes={"TESTE": (99.995, 100.005, time.time())},
                                   janelas={"TESTE": janela(candles)})
            orch.fonte.janela_m1 = lambda simbolo, **_k: janela(candles)
            with patch.object(Orquestrador, "_propor_entrada", return_value=None), \
                    patch("jev_bot.orquestrador.jev.avaliar_entrada") as jev:
                orch._executar_ciclo("c1")
                _drenar(orch)
            jev.assert_not_called()
            self.assertEqual(len(orch.book.fills), 0)
            motivos = {r.get("motivo") for c, r in orch.eventos if c == "oportunidade"}
            self.assertIn("SEM_ENTRADA_PYTHON", motivos)


def _drenar(orch):
    limite = time.monotonic() + 3
    while (orch.analises_pendentes or orch.pendentes_jev) and time.monotonic() < limite:
        orch._drenar_analises(f"ciclo{orch.ciclo}")
        orch._processar_resultados_jev()
        time.sleep(.005)
    orch._processar_resultados_jev()

if __name__ == "__main__":
    unittest.main(verbosity=2)
