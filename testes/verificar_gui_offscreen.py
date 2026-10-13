"""Fumaça de lançamento da GUI offscreen: autostart, pausa, retomada e limites.

Executa a janela real por alguns segundos com FonteMT5 substituído por um
dobro local, seleção de mercados temporária e DeepSeek/JEv desativados.
Nenhum serviço externo é chamado e nenhuma ordem é enviada.
Saída: data/verificacao-gui.txt
"""

import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
os.chdir(RAIZ)
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

LINHAS = []


def registrar(texto):
    print(texto)
    LINHAS.append(texto)


from PySide6.QtWidgets import QApplication

from jev_bot import mercados
from jev_bot.execution.paper import Book
from jev_bot.gui import tema
from jev_bot.gui.janela import JanelaPrincipal
from jev_bot.orquestrador import Orquestrador
from jev_bot.types import MarketState

agora = time.time()
candles = [{"time": 1_700_000_000 + i * 60, "open": 100, "high": 100.5,
            "low": 99.5, "close": 100 + (i % 11) * .1, "tick_volume": 10} for i in range(300)]
estado = MarketState("TESTE", "Crypto", 100.0, bid=99.995, ask=100.005,
                     tick_time=agora, stop_distance=.5, loss_per_lot=10.0,
                     margin_per_lot=100.0, volume_min=.01, volume_max=10,
                     volume_step=.01, trade_mode=4, currencies=("USD",),
                     candles=candles)
estados = [estado]
janela30 = {"simbolo": "TESTE", "candles": candles, "inicio": candles[0]["time"],
            "fim": candles[-1]["time"], "solicitados": 300, "recebidos": 300,
            "lacunas": 0, "minutos_lacuna": 0, "maior_lacuna": 0}


class FonteFake:
    moeda = "USD"
    resumo = {}
    cancelado = lambda self: False  # noqa: E731
    evento = None

    def estados(self, simbolo=None):
        return iter(estados)

    def cotacao(self, simbolo):
        return 99.99, 100.0, agora

    def conta(self):
        return dict(balance=0, equity=0, profit=0, margin_free=0, currency="USD")

    def lucro(self, posicao, preco):
        return round((preco - posicao.entry) * posicao.volume * 100, 2)

    def revalidar(self, estado_):
        return estado_

    def fim_de_semana(self):
        return False

    def janela_m1(self, simbolo, dias=30, lote=4320, cancelado=None, progresso=None):
        return janela30


class ContextoFonte:
    moeda = "USD"

    def __enter__(self):
        return FonteFake()

    def __exit__(self, *_):
        return False


try:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setStyleSheet(tema.FOLHA)
    pasta = Path("data/verificacao-gui-motor")
    pasta.mkdir(parents=True, exist_ok=True)
    banco = pasta / "db.json"
    banco.unlink(missing_ok=True)
    selecao = pasta / "mercados.json"
    mercados.salvar({"selecao": {"Crypto": {"TESTE": {"monitorar": True, "operar": True}}}},
                    selecao)
    janela = JanelaPrincipal(banco=str(banco), caminho_selecao=str(selecao))
    with patch("jev_bot.orquestrador.FonteMT5", return_value=ContextoFonte()), \
            patch("jev_bot.orquestrador.jev.configuracao", return_value=("chave", "endpoint")), \
            patch("jev_bot.orquestrador.jev.decide",
                  side_effect=RuntimeError("JEv desativado na verificação")), \
            patch("jev_bot.deepseek.configuracao_chat",
                  side_effect=RuntimeError("desativado na verificação")), \
            patch.object(Orquestrador, "_proximo_alvo", lambda self, a: (a + 1.0, "verificação")):
        janela.show()
        app.processEvents()
        # AUTOSTART: sem clique em INICIAR, o ciclo deve rodar sozinho.
        limite = time.time() + 6
        while time.time() < limite and janela.orquestrador.ciclo < 2:
            app.processEvents()
            time.sleep(0.05)
        registrar(f"autostart: estado {janela.orquestrador.estado} | "
                  f"ciclo {janela.orquestrador.ciclo} (sem clique em INICIAR)")
        registrar(f"chip: {janela.aba_operacao.chip_estado.text()} | lista calc: "
                  f"{janela.aba_operacao.lista_calc.topLevelItemCount()} linhas | "
                  f"trades na tabela: {janela.aba_operacao.tabela.rowCount()}")
        registrar(f"barra: {janela.label_jev.text()} | Python Quantitativo local | DeepSeek desativado (0 chamadas)")
        # PAUSAR pela UI e aguardar a INTERFACE refletir o estado (chip/botão).
        janela.aba_operacao._clicar_pausar()
        limite_pausa = time.time() + 10
        while time.time() < limite_pausa and \
                (janela.aba_operacao.chip_estado.text() != "PAUSADO"
                 or janela.aba_operacao.btn_pausar.text() != "RETOMAR"):
            app.processEvents()
            time.sleep(0.05)
        registrar(f"após PAUSAR: chip {janela.aba_operacao.chip_estado.text()} | "
                  f"botão {janela.aba_operacao.btn_pausar.text()} | "
                  f"ativo={janela.orquestrador.book.active}")
        # INICIAR (retomada manual) e confirmar que o estado sai de PAUSADO.
        janela.aba_operacao._clicar_iniciar()
        limite_retomada = time.time() + 10
        while time.time() < limite_retomada and \
                (janela.aba_operacao.chip_estado.text() == "PAUSADO"
                 or janela.orquestrador.pausado):
            app.processEvents()
            time.sleep(0.05)
        app.processEvents()
        registrar(f"após INICIAR: chip {janela.aba_operacao.chip_estado.text()} | "
                  f"pausado={janela.orquestrador.pausado} | "
                  f"ativo={janela.orquestrador.book.active}")
        # APLICAR limites pela UI e aguardar persistência no disco.
        janela.aba_operacao.lim_trades.setValue(1.0)
        janela.aba_operacao.lim_total.setValue(10.0)
        janela.aba_operacao.lim_dd.setValue(8.0)
        janela.aba_operacao._aplicar_limites()
        limite_limites = time.time() + 30
        book_disco = Book.load(banco, "USD")
        while time.time() < limite_limites:
            app.processEvents()
            time.sleep(0.2)
            book_disco = Book.load(banco, "USD")
            if abs(book_disco.limits.risk_per_trade - .01) < 1e-9:
                break
        registrar(f"limites no disco: {book_disco.limits.risk_per_trade:.0%}/op | "
                  f"{book_disco.limits.max_total_risk:.0%} total | DD {book_disco.limits.max_drawdown:.0%}")
        janela.orquestrador.parar()
        fim2 = time.time() + 10
        while janela.motor.isRunning() and time.time() < fim2:
            app.processEvents()
            time.sleep(0.05)
        registrar(f"motor encerrado: {not janela.motor.isRunning()}")
    registrar("verificação da GUI concluída (offscreen; sem MT5 real, sem ordens)")
finally:
    (RAIZ / "data" / "verificacao-gui.txt").write_text("\n".join(LINHAS), encoding="utf-8")
