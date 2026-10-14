"""Fumaça da interface offscreen: abas recebem pacotes e permanecem íntegras."""

import tempfile
from pathlib import Path
from unittest.mock import patch
import unittest

from PySide6.QtWidgets import QLabel, QLineEdit

from jev_bot import configuracao


class TestGui(unittest.TestCase):
    """Fumaça da interface offscreen: abas recebem sinais e permanecem íntegras."""

    @classmethod
    def setUpClass(cls):
        import os
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_aba_operacao_recebe_pipeline(self):
        from jev_bot.gui.aba_operacao import AbaOperacao
        abas = AbaOperacao(acoes={k: (lambda: None) for k in
                                  ("iniciar", "pausar", "retomar", "emergencia",
                                   "liberar_risco", "aplicar_limites", "cancelar_ciclo")})
        abas.receber_metricas({"estado": "ANALISANDO", "detalhe": "ciclo", "pausado": False,
                               "parado_risco": False, "motivo_risco": "", "moeda": "JPY",
                               "equity": 10100.0, "saldo": 10050.0, "realizado": 50.0,
                               "aberto": 50.0, "exposicao": 100.0, "exposicao_fracao": .01,
                               "drawdown": .01, "drawdown_max": .03, "posicoes": 1,
                               "monitorados": 3, "operaveis": 2,
                               "limites": {"risk_per_trade": .02, "max_total_risk": .15,
                                           "max_drawdown": .1},
                               "conta": {"balance": 1, "equity": 1, "profit": 1, "currency": "JPY"}})
        self.assertEqual(abas.chip_estado.text(), "ANALISANDO")
        abas.receber_lote("calc", [{"id": "c1", "simbolo": "BTCUSD",
                                    "instante": "2026-01-01T00:00:00+00:00",
                                    "etapa": "calculado", "mensagem": "ok",
                                    "amostra": {"candles": 100, "dias": 1.0},
                                    "padroes_testados": 2, "resultado": "r", "erro": None}])
        abas.receber_lote("calc", [{"id": "c1", "simbolo": "BTCUSD",
                                    "instante": "2026-01-01T00:00:00+00:00",
                                    "etapa": "janela", "mensagem": "Baixando histórico M1 (30 dias)",
                                    "amostra": {"candles": 21600, "solicitados": 43200},
                                    "padroes_testados": None, "resultado": None, "erro": None}])
        self.assertIn("21600/43200", abas.progresso_download.text())
        abas.receber_lote("oportunidade", [{"id": "c1", "simbolo": "BTCUSD",
                                            "instante": "2026-01-01T00:00:00+00:00",
                                            "motivo": "NO_SETUP",
                                            "estado": "sem padrão elegível"}])
        abas.receber_lote("decisao", [{"id": "c1", "simbolo": "BTCUSD",
                                       "instante": "2026-01-01T00:00:00+00:00",
                                       "padrao": "abc123", "candle_ref": 1,
                                       "direcao": "BUY", "decisao": "SIM",
                                       "oportunidade": "quedas", "justificativa": "j",
                                       "risco": "autorizado", "execucao": "paper aberto"}])
        self.assertEqual(abas.lista_calc.topLevelItemCount(), 2)
        self.assertEqual(abas.lista_oportunidade.topLevelItemCount(), 1)
        self.assertEqual(abas.lista_decisao.topLevelItemCount(), 1)
        self.assertEqual(abas.lista_decisao.topLevelItem(0).text(2), "SIM")
        # Resumo de ciclo vai ao painel de status, não à lista de decisões.
        abas.receber_lote("resumo", [{"id": "c1", "ciclo": 1,
                                      "instante": "2026-01-01T00:00:00+00:00",
                                      "oportunidades": 1, "motivos": "enviados_jev=1",
                                      "pendentes": 0, "duracao_s": 3.0}])
        self.assertIn("1 oportunidade", abas.resumo_ciclo.text())
        self.assertEqual(abas.lista_decisao.topLevelItemCount(), 1)
        trade = {"symbol": "BTCUSD", "action": "BUY", "entry": 100.0, "volume": .01,
                 "price_now": 100.5, "exit": None, "profit": 5.0, "risk_amount": 10.0,
                 "opened_at": "2026-01-01T00:00:00+00:00", "closed_at": None,
                 "status": "OPEN", "mode": "paper", "reason": ""}
        abas.receber_trades([trade])
        self.assertEqual(abas.tabela.rowCount(), 1)
        self.assertEqual(abas.tabela.item(0, 11).text(), "OPEN")

    def test_aba_resumo_series_e_filtros(self):
        from jev_bot.gui.aba_resumo import AbaResumo
        aba = AbaResumo()
        trades = [{"symbol": "BTCUSD", "status": "CLOSED", "profit": 10.0,
                   "closed_at": "2026-01-01T01:00:00+00:00"},
                  {"symbol": "ETHUSD", "status": "CLOSED", "profit": -5.0,
                   "closed_at": "2026-01-01T02:00:00+00:00"}]
        aba.receber_trades(trades)
        aba.receber_metricas({"moeda": "USD", "saldo": 10005.0, "realizado": 5.0,
                              "equity": 10010.0, "growth": .001, "daily_return": .001,
                              "drawdown": 0, "drawdown_max": 0, "win_rate": .5,
                              "fechados": 2, "wins": 1, "losses": 1, "aberto": 5.0,
                              "exposicao": 0, "margem": 0, "posicoes": 0})
        self.assertEqual(aba.combo_ativo.count(), 3)  # Todos + 2 ativos
        self.assertGreater(aba.grafico_equity.serie.count(), 1)
        self.assertGreater(aba.grafico_dd.serie.count(), 1)
        self.assertEqual(aba.valores["realizado"].text(), "5,00 USD")
        aba.combo_ativo.setCurrentIndex(1)
        self.assertGreater(aba.grafico_equity.serie.count(), 0)

    def test_paineis_com_titulo_contagem_e_resumo(self):
        """Painéis identificados com contagem, último processamento e resumo."""
        import os
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        QApplication.instance() or QApplication([])
        from jev_bot.gui.aba_operacao import AbaOperacao
        abas = AbaOperacao(acoes={k: (lambda: None) for k in
                                  ("iniciar", "pausar", "retomar", "emergencia",
                                   "liberar_risco", "aplicar_limites", "cancelar_ciclo")})
        abas.receber_lote("decisao", [
            {"id": "c1", "simbolo": "BTCUSD", "instante": "2026-01-01T00:00:00+00:00",
             "oportunidade": "quedas(n=3) → BUY", "decisao": "SIM",
             "justificativa": "padrão validado", "risco": "gate EXECUTE",
             "execucao": "paper aberto"},
            {"id": "c1", "simbolo": "", "instante": "2026-01-01T00:00:00+00:00",
             "oportunidade": "Ciclo 1", "decisao": "RESUMO",
             "justificativa": "sem_padrao=80; rejeitado_validacao=10; executadas=1",
             "risco": "pendentes: 0", "execucao": "duração 12.3s"},
        ])
        self.assertIn("1 itens", abas.painel_decisao.contagem.text())
        self.assertIn("00:00:00", abas.painel_decisao.ultimo.text())
        self.assertEqual(abas.lista_decisao.topLevelItemCount(), 1)

    def test_grafico_copia_como_imagem_no_clipboard(self):
        """COPIAR COMO IMAGEM coloca o gráfico visível na área de transferência."""
        import os
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtGui import QGuiApplication
        from jev_bot.gui.graficos import GraficoView
        grafico = GraficoView("teste")
        grafico.definir_pontos([(1_700_000_000_000, 100.0), (1_700_000_060_000, 101.0)])
        QGuiApplication.clipboard().clear()
        pixmap = grafico.grab()
        QGuiApplication.clipboard().setPixmap(pixmap)
        copia = QGuiApplication.clipboard().pixmap()
        self.assertFalse(copia.isNull())
        self.assertEqual(copia.size(), pixmap.size())

    def test_aba_config_mascara_e_modo_paper(self):
        from jev_bot.gui.aba_config import AbaConfiguracao
        with tempfile.TemporaryDirectory() as pasta, \
                patch.object(configuracao, "CAMINHO_ENV", Path(pasta) / ".env"), \
                patch.dict("os.environ", {}, clear=True):
            aba = AbaConfiguracao()
            self.assertEqual(aba.campo_chave.echoMode(), QLineEdit.EchoMode.Password)
            self.assertEqual(aba.campo_senha.echoMode(), QLineEdit.EchoMode.Password)
            self.assertTrue(aba.radio_paper.isChecked())
            self.assertFalse(aba.radio_real.isEnabled())
            aba.campo_modelo.setText("deepseek/deepseek-chat")
            aba._salvar_openrouter()
            self.assertIn("Modelo salvo", aba.status_or.text())
            arquivo = Path(pasta) / ".env"
            arquivo.write_text("OPENROUTER_API_KEY=chave-existente\n", encoding="utf-8")
            configuracao.carregar_ambiente()
            aba.campo_modelo.setText("deepseek/deepseek-chat")
            aba._salvar_openrouter()
            self.assertIn("OPENROUTER_API_KEY", arquivo.read_text(encoding="utf-8"))

    def test_janela_monta_abas_e_trabalhador(self):
        from jev_bot.gui.janela import JanelaPrincipal
        janela = JanelaPrincipal(banco=":memoria:")
        self.assertEqual(janela.abas.count(), 3)
        self.assertFalse(janela.motor.isRunning())
        janela._receber_pacote({"metricas": {"estado": "PRONTO", "detalhe": "", "jev_ok": True,
                                             "deepseek_calls": 0,
                                             "monitorados": 1, "operaveis": 1}})
        self.assertEqual(janela.label_jev.text(), "JEv: ok")
        self.assertTrue(any("DeepSeek: desativado (0 chamadas)" in label.text()
                            for label in janela.statusBar().findChildren(QLabel)))
        janela.orquestrador.parar()

    def test_diagnostico_funil_mostra_zeros_e_primeiras_rejeicoes(self):
        from jev_bot.gui.aba_operacao import AbaOperacao
        aba = AbaOperacao(acoes={k: (lambda: None) for k in
                                 ("iniciar", "pausar", "retomar", "emergencia",
                                  "liberar_risco", "aplicar_limites", "cancelar_ciclo")})
        totais = {k: 0 for k in ("symbol_selected", "symbol_operable", "symbol_available_mt5",
                                 "fetch_ok", "candidate_generated", "quant_validated",
                                 "queue_enqueued", "jev_request_started", "jev_response_received",
                                 "decision_buy", "decision_sell", "decision_hold", "decision_no",
                                 "paper_opened")}
        aba.receber_metricas({"estado": "ANALISANDO", "diagnostico_funil": {
            "totais": totais,
            "motivos_rejeicao_top3": [{"codigo": "OOS_RATE_BELOW_MIN", "quantidade": 4}],
            "ultimo_envio_jev_age_s": None, "idade_fila_s": 0,
            "ultima_resposta_jev_age_s": None, "fila_size": 0, "worker_alive": False}})
        texto = aba.diagnostico_funil.text()
        self.assertIn("sel 0", texto)
        self.assertIn("resp 0", texto)
        dica = aba.diagnostico_funil.toolTip()
        self.assertIn("OOS_RATE_BELOW_MIN:4", dica)
        self.assertIn("último envio: nunca", dica)
        self.assertIn("idade da fila: 0s", dica)

    def test_atividade_matematica_mostra_estados(self):
        from jev_bot.gui.aba_operacao import AbaOperacao
        aba = AbaOperacao(acoes={k: (lambda: None) for k in
                                 ("iniciar", "pausar", "retomar", "emergencia",
                                  "liberar_risco", "aplicar_limites", "cancelar_ciclo")})
        aba.receber_metricas({"estado": "ANALISANDO", "diagnostico_funil": {
            "totais": {}, "atividade": [
                {"simbolo": "BTCUSD", "estado": "CALCULATING", "buy": 3, "sell": 1,
                 "candle_ref": None, "motivo": None},
                {"simbolo": "USDJPY", "estado": "REJECTED", "buy": 1, "sell": 2,
                 "candle_ref": None, "motivo": "NEGATIVE_EXPECTANCY"}],
            "pool_matematico": {"pico_concorrencia": 4, "duracao_p50_ms": 10,
                                "duracao_p95_ms": 40},
            "max_parallel_symbols": 4, "analises_pendentes": 2}})
        self.assertEqual(aba.tabela_atividade.rowCount(), 2)
        self.assertEqual(aba.tabela_atividade.item(0, 0).text(), "BTCUSD")
        self.assertEqual(aba.tabela_atividade.item(0, 1).text(), "CALCULATING")
        self.assertIn("BUY 3", aba.tabela_atividade.item(0, 3).text())
        self.assertEqual(aba.tabela_atividade.item(1, 5).text(), "NEGATIVE_EXPECTANCY")
        self.assertIn("pico 4", aba.diagnostico_funil.text())

    def test_splitters_redimensionaveis_em_janela_normal_e_reduzida(self):
        """Conteúdo de operação mantém listas utilizáveis em dimensões variadas."""
        from PySide6.QtWidgets import QWidget, QVBoxLayout, QSplitter
        from jev_bot.gui.aba_operacao import AbaOperacao
        aba = AbaOperacao(acoes={k: (lambda: None) for k in
                                 ("iniciar", "pausar", "retomar", "emergencia",
                                  "liberar_risco", "aplicar_limites", "cancelar_ciclo")})
        janela = QWidget()
        layout = QVBoxLayout(janela)
        layout.addWidget(aba)
        janela.show()
        for largura, altura in ((1366, 768), (1920, 1080), (720, 480)):
            janela.resize(largura, altura)
            self.app.processEvents()
            self.assertIsInstance(aba.divisor_principal, QSplitter)
            self.assertIsInstance(aba.divisor_colunas, QSplitter)
            self.assertEqual(aba.divisor_principal.count(), 2)
            self.assertEqual(aba.divisor_colunas.count(), 2)
            self.assertGreater(aba.lista_oportunidade.height(), 0)
            self.assertGreater(aba.lista_decisao.height(), 0)
            self.assertGreater(aba.tabela.height(), 0)
        self.assertEqual(aba.lista_decisao.columnCount(), 7)
        self.assertFalse(hasattr(aba, "painel_deepseek"))
        aba.divisor_principal.setSizes([500, 180])
        aba.salvar_layout()
        janela.close()

    def test_salvar_variaveis_preserva_e_substitui(self):
        with tempfile.TemporaryDirectory() as pasta:
            arquivo = Path(pasta) / ".env"
            arquivo.write_text("A=1\nMT5_TERMINAL_PATH='C:\\antigo\\terminal64.exe'\n# comentário\n", encoding="utf-8")
            with patch.object(configuracao, "CAMINHO_ENV", arquivo), \
                    patch.dict("os.environ", {"X_TESTE": "y"}, clear=True):
                configuracao.salvar_variaveis({"MT5_TERMINAL_PATH": r"C:\novo\terminal64.exe",
                                               "NOVA": "2"})
                texto = arquivo.read_text(encoding="utf-8")
                self.assertIn("A=1", texto)
                self.assertIn("# comentário", texto)
                self.assertIn(r"C:\novo\terminal64.exe", texto)
                self.assertIn("NOVA=2", texto)
                configuracao.salvar_variaveis({"NOVA": None})
                self.assertNotIn("NOVA", arquivo.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
