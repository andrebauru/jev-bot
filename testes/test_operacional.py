"""Testes operacionais do scalping M1: prioridade, fila DeepSeek e política.

Cobre o prompt de arquitetura atualizada: caminho operacional sem
dependência síncrona do DeepSeek, motivo tipado por símbolo, campos
policy/paper, prioridade Operar, mudança estatisticamente relevante como
gatilho de descoberta e calibração empírica (Brier) das probabilidades.
"""

import tempfile
import time
from pathlib import Path
from unittest.mock import Mock, patch
import unittest

from apoio import (FonteFake, LoggerSilencioso, candles_deterministicos,
                   estado_mt5, janela)
from jev_bot import candidatos, mercados, padroes
from jev_bot.execution.paper import Book
from jev_bot.orquestrador import Orquestrador
from jev_bot.types import Decision


class TestMudancaRelevante(unittest.TestCase):
    """Gatilho de descoberta por mudança estatística relevante."""

    RESUMO = {"movimento_1min": {"media_pct": 0.01}, "retorno_janela_pct": 1.0,
              "custo_spread_pct": 0.05}

    def test_ruido_de_um_candle_nao_aciona(self):
        """Deltas pequenos (ruído) não disparam a descoberta."""
        atual = {"movimento_1min": {"media_pct": 0.02}, "retorno_janela_pct": 1.1,
                 "custo_spread_pct": 0.05}
        self.assertFalse(candidatos.mudanca_relevante(self.RESUMO, atual))

    def test_mudanca_relevante_aciona(self):
        """Movimento médio alterado além do limiar aciona a descoberta."""
        atual = {"movimento_1min": {"media_pct": 0.10}, "retorno_janela_pct": 1.0,
                 "custo_spread_pct": 0.05}
        self.assertTrue(candidatos.mudanca_relevante(self.RESUMO, atual))

    def test_primeira_referencia_nao_aciona(self):
        """Sem referência anterior: revisão periódica por TTL cobre."""
        self.assertFalse(candidatos.mudanca_relevante(None, self.RESUMO))


class TestCalibracaoBrier(unittest.TestCase):
    """Probabilidades do JEv só viram probabilidade de lucro com evidência."""

    def test_amostra_insuficiente_nao_calibrada(self):
        trades = [{"probability": .9, "profit": 5, "status": "CLOSED"}]
        resultado = padroes.calibracao_brier(trades, minimo=20)
        self.assertEqual(resultado["status"], "nao_calibrada")
        self.assertEqual(resultado["n"], 1)

    def test_brier_calculado_com_amostra_suficiente(self):
        trades = [{"probability": 1.0, "profit": 5, "status": "CLOSED"}] * 10 + \
                 [{"probability": 0.0, "profit": 5, "status": "CLOSED"}] * 10
        resultado = padroes.calibracao_brier(trades, minimo=20)
        self.assertEqual(resultado["status"], "calibrada")
        self.assertEqual(resultado["n"], 20)
        self.assertEqual(resultado["brier"], 0.5)

    def test_abertas_e_sem_probabilidade_sao_ignoradas(self):
        trades = [{"probability": .9, "profit": 5, "status": "OPEN"},
                  {"profit": 5, "status": "CLOSED"}]
        resultado = padroes.calibracao_brier(trades, minimo=1)
        self.assertEqual(resultado["n"], 0)


