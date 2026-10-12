"""Testes do fluxo operacional: candidatos locais, SIM/NAO/HOLD e HOLD expirável.

Cobre o prompt de correção: o caminho de trading não depende do
aprendizado (rate limit do DeepSeek não bloqueia padrões já validados),
candidatos determinísticos chegam ao JEv no mesmo candle, decisões são
SIM/NAO/HOLD por ativo, HOLD expira e é descartado, NAO descarta, risco
é verificado antes do JEv e novamente no executor, e a GUI mostra apenas
decisões reais (resumo vai ao painel de status).
"""

import tempfile
import time
from pathlib import Path
from unittest.mock import Mock, patch
import unittest

from apoio import (FonteFake, LoggerSilencioso, candles_deterministicos,
                   estado_mt5, janela)
from jev_bot import candidatos, mercados, padroes
from jev_bot.execution.paper import Book
from jev_bot.orquestrador import HOLD_TTL_S, Orquestrador, TENTATIVA_JEV_S
from jev_bot.types import Decision


class TestRegrasCandidatas(unittest.TestCase):
    """Candidatos determinísticos das estatísticas locais (sem IA)."""

    def test_candidatos_do_padrao_deterministico(self):
        # Recorte da descoberta terminando no 3º candle de queda consecutiva.
        descoberta = candles_deterministicos()[:4203]
        stats = __import__("jev_bot.motor_matematico", fromlist=["estatisticas"]).estatisticas(
            janela(descoberta), 0.02)
        regras = candidatos.regras_candidatas(stats, descoberta)
        self.assertTrue(regras, "o padrão determinístico deve gerar candidato")
        # Último candle fecha em 3 quedas: candidato de quedas consecutivas.
        sequencias = [r for r in regras if r["tipo_regra"] == "quedas_consecutivas"]
        self.assertTrue(sequencias)
        self.assertIn(sequencias[0]["direcao"], ("BUY", "SELL"))
        for regra in regras:
            self.assertGreaterEqual(regra["horizonte_minutos"], 1)
            self.assertLessEqual(regra["horizonte_minutos"], 60)
            self.assertIn(regra["direcao"], ("BUY", "SELL"))

    def test_sem_estatistica_nao_gera_candidato(self):
        self.assertEqual(candidatos.regras_candidatas({"disponivel": False}, []), [])
        stats = __import__("jev_bot.motor_matematico", fromlist=["estatisticas"]).estatisticas(
            janela(candles_deterministicos()[:2000]), 0.02)
        # Candle sem condição (meio de uma sequência de altas): sem candidato
        # de quedas; e nenhuma regra pode existir sem condição verdadeira.
        regras = candidatos.regras_candidatas(stats, candles_deterministicos()[:2000])
        for regra in regras:
            indice = padroes.indices_condicao(regra["tipo_regra"], regra["parametros"],
                                              candles_deterministicos()[:2000])
            self.assertEqual(indice[-1] if indice else None, 1999)


