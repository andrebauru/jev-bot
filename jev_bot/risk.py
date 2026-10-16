"""Limites conservadores, dimensionamento por stop e bloqueios de carteira."""

from dataclasses import dataclass, field
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
    reason_code: str = ""
    details: dict = field(default_factory=dict)


def _numero_valido(valor):
    """Dado numérico presente e finito; `None`/NaN são rejeitados explicitamente."""
    return (isinstance(valor, (int, float)) and not isinstance(valor, bool)
            and math.isfinite(valor))


def medir_spread(state, limits):
    """Mede o custo com unidades explicitas: preco, pontos, percentual e spread/stop.

    Devolve (spread_abs | None, detalhes). `None` significa dado ausente/invalido
    e nunca e convertido em zero.
    """
    ponto = 10 ** -state.digits if isinstance(state.digits, int) and state.digits >= 0 else None
    detalhes = {
        "bid": state.bid, "ask": state.ask, "last": state.price,
        "tick_time": state.tick_time, "digits": state.digits, "point": ponto,
        "stop_distance": state.stop_distance,
        "limite_spread_preco_pct": limits.max_spread_fraction,
        "limite_spread_stop_pct": limits.max_spread_stop,
    }
    if not all(_numero_valido(v) for v in (state.bid, state.ask, state.price)):
        return None, detalhes
    spread = state.ask - state.bid
    detalhes["spread_abs"] = spread
    detalhes["spread_pontos"] = (round(spread / ponto, 2) if ponto else None)
    detalhes["spread_pct"] = (round(spread / state.price, 8) if state.price else None)
    detalhes["spread_fracao_stop"] = (round(spread / state.stop_distance, 6)
                                      if _numero_valido(state.stop_distance) and state.stop_distance
                                      else None)
    return spread, detalhes


def bloqueio_spread(state, limits):
    """Motivo específico do custo (string vazia = sem bloqueio).

    Distingue dado ausente, ask<bid, preço/stop inválido e cada teto violado
    (percentual do preco x fracao do stop), sem usar mensagem generica unica.
    """
    spread, _detalhes = medir_spread(state, limits)
    if spread is None:
        return "Dados de preço/spread ausentes"
    if not math.isfinite(spread) or spread < 0:
        return "Spread inválido (ask menor que bid)"
    if state.price <= 0 or state.stop_distance <= 0:
        return "Preço ou distância de stop inválidos"
    if spread / state.price > limits.max_spread_fraction:
        return "Spread acima do limite percentual do preço"
    if spread / state.stop_distance > limits.max_spread_stop:
        return "Spread acima do limite da distância de stop"
    return ""


