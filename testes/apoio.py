"""Utilitários compartilhados pelos testes em testes/ (dobros e geradores)."""

from __future__ import annotations

import random
import time
from types import SimpleNamespace
from unittest.mock import Mock

from jev_bot.types import MarketState


def candles_sinteticos(n=400, seed=1, preco=100.0):
    """Candles M1 realistas com passeio aleatório para estatísticas estáveis."""
    random.seed(seed)
    resultado = []
    for i in range(n):
        variacao = random.uniform(-0.001, 0.001) * preco
        abertura = preco
        preco = max(0.01, preco + variacao)
        resultado.append({"time": 1_700_000_000 + i * 60, "open": abertura,
                          "high": max(abertura, preco) * 1.0005,
                          "low": min(abertura, preco) * 0.9995,
                          "close": preco, "tick_volume": random.randint(1, 100)})
    return resultado


def candles_deterministicos(n=6003, base=100.0):
    """Padrão periódico de 10 candles: 3 quedas seguidas de recuperação.

    closes: ..., 112, 110, 108, 106, 107, 108, 109, 110, 111, 112, ...
    `quedas_consecutivas(n=3)` dispara em todo fim de sequência de quedas e
    o candle seguinte sempre sobe (taxa de acerto ≈ 100% no horizonte 5).
    n=6003 termina no 3º candle de queda (condição verdadeira "agora").
    """
    periodos = [112.0, 110.0, 108.0, 106.0, 107.0, 108.0, 109.0, 110.0, 111.0, 112.0]
    resultado = []
    for i in range(n):
        close = base + periodos[i % 10] - 112.0 + 6.0
        anterior = base + periodos[(i - 1) % 10] - 112.0 + 6.0
        abre = anterior
        alto = max(abre, close) + 0.25
        baixo = min(abre, close) - 0.25
        resultado.append({"time": 1_700_000_000 + i * 60, "open": abre, "high": alto,
                          "low": baixo, "close": close, "tick_volume": 50})
    return resultado


def janela(candles, simbolo="TESTE"):
    return {"simbolo": simbolo, "candles": candles, "inicio": candles[0]["time"],
            "fim": candles[-1]["time"], "solicitados": len(candles), "recebidos": len(candles),
            "lacunas": 0, "minutos_lacuna": 0, "maior_lacuna": 0}


def estado_mt5(simbolo="TESTE", closes=None):
    """Estado válido perto de agora, exigido pelo gate de risco."""
    agora = time.time()
    estado = MarketState(simbolo, "Crypto", closes[-1] if closes else 100.0,
                         bid=99.995, ask=100.005, tick_time=agora,
                         stop_distance=0.5, loss_per_lot=10.0, margin_per_lot=100.0,
                         volume_min=0.01, volume_max=10, volume_step=0.01,
                         trade_mode=4, currencies=("USD",),
                         candles=[{"time": agora - 60 * (len(closes) - i) if closes else agora,
                                   "open": 100, "high": 100.5, "low": 99.5, "close": c,
                                   "tick_volume": 10} for i, c in enumerate(closes or [100.0])])
    estado.mathematics["mt5_execution_spec"] = {
        "point": 10 ** -estado.digits, "trade_stops_level": 0,
        "digits": estado.digits,
    }
    return estado


class LoggerSilencioso:
    """Substitui a fachada de logs nos testes para não gravar arquivos."""

    def debug(self, mensagem, **campos):
        pass

    def info(self, mensagem, **campos):
        pass

    def aviso(self, mensagem, **campos):
        pass

    def erro(self, mensagem, **campos):
        pass


class FonteFake:
    """Dobro de FonteMT5 para testes do orquestrador sem MT5."""

    moeda = "USD"
    resumo = {}

    def __init__(self, estados=(), janelas=None, cotacoes=None, falhas=()):
        self.estados = lambda: iter(estados)
        self.janelas = janelas or {}
        self.cotacoes = cotacoes or {}
        self.falhas = set(falhas)
        self.cancelado = lambda: False
        self.evento = None

    def cotacao(self, simbolo):
        if simbolo in self.falhas:
            return None
        return self.cotacoes.get(simbolo, (99.99, 100.0, time.time()))

    def conta(self):
        return dict(balance=0, equity=0, profit=0, margin_free=0, currency="USD")

    def lucro(self, posicao, preco):
        return round((preco - posicao.entry) * posicao.volume * 100, 2)

    def revalidar(self, estado):
        return estado

    def fim_de_semana(self):
        return False

    def janela_m1(self, simbolo, dias=30, lote=4320, cancelado=None, progresso=None):
        return self.janelas.get(simbolo)

    def incremento_m1(self, simbolo, desde, max_candles=4320):
        return None  # sem cache incremental no dobro padrão


def api_mock_inicializado():
    """Mock de API do MT5 conectada, para testes da fonte."""
    api = Mock()
    api.TIMEFRAME_M1 = 1
    api.ORDER_TYPE_BUY, api.ORDER_TYPE_SELL = 0, 1
    api.symbols_total.return_value = 0
    return api


def resposta_mock(conteudo):
    """Mock de resposta urlopen com corpo JSON pronto (contrato JEv/DeepSeek)."""
    resposta = Mock()
    resposta.__enter__ = Mock(return_value=resposta)
    resposta.__exit__ = Mock(return_value=False)
    resposta.read.return_value = conteudo.encode()
    return resposta
