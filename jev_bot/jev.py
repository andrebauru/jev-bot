"""Decisões externas JEV; sem motor offline ou fallback fictício."""

import json
import math
import os
import time
from dataclasses import asdict
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .types import ACTIONS, Decision, MarketState
from .configuracao import carregar_ambiente
from .risk import Limits


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
    try:
        with urlopen(req, timeout=15) as resposta:
            data = json.loads(resposta.read(1_000_000).decode())
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
        raise RuntimeError(f"Jev/OpenRouter HTTP {exc.code}; confira chave, saldo e acesso ao modelo.") from None
    except (URLError, ValueError, KeyError, TypeError, AttributeError, TimeoutError):
        raise RuntimeError("JEV indisponível ou resposta inválida; execução bloqueada.") from None
    return Decision(state.symbol, acao, probabilidade, confianca, "jev", {
        "engine": "openrouter-jev", "modelo": modelo,
        "latencia_ms": round((time.monotonic() - inicio) * 1000),
        # Probabilidade do modelo NÃO é probabilidade de lucro: sem amostra
        # empírica suficiente por ativo/condição, marcar como não calibrada.
        "calibracao": "nao_calibrada"})
