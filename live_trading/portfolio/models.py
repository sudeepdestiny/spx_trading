"""
Domain models for the SPX strategy portfolio ledger.

Quantity is signed:
  +N = long N contracts
  -N = short N contracts

Unrealized P&L:
    (mark - entry_price) * signed_open_quantity * multiplier
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Optional
import uuid


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


class Right(str, Enum):
    CALL = "C"
    PUT = "P"


class StrategyStatus(str, Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    ARCHIVED = "ARCHIVED"


class MarkSource(str, Enum):
    MID = "MID"
    LAST = "LAST"
    MANUAL = "MANUAL"
    THEO = "THEO"


@dataclass
class Strategy:
    strategy_id: str
    name: str
    underlying: str = "SPX"
    status: StrategyStatus = StrategyStatus.OPEN
    created_at: datetime = field(default_factory=datetime.utcnow)
    closed_at: Optional[datetime] = None
    notes: str = ""

    @staticmethod
    def create(
        name: str,
        underlying: str = "SPX",
        notes: str = "",
    ) -> "Strategy":
        return Strategy(
            strategy_id=new_id("strat"),
            name=name,
            underlying=underlying,
            notes=notes,
        )


@dataclass
class Fill:
    """One opening trade event for an option position lot."""

    fill_id: str
    strategy_id: str
    expiry: date
    strike: float
    right: Right
    quantity: int
    price: float
    multiplier: int = 100
    commission: float = 0.0
    timestamp: datetime = field(default_factory=datetime.utcnow)
    source: str = "MANUAL"

    @staticmethod
    def create(
        strategy_id: str,
        expiry: date,
        strike: float,
        right: str,
        quantity: int,
        price: float,
        multiplier: int = 100,
        commission: float = 0.0,
        source: str = "MANUAL",
    ) -> "Fill":
        return Fill(
            fill_id=new_id("fill"),
            strategy_id=strategy_id,
            expiry=expiry,
            strike=strike,
            right=Right(right),
            quantity=quantity,
            price=price,
            multiplier=multiplier,
            commission=commission,
            source=source,
        )


@dataclass
class CloseAllocation:
    """A closing fill allocated to one opening lot."""

    close_id: str
    strategy_id: str
    lot_id: str
    expiry: date
    strike: float
    right: Right
    closed_quantity: int
    opening_price: float
    closing_price: float
    commission: float
    realized_pnl: float
    timestamp: datetime = field(default_factory=datetime.utcnow)


@dataclass
class Lot:
    """
    An opening lot. Its `open_quantity` decreases towards zero as the
    user books partial or full closes.

    `manual_mark` is deliberately an optional override. When it is None,
    valuation uses live bid/ask midpoint or last price from the quote provider.
    """

    lot_id: str
    strategy_id: str
    opening_fill_id: str
    expiry: date
    strike: float
    right: Right
    original_quantity: int
    open_quantity: int
    entry_price: float
    multiplier: int = 100
    manual_mark: Optional[float] = None

    @property
    def is_open(self) -> bool:
        return self.open_quantity != 0

    @property
    def is_short(self) -> bool:
        return self.original_quantity < 0


@dataclass
class Quote:
    """A live or manual market snapshot for one SPX option contract."""

    expiry: date
    strike: float
    right: Right
    bid: Optional[float] = None
    ask: Optional[float] = None
    last: Optional[float] = None
    iv: Optional[float] = None
    delta: Optional[float] = None
    gamma: Optional[float] = None
    theta: Optional[float] = None
    vega: Optional[float] = None
    timestamp: datetime = field(default_factory=datetime.utcnow)
    source: MarkSource = MarkSource.MID

    @property
    def mark(self) -> Optional[float]:
        if (
            self.bid is not None
            and self.ask is not None
            and self.bid > 0
            and self.ask > 0
        ):
            return round((self.bid + self.ask) / 2, 4)

        if self.last is not None and self.last > 0:
            return self.last

        return None