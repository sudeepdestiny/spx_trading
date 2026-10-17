"""
DuckDB persistence for strategy portfolios.

The schema uses `option_right`, rather than `right`, because RIGHT is a SQL
keyword/function in several SQL dialects, including DuckDB contexts.
"""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional

import duckdb

from live_trading.portfolio.models import (
    Strategy,
    Fill,
    Lot,
    CloseAllocation,
    Right,
    StrategyStatus,
)
from live_trading.portfolio.ledger import StrategyLedger


CREATE_STRATEGIES_TABLE = """
CREATE TABLE IF NOT EXISTS strategies (
    strategy_id VARCHAR PRIMARY KEY,
    name VARCHAR NOT NULL,
    underlying VARCHAR NOT NULL,
    status VARCHAR NOT NULL,
    created_at TIMESTAMP NOT NULL,
    closed_at TIMESTAMP,
    notes VARCHAR
)
"""

CREATE_FILLS_TABLE = """
CREATE TABLE IF NOT EXISTS fills (
    fill_id VARCHAR PRIMARY KEY,
    strategy_id VARCHAR NOT NULL,
    expiry DATE NOT NULL,
    strike DOUBLE NOT NULL,
    option_right VARCHAR NOT NULL,
    quantity INTEGER NOT NULL,
    price DOUBLE NOT NULL,
    multiplier INTEGER NOT NULL,
    commission DOUBLE NOT NULL,
    timestamp TIMESTAMP NOT NULL,
    source VARCHAR NOT NULL
)
"""

CREATE_LOTS_TABLE = """
CREATE TABLE IF NOT EXISTS lots (
    lot_id VARCHAR PRIMARY KEY,
    strategy_id VARCHAR NOT NULL,
    opening_fill_id VARCHAR NOT NULL,
    expiry DATE NOT NULL,
    strike DOUBLE NOT NULL,
    option_right VARCHAR NOT NULL,
    original_quantity INTEGER NOT NULL,
    open_quantity INTEGER NOT NULL,
    entry_price DOUBLE NOT NULL,
    multiplier INTEGER NOT NULL,
    manual_mark DOUBLE
)
"""

CREATE_CLOSE_ALLOCATIONS_TABLE = """
CREATE TABLE IF NOT EXISTS close_allocations (
    close_id VARCHAR PRIMARY KEY,
    strategy_id VARCHAR NOT NULL,
    lot_id VARCHAR NOT NULL,
    expiry DATE NOT NULL,
    strike DOUBLE NOT NULL,
    option_right VARCHAR NOT NULL,
    closed_quantity INTEGER NOT NULL,
    opening_price DOUBLE NOT NULL,
    closing_price DOUBLE NOT NULL,
    commission DOUBLE NOT NULL,
    realized_pnl DOUBLE NOT NULL,
    timestamp TIMESTAMP NOT NULL
)
"""


