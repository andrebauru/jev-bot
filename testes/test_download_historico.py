"""Testes do download paginado do histórico M1 (lotes, cancelamento, progresso)."""

from types import SimpleNamespace
from unittest.mock import Mock
import unittest

from apoio import api_mock_inicializado
from jev_bot.motor_matematico import estatisticas
from jev_bot.markets import FonteMT5

T0 = 1_700_000_000_000 / 1000  # segundos


class ApiHistorico:
    """Dobro da API MT5 que serve páginas paginadas de candles M1."""

    TIMEFRAME_M1 = 1

    def __init__(self, total, fim=T0, pular=None):
        self.total = total            # quantidade de candles encerrados disponíveis
        self.fim = fim                # timestamp do candle mais recente fechado
        self.pular = pular or set()   # posições (1-based) ausentes no broker
        self.chamadas = []

    def copy_rates_from_pos(self, simbolo, timeframe, inicio, quantidade):
        self.chamadas.append((inicio, quantidade))
        lote = []
        for pos in range(inicio + quantidade - 1, inicio - 1, -1):
            if pos > self.total or pos in self.pular:
                continue
            tempo = self.fim - (pos - 1) * 60
            lote.append({"time": tempo, "open": 100.0, "high": 100.5,
                         "low": 99.5, "close": 100.1, "tick_volume": 10})
        return lote


class TestDownloadHistorico(unittest.TestCase):
    """Download em lotes curtos, com cancelamento e progresso cooperativo."""

    def _fonte(self, api):
        return FonteMT5(api=api, ajuste_horas=0, relogio=lambda: T0 + 60)

    def test_paginacao_junta_lotes_em_ordem_cronologica(self):
        """Lotes mais antigos entram na frente; resultado é cronológico e deduplicado."""
        api = ApiHistorico(total=5000)
        fonte = self._fonte(api)
        janela = fonte.janela_m1("TESTE", dias=2, lote=720)
        self.assertIsNotNone(janela)
        self.assertEqual(janela["recebidos"], 2880)
        tempos = [c["time"] for c in janela["candles"]]
        self.assertEqual(tempos, sorted(tempos))
        self.assertEqual(len(set(tempos)), len(tempos))
        # 2880 candles em lotes de 720 = 4 chamadas curtas (não uma gigante).
        self.assertEqual(len(api.chamadas), 4)
        self.assertTrue(all(q <= 720 for _, q in api.chamadas))
        # A primeira chamada pula o candle em formação (start_pos = 1).
        self.assertEqual(api.chamadas[0], (1, 720))

    def test_para_no_fim_do_historico_disponivel(self):
        """Página curta encerra a paginação sem inventar candles."""
        api = ApiHistorico(total=1000)
        fonte = self._fonte(api)
        janela = fonte.janela_m1("TESTE", dias=30, lote=4320)
        self.assertIsNotNone(janela)
        self.assertEqual(janela["recebidos"], 1000)
        self.assertEqual(janela["solicitados"], 30 * 1440)

    def test_progresso_por_lote(self):
        """O callback de progresso recebe contagem cumulativa por lote."""
        api = ApiHistorico(total=5000)
        fonte = self._fonte(api)
        progressos = []
        fonte.janela_m1("TESTE", dias=2, lote=720, progresso=lambda c, s: progressos.append((c, s)))
        self.assertEqual([c for c, _ in progressos], [720, 1440, 2160, 2880])
        self.assertTrue(all(s == 2880 for _, s in progressos))

    def test_cancelamento_cooperativo_entre_lotes(self):
        """Cancelamento entre lotes interrompe sem retornar janela parcial."""
        api = ApiHistorico(total=5000)
        fonte = self._fonte(api)
        estado = {"cancelar": False}

        def cancelado():
            return estado["cancelar"]

        def progresso(coletados, solicitado):
            if coletados >= 720:
                estado["cancelar"] = True

        resultado = fonte.janela_m1("TESTE", dias=2, lote=720, cancelado=cancelado,
                                    progresso=progresso)
        self.assertIsNone(resultado)

    def test_candle_invalido_aborta_sem_substituto(self):
        """Dado inválido em qualquer lote descarta toda a janela."""
        api = ApiHistorico(total=5000)
        fonte = self._fonte(api)
        original = api.copy_rates_from_pos

        def com_nan(simbolo, timeframe, inicio, quantidade):
            lote = original(simbolo, timeframe, inicio, quantidade)
            if inicio > 720 and lote:  # segundo lote recebe candle inválido
                lote = [dict(lote[0], close=float("nan"))] + lote[1:]
            return lote

        api.copy_rates_from_pos = com_nan
        self.assertIsNone(fonte.janela_m1("TESTE", dias=2, lote=720))

    def test_lacunas_sao_contadas_nao_preenchidas(self):
        """Lacuna do broker é contada; nenhum candle é inventado."""
        api = ApiHistorico(total=5000, pular=set(range(100, 110)))  # 10 candles ausentes
        fonte = self._fonte(api)
        janela = fonte.janela_m1("TESTE", dias=1, lote=1440)
        self.assertIsNotNone(janela)
        self.assertEqual(janela["lacunas"], 1)
        self.assertGreaterEqual(janela["minutos_lacuna"], 9)
        tempos = [c["time"] for c in janela["candles"]]
        self.assertEqual(tempos, sorted(tempos))
        self.assertEqual(janela["recebidos"], 1440 - 10)

    def test_estatisticas_usam_janela_baixada(self):
        """A janela paginada alimenta o motor matemático sem alterações."""
        api = ApiHistorico(total=5000)
        fonte = self._fonte(api)
        janela = fonte.janela_m1("TESTE", dias=1, lote=1440)
        stats = estatisticas(janela, 0.05)
        self.assertTrue(stats["disponivel"])
        self.assertEqual(stats["candles"], 1440)

    def test_fonte_sem_historico_retorna_none(self):
        api = ApiHistorico(total=0)
        fonte = self._fonte(api)
        self.assertIsNone(fonte.janela_m1("TESTE", dias=1, lote=1440))

    def test_catalogo_agrupado_por_categoria_real(self):
        """Descoberta agrupa categorias do path real, sem inventar ativos."""
        api = Mock()
        api.symbols_get.return_value = [
            SimpleNamespace(name="BTCUSD", path="Crypto\\BTCUSD"),
            SimpleNamespace(name="ETHUSD", path="Crypto\\ETHUSD"),
            SimpleNamespace(name="EURUSD", path="Forex\\EURUSD"),
            SimpleNamespace(name="XAUUSD", path="Metais\\XAUUSD"),
        ]
        fonte = self._fonte(api)
        grupos = fonte.catalogo_por_categoria()
        self.assertEqual(grupos, {"Crypto": ["BTCUSD", "ETHUSD"],
                                  "Forex": ["EURUSD"], "Metais": ["XAUUSD"]})


if __name__ == "__main__":
    unittest.main(verbosity=2)
