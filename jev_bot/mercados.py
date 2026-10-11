"""Seleção de mercados para monitoramento e operação, persistida localmente.

Estrutura em JSON atômico (data/mercados.json), sem credenciais. Regra de
consistência: Operar exige Monitorar — garantida na normalização dos dados
e reforçada na interface. Padrão conservador: sem arquivo, nada é
monitorado nem operado; o operador escolhe na aba Configurações.

A descoberta de categorias/símbolos vem somente do catálogo real do
MetaTrader 5 conectado; um instantâneo é guardado em cache para uso
offline, sempre identificado como cache.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

SCHEMA = 1
ALTERACOES_EXEMPLO = 10


def _escrita_atomica(caminho, conteudo):
    """Gravação atômica idêntica ao padrão do db.json."""
    caminho = Path(caminho)
    caminho.parent.mkdir(parents=True, exist_ok=True)
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


def normalizar(dados):
    """Valida a estrutura e garante Operar ⇒ Monitorar em todo o arquivo."""
    if not isinstance(dados, dict):
        return {"schema_version": SCHEMA, "selecao": {}, "atualizado_em": 0}
    saida = {"schema_version": int(dados.get("schema_version", SCHEMA)),
             "atualizado_em": dados.get("atualizado_em", 0),
             "selecao": {}}
    selecao = dados.get("selecao")
    if not isinstance(selecao, dict):
        return saida
    for categoria, simbolos in selecao.items():
        if not isinstance(simbolos, dict):
            continue
        limpa = {}
        for simbolo, estado in simbolos.items():
            if not isinstance(estado, dict):
                continue
            monitorar = bool(estado.get("monitorar", False))
            operar = bool(estado.get("operar", False)) and monitorar  # consistência
            limpa[str(simbolo).upper()] = {"monitorar": monitorar, "operar": operar}
        if limpa:
            saida["selecao"][str(categoria)] = limpa
    return saida


class SelecaoMercados:
    """Consulta rápida: monitorar(simbolo), operar(simbolo) e contagens."""

    def __init__(self, dados=None):
        dados = normalizar(dados or {})
        self.dados = dados
        self.mapa = {}
        for simbolos in dados["selecao"].values():
            for simbolo, estado in simbolos.items():
                anterior = self.mapa.get(simbolo, (False, False))
                self.mapa[simbolo] = (anterior[0] or estado["monitorar"],
                                      anterior[1] or estado["operar"])

    def monitorar(self, simbolo):
        estado = self.mapa.get(str(simbolo).upper())
        return bool(estado and estado[0])

    def operar(self, simbolo):
        estado = self.mapa.get(str(simbolo).upper())
        return bool(estado and estado[1])

    def contagens(self):
        monitorados = sum(1 for m, _ in self.mapa.values() if m)
        operaveis = sum(1 for _, o in self.mapa.values() if o)
        return monitorados, operaveis

    def diferencas(self, outra, limite=ALTERACOES_EXEMPLO):
        """Símbolos cuja seleção mudou em relação a `outra` (amostra limitada)."""
        if outra is None:
            return []
        chaves = set(self.mapa) | set(outra.mapa)
        mudancas = [s for s in sorted(chaves)
                    if self.mapa.get(s, (False, False)) != outra.mapa.get(s, (False, False))]
        return mudancas[:limite]

    def vazia(self):
        return not self.mapa


def caminho_padrao():
    return Path(__file__).resolve().parent.parent / "data" / "mercados.json"


def caminho_cache():
    return Path(__file__).resolve().parent.parent / "data" / "mercados_cache.json"


def carregar(caminho=None):
    """Lê a seleção persistida; ausente ou corrompida devolve seleção vazia."""
    caminho = Path(caminho or caminho_padrao())
    if not caminho.exists():
        return SelecaoMercados({})
    try:
        return SelecaoMercados(json.loads(caminho.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, OSError):
        copia = caminho.with_suffix(".json.corrupto")
        try:
            caminho.replace(copia)
        except OSError:
            pass
        return SelecaoMercados({})


def salvar(selecao_dados, caminho=None):
    """Persiste a seleção normalizada de forma atômica."""
    caminho = Path(caminho or caminho_padrao())
    dados = normalizar(selecao_dados)
    dados["atualizado_em"] = time.time()
    _escrita_atomica(caminho, json.dumps(dados, indent=1, allow_nan=False))
    return dados


def catalogo_por_categoria(fonte):
    """Descoberta real: agrupa o catálogo MT5 conectado por categoria (path).

    Não inventa ativos: só símbolos que o broker retornou.
    """
    try:
        grupos = fonte.catalogo_por_categoria()
    except (RuntimeError, AttributeError, OSError):
        return {}
    return {categoria: sorted(nomes) for categoria, nomes in (grupos or {}).items() if nomes}


def salvar_cache(categorias, caminho=None):
    """Instantâneo do catálogo para uso offline, identificado como cache."""
    caminho = Path(caminho or caminho_cache())
    _escrita_atomica(caminho, json.dumps(
        {"schema_version": SCHEMA, "origem": "cache", "salvo_em": time.time(),
         "categorias": categorias}, allow_nan=False))
    return caminho


def carregar_cache(caminho=None):
    caminho = Path(caminho or caminho_cache())
    if not caminho.exists():
        return {}
    try:
        dados = json.loads(caminho.read_text(encoding="utf-8"))
        return dados.get("categorias") or {}
    except (json.JSONDecodeError, OSError):
        return {}
