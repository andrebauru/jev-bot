"""Fluxo real de ponta a ponta com dados sintéticos determinísticos.

Cobertura do prompt de auditoria: (a) hipótese do DeepSeek rejeitada pela
validação local; (b) hipótese validada, elegível por Operar, aprovada pelo
JEv (mock) e executada em paper — comprovando a chegada à terceira lista.
Também cobre motivos resumidos de não encaminhamento, orçamento de tempo
com continuidade incremental e cache incremental M1 (hit/miss).
"""

import tempfile
import time
from pathlib import Path
from unittest.mock import Mock, patch
import unittest

from apoio import (FonteFake, LoggerSilencioso, candles_deterministicos,
                   candles_sinteticos, estado_mt5, janela)
from jev_bot import deepseek, mercados, padroes
from jev_bot.execution.paper import Book
from jev_bot.orquestrador import Orquestrador
from jev_bot.types import Decision


class TestFluxoReal(unittest.TestCase):
    """Pipeline completo com mocks de IA e JEv; sem chamadas pagas."""

    def setUp(self):
        """Proíbe chamadas reais de IA por padrão (testes não gastam tokens)."""
        guardas = [
            patch("jev_bot.orquestrador.deepseek.analisar",
                  side_effect=AssertionError("chamada real de DeepSeek em teste")),
            patch("jev_bot.orquestrador.jev.decide",
                  side_effect=AssertionError("chamada real de JEv em teste")),
        ]
        for guarda in guardas:
            guarda.start()
            self.addCleanup(guarda.stop)

    def _orquestrador(self, pasta):
        arquivo = Path(pasta) / "mercados.json"
        mercados.salvar({"selecao": {"Crypto": {"TESTE": {"monitorar": True, "operar": True}}}},
                        arquivo)
        orch = Orquestrador(banco=Path(pasta) / "db.json",
                            pasta_dados=Path(pasta) / "padroes",
                            caminho_selecao=arquivo, relogio=time.time)
        orch.logger = LoggerSilencioso()
        orch.book = Book.load(Path(pasta) / "db.json", "USD")
        orch.book.active = True
        orch.book.prices_valid = True
        orch.book.day_start = orch.book.cash
        orch.repositorio = padroes.RepositorioPadroes(Path(pasta) / "padroes", relogio=time.time)
        orch.orcamento = deepseek.Orcamento(Path(pasta) / "padroes", 50, 500_000, relogio=time.time)
        orch.deepseek_configurado = True
        orch.jev_configurado = True
        orch.eventos = []
        orch.publicar = lambda canal, registro: orch.eventos.append((canal, registro))
        candles = candles_deterministicos()
        closes = [c["close"] for c in candles[-100:]]
        orch.fonte = FonteFake(estados=[estado_mt5(closes=closes)],
                               cotacoes={"TESTE": (99.995, 100.005, time.time())},
                               janelas={"TESTE": janela(candles)})
        orch.fonte.janela_m1 = Mock(return_value=janela(candles))
        return orch

    def _hipoteses(self):
        rejeitada = {"tipo_regra": "quedas_consecutivas", "parametros": {"n": 5},
                     "direcao": "BUY", "horizonte_minutos": 5,
                     "justificativa": "padrão que não ocorre no histórico"}
        validada = {"tipo_regra": "quedas_consecutivas", "parametros": {"n": 3},
                    "direcao": "BUY", "horizonte_minutos": 5,
                    "justificativa": "recuperação após 3 quedas"}
        return [rejeitada, validada]

    def test_e2e_rejeitada_e_validada_ate_terceira_lista(self):
        """(a) rejeitada na lista 2; (b) validada → JEv → paper → lista 3."""
        with tempfile.TemporaryDirectory() as pasta:
            orch = self._orquestrador(pasta)
            decisao = Decision("TESTE", "BUY", .9, .9)
            with patch("jev_bot.orquestrador.deepseek.analisar",
                       return_value=(self._hipoteses(), [], {"tokens": 42}, 0)) as chamada_ds, \
                    patch("jev_bot.orquestrador.jev.decide",
                          return_value=decisao) as chamada_jev:
                orch._executar_ciclo("c1")
            chamada_ds.assert_called_once()
            chamada_jev.assert_called_once()
            # (a) hipótese rejeitada por validação local (na lista DeepSeek).
            rejeicoes = [r for c, r in orch.eventos if c == "deepseek"
                         and "rejeitado" in r.get("validacao", "")]
            self.assertTrue(rejeicoes)
            self.assertTrue(any("Sem ocorrências" in r["validacao"] for r in rejeicoes),
                            "a hipótese sem ocorrências deve constar como rejeitada")
            status = {p["chave"].split("|")[0]: p["status"]
                      for p in orch.repositorio.carregar("TESTE")["padroes"]}
            self.assertIn("rejeitado", status.values())
            # (b) hipótese validada e entrada paper executada.
            fills = orch.book.fills
            self.assertEqual(len(fills), 1)
            self.assertEqual(fills[0].action, "BUY")
            self.assertEqual(fills[0].mode if hasattr(fills[0], "mode") else "paper", "paper")
            decisoes = [r for c, r in orch.eventos if c == "decisao"]
            sims = [r for r in decisoes if r.get("decisao") == "SIM"]
            self.assertEqual(len(sims), 1)
            self.assertIn("paper aberto", sims[0]["execucao"])
            self.assertIn(sims[0]["decisao"], ("SIM", "NAO", "HOLD"))
            # Motivos resumidos: rejeição estatística distinta de erro técnico.
            resumos = [r for c, r in orch.eventos if c == "resumo"]
            self.assertTrue(resumos)
            self.assertIn("executadas=1", resumos[0]["motivos"])
            self.assertIn("enviados_jev=1", resumos[0]["motivos"])
            # Chegada comprovada na TERCEIRA LISTA da interface.
            self._comprovar_terceira_lista(decisoes)

    def _comprovar_terceira_lista(self, decisoes):
        """A terceira lista mostra decisões reais; resumo vai ao status."""
        import os
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        QApplication.instance() or QApplication([])
        from jev_bot.gui.aba_operacao import AbaOperacao
        abas = AbaOperacao(acoes={k: (lambda: None) for k in
                                  ("iniciar", "pausar", "retomar", "emergencia",
                                   "liberar_risco", "aplicar_limites", "cancelar_ciclo")})
        abas.receber_lote("decisao", decisoes)
        linhas = [[abas.lista_decisao.topLevelItem(i).text(c) for c in range(8)]
                  for i in range(abas.lista_decisao.topLevelItemCount())]
        decisoes_col = [linha[5] for linha in linhas]
        self.assertIn("SIM", decisoes_col, f"decisão SIM ausente: {decisoes_col}")
        self.assertNotIn("RESUMO", decisoes_col,
                         "resumo não pode ocupar a lista de decisões")
        for linha in linhas:
            self.assertEqual(linha[1], "TESTE")  # ativo correto por decisão
        # Resumo de motivos vai para o painel de status (canal próprio).
        abas.receber_lote("resumo", [{"id": "c1", "ciclo": 1,
                                      "instante": "2026-01-01T00:00:00+00:00",
                                      "oportunidades": 0,
                                      "funil": {"sem_padrao": 88},
                                      "motivos": "sem_padrao=88", "pendentes": 0,
                                      "duracao_s": 12.0}])
        self.assertIn("0 oportunidades elegíveis", abas.resumo_ciclo.text())
        self.assertIn("sem:88", abas.resumo_ciclo.text())
        self.assertIn("sem_padrao=88", abas.resumo_ciclo.toolTip())

    def test_sem_padrao_e_somente_monitorar_nos_motivos(self):
        """Sem padrão validado e mercado só-monitorar aparecem no resumo."""
        with tempfile.TemporaryDirectory() as pasta:
            orch = self._orquestrador(pasta)
            orch.deepseek_configurado = False  # sem descoberta neste cenário
            mercados.salvar({"selecao": {"Crypto": {"TESTE": {"monitorar": True,
                                                              "operar": False}}}},
                            orch.caminho_selecao)
            orch.recarregar_selecao_arquivo()
            orch._oportunidade(estado_mt5(closes=[105.0, 104.0, 103.0]), "id")
            mercados.salvar({"selecao": {"Crypto": {"TESTE": {"monitorar": True,
                                                              "operar": True}}}},
                            orch.caminho_selecao)
            orch.recarregar_selecao_arquivo()
            orch._oportunidade(estado_mt5(closes=[105.0, 104.0, 103.0]), "id2")
            self.assertEqual(orch.contadores_ciclo.get("somente_monitorar"), 1)
            self.assertEqual(orch.contadores_ciclo.get("sem_padrao"), 1)
            # Com candidatos locais validados, o JEv pode ser consultado; o
            # que não pode acontecer é entrada sem padrão validado e sem gate.
            with patch("jev_bot.orquestrador.jev.decide",
                       return_value=Decision("TESTE", "HOLD", .5, .5)) as chamada:
                orch._executar_ciclo("c1")
            self.assertEqual(len(orch.book.fills), 0)
            if orch.repositorio.validados("TESTE"):
                chamada.assert_called()


