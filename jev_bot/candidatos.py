"""Candidatos operacionais determinísticos, sem dependência do DeepSeek.

O caminho de trading não pode depender do aprendizado: taxas de chamada da
IA, limites por hora ou indisponibilidade não podem deixar a carteira sem
avaliação nos candles recentes. Aqui, o Python gera regras candidatas
diretamente das estatísticas condicionais JÁ calculadas na janela de
descoberta (apos_quedas/apos_altas, recuperação/retração). Cada candidato:

- só existe quando a condição está verdadeira no último candle FECHADO;
- usa apenas números computados localmente (n, taxa, expectativa) — nunca
  inventa valores;
- passa pela MESMA validação independente de `padroes.validar` (fora da
  amostra cronológica, eventos não sobrepostos, custo de spread), inclusive
  quando serviços externos estão indisponíveis.

Não é um atalho para abrir posições: é o mesmo rigor estatístico, aplicado
sem esperar uma nova sessão de aprendizagem. Limiares nunca são afrouxados.
"""

from __future__ import annotations

from . import padroes

MIN_N_LOCAL = padroes.MIN_EVENTOS       # mesma amostra mínima da validação
MIN_TAXA_LOCAL = padroes.MIN_TAXA       # mesma taxa mínima da validação
HORIZONTE_SEQUENCIA = 5                 # minutos após o evento
HORIZONTE_RECUPERACAO = 30              # minutos após queda/alta relevante
REFERENCIA_MIN = 30                     # janela local para topo/fundo
LIMIAR_MOVIMENTO_PCT = 0.5              # somente para comparar com a busca-base anterior
MAX_LOCAL_HYPOTHESES = 12               # orçamento fixo por ativo/candle
MAX_PER_FAMILY = 2                      # limita busca dentro de cada família


def _fechando_para_baixo(closes, k):
    # "k fechamentos em queda consecutiva" = k closes descendo = k-1
    # comparações (mesma semântica de padroes.indices_condicao).
    return all(closes[-j] < closes[-j - 1] for j in range(1, k))


def _fechando_para_cima(closes, k):
    return all(closes[-j] > closes[-j - 1] for j in range(1, k))


def _taxa(valor):
    return valor if isinstance(valor, (int, float)) else 0.0


LIMIARES_RELEVANCIA = {
    ("movimento_1min.media_pct"): 0.05,
    ("retorno_janela_pct"): 0.5,
    ("custo_spread_pct"): 0.02,
}


def _estatistica(resumo, chaves):
    valor = resumo
    for chave in chaves:
        if not isinstance(valor, dict):
            return None
        valor = valor.get(chave)
    if isinstance(valor, (int, float)) and valor == valor:
        return float(valor)
    return None


def mudanca_relevante(anterior, atual):
    """Detecta mudanca estatisticamente relevante entre dois resumos.
    Comparacao por limiares absolutos em metricas-chave (movimento medio,
    retorno da janela, custo de spread). anterior None (primeira
    referencia) NAO e gatilho - a revisao periodica por TTL cobre o resto.
    Evita disparar o aprendizado por ruido de um unico candle.
    """
    if not isinstance(anterior, dict) or not isinstance(atual, dict):
        return False
    for chaves, limiar in LIMIARES_RELEVANCIA.items():
        a = _estatistica(anterior, chaves.split("."))
        b = _estatistica(atual, chaves.split("."))
        if a is None or b is None:
            continue
        if abs(b - a) >= limiar:
            return True
    return False

