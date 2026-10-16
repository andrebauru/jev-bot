"""Janela principal PySide6: três abas, worker MT5 e encerramento seguro."""

from __future__ import annotations

import sys
import subprocess

from PySide6.QtCore import Qt, QThread
from PySide6.QtWidgets import (QApplication, QLabel, QMainWindow, QMessageBox,
                               QStatusBar, QTabWidget)

from .aba_config import AbaConfiguracao
from .aba_operacao import AbaOperacao
from .aba_resumo import AbaResumo
from .aba_monitoramento import AbaMonitoramento
from .motor import MotorTrabalhador
from . import tema


class JanelaPrincipal(QMainWindow):
    """Monta as abas e interliga os pacotes do motor."""

    def __init__(self, terminal=None, offset_horas=None, banco=None, caminho_selecao=None):
        super().__init__()
        self.setWindowTitle("JEv Bot — paper trading quantitativo local")
        self.resize(1280, 800)
        self.setMinimumSize(720, 480)
        self.setWindowState(self.windowState() | Qt.WindowMaximized)

        self.motor = MotorTrabalhador(terminal=terminal, offset_horas=offset_horas,
                                      banco=banco, caminho_selecao=caminho_selecao)
        self.orquestrador = self.motor.orquestrador

        self.abas = QTabWidget()
        self.aba_operacao = AbaOperacao(acoes={
            "iniciar": self.orquestrador.iniciar,
            "pausar": self.orquestrador.pausar,
            "retomar": self.orquestrador.retomar,
            "emergencia": self.orquestrador.emergencia,
            "liberar_risco": self.orquestrador.liberar_risco,
            "aplicar_limites": self.orquestrador.aplicar_limites,
            "cancelar_ciclo": self.orquestrador.cancelar_ciclo,
        })
        self.aba_resumo = AbaResumo()
        self.aba_monitoramento = AbaMonitoramento(self.aba_operacao)
        self.aba_config = AbaConfiguracao(config_alterada=self.orquestrador.recarregar_config,
                                          pedir_catalogo=self.orquestrador.pedir_catalogo,
                                          selecao_salva=self.orquestrador.recarregar_selecao)
        self.abas.addTab(self.aba_operacao, "Operação e fluxo")
        self.abas.addTab(self.aba_monitoramento, "Monitoramento e tabelas")
        self.abas.addTab(self.aba_resumo, "Resumo e gráficos")
        self.abas.addTab(self.aba_config, "Configurações")
        self.setCentralWidget(self.abas)

        barra = QStatusBar()
        self.label_jev = QLabel("JEv: verificando")
        barra.addWidget(self.label_jev)
        self.label_mt5 = QLabel("MT5: verificando")
        self.label_operacao = QLabel("Modo: PAPER | Timeframes: M1 ativo / M15 aguardando coleta | Posições: 0/5")
        barra.addWidget(self.label_mt5)
        barra.addWidget(self.label_operacao)
        barra.addPermanentWidget(QLabel("PAPER — sem ordens reais"))
        self.setStatusBar(barra)

        self.motor.sinal_pacote.connect(self._receber_pacote)
        self.motor.finished.connect(self._motor_terminou)

    def showEvent(self, evento):
        super().showEvent(evento)
        if not self.motor.isRunning():
            self.motor.start(QThread.HighPriority)

    def closeEvent(self, evento):
        """Encerra o motor com aviso; a carteira é salva pela thread do motor."""
        resposta = QMessageBox.question(
            self, "Encerrar JEv Bot",
            "Encerrar o robô? Posições paper continuam registradas e pausadas no db.json.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if resposta != QMessageBox.Yes:
            evento.ignore()
            return
        self.aba_operacao.salvar_layout()
        self.orquestrador.parar()
        if not self.motor.wait(15000):
            QMessageBox.warning(self, "Encerramento", "O motor não encerrou em 15s; "
                                "a carteira será salva quando a thread terminar.")
        super().closeEvent(evento)

    def _receber_pacote(self, pacote):
        """Roteia o pacote coalescido: listas por canal e registros únicos."""
        for canal in ("calc", "oportunidade", "decisao", "log", "resumo"):
            registros = pacote.get(canal)
            if registros:
                self.aba_operacao.receber_lote(canal, registros)
        if "metricas" in pacote:
            m = pacote["metricas"]
            self.aba_operacao.receber_metricas(m)
            self.aba_resumo.receber_metricas(m)
            self.label_jev.setText(f"JEv: {'ok' if m.get('jev_ok') else 'não configurado'}")
            self.label_mt5.setText("MT5: conectado" if m.get("precos_validos") else "MT5: sem cotação")
            self.label_operacao.setText(
                f"Modo: PAPER | Timeframes: M1 ativo / M15 aguardando coleta | Posições: {m.get('posicoes', 0)}/5 | "
                f"Drawdown: {m.get('drawdown', 0):.1%}/{m.get('limites', {}).get('max_drawdown', .1):.1%}")
        if "estado" in pacote:
            self.aba_operacao.receber_estado(pacote["estado"])
        if "trades" in pacote:
            self._receber_trades(pacote["trades"])
        if "catalogo" in pacote:
            self.aba_config.receber_catalogo(pacote["catalogo"])

    def _receber_trades(self, trades):
        self.aba_operacao.receber_trades(trades[-200:])
        self.aba_resumo.receber_trades(trades)

    def _motor_terminou(self):
        if self.orquestrador.erro:
            resposta = QMessageBox.question(
                self, "MetaTrader 5 indisponível",
                "MetaTrader 5 não está disponível. Deseja abrir o terminal agora?\n\n"
                f"Detalhe: {self.orquestrador.erro}",
                QMessageBox.StandardButton.Open | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if resposta == QMessageBox.StandardButton.Open:
                try:
                    caminho = self.orquestrador.terminal
                    if caminho:
                        subprocess.Popen([str(caminho)])
                except (OSError, TypeError) as exc:
                    QMessageBox.warning(self, "MetaTrader 5", f"Não foi possível abrir o terminal: {exc}")


def executar(terminal=None, offset_horas=None, banco=None, caminho_selecao=None):
    """Abre a aplicação; retorna o código de saída do Qt."""
    app = QApplication.instance() or QApplication(sys.argv)
    app.setStyleSheet(tema.FOLHA)
    janela = JanelaPrincipal(terminal=terminal, offset_horas=offset_horas,
                             banco=banco, caminho_selecao=caminho_selecao)
    janela.show()
    return app.exec()
