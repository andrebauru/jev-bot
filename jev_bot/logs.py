"""Logs em logs/ com rotação, limpeza e mascaramento de segredos.

Política documentada: 10 MB por arquivo, até 5 arquivos rotacionados,
limpeza de arquivos antigos com mais de 14 dias (o arquivo ativo nunca é
excluído). Nenhuma senha, chave ou token é gravada: um filtro mascara
segredos inclusive em erros e tracebacks. Eventos graves também são
publicados no painel de status pelo orquestrador, além do arquivo.
"""

from __future__ import annotations

import logging
import logging.handlers
import re
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
_FACHADA = None
PASTA_PADRAO = RAIZ / "logs"
NOME_ARQUIVO = "jev-bot.log"
LIMITE_MB = 10
BACKUPS = 5
RETENCAO_DIAS = 14

_PADROES_SIGILO = (
    (re.compile(r"(?i)(api[_-]?key|token|password|passwd|secret|authorization|senha)\s*[=:]\s*\S+"),
     r"\1=***"),
    (re.compile(r"(?i)bearer\s+\S+"), "Bearer ***"),
    (re.compile(r"sk-or-[A-Za-z0-9_-]{8,}"), "sk-or-***"),
    (re.compile(r"(?i)(login\s*[=:]\s*)\d+"), r"\1***"),
)


def mascarar(texto):
    """Substitui segredos por *** sem alterar o restante da mensagem."""
    resultado = str(texto)
    for padrao, substituicao in _PADROES_SIGILO:
        resultado = padrao.sub(substituicao, resultado)
    return resultado


class _FiltroSigilo(logging.Filter):
    """Aplica o mascaramento antes de qualquer formato ou arquivo."""

    def filter(self, registro):
        try:
            registro.msg = mascarar(registro.getMessage())
            registro.args = ()
        except Exception:
            return True  # nunca interrompe a aplicação por falha de log
        return True


class Logger:
    """Fachada com campos nomeados; delega ao logger padrão com mascaramento."""

    def __init__(self, interno):
        self._interno = interno

    @property
    def interno(self):
        return self._interno

    def debug(self, mensagem, **campos):
        evento(logging.DEBUG, mensagem, **campos)

    def info(self, mensagem, **campos):
        evento(logging.INFO, mensagem, **campos)

    def aviso(self, mensagem, **campos):
        evento(logging.WARNING, mensagem, **campos)

    def erro(self, mensagem, **campos):
        evento(logging.ERROR, mensagem, **campos)

    # Aliases para quem espera a convenção da biblioteca padrão.
    warning = aviso
    error = erro


def configurar(pasta=None, nivel=logging.INFO):
    """Instala o manipulador rotativo uma única vez e devolve a fachada.

    Chamadas repetidas não duplicam handlers. O arquivo só é criado no
    primeiro registro (delay=True).
    """
    global _FACHADA
    logger = logging.getLogger("jev-bot")
    if _FACHADA is not None and any(isinstance(h, logging.handlers.RotatingFileHandler)
                                    for h in logger.handlers):
        return _FACHADA
    if not any(isinstance(h, logging.handlers.RotatingFileHandler) for h in logger.handlers):
        pasta = Path(pasta or PASTA_PADRAO)
        pasta.mkdir(parents=True, exist_ok=True)
        manipulador = logging.handlers.RotatingFileHandler(
            pasta / NOME_ARQUIVO, maxBytes=LIMITE_MB * 1024 * 1024, backupCount=BACKUPS,
            encoding="utf-8", delay=True)
        manipulador.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(module)s %(message)s"))
        manipulador.addFilter(_FiltroSigilo())
        logger.addHandler(manipulador)
        logger.setLevel(nivel)
        logger.propagate = False
        limpar_antigos(pasta)
    _FACHADA = Logger(logger)
    return _FACHADA


def limpar_antigos(pasta=None, dias=RETENCAO_DIAS, relogio=time.time):
    """Remove arquivos rotacionados com mais de `dias` dias; mantém o ativo."""
    pasta = Path(pasta or PASTA_PADRAO)
    corte = relogio() - dias * 86400
    removidos = []
    for arquivo in pasta.glob("*.log*"):
        if arquivo.name == NOME_ARQUIVO:
            continue  # arquivo ativo nunca é excluído
        try:
            if arquivo.stat().st_mtime < corte:
                arquivo.unlink()
                removidos.append(arquivo.name)
        except OSError:
            continue  # arquivo em uso ou sem permissão: mantém
    return removidos


def evento(nivel, mensagem, **campos):
    """Registra uma linha compacta com metadados seguros.

    Campos podem incluir analysis_id, símbolo, contagens e durações.
    Nunca passe valores sensíveis (são mascarados, mas evite enviá-los).
    """
    logger = logging.getLogger("jev-bot")
    detalhe = " ".join(f"{k}={v}" for k, v in campos.items() if v is not None)
    logger.log(nivel, f"{mensagem}{(' | ' + detalhe) if detalhe else ''}")


def debug(mensagem, **campos):
    evento(logging.DEBUG, mensagem, **campos)


def info(mensagem, **campos):
    evento(logging.INFO, mensagem, **campos)


def aviso(mensagem, **campos):
    evento(logging.WARNING, mensagem, **campos)


def erro(mensagem, **campos):
    evento(logging.ERROR, mensagem, **campos)
