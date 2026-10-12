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
  quando o orçamento de DeepSeek está esgotado.

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
LIMIAR_MOVIMENTO_PCT = 0.5              # mesma família de queda_0.5/alta_0.5


def _fechando_para_baixo(closes, k):
    # "k fechamentos em queda consecutiva" = k closes descendo = k-1
    # comparações (mesma semântica de padroes.indices_condicao).
    return all(closes[-j] < closes[-j - 1] for j in range(1, k))


def _fechando_para_cima(closes, k):
    return all(closes[-j] > closes[-j - 1] for j in range(1, k))


def _taxa(valor):
    return valor if isinstance(valor, (int, float)) else 0.0


def regras_candidatas(stats, candles):
    """Regras determinísticas para a condição verdadeira no último candle.

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
            unicos.append(regra)
    return unicos
