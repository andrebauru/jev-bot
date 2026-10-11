"""Verificação de leitura real: conexão MT5, janela 30d e estatísticas locais.

Somente leitura: catálogo, tick, candles e cálculo de contrato. Nenhuma
ordem é enviada. Saída em texto para data/verificacao-janela30d.txt.
"""

import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from jev_bot import motor_matematico
from jev_bot.markets import FonteMT5

LINHAS = []


def registrar(texto):
    print(texto)
    LINHAS.append(texto)


try:
    with FonteMT5() as fonte:
        registrar(f"Conectado; moeda da conta: {fonte.moeda}")
        for simbolo in ["BTCUSD", "ETHUSD", "1000XPEPE", "EURUSD"]:
            tick = fonte.cotacao(simbolo)
            registrar(f"--- {simbolo} | tick {tick}")
            if tick is None:
                registrar("sem tick recente; ignorado")
                continue
            janela = fonte.janela_m1(simbolo, dias=30)
            if janela is None:
                registrar("janela indisponível")
                continue
            estat = motor_matematico.estatisticas(janela, tick[1] - tick[0])
            registrar(f"janela: {janela['recebidos']} candles de {janela['solicitados']} pedidos | "
                      f"início {janela['inicio']} | fim {janela['fim']} | "
                      f"lacunas {janela['lacunas']} ({janela['minutos_lacuna']} min)")
            registrar(f"stats: disponível={estat['disponivel']} | dias={estat.get('dias')} | "
                      f"mediana 1min={estat.get('movimento_1min', {}).get('mediana_pct')}% | "
                      f"custo spread={estat.get('custo_spread_pct')}%")
            descoberta, _ = motor_matematico.janela_descoberta(janela["candles"])
            recorte = {**janela, "candles": descoberta, "fim": descoberta[-1]["time"]}
            stats_descoberta = motor_matematico.estatisticas(recorte, tick[1] - tick[0])
            resumo = motor_matematico.resumo_compacto(stats_descoberta)
            registrar(f"resumo compacto (descoberta): {len(str(resumo))} caracteres — payload do prompt")
        registrar("verificação concluída sem ordens")
except Exception as exc:  # relatório honesto de falha
    registrar(f"FALHA: {type(exc).__name__}: {exc}")
    raise SystemExit(1)
finally:
    (RAIZ / "data" / "verificacao-janela30d.txt").write_text("\n".join(LINHAS), encoding="utf-8")
