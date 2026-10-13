"""Testes de responsividade do dashboard: coalescência, limites e I/O fora da UI."""

import tempfile
import time
from pathlib import Path
from unittest.mock import Mock

import unittest

from apoio import LoggerSilencioso
from jev_bot.gui.aba_operacao import ListaAnalise, MAX_LINHAS
from jev_bot.orquestrador import Coalescedor, Orquestrador


def _orquestrador(pasta):
    """Orquestrador real com dobro de fonte e logger silencioso."""
    from jev_bot import deepseek, mercados, padroes
    from jev_bot.execution.paper import Book
    arquivo = Path(pasta) / "mercados.json"
    mercados.salvar({"selecao": {"C": {"TESTE": {"monitorar": True, "operar": True}}}}, arquivo)
    orch = Orquestrador(banco=Path(pasta) / "db.json",
                        pasta_dados=Path(pasta) / "padroes",
                        caminho_selecao=arquivo)
    orch.logger = LoggerSilencioso()
    orch.book = Book.load(Path(pasta) / "db.json", "USD")
    orch.book.active = True
    orch.repositorio = padroes.RepositorioPadroes(Path(pasta) / "padroes")
    orch.orcamento = deepseek.Orcamento(Path(pasta) / "padroes", 50, 500_000)
    entregas = []
    orch.entrega = lambda pacote: entregas.append(pacote)
    orch._entregas = entregas
    return orch


class TestCoalescedor(unittest.TestCase):
    """Publicações coalescidas: teto por canal e registros únicos."""

    def test_teto_de_lista_e_registros_unicos(self):
        coalescedor = Coalescedor()
        for i in range(500):
            coalescedor.enviar("calc", {"id": i})
        coalescedor.enviar("metricas", {"equity": 1})
        coalescedor.enviar("metricas", {"equity": 2})  # substitui
        pacote = coalescedor.descarregar()
        self.assertLessEqual(len(pacote["calc"]), Coalescedor.TETOS["calc"])
        self.assertEqual(pacote["metricas"], {"equity": 2})
        self.assertIsNone(coalescedor.descarregar())  # descarregou tudo

    def test_engine_emite_um_pacote_por_flush(self):
        """300 publicações viram uma entrega única na interface."""
        with tempfile.TemporaryDirectory() as pasta:
            orch = _orquestrador(pasta)
            for i in range(300):
                orch.publicar("oportunidade", {"i": i})
            orch.descarregar()
            self.assertEqual(len(orch._entregas), 1)
            self.assertEqual(len(orch._entregas[0]["oportunidade"]), Coalescedor.TETOS["oportunidade"])
            orch.descarregar()
            self.assertEqual(len(orch._entregas), 1)  # nada pendente

    def test_publicacoes_nao_crescem_com_o_catalogo(self):
        """Um ciclo com 800 símbolos publica dezenas de registros, não milhares.

        Reprodução do congelamento original: cada evento do scanner virava
        uma mensagem para a thread da GUI; agora são contagens agregadas.
        """
        with tempfile.TemporaryDirectory() as pasta:
            orch = _orquestrador(pasta)
            publicadas = []
            orch.publicar = lambda canal, registro: publicadas.append((canal, registro))
            orch.contagem_scan = {}
            for i in range(800):
                orch._evento_fonte("verificando", f"S{i}", "Verificando")
                orch._evento_fonte("dados", f"S{i}", "Dados recebidos")
            self.assertLess(len(publicadas), 30, "evento por símbolo não pode ir à GUI")
            self.assertEqual(orch.contagem_scan.get("verificando"), 800)


class TestListasLimitadas(unittest.TestCase):
    """Listas da GUI com limite de linhas e inserção em lote."""

    @classmethod
    def setUpClass(cls):
        import os
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_insercao_em_lote_respeita_teto(self):
        lista = ListaAnalise(["A", "B"])
        itens = [([str(i), "x"], None) for i in range(400)]
        lista.adicionar_lote(itens)
        self.assertEqual(lista.topLevelItemCount(), MAX_LINHAS)
        self.assertEqual(lista.topLevelItem(0).text(0), "399")  # mais recente no topo

    def test_receber_lote_insere_um_por_flush(self):
        """300 eventos do motor viram uma única operação de inserção."""
        from unittest.mock import patch
        from jev_bot.gui.aba_operacao import AbaOperacao
        abas = AbaOperacao(acoes={k: (lambda: None) for k in
                                  ("iniciar", "pausar", "retomar", "emergencia",
                                   "liberar_risco", "aplicar_limites", "cancelar_ciclo")})
        with patch.object(abas.lista_calc, "insertTopLevelItems",
                          return_value=None) as insercoes:
            abas.receber_lote("calc", [{"id": f"c{i}", "simbolo": "X",
                                        "instante": "2026-01-01T00:00:00+00:00",
                                        "etapa": "calculado", "mensagem": "ok",
                                        "amostra": None, "padroes_testados": 1,
                                        "resultado": "r", "erro": None}
                                       for i in range(300)])
            self.assertEqual(insercoes.call_count, 1)

    def test_interface_responsiva_durante_io_paginado(self):
        """Com download lento paginado, a GUI processa eventos sem travar."""
        app = self.app
        orch = _orquestrador(tempfile.mkdtemp())
        orch.fonte = Mock()
        lote_atual = [0]

        def fonte_lote(simbolo, dias=30, lote=4320, cancelado=None, progresso=None):
            # Simula 10 lotes de 0,05s cada (download lento paginado).
            for _ in range(10):
                time.sleep(0.05)
                lote_atual[0] += 1
                if progresso:
                    progresso(lote_atual[0] * 432, 4320)
            return {"simbolo": simbolo, "candles": [{"time": 1, "open": 1, "high": 1,
                                                     "low": 1, "close": 1, "tick_volume": 1}],
                    "inicio": 1, "fim": 1, "solicitados": 1, "recebidos": 1,
                    "lacunas": 0, "minutos_lacuna": 0, "maior_lacuna": 0}

        orch.fonte.janela_m1 = fonte_lote
        concluido = __import__("threading").Event()
        resultados = {}

        def baixar():
            resultados["janela"] = orch._coletar_janela("TESTE", "id")
            concluido.set()

        thread = __import__("threading").Thread(target=baixar, daemon=True)
        inicio = time.time()
        ciclos_gui = [0]
        maximo_gap = [0.0]
        thread.start()
        while not concluido.is_set() and time.time() - inicio < 10:
            marca = time.time()
            app.processEvents()
            ciclos_gui[0] += 1
            maximo_gap[0] = max(maximo_gap[0], time.time() - marca)
            time.sleep(0.01)
        thread.join(5)
        self.assertTrue(concluido.is_set())
        # A thread da GUI continuou processando eventos durante todo o download.
        self.assertGreater(ciclos_gui[0], 20)
        self.assertLess(maximo_gap[0], 0.5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
