"""Testes isolados de risco, MT5 e persistência. Execute python tests.py."""

from dataclasses import replace
from contextlib import redirect_stdout
import io
from datetime import datetime, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from urllib.error import HTTPError
from unittest.mock import Mock, patch

from jev_bot import jev
from jev_bot import configuracao
from jev_bot.cli import bloquear, main
from jev_bot.execution.paper import Book, dia_utc
from jev_bot.loop import run
from jev_bot.markets import FonteMT5
from jev_bot.painel import PainelTerminal, TelaFixa, desenhar, status_curto
from jev_bot.risk import Limits, check
from jev_bot.types import Decision, MarketState
from jev_bot.matematica import resumir


def estado(**alteracoes):
    """Constrói um contrato de teste com orçamento suficiente para o lote mínimo."""
    base = MarketState("EURUSD", "Forex", 100.005,
                       bid=100, ask=100.01, tick_time=1000,
                       stop_distance=2, loss_per_lot=100, margin_per_lot=1000,
                       volume_min=0.01, volume_max=10, volume_step=0.01,
                       trade_mode=4, currencies=("EUR", "USD"))
    return replace(base, **alteracoes)


class TestRisco(unittest.TestCase):
    """Verifica orçamento, bloqueios e valores inválidos sem chamadas externas."""

    def setUp(self):
        """Inicia carteira paper com marcação válida apenas neste teste."""
        self.book = Book(active=True, prices_valid=True, limits=Limits(risk_per_trade=.005,
                         max_total_risk=.02, max_currency_risk=.01, max_drawdown=.05, max_positions=3))
        self.state = estado()
        self.decision = Decision("EURUSD", "BUY", 0.9, 0.9)

    def avaliar(self, state=None, decision=None):
        """Executa a regra com relógio controlado."""
        return check(decision or self.decision, self.book, self.book.limits,
                     state or self.state, agora=1000)

    def test_dimensionamento(self):
        """Risco máximo de 50 gera 0,5 lote e stop obrigatório."""
        g = self.avaliar()
        self.assertEqual(g.verdict, "EXECUTE")
        self.assertEqual(g.volume, 0.5)
        self.assertLessEqual(g.risk_amount, 50)
        self.assertEqual(g.stop, 98.01)
        self.assertEqual(g.target, 104.01)

    def test_arredondamento_nao_aumenta_risco(self):
        """Um passo fracionário é sempre arredondado para baixo."""
        g = self.avaliar(estado(loss_per_lot=137, volume_step=0.03))
        self.assertLessEqual(g.risk_amount, 50)
        self.assertAlmostEqual(g.volume, 0.36)

    def test_rejeicoes(self):
        """Bloqueia cotação antiga, spread, contrato, direção e lote incompatível."""
        for dados in (dict(tick_time=800), dict(tick_time=1001), dict(ask=101),
                      dict(stop_distance=float("nan")), dict(loss_per_lot=0),
                      dict(volume_min=1), dict(trade_mode=0), dict(trade_mode=2),
                      dict(margin_per_lot=10000)):
            with self.subTest(dados=dados):
                self.assertEqual(self.avaliar(estado(**dados)).verdict, "SKIP")

    def test_decisoes_invalidas(self):
        """Não aceita fallback, baixa confiança, símbolos divergentes ou NaN."""
        for d in (replace(self.decision, action="HOLD"), replace(self.decision, source="offline"),
                  replace(self.decision, symbol="X"), replace(self.decision, confidence=0.2),
                  replace(self.decision, probability=float("nan"))):
            self.assertEqual(self.avaliar(decision=d).verdict, "SKIP")

    def test_pausa_e_precos(self):
        """Pausa e posições sem marcação bloqueiam novas entradas."""
        self.book.active = False
        self.assertEqual(self.avaliar().verdict, "SKIP")
        self.book.active, self.book.prices_valid = True, False
        self.assertEqual(self.avaliar().verdict, "SKIP")

    def test_perda_diaria_e_drawdown(self):
        """Perdas realizadas e drawdown impedem reinvestimento."""
        self.book.cash = 9800
        self.assertIn("diária", self.avaliar().reason)
        self.book.cash, self.book.day_start = 9400, 9400
        self.assertIn("drawdown", self.avaliar().reason)
        self.book.cash, self.book.high_water = 10000, 10000
        self.book.daily_blocked = True
        self.assertIn("Bloqueio diário", self.avaliar().reason)

    def test_duplicacao_limite_e_exposicao(self):
        """Posições existentes contam para ativo, moeda, risco e quantidade."""
        self.book.execute(self.decision, self.state, self.avaliar())
        self.assertIn("Já existe", self.avaliar().reason)
        self.book.fills[0].symbol = "GBPUSD"
        self.book.fills[0].risk_amount = 100
        self.assertIn("Exposição", self.avaliar().reason)
        self.book.fills[0].risk_amount = 180
        self.assertIn("agregado", self.avaliar().reason)
        self.book.fills *= 3
        self.assertIn("posições", self.avaliar().reason)


