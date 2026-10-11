"""Limites conservadores, dimensionamento por stop e bloqueios de carteira."""

from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR
import math
import time

from .types import Decision, MarketState


@dataclass(frozen=True)
class Limits:
    min_confidence: float = 0.75
    min_probability: float = 0.65
    max_positions: int | None = None
    risk_per_trade: float = 0.02
    max_total_risk: float = 0.15
    max_currency_risk: float = 0.15
    max_daily_loss: float = 0.02
    max_drawdown: float = 0.10
    max_margin: float = 0.30
    max_spread_fraction: float = 0.001
    max_spread_stop: float = 0.075

    def __post_init__(self):
        """Rejeita limites inválidos antes de consultar ou operar."""
        for nome, valor in self.__dict__.items():
            if nome == "max_positions":
                if valor is None:
                    continue
                if isinstance(valor, bool) or not isinstance(valor, int) or valor <= 0:
                    raise ValueError("max_positions deve ser inteiro positivo.")
            elif not isinstance(valor, (int, float)) or not math.isfinite(valor) or not 0 < valor <= 1:
                raise ValueError(f"Limite inválido: {nome}")
        if self.risk_per_trade > self.max_total_risk:
            raise ValueError("Risco por operação excede risco total.")


@dataclass
class Gate:
    verdict: str
    reason: str
    volume: float = 0.0
    entry: float = 0.0
    stop: float = 0.0
    target: float = 0.0
    risk_amount: float = 0.0
    margin: float = 0.0


def bloqueio_spread(state, limits):
    """Compartilha a verificação de custo entre diagnóstico e autorização de entrada."""
    spread = state.ask - state.bid
    if (state.price <= 0 or state.stop_distance <= 0 or not math.isfinite(spread)
            or spread < 0 or spread / state.price > limits.max_spread_fraction
            or spread / state.stop_distance > limits.max_spread_stop):
        return "Spread elevado ou inválido"
    return ""


def check(decision: Decision, book, limits: Limits, state: MarketState, agora=None) -> Gate:
    """Dimensiona lotes pelo prejuízo no stop, nunca arredondando risco para cima."""
    def recusar(motivo):
        """Padroniza a recusa explicável de uma entrada."""
        return Gate("SKIP", motivo)

    if decision.action not in ("BUY", "SELL"):
        return recusar(f"{decision.action} não executa")
    if decision.source != "jev" or decision.symbol != state.symbol or state.source != "MT5":
        return recusar("Origem ou símbolo inválido")
    if any(not math.isfinite(v) or not 0 <= v <= 1 for v in (decision.confidence, decision.probability)):
        return recusar("Confiança/probabilidade inválida")
    if decision.confidence < limits.min_confidence or decision.probability < limits.min_probability:
        return recusar("Confiança/probabilidade abaixo do limite")
    if not book.active:
        return recusar("Paper pausado; use start (Iniciar trading)")
    if not book.prices_valid:
        return recusar("Posições sem cotação recente ou P&L válido")
    equity = book.equity()
    if equity <= 0 or equity <= book.day_start * (1 - limits.max_daily_loss):
        return recusar("Limite de perda diária atingido")
    if book.drawdown_blocked or equity <= book.high_water * (1 - limits.max_drawdown):
        return recusar("Limite de drawdown atingido")
    if book.daily_blocked:
        return recusar("Bloqueio diário permanece até o próximo dia UTC")
    if limits.max_positions is not None and len(book.fills) >= limits.max_positions:
        return recusar("Limite de posições atingido")
    if any(p.symbol == state.symbol for p in book.fills):
        return recusar("Já existe posição neste ativo")
    if state.trade_mode not in (1, 2, 4) or (state.trade_mode == 1 and decision.action == "SELL") or (state.trade_mode == 2 and decision.action == "BUY"):
        return recusar("Direção não permitida pelo broker")
    valores = (state.price, state.bid, state.ask, state.stop_distance,
               state.loss_per_lot, state.margin_per_lot, state.volume_min,
               state.volume_max, state.volume_step, state.tick_time)
    if any(not math.isfinite(v) or v <= 0 for v in valores):
        return recusar("Dados de preço/contrato/risco inválidos")
    idade = (time.time() if agora is None else agora) - state.tick_time
    if not 0 <= idade <= 120:
        return recusar("Cotação antiga ou relógio inconsistente")
    custo = bloqueio_spread(state, limits)
    if custo:
        return recusar(custo)
    risco = equity * limits.risk_per_trade
    passo = Decimal(str(state.volume_step))
    volume = float((Decimal(str(min(risco / state.loss_per_lot, state.volume_max))) / passo)
                   .to_integral_value(rounding=ROUND_FLOOR) * passo)
    if volume < state.volume_min or volume <= 0:
        return recusar("Lote mínimo excede o orçamento de risco")
    risco_real = volume * state.loss_per_lot
    if sum(p.risk_amount for p in book.fills) + risco_real > equity * limits.max_total_risk:
        return recusar("Limite de risco agregado atingido")
    for moeda in state.currencies:
        exposicao = sum(p.risk_amount for p in book.fills if moeda in p.currencies)
        if exposicao + risco_real > equity * limits.max_currency_risk:
            return recusar(f"Exposição compartilhada em {moeda} atingiu o limite")
    margem = volume * state.margin_per_lot
    if sum(p.margin for p in book.fills) + margem > equity * limits.max_margin:
        return recusar("Margem paper excede 30% do patrimônio")
    entrada = state.ask if decision.action == "BUY" else state.bid
    direcao = 1 if decision.action == "BUY" else -1
    stop = entrada - direcao * state.stop_distance
    alvo = entrada + direcao * 2 * state.stop_distance
    if min(stop, alvo) <= 0:
        return recusar("Stop/alvo inválido")
    return Gate("EXECUTE", "", volume, entrada, stop, alvo, risco_real, margem)
