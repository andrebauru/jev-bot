"""Carregamento e gravação local de configuração, sem registrar credenciais."""

import math
import os
import tempfile
from pathlib import Path

from dotenv import load_dotenv

CAMINHO_ENV = Path(__file__).resolve().parent.parent / ".env"
TERMINAL_PADRAO = r"C:\Program Files\MetaTrader 5 IC Markets Global\terminal64.exe"
ENDPOINT_JEV_PADRAO = "https://openrouter.ai/api/alpha/decisions"
ENDPOINT_CHAT_PADRAO = "https://openrouter.ai/api/v1/chat/completions"
SERVICO_KEYRING = "jev-bot"
MARCA_KEYRING = "keyring"


def carregar_ambiente():
    """Lê o .env do projeto preservando variáveis já definidas no processo.

    Quando a chave OpenRouter é guardada no cofre do sistema (keyring), é
    restaurada aqui para o processo sem nunca ser gravada em texto puro.
    """
    load_dotenv(CAMINHO_ENV, override=False, interpolate=False)
    if os.environ.get("OPENROUTER_API_KEY", "").strip():
        return
    if os.environ.get("OPENROUTER_API_KEY_STORE", "").strip() == MARCA_KEYRING:
        try:
            import keyring
            chave = keyring.get_password(SERVICO_KEYRING, "openrouter_api_key")
        except Exception:
            return
        if chave:
            os.environ["OPENROUTER_API_KEY"] = chave


def terminal_mt5():
    """Resolve o executável do terminal a partir da configuração local."""
    return os.environ.get("MT5_TERMINAL_PATH") or TERMINAL_PADRAO


def ajuste_horario_mt5():
    """Valida o ajuste do broker sem expor valores sensíveis da configuração."""
    try:
        ajuste = float(os.environ.get("MT5_TIME_OFFSET_HOURS", "3"))
    except ValueError:
        raise RuntimeError("MT5_TIME_OFFSET_HOURS deve ser numérico.") from None
    if not math.isfinite(ajuste) or not -14 <= ajuste <= 14:
        raise RuntimeError("MT5_TIME_OFFSET_HOURS deve estar entre -14 e +14.")
    return ajuste


def modelo_deepseek():
    """Identificador do modelo DeepSeek na conta OpenRouter do operador.

    Não presumimos um ID fixo: o padrão é um alias amplamente disponível,
    editável na aba de configurações e verificado pelo teste de conexão.
    """
    return os.environ.get("OPENROUTER_DEEPSEEK_MODEL", "deepseek/deepseek-chat").strip()


def endpoint_chat():
    """Endpoint de chat completions do OpenRouter, restrito ao domínio oficial."""
    url = (os.environ.get("OPENROUTER_CHAT_ENDPOINT") or ENDPOINT_CHAT_PADRAO).strip()
    if url != ENDPOINT_CHAT_PADRAO:
        raise RuntimeError("OPENROUTER_CHAT_ENDPOINT deve usar https://openrouter.ai/api/v1/chat/completions.")
    return url


def _inteiro(nome, padrao, minimo, maximo):
    """Lê um limite numérico do ambiente com faixa restrita."""
    try:
        valor = int(os.environ.get(nome, str(padrao)))
    except ValueError:
        raise RuntimeError(f"{nome} deve ser inteiro.") from None
    if not minimo <= valor <= maximo:
        raise RuntimeError(f"{nome} deve estar entre {minimo} e {maximo}.")
    return valor


def limite_chamadas_hora():
    """Teto de chamadas DeepSeek por hora; evita gasto descontrolado."""
    return _inteiro("DEEPSEEK_MAX_CHAMADAS_HORA", 20, 1, 1000)


def tokens_por_dia():
    """Teto de tokens DeepSeek por dia (entrada+saída somadas)."""
    return _inteiro("DEEPSEEK_TOKENS_DIA", 300_000, 1000, 100_000_000)


def timeout_deepseek():
    """Timeout em segundos de cada chamada DeepSeek."""
    return _inteiro("DEEPSEEK_TIMEOUT_S", 45, 5, 300)


def _escapar_valor(valor):
    """Envolve o valor em aspas simples quando necessário no .env."""
    texto = str(valor)
    if any(c in texto for c in " \t#=\"'") and not (texto.startswith("'") and texto.endswith("'")):
        return "'" + texto.replace("'", "") + "'"
    return texto


def salvar_variaveis(atualizacoes):
    """Grava variáveis no .env preservando as demais linhas, de forma atômica.

    `atualizacoes` mapeia nome -> valor; valor None remove a linha. Comentários
    e variáveis não tocadas permanecem. Nunca registra os valores em log.
    """
    linhas = []
    if CAMINHO_ENV.exists():
        linhas = CAMINHO_ENV.read_text(encoding="utf-8").splitlines()
    pendentes = dict(atualizacoes)
    novas = []
    for linha in linhas:
        nome = linha.split("=", 1)[0].strip() if "=" in linha and not linha.lstrip().startswith("#") else None
        if nome in pendentes:
            valor = pendentes.pop(nome)
            if valor is not None:
                novas.append(f"{nome}={_escapar_valor(valor)}")
            continue
        novas.append(linha)
    for nome, valor in pendentes.items():
        if valor is not None:
            novas.append(f"{nome}={_escapar_valor(valor)}")
    CAMINHO_ENV.parent.mkdir(parents=True, exist_ok=True)
    conteudo = "\n".join(novas) + ("\n" if novas else "")
    temporario = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=CAMINHO_ENV.parent,
                                         prefix=".env.", suffix=".tmp", delete=False) as saida:
            temporario = Path(saida.name)
            saida.write(conteudo)
            saida.flush()
            os.fsync(saida.fileno())
        temporario.replace(CAMINHO_ENV)
    finally:
        if temporario is not None:
            temporario.unlink(missing_ok=True)
    for nome, valor in atualizacoes.items():
        if valor is None:
            os.environ.pop(nome, None)
        else:
            os.environ[nome] = valor