class TestCarteira(unittest.TestCase):
    def test_saida_temporal_m1_usa_cotacao_real(self):
        """Após cinco minutos fecha paper no bid atual, mantendo lucro observado."""
        b = Book(active=True, prices_valid=True)
        s, d = estado(), Decision("EURUSD", "BUY", .9, .9)
        b.execute(d, s, check(d, b, b.limits, s, agora=1000))
        b.fills[0].opened_at = "2026-01-01T00:00:00+00:00"
        b.update(SimpleNamespace(cotacao=lambda _: (100.1, 100.11, 1000), lucro=lambda p, preco: 2))
        self.assertEqual(b.log[-1]["reason"], "TIME_EXIT_M1")
        self.assertEqual(b.trades[0]["status"], "CLOSED")
        self.assertEqual(b.trades[0]["exit"], 100.1)
        self.assertEqual(b.cash, 10002)

    def test_persistencia_repete_bloqueio_transitorio(self):
        """Bloqueio breve do Windows não perde o estado nem deixa temporário solto."""
        with TemporaryDirectory() as pasta:
            arquivo = Path(pasta) / "db.json"
            b = Book(cash=9750)
            original = Path.replace
            tentativas = []
            def substituir(origem, destino):
                """Simula uma única disputa pelo arquivo de destino."""
                tentativas.append(origem)
                if len(tentativas) == 1:
                    raise PermissionError("bloqueio de teste")
                return original(origem, destino)
            with patch.object(Path, "replace", substituir), patch("jev_bot.execution.paper.time.sleep"):
                b.save(arquivo)
            self.assertEqual(Book.load(arquivo).cash, 9750)
            self.assertEqual(len(tentativas), 2)
            self.assertEqual(list(Path(pasta).glob("*.tmp")), [])

    def test_persistencia_falha_preserva_banco(self):
        """Falha persistente mantém o JSON anterior intacto e é propagada."""
        with TemporaryDirectory() as pasta:
            arquivo = Path(pasta) / "db.json"
            b = Book()
            b.save(arquivo)
            anterior = arquivo.read_bytes()
            b.cash = 9500
            with patch.object(Path, "replace", side_effect=PermissionError("bloqueado")), patch("jev_bot.execution.paper.time.sleep"):
                with self.assertRaises(PermissionError):
                    b.save(arquivo)
            self.assertEqual(arquivo.read_bytes(), anterior)

    """Verifica stops, gaps, persistência e proteção contra estado inválido."""

    def test_stop_gap_persistencia(self):
        """Gap fecha pelo preço observado, e perda não desaparece ao reiniciar."""
        b = Book(active=True, prices_valid=True)
        s, d = estado(), Decision("EURUSD", "BUY", .9, .9)
        b.execute(d, s, check(d, b, b.limits, s, agora=1000))
        fonte = SimpleNamespace(cotacao=lambda _: (95, 95.01, 1000), lucro=lambda p, preco: -250)
        b.update(fonte)
        self.assertEqual(b.cash, 9750)
        self.assertEqual(b.open_positions(), 0)
        self.assertTrue(b.daily_blocked)
        with TemporaryDirectory() as pasta:
            arquivo = Path(pasta) / "paper.json"
            b.save(arquivo)
            novo = Book.load(arquivo)
            self.assertEqual(novo.cash, 9750)
            self.assertTrue(novo.daily_blocked)
            self.assertFalse(novo.prices_valid)
            self.assertEqual(novo.limits.risk_per_trade, .02)

    def test_sem_cotacao_preserva_posicao(self):
        """Mercado fechado mantém posição e bloqueia marcação/entradas."""
        b = Book(active=True, prices_valid=True)
        s, d = estado(), Decision("EURUSD", "BUY", .9, .9)
        b.execute(d, s, check(d, b, b.limits, s, agora=1000))
        b.update(SimpleNamespace(cotacao=lambda _: None))
        self.assertEqual(b.open_positions(), 1)
        self.assertFalse(b.prices_valid)

    def test_arquivo_invalido(self):
        """Nunca reinicia capital silenciosamente após corrupção."""
        with TemporaryDirectory() as pasta:
            arquivo = Path(pasta) / "paper.json"
            arquivo.write_text("{", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                Book.load(arquivo)
            self.assertEqual(arquivo.read_text(), "{")

    def test_virada_diaria_preserva_drawdown(self):
        """Virada UTC libera limite diário, mas não libera drawdown acumulado."""
        b = Book(cash=9400, day_start=10000, last_equity=9400, day="2000-01-01",
                 daily_blocked=True, drawdown_blocked=True)
        b.update(SimpleNamespace())
        self.assertEqual(b.day, dia_utc())
        self.assertEqual(b.day_start, 9400)
        self.assertFalse(b.daily_blocked)
        self.assertTrue(b.drawdown_blocked)

    def test_stop_venda(self):
        """Uma venda fecha no ask, considerando custo do spread."""
        b = Book(active=True, prices_valid=True)
        s, d = estado(), Decision("EURUSD", "SELL", .9, .9)
        g = check(d, b, b.limits, s, agora=1000)
        b.execute(d, s, g)
        calculo = Mock(return_value=-100)
        b.update(SimpleNamespace(cotacao=lambda _: (101.99, 102.01, 1000), lucro=calculo))
        self.assertEqual(b.open_positions(), 0)
        self.assertEqual(calculo.call_args.args[1], 102.01)

    def test_nao_reinicia_drawdown_apos_recuperar(self):
        """Uma recuperação posterior não remove o bloqueio já registrado."""
        b = Book(cash=8800)
        b.update(SimpleNamespace())
        self.assertTrue(b.drawdown_blocked)
        b.cash = 10000
        b.active = True
        b.daily_blocked = False
        b.update(SimpleNamespace())
        d = Decision("EURUSD", "BUY", .9, .9)
        self.assertIn("drawdown", check(d, b, b.limits, estado(), agora=1000).reason)

    def test_lock(self):
        """Processos concorrentes não alteram o mesmo livro."""
        with TemporaryDirectory() as pasta:
            arquivo = Path(pasta) / "paper.json"
            with bloquear(arquivo):
                with self.assertRaises(RuntimeError):
                    with bloquear(arquivo):
                        pass
            self.assertFalse(Path(str(arquivo) + ".lock").exists())

    def test_pausa_durante_ciclo(self):
        """Uma pausa solicitada durante JEV impede a entrada imediatamente."""
        b = Book(active=True, prices_valid=True)
        with patch("jev_bot.loop.jev.decide", return_value=Decision("EURUSD", "BUY", .9, .9)), patch("jev_bot.risk.time.time", return_value=1000):
            r = run([estado()], b, b.limits, execute=True, pausado=lambda: True)
        self.assertEqual(b.open_positions(), 0)
        self.assertEqual(r[0][2].verdict, "SKIP")

    def test_consulta_nao_executa(self):
        """Consulta aprovada não registra posição."""
        b = Book(active=True, prices_valid=True)
        with patch("jev_bot.loop.jev.decide", return_value=Decision("EURUSD", "BUY", .9, .9)), patch("jev_bot.risk.time.time", return_value=1000):
            run([estado()], b, b.limits)
        self.assertEqual(b.open_positions(), 0)


class TestFonte(unittest.TestCase):
    """Verifica o filtro antes do histórico, incluindo cache de inatividade."""

    def test_filtra_antes_dos_candles(self):
        """Catálogo completo inclui escondidos; inativos não pedem candles."""
        infos = [SimpleNamespace(name=n, custom=False, trade_mode=modo, visible=visivel)
                 for n, modo, visivel in (("FECHADO", 4, True), ("DESABILITADO", 0, True), ("ABERTO", 4, False))]
        api = Mock()
        api.symbols_get.return_value = infos
        api.symbol_select.return_value = True
        api.symbol_info_tick.side_effect = lambda nome: SimpleNamespace(time_msc=(500 if nome == "FECHADO" else 1000) * 1000, time=1000, bid=100, ask=100.01)
        fonte = FonteMT5(api=api, ajuste_horas=0, relogio=lambda: 1000)
        fonte._estado = Mock(return_value=estado())
        self.assertEqual(len(list(fonte.estados())), 1)
        self.assertEqual(fonte.resumo["catalogo"], 3)
        self.assertEqual(fonte.resumo["inativos"], 2)
        fonte._estado.assert_called_once()
        api.symbol_select.assert_called_once_with("ABERTO", True)
        api.symbol_info_tick.reset_mock()
        list(fonte.estados())
        api.symbol_info_tick.assert_called_once_with("ABERTO")
        api.symbols_get.assert_called_once()

    def test_timestamps_invalidos(self):
        """Ticks futuros, antigos e bid/ask inválidos são inelegíveis."""
        api = Mock()
        fonte = FonteMT5(api=api, ajuste_horas=0, relogio=lambda: 1000)
        for instante, bid, ask in ((1001, 1, 2), (800, 1, 2), (1000, 2, 1), (1000, float("nan"), 2)):
            api.symbol_info_tick.return_value = SimpleNamespace(time_msc=instante * 1000, time=instante, bid=bid, ask=ask)
            self.assertIsNone(fonte.cotacao("X"))

    def test_metricas_reais_e_contrato(self):
        """Métricas usam candles fechados, e contrato ausente não gera substituto."""
        api = Mock()
        api.TIMEFRAME_M1 = 1
        api.ORDER_TYPE_BUY, api.ORDER_TYPE_SELL = 0, 1
        barras = [dict(time=100000 - (100 - i) * 60, open=100, close=100 + i * .01,
                       high=102, low=99, tick_volume=100) for i in range(100)]
        api.copy_rates_from_pos.return_value = barras
        api.order_calc_profit.return_value = -100
        api.order_calc_margin.return_value = 1000
        info = SimpleNamespace(name="X", path="Forex\\X", point=.01,
                               trade_stops_level=0, currency_base="EUR", currency_profit="USD",
                               volume_min=.01, volume_max=10, volume_step=.01, digits=2, trade_mode=4)
        fonte = FonteMT5(api=api, ajuste_horas=0, relogio=lambda: 100000)
        s = fonte._estado(info, (100, 100.01, 100000))
        self.assertEqual(s.source, "MT5")
        self.assertEqual(len(s.candles), 100)
        self.assertFalse(hasattr(s, "atr"))
        self.assertFalse(hasattr(s, "momentum"))
        self.assertAlmostEqual(s.stop_distance, .02)
        self.assertEqual(s.loss_per_lot, 100)
        self.assertEqual(api.copy_rates_from_pos.call_args_list[0].args, ("X", 1, 1, 100))
        self.assertEqual(s.timeframe, "M1")
        self.assertTrue(s.mathematics["disponivel"])
        api.order_calc_profit.return_value = None
        self.assertIsNone(fonte._estado(info, (100, 100.01, 100000)))

    def test_revalidar_exige_mesmo_candle_m1_fechado(self):
        """Candle ainda atual mantém cotação; barra nova torna snapshot obsoleto."""
        api = Mock()
        api.TIMEFRAME_M1 = 1
        api.ORDER_TYPE_BUY, api.ORDER_TYPE_SELL = 0, 1
        api.order_calc_profit.return_value = -100
        api.order_calc_margin.return_value = 1000
        api.symbol_info.return_value = SimpleNamespace(name="EURUSD", trade_mode=4)
        api.symbol_info_tick.return_value = SimpleNamespace(
            time_msc=100000 * 1000, time=100000, bid=100, ask=100.01)
        barra = {"time": 99940, "open": 100, "high": 101, "low": 99,
                 "close": 100.1, "tick_volume": 10}
        api.copy_rates_from_pos.return_value = [barra]
        fonte = FonteMT5(api=api, ajuste_horas=0, relogio=lambda: 100000)
        snapshot = estado(candles=[dict(barra)])
        atualizado = fonte.revalidar(snapshot)
        self.assertIs(atualizado, snapshot)
        api.copy_rates_from_pos.assert_called_once_with("EURUSD", 1, 1, 1)

        api.copy_rates_from_pos.return_value = [{**barra, "time": 100000}]
        self.assertIsNone(fonte.revalidar(snapshot))

    def test_revalidar_falha_fechado_sem_barra_atual(self):
        api = Mock()
        api.TIMEFRAME_M1 = 1
        api.symbol_info.return_value = SimpleNamespace(trade_mode=4)
        api.symbol_info_tick.return_value = SimpleNamespace(
            time_msc=100000 * 1000, time=100000, bid=100, ask=100.01)
        api.copy_rates_from_pos.return_value = None
        fonte = FonteMT5(api=api, ajuste_horas=0, relogio=lambda: 100000)
        self.assertIsNone(fonte.revalidar(estado(candles=[{"time": 99940}])))

    def test_historico_antigo_nao_e_analisado(self):
        """Mesmo com tick novo, candles antigos não alimentam uma decisão."""
        api = Mock()
        barras = [dict(time=-10000, close=100, high=101, low=99, tick_volume=100)] * 100
        api.copy_rates_from_pos.return_value = barras
        fonte = FonteMT5(api=api, ajuste_horas=0, relogio=lambda: 1000)
        self.assertIsNone(fonte._estado(SimpleNamespace(name="X"), (100, 100.01, 1000)))
        self.assertEqual(api.copy_rates_from_pos.call_count, 1)

    def test_ajuste_broker(self):
        """Ajuste explícito converte horário observado do broker para UTC."""
        api = Mock()
        api.symbol_info_tick.return_value = SimpleNamespace(time_msc=11800_000, time=11800, bid=1, ask=2)
        fonte = FonteMT5(api=api, ajuste_horas=3, relogio=lambda: 1000)
        self.assertEqual(fonte.cotacao("X")[2], 1000)

    def test_jev_sem_fallback(self):
        """Ausência de configuração e engine offline falham explicitamente."""
        with patch.dict("os.environ", {}, clear=True), patch("jev_bot.jev.carregar_ambiente"):
            with self.assertRaises(RuntimeError):
                jev.decide(estado())
        with self.assertRaises(ValueError):
            jev.decide(estado(), engine="offline")

    def test_resposta_jev_invalida(self):
        """JEV não converte NaN ou resposta incompleta em uma entrada."""
        resposta = Mock()
        resposta.__enter__ = Mock(return_value=resposta)
        resposta.__exit__ = Mock(return_value=False)
        resposta.read.return_value = json.dumps({"answers": {"action": {"value": "BUY", "probability": float("nan"), "confidence": .9}}}).encode()
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "test"}, clear=True), patch("jev_bot.jev.carregar_ambiente"), patch("jev_bot.jev.urlopen", return_value=resposta) as chamada:
            with self.assertRaises(RuntimeError):
                jev.decide(estado())
            chamada.assert_called_once()

    def test_jev_errors_tipados_sem_chamada_real(self):
        """HTTP 401/429/500, timeout e JSON ruim mantêm estágios explícitos."""
        for status in (401, 429, 500):
            with self.subTest(http=status), \
                    patch.dict("os.environ", {"OPENROUTER_API_KEY": "mock"}, clear=True), \
                    patch("jev_bot.jev.carregar_ambiente"), \
                    patch("jev_bot.jev.urlopen",
                          side_effect=HTTPError("", status, "erro mockado", None, None)):
                with self.assertRaises(jev.JevError) as err:
                    jev.decide(estado())
                self.assertEqual(err.exception.code, f"HTTP_{status}")
                self.assertEqual(err.exception.stage, "http_error")
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "mock"}, clear=True), \
                patch("jev_bot.jev.carregar_ambiente"), \
                patch("jev_bot.jev.urlopen", side_effect=TimeoutError()):
            with self.assertRaises(jev.JevError) as err:
                jev.decide(estado())
            self.assertEqual(err.exception.code, "JEv_TIMEOUT")
            self.assertEqual(err.exception.stage, "timeout")

        resposta = Mock()
        resposta.__enter__ = Mock(return_value=resposta)
        resposta.__exit__ = Mock(return_value=False)
        resposta.read.return_value = b"not-json"
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "mock"}, clear=True), \
                patch("jev_bot.jev.carregar_ambiente"), \
                patch("jev_bot.jev.urlopen", return_value=resposta):
            with self.assertRaises(jev.JevError) as err:
                jev.decide(estado())
            self.assertEqual(err.exception.code, "JEv_PARSE_ERROR")
            self.assertEqual(err.exception.stage, "parse_error")


