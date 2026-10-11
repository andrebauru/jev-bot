"""Gráficos QtCharts com tema escuro e "COPIAR COMO IMAGEM" no botão direito."""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCharts import (QChart, QChartView, QDateTimeAxis, QLineSeries,
                              QValueAxis)
from PySide6.QtCore import QDateTime, Qt
from PySide6.QtGui import QGuiApplication, QPainter, QColor, QPen
from PySide6.QtWidgets import QMenu


def _instante(iso):
    """Converte o timestamp ISO da carteira em msecs; fallback conservador."""
    try:
        return int(datetime.fromisoformat(iso).timestamp() * 1000)
    except (TypeError, ValueError):
        return 0


class GraficoView(QChartView):
    """QChartView com menu de contexto para copiar o gráfico visível."""

    def __init__(self, titulo, cor="#22c55e", parent=None):
        super().__init__(parent)
        self.setRenderHint(QPainter.Antialiasing)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._menu_contexto)
        self.chart().setBackgroundBrush(QColor("#0f1524"))
        self.chart().setTitleBrush(QColor("#e6edf7"))
        self.chart().setTitle(titulo)
        self.chart().legend().hide()
        self._cor = cor
        self.eixo_x = QDateTimeAxis()
        self.eixo_x.setFormat("dd/MM HH:mm")
        self.eixo_x.setLabelsColor("#8ea3c8")
        self.eixo_x.setLinePenColor(QColor("#26334f"))
        self.eixo_y = QValueAxis()
        self.eixo_y.setLabelsColor("#8ea3c8")
        self.eixo_y.setLinePenColor(QColor("#26334f"))
        self.eixo_y.setLabelFormat("%.2f")
        self.eixo_y.setGridLineColor(QColor("#1c2740"))
        self.serie = QLineSeries()
        pen = QPen(QColor(cor))
        pen.setWidth(2)
        self.serie.setPen(pen)
        self.chart().addSeries(self.serie)
        self.chart().setAxisX(self.eixo_x, self.serie)
        self.chart().setAxisY(self.eixo_y, self.serie)

    def _menu_contexto(self, posicao):
        """COPIAR COMO IMAGEM preserva exatamente o gráfico visível."""
        menu = QMenu(self)
        acao = menu.addAction("COPIAR COMO IMAGEM")
        if menu.exec(self.mapToGlobal(posicao)) is acao:
            QGuiApplication.clipboard().setPixmap(self.grab())

    def definir_pontos(self, pontos):
        """Substitui a série; `pontos` é [(msecs, valor), ...] cronológico."""
        self.serie.clear()
        for msecs, valor in pontos:
            if valor is None or msecs <= 0:
                continue
            self.serie.append(msecs, float(valor))
        inicio = pontos[0][0] if pontos else 0
        fim = pontos[-1][0] if pontos else 1
        self.eixo_x.setRange(QDateTime.fromMSecsSinceEpoch(inicio),
                             QDateTime.fromMSecsSinceEpoch(max(inicio + 1, fim)))


def curva_equity(trades, capital_inicial, inicio_ts=None, simbolo=None):
    """Curva de patrimônio realizado a partir dos fechamentos registrados.

    Usa exclusivamente dados persistidos pelo simulador. O ponto final é o
    patrimônio atual, que inclui P&L não realizado, marcado com o instante
    atual para diferenciar realizado de aberto.
    """
    fechados = [t for t in trades
                if t.get("status") == "CLOSED" and isinstance(t.get("profit"), (int, float))
                and (simbolo is None or t.get("symbol") == simbolo)]
    if inicio_ts is not None:
        fechados = [t for t in fechados
                    if _instante(t.get("closed_at")) >= inicio_ts]
    fechados.sort(key=lambda t: _instante(t.get("closed_at")))
    pontos, equity = [], capital_inicial
    for t in fechados:
        equity += t["profit"]
        pontos.append((_instante(t.get("closed_at")), equity))
    return pontos


def curva_drawdown(pontos_equity):
    """Drawdown percentual desde o pico da própria curva, ponto a ponto."""
    if not pontos_equity:
        return []
    pico = pontos_equity[0][1]
    resultado = []
    for msecs, valor in pontos_equity:
        pico = max(pico, valor)
        resultado.append((msecs, max(0.0, (1 - valor / pico) * 100) if pico > 0 else 0.0))
    return resultado
