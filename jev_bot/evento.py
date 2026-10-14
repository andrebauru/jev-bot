"""Evento quantitativo único por ativo/candle (M1 fechado, sem look-ahead).

Um único objeto descritivo por símbolo e candle — retração/recuperação/gap/
neutro — com todos os números disponíveis e `null` explícito para ausentes.
Não decide BUY/SELL: a direção é exclusiva do JEv. Não inventa expectativa nem
probabilidade a partir de amostra insuficiente.
"""

from __future__ import annotations

import math

JANELA_EVENTO = 20            # candles recentes para topo/fundo local
MIN_BARRAS = JANELA_EVENTO + 5  # mínimo para um cálculo matemático mínimo
LIMIAR_EVENTO = 0.7          # fração do range que define retração/recuperação relevantes
LIMIAR_GAP_PCT = 0.05        # descontinuidade relevante entre abertura e fecho prévio


def _std(valores):
    if len(valores) < 2:
        return None
    media = sum(valores) / len(valores)
    return math.sqrt(sum((v - media) ** 2 for v in valores) / len(valores))


def evento_quantitativo(candles, janela=JANELA_EVENTO, spread_pct=None):
    """Calcula o evento do último candle fechado. Nunca vê o candle em formação.

    Devolve `status="INVALID_CORE_DATA"` quando falta o essencial (símbolo,
    candle ou preço) — nesse caso o chamador registra impedimento técnico e
    não envia payload fabricado.
    """
    candles = list(candles or [])
    bars_available = len(candles)
    base = {"event_type": None, "bars_available": bars_available,
            "bars_used": 0, "janela": int(janela),
            "last_closed_m1": (candles[-1].get("time") if candles else None),
            "coverage_minutes": (int((candles[-1]["time"] - candles[0]["time"]) / 60)
                                 if bars_available >= 2 else 0),
            "missing_optional_fields": []}
    if bars_available < MIN_BARRAS:
        return {**base, "status": "INVALID_CORE_DATA", "data_quality": "DADOS_INSUFICIENTES",
                "reason": f"apenas {bars_available} barras; mínimo {MIN_BARRAS}"}
    try:
        closes = [float(c["close"]) for c in candles]
        highs = [float(c["high"]) for c in candles]
        lows = [float(c["low"]) for c in candles]
        aberturas = [float(c["open"]) for c in candles]
    except (TypeError, KeyError, ValueError):
        return {**base, "status": "INVALID_CORE_DATA", "data_quality": "DADOS_INSUFICIENTES",
                "reason": "OHLC inválido"}
    if any(not math.isfinite(v) or v <= 0 for v in closes):
        return {**base, "status": "INVALID_CORE_DATA", "data_quality": "DADOS_INSUFICIENTES",
                "reason": "preço inválido"}

    recorte = candles[-janela:]
    H = max(float(c["high"]) for c in recorte)
    L = min(float(c["low"]) for c in recorte)
    C = closes[-1]
    A = H - L
    C_prev = closes[-2]
    C_5 = closes[-6] if bars_available >= 6 else None
    posicao = (C - L) / A if A > 0 else None
    retracao = (H - C) / A if A > 0 else None
    recuperacao = (C - L) / A if A > 0 else None
    retornos = [closes[i] / closes[i - 1] - 1 for i in range(1, bars_available)]
    volatilidade = _std(retornos[-janela:])
    retorno_1m = C / C_prev - 1
    retorno_5m = (C / C_5 - 1) if C_5 else None
    open_atual = aberturas[-1]
    gap_pct = (open_atual / C_prev - 1) if C_prev else None

    if retracao is not None and retracao >= LIMIAR_EVENTO:
        evento = "RETRACAO_ALTA"
    elif recuperacao is not None and recuperacao >= LIMIAR_EVENTO:
        evento = "RECUPERACAO_BAIXA"
    else:
        evento = "MOVIMENTO_NEUTRO"
    gap_evento = None
    if gap_pct is not None and abs(gap_pct) * 100 >= LIMIAR_GAP_PCT:
        gap_evento = "GAP_ALTA" if gap_pct > 0 else "GAP_BAIXA"

    faltantes = []
    if spread_pct is None:
        faltantes.append("spread_pct")
    if retorno_5m is None:
        faltantes.append("retorno_5m")
    if gap_pct is None:
        faltantes.append("gap_pct")
    return {
        "status": "OK", "event_type": evento, "gap_event_type": gap_evento,
        "high": round(H, 10), "low": round(L, 10), "close": round(C, 10),
        "range": round(A, 10), "posicao_no_range": (round(posicao, 4) if posicao is not None else None),
        "retracao_da_maxima": (round(retracao, 4) if retracao is not None else None),
        "recuperacao_do_minimo": (round(recuperacao, 4) if recuperacao is not None else None),
        "retorno_1m": round(retorno_1m, 6), "retorno_5m": (round(retorno_5m, 6) if retorno_5m is not None else None),
        "volatilidade_1m": (round(volatilidade, 8) if volatilidade is not None else None),
        "gap_pct": (round(gap_pct, 6) if gap_pct is not None else None),
        "spread_pct": (round(spread_pct, 6) if spread_pct is not None else None),
        "bars_used": min(bars_available, janela),
        "bars_available": bars_available,
        "coverage_minutes": int((candles[-1]["time"] - candles[0]["time"]) / 60),
        "last_closed_m1": candles[-1].get("time"),
        "data_quality": "OK",
        "missing_optional_fields": faltantes,
        "nota": "observações quantitativas; não são recomendação de trade",
    }
