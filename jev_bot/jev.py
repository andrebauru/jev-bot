"""Decisões externas JEV; sem motor offline ou fallback fictício."""

import json
import math
import os
import re
import socket
import time
from dataclasses import asdict
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .types import ACTIONS, Decision, MarketState
from .configuracao import carregar_ambiente
from .risk import Limits


class JevError(RuntimeError):
    """Erro externo tipado, sem credenciais ou corpo de resposta sensível."""

    def __init__(self, code, stage, http_success=False, http_status=None,
                 error_type=None, error_code=None, error_message=None):
        self.code = code
        self.stage = stage
        self.http_success = http_success
        self.http_status = http_status
        self.error_type = error_type
        self.error_code = error_code
        self.error_message = error_message
        super().__init__(code)

    def resumo(self):
        """Descrição sanitizada e curta para logs/interface."""
        partes = [self.code]
        if self.error_type:
            partes.append(f"type={self.error_type}")
        if self.error_code:
            partes.append(f"code={self.error_code}")
        if self.error_message:
            partes.append(self.error_message)
        return " | ".join(partes)


_PADROES_SEGREDO = (
    (re.compile(r"(?i)bearer\s+\S+"), "Bearer ***"),
    (re.compile(r"sk-or-[A-Za-z0-9_-]{8,}"), "sk-or-***"),
    (re.compile(r"(?i)(api[_-]?key|authorization|token)\W*\S+"), r"\1=***"),
)


def sanitizar_texto(texto, limite=300):
    """Remove segredos e limita o comprimento de mensagens externas."""
    if texto is None:
        return None
    resultado = str(texto)
    for padrao, substituicao in _PADROES_SEGREDO:
        resultado = padrao.sub(substituicao, resultado)
    resultado = " ".join(resultado.split())
    return resultado[:limite] if resultado else None


CAMPOS_PROPOSTA_OBRIGATORIOS = ("symbol", "side_proposed", "entry_reference", "stop_loss",
                                "take_profit", "volume", "risk_amount", "risk_pct",
                                "candle_ref", "request_id", "run_id")