class TestConfiguracao(unittest.TestCase):
    """Verifica .env, precedência e credenciais reservadas sem registrar segredos."""

    def test_dashboard_ativa_paper_automaticamente(self):
        """Com JEV configurado, ativa antes do scan; no encerramento pausa e salva."""
        with TemporaryDirectory() as pasta:
            args = SimpleNamespace(book=str(Path(pasta) / "db.json"), cmd="dashboard", terminal=None,
                                   mt5_time_offset_hours=3, once=True, interval=15)
            painel = PainelTerminal(args)
            fonte = Mock()
            fonte.moeda = "USD"
            fonte.resumo = {}
            fonte.estados.return_value = []
            fonte.conta.return_value = dict(balance=0, equity=0, profit=0, margin_free=0, currency="USD")
            fonte.__enter__ = Mock(return_value=fonte)
            fonte.__exit__ = Mock(return_value=False)
            ativos = []
            salvar = painel.salvar
            def registrar():
                """Observa ativação sem alterar o mecanismo de persistência."""
                ativos.append(painel.book.active)
                salvar()
            with patch("jev_bot.painel.FonteMT5", return_value=fonte), patch("jev_bot.painel.jev.configuracao", return_value=("teste", "endpoint")), patch.object(painel, "salvar", registrar):
                painel.trabalhar()
            self.assertIn(True, ativos)
            self.assertFalse(ativos[-1])
            self.assertIsNone(painel.erro)

    def test_chave_openrouter_env_e_contrato_online(self):
        """A chave do arquivo chega ao bearer e a decisão tipada retorna ao risco."""
        with TemporaryDirectory() as pasta:
            arquivo = Path(pasta) / ".env"
            arquivo.write_text("AI_PROVIDER=openrouter\nOPENROUTER_API_KEY=teste-isolado\nOPENROUTER_MODEL=typesafe/jev-1.13\n", encoding="utf-8")
            resposta = Mock()
            resposta.__enter__ = Mock(return_value=resposta)
            resposta.__exit__ = Mock(return_value=False)
            resposta.read.return_value = json.dumps({"answers": {"action": {"type": "choice", "choice": "BUY", "probabilities": {"BUY": .8, "SELL": .1, "HOLD": .1, "AVOID": 0}, "confidence": .9}}}).encode()
            with patch.object(configuracao, "CAMINHO_ENV", arquivo), patch.dict("os.environ", {}, clear=True), patch("jev_bot.jev.urlopen", return_value=resposta) as chamada:
                decisao = jev.decide(estado())
            req = chamada.call_args.args[0]
            self.assertEqual(req.full_url, "https://openrouter.ai/api/alpha/decisions")
            self.assertEqual(req.get_header("Authorization"), "Bearer teste-isolado")
            corpo = json.loads(req.data)
            self.assertEqual(corpo["model"], "typesafe/jev-1.13")
            self.assertEqual(set(corpo["questions"]["action"]["criteria"]), {"BUY", "SELL", "HOLD", "AVOID"})
            self.assertEqual(decisao.action, "BUY")
            self.assertEqual(decisao.probability, .8)
            self.assertEqual(decisao.source, "jev")

    def test_endpoint_externo_nao_recebe_chave(self):
        """Endpoint alterado não pode encaminhar a chave para outro domínio."""
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "teste", "OPENROUTER_API_ENDPOINT": "https://example.test/api/alpha/decisions"}, clear=True), patch("jev_bot.jev.carregar_ambiente"), patch("jev_bot.jev.urlopen") as chamada:
            with self.assertRaises(RuntimeError):
                jev.decide(estado())
            chamada.assert_not_called()

    def test_env_preserva_arquivo_e_ambiente(self):
        """Carregamento não sobrescreve o processo nem regrava o arquivo."""
        with TemporaryDirectory() as pasta:
            arquivo = Path(pasta) / ".env"
            arquivo.write_text("TYPESAFE_API_KEY=valor-de-teste\nMT5_TERMINAL_PATH='C:\\Program Files\\Terminal\\terminal64.exe'\nMT5_TIME_OFFSET_HOURS=3\n", encoding="utf-8")
            anterior = arquivo.read_bytes()
            with patch.object(configuracao, "CAMINHO_ENV", arquivo), patch.dict("os.environ", {"TYPESAFE_API_KEY": "valor-processo"}, clear=True):
                configuracao.carregar_ambiente()
                import os
                self.assertEqual(os.environ["TYPESAFE_API_KEY"], "valor-processo")
                self.assertEqual(configuracao.terminal_mt5(), r"C:\Program Files\Terminal\terminal64.exe")
                self.assertEqual(configuracao.ajuste_horario_mt5(), 3)
            self.assertEqual(arquivo.read_bytes(), anterior)

    def test_credenciais_nao_fazem_login(self):
        """Campos reservados nunca são passados ao MT5 na integração paper."""
        with TemporaryDirectory() as pasta:
            terminal = Path(pasta) / "terminal64.exe"
            terminal.touch()
            api = Mock()
            api.initialize.return_value = True
            api.terminal_info.return_value = SimpleNamespace(connected=True)
            api.account_info.return_value = SimpleNamespace(currency="USD")
            with patch.dict("os.environ", {"MT5_LOGIN": "123", "MT5_PASSWORD": "valor-teste", "MT5_SERVER": "teste"}):
                with FonteMT5(caminho=str(terminal), ajuste_horas=0, api=api):
                    pass
            api.initialize.assert_called_once_with(str(terminal), timeout=10000)
            api.login.assert_not_called()

    def test_migracao_limite_sem_resetar_carteira(self):
        """A remoção do indicador preserva as perdas e os limites já gravados."""
        with TemporaryDirectory() as pasta:
            arquivo = Path(pasta) / "paper.json"
            b = Book(cash=9750, daily_blocked=True)
            b.save(arquivo)
            dados = json.loads(arquivo.read_text())
            dados["limits"]["max_spread_atr"] = dados["limits"].pop("max_spread_stop") * 2
            arquivo.write_text(json.dumps(dados), encoding="utf-8")
            novo = Book.load(arquivo)
            self.assertEqual(novo.cash, 9750)
            self.assertTrue(novo.daily_blocked)
            self.assertEqual(novo.limits.max_spread_stop, .075)

    def test_run_pausado_sem_jev(self):
        """Acompanha stops sem chave externa e sem analisar novas entradas."""
        with TemporaryDirectory() as pasta:
            arquivo = Path(pasta) / "paper.json"
            fonte = Mock()
            fonte.moeda = "USD"
            fonte.resumo = dict(catalogo=3, candidatos=2, analisados=2)
            fonte.estados.return_value = [estado(symbol="BTCUSD"), estado(symbol="ETHUSD")]
            fonte.conta.return_value = dict(balance=12000, equity=12000, profit=0, margin_free=12000, currency="USD")
            contexto = Mock()
            contexto.__enter__ = Mock(return_value=fonte)
            contexto.__exit__ = Mock(return_value=False)
            with patch("jev_bot.painel.FonteMT5", return_value=contexto), patch("jev_bot.painel.jev.configuracao", side_effect=RuntimeError("não configurado")), patch("jev_bot.loop.jev.decide") as decidir, redirect_stdout(io.StringIO()):
                self.assertEqual(main(["--book", str(arquivo), "--once", "--plain", "run"]), 0)
            decidir.assert_not_called()
            fonte.estados.assert_called_once()
            fonte.lucro.assert_not_called()
            self.assertFalse(Book.load(arquivo).active)


