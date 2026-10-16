"""Aba dedicada às tabelas detalhadas do fluxo M1/M15.

Os widgets são os mesmos modelos visuais da operação; não há cópia de dados
nem segundo pipeline. A aba apenas separa a área operacional das tabelas.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QTabWidget, QVBoxLayout, QWidget


class AbaMonitoramento(QWidget):
    """Monitoramento e tabelas, com categorias internas e layout amplo."""

    def __init__(self, operacao, parent=None):
        super().__init__(parent)
        raiz = QVBoxLayout(self)
        raiz.setContentsMargins(8, 8, 8, 8)
        self.internas = QTabWidget()
        self.internas.setDocumentMode(True)
        self.internas.setUsesScrollButtons(True)
        self.internas.addTab(operacao.grupo_atividade, "Atividade matemática")
        self.internas.addTab(operacao.divisor_principal, "Cálculos · JEv · Paper")
        self.internas.setMinimumHeight(520)
        raiz.addWidget(self.internas, 1)
        # O histórico e as tabelas passam a viver aqui; a aba operacional fica limpa.
        operacao.grupo_atividade.setMinimumHeight(260)
        operacao.tabela_atividade.verticalHeader().setDefaultSectionSize(28)
        operacao.tabela.verticalHeader().setDefaultSectionSize(28)
        for tabela in (operacao.tabela_atividade, operacao.tabela):
            tabela.setAlternatingRowColors(True)
            tabela.setWordWrap(False)
            tabela.setTextElideMode(Qt.TextElideMode.ElideRight)
