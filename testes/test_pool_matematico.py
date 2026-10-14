"""Pool matemático: concorrência limitada, isolamento de erro e telemetria."""

import os
import threading
import time
from unittest.mock import patch
import unittest

from jev_bot.pool_matematico import (MAX_SEGURO, PoolMatematico,
                                     MAX_PARALLEL_SYMBOLS_PADRAO, max_paralelo)


class TestConfiguracao(unittest.TestCase):
    """Número de análises concorrentes é configurável e com teto seguro."""

    def test_padrao_quatro(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(max_paralelo(), 4)
            self.assertEqual(MAX_PARALLEL_SYMBOLS_PADRAO, 4)

    def test_env_invalido_usa_padrao(self):
        for valor in ("", "abc", "0", "-3"):
            with self.subTest(valor=valor), \
                    patch.dict(os.environ, {"JEV_MAX_PARALLEL_SYMBOLS": valor}, clear=True):
                self.assertEqual(max_paralelo(), 4)

    def test_teto_seguro(self):
        with patch.dict(os.environ, {"JEV_MAX_PARALLEL_SYMBOLS": "99"}, clear=True):
            self.assertEqual(max_paralelo(), MAX_SEGURO)
        with patch.dict(os.environ, {"JEV_MAX_PARALLEL_SYMBOLS": "2"}, clear=True):
            self.assertEqual(max_paralelo(), 2)


class TestConcorrencia(unittest.TestCase):
    """Até N análises simultâneas; o excedente aguarda vaga; erro não derruba."""

    def _coletor(self):
        estado = {"ativos": 0, "pico": 0, "iniciados": [], "terminados": []}
        trava = threading.Lock()

        def entrar(nome):
            with trava:
                estado["ativos"] += 1
                estado["pico"] = max(estado["pico"], estado["ativos"])
                estado["iniciados"].append(nome)

        def sair(nome):
            with trava:
                estado["ativos"] -= 1
                estado["terminados"].append(nome)

        return estado, entrar, sair

    def test_quatro_simultaneos_e_quinto_aguarda(self):
        estado, entrar, sair = self._coletor()
        pool = PoolMatematico(max_workers=4)

        def trabalho(nome, pausa):
            entrar(nome)
            time.sleep(pausa)
            sair(nome)
            return nome

        futuros = [pool.submeter(f"S{i}", trabalho, f"S{i}", 0.05) for i in range(6)]
        resultados = [f.result(timeout=5) for f in futuros]
        pool.encerrar()

        self.assertEqual(set(resultados), {f"S{i}" for i in range(6)})
        self.assertEqual(pool.metricas()["pico_concorrencia"], 4)
        self.assertLessEqual(estado["pico"], 4)
        self.assertEqual(set(estado["terminados"]), {f"S{i}" for i in range(6)})

    def test_worker_lento_nao_bloqueia_os_demais(self):
        estado, entrar, sair = self._coletor()
        pool = PoolMatematico(max_workers=4)

        def lento(nome):
            entrar(nome)
            time.sleep(0.4)
            sair(nome)
            return "lento"

        def rapido(nome):
            entrar(nome)
            time.sleep(0.02)
            sair(nome)
            return "rapido"

        f_lento = pool.submeter("LENTO", lento, "LENTO")
        rapidos = [pool.submeter(f"R{i}", rapido, f"R{i}") for i in range(3)]
        for f in rapidos:
            self.assertEqual(f.result(timeout=2), "rapido")
        self.assertFalse(f_lento.done())
        self.assertEqual(f_lento.result(timeout=2), "lento")
        pool.encerrar()

    def test_excecao_em_um_trabalho_nao_derruba_o_pool(self):
        pool = PoolMatematico(max_workers=4)

        def falha():
            raise ValueError("símbolo problemático")

        f_falha = pool.submeter("ERR", falha)
        f_ok = pool.submeter("OK", lambda: "ok")
        with self.assertRaises(ValueError):
            f_falha.result(timeout=2)
        self.assertEqual(f_ok.result(timeout=2), "ok")
        self.assertEqual(pool.metricas()["concluidos"], 2)
        pool.encerrar()

    def test_metricas_duracao(self):
        pool = PoolMatematico(max_workers=2)
        futuros = [pool.submeter(f"S{i}", lambda: time.sleep(0.01)) for i in range(4)]
        for f in futuros:
            f.result(timeout=2)
        metricas = pool.metricas()
        self.assertEqual(metricas["concluidos"], 4)
        self.assertIsNotNone(metricas["duracao_p50_ms"])
        self.assertGreaterEqual(metricas["duracao_p95_ms"], metricas["duracao_p50_ms"])
        pool.encerrar()


if __name__ == "__main__":
    unittest.main(verbosity=2)
