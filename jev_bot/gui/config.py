"""Configuração segura na interface: cofre do sistema com fallback ao .env.

A chave OpenRouter e as credenciais MT5 são gravadas no cofre do Windows
(keyring) quando disponível; sem keyring, o fallback é o .env existente,
com aviso explícito na interface. Nenhum valor sensível é exibido ou
registrado em log. Testes de conexão rodam em processo separado.
"""

from __future__ import annotations

import json
import subprocess
import sys

from .. import configuracao

SERVICO = configuracao.SERVICO_KEYRING


def _keyring():
    """Retorna o módulo keyring ou None, sem interromper a UI."""
    try:
        import keyring
        return keyring
    except Exception:
        return None


def fonte_chave_openrouter():
    """De onde vem a chave hoje: cofre, .env ou não configurada."""
    import os
    if os.environ.get("OPENROUTER_API_KEY_STORE", "").strip() == configuracao.MARCA_KEYRING:
        return "cofre do sistema (keyring)"
    if os.environ.get("OPENROUTER_API_KEY", "").strip():
        return "arquivo .env"
    return "não configurada"


def salvar_chave_openrouter(chave):
    """Grava a chave preferindo o cofre do sistema; nunca retorna o valor."""
    cofre = _keyring()
    if cofre is not None and chave:
        cofre.set_password(SERVICO, "openrouter_api_key", chave)
        configuracao.salvar_variaveis({"OPENROUTER_API_KEY": None,
                                       "OPENROUTER_API_KEY_STORE": configuracao.MARCA_KEYRING})
        os_recarregar_chave()
        return "Chave gravada no cofre do sistema (keyring)."
    if cofre is not None and chave is None:
        cofre.delete_password(SERVICO, "openrouter_api_key")
        configuracao.salvar_variaveis({"OPENROUTER_API_KEY_STORE": None})
        return "Chave removida do cofre."
    if not chave:
        configuracao.salvar_variaveis({"OPENROUTER_API_KEY": None,
                                       "OPENROUTER_API_KEY_STORE": None})
        return "Chave removida do .env."
    configuracao.salvar_variaveis({"OPENROUTER_API_KEY": chave,
                                   "OPENROUTER_API_KEY_STORE": None})
    return "ATENÇÃO: keyring indisponível; chave gravada no .env em texto puro."


def os_recarregar_chave():
    """Repõe a chave do cofre no ambiente do processo atual."""
    import os
    os.environ.pop("OPENROUTER_API_KEY", None)
    configuracao.carregar_ambiente()


def credenciais_mt5_salvas():
    """Indica onde estão login/senha MT5 (cofre, .env ou ausentes)."""
    import os
    if os.environ.get("MT5_CREDS_STORE", "").strip() == configuracao.MARCA_KEYRING:
        return "cofre do sistema (keyring)"
    if os.environ.get("MT5_LOGIN", "").strip() or os.environ.get("MT5_PASSWORD", "").strip():
        return "arquivo .env"
    return "não configuradas"


def salvar_credenciais_mt5(login, senha):
    """Grava login/senha MT5 preferindo o cofre; campos são reservados."""
    cofre = _keyring()
    if cofre is not None:
        if login:
            cofre.set_password(SERVICO, "mt5_login", login)
        if senha:
            cofre.set_password(SERVICO, "mt5_password", senha)
        configuracao.salvar_variaveis({"MT5_CREDS_STORE": configuracao.MARCA_KEYRING})
        return "Credenciais MT5 gravadas no cofre do sistema (reservadas para integração futura)."
    configuracao.salvar_variaveis({"MT5_LOGIN": login or None, "MT5_PASSWORD": senha or None,
                                   "MT5_CREDS_STORE": None})
    return "ATENÇÃO: keyring indisponível; credenciais MT5 gravadas no .env em texto puro."


def carregar_credenciais_mt5():
    """Recupera login/senha do cofre quando marcado, para preencher a UI."""
    cofre = _keyring()
    if cofre is None:
        return "", ""
    import os
    if os.environ.get("MT5_CREDS_STORE", "").strip() != configuracao.MARCA_KEYRING:
        return "", ""
    try:
        login = cofre.get_password(SERVICO, "mt5_login") or ""
        senha = cofre.get_password(SERVICO, "mt5_password") or ""
        return login, senha
    except Exception:
        return "", ""


