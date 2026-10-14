"""Testes do orquestrador: cadência, pipeline, seleção, cancelamento e memória."""

import json
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import unittest

from apoio import (FonteFake, LoggerSilencioso, candles_sinteticos, estado_mt5,
                   janela)
from jev_bot import deepseek, mercados, padroes
from jev_bot.execution.paper import Book, Fill
from jev_bot.orquestrador import (JANELA_TTL_S, LOTE_DOWNLOAD, Orquestrador,
                                  TENTATIVA_JEV_S)
from jev_bot.motor_matematico import estatisticas
from jev_bot.types import Decision


class TestEmergencia(unittest.TestCase):
    """Fechamento de emergência confirma resultado e relata falhas."""

    def _book_com_posicao(self):
        book = Book(active=True, prices_valid=True)
        estado = estado_mt5("TESTE")
        decisao = Decision("TESTE", "BUY", .9, .9)
        gate = SimpleNamespace(verdict="EXECUTE", entry=100.0, volume=0.1, stop=99.5,
                               target=101.0, risk_amount=50.0, margin=10.0)
        book.execute(decisao, estado, gate)
        return book, estado

    def test_fecha_ao_preco_observado(self):
        book, _ = self._book_com_posicao()
        fonte = FonteFake(cotacoes={"TESTE": (100.5, 100.6, time.time())})
        fechadas, falhas = book.fechar_total(fonte)
        self.assertEqual(len(fechadas), 1)
        self.assertEqual(falhas, [])
        self.assertEqual(book.fills, [])
        self.assertEqual(book.log[-1]["reason"], "EMERGENCIA")
        self.assertEqual(book.trades[0]["status"], "CLOSED")

    def test_sem_cotacao_nao_encerra(self):
        book, _ = self._book_com_posicao()
        fonte = FonteFake(falhas={"TESTE"})
        fechadas, falhas = book.fechar_total(fonte)
        self.assertEqual(fechadas, [])
        self.assertEqual(len(falhas), 1)
        self.assertEqual(len(book.fills), 1)
        self.assertEqual(book.trades[0]["status"], "OPEN")