class TestPainel(unittest.TestCase):
    """Verifica universo completo, calendário e histórico contínuo do dashboard."""

    def test_fim_de_semana_fuso_tokyo(self):
        """Sexta UTC já pode ser sábado no dia local do usuário."""
        api = Mock()
        for instante, esperado in (("2026-10-09T14:59:00+00:00", False),
                                   ("2026-10-09T15:00:00+00:00", True),
                                   ("2026-10-11T14:59:00+00:00", True),
                                   ("2026-10-11T15:00:00+00:00", False)):
            t = datetime.fromisoformat(instante).timestamp()
            self.assertEqual(FonteMT5(api=api, ajuste_horas=0, relogio=lambda: t).fim_de_semana(), esperado)

    def test_varredura_cripto_nao_se_limita_btc(self):
        """Weekend exclui Forex antes dos ticks e visita todos os símbolos Crypto."""
        instante = datetime(2026, 10, 10, 1, tzinfo=timezone.utc).timestamp()
        api = Mock()
        api.symbols_get.return_value = [SimpleNamespace(name=n, path=p, custom=False, trade_mode=4, visible=True)
                                       for n, p in (("BTCUSD", "Crypto\\BTCUSD"), ("ETHUSD", "Crypto\\ETHUSD"),
                                                    ("SOLUSD", "Crypto\\SOLUSD"), ("EURUSD", "Forex\\EURUSD"))]
        api.symbol_info_tick.return_value = SimpleNamespace(time_msc=int(instante * 1000), time=instante, bid=100, ask=100.01)
        f = FonteMT5(api=api, ajuste_horas=0, relogio=lambda: instante)
        f._estado = lambda info, tick: estado(symbol=info.name)
        resultado = list(f.estados())
        self.assertEqual([s.symbol for s in resultado], ["BTCUSD", "ETHUSD", "SOLUSD"])
        self.assertEqual(f.resumo["candidatos"], 3)
        self.assertEqual(f.resumo["fora_dia"], 1)
        self.assertNotIn("EURUSD", [c.args[0] for c in api.symbol_info_tick.call_args_list])

    def test_dia_util_inclui_todos_os_mercados(self):
        """Categoria Forex retorna à elegibilidade na segunda-feira local."""
        instante = datetime(2026, 10, 12, 1, tzinfo=timezone.utc).timestamp()
        api = Mock()
        api.symbols_get.return_value = [SimpleNamespace(name=n, path=p, custom=False, trade_mode=4, visible=True)
                                       for n, p in (("BTCUSD", "Crypto\\BTCUSD"), ("EURUSD", "Forex\\EURUSD"))]
        api.symbol_info_tick.return_value = SimpleNamespace(time_msc=int(instante * 1000), time=instante, bid=100, ask=100.01)
        f = FonteMT5(api=api, ajuste_horas=0, relogio=lambda: instante)
        f._estado = lambda info, tick: estado(symbol=info.name)
        self.assertEqual(len(list(f.estados())), 2)
        self.assertEqual(f.resumo["fora_dia"], 0)

    def test_memecoins_primeiro_sem_excluir_outras_criptos(self):
        """Prioridade meme mantém o universo e ignora nomes fora da categoria Crypto."""
        instante = datetime(2026, 10, 10, 1, tzinfo=timezone.utc).timestamp()
        api = Mock()
        api.symbols_get.return_value = [SimpleNamespace(name=n, path=p, custom=False, trade_mode=4, visible=True)
                                       for n, p in (("BTCUSD", "Crypto\\BTCUSD"), ("1000xPEPE", "Crypto\\1000xPEPE"),
                                                    ("DOGUSD", "Crypto\\DOGUSD"), ("ETHUSD", "Crypto\\ETHUSD"),
                                                    ("EURUSD", "Forex\\EURUSD"))]
        api.symbol_info_tick.return_value = SimpleNamespace(time_msc=int(instante * 1000), time=instante, bid=100, ask=100.01)
        fonte = FonteMT5(api=api, ajuste_horas=0, relogio=lambda: instante)
        fonte._estado = lambda info, tick: estado(symbol=info.name)
        self.assertEqual([s.symbol for s in fonte.estados()], ["1000xPEPE", "DOGUSD", "BTCUSD", "ETHUSD"])
        self.assertEqual(fonte.resumo["memecoins"], 2)
        self.assertEqual(fonte.resumo["candidatos"], 4)

    def test_falha_jev_nao_interrompe_proximos_ativos(self):
        """Falha externa em BTC não impede avaliar ETH nem fabrica uma decisão."""
        b = Book(active=True, prices_valid=True)
        with patch("jev_bot.loop.jev.decide", side_effect=[RuntimeError("indisponível"), Decision("ETHUSD", "HOLD", .8, .9)]):
            registros = run([estado(symbol="BTCUSD"), estado(symbol="ETHUSD")], b, b.limits)
        self.assertEqual(len(registros), 2)
        self.assertIsNone(registros[0][1])
        self.assertEqual(registros[1][1].symbol, "ETHUSD")
        self.assertEqual(b.open_positions(), 0)

    def test_db_trade_completo_e_metricas(self):
        """Trade aberto/fechado conserva sua identidade e todos os valores no JSON."""
        b = Book(active=True, prices_valid=True)
        s, d = estado(), Decision("EURUSD", "BUY", .9, .9)
        b.execute(d, s, check(d, b, b.limits, s, agora=1000))
        trade_id = b.fills[0].trade_id
        with TemporaryDirectory() as pasta:
            arquivo = Path(pasta) / "db.json"
            b.save(arquivo)
            aberto = json.loads(arquivo.read_text())
            self.assertEqual(aberto["trades"][0]["status"], "OPEN")
            self.assertEqual(aberto["trades"][0]["trade_id"], trade_id)
            b.update(SimpleNamespace(cotacao=lambda _: (105, 105.01, 1000), lucro=lambda p, preco: 100))
            b.save(arquivo)
            dados = json.loads(arquivo.read_text())
            self.assertEqual(len(dados["trades"]), 1)
            trade = dados["trades"][0]
            self.assertEqual(trade["status"], "CLOSED")
            self.assertEqual(trade["profit"], 100)
            self.assertEqual(trade["entry"], 100.01)
            self.assertEqual(trade["exit"], 105)
            self.assertEqual(trade["reason"], "TARGET")
            self.assertTrue(trade["opened_at"])
            self.assertTrue(trade["closed_at"])
            novo = Book.load(arquivo)
            self.assertEqual(novo.metrics()["wins"], 1)
            self.assertEqual(novo.metrics()["gains"], 100)
            self.assertAlmostEqual(novo.metrics()["growth"], .01)

    def test_tela_diferencia_conta_real_e_paper(self):
        """Métricas e controles aparecem sem números de conta ou segredos."""
        b = Book()
        tela = desenhar(dict(metricas=b.metrics(), capital=b.initial_capital, limites=b.limits.__dict__,
                            moeda="USD", banco="db.json", conta=dict(balance=50, equity=55, profit=5, currency="USD")), 120, 40)
        for texto in ("CONTA REAL", "PAPER", "DRAWDOWN", "RISCO", "WIN", "LOSS", "I: Iniciar", "db.json"):
            self.assertIn(texto, tela)

    def test_tela_fixa_nao_imprime_novas_janelas(self):
        """A limpeza ocorre uma vez; só a linha alterada é atualizada depois."""
        saida = io.StringIO()
        tela = TelaFixa(saida)
        tela.atualizar("Cabeçalho\nBTCUSD analisando\nControles")
        primeiro = saida.getvalue()
        tela.atualizar("Cabeçalho\nETHUSD analisando\nControles")
        atualizacao = saida.getvalue()[len(primeiro):]
        self.assertEqual(saida.getvalue().count("\033[2J"), 1)
        self.assertNotIn("\n", atualizacao)
        self.assertNotIn("Cabeçalho", atualizacao)
        self.assertIn("\033[2;1H", atualizacao)
        self.assertIn("ETHUSD analisando", atualizacao)
        self.assertIn("\033[37m", atualizacao)
        anterior = saida.getvalue()
        tela.atualizar("Cabeçalho\nETHUSD analisando\nControles")
        self.assertEqual(saida.getvalue(), anterior)

    def test_monitoramento_consulta_cotacoes_entre_scans(self):
        """Uma rodada consulta ambos os ativos e atualiza os preços sem ativar trading."""
        with TemporaryDirectory() as pasta:
            painel = PainelTerminal(SimpleNamespace(book=str(Path(pasta) / "db.json")))
            painel.book = Book()
            painel.fonte = Mock()
            painel.fonte.resumo = {}
            painel.fonte.fim_de_semana.return_value = False
            painel.fonte.conta.return_value = dict(balance=10000, equity=10000, profit=0, margin_free=10000, currency="USD")
            painel.fonte.cotacao.side_effect = [(105, 105.01, 1000), (106, 106.01, 1000)]
            painel.monitorados["BTCUSD"] = estado(symbol="BTCUSD")
            painel.monitorados["ETHUSD"] = estado(symbol="ETHUSD")
            painel.monitorar(12)
            self.assertEqual([c.args[0] for c in painel.fonte.cotacao.call_args_list], ["BTCUSD", "ETHUSD"])
            self.assertEqual(painel.dados["fase"], "MONITORANDO")
            self.assertEqual(painel.monitorados["ETHUSD"].bid, 106)
            self.assertIn("2 ativos nesta rodada", painel.dados["mensagem"])
            self.assertIn("scan em 12s", painel.dados["mensagem"])
            self.assertEqual(painel.cotacoes_verificadas, 2)
            self.assertFalse(painel.book.active)

    def test_monitoramento_lotes_nao_abandona_ativos(self):
        """Rodadas de oito avançam pelo catálogo inteiro sem repetir o primeiro lote."""
        with TemporaryDirectory() as pasta:
            painel = PainelTerminal(SimpleNamespace(book=str(Path(pasta) / "db.json")))
            painel.fonte = Mock()
            painel.fonte.resumo = {}
            painel.fonte.fim_de_semana.return_value = False
            painel.fonte.cotacao.return_value = (100, 100.01, 1000)
            nomes = [f"CRYPTO{i}" for i in range(10)]
            painel.monitorados.update((n, estado(symbol=n)) for n in nomes)
            painel.monitorar(10)
            self.assertEqual(painel.cotacoes_verificadas, 8)
            painel.monitorar(9)
            consultas = [c.args[0] for c in painel.fonte.cotacao.call_args_list]
            self.assertEqual(consultas[:10], nomes)

    def test_tela_mostra_varios_memecoins_em_colunas(self):
        """Tela larga mantém memecoins visíveis mesmo após analisar outros contratos."""
        dados = dict(ativos=[("DOGUSD", "WAITING"), ("1000xPEPE", "WAITING")]
                     + [(f"CRYPTO{i}", "monitorando") for i in range(20)])
        tela = desenhar(dados, 120, 40)
        self.assertIn("*DOGUSD", tela)
        self.assertIn("*1000xPEPE", tela)
        self.assertIn("CRYPTO19", tela)
        self.assertTrue(all(len(l) <= 120 for l in tela.splitlines()))

    def test_tela_compacta_lista_mais_de_dois_pares(self):
        """Console de trinta linhas reserva espaço para uma grade de vários pares."""
        dados = dict(ativos=[(f"PAIR{i}", "monitorando") for i in range(30)])
        tela = desenhar(dados, 110, 30)
        self.assertIn("AV TRADING", tela)
        self.assertGreaterEqual(tela.count("PAIR"), 12)
        self.assertLessEqual(len(tela.splitlines()), 30)

    def test_grade_estavel_ao_abrir_posicao(self):
        """Inserir uma posição não desloca os campos do scanner e dos comandos."""
        b = Book(active=True, prices_valid=True)
        dados = dict(metricas=b.metrics(), capital=b.initial_capital, limites=b.limits.__dict__, moeda="USD", banco="db.json")
        antes = desenhar(dados, 120, 40).splitlines()
        s, d = estado(), Decision("EURUSD", "BUY", .9, .9)
        p = b.execute(d, s, check(d, b, b.limits, s, agora=1000))
        from dataclasses import asdict
        dados["posicoes"] = [asdict(p)]
        depois = desenhar(dados, 120, 40).splitlines()
        self.assertEqual(len(antes), len(depois))
        for texto in ("ÚLTIMOS ATIVOS", "EVENTOS DE TRADING", "I: Iniciar"):
            self.assertEqual(next(i for i,l in enumerate(antes) if texto in l), next(i for i,l in enumerate(depois) if texto in l))


