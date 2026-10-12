"""Aba 1 — Operação e fluxo: controles, limites, três listas e histórico."""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (QDoubleSpinBox, QGroupBox, QHBoxLayout,
                               QLabel, QMessageBox, QPushButton, QSplitter,
                               QTableWidget, QTableWidgetItem, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout, QWidget)

VERDE, VERMELHO, AMBAR, CINZA = "#22c55e", "#ef4444", "#f59e0b", "#8ea3c8"
MAX_LINHAS = 200  # limite de linhas visíveis: inserção em lote e poda única

# Estados formais do orquestrador → (texto do chip, cor). "PARADO POR
# RISCO" permanece como rótulo visível do estado BLOQUEADO_POR_RISCO.
ESTADOS_CHIP = {
    "CONECTANDO": ("CONECTANDO", CINZA),
    "INICIALIZANDO": ("INICIALIZANDO", AMBAR),
    "PRONTO": ("PRONTO", VERDE),
    "AGUARDANDO": ("AGUARDANDO", AMBAR),
    "AGUARDANDO_SELECAO": ("AGUARDANDO SELEÇÃO", AMBAR),
    "ANALISANDO": ("ANALISANDO", VERDE),
    "PAUSADO": ("PAUSADO", AMBAR),
    "PAUSADO_PELO_USUARIO": ("PAUSADO", AMBAR),
    "BLOQUEADO_POR_RISCO": ("PARADO POR RISCO", VERMELHO),
    "PARADO POR RISCO": ("PARADO POR RISCO", VERMELHO),
    "EMERGENCIA": ("EMERGÊNCIA", VERMELHO),
    "ERRO": ("ERRO", VERMELHO),
    "ENCERRANDO": ("ENCERRANDO", CINZA),
    "ENCERRADO": ("ENCERRADO", CINZA),
}


def _fmt_moeda(valor, moeda=""):
    return f"{valor:,.2f} {moeda}".replace(",", "X").replace(".", ",").replace("X", ".")


def _fmt_horario(iso):
    try:
        return datetime.fromisoformat(iso).strftime("%d/%m %H:%M:%S")
    except (TypeError, ValueError):
        return "-"


def _fmt_duracao(trade):
    """Duração entre abertura e fechamento, ou 'aberta'."""
    try:
        abertura = datetime.fromisoformat(trade.get("opened_at"))
        fechamento = trade.get("closed_at")
        if not fechamento:
            return "aberta"
        segundos = (datetime.fromisoformat(fechamento) - abertura).total_seconds()
    except (TypeError, ValueError):
        return "-"
    minutos, segundos = divmod(max(0, int(segundos)), 60)
    horas, minutos = divmod(minutos, 60)
    return f"{horas}h{minutos:02d}m{segundos:02d}s" if horas else f"{minutos}m{segundos:02d}s"


class PainelAnalise(QWidget):
    """Painel identificado: título, contagem de itens e último processamento."""

    def __init__(self, titulo, lista, parent=None):
        super().__init__(parent)
        colunas = QVBoxLayout(self)
        colunas.setContentsMargins(0, 2, 0, 2)
        colunas.setSpacing(2)
        cabecalho = QHBoxLayout()
        rotulo = QLabel(titulo)
        rotulo.setStyleSheet("color:#7aa2f7; font-weight:700;")
        cabecalho.addWidget(rotulo)
        cabecalho.addStretch(1)
        self.contagem = QLabel("0 itens")
        self.contagem.setStyleSheet("color:#8ea3c8;")
        cabecalho.addWidget(self.contagem)
        self.ultimo = QLabel("")
        self.ultimo.setStyleSheet("color:#8ea3c8;")
        cabecalho.addWidget(self.ultimo)
        colunas.addLayout(cabecalho)
        colunas.addWidget(lista)
        lista.setMinimumHeight(70)

    def atualizar(self, quantidade, ultimo_texto=""):
        self.contagem.setText(f"{quantidade} itens")
        if ultimo_texto:
            self.ultimo.setText(f"último: {ultimo_texto}")