class TestOrcamentoContinuidade(unittest.TestCase):
    """Ciclo com orçamento de tempo e continuidade incremental, sem sobreposição."""

    def _orquestrador(self, pasta, n_simbolos):
        selecao = {"selecao": {"C": {f"S{i}": {"monitorar": True, "operar": True}
                                     for i in range(n_simbolos)}}}
        arquivo = Path(pasta) / "mercados.json"
        mercados.salvar(selecao, arquivo)
        orch = Orquestrador(banco=Path(pasta) / "db.json",
                            pasta_dados=Path(pasta) / "padroes",
                            caminho_selecao=arquivo, relogio=time.time)
        orch.logger = LoggerSilencioso()
        orch.book = Book.load(Path(pasta) / "db.json", "USD")
        orch.book.active = True
        orch.repositorio = padroes.RepositorioPadroes(Path(pasta) / "padroes", relogio=time.time)
        orch.orcamento = deepseek.Orcamento(Path(pasta) / "padroes", 50, 500_000, relogio=time.time)
        orch.eventos = []
        orch.publicar = lambda canal, registro: orch.eventos.append((canal, registro))
        processados = []

        original = orch._processar_ativo

        def contando(estado, id_ciclo):
            processados.append(estado.symbol)
            return original(estado, id_ciclo)

        orch._processar_ativo = contando
        orch.processados = processados
        orch.fonte = FonteFake(estados=[estado_mt5(f"S{i}") for i in range(n_simbolos)],
                               janelas={f"S{i}": janela(candles_sinteticos(3000, seed=i + 1))
                                        for i in range(n_simbolos)},
                               cotacoes={f"S{i}": (99.99, 100.0, time.time())
                                         for i in range(n_simbolos)})
        return orch

    def test_orcamento_esgota_e_continua_no_proximo_ciclo(self):
        """Ciclo truncado registra pendências; o próximo processa pendentes primeiro."""
        with tempfile.TemporaryDirectory() as pasta:
            with patch("jev_bot.orquestrador.ORCAMENTO_CICLO_S", 0.05), \
                    patch("jev_bot.orquestrador.RESERVA_PROCESSAMENTO_S", 0.02):
                orch = self._orquestrador(pasta, 60)
                orch._executar_ciclo("c1")
                primeiro_ciclo = list(orch.processados)
                self.assertLess(len(primeiro_ciclo), 60, "orçamento deve truncar o ciclo")
                self.assertTrue(orch.pendentes_ciclo)
                self.assertTrue(any(c == "resumo" for c, _ in orch.eventos),
                                "resumo do ciclo deve existir no painel de status")
                aviso = [r for c, r in orch.eventos if c == "log"
                         and "Orçamento de tempo" in r.get("mensagem", "")]
                self.assertTrue(aviso)
                pendentes_apos_c1 = list(orch.pendentes_ciclo)
                self.assertTrue(pendentes_apos_c1)
                orch.processados.clear()
                orch.eventos.clear()
                orch._executar_ciclo("c2")
                segundo_ciclo = list(orch.processados)
                self.assertEqual(segundo_ciclo[0], pendentes_apos_c1[0],
                                 "continuidade: pendentes do ciclo anterior primeiro")
                # Sem sobreposição: símbolos do primeiro ciclo não repetem no 2º.
                self.assertFalse(set(primeiro_ciclo) & set(segundo_ciclo))

    def test_sem_sobreposicao_de_ciclos(self):
        """Ciclo em andamento adia o próximo (guarda explícita)."""
        with tempfile.TemporaryDirectory() as pasta:
            orch = self._orquestrador(pasta, 3)
            orch.ciclo_ativo = True
            orch._ciclo()
            self.assertEqual(orch.ciclo, 0)


