"""Testes da seleção de mercados: persistência, consistência e filtro no motor."""

import json
import tempfile
import time
from pathlib import Path
from unittest.mock import Mock, patch
import unittest

from apoio import FonteFake, LoggerSilencioso, candles_sinteticos, estado_mt5, janela
from jev_bot import deepseek, mercados, padroes
from jev_bot.execution.paper import Book
from jev_bot.orquestrador import Orquestrador
from jev_bot.types import Decision


class TestModeloSelecao(unittest.TestCase):
    """Normalização, persistência atômica e consultas rápidas."""

    def test_normalizar_garante_operar_exige_monitorar(self):
        dados = {"selecao": {"Crypto": {
            "BTCUSD": {"monitorar": True, "operar": True},
            "ETHUSD": {"monitorar": False, "operar": True},  # inconsistente
        }}}
        normal = mercados.normalizar(dados)
        btc = normal["selecao"]["Crypto"]["BTCUSD"]
        eth = normal["selecao"]["Crypto"]["ETHUSD"]
        self.assertEqual((btc["monitorar"], btc["operar"]), (True, True))
        self.assertEqual((eth["monitorar"], eth["operar"]), (False, False))

    def test_carregar_salvar_e_consultar(self):
        with tempfile.TemporaryDirectory() as pasta:
            arquivo = Path(pasta) / "mercados.json"
            mercados.salvar({"selecao": {"Crypto": {"BTCUSD": {"monitorar": True, "operar": True}}}},
                            arquivo)
            selecao = mercados.carregar(arquivo)
            self.assertTrue(selecao.monitorar("btcusd"))  # maiúsculas indiferente
            self.assertTrue(selecao.operar("BTCUSD"))
            self.assertFalse(selecao.monitorar("ETHUSD"))
            monitorados, operaveis = selecao.contagens()
            self.assertEqual((monitorados, operaveis), (1, 1))

    def test_arquivo_corrompido_nao_trava(self):
        with tempfile.TemporaryDirectory() as pasta:
            arquivo = Path(pasta) / "mercados.json"
            arquivo.write_text("{ quebrado", encoding="utf-8")
            selecao = mercados.carregar(arquivo)
            self.assertTrue(selecao.vazia())
            self.assertTrue(arquivo.with_suffix(".json.corrupto").exists())

    def test_arquivo_ausente_e_conservador(self):
        with tempfile.TemporaryDirectory() as pasta:
            selecao = mercados.carregar(Path(pasta) / "inexistente.json")
            self.assertTrue(selecao.vazia())
            self.assertEqual(selecao.contagens(), (0, 0))

    def test_diferencas_limitadas(self):
        a = mercados.SelecaoMercados({"selecao": {"C": {"A": {"monitorar": True, "operar": True},
                                                       "B": {"monitorar": True, "operar": False}}}})
        b = mercados.SelecaoMercados({"selecao": {"C": {"A": {"monitorar": True, "operar": False},
                                                       "B": {"monitorar": True, "operar": False},
                                                       "C2": {"monitorar": True, "operar": True}}}})
        mudancas = b.diferencas(a)
        self.assertEqual(set(mudancas), {"A", "C2"})

    def test_cache_do_catalogo(self):
        with tempfile.TemporaryDirectory() as pasta:
            arquivo = Path(pasta) / "cache.json"
            mercados.salvar_cache({"Crypto": ["BTCUSD"]}, arquivo)
            self.assertEqual(mercados.carregar_cache(arquivo), {"Crypto": ["BTCUSD"]})

    def test_descoberta_usa_so_o_catalogo_real(self):
        fonte = Mock()
        fonte.catalogo_por_categoria.return_value = {"Forex": ["EURUSD"]}
        self.assertEqual(mercados.catalogo_por_categoria(fonte), {"Forex": ["EURUSD"]})
        fonte.catalogo_por_categoria.side_effect = RuntimeError
        self.assertEqual(mercados.catalogo_por_categoria(fonte), {})


