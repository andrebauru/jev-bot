"""Consulta MT5 e controla a carteira paper; nenhuma ordem real é enviada."""

import argparse
from contextlib import contextmanager, nullcontext
import os
from pathlib import Path
import sys
import time

from . import configuracao, markets, render
from .execution.paper import Book
from .loop import run
from .painel import BANCO_PADRAO, PainelTerminal, carregar_carteira


@contextmanager
def bloquear(arquivo):
    """Evita que processos concorrentes gastem o mesmo orçamento paper."""
    caminho = Path(str(arquivo) + ".lock")
    caminho.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(caminho, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise RuntimeError("Carteira em uso. Se houve interrupção, verifique o processo antes de remover o .lock.") from None
    try:
        os.close(fd)
        yield
    finally:
        caminho.unlink()


def executar(a):
    """Mantém leitura, início, pausa e ciclos paper como ações distintas."""
    pausa = Path(str(a.book) + ".pause")
    if a.cmd == "stop":
        pausa.parent.mkdir(parents=True, exist_ok=True)
        pausa.write_text("paused", encoding="ascii")
        print("Trading paper pausado. O processo em execução observará a pausa antes da próxima entrada.")
        return
    if a.cmd in ("dashboard", "painel", "run"):
        with bloquear(a.book):
            PainelTerminal(a).executar()
        return
    if a.cmd == "gui":
        with bloquear(a.book):
            from .gui.janela import executar as executar_janela
            codigo = executar_janela(terminal=a.terminal, offset_horas=a.mt5_time_offset_hours,
                                     banco=a.book, caminho_selecao=getattr(a, "mercados", None))
        raise SystemExit(codigo if isinstance(codigo, int) and codigo else 0)
    controle = bloquear(a.book) if a.cmd == "start" else nullcontext()
    with controle, markets.FonteMT5(a.terminal, a.mt5_time_offset_hours) as fonte:
        fonte.progresso = lambda indice, total, resumo: print(
            f"Filtro MT5: {indice}/{total} contratos verificados; "
            f"{resumo['analisados']} ativos analisados", flush=True)
        book = Book(currency=fonte.moeda) if a.cmd == "scan" else carregar_carteira(a.book, fonte.moeda)
        if pausa.exists():
            book.active = False
        if a.cmd == "start":
            pausa.unlink(missing_ok=True)
            book.active = True
            book.save(a.book)
            print("Iniciar trading: paper habilitado. Use run para abrir o painel; stop para pausar.")
            print(render.book_summary(book))
            return
        if a.cmd == "status":
            print(render.book_summary(book))
            return
        while True:
            inicio = time.monotonic()
            estados = fonte.estados(getattr(a, "symbol", None))
            if a.cmd == "scan":
                politica = "SÁB/DOM (Asia/Tokyo): SOMENTE CRIPTO + MEMECOINS" if fonte.fim_de_semana() else "Dia útil: mercados elegíveis"
                print(f"{politica} | MT5 real | memecoins primeiro | OHLC M1 encerrado", flush=True)
                for st in estados:
                    print(f"{st.symbol:<20} {st.asset_class:<15} {st.price:.8g}  candles={len(st.candles)}  stop_dist={st.stop_distance:.8g}", flush=True)
            else:
                print(render.header("jev", fonte.api.symbols_total()), flush=True)
                registros = run(estados, book, book.limits, execute=False,
                                fonte=fonte,
                                pausado=pausa.exists)
                if a.cmd == "card":
                    if not registros:
                        raise RuntimeError("Ativo ausente, inativo ou sem dados suficientes.")
                    print(render.decision_card(*registros[0]))
                else:
                    print(render.decisions_table(registros))
            print(f"Filtro MT5: {fonte.resumo}", flush=True)
            if not a.continuous:
                break
            restante = max(0, a.interval - (time.monotonic() - inicio))
            while restante > 0:
                duracao_pausa = min(restante, 5)
                time.sleep(duracao_pausa)
                restante -= duracao_pausa


def build_parser():
    """Expõe catálogo completo e ajuste de horário sem limites de amostragem."""
    p = argparse.ArgumentParser(prog="jev-bot", description="MT5 real, decisões JEV e execução paper")
    p.add_argument("--terminal", default=configuracao.terminal_mt5())
    p.add_argument("--mt5-time-offset-hours", type=float, default=configuracao.ajuste_horario_mt5(),
                   help="Diferença do timestamp deste broker para UTC; verifique após mudança de horário")
    p.add_argument("--book", default=str(BANCO_PADRAO))
    p.add_argument("--once", action="store_true", help="Dashboard: executar um scan completo e encerrar")
    p.add_argument("--plain", action="store_true", help="Dashboard sem redesenho ANSI, para logs")
    p.add_argument("--continuous", action="store_true")
    p.add_argument("--interval", type=int, default=15)
    sub = p.add_subparsers(dest="cmd")
    p.set_defaults(cmd="dashboard")
    for nome in ("dashboard", "painel", "scan", "decisions", "run", "start", "stop", "status"):
        sub.add_parser(nome)
    gui = sub.add_parser("gui")
    gui.add_argument("--mercados", default=None,
                     help="Arquivo de seleção de mercados alternativo (padrão: data/mercados.json)")
    card = sub.add_parser("card")
    card.add_argument("symbol")
    return p


def main(argv=None):
    """Exibe falhas controladas sem revelar configurações ou credenciais."""
    for saida in (sys.stdout, sys.stderr):
        if hasattr(saida, "reconfigure"):
            saida.reconfigure(encoding="utf-8")
    try:
        configuracao.carregar_ambiente()
        configuracao.ajuste_horario_mt5()
    except (RuntimeError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    p = build_parser()
    a = p.parse_args(argv)
    if a.interval < 5 or not -14 <= a.mt5_time_offset_hours <= 14:
        p.error("Intervalo mínimo: 5s. Ajuste de horário: -14 a +14 horas.")
    if a.continuous and a.cmd not in ("scan", "run", "decisions", "dashboard", "painel"):
        p.error("Modo contínuo disponível apenas para scan, decisions e run.")
    try:
        executar(a)
    except (RuntimeError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Ciclo interrompido; estado paper preservado.", file=sys.stderr)
        return 130
    return 0
