"""Pool limitado de análises matemáticas M1 concorrentes por símbolo.

A concorrência aqui é exclusivamente de **cálculo Python puro** (estatísticas,
geração de hipóteses, validação OOS e simulação trade-level). Chamadas ao
terminal MT5 permanecem serializadas na thread do orquestrador, porque a API
Python do MT5 não é thread-safe. Cada trabalho recebe dados imutáveis
(candles já materializados) e devolve um resultado; nenhum worker toca em
carteira, GUI, disco ou estado compartilhado do orquestrador.

`MAX_PARALLEL_SYMBOLS` é configurável pela variável de ambiente
`JEV_MAX_PARALLEL_SYMBOLS` e limitado por `MAX_SEGURO` para impedir explosão
de memória/CPU. Um símbolo lento não impede os demais e uma exceção em um
trabalho não derruba o pool.
"""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor

MAX_PARALLEL_SYMBOLS_PADRAO = 4
MAX_SEGURO = 8


def max_paralelo():
    """Número de análises concorrentes: ambiente, padrão 4, limitado a 8."""
    bruto = os.environ.get("JEV_MAX_PARALLEL_SYMBOLS", "").strip()
    try:
        valor = int(bruto)
    except (TypeError, ValueError):
        valor = MAX_PARALLEL_SYMBOLS_PADRAO
    if valor <= 0:
        valor = MAX_PARALLEL_SYMBOLS_PADRAO
    return max(1, min(MAX_SEGURO, valor))


class PoolMatematico:
    """Executa funções puras por símbolo com até N trabalhos simultâneos."""

    def __init__(self, max_workers=None, relogio=time.time):
        self.max_workers = max(1, min(MAX_SEGURO, int(max_workers or max_paralelo())))
        self.relogio = relogio
        self._executor = ThreadPoolExecutor(max_workers=self.max_workers,
                                            thread_name_prefix="matematica")
        self._em_execucao = 0
        self._pico = 0
        self._duracoes_ms = []
        self._concluidos = 0
        self._erros = 0
        self._lock = __import__("threading").Lock()

    def _envelope(self, nome, funcao, *args, **kwargs):
        with self._lock:
            self._em_execucao += 1
            self._pico = max(self._pico, self._em_execucao)
        inicio = time.monotonic()
        try:
            return funcao(*args, **kwargs)
        finally:
            duracao = round((time.monotonic() - inicio) * 1000)
            with self._lock:
                self._em_execucao -= 1
                self._concluidos += 1
                self._duracoes_ms.append(duracao)
                if len(self._duracoes_ms) > 500:
                    del self._duracoes_ms[:-500]

    def submeter(self, nome, funcao, *args, **kwargs):
        """Enfileira um trabalho; devolve o Future correspondente."""
        return self._executor.submit(self._envelope, nome, funcao, *args, **kwargs)

    def registrar_erro(self):
        with self._lock:
            self._erros += 1

    @staticmethod
    def _percentil(valores, q):
        if not valores:
            return None
        ordenados = sorted(valores)
        k = (len(ordenados) - 1) * q
        f, c = int(k), min(int(k) + 1, len(ordenados) - 1)
        if f == c:
            return ordenados[f]
        return ordenados[f] + (ordenados[c] - ordenados[f]) * (k - f)

    def metricas(self):
        with self._lock:
            duracoes = list(self._duracoes_ms)
            return {"max_workers": self.max_workers,
                    "pico_concorrencia": self._pico,
                    "concluidos": self._concluidos,
                    "erros": self._erros,
                    "duracao_p50_ms": self._percentil(duracoes, .5),
                    "duracao_p95_ms": self._percentil(duracoes, .95),
                    "em_execucao": self._em_execucao}

    def encerrar(self, esperar=True):
        self._executor.shutdown(wait=esperar)
