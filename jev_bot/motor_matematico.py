"""Motor matemático local: estatísticas de candles M1 dos últimos 30 dias.

Exclusivamente Python local, sem consumo de tokens. Não usa indicadores
técnicos (RSI, MACD, médias móveis como sinal): tudo aqui é contagem,
proporção e distribuição de movimentos observados. Nenhuma função retorna
probabilidade de lucro ou previsão; resultados descrevem o histórico.

A janela é móvel: a cada atualização, candles fora dos últimos 30 dias
deixam de ser usados por qualquer decisão. Um corte cronológico separa
70% iniciais (geração local de regras) dos 30% finais
(validação fora da amostra, nunca enviados à API).
"""

from __future__ import annotations

import math
import time

JANELA_DIAS = 30
CORTE_OOS = 0.7          # fração inicial usada para descoberta
MINIMO_DIAS = 7          # amostra mínima para estatísticas confiáveis


def percentil(valores, q):
    """Percentil por interpolação linear, sem dependências externas."""
    if not valores:
        return None
    s = sorted(valores)
    k = (len(s) - 1) * q
    f, c = math.floor(k), math.ceil(k)
    if f == c:
        return s[int(k)]
    return s[f] + (s[c] - s[f]) * (k - f)


def _arredonda(valor, casas=4):
    """Arredonda para economizar tokens, preservando None e finitude."""
    if valor is None:
        return None
    if not math.isfinite(valor):
        return None
    return round(valor, casas)


def _janela_maxima(valores, tamanho):
    """Máximo deslizante O(n) com deque; usado por regras com janela de minutos."""
    from collections import deque
    resultado = [0.0] * len(valores)
    fila = deque()
    for i, v in enumerate(valores):
        while fila and valores[fila[-1]] <= v:
            fila.pop()
        fila.append(i)
        if fila[0] <= i - tamanho:
            fila.popleft()
        resultado[i] = valores[fila[0]]
    return resultado


def _janela_minima(valores, tamanho):
    """Mínimo deslizante O(n) com deque."""
    from collections import deque
    resultado = [0.0] * len(valores)
    fila = deque()
    for i, v in enumerate(valores):
        while fila and valores[fila[-1]] >= v:
            fila.pop()
        fila.append(i)
        if fila[0] <= i - tamanho:
            fila.popleft()
        resultado[i] = valores[fila[0]]
    return resultado


def estatisticas(janela, spread=0.0):
    """Calcula estatísticas descritivas da janela M1 de um ativo.

    `janela` é o dicionário retornado por FonteMT5.janela_m1. Todos os
    valores percentuais estão em % do preço. Amostra (n) acompanha cada
    medida para que a incerteza possa ser avaliada antes de usar o número.
    """
    candles = janela["candles"]
    n = len(candles)
    dias = (candles[-1]["time"] - candles[0]["time"]) / 86400 if n >= 2 else 0
    base = {"simbolo": janela["simbolo"], "candles": n, "dias": _arredonda(dias, 2),
            "inicio": candles[0]["time"], "fim": candles[-1]["time"],
            "lacunas": janela.get("lacunas", 0), "minutos_lacuna": janela.get("minutos_lacuna", 0),
            "maior_lacuna": janela.get("maior_lacuna", 0),
            "custo_spread_pct": _arredonda(spread / candles[-1]["close"] * 100 if spread > 0 else 0),
            "aviso": "Estatísticas descritivas do histórico; não são probabilidade de lucro."}
    if n < 3:
        return {**base, "disponivel": False, "motivo": "Amostra insuficiente"}
    closes = [c["close"] for c in candles]
    if any(not math.isfinite(v) for v in closes):
        return {**base, "disponivel": False, "motivo": "Dados inválidos"}
    mov = [(b / a - 1) * 100 for a, b in zip(closes, closes[1:])]
    retorno_total = (closes[-1] / closes[0] - 1) * 100
    maximo = max(closes)
    minimo = min(closes)
    queda_maxima = (1 - minimo / maximo) * 100 if maximo > 0 else 0
    base.update(disponivel=True, retorno_janela_pct=_arredonda(retorno_total),
                queda_do_topo_pct=_arredonda(queda_maxima),
                preco_max=_arredonda(maximo), preco_min=_arredonda(minimo))

    # Distribuição dos movimentos de 1 minuto.
    positivos = [m for m in mov if m > 0]
    negativos = [m for m in mov if m < 0]
    media_mov = sum(mov) / len(mov)
    base["movimento_1min"] = {
        "n": len(mov), "media_pct": _arredonda(media_mov),
        "mediana_pct": _arredonda(percentil(mov, .5)),
        "p90_pct": _arredonda(percentil(mov, .9)), "p99_pct": _arredonda(percentil(mov, .99)),
        "desvio_pct": _arredonda(math.sqrt(sum((m - media_mov) ** 2 for m in mov) / len(mov))),
        "max_alta_pct": _arredonda(max(mov)), "max_baixa_pct": _arredonda(min(mov)),
        "altas": len(positivos), "baixas": len(negativos),
        "frac_alta": _arredonda(len(positivos) / len(mov)),
    }

    # Frequência de movimentos relevantes e amplitude por candle.
    amplitudes = [(c["high"] / c["low"] - 1) * 100 for c in candles if c["low"] > 0]
    base["amplitude_1min"] = {
        "n": len(amplitudes), "mediana_pct": _arredonda(percentil(amplitudes, .5)),
        "p90_pct": _arredonda(percentil(amplitudes, .9)), "p99_pct": _arredonda(percentil(amplitudes, .99)),
    }
    for limiar in (0.1, 0.25, 0.5, 1.0):
        base[f"freq_abs_{limiar}"] = _arredonda(sum(abs(m) >= limiar for m in mov) / len(mov))

    # Retornos condicionais: sequências de quedas/altas consecutivas.
    for grupo, sinal in (("apos_quedas", -1), ("apos_altas", 1)):
        condicional = {}
        for k in (1, 2, 3):
            indices = [i for i in range(k, len(mov))
                       if all((mov[i - j - 1] * sinal > 0) for j in range(k))]
            if len(indices) < 10:
                continue
            seguintes = [mov[i] for i in indices]
            condicional[str(k)] = {
                "n": len(seguintes),
                "media_pct": _arredonda(sum(seguintes) / len(seguintes)),
                "mediana_pct": _arredonda(percentil(seguintes, .5)),
                "taxa_mesma_direcao": _arredonda(sum(m * sinal > 0 for m in seguintes) / len(seguintes)),
            }
        base[grupo] = condicional

    # Recuperação após quedas e retração após altas, com referência deslizante.
    referencia = 30  # minutos de histórico que definem topo/fundo local
    topo = _janela_maxima(closes, referencia)
    fundo = _janela_minima(closes, referencia)
    for limiar in (0.2, 0.5, 1.0):
        quedas = [i for i in range(1, n) if closes[i] <= topo[i] * (1 - limiar / 100)]
        altas = [i for i in range(1, n) if closes[i] >= fundo[i] * (1 + limiar / 100)]
        base[f"queda_{limiar}"] = _resumo_evento(quedas, closes, meta=lambda i, ref: closes[i] >= ref * 0.9995,
                                                 referencia=topo, horizonte=60)
        base[f"alta_{limiar}"] = _resumo_evento(altas, closes, meta=lambda i, ref: closes[i] <= ref * 1.0005,
                                                referencia=fundo, horizonte=60, meia_vida=True)

    # Tempos até movimento de X% a partir de cada fechamento.
    for limiar in (0.25, 0.5, 1.0):
        tempos = []
        limite = min(n - 1, 360)  # procura até 6h à frente por evento
        for i in range(0, n - 1, 5):   # amostragem a cada 5 minutos reduz custo
            alvo = closes[i] * (1 + limiar / 100)
            piso = closes[i] * (1 - limiar / 100)
            for j in range(i + 1, min(i + limite + 1, n)):
                if closes[j] >= alvo or closes[j] <= piso:
                    tempos.append(j - i)
                    break
        base[f"tempo_para_{limiar}"] = {
            "n": len(tempos), "mediana_min": _arredonda(percentil(tempos, .5), 1),
            "p90_min": _arredonda(percentil(tempos, .9), 1),
            "taxa_em_60min": _arredonda(sum(t <= 60 for t in tempos) / len(tempos)) if tempos else None,
        }
    return base