class ListaAnalise(QTreeWidget):
    """Lista com colunas fixas, mais recente no topo e teto de linhas."""

    def __init__(self, titulos, parent=None):
        super().__init__(parent)
        self.setColumnCount(len(titulos))
        self.setHeaderLabels(titulos)
        self.setRootIsDecorated(False)
        self.setAlternatingRowColors(True)
        self.setUniformRowHeights(True)
        self.setSortingEnabled(False)
        self.setTextElideMode(Qt.ElideRight)
        self.setWordWrap(False)
        for indice in range(len(titulos)):
            self.resizeColumnToContents(indice)
        self.header().resizeSection(0, 130)
        self.header().resizeSection(1, 90)
        self.header().resizeSection(2, 110)

    def adicionar(self, valores, cor=None):
        """Insere no topo com teto de linhas; timestamps visíveis."""
        item = QTreeWidgetItem([str(v) if v is not None else "-" for v in valores])
        if cor:
            item.setForeground(1, QBrush(QColor(cor)))
        self.insertTopLevelItem(0, item)
        while self.topLevelItemCount() > MAX_LINHAS:
            self.takeTopLevelItem(self.topLevelItemCount() - 1)

    def adicionar_lote(self, itens):
        """Insere um lote em uma única operação, mantendo a ordem recente.

        `itens` já chega do mais novo ao mais antigo. Uma chamada de inserção
        por flush evita milhares de eventos de redesenho por ciclo.
        """
        if not itens:
            return
        novos = []
        for valores, cor in itens:
            item = QTreeWidgetItem([str(v) if v is not None else "-" for v in valores])
            if cor:
                item.setForeground(1, QBrush(QColor(cor)))
            novos.append(item)
        self.insertTopLevelItems(0, list(reversed(novos)))
        while self.topLevelItemCount() > MAX_LINHAS:
            self.takeTopLevelItem(self.topLevelItemCount() - 1)