class TestMatematica(unittest.TestCase):
    """Valida diagnósticos experimentais sem assumir poder preditivo."""

    def test_janelas_comparam_direcao_e_custo_sem_probabilidade(self):
        """Trajetória linear produz inclinação conhecida e custo em unidades de spread."""
        barras = [dict(close=100 + i * .2, tick_volume=10) for i in range(20)]
        resultado = resumir(barras, .01, .1)
        self.assertEqual([j["candles"] for j in resultado["janelas_direcionais"]], [5, 10, 20])
        for janela in resultado["janelas_direcionais"]:
            self.assertAlmostEqual(janela["inclinacao_por_minuto"], .2)
            self.assertAlmostEqual(janela["deslocamento_5min_em_spreads"], 10)
            self.assertAlmostEqual(janela["qualidade_ajuste"], 1)
        self.assertEqual(resultado["concordancia_direcional"], 1)
        self.assertNotIn("probabilidade", resultado)

    def test_preco_constante_nao_cria_oportunidade(self):
        """Mercado parado não ganha direção só por haver custo ou peso de volume."""
        resultado = resumir([dict(close=100, tick_volume=10)] * 20, .01, .1)
        self.assertEqual(resultado["concordancia_direcional"], 0)
        self.assertTrue(all(j["deslocamento_5min_em_spreads"] == 0 for j in resultado["janelas_direcionais"]))

    def test_status_compacto_preserva_decisao_e_bloqueio(self):
        """Texto compacto revela por que BUY não vira entrada, sem cortar a ação."""
        status = status_curto("resultado: Jev BUY p=88% c=79% | NÃO: Spread elevado ou inválido")
        self.assertIn("BUY", status)
        self.assertIn("88/79%", status)
        self.assertIn("CUSTO", status)

    def test_amplitudes_normalizadas_e_extremos(self):
        """Alta, baixa e estabilidade geram pesos finitos, normalizados e determinísticos."""
        for passo, grupo in ((1, "alta"), (-1, "baixa"), (0, "estavel")):
            barras = [dict(close=100 + i * passo, tick_volume=10) for i in range(20)]
            resultado = resumir(barras, .01, .1)
            pesos = resultado["amplitudes"]["pesos_historicos"]
            self.assertAlmostEqual(sum(pesos.values()), 1)
            self.assertAlmostEqual(pesos[grupo], 1)
            self.assertEqual(resultado, resumir(barras, .01, .1))

    def test_dados_ausentes_nao_inventam_amplitudes(self):
        """Volume ausente e NaN não produzem pesos operacionais artificiais."""
        barras = [dict(close=100 + i, tick_volume=0) for i in range(20)]
        self.assertFalse(resumir(barras, .01, .1)["amplitudes"]["disponivel"])
        barras[0]["close"] = float("nan")
        self.assertFalse(resumir(barras, .01, .1)["disponivel"])


