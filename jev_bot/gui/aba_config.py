"""Aba 3 — Configurações: OpenRouter, MetaTrader 5 e modo de negociação.

Chaves e senhas são mascaradas, nunca exibidas ou registradas em log, e
gravadas preferencialmente no cofre do sistema (keyring) com fallback
explícito ao .env existente. Testes de conexão não habilitam ordens reais.
"""

from __future__ import annotations

import threading

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import (QButtonGroup, QFileDialog, QGroupBox, QHBoxLayout,
                               QLabel, QLineEdit, QPushButton,
                               QRadioButton, QVBoxLayout, QWidget)

from .. import configuracao
from . import config as cfg_gui
from .mercados_widget import PainelMercados


class Testes(QObject):
    """Resultados de testes executados fora da thread da interface."""

    resultado_openrouter = Signal(dict)
    resultado_mt5 = Signal(dict)


class AbaConfiguracao(QWidget):
    """Configuração segura de credenciais e modo paper obrigatório."""

    def __init__(self, config_alterada=None, pedir_catalogo=None, selecao_salva=None, parent=None):
        super().__init__(parent)
        self.config_alterada = config_alterada or (lambda: None)
        self.testes = Testes()
        self.testes.resultado_openrouter.connect(self._res_openrouter)
        self.testes.resultado_mt5.connect(self._res_mt5)
        self.painel_mercados = PainelMercados(pedir_catalogo=pedir_catalogo)
        if selecao_salva is not None:
            self.painel_mercados.selecao_salva.connect(selecao_salva)

        raiz = QVBoxLayout(self)
        raiz.setContentsMargins(10, 8, 10, 8)

        # ---------------------------------------------------------- OpenRouter
        grupo_or = QGroupBox("OpenRouter (decisão JEv; configuração DeepSeek legada e inativa)")
        layout_or = QVBoxLayout(grupo_or)
        linha_chave = QHBoxLayout()
        rotulo_chave = QLabel("API key:")
        rotulo_chave.setStyleSheet("color:#8ea3c8;")
        self.campo_chave = QLineEdit()
        self.campo_chave.setEchoMode(QLineEdit.EchoMode.Password)
        self.campo_chave.setPlaceholderText(f"•••••••• — {cfg_gui.fonte_chave_openrouter()}")
        linha_chave.addWidget(rotulo_chave)
        linha_chave.addWidget(self.campo_chave, 1)
        layout_or.addLayout(linha_chave)
        linha_modelo = QHBoxLayout()
        rotulo_modelo = QLabel("Modelo DeepSeek legado (não utilizado):")
        rotulo_modelo.setStyleSheet("color:#8ea3c8;")
        self.campo_modelo = QLineEdit(configuracao.modelo_deepseek())
        self.campo_modelo.setPlaceholderText("configuração preservada; sem chamadas DeepSeek")
        self.campo_modelo.setToolTip("Valor legado preservado para compatibilidade; não participa do fluxo operacional.")
        linha_modelo.addWidget(rotulo_modelo)
        linha_modelo.addWidget(self.campo_modelo, 1)
        layout_or.addLayout(linha_modelo)
        endpoint = QLabel(f"Endpoint fixo: {configuracao.ENDPOINT_CHAT_PADRAO} | "
                          "chave nunca aparece em logs")
        endpoint.setStyleSheet("color:#8ea3c8;")
        layout_or.addWidget(endpoint)
        linha_botoes = QHBoxLayout()
        self.btn_testar_or = QPushButton("TESTAR CONEXÃO")
        self.btn_testar_or.clicked.connect(self._testar_openrouter)
        self.btn_testar_or.setEnabled(False)
        self.btn_testar_or.setToolTip("Teste DeepSeek desativado: o bot não consulta esse serviço.")
        self.btn_salvar_or = QPushButton("SALVAR")
        self.btn_salvar_or.setObjectName("btnAplicar")
        self.btn_salvar_or.clicked.connect(self._salvar_openrouter)
        linha_botoes.addWidget(self.btn_testar_or)
        linha_botoes.addWidget(self.btn_salvar_or)
        linha_botoes.addStretch(1)
        layout_or.addLayout(linha_botoes)
        self.status_or = QLabel("")
        self.status_or.setWordWrap(True)
        layout_or.addWidget(self.status_or)
        raiz.addWidget(grupo_or)

        # ---------------------------------------------------------- MetaTrader 5
        grupo_mt5 = QGroupBox("MetaTrader 5 (terminal já conectado; sem login no programa)")
        layout_mt5 = QVBoxLayout(grupo_mt5)
        linha_caminho = QHBoxLayout()
        rotulo_caminho = QLabel("Executável:")
        rotulo_caminho.setStyleSheet("color:#8ea3c8;")
        self.campo_caminho = QLineEdit(configuracao.terminal_mt5())
        btn_procurar = QPushButton("PROCURAR")
        btn_procurar.clicked.connect(self._procurar_terminal)
        linha_caminho.addWidget(rotulo_caminho)
        linha_caminho.addWidget(self.campo_caminho, 1)
        linha_caminho.addWidget(btn_procurar)
        layout_mt5.addLayout(linha_caminho)
        linha_creds = QHBoxLayout()
        rotulo_login = QLabel("Login:")
        rotulo_login.setStyleSheet("color:#8ea3c8;")
        self.campo_login = QLineEdit()
        rotulo_senha = QLabel("Senha:")
        rotulo_senha.setStyleSheet("color:#8ea3c8;")
        self.campo_senha = QLineEdit()
        self.campo_senha.setEchoMode(QLineEdit.EchoMode.Password)
        rotulo_servidor = QLabel("Servidor:")
        rotulo_servidor.setStyleSheet("color:#8ea3c8;")
        self.campo_servidor = QLineEdit()
        for rotulo, campo in ((rotulo_login, self.campo_login), (rotulo_senha, self.campo_senha),
                              (rotulo_servidor, self.campo_servidor)):
            linha_creds.addWidget(rotulo)
            linha_creds.addWidget(campo)
        layout_mt5.addLayout(linha_creds)
        reserva = QLabel("Login e senha são reservados para uma integração futura: o programa usa a "
                         "conta já aberta no terminal, não faz login, não troca de conta e não envia ordens reais.")
        reserva.setStyleSheet("color:#8ea3c8;")
        reserva.setWordWrap(True)
        layout_mt5.addWidget(reserva)
        linha_botoes_mt5 = QHBoxLayout()
        self.btn_testar_mt5 = QPushButton("TESTAR CONEXÃO")
        self.btn_testar_mt5.clicked.connect(self._testar_mt5)
        self.btn_salvar_mt5 = QPushButton("SALVAR")
        self.btn_salvar_mt5.setObjectName("btnAplicar")
        self.btn_salvar_mt5.clicked.connect(self._salvar_mt5)
        linha_botoes_mt5.addWidget(self.btn_testar_mt5)
        linha_botoes_mt5.addWidget(self.btn_salvar_mt5)
        linha_botoes_mt5.addStretch(1)
        layout_mt5.addLayout(linha_botoes_mt5)
        self.status_mt5 = QLabel("")
        self.status_mt5.setWordWrap(True)
        layout_mt5.addWidget(self.status_mt5)
        self._carregar_credenciais()
        raiz.addWidget(grupo_mt5)

        # ---------------------------------------------------------- Mercados
        grupo_mercados = QGroupBox("Mercados para Monitoramento e Operação")
        layout_mercados = QVBoxLayout(grupo_mercados)
        layout_mercados.addWidget(self.painel_mercados)
        raiz.addWidget(grupo_mercados, 1)

        # ---------------------------------------------------------- Modo
        grupo_modo = QGroupBox("Modo de negociação")
        layout_modo = QVBoxLayout(grupo_modo)
        linha_modo = QHBoxLayout()
        self.radio_paper = QRadioButton("PAPER TRADING")
        self.radio_paper.setChecked(True)
        self.radio_real = QRadioButton("Trading real")
        self.radio_real.setEnabled(False)
        grupo_radios = QButtonGroup(self)
        grupo_radios.addButton(self.radio_paper)
        grupo_radios.addButton(self.radio_real)
        linha_modo.addWidget(self.radio_paper)
        linha_modo.addWidget(self.radio_real)
        linha_modo.addStretch(1)
        layout_modo.addLayout(linha_modo)
        nota = QLabel("Paper trading é o único modo desta implementação. A ativação de trading real "
                      "exige um fluxo separado de autorização explícita, verificações e confirmação — "
                      "não é habilitado por teste de conexão.")
        nota.setWordWrap(True)
        nota.setStyleSheet("color:#8ea3c8;")
        layout_modo.addWidget(nota)
        raiz.addWidget(grupo_modo)

        self.status_geral = QLabel("")
        self.status_geral.setWordWrap(True)
        raiz.addWidget(self.status_geral)

    # ------------------------------------------------------------- utilidades
    def _carregar_credenciais(self):
        login, senha = cfg_gui.carregar_credenciais_mt5()
        if login:
            self.campo_login.setText(login)
        if senha:
            self.campo_senha.setText(senha)

    def _procurar_terminal(self):
        caminho, _ = QFileDialog.getOpenFileName(
            self, "Executável do MetaTrader 5", self.campo_caminho.text(),
            "Executáveis (*.exe);;Todos os arquivos (*)")
        if caminho:
            self.campo_caminho.setText(caminho)

    # ------------------------------------------------------------- OpenRouter
    def _testar_openrouter(self):
        self.status_or.setText("Consulta DeepSeek desativada; nenhuma chamada de rede foi feita.")

    def _res_openrouter(self, dados):
        self.btn_testar_or.setEnabled(True)
        if dados.get("ok"):
            self.status_or.setStyleSheet("color:#22c55e;")
            self.status_or.setText(f"Conexão OK. Modelo '{dados.get('modelo')}' disponível.")
        else:
            self.status_or.setStyleSheet("color:#ef4444;")
            self.status_or.setText(f"Falha: {dados.get('erro', 'sem detalhes')}")

    def _salvar_openrouter(self):
        chave = self.campo_chave.text().strip()
        modelo = self.campo_modelo.text().strip()
        if not modelo:
            self.status_or.setStyleSheet("color:#ef4444;")
            self.status_or.setText("A configuração legada DeepSeek não é necessária para o JEv.")
            return
        if chave:
            mensagem = cfg_gui.salvar_chave_openrouter(chave)
        elif cfg_gui.fonte_chave_openrouter() == "não configurada":
            mensagem = "Modelo salvo; API key ainda não configurada."
        else:
            mensagem = f"Chave mantida ({cfg_gui.fonte_chave_openrouter()})."
        configuracao.salvar_variaveis({"OPENROUTER_DEEPSEEK_MODEL": modelo})
        self.campo_chave.clear()
        self.campo_chave.setPlaceholderText(f"•••••••• — {cfg_gui.fonte_chave_openrouter()}")
        self.status_or.setStyleSheet("color:#22c55e;" if "cofre" in mensagem or "mantida" in mensagem else "color:#f59e0b;")
        self.status_or.setText(f"{mensagem} Modelo salvo.")
        self.config_alterada()

    # ------------------------------------------------------------- MetaTrader 5
    def _testar_mt5(self):
        caminho = self.campo_caminho.text().strip()
        if not caminho:
            self.status_mt5.setStyleSheet("color:#ef4444;")
            self.status_mt5.setText("Informe o caminho do executável.")
            return
        self.btn_testar_mt5.setEnabled(False)
        self.status_mt5.setText("Testando terminal MT5 em processo isolado...")
        threading.Thread(target=lambda: self.testes.resultado_mt5.emit(
            cfg_gui.testar_mt5(caminho)), daemon=True).start()

    def receber_catalogo(self, dados):
        """Repassa o catálogo descoberto ao painel de mercados."""
        self.painel_mercados.preencher_catalogo(dados)

    def _res_mt5(self, dados):
        self.btn_testar_mt5.setEnabled(True)
        if dados.get("ok"):
            self.status_mt5.setStyleSheet("color:#22c55e;")
            self.status_mt5.setText(f"Terminal conectado; conta aberta ({dados.get('moeda', '')}). "
                                    "Ordens reais permanecem desabilitadas.")
        else:
            self.status_mt5.setStyleSheet("color:#ef4444;")
            detalhe = dados.get("erro") or "Terminal sem conexão ou sem conta aberta."
            if not dados.get("inicializado", True):
                detalhe = f"Executável não encontrado ou incompatível: {detalhe}"
            self.status_mt5.setText(f"Falha: {detalhe}")

    def _salvar_mt5(self):
        configuracao.salvar_variaveis({"MT5_TERMINAL_PATH": self.campo_caminho.text().strip() or None,
                                       "MT5_SERVER": self.campo_servidor.text().strip() or None})
        login = self.campo_login.text().strip()
        senha = self.campo_senha.text()
        if login or senha:
            mensagem = cfg_gui.salvar_credenciais_mt5(login, senha)
        else:
            mensagem = "Credenciais MT5 vazias mantidas."
        self.campo_senha.clear()
        self.status_mt5.setStyleSheet("color:#22c55e;" if "cofre" in mensagem else "color:#f59e0b;")
        self.status_mt5.setText(f"{mensagem} Caminho do terminal salvo.")
        self.config_alterada()

    def confirmar_saida(self):
        """Aviso ao fechar com campos não salvos é opcional; nada sensível em log."""
        return True