class AbaOperacao(QWidget):
    """Controles do robô, limites editáveis, pipeline em três listas e histórico."""

    def __init__(self, acoes, parent=None):
        super().__init__(parent)
        self.acoes = acoes
        raiz = QVBoxLayout(self)
        raiz.setContentsMargins(10, 8, 10, 8)
        raiz.setSpacing(6)

        # --- controles principais
        grupo_controles = QGroupBox("Controles")
        linha = QHBoxLayout(grupo_controles)
        self.btn_iniciar = QPushButton("INICIAR")
        self.btn_iniciar.setObjectName("btnIniciar")
        self.btn_iniciar.clicked.connect(self._clicar_iniciar)
        self.btn_pausar = QPushButton("PAUSAR")
        self.btn_pausar.setObjectName("btnPausar")
        self.btn_pausar.clicked.connect(self._clicar_pausar)
        self.btn_liberar = QPushButton("LIBERAR APÓS REVISÃO")
        self.btn_liberar.setToolTip("Disponível quando o robô está PARADO POR RISCO")
        self.btn_liberar.clicked.connect(self._clicar_liberar)
        self.btn_cancelar_ciclo = QPushButton("CANCELAR CICLO")
        self.btn_cancelar_ciclo.setToolTip("Cancela com segurança o ciclo/download em andamento; "
                                           "o monitoramento de posições continua")
        self.btn_cancelar_ciclo.clicked.connect(self._clicar_cancelar_ciclo)
        self.btn_emergencia = QPushButton("EMERGÊNCIA — FECHAR TUDO")
        self.btn_emergencia.setObjectName("btnEmergencia")
        self.btn_emergencia.clicked.connect(self._clicar_emergencia)
        for botao in (self.btn_iniciar, self.btn_pausar, self.btn_liberar,
                      self.btn_cancelar_ciclo, self.btn_emergencia):
            linha.addWidget(botao)
        linha.addStretch(1)
        raiz.addWidget(grupo_controles)

        # --- limites editáveis
        grupo_limites = QGroupBox("Limites de risco (% do patrimônio paper)")
        linha_limites = QHBoxLayout(grupo_limites)
        self.lim_trades = self._spin(linha_limites, "Risco por trade")
        self.lim_total = self._spin(linha_limites, "Exposição máxima")
        self.lim_dd = self._spin(linha_limites, "Drawdown máximo")
        self.btn_aplicar = QPushButton("APLICAR")
        self.btn_aplicar.setObjectName("btnAplicar")
        self.btn_aplicar.clicked.connect(self._aplicar_limites)
        linha_limites.addWidget(self.btn_aplicar)
        linha_limites.addStretch(1)
        raiz.addWidget(grupo_limites)

        # --- métricas do momento
        grupo_metricas = QGroupBox("Estado do robô e carteira paper")
        grade = QHBoxLayout(grupo_metricas)
        self.chip_estado = QLabel("CONECTANDO")
        self.chip_estado.setObjectName("chipEstado")
        self.chip_estado.setAlignment(Qt.AlignCenter)
        grade.addWidget(self.chip_estado)
        self.valores = {}
        for chave, titulo in (("equity", "Patrimônio"), ("saldo", "Saldo"), ("aberto", "P/L aberto"),
                              ("exposicao_txt", "Exposição"), ("drawdown_txt", "Drawdown"),
                              ("posicoes", "Posições")):
            caixa = QVBoxLayout()
            rotulo = QLabel(titulo)
            rotulo.setObjectName("rotulo")
            rotulo.setStyleSheet("color:#8ea3c8; font-size:9pt;")
            rotulo.setAlignment(Qt.AlignCenter)
            valor = QLabel("-")
            valor.setObjectName("valor")
            valor.setAlignment(Qt.AlignCenter)
            caixa.addWidget(rotulo)
            caixa.addWidget(valor)
            grade.addLayout(caixa)
            self.valores[chave] = valor
        grade.addStretch(1)
        self.resumo_ciclo = QLabel("")
        self.resumo_ciclo.setWordWrap(True)
        self.resumo_ciclo.setStyleSheet("color:#7aa2f7;")
        self.progresso_download = QLabel("")
        self.progresso_download.setStyleSheet("color:#8ea3c8;")
        self.mensagem = QLabel("")
        self.mensagem.setStyleSheet("color:#8ea3c8;")
        self.mensagem.setWordWrap(True)
        raiz.addWidget(grupo_metricas)
        raiz.addWidget(self.resumo_ciclo)
        raiz.addWidget(self.progresso_download)
        raiz.addWidget(self.mensagem)

        # --- conta real MT5 (somente leitura)
        self.conta_real = QLabel("CONTA REAL MT5: aguardando métricas")
        self.conta_real.setStyleSheet("color:#8ea3c8;")
        raiz.addWidget(self.conta_real)

        # --- três listas + histórico no splitter vertical
        divisor = QSplitter(Qt.Vertical)
        divisor.setChildrenCollapsible(False)
        self.lista_calc = ListaAnalise(["ID", "Ativo", "Horário (UTC)", "Etapa",
                                        "Amostra", "Padrões", "Resultado", "Erro"])
        self.lista_deepseek = ListaAnalise(["ID", "Ativo", "Horário (UTC)", "Padrão",
                                            "Consulta", "Hipótese", "Validação local", "Resultado"])
        self.lista_decisao = ListaAnalise(["analysis_id", "Ativo", "Horário (UTC)",
                                           "pattern_id", "Direção", "Decisão",
                                           "Justificativa", "Risco / Paper"])
        self.painel_calc = PainelAnalise("CÁLCULOS MATEMÁTICOS (Python)", self.lista_calc)
        self.painel_deepseek = PainelAnalise("ANÁLISE DEEPSEEK (padrões candidatos)",
                                             self.lista_deepseek)
        self.painel_decisao = PainelAnalise("DECISÃO JEv (SIM/NÃO e motivo)",
                                            self.lista_decisao)
        self.paineis = {"calc": self.painel_calc, "deepseek": self.painel_deepseek,
                        "decisao": self.painel_decisao}
        for indice, painel in enumerate((self.painel_calc, self.painel_deepseek,
                                         self.painel_decisao)):
            divisor.addWidget(painel)
            divisor.setStretchFactor(indice, 1)
        divisor.setHandleWidth(4)

        grupo_historico = QGroupBox("Histórico de trades (persistido em db.json)")
        layout_historico = QVBoxLayout(grupo_historico)
        self.tabela = QTableWidget(0, 12)
        self.tabela.setHorizontalHeaderLabels(
            ["Horário", "Ativo", "Lado", "Volume", "Entrada", "Saída/Atual", "P/L",
             "P/L % do risco", "Duração", "Modo", "Motivo", "Estado"])
        self.tabela.setAlternatingRowColors(True)
        self.tabela.setSortingEnabled(True)
        self.tabela.verticalHeader().setVisible(False)
        self.tabela.horizontalHeader().setStretchLastSection(True)
        layout_historico.addWidget(self.tabela)
        divisor.addWidget(grupo_historico)
        divisor.setStretchFactor(3, 2)
        raiz.addWidget(divisor, 1)

    # ------------------------------------------------------------- construção
    def _spin(self, layout, titulo):
        caixa = QVBoxLayout()
        rotulo = QLabel(titulo)
        rotulo.setStyleSheet("color:#8ea3c8; font-size:9pt;")
        spin = QDoubleSpinBox()
        spin.setRange(0.01, 100.0)
        spin.setDecimals(2)
        spin.setSuffix(" %")
        spin.setSingleStep(0.25)
        caixa.addWidget(rotulo)
        caixa.addWidget(spin)
        layout.addLayout(caixa)
        return spin

    # ------------------------------------------------------------- botões
    def _clicar_iniciar(self):
        self.acoes["iniciar"]()

    def _clicar_pausar(self):
        if self.btn_pausar.text().startswith("PAUSAR"):
            self.acoes["pausar"]()
        else:
            self.acoes["retomar"]()

    def _clicar_cancelar_ciclo(self):
        self.acoes["cancelar_ciclo"]()
        self.progresso_download.setText("Cancelamento solicitado; aguardando o lote atual terminar…")

    def _clicar_liberar(self):
        resposta = QMessageBox.warning(
            self, "Liberação manual de risco",
            "Confirmar liberação manual dos bloqueios de perda diária e drawdown?\n\n"
            "Esta ação fica registrada no log e o motor de risco continua ativo "
            "para novos bloqueios. Revise as causas antes de liberar.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if resposta == QMessageBox.Yes:
            self.acoes["liberar_risco"]()

    def _clicar_emergencia(self):
        resposta = QMessageBox.critical(
            self, "EMERGÊNCIA — FECHAR TUDO",
            "Isto bloqueia novas entradas imediatamente e tenta fechar TODAS as posições paper "
            "ao preço observado agora.\n\n"
            "Falhas de cotação serão reportadas e as posições permanecerão abertas até "
            "serem confirmadas. O robô permanece bloqueado até INICIAR.\n\nConfirmar?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if resposta == QMessageBox.Yes:
            self.acoes["emergencia"]()

    def _aplicar_limites(self):
        self.acoes["aplicar_limites"](self.lim_trades.value() / 100,
                                      self.lim_total.value() / 100,
                                      self.lim_dd.value() / 100)

    # ------------------------------------------------------------- atualizações
    def receber_metricas(self, m):
        """Atualiza estado, métricas e limites com o snapshot do motor."""
        self._aplicar_estado(m.get("estado", "-"), m.get("detalhe", ""))
        self.btn_pausar.setText("RETOMAR" if m.get("pausado") else "PAUSAR")
        self.btn_liberar.setEnabled(bool(m.get("parado_risco")))
        moeda = m.get("moeda", "")
        self._definir("equity", _fmt_moeda(m.get("equity", 0), moeda))
        self._definir("saldo", _fmt_moeda(m.get("saldo", 0), moeda))
        self._cor_valor("aberto", m.get("aberto", 0), moeda)
        exposicao = m.get("exposicao", 0)
        self._definir("exposicao_txt", f"{_fmt_moeda(exposicao, moeda)} "
                                       f"({m.get('exposicao_fracao', 0):.1%})")
        self._definir("drawdown_txt", f"atual {m.get('drawdown', 0):.2%} | máx {m.get('drawdown_max', 0):.2%}")
        self._definir("posicoes", str(m.get("posicoes", 0)))
        limites = m.get("limites", {})
        if limites and not self.lim_trades.hasFocus() and not self.lim_total.hasFocus() and not self.lim_dd.hasFocus():
            self.lim_trades.setValue(limites.get("risk_per_trade", 0.02) * 100)
            self.lim_total.setValue(limites.get("max_total_risk", 0.15) * 100)
            self.lim_dd.setValue(limites.get("max_drawdown", 0.10) * 100)
        conta = m.get("conta")
        if conta:
            self.conta_real.setText(
                f"CONTA REAL MT5 ({conta.get('currency', '')}): saldo {conta.get('balance', 0):,.2f} | "
                f"equity {conta.get('equity', 0):,.2f} | P&L {conta.get('profit', 0):+,.2f}")
        if m.get("parado_risco"):
            self.mensagem.setText(f"PARADO POR RISCO: {m.get('motivo_risco', '')} "
                                  "Análises e entradas suspensas. Libere manualmente após revisão.")

    def _definir(self, chave, texto):
        if chave in self.valores:
            self.valores[chave].setText(texto)
            self.valores[chave].setObjectName("valor")
            self.valores[chave].setStyleSheet("")

    def _cor_valor(self, chave, valor, moeda=""):
        if chave not in self.valores:
            return
        rotulo = self.valores[chave]
        rotulo.setText(_fmt_moeda(valor, moeda))
        cor = VERDE if valor > 0 else VERMELHO if valor < 0 else "#e6edf7"
        rotulo.setStyleSheet(f"color:{cor}; font-weight:600; font-size:11pt;")

    def receber_estado(self, registro):
        """Aplica a transição de estado imediata ao chip e à mensagem."""
        self._aplicar_estado(registro.get("estado", ""), registro.get("detalhe", ""))
        if registro.get("estado") == "ANALISANDO":
            self.progresso_download.setText("")

    def _aplicar_estado(self, estado, detalhe=""):
        """Chip de estado com texto/cor do mapa; razão objetiva na mensagem."""
        texto, cor = ESTADOS_CHIP.get(estado, (estado or "-", CINZA))
        self.chip_estado.setText(texto)
        self.chip_estado.setStyleSheet(f"background:{cor};")
        self.chip_estado.setToolTip(detalhe)
        if detalhe:
            self.mensagem.setText(detalhe)

    def receber_lote(self, canal, registros):
        """Recebe um lote coalescido e insere cada canal em uma única operação."""
        if canal == "resumo" and isinstance(registros, dict):
            registros = [registros]
        itens = []
        for r in registros:
            if canal == "calc":
                amostra = r.get("amostra")
                texto_amostra = (f"{amostra.get('candles', '-')}/"
                                 f"{amostra.get('solicitados', '?')} candles"
                                 if isinstance(amostra, dict) and r.get("etapa") == "janela"
                                 else f"{amostra['candles']} candles / {amostra['dias']}d"
                                 if isinstance(amostra, dict) else "-")
                if r.get("etapa") == "janela":
                    self.progresso_download.setText(
                        f"Download {r.get('simbolo', '')}: {texto_amostra}")
                elif r.get("etapa") in ("calculado", "erro", "dados"):
                    self.progresso_download.setText("")
                itens.append((
                    [r.get("id"), r.get("simbolo") or "-", _fmt_horario(r.get("instante")),
                     str(r.get("etapa") or "-"), texto_amostra,
                     "-" if r.get("padroes_testados") is None else r.get("padroes_testados"),
                     r.get("resultado") or r.get("mensagem") or "-",
                     r.get("erro") or ""],
                    VERMELHO if r.get("etapa") == "erro" else None))
            elif canal == "deepseek":
                itens.append((
                    [r.get("id"), r.get("simbolo") or "-", _fmt_horario(r.get("instante")),
                     r.get("padrao") or "-", r.get("status_consulta") or "-",
                     r.get("hipotese") or "-", r.get("validacao") or "-", r.get("resultado") or "-"],
                    VERDE if "validado" in str(r.get("validacao", "")).lower()
                    else VERMELHO if "rejeitado" in str(r.get("validacao", "")).lower() else None))
            elif canal == "decisao":
                decisao = str(r.get("decisao") or "-")
                cor = (VERDE if decisao == "SIM" else VERMELHO if decisao in ("NAO", "NÃO")
                       else AMBAR if decisao == "HOLD" else CINZA)
                itens.append((
                    [r.get("id"), r.get("simbolo") or "-", _fmt_horario(r.get("instante")),
                     r.get("padrao") or "-", r.get("direcao") or "-", decisao,
                     r.get("justificativa") or "-",
                     f"{r.get('risco') or '-'} | {r.get('execucao') or '-'}"], cor))
            elif canal == "resumo":
                oportunidades = r.get("oportunidades", 0)
                funil = " → ".join(f"{etapa.split('_')[0]}:{qtd}" for etapa, qtd
                                   in (r.get("funil") or {}).items())
                tempos = r.get("tempos") or {}
                nomes = {"decisao": "decisão", "deepseek": "DeepSeek",
                         "oportunidade": "oportunidade", "trade": "trade"}
                desde = " | ".join(f"{nomes[chave]} há {valor}s"
                                   for chave, valor in tempos.items() if valor is not None)
                texto = (f"Ciclo {r.get('ciclo', '?')}: {oportunidades} oportunidade(s) "
                         f"elegível(is) | funil: {funil or 'vazio'} | "
                         f"pendentes {r.get('pendentes', 0)} | duração {r.get('duracao_s', '?')}s")
                if desde:
                    texto += f" | {desde}"
                if oportunidades == 0:
                    texto += "  ·  0 oportunidades elegíveis"
                self.resumo_ciclo.setText(texto)
                self.resumo_ciclo.setToolTip(r.get("motivos", ""))
                self.paineis["decisao"].atualizar(self.lista_decisao.topLevelItemCount(),
                                                  _fmt_horario(r.get("instante")))
            elif canal == "log":
                self.mensagem.setText(f"{_fmt_horario(r.get('instante'))} {r.get('mensagem', '')}")
                cor = {"erro": VERMELHO, "aviso": AMBAR, "emergencia": VERMELHO,
                       "info": CINZA}.get(r.get("nivel"), CINZA)
                itens.append((["log", "", _fmt_horario(r.get("instante")),
                               r.get("nivel", ""), "", "", r.get("mensagem", "")], cor))
        lista = {"calc": self.lista_calc, "deepseek": self.lista_deepseek,
                 "decisao": self.lista_decisao}.get(canal)
        if lista is not None and itens:
            lista.adicionar_lote(itens)
            painel = self.paineis.get(canal)
            if painel is not None:
                painel.atualizar(lista.topLevelItemCount(), itens[0][0][2])

    def receber_evento(self, canal, r):
        """Compatibilidade: um registro avulso entra como lote de um item."""
        self.receber_lote(canal, [r])

    def receber_trades(self, trades):
        """Repõe a tabela de histórico com os trades persistidos."""
        self.tabela.setSortingEnabled(False)
        self.tabela.setRowCount(0)
        for trade in trades:
            linha = self.tabela.rowCount()
            self.tabela.insertRow(linha)
            aberto = trade.get("status") == "OPEN"
            saida = trade.get("exit") if not aberto else trade.get("price_now")
            lucro = trade.get("profit", 0)
            risco = trade.get("risk_amount") or 0
            percentual = (lucro / risco * 100) if risco > 0 else 0
            try:
                horario = datetime.fromisoformat(trade.get("opened_at")).strftime("%Y-%m-%d %H:%M:%S")
            except (TypeError, ValueError):
                horario = "-"
            valores = [horario, trade.get("symbol", ""),
                       trade.get("action", ""), f"{trade.get('volume', 0):g}",
                       f"{trade.get('entry', 0):.8g}", f"{saida:.8g}" if saida else "-",
                       f"{lucro:+,.2f}", f"{percentual:+.1f}%", _fmt_duracao(trade),
                       trade.get("mode", "paper"),
                       trade.get("reason", "aberta") if not aberto else "-",
                       trade.get("status", "")]
            for coluna, texto in enumerate(valores):
                item = QTableWidgetItem(str(texto))
                if coluna == 6 and lucro:
                    item.setForeground(QBrush(QColor(VERDE if lucro > 0 else VERMELHO)))
                if coluna == 11:
                    item.setForeground(QBrush(QColor(VERDE if aberto else CINZA)))
                self.tabela.setItem(linha, coluna, item)
        self.tabela.setSortingEnabled(True)
