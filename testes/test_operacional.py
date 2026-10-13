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


class TestDeepSeekDesativado(unittest.TestCase):
    """A rota do scanner ? local e nunca despacha/enfileira DeepSeek."""

    def test_ciclo_nao_consulta_ou_enfileira_deepseek(self):
        with tempfile.TemporaryDirectory() as pasta:
            arquivo = Path(pasta) / "mercados.json"
            mercados.salvar({"selecao": {"C": {"TESTE": {
                "monitorar": True, "operar": True}}}}, arquivo)
            orch = Orquestrador(banco=Path(pasta) / "db.json",
                                pasta_dados=Path(pasta) / "padroes",
                                caminho_selecao=arquivo, relogio=time.time)
            orch.logger = LoggerSilencioso()
            orch.book = Book.load(Path(pasta) / "db.json", "USD")
            orch.book.active = True
            orch.repositorio = padroes.RepositorioPadroes(Path(pasta) / "padroes",
                                                          relogio=time.time)
            orch.eventos = []
            orch.publicar = lambda canal, registro: orch.eventos.append((canal, registro))
            candles = candles_deterministicos()
            orch.fonte = FonteFake(estados=[estado_mt5(
                "TESTE", closes=[c["close"] for c in candles[-100:]])],
                cotacoes={"TESTE": (99.995, 100.005, time.time())},
                janelas={"TESTE": janela(candles)})
            with patch("jev_bot.orquestrador.deepseek.analisar",
                       side_effect=AssertionError("DeepSeek deve permanecer desativado")) as analisar:
                orch._executar_ciclo("c1")
            analisar.assert_not_called()
            self.assertFalse(hasattr(orch, "fila_deepseek"))
            self.assertEqual(orch.resumo()["deepseek_calls"], 0)
            self.assertFalse(any(c == "deepseek" for c, _ in orch.eventos))


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