class TestSelecaoNoMotor(unittest.TestCase):
    """Gating no orquestrador: histórico, DeepSeek e JEv por seleção."""

    def _orquestrador(self, pasta, selecao_dados):
        arquivo = Path(pasta) / "mercados.json"
        mercados.salvar(selecao_dados, arquivo)
        orch = Orquestrador(banco=Path(pasta) / "db.json",
                            pasta_dados=Path(pasta) / "padroes",
                            caminho_selecao=arquivo, relogio=time.time)
        orch.logger = LoggerSilencioso()
        orch.pausado = False  # cenário de robô iniciado (INICIAR já executado)
        orch.book = Book.load(Path(pasta) / "db.json", "USD")
        orch.book.active = True
        orch.book.prices_valid = True
        orch.book.day_start = orch.book.cash
        orch.repositorio = padroes.RepositorioPadroes(Path(pasta) / "padroes", relogio=time.time)
        orch.orcamento = deepseek.Orcamento(Path(pasta) / "padroes", 50, 500_000, relogio=time.time)
        orch.eventos = []
        orch.publicar = lambda canal, registro: orch.eventos.append((canal, registro))
        return orch

    def test_sem_selecao_nada_e_analisado(self):
        with tempfile.TemporaryDirectory() as pasta:
            orch = self._orquestrador(pasta, {})
            fonte = FonteFake(estados=[estado_mt5("TESTE")],
                              janelas={"TESTE": janela(candles_sinteticos(3000))})
            fonte.janela_m1 = Mock(return_value=janela(candles_sinteticos(3000)))
            orch.fonte = fonte
            orch._executar_ciclo("c1")
            fonte.janela_m1.assert_not_called()
            calculados = [r for c, r in orch.eventos if c == "calc" and r.get("etapa") == "calculado"]
            self.assertFalse(any(r.get("simbolo") == "TESTE" for r in calculados))

    def test_monitorado_sem_operar_chega_ate_analise_mas_nao_ao_jev(self):
        with tempfile.TemporaryDirectory() as pasta:
            orch = self._orquestrador(pasta, {"selecao": {"C": {
                "TESTE": {"monitorar": True, "operar": False}}}})
            orch.fonte = FonteFake(estados=[estado_mt5("TESTE")],
                                   cotacoes={"TESTE": (99.995, 100.005, time.time())},
                                   janelas={"TESTE": janela(candles_sinteticos(3000, seed=3))})
            orch._executar_ciclo("c1")
            calculados = [r for c, r in orch.eventos if c == "calc" and r.get("etapa") == "calculado"]
            self.assertTrue(any(r.get("simbolo") == "TESTE" for r in calculados))
            candles = candles_sinteticos(60, seed=3)
            for j, preco in enumerate((110.0, 108.0, 106.0, 104.0, 102.0, 100.0), start=54):
                candles[j]["close"] = preco
                candles[j]["open"] = preco + 1
                candles[j]["high"] = preco + 1.5
                candles[j]["low"] = preco - 0.5
            validacao = {"status": "validado", "motivo": "ok",
                         "total": {"eventos": 50, "acertos": 40, "taxa": .8, "media_pct": .1,
                                   "mediana_pct": .1, "desvio_pct": .2},
                         "amostra": {"eventos": 35}, "oos": {"eventos": 15, "taxa": .8},
                         "wilson_inferior": .5, "custo_pct": .02}
            registro = orch.repositorio.registrar(
                "TESTE", {"tipo_regra": "quedas_consecutivas", "parametros": {"n": 3},
                          "direcao": "BUY", "horizonte_minutos": 5}, validacao, "deepseek",
                janela(candles))
            registro["status"] = "validado"
            orch.repositorio.salvar("TESTE", orch.repositorio.carregar("TESTE"))
            estado = estado_mt5(closes=[c["close"] for c in candles[-6:]])
            estado.candles[-1]["time"] = candles[-1]["time"]
            with patch("jev_bot.orquestrador.jev.decide") as chamada:
                orch._oportunidade(estado, "id")
            chamada.assert_not_called()
            self.assertEqual(orch.book.fills, [])

    def test_mudanca_durante_processamento_cancela_pendencias(self):
        """Símbolo desmarcado no meio da execução é ignorado no próximo ciclo."""
        with tempfile.TemporaryDirectory() as pasta:
            orch = self._orquestrador(pasta, {"selecao": {"C": {
                "TESTE": {"monitorar": True, "operar": True}}}})
            fonte = FonteFake(estados=[estado_mt5("TESTE")],
                              janelas={"TESTE": janela(candles_sinteticos(3000, seed=5))})
            fonte.janela_m1 = Mock(return_value=janela(candles_sinteticos(3000, seed=5)))
            orch.fonte = fonte
            orch._executar_ciclo("c1")
            self.assertEqual(fonte.janela_m1.call_count, 1)
            mercados.salvar({"selecao": {}}, orch.caminho_selecao)
            orch.recarregar_selecao_arquivo()
            orch._executar_ciclo("c2")
            self.assertEqual(fonte.janela_m1.call_count, 1)
            monitorados, operaveis = orch.selecao.contagens()
            self.assertEqual((monitorados, operaveis), (0, 0))

    def test_contagens_na_metrica(self):
        with tempfile.TemporaryDirectory() as pasta:
            orch = self._orquestrador(pasta, {"selecao": {"C": {
                "BTCUSD": {"monitorar": True, "operar": True},
                "ETHUSD": {"monitorar": True, "operar": False}}}})
            orch.fonte = FonteFake()
            orch.ultimo_monitor = 0
            orch._monitorar()
            metricas = [r for c, r in orch.eventos if c == "metricas"][-1]
            self.assertEqual(metricas["monitorados"], 2)
            self.assertEqual(metricas["operaveis"], 1)

    def test_nenhuma_ordem_real_por_selecao(self):
        """Operar em paper apenas: nenhum caminho envia ordem real."""
        with tempfile.TemporaryDirectory() as pasta:
            orch = self._orquestrador(pasta, {"selecao": {"C": {
                "TESTE": {"monitorar": True, "operar": True}}}})
            self.assertFalse(hasattr(orch, "order_send"))
            fonte = FonteFake()
            fonte.order_send = Mock()
            orch.fonte = fonte
            orch._oportunidade(estado_mt5(), "id")
            fonte.order_send.assert_not_called()


