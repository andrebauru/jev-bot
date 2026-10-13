"""Testes das regras de padrões, validação fora da amostra e repositório."""

import json
import tempfile
from pathlib import Path
import unittest

from apoio import candles_deterministicos, candles_sinteticos, janela
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

    def test_retorno_buy_sell_usa_mesmo_preco_de_entrada_como_base(self):
        candles = [{"time": 0, "close": 100.0}, {"time": 60, "close": 110.0}]
        self.assertAlmostEqual(padroes._resultado([0], "BUY", 1, candles, 0)["media_pct"], 10.0)
        self.assertAlmostEqual(padroes._resultado([0], "SELL", 1, candles, 0)["media_pct"], -10.0)
        candles[1]["close"] = 90.0
        self.assertAlmostEqual(padroes._resultado([0], "SELL", 1, candles, 0)["media_pct"], 10.0)

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


class TestNovasFamiliasM1(unittest.TestCase):
    """Famílias adicionais são dimensionais, reproduzíveis e causais."""

    @staticmethod
    def _candles(closes, spans=None):
        spans = spans or [.1] * len(closes)
        return [{"time": i * 60, "open": close,
                 "high": close + spans[i] / 2, "low": close - spans[i] / 2,
                 "close": close, "tick_volume": 1}
                for i, close in enumerate(closes)]

    def test_sinais_pos_dir_em_fechamentos_e_sem_lookahead(self):
        alta_rompimento = self._candles([100.0] * 10 + [101.0], [.1] * 10 + [.4])
        reversao = self._candles([100.0] * 10 + [94.0])
        aceleracao = self._candles(
            [100, 100.1, 100.2, 100.3, 100.4, 100.5, 101.1, 101.7,
             102.3, 102.9, 103.5])
        impulso = self._candles([100, 100.1, 101.2, 101.25, 101.27])
        expansao = self._candles([100] * 5 + [101], [.1] * 5 + [1.8])
        expansao[-1]["open"] = 100.1
        casos = (
            ("rompimento_alta", {"janela": 5}, alta_rompimento),
            ("reversao_queda", {"janela": 5, "pct": 5}, reversao),
            ("aceleracao_alta", {"janela": 5, "limiar_pct": .1}, aceleracao),
            ("impulso_pausa_alta", {"pausas": 2, "impulso_pct": .5,
                                     "pausa_pct": .2, "retrace_ratio": .5}, impulso),
            ("expansao_amplitude_alta", {"pct": 1, "compressao_pct": .2}, expansao),
        )
        for tipo, parametros, candles in casos:
            with self.subTest(estrategia=tipo):
                indices = padroes.indices_condicao(tipo, parametros, candles)
                self.assertTrue(indices)
                self.assertEqual(indices[-1], len(candles) - 1)
                candle_futuro = {"time": len(candles) * 60, "open": 1,
                                 "high": 10000, "low": .1, "close": 9000,
                                 "tick_volume": 1}
                estendidos = padroes.indices_condicao(tipo, parametros,
                                                      candles + [candle_futuro])
                self.assertEqual([i for i in estendidos if i < len(candles)], indices)
                escalados = [{**c, **{chave: c[chave] * 1000
                                      for chave in ("open", "high", "low", "close")}}
                             for c in candles]
                self.assertEqual(padroes.indices_condicao(tipo, parametros, escalados), indices)

    def test_familias_tem_limite_e_ids_estaveis(self):
        from jev_bot import candidatos, motor_matematico
        candles = candles_deterministicos()
        discovery, _ = motor_matematico.janela_descoberta(candles)
        stats = motor_matematico.estatisticas(janela(discovery), 0.02)
        regras = candidatos.regras_candidatas(stats, candles)
        self.assertLessEqual(len(regras), candidatos.MAX_LOCAL_HYPOTHESES)
        self.assertTrue(all(regra.get("strategy_id") for regra in regras))
        ids = [padroes.id_regra(padroes.chave_regra(
            r["tipo_regra"], r["parametros"], r["direcao"], r["horizonte_minutos"]))
            for r in regras]
        self.assertEqual(len(ids), len(set(ids)))

        rompimento = self._candles([100.0] * 64 + [101.0], [.1] * 64 + [.4])
        stats_contruidos = {
            "disponivel": True,
            "movimento_1min": {"p75_abs_pct": .02, "p95_abs_pct": .1,
                               "p99_abs_pct": .3, "desvio_pct": .02},
            "amplitude_1min": {"mediana_pct": .1, "p95_pct": .5},
        }
        novas = candidatos.regras_candidatas(stats_contruidos, rompimento)
        self.assertIn("range_breakout", {r["strategy_id"] for r in novas})


if __name__ == "__main__":
    unittest.main(verbosity=2)