def regras_candidatas_baseline(stats, candles):
    """Famílias originais condicionais, mantidas para comparação antes/depois.

    Devolve uma lista de regras no formato de `padroes` (tipo_regra,
    parametros, direcao, horizonte_minutos, justificativa), deduplicada pela
    chave canônica. Lista vazia quando não há estatística disponível, amostra
    suficiente ou edge histórico acima do limiar.
    """
    if not stats.get("disponivel") or len(candles) < REFERENCIA_MIN + 2:
        return []
    closes = [float(c["close"]) for c in candles]
    regras = []

    def adicionar(tipo, parametros, direcao, horizonte, justificativa):
        regras.append({"tipo_regra": tipo, "parametros": parametros,
                       "direcao": direcao, "horizonte_minutos": horizonte,
                       "justificativa": justificativa})

    # Sequências de quedas/altas: direção com edge histórico acima do limiar.
    for k in (2, 3):
        if _fechando_para_baixo(closes, k):
            amostra = stats.get("apos_quedas", {}).get(str(k))
            if amostra and amostra.get("n", 0) >= MIN_N_LOCAL:
                continuacao = _taxa(amostra.get("taxa_mesma_direcao"))
                reversao = 1 - continuacao
                if continuacao >= MIN_TAXA_LOCAL:
                    adicionar("quedas_consecutivas", {"n": k}, "SELL",
                              HORIZONTE_SEQUENCIA,
                              f"após {k} quedas, continuação em {continuacao:.0%} (n={amostra['n']})")
                if reversao >= MIN_TAXA_LOCAL:
                    adicionar("quedas_consecutivas", {"n": k}, "BUY",
                              HORIZONTE_SEQUENCIA,
                              f"após {k} quedas, reversão em {reversao:.0%} (n={amostra['n']})")
        if _fechando_para_cima(closes, k):
            amostra = stats.get("apos_altas", {}).get(str(k))
            if amostra and amostra.get("n", 0) >= MIN_N_LOCAL:
                continuacao = _taxa(amostra.get("taxa_mesma_direcao"))
                reversao = 1 - continuacao
                if continuacao >= MIN_TAXA_LOCAL:
                    adicionar("altas_consecutivas", {"n": k}, "BUY",
                              HORIZONTE_SEQUENCIA,
                              f"após {k} altas, continuação em {continuacao:.0%} (n={amostra['n']})")
                if reversao >= MIN_TAXA_LOCAL:
                    adicionar("altas_consecutivas", {"n": k}, "SELL",
                              HORIZONTE_SEQUENCIA,
                              f"após {k} altas, reversão em {reversao:.0%} (n={amostra['n']})")

    # Recuperação após queda / retração após alta relevantes (referência local).
    referencia = closes[-REFERENCIA_MIN:]
    topo, fundo = max(referencia), min(referencia)
    atual = closes[-1]
    if topo > 0 and atual <= topo * (1 - LIMIAR_MOVIMENTO_PCT / 100):
        recuperacao = stats.get(f"queda_{LIMIAR_MOVIMENTO_PCT}", {})
        if recuperacao.get("n", 0) >= MIN_N_LOCAL and _taxa(recuperacao.get("taxa")) >= MIN_TAXA_LOCAL:
            adicionar("queda_pct", {"pct": LIMIAR_MOVIMENTO_PCT, "minutos": REFERENCIA_MIN},
                      "BUY", HORIZONTE_RECUPERACAO,
                      f"queda de {LIMIAR_MOVIMENTO_PCT}% com recuperação em "
                      f"{recuperacao['taxa']:.0%} (n={recuperacao['n']})")
    if fundo > 0 and atual >= fundo * (1 + LIMIAR_MOVIMENTO_PCT / 100):
        retracao = stats.get(f"alta_{LIMIAR_MOVIMENTO_PCT}", {})
        if retracao.get("n", 0) >= MIN_N_LOCAL and _taxa(retracao.get("taxa")) >= MIN_TAXA_LOCAL:
            adicionar("alta_pct", {"pct": LIMIAR_MOVIMENTO_PCT, "minutos": REFERENCIA_MIN},
                      "SELL", HORIZONTE_RECUPERACAO,
                      f"alta de {LIMIAR_MOVIMENTO_PCT}% com retração em "
                      f"{retracao['taxa']:.0%} (n={retracao['n']})")

    # Deduplicação pela chave canônica (mesmo formato do repositório).
    vistos, unicos = set(), []
    for regra in regras:
        chave = padroes.chave_regra(regra["tipo_regra"], regra["parametros"],
                                    regra["direcao"], regra["horizonte_minutos"])
        if chave not in vistos:
            vistos.add(chave)
            regra["strategy_id"] = ("conditional_sequence" if regra["tipo_regra"] in
                                    ("quedas_consecutivas", "altas_consecutivas")
                                    else "local_recovery")
            unicos.append(regra)
    return unicos


