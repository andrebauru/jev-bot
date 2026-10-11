"""Aba 2 — Resumo: curva de equity, drawdown, estatísticas e filtros.

Os gráficos usam apenas dados persistidos do simulador (fechamentos) e o
patrimônio atual publicado pelo motor; realizado e não realizado ficam
sempre distinguíveis. Botão direito copia o gráfico visível como imagem.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from PySide6.QtWidgets import (QComboBox, QGridLayout, QGroupBox, QHBoxLayout,
                               QLabel, QSplitter, QVBoxLayout, QWidget)

from .graficos import GraficoView, curva_drawdown, curva_equity

PERIODOS = (("Últimas 24 horas", 1), ("Últimos 7 dias", 7),
            ("Últimos 30 dias", 30), ("Todo o histórico", None))


def _fmt_moeda(valor, moeda=""):
    return f"{valor:,.2f} {moeda}".replace(",", "X").replace(".", ",").replace("X", ".")


class AbaResumo(QWidget):
    """Painel de resultados com curvas e estatísticas da carteira paper."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.trades = []
        self.capital = None
        self.equity_atual = None
        self.moeda = ""
        self.ultimo_ponto = None

        raiz = QVBoxLayout(self)
        raiz.setContentsMargins(10, 8, 10, 8)

        filtros = QHBoxLayout()
        filtros.addWidget(QLabel("Período:"))
        self.combo_periodo = QComboBox()
        for titulo, _ in PERIODOS:
            self.combo_periodo.addItem(titulo)
        self.combo_periodo.setCurrentIndex(3)
        self.combo_periodo.currentIndexChanged.connect(self._redesenhar)
        filtros.addWidget(self.combo_periodo)
        filtros.addWidget(QLabel("Ativo:"))
        self.combo_ativo = QComboBox()
        self.combo_ativo.addItem("Todos")
        self.combo_ativo.currentIndexChanged.connect(self._redesenhar)
        filtros.addWidget(self.combo_ativo)
        filtros.addStretch(1)
        dica = QLabel("Botão direito nos gráficos: COPIAR COMO IMAGEM")
        dica.setStyleSheet("color:#8ea3c8;")
        filtros.addWidget(dica)
        raiz.addLayout(filtros)

        divisor = QSplitter()
        divisor.setChildrenCollapsible(False)
        self.grafico_equity = GraficoView("Patrimônio paper (equity)", "#22c55e")
        self.grafico_dd = GraficoView("Drawdown desde o pico (%)", "#ef4444")
        divisor.addWidget(self.grafico_equity)
        divisor.addWidget(self.grafico_dd)
        divisor.setSizes([600, 400])
        raiz.addWidget(divisor, 1)

        grupo = QGroupBox("Estatísticas da carteira")
        grade = QGridLayout(grupo)
        self.valores = {}
        posicoes = [("equity", "Patrimônio atual", 0, 0), ("saldo", "Saldo (realizado)", 0, 2),
                    ("realizado", "P/L realizado", 0, 4), ("aberto", "P/L não realizado", 0, 6),
                    ("growth", "Crescimento total", 1, 0), ("daily_return", "Resultado do dia (UTC)", 1, 2),
                    ("drawdown_txt", "Drawdown atual / máx", 1, 4),
                    ("win_rate_txt", "Taxa de acerto", 1, 6),
                    ("trades_txt", "Trades fechados (ganho/loss)", 2, 0),
                    ("exposicao_txt", "Exposição em risco", 2, 2),
                    ("margem_txt", "Margem comprometida", 2, 4), ("posicoes", "Posições abertas", 2, 6)]
        for chave, titulo, linha, coluna in posicoes:
            caixa = QVBoxLayout()
            rotulo = QLabel(titulo)
            rotulo.setStyleSheet("color:#8ea3c8; font-size:9pt;")
            valor = QLabel("-")
            valor.setStyleSheet("font-weight:600; font-size:11pt; color:#e6edf7;")
            caixa.addWidget(rotulo)
            caixa.addWidget(valor)
            grade.addLayout(caixa, linha, coluna)
            self.valores[chave] = valor
        raiz.addWidget(grupo)

    # ------------------------------------------------------------- atualização
    def receber_metricas(self, m):
        """Atualiza rótulos; redesenha séries apenas quando o equity muda."""
        moeda = m.get("moeda", "")
        self.moeda = moeda
        if m.get("saldo") is not None and m.get("realizado") is not None:
            self.capital = m["saldo"] - m["realizado"]
        self.equity_atual = m.get("equity")
        self._definir("equity", _fmt_moeda(m.get("equity", 0), moeda))
        self._definir("saldo", _fmt_moeda(m.get("saldo", 0), moeda))
        self._definir("realizado", _fmt_moeda(m.get("realizado", 0), moeda),
                      cor="#22c55e" if m.get("realizado", 0) > 0 else "#ef4444" if m.get("realizado", 0) < 0 else None)
        self._definir("aberto", _fmt_moeda(m.get("aberto", 0), moeda),
                      cor="#22c55e" if m.get("aberto", 0) > 0 else "#ef4444" if m.get("aberto", 0) < 0 else None)
        self._definir("growth", f"{m.get('growth', 0):+.2%}",
                      cor="#22c55e" if m.get("growth", 0) > 0 else "#ef4444" if m.get("growth", 0) < 0 else None)
        self._definir("daily_return", f"{m.get('daily_return', 0):+.2%}",
                      cor="#22c55e" if m.get("daily_return", 0) > 0 else "#ef4444" if m.get("daily_return", 0) < 0 else None)
        self._definir("drawdown_txt", f"{m.get('drawdown', 0):.2%} / {m.get('drawdown_max', 0):.2%}")
        taxa = m.get("win_rate")
        self._definir("win_rate_txt", f"{taxa:.1%}" if taxa is not None else "sem fechamentos")
        self._definir("trades_txt", f"{m.get('fechados', 0)} ({m.get('wins', 0)}G/{m.get('losses', 0)}L)")
        self._definir("exposicao_txt", _fmt_moeda(m.get("exposicao", 0), moeda))
        self._definir("margem_txt", _fmt_moeda(m.get("margem", 0), moeda))
        self._definir("posicoes", str(m.get("posicoes", 0)))
        ponto = (self.equity_atual, len(self.trades))
        if ponto != self.ultimo_ponto:
            self.ultimo_ponto = ponto
            self._redesenhar()

    def _definir(self, chave, texto, cor=None):
        rotulo = self.valores.get(chave)
        if rotulo is None:
            return
        rotulo.setText(texto)
        rotulo.setStyleSheet(f"font-weight:600; font-size:11pt; color:{cor or '#e6edf7'};")

    def receber_trades(self, trades):
        """Recebe o histórico persistido e repõe o filtro de ativos."""
        self.trades = trades
        atual = self.combo_ativo.currentText()
        self.combo_ativo.blockSignals(True)
        self.combo_ativo.clear()
        self.combo_ativo.addItem("Todos")
        for simbolo in sorted({t.get("symbol", "") for t in trades if t.get("symbol")}):
            self.combo_ativo.addItem(simbolo)
        indice = self.combo_ativo.findText(atual)
        if indice >= 0:
            self.combo_ativo.setCurrentIndex(indice)
        self.combo_ativo.blockSignals(False)
        self._redesenhar()

    def _redesenhar(self):
        """Reconstrói as séries respeitando os filtros de período e ativo."""
        dias = PERIODOS[self.combo_periodo.currentIndex()][1]
        inicio_ts = None
        if dias is not None:
            inicio_ts = int((datetime.now(timezone.utc) - timedelta(days=dias)).timestamp() * 1000)
        simbolo = None if self.combo_ativo.currentIndex() <= 0 else self.combo_ativo.currentText()
        if self.capital is None:
            return
        pontos = curva_equity(self.trades, self.capital, inicio_ts=inicio_ts, simbolo=simbolo)
        agora = int(datetime.now(timezone.utc).timestamp() * 1000)
        if self.equity_atual is not None and not simbolo:
            pontos = pontos + [(agora, self.equity_atual)]
        self.grafico_equity.definir_pontos(pontos)
        self.grafico_dd.definir_pontos(curva_drawdown(pontos))