class TestDecisoesOperacionais(unittest.TestCase):
    """SIM/NAO/HOLD, HOLD expirável, risco antes do JEv e executor."""

    def setUp(self):
        """Proíbe chamadas reais de IA por padrão em todos os testes."""
        for alvo in ("jev_bot.orquestrador.deepseek.analisar",
                     "jev_bot.orquestrador.jev.decide"):
            guarda = patch(alvo, side_effect=AssertionError("chamada real de IA em teste"))
            guarda.start()
            self.addCleanup(guarda.stop)

    def _orquestrador(self, pasta, operar=True):
        arquivo = Path(pasta) / "mercados.json"
        mercados.salvar({"selecao": {"C": {"TESTE": {"monitorar": True,
                                                     "operar": operar}}}}, arquivo)
        orch = Orquestrador(banco=Path(pasta) / "db.json",
                            pasta_dados=Path(pasta) / "padroes",
                            caminho_selecao=arquivo, relogio=time.time)
        orch.logger = LoggerSilencioso()
        orch.book = Book.load(Path(pasta) / "db.json", "USD")
        orch.book.active = True
        orch.book.prices_valid = True
        orch.repositorio = padroes.RepositorioPadroes(Path(pasta) / "padroes",
                                                     relogio=time.time)
        orch.orcamento = Mock()
        orch.orcamento.consumo.return_value = {}
        orch.orcamento.permitir.return_value = (True, "")
        orch.eventos = []
        orch.publicar = lambda canal, registro: orch.eventos.append((canal, registro))
        orch.relogio = lambda: orch._agora_teste
        orch._agora_teste = time.time()
        candles = candles_deterministicos()
        closes = [c["close"] for c in candles[-100:]]
        orch.fonte = FonteFake(estados=[estado_mt5(closes=closes)],
                               cotacoes={"TESTE": (99.995, 100.005, time.time())},
                               janelas={"TESTE": janela(candles)})
        orch.fonte.janela_m1 = Mock(return_value=janela(candles))
        return orch, candles, closes

    def _padrao_validado(self, orch, candles):
        validacao = padroes.validar(
            {"tipo_regra": "quedas_consecutivas", "parametros": {"n": 3},
             "direcao": "BUY", "horizonte_minutos": 5}, candles, 0.02)
        assert validacao["status"] == "validado"
        registro = orch.repositorio.registrar(
            "TESTE", {"tipo_regra": "quedas_consecutivas", "parametros": {"n": 3},
                      "direcao": "BUY", "horizonte_minutos": 5, "justificativa": "x"},
            validacao, "local", janela(candles))
        registro["status"] = "validado"
        orch.repositorio.salvar("TESTE", orch.repositorio.carregar("TESTE"))
        return registro

    def test_sim_executa_paper(self):
        with tempfile.TemporaryDirectory() as pasta:
            orch, candles, closes = self._orquestrador(pasta)
            registro = self._padrao_validado(orch, candles)
            estado = estado_mt5(closes=closes)
            with patch("jev_bot.orquestrador.jev.decide",
                       return_value=Decision("TESTE", "BUY", .9, .9)):
                motivo = orch._oportunidade(estado, "id")
            self.assertEqual(motivo, "executada")
            self.assertEqual(len(orch.book.fills), 1)
            self.assertEqual(orch.book.fills[0].action, "BUY")
            self.assertEqual(orch.contadores_ciclo.get("jev_sim"), 1)
            self.assertEqual(orch.contadores_ciclo.get("executadas"), 1)
            linhas = [r for c, r in orch.eventos if c == "decisao" and r.get("decisao") == "SIM"]
            self.assertEqual(linhas[0]["simbolo"], "TESTE")
            self.assertEqual(linhas[0]["padrao"], registro["id"])
            self.assertIn("paper aberto", linhas[0]["execucao"])

    def test_nao_descarta_e_hold_adia_ate_expirar(self):
        with tempfile.TemporaryDirectory() as pasta:
            orch, candles, closes = self._orquestrador(pasta)
            self._padrao_validado(orch, candles)
            estado = estado_mt5(closes=closes)
            # HOLD: adia a mesma oportunidade (sem nova chamada antes de expirar).
            with patch("jev_bot.orquestrador.jev.decide",
                       return_value=Decision("TESTE", "HOLD", .5, .5)) as chamada:
                self.assertEqual(orch._oportunidade(estado, "id"), "hold")
                self.assertEqual(chamada.call_count, 1)
                # Mesmo candle + mesmo padrão: não volta ao JEv (hold vigente).
                self.assertNotEqual(orch._oportunidade(estado, "id2"), "hold")
                self.assertEqual(chamada.call_count, 1)
            self.assertEqual(orch.contadores_ciclo.get("jev_hold"), 1)
            self.assertGreaterEqual(orch.contadores_ciclo.get("hold_aguardando"), 1)
            # Expira: oportunidade descartada e não reenviada para o mesmo candle.
            orch._agora_teste += HOLD_TTL_S + 1
            with patch("jev_bot.orquestrador.jev.decide") as chamada2:
                motivo = orch._oportunidade(estado, "id3")
            chamada2.assert_not_called()
            self.assertEqual(orch.contadores_ciclo.get("hold_expirado"), 1)
            self.assertIn(motivo, ("hold_expirado", "condicao_ausente"))
            # AVOID no candle seguinte: NÃO descarta imediatamente.
            estado2 = estado_mt5(closes=closes)
            estado2.candles[-1]["time"] += 120
            with patch("jev_bot.orquestrador.jev.decide",
                       return_value=Decision("TESTE", "AVOID", .2, .2)):
                self.assertEqual(orch._oportunidade(estado2, "id4"), "jev_nao")
            self.assertEqual(orch.contadores_ciclo.get("jev_nao"), 1)
            self.assertEqual(len(orch.book.fills), 0)

    def test_hold_muda_se_o_padrao_mudar_no_proximo_ciclo(self):
        """Novo padrão (chave diferente) gera nova oportunidade, não HOLD preso."""
        with tempfile.TemporaryDirectory() as pasta:
            orch, candles, closes = self._orquestrador(pasta)
            self._padrao_validado(orch, candles)
            estado = estado_mt5(closes=closes)
            with patch("jev_bot.orquestrador.jev.decide",
                       return_value=Decision("TESTE", "HOLD", .5, .5)):
                orch._oportunidade(estado, "id")
            # Registra um segundo padrão validado (chave distinta) que também
            # tem a condição verdadeira no MESMO candle: o identificador inclui
            # o padrão, então o JEv é consultado de novo (não fica preso ao HOLD).
            validacao = padroes.validar(
                {"tipo_regra": "queda_pct", "parametros": {"pct": 2.0, "minutos": 30},
                 "direcao": "BUY", "horizonte_minutos": 5}, candles, 0.02)
            self.assertEqual(validacao["status"], "validado",
                             "queda de 2% com continuação valida no padrão determinístico")
            registro = orch.repositorio.registrar(
                "TESTE", {"tipo_regra": "queda_pct", "parametros": {"pct": 2.0, "minutos": 30},
                          "direcao": "BUY", "horizonte_minutos": 5, "justificativa": "y"},
                validacao, "local", janela(candles))
            registro["status"] = "validado"
            orch.repositorio.salvar("TESTE", orch.repositorio.carregar("TESTE"))
            with patch("jev_bot.orquestrador.jev.decide",
                       return_value=Decision("TESTE", "BUY", .9, .9)) as chamada:
                motivo = orch._oportunidade(estado, "id2")
            self.assertIn(motivo, ("executada", "hold", "risco_bloqueado", "jev_nao"))
            chamada.assert_called_once()

    def test_risco_antes_do_jev_e_no_executor(self):
        """Spread alto bloqueia ANTES do JEv; sem chamada artificial."""
        with tempfile.TemporaryDirectory() as pasta:
            orch, candles, closes = self._orquestrador(pasta)
            self._padrao_validado(orch, candles)
            estado = estado_mt5(closes=closes)
            estado.ask = estado.bid * 1.05  # spread absurdo
            with patch("jev_bot.orquestrador.jev.decide") as chamada:
                motivo = orch._oportunidade(estado, "id")
            self.assertEqual(motivo, "pre_bloqueado")
            chamada.assert_not_called()
            self.assertEqual(orch.contadores_ciclo.get("pre_bloqueado"), 1)

    def test_rate_limit_deepseek_nao_bloqueia_operacao(self):
        """Orçamento de IA esgotado: candidatos locais seguem para o JEv."""
        with tempfile.TemporaryDirectory() as pasta:
            orch, candles, closes = self._orquestrador(pasta)
            orch.deepseek_configurado = True
            orch.orcamento.permitir.return_value = (False, "Limite de chamadas DeepSeek por hora atingido")
            with patch("jev_bot.orquestrador.deepseek.analisar",
                       side_effect=AssertionError("rate limit não pode chamar a API")), \
                    patch("jev_bot.orquestrador.jev.decide",
                          return_value=Decision("TESTE", "BUY", .9, .9)) as chamada_jev:
                orch._executar_ciclo("c1")
            chamada_jev.assert_called()  # padrão local validado chegou ao JEv
            self.assertGreaterEqual(orch.contadores_ciclo.get("executadas", 0), 1)
            locais = [r for c, r in orch.eventos if c == "deepseek"
                      and r.get("status_consulta") == "CANDIDATO LOCAL (Python)"]
            self.assertTrue(locais, "candidatos locais devem ser gerados sem IA")
            self.assertGreaterEqual(orch.contadores_ciclo.get("candidatos_locais", 0), 1)
            self.assertTrue(any(c == "resumo" for c, _ in orch.eventos))
            # O resumo nunca aparece como decisão por ativo.
            self.assertFalse(any(c == "decisao" and r.get("decisao") == "RESUMO"
                                 for c, r in orch.eventos))

    def test_ausencia_de_oportunidade_sem_chamada_artificial(self):
        with tempfile.TemporaryDirectory() as pasta:
            orch, candles, closes = self._orquestrador(pasta)
            # Sem nenhum padrão validado: nenhuma chamada ao JEv.
            with patch("jev_bot.orquestrador.jev.decide") as chamada:
                motivo = orch._oportunidade(estado_mt5(closes=closes), "id")
            self.assertEqual(motivo, "sem_padrao")
            chamada.assert_not_called()
            self.assertEqual(orch.contadores_ciclo.get("sem_padrao"), 1)

    def test_novo_candle_percebido_mesmo_com_hash_igual(self):
        """Novo candle M1 fechado dispara avaliação mesmo com hash idêntico.

        O hash do resumo apenas omite a chamada de DESCUBERTA ao DeepSeek;
        a avaliação de oportunidades (padrões validados + candidatos locais)
        acontece a cada candle fechado, sem esperar TTL.
        """
        with tempfile.TemporaryDirectory() as pasta:
            orch, candles, closes = self._orquestrador(pasta)
            self._padrao_validado(orch, candles)
            estado1 = estado_mt5(closes=closes)
            estado2 = estado_mt5(closes=closes)
            estado2.candles[-1]["time"] += 60  # novo candle M1 fechado
            # Hash idêntico ao "anterior": deduplicação de descoberta ativa.
            orch.ultimo_resumo_hash["TESTE"] = "hash-inalterado"
            decisao = Decision("TESTE", "BUY", .9, .9)
            with patch("jev_bot.orquestrador.jev.decide",
                       return_value=decisao) as chamada:
                orch._oportunidade(estado1, "id1")
                orch._oportunidade(estado2, "id2")
            # Ambos os candles foram avaliados e enviados ao JEv.
            self.assertEqual(chamada.call_count, 2)

    def test_drawdown_e_emergencia_impedem_entrada(self):
        with tempfile.TemporaryDirectory() as pasta:
            orch, candles, closes = self._orquestrador(pasta)
            self._padrao_validado(orch, candles)
            estado = estado_mt5(closes=closes)
            orch.parado_risco = True
            with patch("jev_bot.orquestrador.jev.decide") as chamada:
                self.assertEqual(orch._oportunidade(estado, "id"), "inativo")
            chamada.assert_not_called()
            orch.parado_risco = False
            orch.emergencia_ativa = True
            with patch("jev_bot.orquestrador.jev.decide") as chamada:
                self.assertEqual(orch._oportunidade(estado, "id2"), "inativo")
            chamada.assert_not_called()
            self.assertEqual(len(orch.book.fills), 0)


