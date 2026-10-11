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

from .. import configuracao, deepseek

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
    """Testa chave e modelo na API pública/privada do OpenRouter."""
    return deepseek.testar_conexao(chave=chave, modelo=modelo)