def _resumo_evento(indices, closes, meta, referencia, horizonte, meia_vida=False):
    """Resume quantos eventos atingem uma meta em `horizonte` minutos.

    Para quedas, a meta é voltar ao nível de referência (topo local).
    Para altas, `meia_vida` mede a devolução de metade do movimento
    (retração), não uma reversão completa.
    """
    if not indices:
        return {"n": 0, "taxa": None, "tempo_mediano_min": None}
    tempos, atingidos = [], 0
    for i in indices:
        alvo = referencia[i]
        for j in range(i + 1, min(i + horizonte + 1, len(closes))):
            if meia_vida and closes[j] <= alvo + (closes[i] - alvo) * 0.5:
                tempos.append(j - i); atingidos += 1; break
            if not meia_vida and meta(j, alvo):
                tempos.append(j - i); atingidos += 1; break
    return {"n": len(indices), "atingiram_meta": atingidos,
            "taxa": _arredonda(atingidos / len(indices)),
            "tempo_mediano_min": _arredonda(percentil(tempos, .5), 1) if tempos else None}


def resumo_compacto(stats):
    """Seleciona os números essenciais para o prompt, mantendo o payload pequeno."""
    if not stats.get("disponivel"):
        return {"simbolo": stats.get("simbolo"), "disponivel": False,
                "motivo": stats.get("motivo", "Amostra insuficiente")}
    chaves = ("simbolo", "dias", "candles", "lacunas", "minutos_lacuna", "custo_spread_pct",
              "retorno_janela_pct", "queda_do_topo_pct", "movimento_1min", "amplitude_1min",
              "apos_quedas", "apos_altas", "tempo_para_0.25", "tempo_para_0.5", "tempo_para_1.0")
    resumo = {c: stats[c] for c in chaves if c in stats}
    resumo["quedas_e_recuperacao"] = {f"queda_{l}": stats.get(f"queda_{l}") for l in (0.2, 0.5, 1.0)}
    resumo["altas_e_retracao"] = {f"alta_{l}": stats.get(f"alta_{l}") for l in (0.2, 0.5, 1.0)}
    resumo["frequencias"] = {f"movimentos_{l}pct_ou_mais": stats.get(f"freq_abs_{l}")
                             for l in (0.1, 0.25, 0.5, 1.0)}
    resumo["amostra_cronologica"] = ("resumo calculado sobre os 70% iniciais da janela;"
                                     " os 30% finais são reservados para validação fora da amostra")
    return resumo


def janela_descoberta(candles):
    """Corte cronológico: 70% iniciais alimentam descoberta (sem vazamento)."""
    corte = int(len(candles) * CORTE_OOS)
    return candles[:corte], candles[corte:]


def idade_janela(janela, relogio=time.time):
    """Idade do último candle em segundos, para decidir atualização da janela."""
    return relogio() - janela["fim"]
