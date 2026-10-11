"""Testes do motor matemático local (janela M1 de 30 dias)."""

import json
import unittest

from apoio import candles_sinteticos, janela
from jev_bot import motor_matematico
from jev_bot.motor_matematico import CORTE_OOS, estatisticas, janela_descoberta, percentil, resumo_compacto


class TestMotorMatematico(unittest.TestCase):
    """Estatísticas locais: forma, determinismo e ausência de indicadores."""

    def test_percentil_e_determinismo(self):
        valores = list(range(1, 11))
        self.assertEqual(percentil(valores, 0), 1)
        self.assertEqual(percentil(valores, 1), 10)
        self.assertEqual(percentil(valores, .5), 5.5)

    def test_estatisticas_completas_e_sem_indicadores(self):
        s = estatisticas(janela(candles_sinteticos(600)), 0.05)
        self.assertTrue(s["disponivel"])
        self.assertEqual(s["candles"], 600)
        self.assertAlmostEqual(s["custo_spread_pct"], 0.05, delta=0.02)
        for chave in ("movimento_1min", "amplitude_1min", "apos_quedas", "apos_altas",
                      "tempo_para_0.25", "queda_0.5", "alta_0.5"):
            self.assertIn(chave, s)
        proibidos = {"rsi", "macd", "media_movel"}
        self.assertFalse(proibidos & {k.casefold() for k in s})
        self.assertEqual(estatisticas(janela(candles_sinteticos(600)), 0.05), s)

    def test_amostra_insuficiente_e_invalidez(self):
        self.assertFalse(estatisticas(janela(candles_sinteticos(2)), 0.05)["disponivel"])
        quebrados = candles_sinteticos(50)
        quebrados[3]["close"] = float("nan")
        self.assertFalse(estatisticas(janela(quebrados), 0.05)["disponivel"])

    def test_corte_cronologico_sem_vazamento(self):
        candles = candles_sinteticos(1000)
        descoberta, oos = janela_descoberta(candles)
        self.assertEqual(len(descoberta), int(1000 * CORTE_OOS))
        self.assertEqual(descoberta[-1]["time"], candles[int(1000 * CORTE_OOS) - 1]["time"])
        self.assertEqual(oos[0]["time"], candles[int(1000 * CORTE_OOS)]["time"])

    def test_resumo_compacto_pequeno_e_seguro(self):
        s = estatisticas(janela(candles_sinteticos(600)), 0.05)
        resumo = resumo_compacto(s)
        texto = json.dumps(resumo)
        self.assertLess(len(texto), 4000)
        self.assertNotIn("candles\": [", texto)
        self.assertIn("70% iniciais", texto)


if __name__ == "__main__":
    unittest.main(verbosity=2)
