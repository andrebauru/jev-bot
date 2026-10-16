"""Aba 1 — Operação e fluxo: controles, limites, três listas e histórico."""

from __future__ import annotations

import math
from datetime import datetime, timezone

from PySide6.QtCore import Qt, QSettings
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (QDoubleSpinBox, QGroupBox, QHBoxLayout,
                               QLabel, QMessageBox, QPushButton, QSizePolicy, QSplitter,
                               QTabWidget, QTableWidget, QTableWidgetItem, QTreeWidget,
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
    if not isinstance(valor, (int, float)) or isinstance(valor, bool) \
            or not math.isfinite(valor):
        return f"N/D {moeda}".strip()
    return f"{valor:,.2f} {moeda}".replace(",", "X").replace(".", ",").replace("X", ".")


def _num(valor, formato="g"):
    """Formato numérico seguro: `None`/não numérico vira N/D (dado ausente)."""
    if not isinstance(valor, (int, float)) or isinstance(valor, bool) \
            or not math.isfinite(valor):
        return "N/D"
    try:
        return f"{valor:{formato}}"
    except (ValueError, TypeError):
        return "N/D"


def _fmt_horario(iso):
    try:
        if isinstance(iso, (int, float)) and not isinstance(iso, bool):
            return datetime.fromtimestamp(float(iso), tz=timezone.utc).strftime("%d/%m %H:%M:%S")
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
        for coluna, valor in enumerate(valores):
            item.setToolTip(coluna, str(valor) if valor is not None else "-")
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
            for coluna, valor in enumerate(valores):
                item.setToolTip(coluna, str(valor) if valor is not None else "-")
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
        self.diagnostico_funil = QLabel("Funil: selecionados 0 → dados válidos 0 → candidatos 0 → "
                                        "validados 0 → enfileirados 0 → JEv 0 → respostas 0 → "
                                        "decisões 0 → paper 0")
        self.diagnostico_funil.setWordWrap(True)
        self.diagnostico_funil.setStyleSheet("color:#a9b8d0; font-size:9pt;")
        self.grupo_atividade = QGroupBox("Atividade matemática em paralelo (até 4 ativos)")
        layout_atividade = QVBoxLayout(self.grupo_atividade)
        self.tabela_atividade = QTableWidget(0, 6)
        self.tabela_atividade.setHorizontalHeaderLabels(
            ["Ativo", "Estado", "Candle", "Candles", "BUY/SELL Edge", "Motivo"])
        self.tabela_atividade.setAlternatingRowColors(True)
        self.tabela_atividade.verticalHeader().setVisible(False)
        self.tabela_atividade.setMinimumHeight(110)
        self.tabela_atividade.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        layout_atividade.addWidget(self.tabela_atividade)
        raiz.addWidget(self.grupo_atividade, 1)
        self.progresso_download = QLabel("")
        self.progresso_download.setStyleSheet("color:#8ea3c8;")
        self.mensagem = QLabel("")
        self.mensagem.setStyleSheet("color:#8ea3c8;")
        self.mensagem.setWordWrap(True)
        raiz.addWidget(grupo_metricas)
        raiz.addWidget(self.resumo_ciclo)
        raiz.addWidget(self.diagnostico_funil)
        raiz.addWidget(self.progresso_download)
        raiz.addWidget(self.mensagem)

        # --- conta real MT5 (somente leitura)
        self.conta_real = QLabel("CONTA REAL MT5: aguardando métricas")
        self.conta_real.setStyleSheet("color:#8ea3c8;")
        raiz.addWidget(self.conta_real)

        # --- duas colunas redimensionáveis: cálculos Python | análises/decisões JEv
        divisor = QSplitter(Qt.Vertical)
        divisor.setChildrenCollapsible(False)
        self.lista_calc = ListaAnalise(["Ativo", "Candle", "Status", "BUY/SELL",
                                        "Custo", "Resultado"])
        self.lista_oportunidade = ListaAnalise(["Ativo", "Candle", "Estado matemático",
                                                "Motivo", "ID ciclo"])
        self.lista_decisao = ListaAnalise(["Ativo", "Enviado em", "Aprovação JEv", "Estado API",
                                           "Direção", "Motivo", "Latência"])
        self.painel_calc = PainelAnalise("CÁLCULOS PYTHON (por símbolo)", self.lista_calc)
        self.painel_oportunidade = PainelAnalise("OPORTUNIDADES — PYTHON QUANTITATIVO",
                                                 self.lista_oportunidade)
        self.painel_decisao = PainelAnalise("ANÁLISES ENVIADAS / DECISÕES JEv",
                                            self.lista_decisao)
        self.paineis = {"calc": self.painel_calc, "oportunidade": self.painel_oportunidade,
                        "decisao": self.painel_decisao}
        coluna_esquerda = QSplitter(Qt.Vertical)
        coluna_esquerda.setChildrenCollapsible(False)
        coluna_esquerda.addWidget(self.painel_calc)
        coluna_esquerda.addWidget(self.painel_oportunidade)
        coluna_esquerda.setStretchFactor(0, 3)
        coluna_esquerda.setStretchFactor(1, 2)
        self.coluna_esquerda = coluna_esquerda
        self.divisor_colunas = QSplitter(Qt.Horizontal)
        self.divisor_colunas.setChildrenCollapsible(False)
        self.divisor_colunas.addWidget(coluna_esquerda)
        self.divisor_colunas.addWidget(self.painel_decisao)
        self.divisor_colunas.setStretchFactor(0, 1)
        self.divisor_colunas.setStretchFactor(1, 1)
        self.divisor_colunas.setSizes([680, 680])
        divisor.addWidget(self.divisor_colunas)
        divisor.setStretchFactor(0, 3)
        divisor.setHandleWidth(5)

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
        divisor.setStretchFactor(1, 2)
        divisor.setSizes([570, 220])
        self.divisor_principal = divisor
        self._settings = QSettings("JEvBot", "JEvBot")
        estado_divisor = self._settings.value("operacao/splitter_principal")
        if estado_divisor:
            divisor.restoreState(estado_divisor)
        estado_colunas = self._settings.value("operacao/splitter_colunas")
        if estado_colunas:
            self.divisor_colunas.restoreState(estado_colunas)
        estado_esquerda = self._settings.value("operacao/splitter_coluna_esquerda")
        if estado_esquerda:
            self.coluna_esquerda.restoreState(estado_esquerda)
        raiz.addWidget(divisor, 1)

    def salvar_layout(self):
        """Persiste as proporções escolhidas pelo usuário nos splitters."""
        self._settings.setValue("operacao/splitter_principal", self.divisor_principal.saveState())
        self._settings.setValue("operacao/splitter_colunas", self.divisor_colunas.saveState())
        self._settings.setValue("operacao/splitter_coluna_esquerda",
                                self.coluna_esquerda.saveState())

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
        diag = m.get("diagnostico_funil") or {}
        totais = diag.get("totais") or {}
        decisoes = sum(totais.get(k, 0) for k in
                       ("decision_buy", "decision_sell", "decision_hold", "decision_no"))
        motivos = diag.get("motivos_rejeicao_top3") or []
        top = ", ".join(f"{r.get('codigo')}:{r.get('quantidade', 0)}" for r in motivos) or "nenhum"
        codigos = diag.get("motivos_codigo") or {}
        categorias = (f"SEM_SETUP {codigos.get('SEM_SETUP', 0)} · "
                      f"SEM_DADOS {codigos.get('SEM_DADOS', 0)} · "
                      f"SEM_ELEGIBILIDADE {codigos.get('SEM_ELEGIBILIDADE', 0)} · "
                      f"FILA_BLOQUEADA {codigos.get('FILA_BLOQUEADA', 0)} · "
                      f"IA_INDISPONIVEL {codigos.get('IA_INDISPONIVEL', 0)} · "
                      f"SEM_DECISAO {codigos.get('SEM_DECISAO', 0)} · "
                      f"VETO_RISCO {codigos.get('VETO_RISCO', 0)}")
        envio = diag.get("ultimo_envio_jev_age_s")
        resposta = diag.get("ultima_resposta_jev_age_s")
        envio_txt = f"há {envio:g}s" if envio is not None else "nunca"
        resposta_txt = f"há {resposta:g}s" if resposta is not None else "nunca"
        compacto = (f"Funil: sel {totais.get('symbol_selected', 0)} · "
                    f"dados {totais.get('symbol_available_mt5', 0)} · "
                    f"candidatos {totais.get('candidate_generated', 0)} · "
                    f"env {totais.get('queue_enqueued', 0)} · "
                    f"JEv {totais.get('jev_request_started', 0)} · "
                    f"resp {totais.get('jev_response_received', 0)} · "
                    f"decisões {decisoes} · paper {totais.get('paper_opened', 0)} · "
                    f"calc {diag.get('analises_pendentes', 0)}/{diag.get('max_parallel_symbols', 0)} · "
                    f"pico {diag.get('pool_matematico', {}).get('pico_concorrencia', 0)}")
        detalhe = (
            "Funil (sessão): "
            f"selecionados {totais.get('symbol_selected', 0)} → "
            f"operáveis {totais.get('symbol_operable', 0)} → "
            f"dados válidos {totais.get('symbol_available_mt5', 0)} → "
            f"candidatos {totais.get('candidate_generated', 0)} → "
            f"elegíveis {totais.get('quant_validated', 0)} → "
            f"enfileirados {totais.get('queue_enqueued', 0)} → "
            f"chamadas JEv {totais.get('jev_request_started', 0)} → "
            f"respostas {totais.get('jev_response_received', 0)} → "
            f"decisões {decisoes} → paper {totais.get('paper_opened', 0)}\n"
            f"Rejeições mais comuns: {top} | códigos: {categorias} | último envio: {envio_txt} | "
            f"idade da fila: {diag.get('idade_fila_s', 0):g}s | última resposta: {resposta_txt}\n"
            f"Pool matemático: {diag.get('max_parallel_symbols', 0)} workers | "
            f"pico concorrente {diag.get('pool_matematico', {}).get('pico_concorrencia', 0)} | "
            f"em cálculo {diag.get('analises_pendentes', 0)} | "
            f"p50 {diag.get('pool_matematico', {}).get('duracao_p50_ms')} ms | "
            f"p95 {diag.get('pool_matematico', {}).get('duracao_p95_ms')} ms\n"
            "Custo da validação: spread observado; comissão/slippage não estimados")
        sessao = diag.get("sessao") or {}
        if sessao:
            detalhe += (
                "\nSessão atual: "
                f"candidatos {sessao.get('quant_candidates', 0)} · "
                f"pré-risco ok {sessao.get('pre_risk_pass', 0)} / "
                f"veto {sessao.get('pre_risk_reject', 0)} · "
                f"enviados {sessao.get('jev_enqueued', 0)} · "
                f"SIM {sessao.get('jev_yes', 0)} / "
                f"NÃO {sessao.get('jev_no', 0)} / "
                f"ERRO {sessao.get('jev_error', 0)} · "
                f"pós-risco veto {sessao.get('post_risk_reject', 0)} · "
                f"paper {sessao.get('paper_opened', 0)}")
        nao_exec = diag.get("motivos_nao_execucao_top3") or []
        if nao_exec:
            detalhe += "\nNão execução (top 3): " + ", ".join(
                f"{m.get('codigo')}={m.get('quantidade', 0)} ({m.get('exemplo_simbolo', '-')})"
                for m in nao_exec)
        self.diagnostico_funil.setText(compacto)
        self.diagnostico_funil.setToolTip(detalhe)
        self._preencher_atividade(diag.get("atividade") or [])
        exposicao = m.get("exposicao", 0)
        self._definir("exposicao_txt", f"{_fmt_moeda(exposicao, moeda)} "
                                       f"({_num(m.get('exposicao_fracao'), '.1%')})")
        self._definir("drawdown_txt", f"atual {_num(m.get('drawdown'), '.2%')} | "
                                      f"máx {_num(m.get('drawdown_max'), '.2%')}")
        self._definir("posicoes", str(m.get("posicoes", 0)))
        limites = m.get("limites", {})
        if limites and not self.lim_trades.hasFocus() and not self.lim_total.hasFocus() and not self.lim_dd.hasFocus():
            self.lim_trades.setValue(limites.get("risk_per_trade", 0.02) * 100)
            self.lim_total.setValue(limites.get("max_total_risk", 0.15) * 100)
            self.lim_dd.setValue(limites.get("max_drawdown", 0.10) * 100)
        conta = m.get("conta")
        if conta:
            self.conta_real.setText(
                f"CONTA REAL MT5 ({conta.get('currency', '')}): "
                f"saldo {_num(conta.get('balance'), ',.2f')} | "
                f"equity {_num(conta.get('equity'), ',.2f')} | "
                f"P&L {_num(conta.get('profit'), '+,.2f')}")
        if m.get("parado_risco"):
            self.mensagem.setText(f"PARADO POR RISCO: {m.get('motivo_risco', '')} "
                                  "Análises e entradas suspensas. Libere manualmente após revisão.")

    def _preencher_atividade(self, atividade):
        """Mostra até quatro ativos em cálculo simultâneo e seus estados/edges."""
        self.tabela_atividade.setSortingEnabled(False)
        self.tabela_atividade.setRowCount(0)
        for linha in atividade[:12]:
            item = self.tabela_atividade.rowCount()
            self.tabela_atividade.insertRow(item)
            motivo = str(linha.get("motivo") or "-")
            for coluna, valor in enumerate(
                    [linha.get("simbolo") or "-", linha.get("estado") or "-",
                     _fmt_horario(linha.get("candle_ref")),
                     f"BUY {linha.get('buy', 0)} / SELL {linha.get('sell', 0)}",
                     "-", motivo]):
                celula = QTableWidgetItem(str(valor))
                if motivo != "-":
                    celula.setToolTip(motivo)  # texto completo mesmo se truncado
                self.tabela_atividade.setItem(item, coluna, celula)

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
        if not isinstance(valor, (int, float)) or isinstance(valor, bool):
            cor = "#e6edf7"  # dado ausente: neutro, nunca cor de lucro/prejuízo
        else:
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
                                  else (f"treino {amostra['candles']}c/{amostra['dias']}d · "
                                        f"janela {amostra.get('candles_janela', '?')}c/"
                                        f"{amostra.get('dias_janela', '?')}d"
                                        if isinstance(amostra, dict) and "dias_janela" in amostra
                                        else f"{amostra['candles']} candles / {amostra['dias']}d")
                                 if isinstance(amostra, dict) else "-")
                if r.get("etapa") == "janela":
                    self.progresso_download.setText(
                        f"Download {r.get('simbolo', '')}: {texto_amostra}")
                elif r.get("etapa") in ("calculado", "erro", "dados"):
                    self.progresso_download.setText("")
                itens.append((
                    [r.get("simbolo") or "-", _fmt_horario(r.get("candle_ref")),
                     str(r.get("etapa") or "-"), r.get("direcao") or "-",
                     r.get("custo") or "-",
                     r.get("resultado") or r.get("mensagem") or "-"],
                    VERMELHO if r.get("etapa") == "erro" else None))
            elif canal == "oportunidade":
                itens.append(([r.get("simbolo") or "-", _fmt_horario(r.get("instante")),
                               r.get("estado") or "-", r.get("motivo") or "-", r.get("id") or "-"],
                              VERDE if r.get("motivo") in ("PAPER_EXECUTED", "DECISION_SIM")
                              else VERMELHO if r.get("motivo") in (
                                  "NO_SETUP", "SEM_SETUP", "RISK_BLOCK", "VETO_RISCO",
                                  "ERRO", "ERRO_API", "COST_UNKNOWN", "FILA_BLOQUEADA")
                              else None))
            elif canal == "decisao":
                decisao = str(r.get("decisao") or "-")
                cor = (VERDE if decisao == "SIM" else AMBAR if decisao == "EXPIRADO"
                       else VERMELHO if decisao in ("NAO", "NÃO", "ERRO") else CINZA)
                if decisao not in ("SIM", "NAO", "NÃO", "ERRO", "EXPIRADO"):
                    continue  # PENDING/estado transitório nunca é decisão final
                estado_api = r.get("estado_api") or "-"
                direcao = r.get("side_proposed") or r.get("direcao") or "-"
                itens.append((
                    [r.get("simbolo") or "-", _fmt_horario(r.get("instante")), decisao,
                     estado_api, direcao,
                     r.get("justificativa") or "-",
                     (f"{r.get('latencia_ms')} ms" if r.get("latencia_ms") is not None
                      else f"det.{r.get('latencia_detecao_ms')} ms"
                      if r.get("latencia_detecao_ms") is not None else "-")], cor))
            elif canal == "resumo":
                oportunidades = r.get("oportunidades", 0)
                funil = " → ".join(f"{etapa.split('_')[0]}:{qtd}" for etapa, qtd
                                   in (r.get("funil") or {}).items())
                tempos = r.get("tempos") or {}
                nomes = {"decisao": "decisão", "oportunidade": "oportunidade", "trade": "trade"}
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
                detalhes_ativos = []
                for simbolo, funil_ativo in sorted((r.get("ativos") or {}).items()):
                    etapas = ", ".join(f"{k}={v}" for k, v in sorted(funil_ativo.items())
                                         if isinstance(v, int) and v)
                    motivo = funil_ativo.get("ultimo_motivo")
                    detalhes_ativos.append(f"{simbolo}: {motivo or 'sem motivo'}"
                                           + (f" ({etapas})" if etapas else ""))
                tooltip = r.get("motivos", "")
                motivos_top = r.get("motivos_rejeicao_top3") or []
                if motivos_top:
                    tooltip += "\nTop rejeições: " + ", ".join(
                        f"{m.get('codigo')}={m.get('quantidade', 0)}" for m in motivos_top)
                if detalhes_ativos:
                    tooltip += "\nAtivos no ciclo:\n" + "\n".join(detalhes_ativos)
                self.resumo_ciclo.setToolTip(tooltip)
                self.paineis["decisao"].atualizar(self.lista_decisao.topLevelItemCount(),
                                                  _fmt_horario(r.get("instante")))
            elif canal == "log":
                self.mensagem.setText(f"{_fmt_horario(r.get('instante'))} {r.get('mensagem', '')}")
                cor = {"erro": VERMELHO, "aviso": AMBAR, "emergencia": VERMELHO,
                       "info": CINZA}.get(r.get("nivel"), CINZA)
                itens.append((["log", "", _fmt_horario(r.get("instante")),
                               r.get("nivel", ""), "", "", r.get("mensagem", "")], cor))
        lista = {"calc": self.lista_calc, "oportunidade": self.lista_oportunidade,
                 "decisao": self.lista_decisao}.get(canal)
        if lista is not None and itens:
            lista.adicionar_lote(itens)
            painel = self.paineis.get(canal)
            if painel is not None:
                indice_horario = 1 if canal in ("oportunidade", "decisao") else 2
                painel.atualizar(lista.topLevelItemCount(), itens[0][0][indice_horario])

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
                       trade.get("action", ""), _num(trade.get("volume")),
                       _num(trade.get("entry"), ".8g"), _num(saida, ".8g") if saida else "-",
                       _num(lucro, "+,.2f"), (_num(percentual, "+.1f") + "%"),
                       _fmt_duracao(trade),
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
