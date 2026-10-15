"""Execução paper ponta a ponta com JEv mockado (sem custo de API, sem ordem real).

Cobre o prompt de correção A–J: BUY/SELL viram ordens paper com fill e
histórico; HOLD não gera ordem; bloqueio de risco preserva a decisão;
candle expirado descarta com motivo; erros de API têm estado próprio
(nunca mascarados como HOLD); None em UI/log não derruba o motor e None
em preço/risco rejeita a entrada; idempotência por candidato; persistência
sem duplicação; múltiplos símbolos com atualização de GUI.
"""

import dataclasses
import os
import tempfile
import time
from pathlib import Path
from unittest.mock import patch
import unittest

from apoio import (FonteFake, LoggerSilencioso, candles_deterministicos,
                   estado_mt5, janela)
from jev_bot import mercados, padroes
from jev_bot.execution.paper import Book
from jev_bot.jev import JevError
from jev_bot.orquestrador import Orquestrador
from jev_bot.risk import check as risk_check
from jev_bot.types import Decision

VALIDACAO = {
    "status": "validado", "motivo": "Evidência dentro e fora da amostra",
    "reason_code": "QUANT_VALIDATED", "reason_codes": ["QUANT_VALIDATED"],
    "total": {"eventos": 100, "acertos": 70, "taxa": .7, "media_pct": 0.5,
              "mediana_pct": 0.5, "desvio_pct": .1},
    "amostra": {"eventos": 70, "taxa": .7, "media_pct": 0.5},
    "oos": {"eventos": 30, "taxa": .7, "media_pct": 0.6},
    "estabilidade": {"wilson_inferior_oos_ajustado": .5},
    "custo_pct": .05, "cost_basis": "spread_only",
    "wilson_inferior": .5, "familias_testadas": 1}


