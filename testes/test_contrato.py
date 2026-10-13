"""Testes do contrato DeepSeek -> JEv e das garantias matematicas do fluxo.

Cobre: payload versionado com esquema, vigencia/expiracao, evidencia
(hash amarrado a recalculo), politica de eventos nao sobrepostos,
estabilidade por subperiodo, sem lookahead/candle em formacao, memoria de
padroes com versao de algoritmo e deduplicacao de oportunidades.
"""

import json
import tempfile
import time
from pathlib import Path
from unittest.mock import Mock, patch
import unittest

from apoio import (FonteFake, LoggerSilencioso, candles_deterministicos,
                   candles_sinteticos, estado_mt5, janela)
from jev_bot import mercados, padroes
from jev_bot.execution.paper import Book
from jev_bot.orquestrador import Orquestrador
from jev_bot.types import Decision


class TestContrato(unittest.TestCase):
    """Payload versionado DeepSeek -> JEv validado por esquema."""

    def _padrao(self, candles):
        validacao = padroes.validar(
            {"tipo_regra": "quedas_consecutivas", "parametros": {"n": 3},
             "direcao": "BUY", "horizonte_minutos": 5, "justificativa": "recuperacao"},
            candles, 0.02)
        self.assertEqual(validacao["status"], "validado")
        with tempfile.TemporaryDirectory() as pasta:
            repo = padroes.RepositorioPadroes(Path(pasta), relogio=lambda: time.time())
            registro = repo.registrar("TESTE", {
                "tipo_regra": "quedas_consecutivas", "parametros": {"n": 3},
                "direcao": "BUY", "horizonte_minutos": 5, "justificativa": "recuperacao"},
                validacao, "deepseek", janela(candles))
        return registro, validacao

    def test_contrato_completo_e_valido(self):
        """Todos os campos exigidos pelo prompt de auditoria estao presentes."""
        candles = candles_deterministicos()
        registro, _ = self._padrao(candles)
        contrato = padroes.contrato_oportunidade(
            registro, candles, "c1-TESTE",
            {"equity": 10000.0, "moeda": "USD", "exposicao_fracao": 0.01,
             "max_total_risk": 0.15, "max_drawdown": 0.1, "precos_validos": True})
        ok, motivo = padroes.validar_contrato(contrato)
        self.assertTrue(ok, motivo)
        for campo in padroes.CAMPOS_CONTRATO:
            self.assertIn(campo, contrato)
        self.assertEqual(contrato["timeframe"], "M1")
        self.assertEqual(contrato["direction"], "BUY")
        self.assertEqual(contrato["pattern_id"], registro["id"])
        self.assertEqual(contrato["validation_status"], "VALIDADO")
        self.assertGreater(contrato["sample_size"], 0)
        self.assertGreater(contrato["expected_net_return"], 0)
        self.assertGreater(contrato["expiry"], time.time())
        self.assertEqual(len(contrato["evidence_hash"]), 40)
        self.assertLessEqual(contrato["window_end"] - contrato["window_start"],
                             30 * 86400 + 60)

    def test_contrato_rejeita_falhas_fechado(self):
        """Contrato invalido nao prossegue: campos, status, vigencia, amostra."""
        candles = candles_deterministicos()
        registro, _ = self._padrao(candles)
        base = padroes.contrato_oportunidade(
            registro, candles, "c1", {"equity": 10000.0})
        agora = time.time()
        casos = [
            ("schema errado", {**base, "schema_version": 99}),
            ("timeframe errado", {**base, "timeframe": "M5"}),
            ("status nao validado", {**base, "validation_status": "CANDIDATO"}),
            ("direcao invalida", {**base, "direction": "COMPRAR"}),
            ("amostra insuficiente", {**base, "sample_size": 5}),
            ("expectativa negativa", {**base, "expected_net_return": -0.1}),
            ("expirado", {**base, "expiry": agora - 10}),
            ("janela invertida", {**base, "window_start": 10, "window_end": 5}),
            ("hash invalido", {**base, "evidence_hash": "abc"}),
            ("risk_context ausente", {**base, "risk_context": None}),
            ("campo removido", {k: v for k, v in base.items()
                                if k != "reference_price"}),
        ]
        for etiqueta, payload in casos:
            with self.subTest(etiqueta=etiqueta):
                ok, motivo = padroes.validar_contrato(payload, agora=agora)
                self.assertFalse(ok, etiqueta)
                self.assertTrue(motivo)

    def test_evidence_hash_amarra_recalculo(self):
        """Evidencias identicas produzem o mesmo hash; mudanca muda o hash."""
        candles = candles_deterministicos()
        registro, _ = self._padrao(candles)
        um = padroes.contrato_oportunidade(registro, candles, "c1", {"equity": 1})
        dois = padroes.contrato_oportunidade(registro, candles, "c2", {"equity": 1})
        self.assertEqual(um["evidence_hash"], dois["evidence_hash"])
        registro["eventos"] = {**registro["eventos"], "taxa": 0.9}
        tres = padroes.contrato_oportunidade(registro, candles, "c1", {"equity": 1})
        self.assertNotEqual(um["evidence_hash"], tres["evidence_hash"])


