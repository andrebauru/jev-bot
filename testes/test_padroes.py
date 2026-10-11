"""Testes das regras de padrões, validação fora da amostra e repositório."""

import json
import tempfile
from pathlib import Path
import unittest

from apoio import candles_sinteticos, janela
from jev_bot import padroes


class TestPadroes(unittest.TestCase):
    """Regras reproduzíveis, validação fora da amostra e repositório."""

    def test_normalizacao_aceita_e_descarta(self):
        hipotese = {"tipo_regra": "quedas_consecutivas", "parametros": {"n": 3},
                    "direcao": "buy", "horizonte_minutos": 5.0, "justificativa": "x" * 600}
        normal = padroes.normalizar_hipotese(hipotese)
        self.assertEqual(normal["direcao"], "BUY")
        self.assertEqual(normal["horizonte_minutos"], 5)
        self.assertEqual(len(normal["justificativa"]), 500)
        for quebra in ({"tipo_regra": "rsi_sobrecomprado", "parametros": {}, "direcao": "BUY",
                        "horizonte_minutos": 5},
                       {"tipo_regra": "quedas_consecutivas", "parametros": {"n": 99},
                        "direcao": "BUY", "horizonte_minutos": 5},
                       {"tipo_regra": "quedas_consecutivas", "parametros": {"n": 2},
                        "direcao": "comprar", "horizonte_minutos": 5},
                       {"tipo_regra": "quedas_consecutivas", "parametros": {"n": 2},
                        "direcao": "BUY", "horizonte_minutos": 120}):
            with self.assertRaises(padroes.ErroRegra):
                padroes.normalizar_hipotese(quebra)

    def test_condicao_deterministica(self):
        candles = candles_sinteticos(60, seed=3, preco=100)
        for j, preco in enumerate((110.0, 108.0, 106.0, 104.0, 102.0, 100.0), start=54):
            candles[j]["close"] = preco
            candles[j]["open"] = preco + 1
            candles[j]["high"] = preco + 1.5
            candles[j]["low"] = preco - 0.5
        indices = padroes.indices_condicao("quedas_consecutivas", {"n": 3}, candles)
        self.assertEqual(indices[-1], len(candles) - 1)
        self.assertTrue(padroes.condicao_atual(
            {"regra": {"tipo": "quedas_consecutivas", "parametros": {"n": 3},
                       "direcao": "BUY", "horizonte_minutos": 5}}, candles))

    def test_validacao_respeita_minimo_e_oos(self):
        regra = {"tipo_regra": "quedas_consecutivas", "parametros": {"n": 2},
                 "direcao": "SELL", "horizonte_minutos": 3}
        resultado = padroes.validar(regra, candles_sinteticos(20000, seed=5), 0.02)
        self.assertIn(resultado["status"], ("validado", "rejeitado"))
        if resultado["total"]["eventos"] >= padroes.MIN_EVENTOS and \
                resultado["oos"]["eventos"] >= padroes.MIN_EVENTOS_OOS:
            self.assertLess(resultado["oos"]["eventos"], resultado["total"]["eventos"])
        rejeitado = padroes.validar(regra, candles_sinteticos(50, seed=5), 0.02)
        self.assertEqual(rejeitado["status"], "rejeitado")
        self.assertIn("amostra", rejeitado["motivo"].casefold())

    def test_repositorio_deduplica_persiste_e_expira(self):
        with tempfile.TemporaryDirectory() as pasta:
            repo = padroes.RepositorioPadroes(Path(pasta), relogio=lambda: 1_000_000)
            regra = {"tipo_regra": "quedas_consecutivas", "parametros": {"n": 2},
                     "direcao": "BUY", "horizonte_minutos": 5, "justificativa": "t"}
            validacao = {"status": "validado", "motivo": "ok",
                         "total": {"eventos": 50, "acertos": 40, "taxa": .8, "media_pct": .1,
                                   "mediana_pct": .1, "desvio_pct": .2},
                         "amostra": {"eventos": 35}, "oos": {"eventos": 15},
                         "wilson_inferior": .5, "custo_pct": .02}
            j = janela(candles_sinteticos(400))
            r1 = repo.registrar("TESTE", regra, validacao, "deepseek", j)
            r2 = repo.registrar("TESTE", regra, validacao, "deepseek", j)
            self.assertEqual(r1["id"], r2["id"])
            dados = json.loads((Path(pasta) / "TESTE.json").read_text(encoding="utf-8"))
            self.assertEqual(len(dados["padroes"]), 1)
            self.assertEqual(dados["padroes"][0]["custo_spread_pct"], .02)
            self.assertEqual(len(repo.validados("TESTE")), 1)
            dados["padroes"][0]["validado_em"] -= padroes.VALIDADE_DIAS * 86400 + 10
            (Path(pasta) / "TESTE.json").write_text(json.dumps(dados), encoding="utf-8")
            self.assertEqual(len(repo.validados("TESTE")), 0)
            self.assertEqual(repo.carregar("TESTE")["padroes"][0]["status"], "expirado")

    def test_revisao_deepseek_somente_expira(self):
        with tempfile.TemporaryDirectory() as pasta:
            repo = padroes.RepositorioPadroes(Path(pasta), relogio=lambda: 1_000_000)
            regra = {"tipo_regra": "altas_consecutivas", "parametros": {"n": 2},
                     "direcao": "BUY", "horizonte_minutos": 5}
            validacao = {"status": "validado", "motivo": "ok", "total": {"eventos": 50, "acertos": 40},
                         "amostra": {"eventos": 35}, "oos": {"eventos": 15},
                         "wilson_inferior": .5, "custo_pct": .02}
            repo.registrar("TESTE", regra, validacao, "deepseek", janela(candles_sinteticos(400)))
            aplicadas = repo.aplicar_revisoes(
                "TESTE", [{"id": repo.validados("TESTE")[0]["id"], "acao": "expirar", "motivo": "x"}])
            self.assertEqual(len(aplicadas), 1)
            self.assertEqual(len(repo.validados("TESTE")), 0)
            self.assertEqual(repo.aplicar_revisoes(
                "TESTE", [{"id": "inexistente", "acao": "expirar", "motivo": ""}]), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