_SCRIPT_MT5 = r"""
import json
r = {"ok": False}
try:
    import MetaTrader5 as mt5
    ok = mt5.initialize(r"CAMINHO")
    r["inicializado"] = bool(ok)
    t = mt5.terminal_info()
    a = mt5.account_info()
    r["conectado"] = bool(t and t.connected)
    r["conta"] = a is not None
    r["moeda"] = a.currency if a is not None else None
    mt5.shutdown()
    r["ok"] = bool(r["conectado"] and r["conta"])
except Exception as exc:
    r["erro"] = str(exc)[:200]
print(json.dumps(r))
"""


def testar_mt5(caminho, timeout=60):
    """Testa existência e conexão em subprocesso isolado, sem credenciais."""
    script = _SCRIPT_MT5.replace("r\"CAMINHO\"", repr(caminho))
    try:
        resultado = subprocess.run([sys.executable, "-X", "utf8", "-c", script],
                                   capture_output=True, text=True, timeout=timeout)
        dados = json.loads(resultado.stdout.strip().splitlines()[-1])
        return dados
    except (subprocess.TimeoutExpired, json.JSONDecodeError, IndexError, OSError) as exc:
        return {"ok": False, "erro": f"Falha no teste isolado: {exc}"[:200]}


def testar_openrouter(chave=None, modelo=None):
    """Retém a assinatura antiga sem chamar DeepSeek/OpenRouter."""
    return {"ok": False, "erro": "Teste DeepSeek desativado; nenhuma chamada de rede foi feita."}


JID_DIAGNOSTICO = {
    "symbol": "DIAGNOSTICO", "side_proposed": "BUY", "entry_reference": 1.0,
    "stop_loss": 0.99, "take_profit": 1.01, "volume": 0.01, "risk_amount": 0.01,
    "risk_pct": 0.01, "spread": 0.0001, "candidate_id": "diagnostico",
    "request_id": "diagnostico", "run_id": "diagnostico", "candle_ref": 0,
    "expires_at": 0, "evidencias": {"origem": "teste_local_de_conexao"},
}


def validar_openrouter_local():
    """Diagnóstico local (sem rede): chave, endpoint, modelo e payload binário."""
    import os
    from urllib.parse import urlparse
    from .. import jev as _jev
    etapas = [("provider", os.environ.get("AI_PROVIDER", "openrouter").strip().lower())]
    etapas.append(("chave", "presente" if os.environ.get("OPENROUTER_API_KEY", "").strip()
                   else "ausente"))
    try:
        _chave, endpoint = _jev.configuracao()
        host = urlparse(endpoint).hostname or "?"
        etapas.append(("endpoint", host))
        modelo = os.environ.get("OPENROUTER_MODEL", "typesafe/jev-1.13").strip()
        etapas.append(("modelo", modelo))
        _jev.validar_proposta(dict(JID_DIAGNOSTICO))
        etapas.append(("payload_binario", "ok"))
        return {"ok": True, "status": "PRONTO", "remoto": False, "etapas": etapas,
                "endpoint_host": host, "modelo": modelo}
    except RuntimeError as exc:
        return {"ok": False, "status": "FALHA", "remoto": False, "etapas": etapas,
                "erro": str(exc)[:200]}
    except _jev.JevError as exc:
        return {"ok": False, "status": "FALHA", "remoto": False, "etapas": etapas,
                "erro": exc.resumo()[:200]}


def testar_openrouter_remoto(timeout=20):
    """Teste remoto OPT-IN da aprovação binária. Pode consumir créditos.

    Nunca é chamado automaticamente; o usuário confirma na interface. Não cria
    ordens, não enfileira candidatos e não altera contadores do motor.
    """
    from .. import jev as _jev
    local = validar_openrouter_local()
    if not local["ok"]:
        return local
    try:
        resultado = _jev.avaliar_entrada(dict(JID_DIAGNOSTICO))
    except _jev.JevError as exc:
        return {"ok": False, "status": "FALHA", "remoto": True, "etapas": local["etapas"],
                "endpoint_host": local["endpoint_host"], "modelo": local["modelo"],
                "http_status": exc.http_status, "codigo": exc.code,
                "erro_tipo": exc.error_type, "erro_mensagem": exc.error_message,
                "erro": exc.resumo()[:300]}
    except Exception as exc:  # nunca derruba a GUI por erro inesperado
        return {"ok": False, "status": "FALHA", "remoto": True, "etapas": local["etapas"],
                "endpoint_host": local["endpoint_host"], "modelo": local["modelo"],
                "erro": f"{type(exc).__name__}: {str(exc)[:200]}"}
    return {"ok": True, "status": "OK", "remoto": True, "etapas": local["etapas"],
            "endpoint_host": local["endpoint_host"], "modelo": local["modelo"],
            "latencia_ms": resultado.get("latencia_ms"),
            "resposta": {"approved": resultado["approved"], "reason": resultado["reason"]}}