class TestQualidadeMatematica(unittest.TestCase):
    """Eventos nao sobrepostos, estabilidade por subperiodo e sem lookahead."""

    def test_eventos_sobrepostos_sao_deduplicados(self):
        """Eventos a menos de h candles de distancia contam uma vez."""
        candles = candles_deterministicos(3000)
        regra = {"tipo_regra": "quedas_consecutivas", "parametros": {"n": 2},
                 "direcao": "BUY", "horizonte_minutos": 5}
        brutos = padroes.indices_condicao("quedas_consecutivas", {"n": 2}, candles)
        resultado = padroes.validar(regra, candles, 0.02)
        pares_consecutivos = sum(1 for a, b in zip(brutos, brutos[1:]) if b - a <= 5)
        if pares_consecutivos > 0:
            self.assertLess(resultado["total"]["eventos"], len(brutos))

    def test_estabilidade_por_subperiodo_reportada(self):
        """Metades do fora-da-amostra aparecem com n e taxa proprios."""
        candles = candles_deterministicos(6003)
        resultado = padroes.validar(
            {"tipo_regra": "quedas_consecutivas", "parametros": {"n": 3},
             "direcao": "BUY", "horizonte_minutos": 5}, candles, 0.02)
        self.assertIn("estabilidade", resultado)
        for chave in ("oos_primeira_metade", "oos_segunda_metade"):
            self.assertIn("eventos", resultado["estabilidade"][chave])
            self.assertIn("taxa", resultado["estabilidade"][chave])
        soma = (resultado["estabilidade"]["oos_primeira_metade"]["eventos"]
                + resultado["estabilidade"]["oos_segunda_metade"]["eventos"])
        self.assertEqual(soma + resultado["estabilidade"]["eventos_purgados_na_metade"],
                         resultado["oos"]["eventos"])

    def test_sem_extrapolacao_alem_da_janela(self):
        """Eventos sem i + horizonte dentro da janela nao sao contados."""
        candles = candles_deterministicos(120)
        regra = {"tipo_regra": "quedas_consecutivas", "parametros": {"n": 3},
                 "direcao": "BUY", "horizonte_minutos": 30}
        resultado = padroes.validar(regra, candles, 0.02)
        from jev_bot.padroes import indices_condicao
        indices = indices_condicao("quedas_consecutivas", {"n": 3}, candles)
        utilizaveis = [i for i in indices if i + 30 < len(candles)]
        self.assertLessEqual(resultado["total"]["eventos"], len(utilizaveis))

    def test_horizonte_de_treino_nao_vaza_para_oos(self):
        """Label de treino termina antes do corte; OOS é purgado por horizonte."""
        candles = candles_deterministicos(1000)
        for candle in candles:
            centro = candle["close"]
            candle["high"], candle["low"] = centro * 1.001, centro * .999
            candle["open"] = centro
        capturados = []
        original = padroes._resultado

        def observar(indices, *args, **kwargs):
            capturados.append(list(indices))
            return original(indices, *args, **kwargs)

        with patch("jev_bot.padroes._resultado", side_effect=observar):
            padroes.validar({"tipo_regra": "amplitude_1min_pct", "parametros": {"pct": .05},
                             "direcao": "BUY", "horizonte_minutos": 5}, candles, .01)
        corte = int(len(candles) * .7)
        train, oos, oos_1, oos_2 = capturados[1:]
        self.assertTrue(train)
        self.assertTrue(oos)
        self.assertLess(max(train) + 5, corte)
        self.assertGreaterEqual(min(oos), corte)
        meio = corte + (len(candles) - corte) // 2
        self.assertTrue(all(i + 5 < meio for i in oos_1))
        self.assertTrue(all(i >= meio for i in oos_2))

    def test_multiplas_hipoteses_reduzem_limite_wilson_oos(self):
        candles = candles_deterministicos(6003)
        regra = {"tipo_regra": "quedas_consecutivas", "parametros": {"n": 3},
                 "direcao": "BUY", "horizonte_minutos": 5}
        simples = padroes.validar(regra, candles, .02, familias_testadas=1)
        multiplas = padroes.validar(regra, candles, .02, familias_testadas=12)
        self.assertLess(multiplas["estabilidade"]["wilson_inferior_oos_ajustado"],
                        simples["estabilidade"]["wilson_inferior_oos_ajustado"])
        self.assertEqual(multiplas["familias_testadas"], 12)

    def test_candle_em_formacao_nunca_e_usado(self):
        """Janela/incremento partem de start_pos=1 (candle fechado)."""
        api = Mock()
        api.TIMEFRAME_M1 = 1
        api.copy_rates_from_pos.return_value = [
            {"time": 1_700_000_000, "open": 100, "high": 100.5, "low": 99.5,
             "close": 100.1, "tick_volume": 10}]
        from jev_bot.markets import FonteMT5
        fonte = FonteMT5(api=api, ajuste_horas=0, relogio=lambda: 1_700_000_060)
        fonte.incremento_m1("TESTE", desde=1_700_000_000 - 60)
        self.assertEqual(api.copy_rates_from_pos.call_args.args[2], 1)
        fonte.janela_m1("TESTE", dias=1, lote=100)
        self.assertEqual(api.copy_rates_from_pos.call_args_list[-1].args[2], 1)

    def test_memoria_registra_versao_do_algoritmo(self):
        """Registros de padrao carregam versao do algoritmo para rastreabilidade."""
        with tempfile.TemporaryDirectory() as pasta:
            repo = padroes.RepositorioPadroes(Path(pasta))
            regra = {"tipo_regra": "quedas_consecutivas", "parametros": {"n": 3},
                     "direcao": "BUY", "horizonte_minutos": 5}
            validacao = padroes.validar(regra, candles_deterministicos(6003), 0.02)
            registro = repo.registrar("TESTE", regra, validacao, "deepseek",
                                      janela(candles_deterministicos(6003)))
            self.assertEqual(registro["algoritmo_versao"], padroes.ALGORITMO_VERSAO)
            dados = json.loads(repo.caminho("TESTE").read_text(encoding="utf-8"))
            self.assertEqual(dados["padroes"][0]["algoritmo_versao"],
                             padroes.ALGORITMO_VERSAO)
            self.assertIn("janela_inicio", registro)
            self.assertIn("validado_em", registro)


