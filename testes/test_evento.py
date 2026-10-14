"""Evento quantitativo único por ativo/candle (retração/gap/neutro)."""

import unittest

from jev_bot import evento


def _candles(n=1440, preco=100.0):
    return [{"time": i * 60, "open": preco, "high": preco + .5,
             "low": preco - .5, "close": preco, "tick_volume": 10}
            for i in range(n)]


class TestEventoQuantitativo(unittest.TestCase):
    """Um único objeto descritivo, com nulls explícitos e sem look-ahead."""

    def test_dados_insuficientes_nao_inventam(self):
        resultado = evento.evento_quantitativo(_candles(5))
        self.assertEqual(resultado["status"], "INVALID_CORE_DATA")
        self.assertEqual(resultado["data_quality"], "DADOS_INSUFICIENTES")
        self.assertIn("bars_available", resultado)

    def test_retracao_alta(self):
        candles = _candles(1440)
        for i in range(1420, 1440):
            candles[i]["high"] = 120.0
            candles[i]["low"] = 100.0
            candles[i]["close"] = 100.0
        candles[-1]["close"] = 105.0  # topo 120, mínimo 100, fecho 105 => retração 0.75
        resultado = evento.evento_quantitativo(candles)
        self.assertEqual(resultado["status"], "OK")
        self.assertEqual(resultado["event_type"], "RETRACAO_ALTA")
        self.assertAlmostEqual(resultado["retracao_da_maxima"], 0.75, places=2)

    def test_recuperacao_baixa(self):
        candles = _candles(1440)
        for i in range(1420, 1440):
            candles[i]["high"] = 120.0
            candles[i]["low"] = 100.0
            candles[i]["close"] = 100.0
        candles[-1]["close"] = 115.0  # perto do topo => recuperação 0.75
        resultado = evento.evento_quantitativo(candles)
        self.assertEqual(resultado["event_type"], "RECUPERACAO_BAIXA")

    def test_neutro(self):
        resultado = evento.evento_quantitativo(_candles(1440))
        self.assertEqual(resultado["event_type"], "MOVIMENTO_NEUTRO")

    def test_gap_detectado(self):
        candles = _candles(1440)
        candles[-1]["open"] = candles[-2]["close"] * 1.02
        resultado = evento.evento_quantitativo(candles)
        self.assertEqual(resultado["gap_event_type"], "GAP_ALTA")
        self.assertGreater(resultado["gap_pct"], 0)

    def test_spread_ausente_e_opcional(self):
        resultado = evento.evento_quantitativo(_candles(1440), spread_pct=None)
        self.assertIsNone(resultado["spread_pct"])
        self.assertIn("spread_pct", resultado["missing_optional_fields"])
        # Sem spread ainda é um evento válido (campo opcional ausente).
        self.assertEqual(resultado["status"], "OK")

    def test_nao_usa_candle_em_formacao(self):
        # O último candle é o fechado; acrescentar "futuro" não muda o evento.
        candles = _candles(1440)
        base = evento.evento_quantitativo(candles)
        futuro = {"time": 1440 * 60, "open": 1, "high": 100000, "low": 0.1,
                  "close": 99999, "tick_volume": 1}
        estendido = evento.evento_quantitativo(candles + [futuro])
        self.assertNotEqual(estendido["last_closed_m1"], base["last_closed_m1"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
