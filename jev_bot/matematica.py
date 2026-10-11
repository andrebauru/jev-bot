"""Diagnósticos experimentais de candles reais; não geram decisões ou confiança."""

import cmath
import math


def janela_direcional(precos, spread):
    """Compara inclinação ajustada e dispersão com o custo real, sem prever lucro."""
    n = len(precos)
    centro = (n - 1) / 2
    media = sum(precos) / n
    denominador = sum((i - centro) ** 2 for i in range(n))
    inclinacao = sum((i - centro) * (p - media) for i, p in enumerate(precos)) / denominador
    residuos = [p - (media + inclinacao * (i - centro)) for i, p in enumerate(precos)]
    dispersao = math.sqrt(sum(r * r for r in residuos) / n)
    variacao = sum((p - media) ** 2 for p in precos)
    ajuste = max(0, 1 - sum(r * r for r in residuos) / variacao) if variacao else 0
    return {"candles": n, "inclinacao_por_minuto": inclinacao, "dispersao_preco": dispersao,
            "qualidade_ajuste": ajuste,
            "deslocamento_5min_linear": 5 * inclinacao,
            "deslocamento_5min_em_spreads": 5 * inclinacao / spread if spread > 0 else None,
            "aviso": "Extrapolação linear hipotética; não é previsão nem retorno esperado."}


def resumir(candles, passo, spread):
    """Resume deslocamento, custo e amplitudes de fase numa janela curta M1."""
    barras = candles[-20:]
    if len(barras) < 3:
        return {"disponivel": False, "motivo": "Histórico insuficiente"}
    precos = [float(b["close"]) for b in barras]
    volumes = [float(b["tick_volume"]) for b in barras[1:]]
    if any(not math.isfinite(p) or p <= 0 for p in precos) or any(not math.isfinite(v) or v < 0 for v in volumes):
        return {"disponivel": False, "motivo": "Dados inválidos"}
    retornos = [math.log(b / a) for a, b in zip(precos, precos[1:])]
    caminho = sum(abs(b - a) for a, b in zip(precos, precos[1:]))
    deslocamento = precos[-1] - precos[0]
    resultado = {"disponivel": True, "origem": "OHLC MT5 M1 fechado", "janela": len(barras),
                 "retorno_janela": precos[-1] / precos[0] - 1,
                 "eficiencia_direcional": deslocamento / caminho if caminho else 0,
                 "deslocamento_em_spreads": deslocamento / spread if spread > 0 else None,
                 "aviso": "Diagnósticos descritivos; não são probabilidades de lucro nem previsão."}
    janelas = [janela_direcional(precos[-n:], spread) for n in (5, 10, 20) if len(precos) >= n]
    resultado["janelas_direcionais"] = janelas
    direcoes = [1 if j["inclinacao_por_minuto"] > 0 else -1 if j["inclinacao_por_minuto"] < 0 else 0 for j in janelas]
    resultado["concordancia_direcional"] = sum(direcoes) / len(direcoes) if direcoes else 0
    resultado["custo_spread_preco"] = spread
    if not sum(volumes):
        resultado["amplitudes"] = {"disponivel": False, "motivo": "Volume de ticks ausente"}
        return resultado
    escala = max(abs(r) for r in retornos)
    amplitudes = dict(alta=0j, baixa=0j, estavel=0j)
    soma_raizes = 0
    for r, v in zip(retornos, volumes):
        tolerancia = passo / precos[-1]
        grupo = "alta" if r > tolerancia else "baixa" if r < -tolerancia else "estavel"
        raiz = math.sqrt(v / sum(volumes))
        amplitudes[grupo] += raiz * cmath.exp(1j * (math.pi / 2) * r / escala) if escala else raiz
        soma_raizes += raiz
    norma = sum(abs(a) ** 2 for a in amplitudes.values())
    pesos = {k: abs(a) ** 2 / norma for k, a in amplitudes.items()}
    resultado["amplitudes"] = {"disponivel": True, "experimental": True, "pesos_historicos": pesos,
        "coerencia": min(1, abs(sum(amplitudes.values())) ** 2 / soma_raizes ** 2),
        "entropia": -sum(p * math.log(p) for p in pesos.values() if p) / math.log(3),
        "saldo_direcional": pesos["alta"] - pesos["baixa"]}
    return resultado