class TestPainelMercados(unittest.TestCase):
    """Interface da seleção: árvore, consistência Operar⇒Monitorar e persistência."""

    @classmethod
    def setUpClass(cls):
        import os
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _painel(self, pasta):
        from jev_bot.gui.mercados_widget import PainelMercados
        return PainelMercados(caminho_selecao=Path(pasta) / "mercados.json")

    def test_catalogo_descoberto_e_expansao_sob_demanda(self):
        with tempfile.TemporaryDirectory() as pasta:
            painel = self._painel(pasta)
            painel.preencher_catalogo({"categorias": {"Crypto": ["BTCUSD", "ETHUSD"],
                                                      "Forex": ["EURUSD"]}, "origem": "ao vivo"})
            self.assertEqual(painel.arvore.topLevelItemCount(), 2)
            no = painel.arvore.topLevelItem(0)
            self.assertEqual(no.childCount(), 0)  # filhos só na expansão
            no.setExpanded(True)
            self.assertEqual(no.childCount(), 2)

    def test_operar_exige_monitorar_na_interface(self):
        with tempfile.TemporaryDirectory() as pasta:
            painel = self._painel(pasta)
            painel.preencher_catalogo({"categorias": {"Crypto": ["BTCUSD"]}, "origem": "ao vivo"})
            no = painel.arvore.topLevelItem(0)
            no.setExpanded(True)
            filho = no.child(0)
            from PySide6.QtCore import Qt
            filho.setCheckState(2, Qt.Checked)   # Operar sem Monitorar
            self.assertEqual(filho.checkState(1), Qt.Checked)  # Monitorar ligado junto
            filho.setCheckState(1, Qt.Unchecked)  # desmarcar Monitorar
            self.assertEqual(filho.checkState(2), Qt.Unchecked)  # desliga Operar

    def test_marcar_visiveis_respeita_filtro(self):
        with tempfile.TemporaryDirectory() as pasta:
            painel = self._painel(pasta)
            painel.preencher_catalogo({"categorias": {"Crypto": ["BTCUSD", "ETHUSD"]},
                                       "origem": "ao vivo"})
            no = painel.arvore.topLevelItem(0)
            no.setExpanded(True)
            painel.filtro.setText("BTC")
            painel._filtrar_agora()
            painel._marcar_visiveis(True)
            from PySide6.QtCore import Qt
            btc = no.child(0)
            eth = no.child(1)
            self.assertEqual(btc.checkState(1), Qt.Checked)
            self.assertEqual(eth.checkState(1), Qt.Unchecked)  # filtrado fora
            self.assertEqual(eth.checkState(2), Qt.Unchecked)

    def test_persistencia_roundtrip(self):
        with tempfile.TemporaryDirectory() as pasta:
            painel = self._painel(pasta)
            painel.preencher_catalogo({"categorias": {"Crypto": ["BTCUSD"]}, "origem": "ao vivo"})
            no = painel.arvore.topLevelItem(0)
            no.setExpanded(True)
            from PySide6.QtCore import Qt
            no.child(0).setCheckState(1, Qt.Checked)
            no.child(0).setCheckState(2, Qt.Checked)
            painel._salvar()
            arquivo = Path(pasta) / "mercados.json"
            self.assertTrue(arquivo.exists())
            dados = json.loads(arquivo.read_text(encoding="utf-8"))
            self.assertEqual(dados["selecao"]["Crypto"]["BTCUSD"],
                             {"monitorar": True, "operar": True})
            painel2 = self._painel(pasta)
            painel2.preencher_catalogo({"categorias": {"Crypto": ["BTCUSD"]}, "origem": "ao vivo"})
            no2 = painel2.arvore.topLevelItem(0)
            no2.setExpanded(True)
            self.assertEqual(no2.child(0).checkState(1), Qt.Checked)
            self.assertEqual(no2.child(0).checkState(2), Qt.Checked)

    def test_categoria_nao_expandida_aplica_ao_catalogo_completo(self):
        """Tick em categoria NÃO expandida vale para todos os símbolos dela."""
        with tempfile.TemporaryDirectory() as pasta:
            painel = self._painel(pasta)
            painel.preencher_catalogo({"categorias": {"Crypto": ["BTCUSD", "ETHUSD"],
                                                      "Forex": ["EURUSD"]}, "origem": "ao vivo"})
            no = painel.arvore.topLevelItem(0)  # Crypto, SEM expandir
            from PySide6.QtCore import Qt
            no.setCheckState(1, Qt.Checked)  # Monitorar na categoria
            painel._salvar()
            dados = json.loads((Path(pasta) / "mercados.json").read_text(encoding="utf-8"))
            crypto = dados["selecao"]["Crypto"]
            self.assertEqual(crypto["BTCUSD"], {"monitorar": True, "operar": False})
            self.assertEqual(crypto["ETHUSD"], {"monitorar": True, "operar": False})
            self.assertNotIn("Forex", dados["selecao"])
            # Expansão depois: filhos nascem marcados conforme o tick da categoria.
            painel2 = self._painel(pasta)
            painel2.preencher_catalogo({"categorias": {"Crypto": ["BTCUSD", "ETHUSD"]},
                                        "origem": "ao vivo"})
            no2 = painel2.arvore.topLevelItem(0)
            no2.setExpanded(True)
            self.assertEqual(no2.child(0).checkState(1), Qt.Checked)
            self.assertEqual(no2.child(1).checkState(1), Qt.Checked)

    def test_selecao_salva_por_simbolo_tem_prioridade_na_expansao(self):
        """Estado salvo por símbolo prevalece sobre o tick da categoria."""
        with tempfile.TemporaryDirectory() as pasta:
            painel = self._painel(pasta)
            painel.preencher_catalogo({"categorias": {"Crypto": ["BTCUSD", "ETHUSD"]},
                                       "origem": "ao vivo"})
            no = painel.arvore.topLevelItem(0)
            no.setExpanded(True)
            from PySide6.QtCore import Qt
            no.child(0).setCheckState(1, Qt.Checked)   # apenas BTCUSD monitorar
            no.child(0).setCheckState(2, Qt.Checked)   # BTCUSD operar
            painel._salvar()
            painel2 = self._painel(pasta)
            painel2.preencher_catalogo({"categorias": {"Crypto": ["BTCUSD", "ETHUSD"]},
                                        "origem": "ao vivo"})
            no2 = painel2.arvore.topLevelItem(0)
            no2.setCheckState(1, Qt.Checked)  # operador marca a categoria INTEIRA depois
            no2.setExpanded(True)
            self.assertEqual(no2.child(0).checkState(2), Qt.Checked)   # BTCUSD operar (salvo)
            self.assertEqual(no2.child(1).checkState(1), Qt.Checked)   # ETHUSD monitorar (categoria)

    def test_sem_invencao_de_ativos_offline(self):
        """Sem catálogo, o painel não cria categorias fictícias."""
        with tempfile.TemporaryDirectory() as pasta:
            painel = self._painel(pasta)
            painel.preencher_catalogo({"categorias": {}, "origem": "cache (terminal indisponível)"})
            self.assertEqual(painel.arvore.topLevelItemCount(), 0)
            self.assertIn("cache", painel.status.text())


if __name__ == "__main__":
    unittest.main(verbosity=2)