class PortfolioRepository:
    def __init__(self, db_path: str = "data/portfolio.duckdb"):
        self.db_path = db_path
        self.con = duckdb.connect(db_path)

    def init_schema(self) -> None:
        self.con.execute(CREATE_STRATEGIES_TABLE)
        self.con.execute(CREATE_FILLS_TABLE)
        self.con.execute(CREATE_LOTS_TABLE)
        self.con.execute(CREATE_CLOSE_ALLOCATIONS_TABLE)
        self._migrate_schema()

    def _migrate_schema(self) -> None:
        """Safely add `manual_mark` for databases created before this change."""
        existing_columns = {
            row[1]
            for row in self.con.execute("PRAGMA table_info('lots')").fetchall()
        }

        if "manual_mark" not in existing_columns:
            self.con.execute(
                "ALTER TABLE lots ADD COLUMN manual_mark DOUBLE"
            )

    # ------------------------------------------------------------------
    # Strategy operations
    # ------------------------------------------------------------------

    def save_strategy(self, strategy: Strategy) -> None:
        self.con.execute(
            """
            INSERT OR REPLACE INTO strategies (
                strategy_id,
                name,
                underlying,
                status,
                created_at,
                closed_at,
                notes
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                strategy.strategy_id,
                strategy.name,
                strategy.underlying,
                strategy.status.value,
                strategy.created_at,
                strategy.closed_at,
                strategy.notes,
            ],
        )

    def list_strategies(
        self,
        status: Optional[StrategyStatus] = None,
    ) -> List[Strategy]:
        query = """
        SELECT
            strategy_id,
            name,
            underlying,
            status,
            created_at,
            closed_at,
            notes
        FROM strategies
        """
        params = []

        if status is not None:
            query += " WHERE status = ?"
            params.append(status.value)

        query += " ORDER BY created_at DESC"

        rows = self.con.execute(query, params).fetchall()

        return [
            Strategy(
                strategy_id=row[0],
                name=row[1],
                underlying=row[2],
                status=StrategyStatus(row[3]),
                created_at=row[4],
                closed_at=row[5],
                notes=row[6] or "",
            )
            for row in rows
        ]

    def close_strategy(self, strategy_id: str) -> None:
        self.con.execute(
            """
            UPDATE strategies
            SET status = ?, closed_at = ?
            WHERE strategy_id = ?
            """,
            [
                StrategyStatus.CLOSED.value,
                datetime.utcnow(),
                strategy_id,
            ],
        )

    # ------------------------------------------------------------------
    # Persist opening fills and position lots
    # ------------------------------------------------------------------

    def save_fill(self, fill: Fill) -> None:
        self.con.execute(
            """
            INSERT INTO fills (
                fill_id,
                strategy_id,
                expiry,
                strike,
                option_right,
                quantity,
                price,
                multiplier,
                commission,
                timestamp,
                source
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                fill.fill_id,
                fill.strategy_id,
                fill.expiry,
                fill.strike,
                fill.right.value,
                fill.quantity,
                fill.price,
                fill.multiplier,
                fill.commission,
                fill.timestamp,
                fill.source,
            ],
        )

    def save_lot(self, lot: Lot) -> None:
        self.con.execute(
            """
            INSERT OR REPLACE INTO lots (
                lot_id,
                strategy_id,
                opening_fill_id,
                expiry,
                strike,
                option_right,
                original_quantity,
                open_quantity,
                entry_price,
                multiplier,
                manual_mark
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                lot.lot_id,
                lot.strategy_id,
                lot.opening_fill_id,
                lot.expiry,
                lot.strike,
                lot.right.value,
                lot.original_quantity,
                lot.open_quantity,
                lot.entry_price,
                lot.multiplier,
                lot.manual_mark,
            ],
        )

    def save_close(self, close: CloseAllocation) -> None:
        self.con.execute(
            """
            INSERT INTO close_allocations (
                close_id,
                strategy_id,
                lot_id,
                expiry,
                strike,
                option_right,
                closed_quantity,
                opening_price,
                closing_price,
                commission,
                realized_pnl,
                timestamp
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                close.close_id,
                close.strategy_id,
                close.lot_id,
                close.expiry,
                close.strike,
                close.right.value,
                close.closed_quantity,
                close.opening_price,
                close.closing_price,
                close.commission,
                close.realized_pnl,
                close.timestamp,
            ],
        )

    # ------------------------------------------------------------------
    # Load complete strategy ledger after Streamlit reruns
    # ------------------------------------------------------------------

    def load_ledger(self, strategy_id: str) -> StrategyLedger:
        ledger = StrategyLedger(strategy_id)

        # Load fills first. FIFO close logic relies on opening timestamps.
        fill_rows = self.con.execute(
            """
            SELECT
                fill_id,
                strategy_id,
                expiry,
                strike,
                option_right,
                quantity,
                price,
                multiplier,
                commission,
                timestamp,
                source
            FROM fills
            WHERE strategy_id = ?
            ORDER BY timestamp, fill_id
            """,
            [strategy_id],
        ).fetchall()

        for row in fill_rows:
            ledger.fills.append(
                Fill(
                    fill_id=row[0],
                    strategy_id=row[1],
                    expiry=row[2],
                    strike=row[3],
                    right=Right(row[4]),
                    quantity=row[5],
                    price=row[6],
                    multiplier=row[7],
                    commission=row[8],
                    timestamp=row[9],
                    source=row[10],
                )
            )

        lot_rows = self.con.execute(
            """
            SELECT
                lot_id,
                strategy_id,
                opening_fill_id,
                expiry,
                strike,
                option_right,
                original_quantity,
                open_quantity,
                entry_price,
                multiplier,
                manual_mark
            FROM lots
            WHERE strategy_id = ?
            ORDER BY expiry, strike, option_right, lot_id
            """,
            [strategy_id],
        ).fetchall()

        for row in lot_rows:
            lot = Lot(
                lot_id=row[0],
                strategy_id=row[1],
                opening_fill_id=row[2],
                expiry=row[3],
                strike=row[4],
                right=Right(row[5]),
                original_quantity=row[6],
                open_quantity=row[7],
                entry_price=row[8],
                multiplier=row[9],
                manual_mark=row[10],
            )
            ledger.lots[lot.lot_id] = lot

        close_rows = self.con.execute(
            """
            SELECT
                close_id,
                strategy_id,
                lot_id,
                expiry,
                strike,
                option_right,
                closed_quantity,
                opening_price,
                closing_price,
                commission,
                realized_pnl,
                timestamp
            FROM close_allocations
            WHERE strategy_id = ?
            ORDER BY timestamp, close_id
            """,
            [strategy_id],
        ).fetchall()

        for row in close_rows:
            ledger.closes.append(
                CloseAllocation(
                    close_id=row[0],
                    strategy_id=row[1],
                    lot_id=row[2],
                    expiry=row[3],
                    strike=row[4],
                    right=Right(row[5]),
                    closed_quantity=row[6],
                    opening_price=row[7],
                    closing_price=row[8],
                    commission=row[9],
                    realized_pnl=row[10],
                    timestamp=row[11],
                )
            )

        return ledger

    # ------------------------------------------------------------------
    # Correct/delete manual data
    # ------------------------------------------------------------------

    def delete_untraded_lot(self, lot_id: str) -> None:
        """
        Delete only an erroneous manual entry that has never been closed.
        A lot with close allocations has realised P&L history and must not be
        deleted, because that would corrupt the strategy audit trail.
        """
        close_count = self.con.execute(
            """
            SELECT COUNT(*)
            FROM close_allocations
            WHERE lot_id = ?
            """,
            [lot_id],
        ).fetchone()[0]

        if close_count > 0:
            raise ValueError(
                "Cannot delete this lot because it has booked close events. "
                "Use a correction or a new compensating trade instead."
            )

        self.con.execute(
            "DELETE FROM lots WHERE lot_id = ?",
            [lot_id],
        )

        self.con.execute(
            "DELETE FROM fills WHERE fill_id = ("
            "SELECT opening_fill_id FROM lots WHERE lot_id = ?"
            ")",
            [lot_id],
        )

    # ------------------------------------------------------------------
    # Batch persistence
    # ------------------------------------------------------------------

    def persist_ledger_delta(
        self,
        ledger: StrategyLedger,
        new_fills: List[Fill],
        new_lots: List[Lot],
        new_closes: List[CloseAllocation],
    ) -> None:
        for fill in new_fills:
            self.save_fill(fill)

        for close in new_closes:
            self.save_close(close)

        # Save all current lots because a close changes open_quantity.
        for lot in ledger.lots.values():
            self.save_lot(lot)