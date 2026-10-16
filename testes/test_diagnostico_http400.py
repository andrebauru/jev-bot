"""Diagnóstico HTTP 400 do JEv, estados, payload e botão OpenRouter (A–Q)."""

import io
import json
import os
import tempfile
import time
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError
import unittest

from apoio import (FonteFake, LoggerSilencioso, candles_deterministicos,
                   estado_mt5, janela)
from jev_bot import jev, mercados, padroes
from jev_bot.execution.paper import Book
from jev_bot.gui import config as cfg_gui
from jev_bot.orquestrador import Orquestrador
from jev_bot.risk import pre_check


def _http_error(status, corpo):
    bruto = json.dumps(corpo).encode() if isinstance(corpo, dict) else str(corpo).encode()
    return HTTPError("https://openrouter.ai/api/alpha/decisions", status, "erro",
                     {}, io.BytesIO(bruto))


def _ok_choice(escolha):
    corpo = {"answers": {"action": {"type": "choice", "choice": escolha, "confidence": .9,
                                    "probabilities": {"YES": .6, "NO": .4}}}}
    resposta = Mock()
    resposta.__enter__ = Mock(return_value=resposta)
    resposta.__exit__ = Mock(return_value=False)
    resposta.read.return_value = json.dumps(corpo).encode()
    return resposta


class TestClienteHTTP400(unittest.TestCase):
    """O cliente distingue erros, valida localmente e nunca aprova implicitamente."""

    def _ambiente(self):
        return patch.dict("os.environ", {"OPENROUTER_API_KEY": "mock"}, clear=True)

    def _proposta_valida(self):
        return dict(cfg_gui.JID_DIAGNOSTICO)

    # J. HTTP 400 com mensagem sanitizada.
    def test_j_http400_mensagem_sanitizada(self):
        corpo = {"error": {"message": "unknown question type 'approval' for model x",
                           "type": "invalid_request_error", "code": "invalid_type"}}
        with self._ambiente(), patch("jev_bot.jev.carregar_ambiente"), \
                patch("jev_bot.jev.urlopen", side_effect=_http_error(400, corpo)):
            with self.assertRaises(jev.JevError) as err:
                jev.avaliar_entrada(self._proposta_valida())
        self.assertEqual(err.exception.code, "HTTP_400")
        self.assertEqual(err.exception.http_status, 400)
        self.assertEqual(err.exception.error_code, "invalid_type")
        self.assertIn("approval", err.exception.error_message)

    # K. Requisição usa o envelope documentado (action/choice), não 'approval'.
    def test_k_envelope_action_choice(self):
        with self._ambiente(), patch("jev_bot.jev.carregar_ambiente"), \
                patch("jev_bot.jev.urlopen", return_value=_ok_choice("YES")) as chamada:
            resultado = jev.avaliar_entrada(self._proposta_valida())
        self.assertTrue(resultado["approved"])
        corpo = json.loads(chamada.call_args[0][0].data.decode())
        self.assertIn("action", corpo["questions"])
        self.assertNotIn("approval", corpo["questions"])
        self.assertEqual(corpo["questions"]["action"]["type"], "choice")

    # L. Payload inválido falha localmente, sem POST e sem inventar zero.
    def test_l_payload_invalido_sem_post(self):
        casos = [dict(self._proposta_valida(), entry_reference=float("nan")),
                 dict(self._proposta_valida(), volume=float("inf")),
                 {k: v for k, v in self._proposta_valida().items() if k != "stop_loss"},
                 dict(self._proposta_valida(), side_proposed="HOLD"),
                 dict(self._proposta_valida(), evidencias={"x": object()})]
        for proposta in casos:
            with self.subTest(proposta=list(proposta)[:2]), self._ambiente(), \
                    patch("jev_bot.jev.carregar_ambiente"), \
                    patch("jev_bot.jev.urlopen") as chamada:
                with self.assertRaises(jev.JevError) as err:
                    jev.avaliar_entrada(proposta)
                self.assertEqual(err.exception.code, "JEv_PAYLOAD_INVALID")
                chamada.assert_not_called()

    # M. Classificação distinta de erros; nenhuma aprovação implícita.
    def test_m_classificacao_de_erros(self):
        for status in (401, 402, 403, 404, 429, 500):
            with self.subTest(http=status), self._ambiente(), \
                    patch("jev_bot.jev.carregar_ambiente"), \
                    patch("jev_bot.jev.urlopen", side_effect=_http_error(
                        status, {"error": {"message": "negado"}})):
                with self.assertRaises(jev.JevError) as err:
                    jev.avaliar_entrada(self._proposta_valida())
                self.assertEqual(err.exception.code, f"HTTP_{status}")
        with self._ambiente(), patch("jev_bot.jev.carregar_ambiente"), \
                patch("jev_bot.jev.urlopen", side_effect=TimeoutError()):
            with self.assertRaises(jev.JevError) as err:
                jev.avaliar_entrada(self._proposta_valida())
            self.assertEqual(err.exception.stage, "timeout")
        resposta = Mock()
        resposta.__enter__ = Mock(return_value=resposta)
        resposta.__exit__ = Mock(return_value=False)
        resposta.read.return_value = b"{nao json}"
        with self._ambiente(), patch("jev_bot.jev.carregar_ambiente"), \
                patch("jev_bot.jev.urlopen", return_value=resposta):
            with self.assertRaises(jev.JevError) as err:
                jev.avaliar_entrada(self._proposta_valida())
            self.assertEqual(err.exception.stage, "parse_error")

    def test_sanitizacao_remove_segredos(self):
        texto = jev.sanitizar_texto("Authorization: Bearer sk-or-abcdefgh12345678 erro")
        self.assertNotIn("sk-or-abcdefgh12345678", texto)
        self.assertIn("***", texto)


