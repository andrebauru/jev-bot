"""Padrões: hipóteses reproduzíveis, validação local e repositório por ativo.

Uma hipótese só entra no repositório se for expressa por um dos tipos de
regra suportados, com parâmetros numéricos. O Python reproduz cada regra
sobre a janela M1 de 30 dias, conta ocorrências, mede acerto, magnitude,
dispersão e custo, e avalia a mesma regra separadamente na parte final
da janela (fora da amostra, cronológica). Nada aqui confunde correlação
histórica com garantia de retorno: o status registra exatamente a
evidência observada.

O repositório fica em JSON por ativo (data/padroes/<SIMBOLO>.json), com
escrita atômica, deduplicação e versionamento de schema, sem tocar o
db.json da carteira paper.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import time
from pathlib import Path

from . import motor_matematico

SCHEMA = 1
ALGORITMO_VERSAO = "regras-v2.1"  # semântica de regras + validação (off-by-one corrigido)
MIN_EVENTOS = 40        # ocorrências mínimas na janela inteira
MIN_EVENTOS_OOS = 12    # ocorrências mínimas fora da amostra
MIN_TAXA = 0.55         # taxa de acerto mínima, dentro e fora da amostra
VALIDADE_DIAS = 7       # validação mais antiga que isso expira
MAX_PADROES_ATIVO = 40

# Tipos de regra suportados: cada um é avaliado de forma determinística.
REGRAS = {
    "quedas_consecutivas": {"params": {"n": (2, 5)}, "desc": "n fechamentos de M1 em queda consecutiva"},
    "altas_consecutivas": {"params": {"n": (2, 5)}, "desc": "n fechamentos de M1 em alta consecutiva"},
    "queda_pct": {"params": {"pct": (0.05, 5.0), "minutos": (5, 240)},
                  "desc": "queda de pct% a partir do topo local dos últimos minutos"},
    "alta_pct": {"params": {"pct": (0.05, 5.0), "minutos": (5, 240)},
                 "desc": "alta de pct% a partir do fundo local dos últimos minutos"},
    "amplitude_1min_pct": {"params": {"pct": (0.02, 5.0)},
                           "desc": "amplitude do último candle M1 >= pct% do preço"},
    "movimento_pct": {"params": {"pct": (0.05, 5.0), "minutos": (5, 240)},
                      "desc": "movimento de pct% em minutos, na direção da condição"},
}
DIRECOES = ("BUY", "SELL")


class ErroRegra(ValueError):
    """Regra malformada ou parâmetro fora de faixa."""


def chave_regra(tipo, parametros, direcao, horizonte):
    """Chave canônica de deduplicação de um padrão."""
    params = ",".join(f"{k}={parametros[k]:g}" for k in sorted(parametros))
    return f"{tipo}|{params}|{direcao}|{int(horizonte)}"


def id_regra(chave):
    """Identificador estável derivado da chave canônica."""
    return hashlib.sha1(chave.encode("utf-8")).hexdigest()[:12]


def normalizar_hipotese(hipotese):
    """Valida e normaliza uma hipótese recebida do DeepSeek.

    Campos desconhecidos são descartados; valores inválidos levantam
    ErroRegra. A hipótese normalizada contém apenas regra, direção,
    horizonte e justificativa — nunca probabilidade ou confiança.
    """
    if not isinstance(hipotese, dict):
        raise ErroRegra("Hipótese não é um objeto.")
    tipo = str(hipotese.get("tipo_regra", "")).strip()
    if tipo not in REGRAS:
        raise ErroRegra(f"tipo_regra desconhecido: {tipo[:40]}")
    parametros = hipotese.get("parametros")
    if not isinstance(parametros, dict):
        raise ErroRegra("parametros ausente")
    especificacao = REGRAS[tipo]["params"]
    if set(parametros) - set(especificacao):
        raise ErroRegra("parâmetros fora do contrato da regra")
    limpos = {}
    for nome, (minimo, maximo) in especificacao.items():
        bruto = parametros.get(nome)
        if isinstance(bruto, bool) or not isinstance(bruto, (int, float)) or not math.isfinite(bruto):
            raise ErroRegra(f"parâmetro {nome} inválido")
        valor = float(bruto)
        if not minimo <= valor <= maximo:
            raise ErroRegra(f"parâmetro {nome}={valor:g} fora de [{minimo}, {maximo}]")
        limpos[nome] = int(valor) if nome in ("n", "minutos") else valor
    direcao = str(hipotese.get("direcao", "")).strip().upper()
    if direcao not in DIRECOES:
        raise ErroRegra("direcao deve ser BUY ou SELL")
    horizonte = hipotese.get("horizonte_minutos")
    if isinstance(horizonte, bool) or not isinstance(horizonte, (int, float)) or not math.isfinite(horizonte):
        raise ErroRegra("horizonte_minutos inválido")
    horizonte = int(horizonte)
    if not 1 <= horizonte <= 60:
        raise ErroRegra("horizonte_minutos deve estar entre 1 e 60")
    justificativa = str(hipotese.get("justificativa", "")).strip()[:500]
    return {"tipo_regra": tipo, "parametros": limpos, "direcao": direcao,
            "horizonte_minutos": horizonte, "justificativa": justificativa}


# ---------------------------------------------------------------------------
# Avaliação determinística das regras sobre candles
# ---------------------------------------------------------------------------

def _closes(candles):
    return [float(c["close"]) for c in candles]


def _amplitudes(candles):
    return [(float(c["high"]) / float(c["low"]) - 1) * 100 if float(c["low"]) > 0 else 0.0
            for c in candles]


def indices_condicao(tipo, parametros, candles):
    """Índices i (usando apenas candles até i) em que a condição é verdadeira."""
    closes = _closes(candles)
    n = len(closes)
    if n < 3:
        return []
    if tipo == "quedas_consecutivas":
        k = int(parametros["n"])
        # "n fechamentos em queda consecutiva" = k closes descendo = k-1
        # comparações; o laço anterior exigia k+1 valores (off-by-one).
        return [i for i in range(k, n)
                if all(closes[i - j] < closes[i - j - 1] for j in range(k - 1))]
    if tipo == "altas_consecutivas":
        k = int(parametros["n"])
        return [i for i in range(k, n)
                if all(closes[i - j] > closes[i - j - 1] for j in range(k - 1))]
    if tipo in ("queda_pct", "alta_pct"):
        m = int(parametros["minutos"])
        pct = float(parametros["pct"])
        alvo = motor_matematico._janela_maxima(closes, m) if tipo == "queda_pct" \
            else motor_matematico._janela_minima(closes, m)
        if tipo == "queda_pct":
            return [i for i in range(m, n) if closes[i] <= alvo[i] * (1 - pct / 100)]
        return [i for i in range(m, n) if closes[i] >= alvo[i] * (1 + pct / 100)]
    if tipo == "amplitude_1min_pct":
        pct = float(parametros["pct"])
        amplitudes = _amplitudes(candles)
        return [i for i in range(n) if amplitudes[i] >= pct]
    if tipo == "movimento_pct":
        m = int(parametros["minutos"])
        pct = float(parametros["pct"])
        return [i for i in range(m, n) if abs(closes[i] / closes[i - m] - 1) * 100 >= pct]
    raise ErroRegra(f"tipo_regra desconhecido: {tipo}")


def condicao_atual(padrao, candles):
    """A condição do padrão é verdadeira no candle mais recente fechado?"""
    tipo = padrao["tipo_regra"] if "tipo_regra" in padrao else padrao["regra"]["tipo"]
    parametros = padrao.get("parametros") or padrao["regra"]["parametros"]
    indices = indices_condicao(tipo, parametros, candles)
    return bool(indices) and indices[-1] == len(candles) - 1


def _resultado(indices, direcao, horizonte, candles, custo_pct):
    """Resultado de cada ocorrência líquida de custo, no horizonte fixado."""
    closes = _closes(candles)
    liquidos = []
    for i in indices:
        j = i + horizonte
        if j >= len(closes):
            break  # sem futuro observado: não extrapola
        entrada, saida = closes[i], closes[j]
        bruto = (saida / entrada - 1) if direcao == "BUY" else (entrada / saida - 1)
        liquidos.append(bruto * 100 - custo_pct)
    if not liquidos:
        return {"eventos": 0, "acertos": 0, "taxa": None, "media_pct": None,
                "mediana_pct": None, "desvio_pct": None}
    media = sum(liquidos) / len(liquidos)
    return {"eventos": len(liquidos), "acertos": sum(v > 0 for v in liquidos),
            "taxa": round(sum(v > 0 for v in liquidos) / len(liquidos), 4),
            "media_pct": round(media, 4),
            "mediana_pct": round(motor_matematico.percentil(liquidos, .5), 4),
            "desvio_pct": round(math.sqrt(sum((v - media) ** 2 for v in liquidos) / len(liquidos)), 4)}


def calibracao_brier(trades_do_padrao, minimo=20):
    """Calibração empírica das probabilidades do JEv para um padrão.

    Brier score = média de (probabilidade_prevista - resultado)² sobre
    trades paper FECHADOS do padrão (resultado = 1 se lucro, 0 se
    prejuízo). Amostra < mínimo => marcada como nao_calibrada: a
    probabilidade do modelo não é tratada como probabilidade de lucro
    sem evidência local.
    """
    pares = []
    for trade in trades_do_padrao:
        probabilidade = trade.get("probability")
        lucro = trade.get("profit")
        if not isinstance(probabilidade, (int, float)) or \
                not isinstance(lucro, (int, float)) or \
                trade.get("status") != "CLOSED":
            continue
        pares.append((probabilidade, 1.0 if lucro > 0 else 0.0))
    n = len(pares)
    if n < minimo:
        return {"n": n, "status": "nao_calibrada"}
    brier = sum((p - o) ** 2 for p, o in pares) / n
    return {"n": n, "status": "calibrada", "brier": round(brier, 4)}

def wilson_inferior(acertos, n, z=1.96):
    """Limite inferior do intervalo de confiança de Wilson para a taxa."""
    if n == 0:
        return 0.0
    p = acertos / n
    denominador = 1 + z * z / n
    centro = p + z * z / (2 * n)
    margem = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (centro - margem) / denominador


def validar(regra, candles, custo_pct):
    """Reproduz a regra em Python e decide candidato/validado/rejeitado.

    `custo_pct` é o custo de ida e volta em % do preço (spread observado).
    O corte fora da amostra é cronológico: os últimos 30% dos eventos
    jamais participam da decisão de descoberta, apenas da verificação.
    """
    tipo = regra["tipo_regra"]
    horizonte = regra["horizonte_minutos"]
    brutos = indices_condicao(tipo, regra["parametros"], candles)
    # Eventos sobrepostos: eventos dependentes (a menos de h candles) contam
    # uma vez; evita que sequências correlacionadas inflem a amostra.
    indices = []
    for i in brutos:
        if not indices or i - indices[-1] > horizonte:
            indices.append(i)
    total = _resultado(indices, regra["direcao"], horizonte, candles, custo_pct)
    corte = int(len(candles) * motor_matematico.CORTE_OOS)
    dentro = [i for i in indices if i < corte]
    fora = [i for i in indices if i >= corte]
    amostra = _resultado(dentro, regra["direcao"], horizonte, candles, custo_pct)
    oos = _resultado(fora, regra["direcao"], horizonte, candles, custo_pct)
    # Estabilidade por subperíodo: metades cronológicas do fora-da-amostra.
    meio = len(fora) // 2
    oos_1 = _resultado(fora[:meio], regra["direcao"], horizonte, candles, custo_pct)
    oos_2 = _resultado(fora[meio:], regra["direcao"], horizonte, candles, custo_pct)
    estabilidade = {"oos_primeira_metade": oos_1, "oos_segunda_metade": oos_2}
    if total["eventos"] == 0:
        return {"status": "rejeitado", "motivo": "Sem ocorrências na janela",
                "total": total, "amostra": amostra, "oos": oos,
                "estabilidade": estabilidade, "custo_pct": custo_pct,
                "wilson_inferior": None}
    if total["eventos"] < MIN_EVENTOS or oos["eventos"] < MIN_EVENTOS_OOS:
        return {"status": "rejeitado", "motivo": "Amostra insuficiente",
                "total": total, "amostra": amostra, "oos": oos,
                "estabilidade": estabilidade, "custo_pct": custo_pct,
                "wilson_inferior": round(wilson_inferior(total["acertos"], total["eventos"]), 4)}
    aprovado = (amostra["taxa"] >= MIN_TAXA and oos["taxa"] >= MIN_TAXA and total["media_pct"] > 0)
    motivo = ("Evidência dentro e fora da amostra" if aprovado
              else "Taxa/expectativa abaixo do mínimo fora da amostra")
    return {"status": "validado" if aprovado else "rejeitado", "motivo": motivo,
            "total": total, "amostra": amostra, "oos": oos, "estabilidade": estabilidade,
            "custo_pct": custo_pct,
            "wilson_inferior": round(wilson_inferior(total["acertos"], total["eventos"]), 4)}


# ---------------------------------------------------------------------------
# Contrato DeepSeek → JEv (payload versionado e validado por esquema)
# ---------------------------------------------------------------------------

SCHEMA_CONTRATO = 1
CAMPOS_CONTRATO = ("schema_version", "analysis_id", "symbol", "timeframe",
                   "window_start", "window_end", "pattern_id", "hypothesis",
                   "validation_status", "sample_size", "out_of_sample_metrics",
                   "expected_net_return", "direction", "reference_price",
                   "expiry", "risk_context", "evidence_hash")


def contrato_oportunidade(padrao, candles, analysis_id, risk_context, agora=None):
    """Monta o payload versionado entregue ao JEv com uma oportunidade validada.

    Todos os campos derivam de dados calculados em Python (padrão validado
    no repositório + janela atual). `evidence_hash` amarra o payload às
    evidências: hash do JSON canônico do padrão + janela + métricas — um
    recálculo divergente invalida o contrato antes do JEv.
    """
    regra = padrao["regra"]
    close_referencia = float(candles[-1]["close"])
    eventos = padrao.get("eventos") or {}
    fora = padrao.get("fora_amostra") or {}
    evidencia = {
        "pattern_id": padrao["id"], "chave": padrao.get("chave"),
        "algoritmo_versao": padrao.get("algoritmo_versao", ALGORITMO_VERSAO),
        "janela": [padrao.get("janela_inicio"), padrao.get("janela_fim")],
        "eventos": eventos, "fora_amostra": fora,
        "custo_spread_pct": padrao.get("custo_spread_pct"),
        "wilson_inferior": padrao.get("wilson_inferior"),
        "reference_price": close_referencia,
    }
    canônico = json.dumps(evidencia, sort_keys=True, allow_nan=False)
    validade = padrao.get("validado_em", 0) + VALIDADE_DIAS * 86400
    return {
        "schema_version": SCHEMA_CONTRATO,
        "analysis_id": analysis_id,
        "symbol": padrao["simbolo"],
        "timeframe": "M1",
        "window_start": padrao.get("janela_inicio"),
        "window_end": padrao.get("janela_fim"),
        "pattern_id": padrao["id"],
        "hypothesis": (f"{regra['tipo']} {regra['parametros']} → {regra['direcao']} "
                       f"em {regra['horizonte_minutos']}min"),
        "validation_status": "VALIDADO",
        "sample_size": eventos.get("eventos", 0),
        "out_of_sample_metrics": fora,
        "expected_net_return": eventos.get("media_pct", 0.0),
        "direction": regra["direcao"],
        "reference_price": close_referencia,
        "expiry": validade,
        "risk_context": risk_context,
        "evidence_hash": hashlib.sha1(canônico.encode("utf-8")).hexdigest(),
    }


def validar_contrato(payload, agora=None):
    """Esquema do contrato: campos obrigatórios, coerência e vigência.

    Devolve (ok, motivo). Falha fechada: contrato inválido nunca chega ao
    JEv nem a uma entrada paper.
    """
    agora = time.time() if agora is None else agora
    if not isinstance(payload, dict):
        return False, "contrato não é um objeto"
    faltando = [c for c in CAMPOS_CONTRATO if c not in payload]
    if faltando:
        return False, f"campos ausentes: {', '.join(faltando)}"
    if payload["schema_version"] != SCHEMA_CONTRATO:
        return False, f"schema_version incompatível: {payload['schema_version']}"
    if payload["timeframe"] != "M1":
        return False, f"timeframe deve ser M1: {payload['timeframe']}"
    if payload["validation_status"] != "VALIDADO":
        return False, f"status não é VALIDADO: {payload['validation_status']}"
    if payload["direction"] not in DIRECOES:
        return False, f"direção inválida: {payload['direction']}"
    if not isinstance(payload["sample_size"], int) or payload["sample_size"] < MIN_EVENTOS:
        return False, f"amostra insuficiente no contrato: {payload['sample_size']}"
    if not isinstance(payload["expected_net_return"], (int, float)) or             payload["expected_net_return"] <= 0:
        return False, "expectativa líquida deve ser positiva"
    if not isinstance(payload["expiry"], (int, float)) or payload["expiry"] < agora:
        return False, "padrão expirado no contrato"
    if payload["window_start"] is None or payload["window_end"] is None or             payload["window_end"] <= payload["window_start"]:
        return False, "janela inválida no contrato"
    if not isinstance(payload["evidence_hash"], str) or len(payload["evidence_hash"]) != 40:
        return False, "evidence_hash inválido"
    risco = payload["risk_context"]
    if not isinstance(risco, dict) or not isinstance(risco.get("equity"), (int, float)):
        return False, "risk_context ausente no contrato"
    return True, ""


# ---------------------------------------------------------------------------
# Repositório persistente por ativo
# ---------------------------------------------------------------------------

class RepositorioPadroes:
    """JSON por ativo com escrita atômica, deduplicação e versionamento."""

    def __init__(self, pasta, relogio=time.time):
        self.pasta = Path(pasta)
        self.relogio = relogio
        self.pasta.mkdir(parents=True, exist_ok=True)

    def caminho(self, simbolo):
        return self.pasta / f"{simbolo.upper()}.json"

    def carregar(self, simbolo):
        """Lê o arquivo do ativo; corrige versão antiga sem perder registros."""
        caminho = self.caminho(simbolo)
        if not caminho.exists():
            return {"schema_version": SCHEMA, "simbolo": simbolo.upper(), "padroes": []}
        try:
            dados = json.loads(caminho.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            copia = caminho.with_suffix(".json.corrupto")
            try:
                caminho.replace(copia)
            except OSError:
                pass
            return {"schema_version": SCHEMA, "simbolo": simbolo.upper(), "padroes": []}
        if not isinstance(dados, dict) or not isinstance(dados.get("padroes"), list):
            return {"schema_version": SCHEMA, "simbolo": simbolo.upper(), "padroes": []}
        if dados.get("schema_version", 0) < SCHEMA:
            dados["schema_version"] = SCHEMA
        return dados

    def salvar(self, simbolo, dados):
        """Escrita atômica idêntica ao padrão do db.json."""
        caminho = self.caminho(simbolo)
        conteudo = json.dumps(dados, indent=1, allow_nan=False)
        temporario = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=caminho.parent,
                                             prefix=caminho.name + ".", suffix=".tmp", delete=False) as saida:
                temporario = Path(saida.name)
                saida.write(conteudo)
                saida.flush()
                os.fsync(saida.fileno())
            for tentativa in range(5):
                try:
                    temporario.replace(caminho)
                    break
                except PermissionError:
                    if tentativa == 4:
                        raise
                    time.sleep(.05 * (tentativa + 1))
        finally:
            if temporario is not None:
                temporario.unlink(missing_ok=True)

    def registrar(self, simbolo, regra, validacao, origem, janela_info):
        """Insere ou atualiza um padrão com o resultado da validação local.

        Deduplica pela chave canônica. Retorna o registro persistido.
        """
        dados = self.carregar(simbolo)
        tipo, parametros = regra["tipo_regra"], regra["parametros"]
        chave = chave_regra(tipo, parametros, regra["direcao"], regra["horizonte_minutos"])
        identificador = id_regra(chave)
        agora = self.relogio()
        registro = next((p for p in dados["padroes"] if p.get("id") == identificador), None)
        if registro is None:
            registro = {"id": identificador, "chave": chave, "simbolo": simbolo.upper(),
                        "schema_version": SCHEMA, "algoritmo_versao": ALGORITMO_VERSAO,
                        "origem": origem, "criado_em": agora,
                        "regra": {"tipo": tipo, "parametros": parametros,
                                  "direcao": regra["direcao"],
                                  "horizonte_minutos": regra["horizonte_minutos"]},
                        "justificativa": regra.get("justificativa", ""),
                        "historico_status": []}
            dados["padroes"].append(registro)
        else:
            registro["origem"] = registro.get("origem") or origem
            registro["justificativa"] = regra.get("justificativa", "") or registro.get("justificativa", "")
        registro.update({
            "status": validacao["status"], "motivo_status": validacao["motivo"],
            "janela_inicio": janela_info["inicio"], "janela_fim": janela_info["fim"],
            "janela_dias": janela_info.get("dias"), "amostra_candles": janela_info.get("candles"),
            "eventos": validacao["total"], "amostra": validacao["amostra"],
            "fora_amostra": validacao["oos"], "wilson_inferior": validacao["wilson_inferior"],
            "custo_spread_pct": validacao.get("custo_pct"),
            "validado_em": agora,
        })
        registro["historico_status"].append({"em": agora, "status": validacao["status"],
                                             "motivo": validacao["motivo"]})
        registro["historico_status"] = registro["historico_status"][-20:]
        dados["atualizado_em"] = agora
        self._podar(dados)
        self.salvar(simbolo, dados)
        return registro

    @staticmethod
    def _podar(dados):
        """Mantém no máximo MAX_PADROES_ATIVO por ativo, descartando rejeitados antigos."""
        if len(dados["padroes"]) <= MAX_PADROES_ATIVO:
            return
        dados["padroes"].sort(key=lambda p: (p.get("status") == "rejeitado",
                                             -(p.get("validado_em") or 0)))
        dados["padroes"] = dados["padroes"][:MAX_PADROES_ATIVO]

    def expirar_antigos(self, simbolo):
        """Marca validados cujo reexame passou da validade como expirados."""
        dados = self.carregar(simbolo)
        agora = self.relogio()
        alterou = False
        for registro in dados["padroes"]:
            if registro.get("status") == "validado" and \
                    agora - registro.get("validado_em", 0) > VALIDADE_DIAS * 86400:
                registro["status"] = "expirado"
                registro["motivo_status"] = "Validação mais antiga que a validade; reexamine"
                registro["historico_status"].append({"em": agora, "status": "expirado",
                                                     "motivo": registro["motivo_status"]})
                alterou = True
        if alterou:
            self.salvar(simbolo, dados)
        return alterou

    def validados(self, simbolo):
        """Padrões com evidência atual; já remove expirações vencidas."""
        self.expirar_antigos(simbolo)
        dados = self.carregar(simbolo)
        return [p for p in dados["padroes"] if p.get("status") == "validado"]

    def padroes_ativos(self, simbolo):
        """Validados e candidatos, para revisão pelo DeepSeek."""
        self.expirar_antigos(simbolo)
        dados = self.carregar(simbolo)
        return [p for p in dados["padroes"] if p.get("status") in ("validado", "candidato")]

    def aplicar_revisoes(self, simbolo, revisoes):
        """Aplica sugestões de revisão do DeepSeek como rascunho de status.

        Nenhuma sugestão do modelo decide sozinha: um padrão só muda para
        'expirado' por sugestão quando a validação local atual também não
        sustenta mais o status; 'manter' não altera nada sem revalidação.
        """
        dados = self.carregar(simbolo)
        aplicadas = []
        for revisao in revisoes:
            identificador = str(revisao.get("id", ""))[:12]
            acao = str(revisao.get("acao", "")).strip().lower()
            registro = next((p for p in dados["padroes"] if p.get("id") == identificador), None)
            if registro is None or acao != "expirar":
                continue
            if registro.get("status") == "validado":
                registro["status"] = "expirado"
                registro["motivo_status"] = f"Sugerido expirar pelo DeepSeek: {str(revisao.get('motivo', ''))[:200]}"
                registro["historico_status"].append({"em": self.relogio(), "status": "expirado",
                                                     "motivo": registro["motivo_status"]})
                aplicadas.append(identificador)
        if aplicadas:
            dados["atualizado_em"] = self.relogio()
            self.salvar(simbolo, dados)
        return aplicadas
