"""Typed shapes. State in, a decision out.

The whole project is built around one idea: a market state goes in, and a
typed decision with a calibrated probability comes out. These two
dataclasses are that contract. Nothing downstream re-parses text; every
stage reads typed fields.
"""

from __future__ import annotations

from dataclasses import dataclass, field

ACTIONS = ("BUY", "SELL", "HOLD", "AVOID")


@dataclass
class MarketState:
    """One snapshot of a market, the input JEV evaluates."""

    symbol: str
    asset_class: str          # "stock", "crypto", "meme", "option"
    price: float
    bid: float = 0.0
    ask: float = 0.0
    tick_time: float = 0.0
    stop_distance: float = 0.0
    loss_per_lot: float = 0.0
    margin_per_lot: float = 0.0
    volume_min: float = 0.0
    volume_max: float = 0.0
    volume_step: float = 0.0
    digits: int = 5
    trade_mode: int = 0
    currencies: tuple[str, ...] = ()
    source: str = "MT5"
    timeframe: str = "M1"
    candles: list[dict] = field(default_factory=list)
    mathematics: dict = field(default_factory=dict)


@dataclass
class Decision:
    """What JEV returned for a state: a typed action and two numbers.

    `source` identifies the external decision service; no offline backend exists.
    """

    symbol: str
    action: str               # one of ACTIONS
    probability: float        # calibrated P(action is right), 0..1
    confidence: float         # how sure JEV is it should act at all, 0..1
    source: str = "jev"
    reasons: dict = field(default_factory=dict)   # feature -> signed weight