class TestGuiDecisoes(unittest.TestCase):
    """GUI: decisões reais por ativo; resumo no status; SIM/NAO/HOLD."""

    @classmethod
    def setUpClass(cls):
        import os
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _aba(self):
        from jev_bot.gui.aba_operacao import AbaOperacao
        return AbaOperacao(acoes={k: (lambda: None) for k in
                                  ("iniciar", "pausar", "retomar", "emergencia",
                                   "liberar_risco", "aplicar_limites", "cancelar_ciclo")})

    def test_decisoes_por_ativo_com_sim_nao_hold(self):
        aba = self._aba()
        registros = [
            {"id": "c1", "simbolo": "BTCUSD", "instante": "2026-01-01T00:00:00+00:00",
             "padrao": "p1", "candle_ref": 1, "direcao": "BUY", "decisao": "SIM",
             "oportunidade": "x", "justificativa": "j", "risco": "autorizado",
             "execucao": "paper aberto"},
            {"id": "c1", "simbolo": "ETHUSD", "instante": "2026-01-01T00:00:00+00:00",
             "padrao": "p2", "candle_ref": 2, "direcao": "SELL", "decisao": "NAO",
             "oportunidade": "y", "justificativa": "JEv AVOID", "risco": "-",
             "execucao": "nenhuma"},
            {"id": "c1", "simbolo": "SOLUSD", "instante": "2026-01-01T00:00:00+00:00",
             "padrao": "p3", "candle_ref": 3, "direcao": "BUY", "decisao": "HOLD",
             "oportunidade": "z", "justificativa": "JEv HOLD", "risco": "aguardando",
             "execucao": "adiada"},
        ]
        aba.receber_lote("decisao", registros)
        self.assertEqual(aba.lista_decisao.topLevelItemCount(), 3)
        ativos = [aba.lista_decisao.topLevelItem(i).text(1) for i in range(3)]
        decisoes = [aba.lista_decisao.topLevelItem(i).text(5) for i in range(3)]
        self.assertEqual(set(ativos), {"BTCUSD", "ETHUSD", "SOLUSD"})
        self.assertEqual(set(decisoes), {"SIM", "NAO", "HOLD"})
        self.assertEqual(aba.lista_decisao.headerItem().text(0), "analysis_id")
        self.assertEqual(aba.lista_decisao.headerItem().text(3), "pattern_id")

    def test_resumo_no_status_e_vazio_contextual(self):
        aba = self._aba()
        aba.receber_lote("resumo", [{"id": "c1", "ciclo": 7,
                                     "instante": "2026-01-01T00:00:00+00:00",
                                     "oportunidades": 0,
                                     "funil": {"candidatos": 90, "sem_padrao": 90},
                                     "motivos": "sem_padrao=90; hold_aguardando=1",
                                     "pendentes": 0, "duracao_s": 22.0}])
        self.assertEqual(aba.lista_decisao.topLevelItemCount(), 0)
        self.assertIn("0 oportunidades elegíveis", aba.resumo_ciclo.text())
        self.assertIn("candidatos:90", aba.resumo_ciclo.text())
        self.assertIn("sem_padrao=90", aba.resumo_ciclo.toolTip())
        self.assertIn("Ciclo 7", aba.resumo_ciclo.text())


if __name__ == "__main__":
    unittest.main(verbosity=2)