class TestOrquestrador(unittest.TestCase):
    """Cadência, pipeline, travas de risco e emergência com dobro local."""

    def _orquestrador(self, pasta, estados=(), janelas=None, cotacoes=None):
        relogio = [time.time()]
        arquivo = Path(pasta) / "mercados.json"
        mercados.salvar({"selecao": {"C": {"TESTE": {"monitorar": True, "operar": True}}}}, arquivo)
        orch = Orquestrador(banco=Path(pasta) / "db.json",
                            pasta_dados=Path(pasta) / "padroes",
                            caminho_selecao=arquivo,
                            relogio=lambda: relogio[0])
        orch.logger = LoggerSilencioso()
        orch.fonte = FonteFake(estados=estados, janelas=janelas, cotacoes=cotacoes)
        orch.book = Book.load(Path(pasta) / "db.json", "USD")
        orch.book.active = True
        orch.book.prices_valid = True
        orch.book.day_start = orch.book.cash
        orch.repositorio = padroes.RepositorioPadroes(orch.pasta_dados, relogio=lambda: relogio[0])
        orch.orcamento = deepseek.Orcamento(orch.pasta_dados, 50, 500_000, relogio=lambda: relogio[0])
        orch.eventos = []
        orch.publicar = lambda canal, registro: orch.eventos.append((canal, registro))
        return orch, relogio

    def test_proximo_alvo_troca_com_posicoes(self):
        with tempfile.TemporaryDirectory() as pasta:
            orch, _ = self._orquestrador(pasta)
            agora = orch.relogio()
            alvo, base = orch._proximo_alvo(agora)
            self.assertIn("fechamento do M1", base)
            self.assertEqual(alvo, (int(agora // 60) + 1) * 60 + 2)
            orch.book.fills.append(Fill("TESTE", "BUY", 100, .01, 99, 101, 5, 10, ("USD",), 100))
            alvo, base = orch._proximo_alvo(agora)
            self.assertIn("15min", base)
            self.assertEqual(alvo, agora + orch.intervalo_com_posicoes)

    def test_limite_aplicado_persiste_no_motor(self):
        with tempfile.TemporaryDirectory() as pasta:
            orch, _ = self._orquestrador(pasta)
            orch._acao_limites({"risk_per_trade": .01, "max_total_risk": .1, "max_drawdown": .08})
            self.assertEqual(orch.book.limits.risk_per_trade, .01)
            self.assertEqual(orch.book.limits.max_drawdown, .08)
            recarregado = Book.load(Path(pasta) / "db.json", "USD")
            self.assertEqual(recarregado.limits.risk_per_trade, .01)
            with self.assertRaises(RuntimeError):
                orch._acao_limites({"risk_per_trade": .5, "max_total_risk": .1, "max_drawdown": .08})

    def test_parado_por_risco_e_liberacao_manual(self):
        with tempfile.TemporaryDirectory() as pasta:
            orch, _ = self._orquestrador(pasta)
            orch.book.daily_blocked = True
            orch._monitorar()
            self.assertTrue(orch.parado_risco)
            self.assertTrue(any(c == "estado" and r["estado"] == "BLOQUEADO_POR_RISCO"
                                for c, r in orch.eventos))
            antes = len(orch.book.fills)
            orch._oportunidade(estado_mt5(), "id")
            self.assertEqual(len(orch.book.fills), antes)
            orch._acao_liberar_risco()
            self.assertFalse(orch.parado_risco)
            self.assertFalse(orch.book.daily_blocked)
            registros = [r for c, r in orch.eventos if c == "log" and "Liberação manual" in r["mensagem"]]
            self.assertEqual(len(registros), 1)

    def test_emergencia_fecha_e_bloqueia(self):
        with tempfile.TemporaryDirectory() as pasta:
            orch, _ = self._orquestrador(pasta)
            estado = estado_mt5()
            decisao = Decision("TESTE", "BUY", .9, .9)
            gate = SimpleNamespace(verdict="EXECUTE", entry=100.0, volume=0.1, stop=99.5,
                                   target=101.0, risk_amount=50.0, margin=10.0)
            orch.book.execute(decisao, estado, gate)
            orch.fonte = FonteFake(cotacoes={"TESTE": (100.2, 100.3, time.time())})
            orch._acao_emergencia()
            self.assertTrue(orch.emergencia_ativa)
            self.assertFalse(orch.book.active)
            self.assertEqual(orch.book.fills, [])
            decisoes = [r for c, r in orch.eventos if c == "decisao"]
            self.assertTrue(any(r["decisao"] == "ENCERRADA" for r in decisoes))
            orch.book.active = True  # reentrada apenas para criar segunda posição de teste
            orch.book.execute(decisao, estado, gate)
            orch.fonte = FonteFake(falhas={"TESTE"})
            orch._executar_emergencia()
            self.assertEqual(len(orch.book.fills), 1)
            self.assertTrue(any(r["decisao"] == "PENDENTE" for c, r in orch.eventos if c == "decisao"))

    def test_oportunidade_validada_passa_por_jev_e_risco(self):
        with tempfile.TemporaryDirectory() as pasta:
            closes = [100.0 + (i % 7) * .1 for i in range(400)]
            for j, preco in enumerate((105.0, 104.0, 103.0, 102.0, 101.0, 100.0), start=394):
                closes[j] = preco
            orch, _ = self._orquestrador(pasta, cotacoes={"TESTE": (99.995, 100.005, time.time())})
            candles = [{"time": time.time() - 60 * (len(closes) - i), "open": 100, "high": 100.5,
                        "low": 99.5, "close": c, "tick_volume": 10} for i, c in enumerate(closes)]
            validacao = {"status": "validado", "motivo": "ok",
                         "total": {"eventos": 50, "acertos": 40, "taxa": .8, "media_pct": .1,
                                   "mediana_pct": .1, "desvio_pct": .2},
                         "amostra": {"eventos": 35}, "oos": {"eventos": 15, "taxa": .8},
                         "wilson_inferior": .5, "custo_pct": .02}
            registro = orch.repositorio.registrar(
                "TESTE", {"tipo_regra": "quedas_consecutivas", "parametros": {"n": 3},
                          "direcao": "BUY", "horizonte_minutos": 5}, validacao, "deepseek",
                janela(candles))
            registro["status"] = "validado"
            orch.repositorio.salvar("TESTE", orch.repositorio.carregar("TESTE"))
            estado = estado_mt5(closes=closes)
            decisao = Decision("TESTE", "BUY", .9, .9)
            with patch("jev_bot.orquestrador.jev.decide", return_value=decisao) as chamada:
                orch._oportunidade(estado, "id-teste")
            chamada.assert_called_once()
            self.assertEqual(len(orch.book.fills), 1)
            decisoes = [r for c, r in orch.eventos if c == "decisao"]
            self.assertTrue(any(r["decisao"].startswith("SIM") for r in decisoes))
            with patch("jev_bot.orquestrador.jev.decide", return_value=decisao) as chamada:
                orch._oportunidade(estado, "id-teste")
            chamada.assert_not_called()
            self.assertEqual(len(orch.book.fills), 1)

    def test_jev_recusa_bloqueia_entrada(self):
        with tempfile.TemporaryDirectory() as pasta:
            closes = [105.0, 104.0, 103.0, 102.0, 101.0, 100.0]
            orch, _ = self._orquestrador(pasta, cotacoes={"TESTE": (99.995, 100.005, time.time())})
            candles = [{"time": time.time() - 60 * (len(closes) - i), "open": 100, "high": 100.5,
                        "low": 99.5, "close": c, "tick_volume": 10} for i, c in enumerate(closes)]
            validacao = {"status": "validado", "motivo": "ok",
                         "total": {"eventos": 50, "acertos": 40, "taxa": .8, "media_pct": .1,
                                   "mediana_pct": .1, "desvio_pct": .2},
                         "amostra": {"eventos": 35}, "oos": {"eventos": 15, "taxa": .8},
                         "wilson_inferior": .5, "custo_pct": .02}
            registro = orch.repositorio.registrar(
                "TESTE", {"tipo_regra": "quedas_consecutivas", "parametros": {"n": 3},
                          "direcao": "BUY", "horizonte_minutos": 5}, validacao, "deepseek",
                janela(candles))
            registro["status"] = "validado"
            orch.repositorio.salvar("TESTE", orch.repositorio.carregar("TESTE"))
            estado = estado_mt5(closes=closes)
            decisao = Decision("TESTE", "HOLD", .5, .5)
            with patch("jev_bot.orquestrador.jev.decide", return_value=decisao):
                orch._oportunidade(estado, "id")
            decisoes = [r for c, r in orch.eventos if c == "decisao"]
            self.assertTrue(any(r["decisao"] == "HOLD" for r in decisoes))
            self.assertEqual(orch.contadores_ciclo.get("jev_hold"), 1)
            self.assertEqual(orch.book.fills, [])
            with patch("jev_bot.orquestrador.jev.decide", side_effect=RuntimeError("api fora")):
                estado2 = estado_mt5(closes=closes)
                estado2.candles[-1]["time"] += 60
                orch._oportunidade(estado2, "id2")
            self.assertGreater(orch.proxima_tentativa_jev, orch.relogio() + TENTATIVA_JEV_S - 1)

    def test_deepseek_nao_participa_do_fluxo_operacional(self):
        """Ciclos locais n?o enfileiram nem consultam o cliente legado."""
        with tempfile.TemporaryDirectory() as pasta:
            orch, _ = self._orquestrador(pasta)
            with patch("jev_bot.deepseek.analisar",
                       side_effect=AssertionError("DeepSeek desativado")) as analisar:
                orch._executar_ciclo("c1")
            analisar.assert_not_called()
            self.assertEqual(orch.resumo()["deepseek_calls"], 0)
            self.assertFalse(hasattr(orch, "fila_deepseek"))

    def test_janela_cache_ttl_e_memoria_limitada(self):
        """Refresh respeita TTL e o cache mantém só a cauda, nunca a janela cheia."""
        with tempfile.TemporaryDirectory() as pasta:
            candles = candles_sinteticos(5000, seed=9)
            j = janela(candles)
            orch, relogio = self._orquestrador(pasta, janelas={"TESTE": j})
            fonte = orch.fonte
            fonte.janela_m1 = Mock(return_value=j)
            estado = estado_mt5(closes=[c["close"] for c in candles[-6:]])
            estado.candles[-1]["time"] = candles[-1]["time"]
            orch._processar_ativo(estado, "c1")
            self.assertEqual(fonte.janela_m1.call_count, 1)
            cache = orch.dados_ativo["TESTE"]
            self.assertNotIn("janela", cache)  # janela completa descartada
            self.assertLessEqual(len(cache["tail"]), 720)
            self.assertIsNone(cache["stats"].get("janela"))
            orch._processar_ativo(estado, "c1")
            self.assertEqual(fonte.janela_m1.call_count, 1)  # cache TTL
            relogio[0] += JANELA_TTL_S + 1
            orch._processar_ativo(estado, "c1")
            self.assertEqual(fonte.janela_m1.call_count, 2)  # expirou: nova coleta

    def test_download_paginado_com_lotes_no_motor(self):
        """A coleta usa lotes curtos com cancelamento e progresso."""
        with tempfile.TemporaryDirectory() as pasta:
            candles = candles_sinteticos(9000, seed=4)
            j = janela(candles)
            orch, _ = self._orquestrador(pasta, janelas={"TESTE": j})
            progressos = []
            cancelados = []

            def fonte_lote(simbolo, dias=30, lote=4320, cancelado=None, progresso=None):
                cancelados.append(cancelado)
                chamadas[0] += 1
                if progresso:
                    solicitado = dias * 1440
                    progressos.append(solicitado)
                    progresso(solicitado, solicitado)
                return j

            orch.fonte.janela_m1 = fonte_lote
            chamadas = [0]
            orch._coletar_janela("TESTE", "id")
            self.assertEqual(chamadas[0], 1)
            self.assertGreater(len(progressos), 0)
            self.assertTrue(all(callable(c) for c in cancelados))
            # O cancelamento cooperativo inclui emergência e encerramento.
            cancelado = cancelados[0]
            orch.encerrar.set()
            self.assertTrue(cancelado())
            orch.encerrar.clear()
            orch.evento_emergencia.set()
            self.assertTrue(cancelado())
            orch.evento_emergencia.clear()
            self.assertFalse(cancelado())

    def test_download_lote_configurado(self):
        """O lote de paginação é curto (LOTE_DOWNLOAD), não uma chamada gigante."""
        self.assertLessEqual(LOTE_DOWNLOAD, 4320)

    def test_selecao_impede_analise_de_desmarcado(self):
        """Não monitorado: sem download, sem estatísticas, sem DeepSeek."""
        with tempfile.TemporaryDirectory() as pasta:
            j = janela(candles_sinteticos(3000, seed=8))
            orch, _ = self._orquestrador(pasta, janelas={"FORA": j})
            orch.fonte.janela_m1 = Mock(return_value=j)
            mercados.salvar({"selecao": {"C": {"TESTE": {"monitorar": True, "operar": True}}}},
                            Path(pasta) / "mercados.json")
            orch.recarregar_selecao_arquivo()
            estado_fora = estado_mt5("FORA")
            orch.fonte.estados = lambda: iter([estado_fora])
            orch._executar_ciclo("c1")
            orch.fonte.janela_m1.assert_not_called()
            self.assertEqual([r for c, r in orch.eventos if c == "calc" and r.get("simbolo") == "FORA"],
                             [])
            self.assertNotIn("FORA", orch.dados_ativo)

    def test_selecao_operar_exige_monitorar_para_jev(self):
        """Monitorado sem Operar: análise ocorre, mas JEv nunca é consultado."""
        with tempfile.TemporaryDirectory() as pasta:
            closes = [105.0, 104.0, 103.0, 102.0, 101.0, 100.0]
            orch, _ = self._orquestrador(pasta, cotacoes={"TESTE": (99.995, 100.005, time.time())})
            mercados.salvar({"selecao": {"C": {"TESTE": {"monitorar": True, "operar": False}}}},
                            Path(pasta) / "mercados.json")
            orch.recarregar_selecao_arquivo()
            candles = [{"time": time.time() - 60 * (len(closes) - i), "open": 100, "high": 100.5,
                        "low": 99.5, "close": c, "tick_volume": 10} for i, c in enumerate(closes)]
            validacao = {"status": "validado", "motivo": "ok",
                         "total": {"eventos": 50, "acertos": 40, "taxa": .8, "media_pct": .1,
                                   "mediana_pct": .1, "desvio_pct": .2},
                         "amostra": {"eventos": 35}, "oos": {"eventos": 15, "taxa": .8},
                         "wilson_inferior": .5, "custo_pct": .02}
            registro = orch.repositorio.registrar(
                "TESTE", {"tipo_regra": "quedas_consecutivas", "parametros": {"n": 3},
                          "direcao": "BUY", "horizonte_minutos": 5}, validacao, "deepseek",
                janela(candles))
            registro["status"] = "validado"
            orch.repositorio.salvar("TESTE", orch.repositorio.carregar("TESTE"))
            estado = estado_mt5(closes=closes)
            with patch("jev_bot.orquestrador.jev.decide") as chamada:
                orch._oportunidade(estado, "id")
            chamada.assert_not_called()
            self.assertEqual(orch.book.fills, [])

    def test_selecao_mudanca_durante_execucao(self):
        """Mudança de seleção em arquivo é aplicada no próximo ciclo, com registro."""
        with tempfile.TemporaryDirectory() as pasta:
            orch, _ = self._orquestrador(pasta)
            monitorados_antes, operaveis_antes = orch.selecao.contagens()
            mercados.salvar({"selecao": {"C": {"TESTE": {"monitorar": True, "operar": False}}}},
                            Path(pasta) / "mercados.json")
            orch.recarregar_selecao_arquivo()
            monitorados, operaveis = orch.selecao.contagens()
            self.assertEqual((monitorados, operaveis), (1, 0))
            self.assertEqual(operaveis_antes, 1)
            self.assertNotEqual(operaveis, operaveis_antes)
            registros = [r for c, r in orch.eventos if c == "log" and "Seleção de mercados" in r["mensagem"]]
            self.assertTrue(registros)

    def test_sobreposicao_de_ciclo_e_adiada(self):
        """Se um ciclo ainda roda, o próximo é adiado com registro explícito."""
        with tempfile.TemporaryDirectory() as pasta:
            orch, _ = self._orquestrador(pasta)
            orch.ciclo_ativo = True
            orch._ciclo()
            self.assertEqual(orch.ciclo, 0)
            self.assertTrue(any(c == "log" and "adiado" in r["mensagem"]
                                for c, r in orch.eventos))

    def test_cancelar_ciclo_aborta_download(self):
        """CANCELAR CICLO interrompe a coleta entre lotes, com registro."""
        with tempfile.TemporaryDirectory() as pasta:
            orch, _ = self._orquestrador(pasta)
            chamadas = []

            def fonte_cancelavel(simbolo, dias=30, lote=4320, cancelado=None, progresso=None):
                chamadas.append(1)
                if len(chamadas) > 1:
                    return janela(candles_sinteticos(1000))
                return None  # simulado: cancelado no meio

            orch.fonte.janela_m1 = fonte_cancelavel
            orch.evento_ciclo_cancelado.set()
            resultado = orch._coletar_janela("TESTE", "id")
            self.assertIsNone(resultado)
            self.assertTrue(orch._cancelado_ciclo())

    def test_loop_completo_executa_ciclos_com_dobro_local(self):
        """executar() roda a cadência real com MT5 simulado e sem APIs externas."""
        with tempfile.TemporaryDirectory() as pasta:
            closes = [100.0 + (i % 9) * .2 for i in range(300)]
            candles = [{"time": 1_700_000_000 + i * 60, "open": 100, "high": 100.5,
                        "low": 99.5, "close": c, "tick_volume": 10} for i, c in enumerate(closes)]
            j = janela(candles)
            estados = [estado_mt5(closes=closes)]
            fonte = FonteFake(estados=estados, janelas={"TESTE": j},
                              cotacoes={"TESTE": (99.995, 100.005, time.time())})

            class ContextoFonte:
                moeda = "USD"

                def __enter__(self):
                    return fonte

                def __exit__(self, *_):
                    return False

            arquivo = Path(pasta) / "mercados.json"
            mercados.salvar({"selecao": {"C": {"TESTE": {"monitorar": True, "operar": True}}}}, arquivo)
            orch = Orquestrador(banco=Path(pasta) / "db.json",
                                pasta_dados=Path(pasta) / "padroes",
                                caminho_selecao=arquivo, relogio=time.time)
            orch.logger = LoggerSilencioso()
            orch.intervalo_com_posicoes = 2
            orch.publicar = lambda canal, registro: orch.eventos.append((canal, registro))
            orch.eventos = []
            with patch("jev_bot.orquestrador.FonteMT5", return_value=ContextoFonte()), \
                    patch("jev_bot.orquestrador.jev.configuracao", return_value=("chave", "endpoint")), \
                    patch("jev_bot.deepseek.configuracao_chat",
                          side_effect=RuntimeError("sem chave no teste")), \
                    patch.object(Orquestrador, "_proximo_alvo",
                                 lambda self, agora: (agora + 1.0, "teste rápido")):
                orch.iniciar()  # INICIAR: habilita ciclo e entradas paper
                thread = threading.Thread(target=orch.executar, daemon=True)
                thread.start()
                limite = time.time() + 15
                while time.time() < limite and orch.ciclo < 2:
                    time.sleep(0.2)
                orch.parar()
                thread.join(15)
            self.assertFalse(thread.is_alive())
            self.assertGreaterEqual(orch.ciclo, 1)
            calc = [r for c, r in orch.eventos if c == "calc" and r.get("etapa") == "calculado"]
            self.assertTrue(any(r["simbolo"] == "TESTE" for r in calc),
                            "o ciclo deve calcular estatísticas do ativo")
            self.assertTrue(any(c == "trades" for c, r in orch.eventos))
            self.assertFalse(orch.book.active)
            self.assertTrue((Path(pasta) / "db.json").exists())

    def test_comandos_da_fila_sao_drenados(self):
        """iniciar/pausar/limites saem da fila e alteram o motor, sem resíduo."""
        with tempfile.TemporaryDirectory() as pasta:
            orch, _ = self._orquestrador(pasta)
            orch.iniciar()
            orch.pausar()
            orch.aplicar_limites(.01, .1, .08)
            with patch("jev_bot.orquestrador.jev.configuracao", return_value=("c", "e")):
                orch._processar_acoes()
            self.assertEqual(orch.acoes.qsize(), 0, "fila deve terminar vazia")
            self.assertTrue(orch.pausado, "o último comando (pausar) prevalece")
            self.assertFalse(orch.book.active)
            self.assertEqual(orch.book.limits.risk_per_trade, .01)
            orch.retomar()
            orch._processar_acoes()
            self.assertFalse(orch.pausado)
            self.assertTrue(orch.book.active)
            self.assertEqual(orch.acoes.qsize(), 0)

    def test_pausa_bloqueia_entradas_e_mantem_monitoramento(self):
        with tempfile.TemporaryDirectory() as pasta:
            orch, _ = self._orquestrador(pasta)
            orch._acao_pausar()
            self.assertTrue(orch.pausado)
            self.assertFalse(orch.book.active)
            orch._oportunidade(estado_mt5(), "id")
            self.assertEqual(orch.book.fills, [])
            contador = [0]

            def conta():
                contador[0] += 1
                return dict(balance=0, equity=0, profit=0, margin_free=0, currency="USD")

            orch.fonte.conta = conta
            orch.ultimo_monitor = 0
            orch._monitorar()
            self.assertGreaterEqual(contador[0], 1)

    def test_posicoes_supervisionadas_mesmo_desmarcado(self):
        """Desmarcar o símbolo não interrompe stops/P&L de posição aberta."""
        with tempfile.TemporaryDirectory() as pasta:
            orch, _ = self._orquestrador(pasta, cotacoes={"TESTE": (99.995, 100.005, time.time())})
            estado = estado_mt5()
            decisao = Decision("TESTE", "BUY", .9, .9)
            gate = SimpleNamespace(verdict="EXECUTE", entry=100.0, volume=0.1, stop=99.5,
                                   target=101.0, risk_amount=50.0, margin=10.0)
            orch.book.execute(decisao, estado, gate)
            mercados.salvar({"selecao": {"C": {"TESTE": {"monitorar": False, "operar": False}}}},
                            Path(pasta) / "mercados.json")
            orch.recarregar_selecao_arquivo()
            orch.ultimo_monitor = 0
            orch._monitorar()
            marcada = orch.book.fills[0]
            self.assertIsNotNone(marcada.price_now)
            self.assertEqual(marcada.mode if hasattr(marcada, "mode") else "paper", "paper")


if __name__ == "__main__":
    unittest.main(verbosity=2)