class TestOrquestradorEstados(unittest.TestCase):
    """Estágio terminal, contadores por sessão e ausência de ordens em erro."""

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
        candles = candles_deterministicos(n=400)
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

    def _erro_400(self):
        return jev.JevError("HTTP_400", "http_error", http_success=False, http_status=400,
                            error_type="invalid_request_error", error_code="invalid_type",
                            error_message="unknown question type 'approval'")

    def test_j_orquestrador_erro400_terminal(self):
        with tempfile.TemporaryDirectory() as pasta:
            orch = self._orquestrador(pasta)
            with patch("jev_bot.orquestrador.jev.avaliar_entrada", side_effect=self._erro_400()):
                orch._executar_ciclo("c1")
                self._drenar(orch)
            self.assertEqual(len(orch.book.fills), 0)
            linha = [r for c, r in orch.eventos if c == "decisao"][-1]
            self.assertEqual(linha["decisao"], "ERRO")
            self.assertEqual(linha["estado_api"], "ERROR_HTTP_400")
            etapas = [e["etapa"] for e in orch.funil_eventos]
            self.assertIn("jev_error", etapas)
            terminal = [e for e in orch.funil_eventos if e["etapa"] == "jev_error"][-1]
            self.assertEqual(terminal["estagio_terminal"], "JEV_REQUEST_ERROR")
            self.assertEqual(orch.contadores_sessao["jev_error"], 1)
            self.assertEqual(orch.contadores_sessao["jev_http_400"], 1)
            self.assertEqual(orch.contadores_sessao["jev_yes"], 0)
            motivos = {r.get("motivo") for c, r in orch.eventos if c == "oportunidade"}
            self.assertIn("JEV_REQUEST_ERROR", motivos)

    # P. Duas requisições HTTP 400 → 2 tentativas, 2 erros, 0 aprovações, 0 paper.
    def test_p_duas_requisicoes_400(self):
        with tempfile.TemporaryDirectory() as pasta:
            orch = self._orquestrador(pasta)
            with patch("jev_bot.orquestrador.jev.avaliar_entrada",
                       side_effect=[self._erro_400(), self._erro_400()]):
                orch._executar_ciclo("c1")
                self._drenar(orch)
                orch.proxima_tentativa_jev = 0
                orch.analisados_jev.clear()
                orch._executar_ciclo("c2")
                self._drenar(orch)
            self.assertEqual(orch.contadores_sessao["jev_enqueued"], 2)
            self.assertEqual(orch.contadores_sessao["jev_error"], 2)
            self.assertEqual(orch.contadores_sessao["jev_http_400"], 2)
            self.assertEqual(orch.contadores_sessao.get("jev_yes", 0), 0)
            self.assertEqual(orch.contadores_sessao.get("paper_opened", 0), 0)
            self.assertEqual(len(orch.book.fills), 0)

    # Q. Histórico antigo não conta como posição aberta; reabrir não duplica.
    def test_q_historico_antigo_nao_duplica(self):
        with tempfile.TemporaryDirectory() as pasta:
            orch = self._orquestrador(pasta)
            banco = Path(pasta) / "db.json"
            orch.book.trades.append({
                "symbol": "XNGUSD", "action": "SELL", "status": "CLOSED", "trade_id": "antigo",
                "opened_at": "2026-10-01T00:00:00+00:00", "exit": 1.0, "profit": 1.0,
                "mode": "paper", "reason": "TIME_EXIT_M1", "volume": 0.1,
                "entry": 1.0, "stop": 1.1, "target": 0.9})
            orch._persistir()
            recarregado = Book.load(banco, "USD")
            self.assertEqual(len(recarregado.fills), 0)      # não é posição aberta
            self.assertEqual(len(recarregado.trades), 1)
            recarregado.save(banco)
            de_novo = Book.load(banco, "USD")
            self.assertEqual(len(de_novo.trades), 1)         # sem duplicação

    # O. GUI: motivo completo acessível; ERRO_API distinto de REJECTED; sem PENDING.
    def test_o_gui_estados_e_tooltip(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        QApplication.instance() or QApplication([])
        from jev_bot.gui.aba_operacao import AbaOperacao
        aba = AbaOperacao(acoes={k: (lambda: None) for k in
                                 ("iniciar", "pausar", "retomar", "emergencia",
                                  "liberar_risco", "aplicar_limites", "cancelar_ciclo")})
        base = {"id": "c1", "instante": "2026-01-01T00:00:00+00:00", "padrao": "s",
                "candle_ref": 1, "side_proposed": "BUY", "execucao": "-"}
        aba.receber_lote("decisao", [
            {**base, "simbolo": "XNGUSD", "decisao": "ERRO", "estado_api": "ERROR_HTTP_400",
             "justificativa": "JEv indisponível (http_error); HTTP 400 unknown question type",
             "detalhes": {"error_type": "invalid_request_error"}},
            {**base, "simbolo": "XBRUSD", "decisao": "NAO", "estado_api": "COMPLETED",
             "justificativa": "rejeitado pelo JEv"},
            {**base, "simbolo": "PEND", "decisao": "PENDING", "estado_api": "PENDING"},
        ])
        simbolos = [aba.lista_decisao.topLevelItem(i).text(0)
                    for i in range(aba.lista_decisao.topLevelItemCount())]
        self.assertEqual(simbolos, ["XBRUSD", "XNGUSD"])  # PENDING oculto
        erros = [i for i in range(2) if aba.lista_decisao.topLevelItem(i).text(2) == "ERRO"]
        self.assertTrue(erros)
        self.assertIn("HTTP 400", aba.lista_decisao.topLevelItem(erros[0]).text(5))


class TestPreRiscoPreciso(unittest.TestCase):
    """Vetos pré-risco com código e medidas distintas (não uma mensagem genérica)."""

    def _book(self, pasta):
        book = Book.load(Path(pasta) / "db.json", "USD")
        book.active = True
        book.prices_valid = True
        return book

    def test_spread_stop_vs_preco(self):
        with tempfile.TemporaryDirectory() as pasta:
            book = self._book(pasta)
            estado = estado_mt5("TESTE", closes=[100.0] * 30)
            # spread grande em relação ao stop (0.5) mas dentro do limite do preço
            estado.bid, estado.ask = 99.975, 100.025
            gate = pre_check("BUY", book, book.limits, estado)
            self.assertEqual(gate.verdict, "EXECUTE")
            self.assertEqual(gate.reason_code, "APPROVED")
            self.assertIn("spread_fracao_stop", gate.details)
            self.assertIn("limite_spread_stop_pct", gate.details)

    def test_dados_ausentes_e_ask_invertido(self):
        with tempfile.TemporaryDirectory() as pasta:
            book = self._book(pasta)
            ausente = estado_mt5("TESTE", closes=[100.0] * 30)
            ausente.bid = None
            gate = pre_check("BUY", book, book.limits, ausente)
            self.assertEqual(gate.reason_code, "INVALID_MARKET_DATA")
            invertido = estado_mt5("TESTE", closes=[100.0] * 30)
            invertido.bid, invertido.ask = 101.0, 100.0
            gate2 = pre_check("BUY", book, book.limits, invertido)
            self.assertEqual(gate2.reason_code, "SPREAD_INVALID")

    def test_lote_minimo_incompativel(self):
        import dataclasses
        with tempfile.TemporaryDirectory() as pasta:
            book = self._book(pasta)
            estado = dataclasses.replace(estado_mt5("TESTE", closes=[100.0] * 30),
                                         loss_per_lot=1e9)
            gate = pre_check("BUY", book, book.limits, estado)
            self.assertEqual(gate.reason_code, "APPROVED")
            self.assertEqual(gate.volume, estado.volume_min)


class TestBotaoOpenRouter(unittest.TestCase):
    """Botão reativado: local sem rede; remoto opt-in; isolado do motor."""

    def test_local_valido(self):
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "mock"}, clear=True), \
                patch("jev_bot.jev.carregar_ambiente"):
            dados = cfg_gui.validar_openrouter_local()
        self.assertTrue(dados["ok"])
        self.assertIn("payload_binario", [e[0] for e in dados["etapas"]])

    def test_local_sem_chave(self):
        with patch.dict("os.environ", {}, clear=True), \
                patch("jev_bot.jev.carregar_ambiente"):
            dados = cfg_gui.validar_openrouter_local()
        self.assertFalse(dados["ok"])
        self.assertIn("ausente", [e[1] for e in dados["etapas"] if e[0] == "chave"])

    def test_remoto_ok_e_erros(self):
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "mock"}, clear=True), \
                patch("jev_bot.jev.carregar_ambiente"), \
                patch("jev_bot.jev.urlopen", return_value=_ok_choice("YES")):
            dados = cfg_gui.testar_openrouter_remoto()
        self.assertTrue(dados["ok"])
        self.assertTrue(dados["resposta"]["approved"])
        for status in (400, 401, 402, 429, 500):
            with self.subTest(http=status), \
                    patch.dict("os.environ", {"OPENROUTER_API_KEY": "mock"}, clear=True), \
                    patch("jev_bot.jev.carregar_ambiente"), \
                    patch("jev_bot.jev.urlopen",
                          side_effect=_http_error(status, {"error": {"message": "falha"}})):
                dados = cfg_gui.testar_openrouter_remoto()
            self.assertFalse(dados["ok"])
            self.assertEqual(dados["http_status"], status)

    def test_compacto_usa_contadores_do_fluxo_binario(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        QApplication.instance() or QApplication([])
        from jev_bot.gui.aba_operacao import AbaOperacao
        aba = AbaOperacao(acoes={k: (lambda: None) for k in
                                 ("iniciar", "pausar", "retomar", "emergencia",
                                  "liberar_risco", "aplicar_limites", "cancelar_ciclo")})
        aba.receber_metricas({"estado": "AGUARDANDO", "diagnostico_funil": {
            "totais": {"jev_enqueued": 79, "jev_request_started": 79,
                       "jev_response_received": 79, "jev_yes": 1, "jev_no": 77,
                       "jev_error": 1, "jev_expired": 3, "pre_risk_pass": 4,
                       "pre_risk_reject": 2, "sem_entrada_python": 1, "paper_opened": 1},
            "pool_matematico": {}, "analises_pendentes": 0, "max_parallel_symbols": 4}})
        texto = aba.diagnostico_funil.text()
        self.assertIn("env 79", texto)
        self.assertIn("SIM 1/NÃO 77/ERRO 1", texto)
        self.assertIn("exp 3", texto)
        self.assertIn("pré 4ok/2veto", texto)

    def test_botao_isolado_do_motor(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        QApplication.instance() or QApplication([])
        from jev_bot.gui.aba_config import AbaConfiguracao
        aba = AbaConfiguracao()
        self.assertTrue(aba.btn_testar_or.isEnabled())
        from PySide6.QtWidgets import QMessageBox
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "mock"}, clear=True), \
                patch("jev_bot.jev.carregar_ambiente"), \
                patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No):
            aba._testar_openrouter()  # local apenas; remoto recusado
        self.assertIn("local", aba.status_or.text().lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
