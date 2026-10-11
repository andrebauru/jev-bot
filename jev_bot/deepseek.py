"""Descoberta autônoma de padrões pelo DeepSeek via OpenRouter.

O modelo recebe apenas resumos estatísticos compactos calculados em
Python e o repositório de padrões do ativo. Propõe hipóteses estruturadas
(tipo de regra + parâmetros + direção + horizonte) e sugere revisões das
existentes. Nunca devolve probabilidade, confiança ou resultado calculado:
isso é papel da validação local em `padroes.py`.

Controles obrigatórios: timeout, retry com recuo apenas em falhas
transitórias, limite de chamadas por hora e de tokens por dia, e saída
validada contra um schema explícito. A chave nunca é registrada em log.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from . import configuracao
from .padroes import REGRAS

TENTATIVAS = 3          # 1 tentativa + 2 retries
MAX_PADROES_POR_RESPOSTA = 8
MAX_REVISAOES_POR_RESPOSTA = 20
GUARDA_PASTA = "padroes"


class Orcamento:
    """Limites de gasto: chamadas por hora e tokens por dia, persistidos."""

    def __init__(self, pasta, max_hora, tokens_dia, relogio=time.time):
        self.arquivo = Path(pasta) / "orcamento.json"
        self.arquivo.parent.mkdir(parents=True, exist_ok=True)
        self.max_hora = max_hora
        self.tokens_dia = tokens_dia
        self.relogio = relogio

    def _estado(self):
        try:
            dados = json.loads(self.arquivo.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            dados = {}
        hoje = time.strftime("%Y-%m-%d", time.gmtime(self.relogio()))
        if dados.get("dia") != hoje:
            dados = {"dia": hoje, "tokens": 0, "chamadas": []}
        dados["chamadas"] = [t for t in dados.get("chamadas", [])
                             if self.relogio() - t < 3600 and isinstance(t, (int, float))]
        return dados

    def permitir(self):
        """(permitido, motivo) antes de gastar qualquer token."""
        dados = self._estado()
        if len(dados["chamadas"]) >= self.max_hora:
            return False, "Limite de chamadas DeepSeek por hora atingido"
        if dados.get("tokens", 0) >= self.tokens_dia:
            return False, "Limite de tokens DeepSeek do dia atingido"
        return True, ""

    def registrar(self, tokens):
        """Soma tokens consumidos e o instante da chamada, gravando em disco."""
        dados = self._estado()
        dados["tokens"] = int(dados.get("tokens", 0)) + max(0, int(tokens))
        dados["chamadas"].append(self.relogio())
        try:
            self.arquivo.write_text(json.dumps(dados), encoding="utf-8")
        except OSError:
            pass  # sem persistência o orçamento reconta; não bloqueia análise

    def consumo(self):
        dados = self._estado()
        return {"tokens_dia": dados.get("tokens", 0), "tokens_limite": self.tokens_dia,
                "chamadas_hora": len(dados["chamadas"]), "chamadas_limite": self.max_hora}


def configuracao_chat():
    """Lê chave e parâmetros restringindo o envio à API oficial de chat."""
    configuracao.carregar_ambiente()
    chave = (os_chave() or "").strip()
    if not chave:
        raise RuntimeError("Preencha OPENROUTER_API_KEY na aba Configurações ou no .env.")
    endpoint = configuracao.endpoint_chat()
    modelo = configuracao.modelo_deepseek()
    if not modelo or "/" not in modelo or len(modelo) > 80:
        raise RuntimeError("OPENROUTER_DEEPSEEK_MODEL deve ser um identificador válido da OpenRouter.")
    return chave, endpoint, modelo


def os_chave():
    """Chave do processo (carregada do .env ou do cofre do sistema)."""
    import os
    return os.environ.get("OPENROUTER_API_KEY", "")


def validar_resposta(dados):
    """Valida o JSON do modelo contra o schema; devolve (hipoteses, revisoes).

    Entradas inválidas são descartadas com contador, nunca corrigidas.
    """
    if not isinstance(dados, dict) or "padroes" not in dados or "revisoes" not in dados:
        raise ValueError("Resposta deve conter as listas 'padroes' e 'revisoes'.")
    hipoteses_brutas = dados.get("padroes", [])
    revisoes_brutas = dados.get("revisoes", [])
    if not isinstance(hipoteses_brutas, list) or not isinstance(revisoes_brutas, list):
        raise ValueError("Campos 'padroes' e 'revisoes' devem ser listas.")
    if len(hipoteses_brutas) > MAX_PADROES_POR_RESPOSTA or len(revisoes_brutas) > MAX_REVISAOES_POR_RESPOSTA:
        raise ValueError("Resposta excede o número máximo de itens.")
    from .padroes import normalizar_hipotese, ErroRegra
    hipoteses, descartadas = [], 0
    for bruta in hipoteses_brutas:
        try:
            hipoteses.append(normalizar_hipotese(bruta))
        except ErroRegra:
            descartadas += 1
    revisoes = []
    for bruta in revisoes_brutas:
        if not isinstance(bruta, dict):
            continue
        item = {"id": str(bruta.get("id", ""))[:12], "acao": str(bruta.get("acao", ""))[:12],
                "motivo": str(bruta.get("motivo", ""))[:300]}
        if item["id"] and item["acao"] in ("manter", "expirar"):
            revisoes.append(item)
    return hipoteses, revisoes, descartadas


def _corpo_requisicao(chave, endpoint, modelo, resumo, padroes_atuais, max_tokens=2000):
    """Monta a chamada de chat completions; a chave fica apenas no header."""
    especificacao = {tipo: {"parametros": {k: list(v) for k, v in specs["params"].items()},
                            "descricao": specs["desc"]} for tipo, specs in REGRAS.items()}
    sistema = (
        "Você é um analista quantitativo. Recebe resumos estatísticos calculados em Python "
        "sobre candles M1 de um ativo (janela móvel de 30 dias, corte cronológico de 70% para "
        "descoberta) e o repositório de padrões do ativo com suas estatísticas. "
        "Tarefas: (1) revisar os padrões existentes marcando acao manter ou expirar com motivo; "
        "(2) propor novas hipóteses testáveis a partir dos números fornecidos. "
        "Regras: use exclusivamente os números fornecidos; não invente probabilidades, "
        "confiança ou resultados; não cite notícias ou dados indisponíveis; cada hipótese usa "
        "um tipo_regra suportado com parâmetros numéricos dentro das faixas indicadas, "
        "direcao BUY ou SELL e horizonte_minutos entre 1 e 60; justificativa de até 300 "
        "caracteres citando os números usados. Responda SOMENTE com um objeto JSON no formato "
        '{\"padroes\": [...], \"revisoes\": [...]}.'
    )
    usuario = json.dumps({"ativo": resumo, "padroes_existentes": padroes_atuais,
                          "tipos_regra_suportados": especificacao}, allow_nan=False)
    payload = {
        "model": modelo,
        "messages": [{"role": "system", "content": sistema}, {"role": "user", "content": usuario}],
        "temperature": 0.2,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }
    corpo = json.dumps(payload, allow_nan=False).encode()
    return Request(endpoint, data=corpo, headers={
        "content-type": "application/json", "authorization": f"Bearer {chave}",
        "http-referer": "https://github.com/local/jev-bot", "x-title": "jev-bot",
    })


def _extrair_json(conteudo):
    """Converte a saída do modelo em objeto, tolerando cercas de código."""
    texto = conteudo.strip()
    if texto.startswith("```"):
        texto = texto.strip("`")
        if texto.startswith("json"):
            texto = texto[4:]
    inicio, fim = texto.find("{"), texto.rfind("}")
    if inicio < 0 or fim <= inicio:
        raise ValueError("Resposta sem objeto JSON")
    return json.loads(texto[inicio:fim + 1])


def _dormir(segundos):
    """Recuo entre tentativas; separado para permitir isolamento em testes."""
    time.sleep(segundos)


def analisar(resumo, padroes_atuais, cancelado=lambda: False):
    """Consulta o DeepSeek e devolve (hipoteses, revisoes, uso, descartadas).

    Retries com recuo apenas em falhas transitórias (5xx, 429, rede).
    Erros de autenticação ou cobrança não repetem a chamada.
    """
    chave, endpoint, modelo = configuracao_chat()
    url = urlparse(endpoint)
    if url.scheme != "https" or url.hostname != "openrouter.ai" or url.path != "/api/v1/chat/completions":
        raise RuntimeError("Endpoint DeepSeek inválido.")
    timeout = configuracao.timeout_deepseek()
    ultimo_erro = None
    for tentativa in range(TENTATIVAS):
        if cancelado():
            return [], [], {"tokens": 0, "motivo": "cancelado"}, 0
        req = _corpo_requisicao(chave, endpoint, modelo, resumo, padroes_atuais)
        try:
            with urlopen(req, timeout=timeout) as resposta:
                dados = json.loads(resposta.read(2_000_000).decode())
            uso = dados.get("usage", {}) or {}
            tokens = int(uso.get("total_tokens") or (uso.get("prompt_tokens", 0) + uso.get("completion_tokens", 0)))
            conteudo = dados["choices"][0]["message"]["content"]
            hipoteses, revisoes, descartadas = validar_resposta(_extrair_json(conteudo))
            return hipoteses, revisoes, {"tokens": tokens}, descartadas
        except HTTPError as exc:
            if exc.code in (401, 402, 403):
                raise RuntimeError(f"OpenRouter HTTP {exc.code}: confira chave e crédito; chamada não repetida.") from None
            if exc.code in (429,) or 500 <= exc.code < 600:
                ultimo_erro = f"OpenRouter HTTP {exc.code}"
            else:
                raise RuntimeError(f"OpenRouter HTTP {exc.code}: requisição rejeitada.") from None
        except (URLError, TimeoutError, json.JSONDecodeError, KeyError, TypeError, ValueError, OSError):
            ultimo_erro = "Falha de rede ou resposta inválida do DeepSeek"
        if tentativa < TENTATIVAS - 1:
            _dormir(2 * (2 ** tentativa))
    raise RuntimeError(f"DeepSeek indisponível ({ultimo_erro}); hipóteses não atualizadas.")


def testar_conexao(chave=None, modelo=None, timeout=20):
    """Verifica chave e disponibilidade do modelo configurado, sem segredo na saída.

    1) Confere o modelo em /api/v1/models (público). 2) Envia uma chamada
    mínima com a chave para confirmar autorização e cobrança.
    """
    configuracao.carregar_ambiente()
    chave = (chave or os_chave()).strip()
    modelo = (modelo or configuracao.modelo_deepseek()).strip()
    if not chave:
        return {"ok": False, "erro": "Chave não configurada."}
    try:
        with urlopen(Request("https://openrouter.ai/api/v1/models"), timeout=timeout) as resposta:
            catalogo = json.loads(resposta.read(2_000_000).decode())
        ids = {m.get("id") for m in catalogo.get("data", [])}
        if modelo not in ids:
            return {"ok": False, "erro": f"Modelo '{modelo}' não está disponível nesta conta OpenRouter."}
        req = _corpo_requisicao(chave, configuracao.endpoint_chat(), modelo,
                                {"simbolo": "TESTE", "disponivel": False, "motivo": "teste de conexão"}, [])
        req.data = json.dumps({"model": modelo, "max_tokens": 1,
                               "messages": [{"role": "user", "content": "Responda: {}"}]}).encode()
        with urlopen(req, timeout=timeout) as resposta:
            json.loads(resposta.read(100_000).decode())
        return {"ok": True, "modelo": modelo}
    except HTTPError as exc:
        return {"ok": False, "erro": f"OpenRouter HTTP {exc.code}: confira chave, crédito e acesso ao modelo."}
    except (URLError, TimeoutError, OSError, json.JSONDecodeError, KeyError):
        return {"ok": False, "erro": "Sem resposta do OpenRouter; verifique a rede."}
