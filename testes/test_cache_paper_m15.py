import json
import tempfile
import time
import unittest
from pathlib import Path

from jev_bot.cache_m1 import CacheM1, CacheM15, SCHEMA


class TestCachesTimeframe(unittest.TestCase):
    def test_m1_m15_separados_e_limpeza_whitelist(self):
        with tempfile.TemporaryDirectory() as pasta:
            raiz = Path(pasta)
            m1 = CacheM1(raiz / "m1", max_dias=1, relogio=lambda: 1_000_000)
            m15 = CacheM15(raiz / "m15", max_dias=7, relogio=lambda: 1_000_000)
            candles = [{"time": 9_900, "open": 1, "high": 1.1, "low": .9,
                        "close": 1, "tick_volume": 1}]
            m1.salvar("EURUSD", candles)
            m15.salvar("EURUSD", candles)
            sentinela = raiz / "nao_apagar.json"
            sentinela.write_text("preservar", encoding="utf-8")
            # Cache gerenciável e antigo é removível; sentinela externa permanece.
            m1.caminho("EURUSD").write_text(json.dumps({"schema_version": SCHEMA,
                                                          "atualizado_em": 0,
                                                          "candles": []}), encoding="utf-8")
            resultado = m1.limpar_expirados()
            self.assertEqual(resultado["arquivos"], 1)
            self.assertTrue(sentinela.exists())
            self.assertTrue(m15.caminho("EURUSD").exists())


if __name__ == "__main__":
    unittest.main()