class TestCiclosMulti(unittest.TestCase):
    """Ciclos múltiplos com IDs crescentes e timeout JEv."""

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

    def test_tres_ciclos_ids_crescentes(self):
        """Ciclos 1→2→3 registram run_id, despacho, latência e contadores."""
        with tempfile.TemporaryDirectory() as pasta:
            orch, estado = self._orquestrador(pasta)
            chamadas = []

            def decidir(_estado):
                chamadas.append(_estado.symbol)
                return Decision("TESTE", "HOLD", .5, .5)

            with patch("jev_bot.orquestrador.jev.decide",
                       side_effect=decidir):
                for numero in range(3):
                    candle = estado_mt5(closes=[c["close"] for c in
                                                candles_deterministicos()[-100:]])
                    candle.candles[-1]["time"] = estado.candles[-1]["time"] + numero * 60
                    orch.fonte.estados = lambda candle=candle: iter([candle])
                    orch._ciclo()
                limite = time.monotonic() + 2
                while orch.pendentes_jev and time.monotonic() < limite:
                    orch._processar_resultados_jev()
                    time.sleep(.005)
                orch._processar_resultados_jev()
            resumos_por_run = {}
            for canal, registro in orch.eventos:
                if canal == "resumo":
                    resumos_por_run[registro["run_id"]] = registro
            resumos = list(resumos_por_run.values())
            c1, c2, c3 = [r["ciclo"] for r in resumos]
            self.assertEqual((c1, c2, c3), (1, 2, 3))
            estados = [r.get("estado") for c, r in orch.eventos if c == "estado"]
            self.assertGreaterEqual(estados.count("ANALISANDO"), 2)
            self.assertEqual(len(orch.book.fills), 0)  # HOLD não executa
            decisoes = [r for canal, r in orch.eventos
                        if canal == "decisao" and r.get("decisao") == "HOLD"]
            self.assertEqual(len(chamadas), 3)
            self.assertEqual(len({r["run_id"] for r in resumos}), 3)
            self.assertEqual(len(decisoes), 3)
            for resumo, decisao in zip(resumos, decisoes):
                self.assertEqual(resumo["resultado"], "CONCLUIDO")
                self.assertEqual(resumo["funil"]["deepseek_calls"], 0)
                self.assertEqual(resumo["funil"]["jev_hold"], 1)
                self.assertEqual(resumo["funil"].get("aguardando_decisao", 0), 0)
                self.assertEqual(resumo["jev_pendentes"], 0)
                self.assertEqual(decisao["run_id"], resumo["run_id"])
                self.assertTrue(decisao["python_validated_at"])
                self.assertTrue(decisao["jev_dispatched_at"])
                self.assertGreaterEqual(decisao["latencia_detecao_ms"], 0)
                self.assertGreaterEqual(decisao["latencia_ms"], 0)

    def test_timeout_de_jev_nao_bloqueia_proximo_simbolo(self):
        """O worker de rede espera; o scanner percorre o ativo seguinte."""
        with tempfile.TemporaryDirectory() as pasta:
            orch, estado = self._orquestrador(pasta)
            mercados.salvar({"selecao": {"C": {
                "TESTE": {"monitorar": True, "operar": True},
                "SEGUNDO": {"monitorar": True, "operar": True}}}}, orch.caminho_selecao)
            orch._registrar_selecao()
            segundo = estado_mt5("SEGUNDO", closes=[c["close"] for c in
                                                      candles_deterministicos()[-100:]])
            ordem = []

            def estados(simbolo=None):
                ordem.append(f"fetch:{simbolo}")
                return iter([estado if simbolo == "TESTE" else segundo]
                            if simbolo in ("TESTE", "SEGUNDO") else [])

            orch.fonte.estados = estados
            orch.fonte.janela_m1 = lambda simbolo, **_kwargs: (
                janela(candles_deterministicos()) if simbolo == "TESTE" else None)
            import threading
            terminou = threading.Event()

            def jev_lento(_estado):
                time.sleep(.35)
                terminou.set()
                return Decision("TESTE", "HOLD", .5, .5)

            inicio = time.monotonic()
            with patch("jev_bot.orquestrador.jev.decide", side_effect=jev_lento):
                orch._ciclo()
                duracao_ciclo = time.monotonic() - inicio
                self.assertLess(duracao_ciclo, .30)
                self.assertIn("fetch:SEGUNDO", ordem)
                self.assertFalse(terminou.is_set(), "o ciclo não esperou a resposta de rede")
                self.assertEqual(len(orch.book.fills), 0)
                self.assertTrue(terminou.wait(2))
                orch._processar_resultados_jev()
            holds = [r for canal, r in orch.eventos
                     if canal == "decisao" and r.get("decisao") == "HOLD"]
            self.assertTrue(holds)

    def test_jev_timeout_resulta_sem_decisao(self):
        """JEv timeout: SEM_DECISAO; novo candle → JEv funciona."""
        with tempfile.TemporaryDirectory() as pasta:
            orch, estado = self._orquestrador(pasta)
            from apoio import candles_deterministicos
            candles = candles_deterministicos()
            with patch("jev_bot.orquestrador.jev.decide",
                       side_effect=RuntimeError("timeout JEv")):
                orch._oportunidade(estado, "id1")
            self.assertGreaterEqual(
                orch.contadores_ciclo.get("ia_indisponivel", 0), 1,
                "timeout JEv deve incrementar ia_indisponivel")
            self.assertEqual(len(orch.book.fills), 0)
            estado2 = estado_mt5(closes=[c["close"] for c in candles[-100:]])
            estado2.candles[-1]["time"] = estado.candles[-1]["time"] + 60
            with patch("jev_bot.orquestrador.jev.decide",
                       return_value=Decision("TESTE", "BUY", .9, .9)):
                motivo2 = orch._oportunidade(estado2, "id2")
            self.assertIn(motivo2, ("executada", "risco_bloqueado",
                                    "condicao_presente_sem_decisao"))
            self.assertEqual(orch.contadores_ciclo.get("ia_indisponivel"), 1)

if __name__ == "__main__":
    unittest.main(verbosity=2)