class TestPrioridadeOperar(unittest.TestCase):
    """Ativos com Operar são avaliados antes dos demais monitorados."""

    def _orquestrador(self, pasta):
        arquivo = Path(pasta) / "mercados.json"
        selecao = {"selecao": {"C": {f"S{i}": {"monitorar": True, "operar": i % 2 == 0}
                                     for i in range(6)}}}
        mercados.salvar(selecao, arquivo)
        orch = Orquestrador(banco=Path(pasta) / "db.json",
                            pasta_dados=Path(pasta) / "padroes",
                            caminho_selecao=arquivo, relogio=time.time)
        orch.logger = LoggerSilencioso()
        orch.book = Book.load(Path(pasta) / "db.json", "USD")
        orch.book.active = True
        orch.repositorio = padroes.RepositorioPadroes(Path(pasta) / "padroes",
                                                      relogio=time.time)
        orch.orcamento = Mock()
        orch.orcamento.consumo.return_value = {}
        orch.eventos = []
        orch.publicar = lambda canal, registro: orch.eventos.append((canal, registro))
        orch.processados = []
        original = orch._processar_ativo

        def contando(estado, id_ciclo):
            orch.processados.append(estado.symbol)
            return original(estado, id_ciclo)

        orch._processar_ativo = contando
        orch.fonte = FonteFake(estados=[estado_mt5(f"S{i}") for i in range(6)],
                               janelas={f"S{i}": janela(candles_deterministicos(200))
                                        for i in range(6)},
                               cotacoes={f"S{i}": (99.99, 100.0, time.time())
                                         for i in range(6)})
        return orch

    def test_operar_antes_de_monitorar_apenas(self):
        """Operar=true é processado antes dos só-monitorar no mesmo ciclo."""
        with tempfile.TemporaryDirectory() as pasta:
            orch = self._orquestrador(pasta)
            patch_jev = patch("jev_bot.orquestrador.jev.decide", return_value=Decision("TESTE", "HOLD", .5, .5))
            patch_jev.start()
            self.addCleanup(patch_jev.stop)
            patch_jev = patch("jev_bot.orquestrador.jev.decide", return_value=Decision("TESTE", "HOLD", .5, .5))
            patch_jev.start()
            self.addCleanup(patch_jev.stop)
            orch._executar_ciclo("c1")
            self.assertEqual(len(orch.processados), 6)
            operaveis = [s for s in orch.processados if int(s[1:]) % 2 == 0]
            apenas_monitorar = [s for s in orch.processados if int(s[1:]) % 2 == 1]
            self.assertEqual(len(operaveis), 3)
            self.assertEqual(len(apenas_monitorar), 3)
            ultima_operavel = max(orch.processados.index(s) for s in operaveis)
            primeira_monitor = min(orch.processados.index(s) for s in apenas_monitorar)
            self.assertLess(ultima_operavel, primeira_monitor)


