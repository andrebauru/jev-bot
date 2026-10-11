"""Carteira paper persistente: capital fixo, stops e limites entre reinícios."""

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
import math
import os
import tempfile
import time
from pathlib import Path
from uuid import uuid4

from ..risk import Limits


def dia_utc():
    """Define a fronteira diária de risco independente do fuso do computador."""
    return datetime.now(timezone.utc).date().isoformat()


@dataclass
class Fill:
    symbol: str
    action: str
    entry: float
    volume: float
    stop: float
    target: float
    risk_amount: float
    margin: float
    currencies: tuple
    price_now: float
    profit: float = 0.0
    trade_id: str = ""
    opened_at: str = ""

    def pnl(self):
        """Retorna o P&L calculado pelo contrato do MT5 na moeda da conta."""
        return self.profit


@dataclass
class Book:
    initial_capital: float = 10_000.0
    cash: float = 10_000.0
    currency: str = "USD"
    active: bool = False
    fills: list = field(default_factory=list)
    log: list = field(default_factory=list)
    limits: Limits = field(default_factory=Limits)
    day: str = field(default_factory=dia_utc)
    day_start: float = 10_000.0
    high_water: float = 10_000.0
    daily_blocked: bool = False
    drawdown_blocked: bool = False
    prices_valid: bool = False
    last_equity: float = 10_000.0
    trades: list = field(default_factory=list)
    max_drawdown_seen: float = 0.0
    risk_profile_version: int = 2

    @classmethod
    def load(cls, arquivo, currency="USD"):
        """Carrega sem resetar perdas; estado inválido impede execução."""
        caminho = Path(arquivo)
        if not caminho.exists():
            return cls(currency=currency)
        try:
            dados = json.loads(caminho.read_text(encoding="utf-8"))
            if dados.get("risk_profile_version", 1) < 2:
                dados["limits"].update(risk_per_trade=.02, max_total_risk=.15,
                                       max_drawdown=.10, max_positions=None, max_currency_risk=.15)
                dados["risk_profile_version"] = 2
            # Migra o nome do limite antigo sem zerar capital, perdas ou posições.
            limite_antigo = "max_spread_atr"
            if limite_antigo in dados["limits"]:
                anterior = dados["limits"].pop(limite_antigo)
                dados["limits"].setdefault("max_spread_stop", anterior / 2)
            dados["limits"] = Limits(**dados["limits"])
            dados["fills"] = [Fill(**p) for p in dados["fills"]]
            book = cls(**dados)
            if any(type(v) is not bool for v in (book.active, book.daily_blocked, book.drawdown_blocked)):
                raise ValueError("Controle de execução inválido")
            if book.currency != currency:
                raise ValueError("Moeda da conta diferente da carteira paper")
            if any(not math.isfinite(v) for v in (book.cash, book.initial_capital, book.day_start, book.high_water, book.last_equity)) or min(book.initial_capital, book.day_start, book.high_water) <= 0:
                raise ValueError("Capital inválido")
            if not isinstance(book.trades, list) or not math.isfinite(book.max_drawdown_seen) or book.max_drawdown_seen < 0:
                raise ValueError("Histórico ou drawdown inválido")
            if len({p.symbol for p in book.fills}) != len(book.fills):
                raise ValueError("Posições duplicadas")
            for p in book.fills:
                if p.action not in ("BUY", "SELL") or any(not math.isfinite(v) or v <= 0 for v in (p.entry, p.volume, p.stop, p.target, p.risk_amount, p.margin, p.price_now)) or not math.isfinite(p.profit):
                    raise ValueError("Posição inválida")
            book.prices_valid = False
            for posicao in book.fills:
                if not posicao.trade_id:
                    posicao.trade_id = uuid4().hex
                    book.trades.append({**asdict(posicao), "status": "OPEN", "mode": "paper", "origin": "legacy_position"})
            return book
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise RuntimeError("Carteira paper inválida; não será sobrescrita.") from exc

    def save(self, arquivo):
        """Grava atomicamente todo o estado e os limites registrados."""
        caminho = Path(arquivo)
        caminho.parent.mkdir(parents=True, exist_ok=True)
        conteudo = json.dumps(asdict(self), indent=2, allow_nan=False)
        temporario = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=caminho.parent,
                                             prefix=caminho.name + ".", suffix=".tmp", delete=False) as saida:
                temporario = Path(saida.name)
                saida.write(conteudo)
                saida.flush()
                os.fsync(saida.fileno())
            # Leitores/antivírus Windows podem segurar brevemente o arquivo de destino.
            for tentativa in range(5):
                try:
                    temporario.replace(caminho)
                    break
                except PermissionError:
                    if tentativa == 4:
                        raise
                    time.sleep(.05 * (tentativa + 1))
        finally:
            if temporario is not None:
                temporario.unlink(missing_ok=True)

    def open_positions(self):
        """Conta somente posições paper abertas."""
        return len(self.fills)

    def open_pnl(self):
        """Soma o resultado não realizado da carteira."""
        return sum(p.profit for p in self.fills)

    def equity(self):
        """Retorna capital realizado mais marcação a mercado."""
        return self.cash + self.open_pnl()

    def metrics(self):
        """Resume resultados registrados sem misturar dinheiro real e paper."""
        fechamentos = [e for e in self.log if e.get("event") == "close"]
        ganhos = sum(e["profit"] for e in fechamentos if e["profit"] > 0)
        perdas = -sum(e["profit"] for e in fechamentos if e["profit"] < 0)
        wins = sum(e["profit"] > 0 for e in fechamentos)
        losses = sum(e["profit"] < 0 for e in fechamentos)
        equity = self.equity()
        return dict(equity=equity, growth=equity / self.initial_capital - 1,
                    drawdown=max(0, 1 - equity / self.high_water), max_drawdown=self.max_drawdown_seen,
                    daily_return=equity / self.day_start - 1, gains=ganhos, losses=perdas,
                    wins=wins, losing_trades=losses, closed=len(fechamentos),
                    win_rate=wins / len(fechamentos) if fechamentos else None,
                    open_profit=self.open_pnl(), realized=self.cash - self.initial_capital,
                    risk=sum(p.risk_amount for p in self.fills),
                    risk_fraction=sum(p.risk_amount for p in self.fills) / equity if equity > 0 else 1,
                    margin=sum(p.margin for p in self.fills), positions=len(self.fills))

    def update(self, fonte):
        """Marca posições e executa stops paper ao preço atual, inclusive gaps."""
        self.prices_valid = True
        for p in list(self.fills):
            tick = fonte.cotacao(p.symbol)
            if tick is None:
                self.prices_valid = False
                continue
            preco = tick[0] if p.action == "BUY" else tick[1]
            try:
                lucro = fonte.lucro(p, preco)
            except RuntimeError:
                self.prices_valid = False
                continue
            p.price_now, p.profit = preco, lucro
            registro = next((t for t in self.trades if t.get("trade_id") == p.trade_id), None)
            if registro is not None:
                registro.update(price_now=preco, profit=lucro)
            atingiu = (preco <= p.stop or preco >= p.target) if p.action == "BUY" else (preco >= p.stop or preco <= p.target)
            expirou = bool(p.opened_at) and (datetime.now(timezone.utc) - datetime.fromisoformat(p.opened_at)).total_seconds() >= 300
            if atingiu or expirou:
                self.cash += lucro
                self.fills.remove(p)
                instante = datetime.now(timezone.utc).isoformat()
                motivo = ("STOP" if ((p.action == "BUY" and preco <= p.stop) or (p.action == "SELL" and preco >= p.stop)) else "TARGET") if atingiu else "TIME_EXIT_M1"
                self.log.append({**asdict(p), "event": "close", "price": preco, "profit": lucro, "reason": motivo, "time": instante})
                if registro is not None:
                    registro.update(status="CLOSED", exit=preco, closed_at=instante, reason=motivo)
        if not self.prices_valid:
            return
        # Reinício nunca zera perda do dia; rollover usa o último patrimônio persistido.
        if self.day != dia_utc():
            self.day, self.daily_blocked = dia_utc(), False
            if max(self.last_equity, self.equity()) > 0:
                self.day_start = max(self.last_equity, self.equity())
        equity = self.equity()
        self.high_water = max(self.high_water, equity)
        self.max_drawdown_seen = max(self.max_drawdown_seen, max(0, 1 - equity / self.high_water))
        if equity <= self.day_start * (1 - self.limits.max_daily_loss):
            self.daily_blocked = True
        if equity <= self.high_water * (1 - self.limits.max_drawdown):
            self.drawdown_blocked = True
        self.last_equity = equity

    def execute(self, decision, state, gate):
        """Registra entrada paper com volume, margem, stop e alvo aprovados."""
        if not self.active or gate.verdict != "EXECUTE":
            raise RuntimeError("Entrada paper não autorizada pelo controle de risco.")
        p = Fill(state.symbol, decision.action, gate.entry, gate.volume, gate.stop,
                 gate.target, gate.risk_amount, gate.margin, state.currencies, gate.entry,
                 trade_id=uuid4().hex, opened_at=datetime.now(timezone.utc).isoformat())
        self.fills.append(p)
        self.trades.append({**asdict(p), "status": "OPEN", "mode": "paper",
                            "confidence": decision.confidence, "probability": decision.probability})
        self.log.append({**asdict(p), "event": "open", "risk": p.risk_amount, "time": p.opened_at})
        return p

    def fechar_total(self, fonte, motivo="EMERGENCIA"):
        """Tenta fechar todas as posições paper agora, ao preço observado.

        Cada fechamento usa cotação real do simulador/terminal e só então é
        registrado; sem cotação válida a posição permanece aberta e é
        reportada como falha, nunca como encerrada. Retorna (fechadas, falhas)
        com P&L por posição e o motivo de cada recusa.
        """
        fechadas, falhas = [], []
        for p in list(self.fills):
            tick = fonte.cotacao(p.symbol)
            if tick is None:
                falhas.append({"symbol": p.symbol, "motivo": "Sem cotação válida agora"})
                continue
            preco = tick[0] if p.action == "BUY" else tick[1]
            try:
                lucro = fonte.lucro(p, preco)
            except RuntimeError as exc:
                falhas.append({"symbol": p.symbol, "motivo": str(exc)})
                continue
            self.cash += lucro
            self.fills.remove(p)
            instante = datetime.now(timezone.utc).isoformat()
            registro = next((t for t in self.trades if t.get("trade_id") == p.trade_id), None)
            self.log.append({**asdict(p), "event": "close", "price": preco,
                             "profit": lucro, "reason": motivo, "time": instante})
            if registro is not None:
                registro.update(status="CLOSED", exit=preco, closed_at=instante,
                                reason=motivo, price_now=preco, profit=lucro)
            fechadas.append({"symbol": p.symbol, "lucro": lucro, "preco": preco})
        return fechadas, falhas
