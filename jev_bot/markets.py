"""Dados reais do terminal MT5; falhas nunca geram preços substitutos."""

from __future__ import annotations

import math
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .types import MarketState
from . import configuracao
from .matematica import resumir

TERMINAL = configuracao.TERMINAL_PADRAO
MEMECOINS_MT5 = frozenset({"DOGUSD", "PENGUUSD", "WIFUSD", "FARTCOINUSD", "SPXUSD",
                          "TRUMPUSD", "1000XSHIB", "1000XBONK", "1000XFLOKI", "1000XPEPE"})


def eh_memecoin(simbolo):
    """Reconhece os contratos meme identificados no catálogo deste broker."""
    return simbolo.upper() in MEMECOINS_MT5


class FonteMT5:
    """Consulta o catálogo inteiro e coleta candles apenas de ativos elegíveis."""

    def __init__(self, caminho=None, ajuste_horas=None, api=None, relogio=time.time):
        """Configura o terminal existente e o ajuste explícito do relógio do broker."""
        if api is None:
            try:
                import MetaTrader5 as api
            except ImportError as exc:
                raise RuntimeError("Instale o projeto com pip install -e . no Windows.") from exc
        configuracao.carregar_ambiente()
        self.api = api
        self.caminho = caminho or configuracao.terminal_mt5()
        ajuste_horas = configuracao.ajuste_horario_mt5() if ajuste_horas is None else ajuste_horas
        self.ajuste = ajuste_horas * 3600
        self.relogio = relogio
        self.inativos = {}
        self.resumo = {}
        self.progresso = None
        self.evento = None
        self.cancelado = lambda: False
        self.catalogo = None
        self.dia_catalogo = None
        self.instante_catalogo = 0

    def fim_de_semana(self):
        """Usa o dia local em Asia/Tokyo para a política de novas análises."""
        return datetime.fromtimestamp(self.relogio(), timezone(timedelta(hours=9))).weekday() >= 5

    @staticmethod
    def eh_cripto(info):
        """Identifica cripto pela categoria do broker, sem restringir a BTC."""
        categorias = getattr(info, "path", "").replace("/", "\\").casefold().split("\\")
        return any(c in {"crypto", "cryptos", "cryptocurrency", "cryptocurrencies", "cripto", "criptomoedas"} for c in categorias)

    def informar(self, etapa, simbolo, mensagem):
        """Publica etapas do scanner sem credenciais ou conteúdo do serviço externo."""
        if self.evento:
            self.evento(etapa, simbolo, mensagem)

    def conta(self):
        """Retorna apenas métricas financeiras autorizadas para exibição local."""
        info = self.api.account_info()
        terminal = self.api.terminal_info()
        if info is None or not terminal or not terminal.connected:
            raise RuntimeError("Conta MT5 desconectada.")
        dados = {nome: float(getattr(info, nome)) for nome in ("balance", "equity", "profit", "margin_free")}
        if any(not math.isfinite(v) for v in dados.values()):
            raise RuntimeError("Métricas financeiras MT5 inválidas.")
        dados["currency"] = info.currency
        return dados

    def __enter__(self):
        """Conecta à conta aberta, sem login ou troca de conta."""
        if not Path(self.caminho).is_file():
            raise RuntimeError("Executável MT5 não encontrado no caminho informado.")
        if not self.api.initialize(self.caminho, timeout=10000):
            raise RuntimeError("Não foi possível conectar ao terminal MT5.")
        terminal = self.api.terminal_info()
        conta = self.api.account_info()
        if not terminal or not terminal.connected or conta is None:
            self.api.shutdown()
            raise RuntimeError("MT5 sem conexão ou sem conta aberta.")
        self.moeda = conta.currency
        return self

    def __exit__(self, *_):
        """Libera a conexão Python sem fechar o terminal."""
        self.api.shutdown()

    def cotacao(self, simbolo):
        """Aceita somente bid/ask válidos e ticks com até 120 segundos."""
        tick = self.api.symbol_info_tick(simbolo)
        if tick is None:
            return None
        instante = tick.time_msc / 1000 if tick.time_msc else tick.time
        instante -= self.ajuste
        idade = self.relogio() - instante
        if not (0 <= idade <= 120 and 0 < tick.bid <= tick.ask
                and math.isfinite(tick.bid) and math.isfinite(tick.ask)):
            return None
        return tick.bid, tick.ask, instante

    def _validar_barras(self, barras):
        """Valida candles brutos do broker; None em qualquer dado inválido.

        Retorna candles normalizados (UTC) ou None — nunca substitutos.
        """
        lote_validado = []
        for b in barras:
            try:
                tempo, abre, alto, baixo, fecha = (float(b["time"]), float(b["open"]),
                                                   float(b["high"]), float(b["low"]), float(b["close"]))
                volume = int(b["tick_volume"])
            except (TypeError, KeyError, ValueError, OverflowError):
                return None
            if any(not math.isfinite(v) or v <= 0 for v in (abre, alto, baixo, fecha)):
                return None
            if not math.isfinite(tempo) or tempo <= 0 or volume < 0:
                return None
            if not baixo <= min(abre, fecha) <= max(abre, fecha) <= alto:
                return None
            lote_validado.append({"time": tempo - self.ajuste, "open": abre, "high": alto,
                                  "low": baixo, "close": fecha, "tick_volume": volume})
        return lote_validado

    def incremento_m1(self, simbolo, desde, max_candles=4320):
        """Baixa somente os candles M1 encerrados mais recentes que `desde`.

        Para o cache incremental: uma chamada curta em vez de rebaixar a
        janela completa. Devolve candles cronológicos normalizados ou None
        (sem dados / dado inválido). `desde` é o timestamp do último
        candle já armazenado (UTC).
        """
        minutos = max(1, int((self.relogio() - 60 - desde) // 60) + 2)
        quantidade = max(1, min(int(max_candles), minutos))
        barras = self.api.copy_rates_from_pos(simbolo, self.api.TIMEFRAME_M1, 1, quantidade)
        if barras is None or len(barras) == 0:
            return None
        candles = self._validar_barras(barras)
        if candles is None:
            return None
        candles = [c for c in candles if c["time"] > desde]
        candles = [c for c in candles if c["time"] + 60 <= self.relogio()]
        if not candles:
            return None
        candles.sort(key=lambda c: c["time"])
        return candles

    def janela_m1(self, simbolo, dias=1, lote=4320, cancelado=None, progresso=None):
        """Coleta até `dias` de candles M1 encerrados, em lotes paginados.

        Cada lote é uma chamada curta ao terminal; entre lotes há
        cancelamento cooperativo e callback de progresso, evitando uma
        chamada longa que trave o motor. Lacunas do broker não são
        preenchidas: são contadas e informadas, sem inventar candles.
        Falha de coleta retorna None; nunca gera substituto.
        """
        solicitado = int(dias * 1440)
        coletados = []
        inicio_lote = 1  # pula o candle em formação
        while len(coletados) < solicitado:
            if cancelado is not None and cancelado():
                return None
            quantidade = min(lote, solicitado - len(coletados))
            barras = self.api.copy_rates_from_pos(simbolo, self.api.TIMEFRAME_M1,
                                                  inicio_lote, quantidade)
            if barras is None or len(barras) == 0:
                break  # broker não tem mais histórico
            lote_validado = self._validar_barras(barras)
            if lote_validado is None:
                return None
            # Lotes posteriores são mais antigos: entram na frente.
            coletados = lote_validado + coletados
            if progresso is not None:
                progresso(min(len(coletados), solicitado), solicitado)
            if len(barras) < quantidade:
                break  # fim do histórico disponível no terminal
            inicio_lote += len(barras)
        # Ordem cronológica estrita: descarta repetições ou regressões do broker.
        coletados.sort(key=lambda c: c["time"])
        coletados = [c for i, c in enumerate(coletados) if i == 0 or c["time"] > coletados[i - 1]["time"]]
        # Filtra adicionalmente por relógio UTC local: alguns terminais podem
        # incluir a barra M1 corrente mesmo quando se solicitou start_pos=1.
        coletados = [c for c in coletados if c["time"] + 60 <= self.relogio()]
        if len(coletados) < 2:
            return None
        lacunas = [int((b["time"] - a["time"]) / 60 - 1) for a, b in zip(coletados, coletados[1:])]
        lacunas = [g for g in lacunas if g > 0]
        return {"simbolo": simbolo, "candles": coletados,
                "inicio": coletados[0]["time"], "fim": coletados[-1]["time"],
                "solicitados": solicitado, "recebidos": len(coletados),
                "lacunas": len(lacunas), "minutos_lacuna": sum(lacunas),
                "maior_lacuna": max(lacunas) if lacunas else 0}

    def catalogo_por_categoria(self):
        """Agrupa o catálogo do terminal por categoria (primeiro nível do path).

        Somente símbolos realmente retornados pelo broker; sem listas fixas.
        """
        infos = self.api.symbols_get()
        if infos is None:
            raise RuntimeError("MT5 não retornou o catálogo de símbolos.")
        grupos = {}
        for info in infos:
            partes = (getattr(info, "path", "") or "").replace("\\", "/").split("/")
            categoria = partes[0] if partes and partes[0] else "Outros"
            grupos.setdefault(categoria, []).append(info.name)
        return {categoria: sorted(nomes) for categoria, nomes in sorted(grupos.items())}

    def estados(self, simbolo=None):
        """Filtra inativos antes do histórico; sem tick recente não há análise."""
        dia = datetime.fromtimestamp(self.relogio(), timezone(timedelta(hours=9))).date()
        if self.catalogo is None or dia != self.dia_catalogo or self.relogio() - self.instante_catalogo >= 900:
            self.catalogo = self.api.symbols_get()
            if self.catalogo is None:
                raise RuntimeError("MT5 não retornou o catálogo de símbolos.")
            self.dia_catalogo, self.instante_catalogo = dia, self.relogio()
        catalogo = self.catalogo
        fds = self.fim_de_semana()
        candidatos = [info for info in catalogo
                      if (not simbolo or info.name.upper() == simbolo.upper())
                      and (not fds or self.eh_cripto(info))]
        candidatos.sort(key=lambda info: not (self.eh_cripto(info) and eh_memecoin(info.name)))
        self.resumo = dict(catalogo=len(catalogo), candidatos=len(candidatos), verificados=0,
                           fora_dia=sum(not self.eh_cripto(i) for i in catalogo) if fds else 0,
                           analisados=0, inativos=0, sem_historico=0, fim_de_semana=fds,
                           memecoins=sum(self.eh_cripto(i) and eh_memecoin(i.name) for i in candidatos))
        self.informar("catalogo", "", f"{len(candidatos)} candidatos de {len(catalogo)} contratos; "
                      + ("sábado/domingo: somente Crypto" if fds else "dia útil: todas as categorias"))
        for indice, info in enumerate(candidatos, 1):
            if self.cancelado():
                break
            self.resumo["verificados"] = indice
            if self.progresso:
                self.progresso(indice, len(candidatos), self.resumo)
            self.informar("verificando", info.name, "Verificando negociação e cotação")
            if self.inativos.get(info.name, 0) > self.relogio():
                self.resumo["inativos"] += 1
                self.informar("ignorado", info.name, "Inativo em espera; sem buscar candles")
                continue
            if info.custom or info.trade_mode not in (1, 2, 4):
                self.resumo["inativos"] += 1
                self.inativos[info.name] = self.relogio() + 900
                self.informar("ignorado", info.name, "Contrato não permite novas operações")
                continue
            if not info.visible and not self.api.symbol_select(info.name, True):
                self.resumo["inativos"] += 1
                self.inativos[info.name] = self.relogio() + 900
                self.informar("ignorado", info.name, "Seleção de símbolo indisponível")
                continue
            cotacao = self.cotacao(info.name)
            if cotacao is None:
                self.resumo["inativos"] += 1
                self.inativos[info.name] = self.relogio() + 900
                self.informar("ignorado", info.name, "Sem tick válido recente; não analisa mercado inativo")
                continue
            self.informar("historico", info.name, "Coletando 100 candles M1 encerrados")
            try:
                estado = self._estado(info, cotacao)
            except (ValueError, TypeError, KeyError, OverflowError):
                estado = None
            if estado is None:
                self.resumo["sem_historico"] += 1
                self.informar("ignorado", info.name, "Histórico recente ou contrato insuficiente")
                continue
            self.resumo["analisados"] += 1
            self.informar("dados", info.name, "Dados MT5 recebidos; análise pronta")
            yield estado

    def revalidar(self, estado):
        """Revalida cotação, contrato e candle M1 fechado após a resposta JEv.

        Consulta apenas a última barra fechada. Se o broker não confirmar o
        mesmo candle de referência usado no despacho, falha fechada para não
        aprovar uma resposta referente a um snapshot vencido.
        """
        info = self.api.symbol_info(estado.symbol)
        tick = self.cotacao(estado.symbol)
        if info is None or tick is None or (self.fim_de_semana() and not self.eh_cripto(info)):
            return None
        barras = self.api.copy_rates_from_pos(estado.symbol, self.api.TIMEFRAME_M1, 1, 1)
        if barras is None or len(barras) != 1:
            return None
        fechada = self._validar_barras(barras)
        if (not fechada or not estado.candles
                or fechada[-1]["time"] != estado.candles[-1]["time"]):
            return None
        valores = self._contrato(info, tick, estado.stop_distance)
        if valores is None:
            return None
        estado.bid, estado.ask, estado.tick_time = tick
        estado.price = (estado.bid + estado.ask) / 2
        estado.trade_mode = info.trade_mode
        estado.loss_per_lot, estado.margin_per_lot = valores
        return estado

    def _contrato(self, info, cotacao, distancia):
        """Calcula risco e margem na moeda da conta para as direções permitidas."""
        bid, ask, _ = cotacao
        losses, margins = [], []
        if info.trade_mode not in (1, 2, 4):
            return None
        for acao, entrada, stop in ((self.api.ORDER_TYPE_BUY, ask, ask - distancia),
                                    (self.api.ORDER_TYPE_SELL, bid, bid + distancia)):
            if (info.trade_mode == 1 and acao == self.api.ORDER_TYPE_SELL) or (info.trade_mode == 2 and acao == self.api.ORDER_TYPE_BUY):
                continue
            perda = self.api.order_calc_profit(acao, info.name, 1.0, entrada, stop)
            margem = self.api.order_calc_margin(acao, info.name, 1.0, entrada)
            if perda is None or margem is None or not math.isfinite(perda) or not math.isfinite(margem) or perda >= 0 or margem <= 0:
                return None
            losses.append(abs(perda))
            margins.append(margem)
        return max(losses), max(margins)

    def _estado(self, info, cotacao):
        """Coleta OHLC bruto e calcula só a distância conservadora de proteção."""
        barras = self.api.copy_rates_from_pos(info.name, self.api.TIMEFRAME_M1, 1, 100)
        if barras is None or len(barras) < 100:
            return None
        closes = [float(b["close"]) for b in barras]
        if any(not math.isfinite(c) or c <= 0 for c in closes):
            return None
        idade_barra = self.relogio() - (float(barras[-1]["time"]) - self.ajuste + 60)
        if not 0 <= idade_barra <= 120:
            return None
        if any(not math.isfinite(float(b[n])) or float(b[n]) < 0
               for b in barras for n in ("open", "high", "low", "tick_volume")):
            return None
        if any(not math.isfinite(float(b["time"])) or float(b["time"]) <= 0
               or min(float(b["open"]), float(b["low"])) <= 0
               or not float(b["low"]) <= min(float(b["open"]), float(b["close"]))
               <= max(float(b["open"]), float(b["close"])) <= float(b["high"])
               for b in barras):
            return None
        if any(float(atual["time"]) <= float(anterior["time"])
               for anterior, atual in zip(barras, barras[1:])):
            return None
        bid, ask, instante = cotacao
        preco = (bid + ask) / 2
        # Proteção por deslocamento extremo bruto; não é indicador de entrada.
        deslocamento = max(abs(atual - anterior) for anterior, atual in zip(closes, closes[1:]))
        distancia = max(2 * deslocamento, (info.trade_stops_level + 1) * info.point, 2 * (ask - bid))
        contrato = self._contrato(info, cotacao, distancia)
        if contrato is None:
            return None
        classe = info.path.split("\\")[0]
        moedas = tuple(sorted({info.currency_base, info.currency_profit} - {""}))
        estado = MarketState(
            symbol=info.name, asset_class=classe, price=preco,
            bid=bid, ask=ask, tick_time=instante, stop_distance=distancia,
            loss_per_lot=contrato[0], margin_per_lot=contrato[1],
            volume_min=info.volume_min, volume_max=info.volume_max,
            volume_step=info.volume_step, digits=info.digits, trade_mode=info.trade_mode,
            currencies=moedas,
            candles=[{"time": float(b["time"]) - self.ajuste,
                      "open": float(b["open"]), "high": float(b["high"]),
                      "low": float(b["low"]), "close": float(b["close"]),
                      "tick_volume": int(b["tick_volume"])} for b in barras],
        )
        estado.mathematics = resumir(estado.candles, info.point, ask - bid)
        estado.mathematics["mt5_execution_spec"] = {
            "point": float(info.point),
            "trade_stops_level": int(info.trade_stops_level),
            "digits": int(info.digits),
        }
        return estado

    def lucro(self, posicao, preco):
        """Converte o P&L paper na moeda da conta usando o contrato real."""
        acao = self.api.ORDER_TYPE_BUY if posicao.action == "BUY" else self.api.ORDER_TYPE_SELL
        valor = self.api.order_calc_profit(acao, posicao.symbol, posicao.volume, posicao.entry, preco)
        if valor is None or not math.isfinite(valor):
            raise RuntimeError(f"MT5 não calculou P&L de {posicao.symbol}.")
        return float(valor)
