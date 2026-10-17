"""Live / manual mark-to-market valuation for open option lots."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from live_trading.portfolio.models import Lot, Quote, Right


@dataclass
class LegValuation:
    lot_id: str
    expiry: str
    strike: float
    right: str
    open_quantity: int
    entry_price: float
    mark: Optional[float]
    mark_source: str
    unrealized_pnl: Optional[float]
    delta: Optional[float]
    gamma: Optional[float]
    theta: Optional[float]
    vega: Optional[float]


QuoteFn = Callable[[object, float, Right], Optional[Quote]]


def value_lot(
    lot: Lot,
    quote: Optional[Quote],
) -> LegValuation:
    if lot.manual_mark is not None:
        mark = lot.manual_mark
        mark_source = "MANUAL"
    else:
        mark = quote.mark if quote else None
        mark_source = quote.source.value if quote else "NONE"

    unrealized = None

    if mark is not None:
        unrealized = round(
            (mark - lot.entry_price)
            * lot.open_quantity
            * lot.multiplier,
            2,
        )

    return LegValuation(
        lot_id=lot.lot_id,
        expiry=lot.expiry.isoformat(),
        strike=lot.strike,
        right=lot.right.value,
        open_quantity=lot.open_quantity,
        entry_price=lot.entry_price,
        mark=mark,
        mark_source=mark_source,
        unrealized_pnl=unrealized,
        delta=(
            quote.delta * lot.open_quantity
            if quote and quote.delta is not None
            else None
        ),
        gamma=(
            quote.gamma * lot.open_quantity
            if quote and quote.gamma is not None
            else None
        ),
        theta=(
            quote.theta * lot.open_quantity
            if quote and quote.theta is not None
            else None
        ),
        vega=(
            quote.vega * lot.open_quantity
            if quote and quote.vega is not None
            else None
        ),
    )


def value_open_lots(
    lots: List[Lot],
    get_quote: QuoteFn,
) -> List[LegValuation]:
    return [
        value_lot(
            lot,
            get_quote(lot.expiry, lot.strike, lot.right),
        )
        for lot in lots
    ]


def portfolio_summary(
    valuations: List[LegValuation],
    realized_pnl: float,
) -> Dict:
    unrealized = sum(
        valuation.unrealized_pnl
        for valuation in valuations
        if valuation.unrealized_pnl is not None
    )

    missing_marks = [
        valuation
        for valuation in valuations
        if valuation.mark is None
    ]

    return {
        "realized_pnl": round(realized_pnl, 2),
        "unrealized_pnl": round(unrealized, 2),
        "total_pnl": round(realized_pnl + unrealized, 2),
        "net_delta": round(
            sum(
                valuation.delta
                for valuation in valuations
                if valuation.delta is not None
            ),
            4,
        ),
        "net_gamma": round(
            sum(
                valuation.gamma
                for valuation in valuations
                if valuation.gamma is not None
            ),
            4,
        ),
        "net_theta": round(
            sum(
                valuation.theta
                for valuation in valuations
                if valuation.theta is not None
            ),
            2,
        ),
        "net_vega": round(
            sum(
                valuation.vega
                for valuation in valuations
                if valuation.vega is not None
            ),
            2,
        ),
        "legs_missing_marks": len(missing_marks),
    }