class TesteExecucaoPaper(unittest.TestCase):
    """Encadeamento técnico: candidato → JEv → risco → executor paper."""

    def _orquestrador(self, pasta, simbolos=("TESTE",)):
        arquivo = Path(pasta) / "mercados.json"
        mercados.salvar({"selecao": {"C": {s: {"monitorar": True, "operar": True}
                                            for s in simbolos}}}, arquivo)
        orch = Orquestrador(banco=Path(pasta) / "db.json",
                            pasta_dados=Path(pasta) / "padroes",
                            caminho_selecao=arquivo, relogio=time.time)
        orch.logger = LoggerSilencioso()
        orch.book = Book.load(Path(pasta) / "db.json", "USD")
        orch.book.active = True
        orch.book.prices_valid = True
        orch.jev_configurado = True
        orch.repositorio = padroes.RepositorioPadroes(Path(pasta) / "padroes")
        orch.eventos = []
        orch.publicar = lambda canal, registro: orch.eventos.append((canal, registro))
        estados, cotacoes, janelas = [], {}, {}
        for indice, simbolo in enumerate(simbolos):
            candles = candles_deterministicos(base=100.0 + indice)
            closes = [c["close"] for c in candles[-100:]]
            estados.append(estado_mt5(simbolo, closes=closes))
            cotacoes[simbolo] = (99.995, 100.005, time.time())
            janelas[simbolo] = janela(candles)
        orch.fonte = FonteFake(estados=estados, cotacoes=cotacoes, janelas=janelas)
        orch.fonte.janela_m1 = lambda simbolo, **_k: janelas[simbolo]
        self.addCleanup(orch.encerrar.set)
        self.addCleanup(lambda: orch.pool_matematico.encerrar())
        return orch

    def _drenar(self, orch):
        limite = time.monotonic() + 3
        while (orch.analises_pendentes or orch.pendentes_jev) and time.monotonic() < limite:
            orch._drenar_analises(f"ciclo{orch.ciclo}")
            orch._processar_resultados_jev()
            time.sleep(.005)
        orch._processar_resultados_jev()

    def _ciclo_validado(self, orch, decisao):
        """Ciclo com validação local positiva e resposta JEv controlada."""
        with patch.object(Orquestrador, "_validar_candidato_m1",
                          return_value=VALIDACAO), \
                patch("jev_bot.orquestrador.jev.decide", return_value=decisao) as jev:
            orch._executar_ciclo("c1")
            self._drenar(orch)
        return jev

    # A. BUY válido + risco aprovado → ordem paper, fill, posição e histórico.
    def test_a_buy_valido_abre_ordem_paper(self):
        with tempfile.TemporaryDirectory() as pasta:
            orch = self._orquestrador(pasta)
            self._ciclo_validado(orch, Decision("TESTE", "BUY", .9, .9))
            self.assertEqual(len(orch.book.fills), 1)
            posicao = orch.book.fills[0]
            self.assertEqual(posicao.action, "BUY")
            self.assertAlmostEqual(posicao.entry, 100.005, places=6)  # ask observado
            self.assertGreater(posicao.volume, 0)
            self.assertGreater(posicao.risk_amount, 0)
            trade = orch.book.trades[-1]
            self.assertEqual(trade["status"], "OPEN")
            self.assertEqual(trade["candidate_id"], "analise")
            self.assertEqual(trade["candle_ref"], orch.analises_memoria["TESTE"]["candle_time"])
            self.assertAlmostEqual(trade["fill_price"], posicao.entry, places=6)
            self.assertEqual(trade["expected_net_pct"], 0.5)     # previsto registrado
            self.assertAlmostEqual(trade["spread_pct"],
                                   orch.analises_memoria["TESTE"]["cost_spread_pct"], places=8)
            self.assertEqual(orch.funil_total.get("paper_opened"), 1)
            self.assertEqual(orch.contadores_sessao["decision_buy"], 1)

    # B. SELL válido + risco aprovado → 1 ordem paper SELL.
    def test_b_sell_valido_abre_ordem_paper(self):
        with tempfile.TemporaryDirectory() as pasta:
            orch = self._orquestrador(pasta)
            self._ciclo_validado(orch, Decision("TESTE", "SELL", .8, .8))
            self.assertEqual(len(orch.book.fills), 1)
            posicao = orch.book.fills[0]
            self.assertEqual(posicao.action, "SELL")
            self.assertAlmostEqual(posicao.entry, 99.995, places=6)  # bid observado
            self.assertEqual(orch.book.trades[-1]["status"], "OPEN")

    # C. HOLD → 0 ordens; estado da API COMPLETED; motivo claro.
    def test_c_hold_nao_abre_ordem_e_api_completa(self):
        with tempfile.TemporaryDirectory() as pasta:
            orch = self._orquestrador(pasta)
            self._ciclo_validado(orch, Decision("TESTE", "HOLD", .5, .5))
            self.assertEqual(len(orch.book.fills), 0)
            linhas = [r for c, r in orch.eventos if c == "decisao"]
            self.assertEqual(linhas[-1]["decisao"], "HOLD")
            self.assertEqual(linhas[-1]["estado_api"], "COMPLETED")
            self.assertEqual(linhas[-1]["paper"], "NOT_OPENED")
            self.assertIn("JEv HOLD", linhas[-1]["justificativa"])
            self.assertEqual(orch.contadores_sessao["decision_hold"], 1)
            top = orch._metricas()["diagnostico_funil"]["motivos_nao_execucao_top3"]
            self.assertIn(("JEv_HOLD", 1), [(m["codigo"], m["quantidade"]) for m in top])

    # D. BUY + risco reprovado → 0 ordens, rejeição rastreável.
    def test_d_risco_reprovado_preserva_decisao_sem_ordem(self):
        with tempfile.TemporaryDirectory() as pasta:
            orch = self._orquestrador(pasta)
            orch.book.drawdown_blocked = True  # bloqueio determinístico real
            # O autostart suspende tudo com bloqueios; o veto por decisão é
            # testado mantendo o motor ativo (mesmo gate real de risco).
            with patch.object(Orquestrador, "_avaliar_autostart",
                              lambda self, origem=None: setattr(self, "pausado", False)):
                self._ciclo_validado(orch, Decision("TESTE", "BUY", .9, .9))
            self.assertEqual(len(orch.book.fills), 0)
            linhas = [r for c, r in orch.eventos if c == "decisao"]
            self.assertEqual(linhas[-1]["decisao"], "JEV_BUY_RISK_BLOCKED")
            self.assertEqual(linhas[-1]["estado_api"], "COMPLETED")
            self.assertEqual(linhas[-1]["decisao_jev"], "BUY")
            self.assertEqual(orch.funil_total.get("risk_veto"), 1)

    # E. Candle expirado → 0 ordens, motivo explícito STALE.
    def test_e_candle_expirado_descarta_resposta(self):
        with tempfile.TemporaryDirectory() as pasta:
            orch = self._orquestrador(pasta)
            candles = candles_deterministicos()
            closes = [c["close"] for c in candles[-100:]]
            novo = estado_mt5("TESTE", closes=closes)
            novo.candles[-1]["time"] += 60  # candle M1 seguinte durante a espera
            orch.fonte.revalidar = lambda _estado: novo
            self._ciclo_validado(orch, Decision("TESTE", "BUY", .9, .9))
            self.assertEqual(len(orch.book.fills), 0)
            self.assertEqual(orch.funil_total.get("response_discarded_stale"), 1)
            linhas = [r for c, r in orch.eventos if c == "decisao"]
            self.assertEqual(linhas[-1]["decisao"], "STALE")
            self.assertEqual(linhas[-1]["estado_api"], "DISCARDED_STALE")

    # F. Erros de API/timeout/JSON inválido → estados próprios, nunca HOLD.
    def test_f_erros_de_api_tem_estado_proprio(self):
        casos = [(JevError("HTTP_401", "http_error"), "ERROR_HTTP_401"),
                 (JevError("JEv_TIMEOUT", "timeout"), "ERROR_JEv_TIMEOUT"),
                 (JevError("JEv_PARSE_ERROR", "parse_error"), "ERROR_JEv_PARSE_ERROR")]
        for erro, estado_esperado in casos:
            with self.subTest(erro=erro.code), tempfile.TemporaryDirectory() as pasta:
                orch = self._orquestrador(pasta)
                with patch.object(Orquestrador, "_validar_candidato_m1",
                                  return_value=VALIDACAO), \
                        patch("jev_bot.orquestrador.jev.decide", side_effect=erro):
                    orch._executar_ciclo("c1")
                    self._drenar(orch)
                self.assertEqual(len(orch.book.fills), 0)
                linhas = [r for c, r in orch.eventos if c == "decisao"]
                self.assertEqual(linhas[-1]["decisao"], "ERRO_API")
                self.assertEqual(linhas[-1]["estado_api"], estado_esperado)
                self.assertNotEqual(linhas[-1]["decisao"], "HOLD")
                self.assertEqual(orch.contadores_sessao["paper_opened"], 0)

    # G. None em UI/log não derruba; None em preço/risco rejeita com segurança.
    def test_g_none_nao_derruba_motor_e_risco_rejeita(self):
        with tempfile.TemporaryDirectory() as pasta:
            orch = self._orquestrador(pasta)
            validacao_none = {**VALIDACAO,
                              "total": dict(VALIDACAO["total"], taxa=None),
                              "oos": {"eventos": 0, "acertos": 0, "taxa": None,
                                      "media_pct": None}}
            texto = orch._texto_validacao(validacao_none)  # regressão do crash
            self.assertIn("N/D", texto)
            self.assertIn("OOS sem eventos", texto)
            estado_sem_preco = dataclasses.replace(estado_mt5("TESTE"), bid=None, ask=None)
            gate = risk_check(Decision("TESTE", "BUY", .9, .9), orch.book,
                              orch.book.limits, estado_sem_preco)
            self.assertNotEqual(gate.verdict, "EXECUTE")
            self.assertIn("inválid", gate.reason)
            # Motor continua após falha recuperável de aplicação de análise.
            with patch.object(Orquestrador, "_validar_candidato_m1",
                              return_value=VALIDACAO), \
                    patch.object(Orquestrador, "_aplicar_matematica",
                                 side_effect=TypeError("unsupported format string "
                                                       "passed to NoneType.__format__")):
                orch._executar_ciclo("c1")  # não propaga: erro é recuperável
            self.assertEqual(orch.estados_ativos.get("TESTE"), "ERROR")
            motivos = [r.get("motivo") for c, r in orch.eventos if c == "oportunidade"]
            self.assertIn("SEM_ELEGIBILIDADE", motivos)
            # O motor segue operável: um novo ciclo roda normalmente.
            with patch.object(Orquestrador, "_validar_candidato_m1",
                              return_value=VALIDACAO), \
                    patch("jev_bot.orquestrador.jev.decide",
                          return_value=Decision("TESTE", "HOLD", .5, .5)) as jev:
                orch._executar_ciclo("c2")
                self._drenar(orch)
            jev.assert_called()

    def test_g_none_na_interface_exibe_nd(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        QApplication.instance() or QApplication([])
        from jev_bot.gui.aba_operacao import AbaOperacao
        aba = AbaOperacao(acoes={k: (lambda: None) for k in
                                 ("iniciar", "pausar", "retomar", "emergencia",
                                  "liberar_risco", "aplicar_limites", "cancelar_ciclo")})
        aba.receber_metricas({"estado": "PRONTO", "detalhe": "", "ativo": True,
                              "moeda": "USD", "equity": None, "saldo": None, "aberto": None,
                              "exposicao": None, "exposicao_fracao": None,
                              "drawdown": None, "drawdown_max": None, "posicoes": 0,
                              "limites": {}, "conta": {"currency": "USD", "balance": None,
                                                       "equity": None, "profit": None},
                              "diagnostico_funil": {}})
        self.assertIn("N/D", aba.valores["equity"].text())
        self.assertIn("N/D", aba.conta_real.text())

    # H. Duas respostas para o mesmo candidato → no máximo uma ordem.
    def test_h_idempotencia_por_candidato(self):
        with tempfile.TemporaryDirectory() as pasta:
            orch = self._orquestrador(pasta)
            with patch.object(Orquestrador, "_validar_candidato_m1",
                              return_value=VALIDACAO), \
                    patch("jev_bot.orquestrador.jev.decide",
                          return_value=Decision("TESTE", "BUY", .9, .9)) as jev:
                orch._executar_ciclo("c1")
                # Entrega duplicada da MESMA resposta antes do processamento.
                request_id = (f"{orch.run_id_atual}:TESTE:analise:"
                              f"{orch.analises_memoria['TESTE']['candle_time']}")
                orch.resultados_jev.put((request_id, orch.run_id_atual,
                                         Decision("TESTE", "BUY", .9, .9), None, 5,
                                         {"run_id": orch.run_id_atual, "symbol": "TESTE",
                                          "candidate_id": "analise",
                                          "candle_ref": orch.analises_memoria["TESTE"]["candle_time"]}))
                self._drenar(orch)
            jev.assert_called_once()
            self.assertEqual(len(orch.book.fills), 1)
            self.assertGreaterEqual(orch.funil_total.get("response_discarded_duplicate"), 1)

    # I. Persistência e restauração de posição/trade sem duplicação.
    def test_i_persistencia_sem_duplicacao(self):
        with tempfile.TemporaryDirectory() as pasta:
            orch = self._orquestrador(pasta)
            self._ciclo_validado(orch, Decision("TESTE", "BUY", .9, .9))
            orch._persistir()
            restaurado = Book.load(Path(pasta) / "db.json", "USD")
            self.assertEqual(len(restaurado.fills), 1)
            self.assertEqual(len(restaurado.trades), 1)
            restaurado.save(Path(pasta) / "db.json")
            novamente = Book.load(Path(pasta) / "db.json", "USD")
            self.assertEqual(len(novamente.fills), 1)
            self.assertEqual(len(novamente.trades), 1)
            self.assertEqual(restaurado.trades[0]["trade_id"],
                             novamente.trades[0]["trade_id"])

    # J. Múltiplos símbolos, filas e atualização de GUI sem congelamento.
    def test_j_multipla_simbolos_e_gui(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        QApplication.instance() or QApplication([])
        from jev_bot.gui.aba_operacao import AbaOperacao
        with tempfile.TemporaryDirectory() as pasta:
            orch = self._orquestrador(pasta, simbolos=("TESTE", "TESTE2"))
            with patch.object(Orquestrador, "_validar_candidato_m1",
                              return_value=VALIDACAO), \
                    patch("jev_bot.orquestrador.jev.decide",
                          side_effect=lambda estado: Decision(estado.symbol, "BUY", .9, .9)):
                orch._executar_ciclo("c1")
                self._drenar(orch)
            self.assertEqual(len(orch.book.fills), 2)
            self.assertEqual({p.symbol for p in orch.book.fills}, {"TESTE", "TESTE2"})
            self.assertEqual(orch.contadores_sessao["paper_opened"], 2)
            aba = AbaOperacao(acoes={k: (lambda: None) for k in
                                     ("iniciar", "pausar", "retomar", "emergencia",
                                      "liberar_risco", "aplicar_limites", "cancelar_ciclo")})
            registros = [{"id": f"c{i}", "simbolo": f"S{i}",
                          "instante": "2026-01-01T00:00:00+00:00", "padrao": "analise",
                          "candle_ref": i, "direcao": "ANALISE", "decisao": "HOLD",
                          "estado_api": "COMPLETED", "policy": "HOLD",
                          "justificativa": "j", "risco": "-", "execucao": "-"}
                         for i in range(60)]
            aba.receber_lote("decisao", registros)
            self.assertEqual(aba.lista_decisao.topLevelItemCount(), 60)
            self.assertEqual(aba.lista_decisao.topLevelItem(0).text(3), "COMPLETED")

    def test_estados_api_distintos_na_interface(self):
        """PENDING nunca aparece como estado de API; COMPLETED/ERROR aparecem."""
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        QApplication.instance() or QApplication([])
        from jev_bot.gui.aba_operacao import AbaOperacao
        aba = AbaOperacao(acoes={k: (lambda: None) for k in
                                 ("iniciar", "pausar", "retomar", "emergencia",
                                  "liberar_risco", "aplicar_limites", "cancelar_ciclo")})
        base = {"id": "c1", "instante": "2026-01-01T00:00:00+00:00", "padrao": "analise",
                "candle_ref": 1, "direcao": "ANALISE", "justificativa": "j",
                "risco": "-", "execucao": "-"}
        aba.receber_lote("decisao", [
            {**base, "simbolo": "A", "decisao": "HOLD", "estado_api": "COMPLETED",
             "policy": "HOLD"},
            {**base, "simbolo": "B", "decisao": "ERRO_API", "estado_api": "ERROR_HTTP_429",
             "policy": "ERROR"},
            {**base, "simbolo": "C", "decisao": "STALE", "estado_api": "DISCARDED_STALE",
             "policy": "STALE"},
            {**base, "simbolo": "D", "decisao": "PENDING", "estado_api": "PENDING",
             "policy": "PENDING"},  # estado pendente nunca é exibido como decisão
        ])
        simbolos = [aba.lista_decisao.topLevelItem(i).text(0)
                    for i in range(aba.lista_decisao.topLevelItemCount())]
        self.assertEqual(simbolos, ["C", "B", "A"])  # mais recente no topo; PENDING oculto
        estados = [aba.lista_decisao.topLevelItem(i).text(3) for i in range(3)]
        self.assertEqual(estados, ["DISCARDED_STALE", "ERROR_HTTP_429", "COMPLETED"])

    def test_sessao_separada_de_historico(self):
        """Contadores por sessão começam zerados e não herdam acumulados."""
        with tempfile.TemporaryDirectory() as pasta:
            orch = self._orquestrador(pasta)
            self.assertEqual(sum(orch.contadores_sessao.values()), 0)
            self._ciclo_validado(orch, Decision("TESTE", "HOLD", .5, .5))
            metricas = orch._metricas()["diagnostico_funil"]
            self.assertGreater(metricas["sessao"].get("decision_hold", 0), 0)
            self.assertGreater(metricas["sessao"].get("queue_enqueued", 0), 0)
            self.assertLess(metricas["sessao"].get("paper_opened", 0), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