class TestDeepSeekAssincrono(unittest.TestCase):
    """Descoberta fora da rota de decisão: fila pós-varredura, por evento."""

    def setUp(self):
        """Bloqueia o transporte de rede das IAs (nenhum token pago)."""
        for alvo in ("jev_bot.deepseek.urlopen", "jev_bot.jev.urlopen"):
            guarda = patch(alvo, side_effect=AssertionError("rede bloqueada em teste"))
            guarda.start()
            self.addCleanup(guarda.stop)

    def _orquestrador(self, pasta):
        arquivo = Path(pasta) / "mercados.json"
        mercados.salvar({"selecao": {"C": {"TESTE": {"monitorar": True,
                                                     "operar": True}}}}, arquivo)
        orch = Orquestrador(banco=Path(pasta) / "db.json",
                            pasta_dados=Path(pasta) / "padroes",
                            caminho_selecao=arquivo, relogio=time.time)
        orch.logger = LoggerSilencioso()
        orch.book = Book.load(Path(pasta) / "db.json", "USD")
        orch.book.active = True
        orch.repositorio = padroes.RepositorioPadroes(Path(pasta) / "padroes",
                                                      relogio=time.time)
        orch.orcamento = Mock()
        orch.orcamento.consumo.return_value = {}
        orch.orcamento.permitir.return_value = (True, "")
        orch.deepseek_configurado = True
        orch.eventos = []
        orch.publicar = lambda canal, registro: orch.eventos.append((canal, registro))
        candles = candles_deterministicos()
        orch.fonte = FonteFake(estados=[estado_mt5(closes=[c["close"] for c in candles[-100:]])],
                               cotacoes={"TESTE": (99.995, 100.005, time.time())},
                               janelas={"TESTE": janela(candles)})
        orch.fonte.janela_m1 = Mock(return_value=janela(candles))
        orch.fonte.incremento_m1 = Mock(return_value=None)
        return orch

    def test_decisao_avaliada_antes_da_fila_deepseek(self):
        """No ciclo, a avaliação de oportunidades roda antes da descoberta."""
        with tempfile.TemporaryDirectory() as pasta:
            orch = self._orquestrador(pasta)
            patch_jev = patch("jev_bot.orquestrador.jev.decide", return_value=Decision("TESTE", "HOLD", .5, .5))
            patch_jev.start()
            self.addCleanup(patch_jev.stop)
            patch_jev = patch("jev_bot.orquestrador.jev.decide", return_value=Decision("TESTE", "HOLD", .5, .5))
            patch_jev.start()
            self.addCleanup(patch_jev.stop)
            ordem = []
            original_op = orch._oportunidade
            original_ds = orch._etapa_deepseek

            def op(estado, id_analise):
                ordem.append("oportunidade")
                return original_op(estado, id_analise)

            def ds(*_a, **_k):
                ordem.append("deepseek")
                return False

            orch._oportunidade = op
            orch._etapa_deepseek = ds
            orch._executar_ciclo("c1")
            self.assertIn("oportunidade", ordem)
            self.assertIn("deepseek", ordem)
            self.assertLess(ordem.index("oportunidade"), ordem.index("deepseek"),
                            "descoberta deve rodar após as decisões do ciclo")

    def test_mudanca_relevante_aciona_fila_sem_ttl(self):
        """Mudança estatisticamente relevante agenda a descoberta antes do TTL."""
        with tempfile.TemporaryDirectory() as pasta:
            orch = self._orquestrador(pasta)
            patch_jev = patch("jev_bot.orquestrador.jev.decide", return_value=Decision("TESTE", "HOLD", .5, .5))
            patch_jev.start()
            self.addCleanup(patch_jev.stop)
            patch_jev = patch("jev_bot.orquestrador.jev.decide", return_value=Decision("TESTE", "HOLD", .5, .5))
            patch_jev.start()
            self.addCleanup(patch_jev.stop)
            orch.ultimo_deepseek["TESTE"] = time.time()  # TTL longe de vencer
            hipoteses = [{"tipo_regra": "quedas_consecutivas", "parametros": {"n": 2},
                          "direcao": "BUY", "horizonte_minutos": 5, "justificativa": "x"}]
            with patch("jev_bot.orquestrador.candidatos.mudanca_relevante",
                       return_value=True), \
                    patch("jev_bot.orquestrador.deepseek.analisar",
                          return_value=(hipoteses, [], {"tokens": 10}, 0)) as analisar:
                orch._executar_ciclo("c1")
            analisar.assert_called()  # descoberta executada na fila pós-varredura
            self.assertGreaterEqual(orch.contadores_ciclo.get("deepseek_enviados", 0), 1,
                                    "mudança relevante deve agendar descoberta apesar do TTL")

    def test_ruido_nao_aciona_e_ttl_cobre(self):
        """Sem mudança relevante e dentro do TTL: fila vazia (sem gasto)."""
        with tempfile.TemporaryDirectory() as pasta:
            orch = self._orquestrador(pasta)
            patch_jev = patch("jev_bot.orquestrador.jev.decide", return_value=Decision("TESTE", "HOLD", .5, .5))
            patch_jev.start()
            self.addCleanup(patch_jev.stop)
            patch_jev = patch("jev_bot.orquestrador.jev.decide", return_value=Decision("TESTE", "HOLD", .5, .5))
            patch_jev.start()
            self.addCleanup(patch_jev.stop)
            orch.ultimo_deepseek["TESTE"] = time.time()  # TTL longe de vencer
            with patch("jev_bot.orquestrador.candidatos.mudanca_relevante",
                       return_value=False):
                orch._executar_ciclo("c1")
            self.assertEqual(orch.fila_deepseek, [])
            self.assertIsNone(orch.contadores_ciclo.get("deepseek_enviados"))


class TestDecisionTipada(unittest.TestCase):
    """Decisão JEv com metadados: modelo, latência e calibração marcada."""

    def test_reasons_carregam_modelo_latencia_calibracao(self):
        resposta = Mock()
        resposta.__enter__ = Mock(return_value=resposta)
        resposta.__exit__ = Mock(return_value=False)
        corpo = __import__("json").dumps({"answers": {"action": {
            "type": "choice", "choice": "BUY",
            "probabilities": {"BUY": .8, "SELL": .1, "HOLD": .1, "AVOID": 0},
            "confidence": .9}}}).encode()
        resposta.read.return_value = corpo
        from apoio import estado_mt5
        from jev_bot import jev
        estado = estado_mt5(closes=[100.0] * 25)
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "k"}, clear=True), \
                patch("jev_bot.jev.carregar_ambiente"), \
                patch("jev_bot.jev.urlopen", return_value=resposta):
            decisao = jev.decide(estado)
        self.assertEqual(decisao.reasons["modelo"], "typesafe/jev-1.13")
        self.assertGreaterEqual(decisao.reasons["latencia_ms"], 0)
        self.assertEqual(decisao.reasons["calibracao"], "nao_calibrada")


