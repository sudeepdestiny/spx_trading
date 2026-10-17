"""
Booking logic: opening lots and FIFO closes with permanent realized P&L.

This is the piece that makes "close a position and keep the booked profit"
work correctly, including partial closes and rolls.
"""

from __future__ import annotations

from datetime import date
from typing import Dict, List, Tuple

from live_trading.portfolio.models import Fill, Lot, CloseAllocation, Right, new_id


class LedgerError(Exception):
    pass


class StrategyLedger:
    """
    In-memory ledger for ONE strategy. The repository layer is responsible
    for loading/saving these lists to persistent storage (DuckDB).
    """

    def __init__(self, strategy_id: str):
        self.strategy_id = strategy_id
        self.lots: Dict[str, Lot] = {}
        self.fills: List[Fill] = []
        self.closes: List[CloseAllocation] = []

    # ---------- opening ----------

    def apply_fill(self, fill: Fill) -> Lot:
        if fill.strategy_id != self.strategy_id:
            raise LedgerError("Fill belongs to a different strategy")
        lot = Lot(
            lot_id=new_id("lot"),
            strategy_id=self.strategy_id,
            opening_fill_id=fill.fill_id,
            expiry=fill.expiry,
            strike=fill.strike,
            right=fill.right,
            original_quantity=fill.quantity,
            open_quantity=fill.quantity,
            entry_price=fill.price,
            multiplier=fill.multiplier,
        )
        self.fills.append(fill)
        self.lots[lot.lot_id] = lot
        return lot

    # ---------- closing ----------

    def _matching_open_lots(self, expiry: date, strike: float, right: Right, direction_sign: int) -> List[Lot]:
        """
        direction_sign: sign of the ORIGINAL lot quantity we intend to reduce.
        +1 to close longs, -1 to close shorts.
        Sorted oldest-first for FIFO.
        """
        matches = [
            lot for lot in self.lots.values()
            if lot.expiry == expiry and lot.strike == strike and lot.right == right
            and lot.is_open
            and (1 if lot.open_quantity > 0 else -1) == direction_sign
        ]
        # FIFO by actual fill timestamp (oldest opened first), not by random fill id
        fill_ts = {f.fill_id: f.timestamp for f in self.fills}
        return sorted(matches, key=lambda l: fill_ts.get(l.opening_fill_id, l.lot_id))

    def close_quantity(
        self,
        expiry: date,
        strike: float,
        right: Right,
        quantity_to_close: int,
        exit_price: float,
        commission: float = 0.0,
    ) -> List[CloseAllocation]:
        """
        quantity_to_close: positive int, number of contracts to close.
        Automatically figures out whether this reduces a long or a short
        lot based on what is open for that contract key.

        Returns the list of CloseAllocation rows created (can be >1 if the
        close spans multiple lots, e.g. two separate opening fills).
        """
        if quantity_to_close <= 0:
            raise LedgerError("quantity_to_close must be positive")

        long_lots = self._matching_open_lots(expiry, strike, right, +1)
        short_lots = self._matching_open_lots(expiry, strike, right, -1)

        if long_lots and short_lots:
            raise LedgerError("Both long and short open lots exist for this contract; resolve manually")
        if not long_lots and not short_lots:
            raise LedgerError("No open position found for this contract")

        lots = long_lots if long_lots else short_lots
        is_long = bool(long_lots)

        remaining = quantity_to_close
        allocations: List[CloseAllocation] = []

        for lot in lots:
            if remaining <= 0:
                break
            available = abs(lot.open_quantity)
            take = min(available, remaining)
            if take == 0:
                continue

            if is_long:
                realized = (exit_price - lot.entry_price) * take * lot.multiplier
                lot.open_quantity -= take
            else:
                realized = (lot.entry_price - exit_price) * take * lot.multiplier
                lot.open_quantity += take

            realized -= commission * (take / quantity_to_close)

            alloc = CloseAllocation(
                close_id=new_id("close"),
                strategy_id=self.strategy_id,
                lot_id=lot.lot_id,
                expiry=expiry,
                strike=strike,
                right=right,
                closed_quantity=take,
                opening_price=lot.entry_price,
                closing_price=exit_price,
                commission=commission * (take / quantity_to_close),
                realized_pnl=round(realized, 2),
            )
            allocations.append(alloc)
            self.closes.append(alloc)
            remaining -= take

        if remaining > 0:
            raise LedgerError(
                f"Requested close of {quantity_to_close} exceeds open quantity "
                f"({quantity_to_close - remaining} available)"
            )

        return allocations

    def roll(
        self,
        old_expiry: date, old_strike: float, old_right: Right, roll_quantity: int, exit_price: float,
        new_expiry: date, new_strike: float, new_right: Right, new_quantity: int, entry_price: float,
        commission: float = 0.0,
    ) -> Tuple[List[CloseAllocation], Lot]:
        """A roll is just a close followed by a new fill, recorded as two auditable events."""
        allocations = self.close_quantity(old_expiry, old_strike, old_right, roll_quantity, exit_price, commission)
        new_fill = Fill.create(self.strategy_id, new_expiry, new_strike, new_right, new_quantity, entry_price)
        new_lot = self.apply_fill(new_fill)
        return allocations, new_lot

    # ---------- aggregates ----------

    def realized_pnl(self) -> float:
        return round(sum(c.realized_pnl for c in self.closes), 2)

    def open_lots(self) -> List[Lot]:
        return [lot for lot in self.lots.values() if lot.is_open]

    def total_commission(self) -> float:
        fill_comm = sum(f.commission for f in self.fills)
        close_comm = sum(c.commission for c in self.closes)
        return round(fill_comm + close_comm, 2)