class TestCacheIncremental(unittest.TestCase):
    """Cache M1 por símbolo: hit incremental, miss completo e integridade."""

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
        orch.repositorio = padroes.RepositorioPadroes(Path(pasta) / "padroes", relogio=time.time)
        orch.orcamento = deepseek.Orcamento(Path(pasta) / "padroes", 50, 500_000, relogio=time.time)
        orch.eventos = []
        orch.publicar = lambda canal, registro: orch.eventos.append((canal, registro))
        return orch

    def test_hit_incremental_evita_refetch_completo(self):
        """Segunda atualização usa incremento (cache hit) e não refetch completo."""
        from apoio import candles_sinteticos
        with tempfile.TemporaryDirectory() as pasta:
            orch = self._orquestrador(pasta)
            candles = candles_sinteticos(3000)
            j = janela(candles)
            orch.fonte = FonteFake(cotacoes={"TESTE": (99.99, 100.0, time.time())},
                                   janelas={"TESTE": j})
            orch.fonte.janela_m1 = Mock(return_value=j)
            orch.fonte.incremento_m1 = Mock(return_value=None)
            orch._janela_com_cache("TESTE", "id")   # miss: fetch completo + cache
            self.assertEqual(orch.fonte.janela_m1.call_count, 1)
            self.assertTrue(orch.cache_m1.caminho("TESTE").exists())
            # Agora o incremento retorna 30 candles novos: hit incremental.
            novos = [{"time": candles[-1]["time"] + (i + 1) * 60, "open": 100,
                      "high": 100.5, "low": 99.5, "close": 100.1, "tick_volume": 10}
                     for i in range(30)]
            orch.fonte.incremento_m1 = Mock(return_value=novos)
            segunda = orch._janela_com_cache("TESTE", "id")
            self.assertEqual(orch.fonte.janela_m1.call_count, 1)  # refetch completo não ocorreu
            self.assertEqual(segunda["recebidos"], 3000 + 30)
            self.assertEqual(orch.contadores_ciclo.get("cache_incremental"), 1)

    def test_lacuna_grande_invalida_e_refetch_completo(self):
        from apoio import candles_sinteticos
        with tempfile.TemporaryDirectory() as pasta:
            orch = self._orquestrador(pasta)
            candles = candles_sinteticos(3000)
            j = janela(candles)
            orch.fonte = FonteFake(cotacoes={"TESTE": (99.99, 100.0, time.time())},
                                   janelas={"TESTE": j})
            orch.fonte.janela_m1 = Mock(return_value=j)
            orch.fonte.incremento_m1 = Mock(return_value=None)
            orch._janela_com_cache("TESTE", "id")
            # Incremento com lacuna de 2 dias: cache invalidado, refetch completo.
            novos = [{"time": candles[-1]["time"] + 2 * 86400, "open": 100,
                      "high": 100.5, "low": 99.5, "close": 100.1, "tick_volume": 10}]
            orch.fonte.incremento_m1 = Mock(return_value=novos)
            orch._janela_com_cache("TESTE", "id")
            self.assertEqual(orch.fonte.janela_m1.call_count, 2)
            self.assertEqual(orch.contadores_ciclo.get("cache_invalido"), 1)

    def test_janela_restrita_aos_ultimos_30_dias(self):
        """Cache anexado é recortado: nada fora da janela móvel de 30 dias."""
        from apoio import candles_sinteticos
        with tempfile.TemporaryDirectory() as pasta:
            cache = __import__("jev_bot.cache_m1", fromlist=["CacheM1"]).CacheM1(
                Path(pasta) / "cache")
            antigos = candles_sinteticos(1000)  # janela antiga (2023)
            novos = [{"time": antigos[-1]["time"] + (i + 1) * 60, "open": 100,
                      "high": 100.5, "low": 99.5, "close": 100.1, "tick_volume": 10}
                     for i in range(43200)]  # +30 dias novos
            cache.salvar("TESTE", antigos)
            combinados, info = cache.anexar("TESTE", novos)
            self.assertFalse(info["recarregar"])
            inicio = combinados[0]["time"]
            fim = combinados[-1]["time"]
            self.assertLessEqual(fim - inicio, 30 * 86400 + 60)
            relidos = cache.carregar("TESTE")
            self.assertEqual([c["time"] for c in relidos],
                             [c["time"] for c in combinados])

    def test_arquivo_corrompido_invalida_sem_inventar(self):
        from apoio import candles_sinteticos
        with tempfile.TemporaryDirectory() as pasta:
            cache = __import__("jev_bot.cache_m1", fromlist=["CacheM1"]).CacheM1(
                Path(pasta) / "cache")
            cache.salvar("TESTE", candles_sinteticos(100))
            cache.caminho("TESTE").write_text("{corrompido", encoding="utf-8")
            self.assertEqual(cache.carregar("TESTE"), [])
            self.assertFalse(cache.caminho("TESTE").exists())

    def test_fase_no_estado_e_na_metrica(self):
        """Fase do trabalho aparece no estado (e não fica em PRONTO)."""
        with tempfile.TemporaryDirectory() as pasta:
            orch = self._orquestrador(pasta)
            candles = candles_sinteticos(3000)
            orch.fonte = FonteFake(cotacoes={"TESTE": (99.99, 100.0, time.time())},
                                   janelas={"TESTE": janela(candles)})
            orch.fonte.janela_m1 = Mock(return_value=janela(candles))
            orch.fonte.incremento_m1 = Mock(return_value=None)
            orch.dados_ativo.clear()
            orch._janela_com_cache("TESTE", "id")
            self.assertEqual(orch.fase, "BAIXANDO_HISTORICO")  # última fase do download
            orch._estado_set("ANALISANDO", "Ciclo 1", fase="BAIXANDO_HISTORICO")
            metricas = orch._metricas()
            self.assertEqual(metricas["fase"], "BAIXANDO_HISTORICO")
            estados = [r for c, r in orch.eventos if c == "estado"]
            self.assertIn("fase", estados[-1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