class TestPoliticaPaper(unittest.TestCase):
    """Campos policy/paper: ALLOW/VETO/PENDING e OPENED/NOT_OPENED."""

    def _orquestrador(self, pasta):
        arquivo = Path(pasta) / "mercados.json"
        mercados.salvar({"selecao": {"C": {"TESTE": {"monitorar": True,
                                                     "operar": True}}}}, arquivo)
        orch = Orquestrador(banco=Path(pasta) / "db.json",
                            pasta_dados=Path(pasta) / "padroes",
                            caminho_selecao=arquivo, relogio=time.time)
        orch.logger = LoggerSilencioso()
        orch.book = Book.load(Path(pasta) / "db.json", "USD")
        orch.book.active = True
        orch.book.prices_valid = True
        orch.repositorio = padroes.RepositorioPadroes(Path(pasta) / "padroes",
                                                      relogio=time.time)
        orch.orcamento = Mock()
        orch.orcamento.consumo.return_value = {}
        orch.eventos = []
        orch.publicar = lambda canal, registro: orch.eventos.append((canal, registro))
        candles = candles_deterministicos()
        closes = [c["close"] for c in candles[-100:]]
        orch.fonte = FonteFake(estados=[estado_mt5(closes=closes)],
                               cotacoes={"TESTE": (99.995, 100.005, time.time())},
                               janelas={"TESTE": janela(candles)})
        v = padroes.validar({"tipo_regra": "quedas_consecutivas", "parametros": {"n": 3},
                             "direcao": "BUY", "horizonte_minutos": 5}, candles, 0.02)
        registro = orch.repositorio.registrar(
            "TESTE", {"tipo_regra": "quedas_consecutivas", "parametros": {"n": 3},
                      "direcao": "BUY", "horizonte_minutos": 5, "justificativa": "x"},
            v, "local", janela(candles))
        registro["status"] = "validado"
        orch.repositorio.salvar("TESTE", orch.repositorio.carregar("TESTE"))
        return orch, estado_mt5(closes=closes)

    def test_sim_com_risco_allow_e_paper_opened(self):
        with tempfile.TemporaryDirectory() as pasta:
            orch, estado = self._orquestrador(pasta)
            with patch("jev_bot.orquestrador.jev.decide",
                       return_value=Decision("TESTE", "BUY", .9, .9)):
                orch._oportunidade(estado, "id")
            sim = [r for c, r in orch.eventos if c == "decisao"
                   and r.get("decisao") == "SIM"][-1]
            self.assertEqual(sim["policy"], "ALLOW")
            self.assertEqual(sim["paper"], "OPENED")
            self.assertEqual(sim["direcao"], "BUY")

    def test_sim_vetado_pelo_risco_policy_veto(self):
        with tempfile.TemporaryDirectory() as pasta:
            orch, estado = self._orquestrador(pasta)
            gate = Mock(verdict="SKIP", reason="Limite de drawdown", volume=0,
                        entry=0, stop=0, target=0, risk_amount=0, margin=0)
            with patch("jev_bot.orquestrador.risk.check", return_value=gate), \
                    patch("jev_bot.orquestrador.jev.decide",
                          return_value=Decision("TESTE", "BUY", .9, .9)):
                orch._oportunidade(estado, "id")
            veto = [r for c, r in orch.eventos if c == "decisao"
                    and r.get("decisao") == "SIM"][-1]
            self.assertEqual(veto["policy"], "VETO")
            self.assertEqual(veto["paper"], "NOT_OPENED")
            self.assertEqual(orch.contadores_ciclo.get("risco_bloqueado"), 1)
            self.assertEqual(len(orch.book.fills), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