def _familias_adicionais(stats, candles):
    """Hipóteses M1 predefinidas, escaladas por distribuição do próprio símbolo.

    Os limiares são extraídos apenas do recorte de treino (70% inicial). Os
    tipos/horizontes são fixos: nenhuma busca de hiperparâmetros usa o OOS.
    Cada família só emite a direção do evento que está verdadeira no candle
    fechado mais recente.
    """
    movimento = stats.get("movimento_1min") or {}
    amplitude = stats.get("amplitude_1min") or {}
    p75 = _taxa(movimento.get("p75_abs_pct"))
    p95 = _taxa(movimento.get("p95_abs_pct"))
    p99 = _taxa(movimento.get("p99_abs_pct"))
    amp_p50 = _taxa(amplitude.get("mediana_pct"))
    amp_p95 = _taxa(amplitude.get("p95_pct"))
    if min(p75, p95, p99, amp_p50, amp_p95) <= 0:
        return []

    regras = []

    def adicionar(tipo, parametros, direcao, horizonte, familia, nota):
        probe = {"tipo_regra": tipo, "parametros": parametros}
        if not padroes.condicao_atual(probe, candles):
            return
        regras.append({"tipo_regra": tipo, "parametros": parametros,
                       "direcao": direcao, "horizonte_minutos": horizonte,
                       "strategy_id": familia, "justificativa": nota})

    # Aceleração: mudança entre dois blocos consecutivos de retornos log.
    janela_acc = 5
    limiar_acc = round(p95 * (2 * janela_acc) ** 0.5, 8)
    adicionar("aceleracao_alta", {"janela": janela_acc, "limiar_pct": limiar_acc},
              "BUY", 5, "return_acceleration",
              f"aceleração positiva acima da escala empírica ({limiar_acc:g}%)")
    adicionar("aceleracao_baixa", {"janela": janela_acc, "limiar_pct": limiar_acc},
              "SELL", 5, "return_acceleration",
              f"aceleração negativa acima da escala empírica ({limiar_acc:g}%)")

    # Reversão/retração: limiar relativo derivado de retornos absolutos do treino.
    janela_reversao = 30
    limiar_reversao = round(max(p95 * janela_reversao ** 0.5, p99), 8)
    adicionar("reversao_queda", {"janela": janela_reversao, "pct": limiar_reversao},
              "BUY", 30, "empirical_mean_reversion",
              f"deslocamento de queda acima do quantil empírico ({limiar_reversao:g}%)")
    adicionar("reversao_alta", {"janela": janela_reversao, "pct": limiar_reversao},
              "SELL", 30, "empirical_mean_reversion",
              f"deslocamento de alta acima do quantil empírico ({limiar_reversao:g}%)")

    # Rompimento: extremo das 60 barras anteriores, sem incluir o candle atual.
    janela_rompimento = 60
    adicionar("rompimento_alta", {"janela": janela_rompimento}, "BUY", 5,
              "range_breakout", "fechamento acima do máximo das 60 barras anteriores")
    adicionar("rompimento_baixa", {"janela": janela_rompimento}, "SELL", 5,
              "range_breakout", "fechamento abaixo do mínimo das 60 barras anteriores")

    # Impulso seguido de duas barras de pausa, com retorno e amplitude empíricos.
    parametros_impulso = {"pausas": 2, "impulso_pct": round(p99, 8),
                          "pausa_pct": round(p75, 8), "retrace_ratio": 0.5}
    adicionar("impulso_pausa_alta", parametros_impulso, "BUY", 5,
              "impulse_pause_continuation", "impulso positivo e pausa curta sem retração profunda")
    adicionar("impulso_pausa_baixa", parametros_impulso, "SELL", 5,
              "impulse_pause_continuation", "impulso negativo e pausa curta sem retração profunda")

    # Expansão só após compressão recente: quantis são aprendidos no treino.
    parametros_expansao = {"pct": round(amp_p95, 8),
                           "compressao_pct": round(amp_p50, 8)}
    adicionar("expansao_amplitude_alta", parametros_expansao, "BUY", 5,
              "range_compression_expansion", "expansão positiva após cinco barras comprimidas")
    adicionar("expansao_amplitude_baixa", parametros_expansao, "SELL", 5,
              "range_compression_expansion", "expansão negativa após cinco barras comprimidas")
    return regras


def regras_candidatas(stats, candles):
    """Une famílias-base e novas hipóteses, sem busca combinatória/OOS tuning."""
    # Não reintroduz no caminho atual os antigos gatilhos fixos de 0,5%; as
    # famílias novas de reversão usam quantis de treino por símbolo.
    base = [r for r in regras_candidatas_baseline(stats, candles)
            if r["tipo_regra"] not in ("queda_pct", "alta_pct")]
    if not stats.get("disponivel"):
        return base[:MAX_LOCAL_HYPOTHESES]
    adicionais = _familias_adicionais(stats, candles)
    vistos, saida, contagem_familia = set(), [], {}
    for regra in base + adicionais:
        chave = padroes.chave_regra(regra["tipo_regra"], regra["parametros"],
                                    regra["direcao"], regra["horizonte_minutos"])
        familia = regra.get("strategy_id", regra["tipo_regra"])
        if (chave in vistos or contagem_familia.get(familia, 0) >= MAX_PER_FAMILY
                or len(saida) >= MAX_LOCAL_HYPOTHESES):
            continue
        vistos.add(chave)
        contagem_familia[familia] = contagem_familia.get(familia, 0) + 1
        saida.append(regra)
    return saida