def validar_proposta(proposta):
    """Validação local antes do POST: nada inválido sai para a rede.

    Rejeita campo essencial ausente/None, direção fora de BUY/SELL e valores
    não serializáveis (NaN/Infinity/objetos). Nunca converte None em zero.
    """
    if not isinstance(proposta, dict):
        raise JevError("JEv_PAYLOAD_INVALID", "payload",
                       error_message="payload não é um objeto")
    for campo in CAMPOS_PROPOSTA_OBRIGATORIOS:
        if campo not in proposta or proposta[campo] is None:
            raise JevError("JEv_PAYLOAD_INVALID", "payload",
                           error_message=f"campo essencial ausente: {campo}")
    if proposta["side_proposed"] not in ("BUY", "SELL"):
        raise JevError("JEv_PAYLOAD_INVALID", "payload",
                       error_message="side_proposed inválido")
    try:
        json.dumps(proposta, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise JevError("JEv_PAYLOAD_INVALID", "payload",
                       error_message=f"não serializável: {type(exc).__name__}") from None
    return proposta



def configuracao():
    """Lê a chave OpenRouter e restringe seu envio à API oficial de decisões."""
    carregar_ambiente()
    fornecedor = os.environ.get("AI_PROVIDER", "openrouter").strip().lower()
    if fornecedor != "openrouter":
        raise RuntimeError("AI_PROVIDER deve ser openrouter para usar o modelo Jev.")
    chave = os.environ.get("OPENROUTER_API_KEY", "").strip()
    endpoint = os.environ.get("OPENROUTER_API_ENDPOINT", "https://openrouter.ai/api/alpha/decisions").strip()
    if not chave:
        raise RuntimeError("Preencha OPENROUTER_API_KEY no .env do projeto. scan consulta o MT5 sem JEV.")
    url = urlparse(endpoint)
    if (url.scheme != "https" or url.hostname != "openrouter.ai" or url.username or url.password
            or url.port not in (None, 443) or url.path != "/api/alpha/decisions" or url.query or url.fragment):
        raise RuntimeError("OPENROUTER_API_ENDPOINT deve usar https://openrouter.ai/api/alpha/decisions.")
    modelo = os.environ.get("OPENROUTER_MODEL", "typesafe/jev-1.13").strip()
    if not (modelo.startswith("typesafe/jev-") or modelo == "~typesafe/jev-latest"):
        raise RuntimeError("OPENROUTER_MODEL deve identificar o modelo Jev da TypeSafe.")
    return chave, endpoint


def _erro_http(exc, http_success=False):
    """Extrai status e mensagem do erro sanitizados, sem expor segredos."""
    status = getattr(exc, "code", None)
    corpo = ""
    try:
        corpo = exc.read(4000)
        if isinstance(corpo, bytes):
            corpo = corpo.decode("utf-8", errors="replace")
    except Exception:
        corpo = ""
    try:
        exc.close()
    except Exception:
        pass
    tipo = codigo = mensagem = None
    try:
        dados = json.loads(corpo) if corpo else {}
        erro = dados.get("error") if isinstance(dados, dict) else None
        if isinstance(erro, dict):
            tipo = sanitizar_texto(erro.get("type"), 80)
            codigo = erro.get("code")
            mensagem = sanitizar_texto(erro.get("message"), 300)
        elif isinstance(dados, dict) and dados.get("message"):
            mensagem = sanitizar_texto(dados.get("message"), 300)
    except (ValueError, TypeError):
        mensagem = sanitizar_texto(corpo, 200)
    return JevError(f"HTTP_{status}", "http_error", http_success=http_success,
                    http_status=status, error_type=tipo, error_code=codigo,
                    error_message=mensagem)


def avaliar_entrada(proposta, engine="jev") -> dict:
    """Aprovação binária de uma entrada já decidida e pré-aprovada pelo Python.

    O Python mantém a direção fixa (BUY/SELL) e todos os parâmetros; o JEv só
    aprova ou reprova. A resposta é normalizada para `{"approved": bool,
    "reason": str}`. O envelope da requisição segue o contrato documentado do
    endpoint (`questions.action`, `type: "choice"`, critérios YES/NO). Resposta
    ambígua, legado BUY/SELL/HOLD, JSON inválido, timeout ou HTTP de erro viram
    `JevError` — nunca aprovação implícita.
    """
    if engine != "jev":
        raise ValueError("Somente o motor JEV é permitido.")
    validar_proposta(proposta)
    inicio = time.monotonic()
    modelo = os.environ.get("OPENROUTER_MODEL", "typesafe/jev-1.13").strip()
    chave, endpoint = configuracao()
    side = proposta["side_proposed"]
    body = json.dumps({"model": modelo,
                       "state": proposta,
                       "questions": {"action": {
        "type": "choice",
        "instructions": "Avalie exclusivamente se esta entrada proposta merece aprovação. "
                        f"A direção é fixa ({side}) e definida pelo sistema; você NÃO pode propor "
                        "a direção oposta nem alterar direção, lote, stop, take-profit ou risco. "
                        "Responda YES para aprovar ou NO para reprovar. "
                        "Se faltar informação suficiente, responda NO.",
        "criteria": {
            "YES": "A evidência quantitativa fornecida apoia a entrada proposta.",
            "NO": "A evidência é insuficiente, contraditória ou não apoia a entrada proposta.",
        },
    }}}, allow_nan=False).encode()
    req = Request(endpoint, data=body, headers={
        "content-type": "application/json", "authorization": f"Bearer {chave}",
    })
    http_success = False
    try:
        with urlopen(req, timeout=15) as resposta:
            corpo = resposta.read(1_000_000).decode()
        http_success = True
        aprovado, motivo = _interpretar_resposta(json.loads(corpo))
    except HTTPError as exc:
        raise _erro_http(exc) from None
    except (TimeoutError, socket.timeout):
        raise JevError("JEv_TIMEOUT", "timeout") from None
    except URLError as exc:
        if isinstance(getattr(exc, "reason", None), (TimeoutError, socket.timeout)):
            raise JevError("JEv_TIMEOUT", "timeout") from None
        raise JevError("JEv_NETWORK_ERROR", "http_error") from None
    except (ValueError, KeyError, TypeError, AttributeError):
        raise JevError("JEv_PARSE_ERROR", "parse_error", http_success=http_success) from None
    return {"approved": bool(aprovado), "reason": motivo[:400],
            "modelo": modelo, "latencia_ms": round((time.monotonic() - inicio) * 1000)}


def _interpretar_resposta(dados):
    """Aceita apenas aprovação binária; devolve (approved, reason)."""
    if not isinstance(dados, dict):
        raise ValueError("resposta não é objeto")
    answers = dados.get("answers")
    if not isinstance(answers, dict):
        raise ValueError("respostas ausentes")
    if "action" in answers and isinstance(answers["action"], dict):
        ans = answers["action"]
        escolha = ans.get("choice")
        if ans.get("type") == "choice" and escolha in ("YES", "NO"):
            aprovado = escolha == "YES"
            motivo = ans.get("reason") or ans.get("rationale")
            if not (isinstance(motivo, str) and motivo.strip()):
                motivo = "aprovado pelo JEv" if aprovado else "rejeitado pelo JEv"
            return aprovado, str(motivo).strip()
    if "approval" in answers and isinstance(answers["approval"], dict):
        ans = answers["approval"]
        aprovado = ans.get("approved")
        motivo = ans.get("reason")
        if isinstance(aprovado, bool) and isinstance(motivo, str) and motivo.strip():
            return aprovado, motivo.strip()
    raise ValueError("resposta não binária")


def decide(state: MarketState, engine="jev", limites=None) -> Decision:
    """Consulta JEV e rejeita respostas incompletas ou fora do contrato esperado."""
    if engine != "jev":
        raise ValueError("Somente o motor JEV é permitido.")
    inicio = time.monotonic()
    modelo = os.environ.get("OPENROUTER_MODEL", "typesafe/jev-1.13").strip()
    chave, endpoint = configuracao()
    limites = limites or Limits()
    contexto = asdict(state)
    contexto["historico_coletado_candles"] = len(state.candles)
    contexto["candles"] = contexto["candles"][-20:]
    contexto["custos_observados"] = {"spread_fracao_preco": (state.ask - state.bid) / state.price,
        "spread_fracao_stop": (state.ask - state.bid) / state.stop_distance if state.stop_distance else None}
    contexto["regras_avaliacao"] = {"horizonte_minutos": 5, "modo": "paper",
        "capital_inicial_jpy": 10000, "limite_spread_fracao_preco": limites.max_spread_fraction,
        "limite_spread_fracao_stop": limites.max_spread_stop}
    body = json.dumps({"model": modelo,
                       "state": contexto, "questions": {"action": {
        "type": "choice",
        "instructions": "Evaluate only the supplied real MT5 market snapshot. Choose a trading action. "
                        "Use the mathematics summaries to assess short M1 entries, with a maximum five-minute paper holding time. "
                        "Evaluate BUY and SELL on their evidence; HOLD is appropriate when neither direction is supported. "
                        "Historical amplitude weights are descriptive, not profit probabilities. "
                        "Compare the 5/10/20 candle slopes, their agreement and observed spread. "
                        "Treat linear five-minute displacement as a hypothetical scenario, never a forecast. "
                        "A valid paper directional setup may be BUY or SELL; do not demand certainty. "
                        "Do not invent news, liquidity, forecasts or unavailable data.",
        "criteria": {
            "BUY": "Evidence in the supplied closed candles supports a long entry despite the spread and stop distance.",
            "SELL": "Evidence in the supplied closed candles supports a short entry despite the spread and stop distance.",
            "HOLD": "No sufficiently supported directional entry; continue monitoring.",
            "AVOID": "Supplied data or trading conditions make an entry unsuitable.",
        },
    }}}, allow_nan=False).encode()
    req = Request(endpoint, data=body, headers={
        "content-type": "application/json", "authorization": f"Bearer {chave}",
    })
    http_success = False
    try:
        with urlopen(req, timeout=15) as resposta:
            corpo = resposta.read(1_000_000).decode()
        http_success = True
        data = json.loads(corpo)
        ans = data["answers"]["action"]
        acao = ans["choice"]
        if ans["type"] != "choice" or acao not in ACTIONS:
            raise ValueError("Resposta inválida")
        probabilidades = {acao: float(ans["probabilities"][acao]) for acao in ACTIONS}
        if (any(not math.isfinite(v) or not 0 <= v <= 1 for v in probabilidades.values())
                or not math.isclose(sum(probabilidades.values()), 1, abs_tol=.02)):
            raise ValueError("Distribuição inválida")
        probabilidade = probabilidades[acao]
        confianca = float(ans["confidence"])
        if acao not in ACTIONS or any(not math.isfinite(v) or not 0 <= v <= 1
                                    for v in (probabilidade, confianca)):
            raise ValueError("Resposta inválida")
    except HTTPError as exc:
        code = exc.code
        exc.close()
        raise JevError(f"HTTP_{code}", "http_error") from None
    except (TimeoutError, socket.timeout):
        raise JevError("JEv_TIMEOUT", "timeout") from None
    except URLError as exc:
        if isinstance(getattr(exc, "reason", None), (TimeoutError, socket.timeout)):
            raise JevError("JEv_TIMEOUT", "timeout") from None
        raise JevError("JEv_NETWORK_ERROR", "http_error") from None
    except (ValueError, KeyError, TypeError, AttributeError):
        raise JevError("JEv_PARSE_ERROR", "parse_error", http_success=http_success) from None
    return Decision(state.symbol, acao, probabilidade, confianca, "jev", {
        "engine": "openrouter-jev", "modelo": modelo,
        "latencia_ms": round((time.monotonic() - inicio) * 1000),
        # Probabilidade do modelo NÃO é probabilidade de lucro: sem amostra
        # empírica suficiente por ativo/condição, marcar como não calibrada.
        "calibracao": "nao_calibrada"})