class TestNovoPerfilRisco(unittest.TestCase):
    """Valida os limites solicitados sem apagar resultados ou depender do MT5."""

    def test_sem_teto_de_posicoes_mas_com_risco_agregado(self):
        """Mais de três posições são permitidas; nova entrada acima de 15% é recusada."""
        b = Book(active=True, prices_valid=True)
        for i in range(7):
            s = estado(symbol=f"ATIVO{i}", margin_per_lot=1)
            d = Decision(s.symbol, "BUY", .9, .9)
            g = check(d, b, b.limits, s, agora=1000)
            self.assertEqual(g.verdict, "EXECUTE")
            self.assertEqual(g.risk_amount, 200)
            b.execute(d, s, g)
        self.assertEqual(len(b.fills), 7)
        s = estado(symbol="ATIVO8", margin_per_lot=1)
        self.assertIn("agregado", check(Decision(s.symbol, "BUY", .9, .9), b, b.limits, s, agora=1000).reason)

    def test_migracao_preserva_perdas_e_bloqueios(self):
        """Perfil antigo muda uma vez, preservando dinheiro, logs e bloqueios persistidos."""
        with TemporaryDirectory() as pasta:
            arquivo = Path(pasta) / "db.json"
            b = Book(cash=9750, daily_blocked=True, drawdown_blocked=True)
            b.save(arquivo)
            dados = json.loads(arquivo.read_text())
            dados.pop("risk_profile_version")
            dados["limits"].update(risk_per_trade=.005, max_total_risk=.02,
                                   max_positions=3, max_currency_risk=.01, max_drawdown=.05)
            arquivo.write_text(json.dumps(dados))
            novo = Book.load(arquivo)
            self.assertEqual(novo.cash, 9750)
            self.assertTrue(novo.daily_blocked)
            self.assertTrue(novo.drawdown_blocked)
            self.assertEqual(novo.limits.risk_per_trade, .02)
            self.assertEqual(novo.limits.max_total_risk, .15)
            self.assertEqual(novo.limits.max_currency_risk, .15)
            self.assertEqual(novo.limits.max_drawdown, .10)
            self.assertIsNone(novo.limits.max_positions)
            novo.save(arquivo)
            self.assertEqual(Book.load(arquivo).cash, 9750)


if __name__ == "__main__":
    unittest.main(verbosity=2)