class TestOportunidadeNoPipeline(unittest.TestCase):
    """Contrato chega ao JEv; sem padrao -> resumo honesto, sem falsa decisao."""

    def _orquestrador(self, pasta):
        arquivo = Path(pasta) / "mercados.json"
        mercados.salvar({"selecao": {"C": {"TESTE": {"monitorar": True, "operar": True}}}},
                        arquivo)
        orch = Orquestrador(banco=Path(pasta) / "db.json",
                            pasta_dados=Path(pasta) / "padroes",
                            caminho_selecao=arquivo, relogio=time.time)
        orch.logger = LoggerSilencioso()
        orch.book = Book.load(Path(pasta) / "db.json", "USD")
        orch.book.active = True
        orch.book.prices_valid = True
        orch.repositorio = padroes.RepositorioPadroes(Path(pasta) / "padroes", relogio=time.time)
        orch.orcamento = Mock()
        orch.orcamento.consumo.return_value = {}
        orch.eventos = []
        orch.publicar = lambda canal, registro: orch.eventos.append((canal, registro))
        candles = candles_deterministicos()
        closes = [c["close"] for c in candles[-100:]]
        orch.fonte = FonteFake(estados=[estado_mt5(closes=closes)],
                               cotacoes={"TESTE": (99.995, 100.005, time.time())},
                               janelas={"TESTE": janela(candles)})
        return orch, candles, closes

    def test_contrato_valido_chega_ao_jev_e_registros_tem_padrao(self):
        """Contrato anexado ao estado do JEv com todos os campos obrigatorios."""
        with tempfile.TemporaryDirectory() as pasta:
            orch, candles, closes = self._orquestrador(pasta)
            validacao = padroes.validar(
                {"tipo_regra": "quedas_consecutivas", "parametros": {"n": 3},
                 "direcao": "BUY", "horizonte_minutos": 5}, candles, 0.02)
            self.assertEqual(validacao["status"], "validado")
            registro = orch.repositorio.registrar(
                "TESTE", {"tipo_regra": "quedas_consecutivas", "parametros": {"n": 3},
                          "direcao": "BUY", "horizonte_minutos": 5, "justificativa": "x"},
                validacao, "deepseek", janela(candles))
            registro["status"] = "validado"
            orch.repositorio.salvar("TESTE", orch.repositorio.carregar("TESTE"))
            decisao = Decision("TESTE", "BUY", .9, .9)
            estado = estado_mt5(closes=closes)
            capturado = {}

            def espionar(estado_):
                capturado["oportunidade"] = estado_.mathematics.get("oportunidade")
                return decisao

            with patch("jev_bot.orquestrador.jev.decide", side_effect=espionar):
                motivo = orch._oportunidade(estado, "id-teste")
            self.assertEqual(motivo, "executada")
            contrato = capturado.get("oportunidade")
            self.assertIsNotNone(contrato)
            ok, motivo_erro = padroes.validar_contrato(contrato)
            self.assertTrue(ok, motivo_erro)
            decisoes = [r for c, r in orch.eventos if c == "decisao"
                        and r.get("decisao") == "SIM"]
            self.assertEqual(decisoes[0].get("padrao"), registro["id"])
            self.assertIsNotNone(decisoes[0].get("candle_ref"))
            self.assertIn(decisoes[0].get("direcao"), ("BUY", "SELL"))
            self.assertEqual(orch.contadores_ciclo.get("executadas"), 1)
            self.assertEqual(orch.contadores_ciclo.get("jev_sim"), 1)

    def test_jev_nao_e_condicao_ausente_contabilizados(self):
        """JEv NAO e validado-sem-condicao aparecem no resumo do ciclo."""
        with tempfile.TemporaryDirectory() as pasta:
            orch, candles, closes = self._orquestrador(pasta)
            validacao = padroes.validar(
                {"tipo_regra": "quedas_consecutivas", "parametros": {"n": 3},
                 "direcao": "BUY", "horizonte_minutos": 5}, candles, 0.02)
            registro = orch.repositorio.registrar(
                "TESTE", {"tipo_regra": "quedas_consecutivas", "parametros": {"n": 3},
                          "direcao": "BUY", "horizonte_minutos": 5, "justificativa": "x"},
                validacao, "deepseek", janela(candles))
            registro["status"] = "validado"
            orch.repositorio.salvar("TESTE", orch.repositorio.carregar("TESTE"))
            estado = estado_mt5(closes=closes)
            with patch("jev_bot.orquestrador.jev.decide",
                       return_value=Decision("TESTE", "HOLD", .5, .5)):
                motivo = orch._oportunidade(estado, "id")
            self.assertEqual(motivo, "hold")
            self.assertEqual(orch.contadores_ciclo.get("jev_hold"), 1)
            self.assertTrue(any(simbolo == "TESTE" for simbolo, _ in orch.holds))
            # AVOID: JEv decidiu NÃO (descarte imediato).
            estado2 = estado_mt5(closes=closes)
            estado2.candles[-1]["time"] += 60
            with patch("jev_bot.orquestrador.jev.decide",
                       return_value=Decision("TESTE", "AVOID", .2, .2)):
                motivo = orch._oportunidade(estado2, "id")
            self.assertEqual(motivo, "jev_nao")
            self.assertEqual(orch.contadores_ciclo.get("jev_nao"), 1)
            closes_neutras = [100.0 + (i % 3) * .1 for i in range(100)]
            estado_neutro = estado_mt5(closes=closes_neutras)
            motivo = orch._oportunidade(estado_neutro, "id2")
            self.assertEqual(motivo, "condicao_ausente")
            self.assertEqual(orch.contadores_ciclo.get("validado_condicao_ausente"), 1)

    def test_deduplicacao_de_oportunidades_por_candle(self):
        """Mesmo padrao + mesmo candle nao gera duas decisoes JEv."""
        with tempfile.TemporaryDirectory() as pasta:
            orch, candles, closes = self._orquestrador(pasta)
            validacao = padroes.validar(
                {"tipo_regra": "quedas_consecutivas", "parametros": {"n": 3},
                 "direcao": "BUY", "horizonte_minutos": 5}, candles, 0.02)
            registro = orch.repositorio.registrar(
                "TESTE", {"tipo_regra": "quedas_consecutivas", "parametros": {"n": 3},
                          "direcao": "BUY", "horizonte_minutos": 5, "justificativa": "x"},
                validacao, "deepseek", janela(candles))
            registro["status"] = "validado"
            orch.repositorio.salvar("TESTE", orch.repositorio.carregar("TESTE"))
            decisao = Decision("TESTE", "BUY", .9, .9)
            estado = estado_mt5(closes=closes)
            with patch("jev_bot.orquestrador.jev.decide", return_value=decisao) as chamada:
                orch._oportunidade(estado, "id-a")
                orch._oportunidade(estado, "id-b")
            chamada.assert_called_once()  # deduplicacao por (simbolo, padrao, candle)


if __name__ == "__main__":
    unittest.main(verbosity=2)