def pre_check(side: str, book, limits: Limits, state: MarketState, agora=None) -> Gate:
    """Avalia e dimensiona uma entrada ja direcionada pelo Python (pre e pos-JEv).

    Nao conhece JEv nem probabilidade: direcao fixa, dados de mercado, limites
    de risco e capacidade de execucao PAPER. `SKIP` = veto deterministico, com
    codigo (`reason_code`) e medidas (`details`) para auditoria na unidade certa.
    """
    def recusar(motivo, codigo, detalhes=None):
        return Gate("SKIP", motivo, reason_code=codigo, details=detalhes or {})

    if side not in ("BUY", "SELL"):
        return recusar("Direção proposta inválida", "INVALID_SIDE")
    if state.source != "MT5":
        return recusar("Origem inválida", "INVALID_ORIGIN")
    if not book.active:
        return recusar("Paper pausado; use start (Iniciar trading)", "PAPER_INACTIVE")
    if not book.prices_valid:
        return recusar("Posições sem cotação recente ou P&L válido", "PRICES_INVALID")
    equity = book.equity()
    if equity <= 0 or equity <= book.day_start * (1 - limits.max_daily_loss):
        return recusar("Limite de perda diária atingido", "DAILY_LOSS_LIMIT",
                       {"equity": equity, "day_start": book.day_start,
                        "limite_perda_diaria": limits.max_daily_loss})
    if book.drawdown_blocked or equity <= book.high_water * (1 - limits.max_drawdown):
        return recusar("Limite de drawdown atingido", "DRAWDOWN_LIMIT",
                       {"equity": equity, "high_water": book.high_water,
                        "limite_drawdown": limits.max_drawdown})
    if book.daily_blocked:
        return recusar("Bloqueio diário permanece até o próximo dia UTC", "DAILY_BLOCKED")
    if limits.max_positions is not None and len(book.fills) >= limits.max_positions:
        return recusar("Limite de posições atingido", "MAX_POSITIONS",
                       {"posicoes": len(book.fills), "limite": limits.max_positions})
    if any(p.symbol == state.symbol for p in book.fills):
        return recusar("Já existe posição neste ativo", "POSITION_EXISTS")
    if (state.trade_mode not in (1, 2, 4)
            or (state.trade_mode == 1 and side == "SELL")
            or (state.trade_mode == 2 and side == "BUY")):
        return recusar("Direção não permitida pelo broker", "DIRECTION_NOT_ALLOWED",
                       {"trade_mode": state.trade_mode, "side": side})
    valores = (state.price, state.bid, state.ask, state.stop_distance,
               state.loss_per_lot, state.margin_per_lot, state.volume_min,
               state.volume_max, state.volume_step, state.tick_time)
    if any(not _numero_valido(v) or v <= 0 for v in valores):
        medidos = {"bid": state.bid, "ask": state.ask, "last": state.price,
                   "stop_distance": state.stop_distance, "volume_min": state.volume_min,
                   "volume_step": state.volume_step, "volume_max": state.volume_max,
                   "loss_per_lot": state.loss_per_lot, "margin_per_lot": state.margin_per_lot,
                   "tick_time": state.tick_time}
        return recusar("Dados de preço/contrato/risco inválidos", "INVALID_MARKET_DATA", medidos)
    idade = (time.time() if agora is None else agora) - state.tick_time
    if not 0 <= idade <= 120:
        return recusar("Cotação antiga ou relógio inconsistente", "STALE_QUOTE",
                       {"idade_s": round(idade, 1), "limite_s": 120})
    spread, detalhes_spread = medir_spread(state, limits)
    if spread is None:
        return recusar("Dados de preço/spread ausentes", "INVALID_MARKET_DATA", detalhes_spread)
    if spread < 0 or not math.isfinite(spread):
        return recusar("Spread inválido (ask menor que bid)", "SPREAD_INVALID", detalhes_spread)
    if state.price <= 0 or state.stop_distance <= 0:
        return recusar("Preço ou distância de stop inválidos", "INVALID_STOP", detalhes_spread)
    if spread / state.price > limits.max_spread_fraction:
        return recusar("Spread acima do limite percentual do preço", "SPREAD_TOO_HIGH", detalhes_spread)
    if spread / state.stop_distance > limits.max_spread_stop:
        return recusar("Spread acima do limite da distância de stop", "SPREAD_TOO_HIGH_STOP",
                       detalhes_spread)
    risco = equity * limits.risk_per_trade
    passo = Decimal(str(state.volume_step))
    volume = float((Decimal(str(min(risco / state.loss_per_lot, state.volume_max))) / passo)
                   .to_integral_value(rounding=ROUND_FLOOR) * passo)
    if volume < state.volume_min or volume <= 0:
        return recusar("Lote mínimo excede o orçamento de risco", "MIN_LOT_EXCEEDS_RISK",
                       {"volume_calculado": volume, "volume_min": state.volume_min,
                        "risco_orcado": round(risco, 6), "loss_per_lot": state.loss_per_lot})
    risco_real = volume * state.loss_per_lot
    if sum(p.risk_amount for p in book.fills) + risco_real > equity * limits.max_total_risk:
        return recusar("Limite de risco agregado atingido", "AGGREGATE_RISK_LIMIT",
                       {"risco_real": round(risco_real, 6),
                        "risco_aberto": round(sum(p.risk_amount for p in book.fills), 6),
                        "limite_total": limits.max_total_risk})
    for moeda in state.currencies:
        exposicao = sum(p.risk_amount for p in book.fills if moeda in p.currencies)
        if exposicao + risco_real > equity * limits.max_currency_risk:
            return recusar(f"Exposição compartilhada em {moeda} atingiu o limite",
                           "CURRENCY_EXPOSURE_LIMIT",
                           {"moeda": moeda, "exposicao": round(exposicao, 6),
                            "risco_real": round(risco_real, 6),
                            "limite_moeda": limits.max_currency_risk})
    margem = volume * state.margin_per_lot
    if sum(p.margin for p in book.fills) + margem > equity * limits.max_margin:
        return recusar("Margem paper excede o teto do patrimônio", "MARGIN_LIMIT",
                       {"margem": round(margem, 6), "limite_margem": limits.max_margin})
    entrada = state.ask if side == "BUY" else state.bid
    direcao = 1 if side == "BUY" else -1
    stop = entrada - direcao * state.stop_distance
    alvo = entrada + direcao * 2 * state.stop_distance
    if min(stop, alvo) <= 0:
        return recusar("Stop/alvo inválido", "INVALID_STOP",
                       {"entrada": entrada, "stop": stop, "alvo": alvo})
    return Gate("EXECUTE", "", volume, entrada, stop, alvo, risco_real, margem,
                reason_code="APPROVED",
                details={**detalhes_spread, "volume": volume, "risco_real": round(risco_real, 6),
                         "entrada": entrada, "stop": stop, "alvo": alvo})


def check(decision: Decision, book, limits: Limits, state: MarketState, agora=None) -> Gate:
    """Compatibilidade: valida a decisao legada e delega o dimensionamento."""
    if decision.action not in ("BUY", "SELL"):
        return Gate("SKIP", f"{decision.action} nao executa")
    if decision.source != "jev" or decision.symbol != state.symbol or state.source != "MT5":
        return Gate("SKIP", "Origem ou simbolo invalido")
    if not (_numero_valido(decision.confidence) and _numero_valido(decision.probability)):
        return Gate("SKIP", "Confianca/probabilidade ausente ou invalida")
    if any(not 0 <= v <= 1 for v in (decision.confidence, decision.probability)):
        return Gate("SKIP", "Confianca/probabilidade invalida")
    if decision.confidence < limits.min_confidence or decision.probability < limits.min_probability:
        return Gate("SKIP", "Confianca/probabilidade abaixo do limite")
    return pre_check(decision.action, book, limits, state, agora)
