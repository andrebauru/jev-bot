"""Aprovação binária do JEv com risco antes e depois (A–L), sem custo e sem ordem real.

Contrato: o Python decide a direção e pré-aprova o risco; o JEv só responde
SIM/NÃO; a execução é PAPER e a direção executada é exatamente a proposta.
"""

import dataclasses
import os
import tempfile
import threading
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
from jev_bot.risk import pre_check
from jev_bot.types import Decision

APROVADO = {"approved": True, "reason": "evidência suficiente"}

CAMPOS_PROPOSTA = {"symbol", "side_proposed", "entry_reference", "stop_loss",
                   "take_profit", "volume", "risk_amount", "risk_pct", "spread",
                   "candidate_id", "request_id", "run_id", "candle_ref", "expires_at",
                   "evidencias"}


class TesteAprovacaoBinaria(unittest.TestCase):
    """Risco pré-JEv, aprovação SIM/NÃO e execução paper direcionada pelo Python."""

    def _orquestrador(self, pasta, simbolos=("TESTE",), estados=None, n_candles=400):
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
        estados = estados or []
        listados, cotacoes, janelas = [], {}, {}
        for indice, simbolo in enumerate(simbolos):
            candles = candles_deterministicos(n=n_candles, base=100.0 + indice)
            closes = [c["close"] for c in candles[-100:]]
            listados.append(estado_mt5(simbolo, closes=closes))
            cotacoes[simbolo] = (99.995, 100.005, time.time())
            janelas[simbolo] = janela(candles)
        for extra in estados:
            listados.append(extra)
        orch.fonte = FonteFake(estados=listados, cotacoes=cotacoes, janelas=janelas)
        orch.fonte.janela_m1 = lambda simbolo, **_k: janelas.get(
            simbolo, janela(candles_deterministicos(n=n_candles)))
        self.addCleanup(orch.encerrar.set)
        self.addCleanup(lambda: orch.pool_matematico.encerrar())
        return orch

    def _ciclo_bloqueado(self, orch, construir_resposta):
        """Executa um ciclo com a resposta JEv bloqueada ate o teste liberar."""
        entrou, liberar = threading.Event(), threading.Event()

        def resposta(_payload):
            entrou.set()
            liberar.wait(3)
            return construir_resposta()

        with patch("jev_bot.orquestrador.jev.avaliar_entrada", side_effect=resposta):
            orch._executar_ciclo("c1")
        self.assertTrue(entrou.wait(2), "worker JEv nao iniciou a chamada")
        return liberar

    def _drenar(self, orch):
        limite = time.monotonic() + 4
        while (orch.analises_pendentes or orch.pendentes_jev) and time.monotonic() < limite:
            orch._drenar_analises(f"ciclo{orch.ciclo}")
            orch._processar_resultados_jev()
            time.sleep(.005)
        orch._processar_resultados_jev()

    def _ciclo(self, orch, **patch_kw):
        with patch("jev_bot.orquestrador.jev.avaliar_entrada", **patch_kw) as mock:
            orch._executar_ciclo("c1")
            self._drenar(orch)
        return mock

    def _decisoes(self, orch):
        return [r for c, r in orch.eventos if c == "decisao"]

    # A / B. BUY e SELL aprovados -> ordem paper na direção proposta.
    def test_ab_buy_e_sell_aprovados_abrem_paper(self):
        for side in ("BUY", "SELL"):
            with self.subTest(side=side), tempfile.TemporaryDirectory() as pasta:
                orch = self._orquestrador(pasta)
                # Força a direção proposta pelo Python para o lado testado.
                with patch.object(Orquestrador, "_propor_entrada",
                                  side_effect=lambda *a, **k: {"side_proposed": side,
                                                               "strategy_id": "s",
                                                               "candidate_id": "s",
                                                               "status": "validado",
                                                               "expectancy_net_pct": 0.5,
                                                               "sample_size": 10, "oos_n": 3,
                                                               "oos_rate": 0.7,
                                                               "reason_codes": []}):
                    mock = self._ciclo(orch, return_value=dict(APROVADO))
                self.assertEqual(mock.call_count, 1)
                payload = mock.call_args[0][0]
                self.assertEqual(payload["side_proposed"], side)
                self.assertTrue(CAMPOS_PROPOSTA.issubset(payload))
                self.assertEqual(len(orch.book.fills), 1)
                posicao = orch.book.fills[0]
                self.assertEqual(posicao.action, side)
                esperado = 100.005 if side == "BUY" else 99.995
                self.assertAlmostEqual(posicao.entry, esperado, places=6)
                trade = orch.book.trades[0]
                self.assertEqual(trade["side_proposed"], side)
                self.assertEqual(trade["jev_reason"], APROVADO["reason"])
                self.assertEqual(orch.contadores_sessao["paper_opened"], 1)
                self.assertEqual(orch.contadores_sessao["jev_yes"], 1)
                self.assertEqual(self._decisoes(orch)[-1]["decisao"], "SIM")
                self.assertEqual(self._decisoes(orch)[-1]["estado_api"], "COMPLETED")

    # C. JEv NO -> zero posições.
    def test_c_jev_no_zero_posicoes(self):
        with tempfile.TemporaryDirectory() as pasta:
            orch = self._orquestrador(pasta)
            self._ciclo(orch, return_value={"approved": False, "reason": "evidência fraca"})
            self.assertEqual(len(orch.book.fills), 0)
            self.assertEqual(orch.contadores_sessao["jev_no"], 1)
            self.assertEqual(orch.contadores_sessao["paper_opened"], 0)
            self.assertEqual(self._decisoes(orch)[-1]["decisao"], "NAO")

    # D. Risco pré-vetado -> zero chamadas ao JEv e zero posições.
    def test_d_risco_pre_vetado_nao_chama_jev(self):
        casos = {"lote": dataclasses.replace(estado_mt5("TESTE"), loss_per_lot=1e9),
                 "spread": dataclasses.replace(estado_mt5("TESTE"), bid=90.0, ask=110.0),
                 "stop": dataclasses.replace(estado_mt5("TESTE"), stop_distance=0.0)}
        for nome, estado in casos.items():
            with self.subTest(caso=nome), tempfile.TemporaryDirectory() as pasta:
                candles = candles_deterministicos()
                closes = [c["close"] for c in candles[-100:]]
                estado.candles = [{"time": time.time() - 60 * (len(closes) - i),
                                   "open": 100, "high": 100.5, "low": 99.5, "close": c,
                                   "tick_volume": 10} for i, c in enumerate(closes)]
                orch = self._orquestrador(pasta)
                orch.fonte.estados = lambda: iter([estado])
                with patch("jev_bot.orquestrador.jev.avaliar_entrada") as mock:
                    orch._executar_ciclo("c1")
                    self._drenar(orch)
                self.assertEqual(mock.call_count, 0, "veto pré-JEv não deve chamar o JEv")
                self.assertEqual(len(orch.book.fills), 0)
                self.assertEqual(orch.contadores_sessao.get("pre_risk_reject", 0), 1)
                motivos = {r.get("motivo") for c, r in orch.eventos if c == "oportunidade"}
                self.assertIn("RISK_REJECTED_PRE_JEV", motivos)

    # E. JEv não muda a direção; protocolo legado é rejeitado.
    def test_e_jev_nao_muda_direcao_e_legado_rejeitado(self):
        with tempfile.TemporaryDirectory() as pasta:
            orch = self._orquestrador(pasta)
            # Extra keys não alteram a direção proposta pelo Python.
            self._ciclo(orch, return_value={"approved": True, "reason": "ok",
                                            "side": "SELL", "volume": 999})
            self.assertEqual(len(orch.book.fills), 1)
            self.assertEqual(orch.book.fills[0].action, orch.book.trades[0]["side_proposed"])
        # Resposta legada/ambígua -> erro de protocolo, sem ordem.
        for resposta in ({"choice": "SELL"}, {"approved": "yes", "reason": "x"},
                         {"approved": True}, None):
            with self.subTest(resposta=resposta), tempfile.TemporaryDirectory() as pasta:
                orch = self._orquestrador(pasta)
                self._ciclo(orch, return_value=resposta)
                self.assertEqual(len(orch.book.fills), 0)
                self.assertEqual(self._decisoes(orch)[-1]["decisao"], "ERRO")
                self.assertEqual(self._decisoes(orch)[-1]["estado_api"], "ERROR_PROTOCOL")

    # F. Erros técnicos explícitos; motor continua.
    def test_f_erros_tecnicos_sem_ordem_e_motor_continua(self):
        casos = [JevError("HTTP_401", "http_error"), JevError("HTTP_429", "http_error"),
                 JevError("HTTP_500", "http_error"), JevError("JEv_TIMEOUT", "timeout"),
                 JevError("JEv_PARSE_ERROR", "parse_error")]
        for erro in casos:
            with self.subTest(erro=erro.code), tempfile.TemporaryDirectory() as pasta:
                orch = self._orquestrador(pasta)
                self._ciclo(orch, side_effect=erro)
                self.assertEqual(len(orch.book.fills), 0)
                linha = self._decisoes(orch)[-1]
                self.assertEqual(linha["decisao"], "ERRO")
                self.assertEqual(linha["estado_api"], f"ERROR_{erro.code}")
                self.assertEqual(orch.contadores_sessao["jev_error"], 1)
                # Motor segue: novo ciclo processa normalmente (novo candle).
                orch.proxima_tentativa_jev = 0
                orch.analisados_jev.clear()
                self._ciclo(orch, return_value=dict(APROVADO))
                self.assertEqual(len(orch.book.fills), 1)

    # G. YES tardio/duplicado/IDs inconsistentes -> zero posicoes.
    def test_g_candle_expirado(self):
        with tempfile.TemporaryDirectory() as pasta:
            orch = self._orquestrador(pasta)
            liberar = self._ciclo_bloqueado(orch, lambda: dict(APROVADO))
            candles = candles_deterministicos()
            closes = [c["close"] for c in candles[-100:]]
            novo_estado = estado_mt5("TESTE", closes=closes)
            novo_estado.candles[-1]["time"] += 60
            orch.fonte.revalidar = lambda _estado: novo_estado
            liberar.set()
            self._drenar(orch)
            self.assertEqual(len(orch.book.fills), 0)
            self.assertEqual(self._decisoes(orch)[-1]["decisao"], "EXPIRADO")

    def test_g_ttl_expirado(self):
        with tempfile.TemporaryDirectory() as pasta:
            orch = self._orquestrador(pasta)
            liberar = self._ciclo_bloqueado(orch, lambda: dict(APROVADO))
            real = orch.relogio
            orch.relogio = lambda: real() + 10_000
            liberar.set()
            self._drenar(orch)
            orch.relogio = real
            self.assertEqual(len(orch.book.fills), 0)
            self.assertEqual(self._decisoes(orch)[-1]["decisao"], "EXPIRADO")

    def test_g_resposta_duplicada(self):
        with tempfile.TemporaryDirectory() as pasta:
            orch = self._orquestrador(pasta)
            liberar = self._ciclo_bloqueado(orch, lambda: dict(APROVADO))
            liberar.set()
            limite = time.monotonic() + 3
            while orch.resultados_jev.empty() and time.monotonic() < limite:
                time.sleep(.005)
            rid, rrun, resp, err, lat, meta = orch.resultados_jev.get()
            orch.resultados_jev.put((rid, rrun, resp, err, lat, meta))
            orch.resultados_jev.put((rid, rrun, dict(APROVADO), None, 1, meta))
            self._drenar(orch)
            self.assertEqual(len(orch.book.fills), 1)
            self.assertGreaterEqual(
                orch.funil_total.get("response_discarded_duplicate", 0), 1)

    def test_g_ids_inconsistentes(self):
        with tempfile.TemporaryDirectory() as pasta:
            orch = self._orquestrador(pasta)
            liberar = self._ciclo_bloqueado(orch, lambda: dict(APROVADO))
            liberar.set()
            limite = time.monotonic() + 3
            while orch.resultados_jev.empty() and time.monotonic() < limite:
                time.sleep(.005)
            rid, rrun, resp, err, lat, meta = orch.resultados_jev.get()
            orch.resultados_jev.put((rid, "run-errado", dict(APROVADO), None, 1, meta))
            self._drenar(orch)
            self.assertEqual(len(orch.book.fills), 0)
            self.assertGreaterEqual(
                orch.funil_total.get("response_discarded_run_mismatch", 0), 1)

    # H. YES + risco alterado -> veto pos-JEv, zero posicoes.
    def test_h_risco_alterado_veta_pos_jev(self):
        with tempfile.TemporaryDirectory() as pasta:
            orch = self._orquestrador(pasta)
            liberar = self._ciclo_bloqueado(orch, lambda: dict(APROVADO))
            orch.book.drawdown_blocked = True  # risco mudou apos a aprovacao do JEv
            liberar.set()
            self._drenar(orch)
            self.assertEqual(len(orch.book.fills), 0)
            self.assertEqual(orch.contadores_sessao.get("post_risk_reject", 0), 1)
            linha = self._decisoes(orch)[-1]
            self.assertEqual(linha["decisao"], "SIM")
            self.assertEqual(linha["paper"], "NOT_OPENED")

    # I. Muitos símbolos e duas oportunidades -> sem duplicação/travamento.
    def test_i_muitos_simbolos_sem_duplicacao(self):
        simbolos = [f"S{i}" for i in range(90)]
        with tempfile.TemporaryDirectory() as pasta:
            orch = self._orquestrador(pasta, simbolos=simbolos)
            inicio = time.monotonic()
            self._ciclo(orch, return_value={"approved": False, "reason": "no"})
            duracao = time.monotonic() - inicio
            self.assertLess(duracao, 60)
            self.assertEqual(len(orch.book.fills), 0)
            # Nenhum símbolo recebe mais de uma resposta na mesma sessão/candle.
            self.assertLessEqual(orch.contadores_sessao["jev_no"],
                                 orch.contadores_sessao["jev_enqueued"])

    # J. GUI mostra SIM/NÃO/ERRO e veto, sem falso PENDING.
    def test_j_gui_sim_nao_erro_sem_pending(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        QApplication.instance() or QApplication([])
        from jev_bot.gui.aba_operacao import AbaOperacao
        aba = AbaOperacao(acoes={k: (lambda: None) for k in
                                 ("iniciar", "pausar", "retomar", "emergencia",
                                  "liberar_risco", "aplicar_limites", "cancelar_ciclo")})
        base = {"id": "c1", "instante": "2026-01-01T00:00:00+00:00", "padrao": "s",
                "candle_ref": 1, "direcao": "ANALISE", "justificativa": "j",
                "risco": "-", "execucao": "-"}
        aba.receber_lote("decisao", [
            {**base, "simbolo": "A", "decisao": "SIM", "estado_api": "COMPLETED",
             "side_proposed": "BUY", "paper": "OPENED"},
            {**base, "simbolo": "B", "decisao": "NAO", "estado_api": "COMPLETED",
             "side_proposed": "SELL"},
            {**base, "simbolo": "C", "decisao": "ERRO", "estado_api": "ERROR_HTTP_429",
             "side_proposed": "BUY"},
            {**base, "simbolo": "D", "decisao": "PENDING", "estado_api": "PENDING"},
        ])
        simbolos = [aba.lista_decisao.topLevelItem(i).text(0)
                    for i in range(aba.lista_decisao.topLevelItemCount())]
        self.assertEqual(simbolos, ["C", "B", "A"])  # PENDING oculto
        self.assertEqual(aba.lista_decisao.topLevelItem(2).text(2), "SIM")
        self.assertEqual(aba.lista_decisao.topLevelItem(2).text(4), "BUY")

    # K. Regressão do crash OOS None.
    def test_k_regressao_oos_none(self):
        with tempfile.TemporaryDirectory() as pasta:
            orch = self._orquestrador(pasta)
            texto = orch._texto_validacao({
                "total": {"eventos": 5, "taxa": None, "media_pct": None},
                "oos": {"eventos": 0, "taxa": None, "media_pct": None},
                "custo_pct": 0.01, "reason_code": "INSUFFICIENT_SAMPLE"})
            self.assertIn("N/D", texto)
            self.assertIn("OOS sem eventos", texto)

    # L. Nenhum caminho de ordem real.
    def test_l_sem_order_send(self):
        raiz = Path(__file__).resolve().parent.parent / "jev_bot"
        achados = []
        for arquivo in raiz.rglob("*.py"):
            if "order_send" in arquivo.read_text(encoding="utf-8"):
                achados.append(str(arquivo))
        self.assertEqual(achados, [], "nenhum caminho de ordem real é permitido")

    def test_direcao_executada_igual_a_proposta(self):
        """A direção executada é exatamente `side_proposed`, nunca invertida."""
        with tempfile.TemporaryDirectory() as pasta:
            orch = self._orquestrador(pasta)
            for side in ("BUY", "SELL"):
                orch.book.fills.clear()
                orch.book.trades.clear()
                orch.analisados_jev.clear()
                with patch.object(Orquestrador, "_propor_entrada",
                                  side_effect=lambda *a, **k: {"side_proposed": side,
                                                               "strategy_id": "s",
                                                               "candidate_id": "s",
                                                               "status": "validado",
                                                               "expectancy_net_pct": 0.5,
                                                               "sample_size": 1, "oos_n": 1,
                                                               "oos_rate": 0.7,
                                                               "reason_codes": []}):
                    self._ciclo(orch, return_value={"approved": True, "reason": "r",
                                                    "side": "SELL" if side == "BUY" else "BUY"})
                self.assertEqual(orch.book.trades[-1]["side_proposed"], side)
                self.assertEqual(orch.book.trades[-1]["action"], side)


if __name__ == "__main__":
    unittest.main(verbosity=2)
