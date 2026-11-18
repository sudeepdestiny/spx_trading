"""
Tests for fill-based entry tracking and price-source labels in
live_trading/paper_trading.py.

Runs without IBKR: a fake connection manager supplies order statuses and quotes.
Covers complete, partial, cancelled, late and unfilled orders, plus unknown
prices. Run from the live_trading directory:

    python3 -m unittest tests.test_pricing_tracking -v

or directly:

    python3 tests/test_pricing_tracking.py
"""

import logging
import sys
import types
import unittest
from pathlib import Path

import pandas as pd

LIVE_TRADING_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = LIVE_TRADING_DIR.parent
for p in (str(LIVE_TRADING_DIR), str(REPO_ROOT), str(REPO_ROOT / "backtest")):
    if p not in sys.path:
        sys.path.insert(0, p)

# Stub config before importing paper_trading/strategy so no server paths are touched.
config_stub = types.ModuleType("config")
config_stub.STRATEGY_CONFIG = {
    "lot_size": 100,
    "sell_delta": 0.3,
    "hedge_dist": 80,
    "pos_delta_thresh": 0.25,
    "max_adjusts": 3,
    "max_loss_stop_loss": 250,
}
config_stub.US_MARKET_HOLIDAYS = []
config_stub.apikey_config = {}
config_stub.LOG_PATH = Path("/tmp/spx_test_logs")
sys.modules["config"] = config_stub

import paper_trading  # noqa: E402
from paper_trading import PaperTrader  # noqa: E402


class FakeWrapper:
    def __init__(self):
        self.filled_orders = {}


class FakeConnMgr:
    """Minimal stand-in for IBKRConnectionManager used by PaperTrader."""

    def __init__(self):
        self.wrapper = FakeWrapper()
        self.is_connected = True
        self.wait_for_fill_calls = 0

    def get_wrapper(self):
        return self.wrapper

    def get_client(self):
        return None

    def get_account(self):
        return "DU123456"

    def wait_for_fill(self, order_id, timeout=10.0):
        self.wait_for_fill_calls += 1
        return self.wrapper.filled_orders.get(order_id, {}).get("status") == "Filled"


class LogCapture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())

    def text(self):
        return "\n".join(self.messages)


def build_chain():
    """Chain with clean deltas, quotes and both option types."""
    rows = []
    for strike in range(7000, 7605, 5):
        frac = (strike - 7000) / 600.0
        call_delta = round(0.05 + 0.9 * frac, 4)
        put_delta = -round(0.95 - 0.9 * frac, 4)
        call_mid = round(30.0 - 25.0 * frac, 2)
        put_mid = round(5.0 + 25.0 * frac, 2)
        rows.append(
            {
                "underlying": "SPX",
                "expiry": "20261120",
                "strike": float(strike),
                "spot_price": 7300.0,
                "dte_days": 2.0,
                "call_delta": call_delta,
                "put_delta": put_delta,
                "call_mid": call_mid,
                "put_mid": put_mid,
                "call_bid": round(call_mid - 0.5, 2),
                "call_ask": round(call_mid + 0.5, 2),
                "put_bid": round(put_mid - 0.5, 2),
                "put_ask": round(put_mid + 0.5, 2),
            }
        )
    return pd.DataFrame(rows)


def make_trader(fill_price=7.77, short_status="Filled"):
    """PaperTrader with fake order placement. short_status controls short-leg fills."""
    trader = PaperTrader(conn_mgr=FakeConnMgr(), is_paper=True)
    trader._preview_selected_condor = lambda **kwargs: {"can_trade": True, "reason": "ok"}

    state = {"next_oid": 1000}

    def fake_place(position, quantity):
        state["next_oid"] += 1
        oid = state["next_oid"]
        is_short = position["action"] == "SELL"
        status = short_status if is_short else "Filled"
        if status is not None:
            trader.conn_mgr.wrapper.filled_orders[oid] = {
                "status": status,
                "filled": quantity if status == "Filled" else 0,
                "avg_price": fill_price,
            }
        return oid

    trader._place_option_order = fake_place
    return trader


def open_cycle(trader, **kwargs):
    chain = build_chain()
    positions = trader.select_and_open_positions(chain, target_delta=0.30, quantity=1, hedge_dist=80, **kwargs)
    return chain, positions


class OptionPriceSourceTests(unittest.TestCase):
    def setUp(self):
        self.trader = PaperTrader(conn_mgr=FakeConnMgr(), is_paper=True)

    def test_short_uses_bid_long_uses_ask(self):
        row = pd.Series({"call_bid": 10.0, "call_ask": 11.0, "call_mid": 10.5})
        self.assertEqual(self.trader._option_price_with_source(row, "CE", "S"), (10.0, "bid"))
        self.assertEqual(self.trader._option_price_with_source(row, "CE", "B"), (11.0, "ask"))

    def test_fallback_order_labels_source(self):
        row = pd.Series({"call_mid": 10.5, "call_model_price": 10.4, "call_last": 10.2})
        self.assertEqual(self.trader._option_price_with_source(row, "CE", "S"), (10.5, "mid"))

        row = pd.Series({"call_model_price": 10.4, "call_last": 10.2})
        self.assertEqual(self.trader._option_price_with_source(row, "CE", "S"), (10.4, "model_price"))

        row = pd.Series({"call_last": 10.2})
        self.assertEqual(self.trader._option_price_with_source(row, "CE", "S"), (10.2, "last"))

    def test_intrinsic_floor_labelled(self):
        row = pd.Series({"spot_price": 7300.0, "strike": 7200.0})
        self.assertEqual(self.trader._option_price_with_source(row, "CE", "S"), (100.0, "intrinsic_floor"))

        row = pd.Series({"spot_price": 7300.0, "strike": 7400.0})
        self.assertEqual(self.trader._option_price_with_source(row, "CE", "S"), (0.05, "intrinsic_floor"))

    def test_unknown_price_returns_none_pair(self):
        row = pd.Series({"strike": 7000.0})
        self.assertEqual(self.trader._option_price_with_source(row, "CE", "S"), (None, None))

    def test_option_price_still_returns_price_only(self):
        row = pd.Series({"put_bid": 3.0, "put_ask": 3.5})
        self.assertEqual(self.trader._option_price(row, "PE", "S"), 3.0)


class EntryFillTrackingTests(unittest.TestCase):
    def test_complete_fills_rebase_entries(self):
        trader = make_trader(fill_price=7.77)
        _, positions = open_cycle(trader)

        self.assertEqual(len(positions), 4)
        for pos in positions:
            self.assertEqual(pos["status"], "FILLED")
            self.assertEqual(pos["entry_price"], 7.77)
            self.assertEqual(pos["price"], 7.77)
            self.assertEqual(pos["entry_price_source"], "ibkr_fill")
            self.assertNotEqual(pos["quote_at_selection"], 7.77)

    def test_fractional_average_fill_kept_at_full_precision(self):
        trader = make_trader(fill_price=17.885)
        _, positions = open_cycle(trader)

        for pos in positions:
            self.assertEqual(pos["entry_price"], 17.885)  # not rounded to 17.89
            self.assertEqual(pos["price"], 17.885)
            self.assertEqual(pos["entry_price_source"], "ibkr_fill")

    def test_unfilled_short_keeps_quote_basis_and_existing_status(self):
        # Short-leg status stays FILLED exactly as the current code does; this
        # test pins that pre-existing behavior, it does not endorse it.
        trader = make_trader(short_status=None)
        _, positions = open_cycle(trader)

        self.assertEqual(len(positions), 4)
        for pos in positions:
            if pos["side"] == "S":
                self.assertEqual(pos["status"], "FILLED")  # pre-existing behavior, unchanged
                self.assertEqual(pos["entry_price_source"], "selection_quote")
                self.assertEqual(pos["entry_price"], pos["quote_at_selection"])
            else:
                self.assertEqual(pos["entry_price_source"], "ibkr_fill")

    def test_partial_fill_is_not_treated_as_actual_fill(self):
        trader = make_trader(short_status="PartiallyFilled")
        _, positions = open_cycle(trader)

        for pos in positions:
            if pos["side"] == "S":
                self.assertEqual(pos["entry_price_source"], "selection_quote")
                self.assertEqual(pos["entry_price"], pos["quote_at_selection"])

    def test_cancelled_order_is_not_treated_as_actual_fill(self):
        trader = make_trader(short_status="Cancelled")
        _, positions = open_cycle(trader)

        for pos in positions:
            if pos["side"] == "S":
                self.assertEqual(pos["entry_price_source"], "selection_quote")

    def test_late_fill_applied_on_later_refresh_without_waiting(self):
        trader = make_trader(short_status=None)  # shorts unconfirmed at placement
        _, positions = open_cycle(trader)
        short = next(p for p in positions if p["side"] == "S")
        quote = short["entry_price"]

        waits_before = trader.conn_mgr.wait_for_fill_calls
        trader._refresh_entries_from_fills(positions)  # nothing arrived yet
        self.assertEqual(short["entry_price"], quote)
        self.assertEqual(short["entry_price_source"], "selection_quote")

        # Fill completes later; refresh picks it up with no wait_for_fill call.
        trader.conn_mgr.wrapper.filled_orders[short["order_id"]] = {
            "status": "Filled",
            "filled": 1,
            "avg_price": 8.88,
        }
        trader._refresh_entries_from_fills(positions)
        self.assertEqual(trader.conn_mgr.wait_for_fill_calls, waits_before)
        self.assertEqual(short["entry_price"], 8.88)
        self.assertEqual(short["entry_price_source"], "ibkr_fill")
        self.assertEqual(short["quote_at_selection"], quote)

        # Idempotent: further refreshes change nothing.
        trader.conn_mgr.wrapper.filled_orders[short["order_id"]]["avg_price"] = 99.99
        trader._refresh_entries_from_fills(positions)
        self.assertEqual(short["entry_price"], 8.88)

    def test_refresh_skips_partial_and_cancelled(self):
        trader = make_trader(short_status=None)
        _, positions = open_cycle(trader)
        short = next(p for p in positions if p["side"] == "S")

        for status in ("PartiallyFilled", "Cancelled", "Submitted", "PendingSubmit"):
            trader.conn_mgr.wrapper.filled_orders[short["order_id"]] = {
                "status": status,
                "filled": 0,
                "avg_price": 8.88,
            }
            trader._refresh_entries_from_fills(positions)
            self.assertEqual(short["entry_price_source"], "selection_quote", status)

    def test_refresh_ignores_legs_without_source_label(self):
        # Positions recorded before this patch have no entry_price_source.
        trader = PaperTrader(conn_mgr=FakeConnMgr(), is_paper=True)
        legacy = [{"status": "OPEN", "order_id": 42, "entry_price": 10.0, "price": 10.0, "type": "CE", "strike": 7000.0}]
        trader.conn_mgr.wrapper.filled_orders[42] = {"status": "Filled", "filled": 1, "avg_price": 9.0}
        trader._refresh_entries_from_fills(legacy)
        self.assertEqual(legacy[0]["entry_price"], 10.0)

    def test_adjustment_submit_adds_no_wait(self):
        trader = make_trader()
        chain = build_chain()
        row = chain[chain["strike"] == 7200.0].iloc[0]
        pos = trader._position_from_row(row, "S", "SELL", "CE", 1, cycle_id=9)
        self.assertEqual(pos["entry_price_source"], "selection_quote")
        self.assertEqual(pos["quote_at_selection"], pos["entry_price"])

        waits_before = trader.conn_mgr.wait_for_fill_calls
        trader._submit_new_position(pos)
        self.assertEqual(trader.conn_mgr.wait_for_fill_calls, waits_before)  # fire-and-forget, unchanged
        self.assertEqual(pos["status"], "SUBMITTED")  # pre-existing behavior, unchanged

        # Completed fill is picked up by the next monitoring pass instead.
        trader._refresh_entries_from_fills([pos])
        self.assertEqual(pos["entry_price"], 7.77)
        self.assertEqual(pos["entry_price_source"], "ibkr_fill")

    def test_adopted_ibkr_positions_label_avg_cost_basis(self):
        trader = PaperTrader(conn_mgr=FakeConnMgr(), is_paper=True)
        capture = LogCapture()
        logging.getLogger("paper_trading").addHandler(capture)
        try:
            added = trader._load_ibkr_positions_into_tracker([
                {"symbol": "SPX", "expiry": "20261120", "strike": 7460.0, "right": "C", "position": -1, "avgCost": 17.90},
                {"symbol": "SPX", "expiry": "20261120", "strike": 7540.0, "right": "C", "position": 1, "avgCost": 532.0},
            ])
        finally:
            logging.getLogger("paper_trading").removeHandler(capture)

        self.assertTrue(added)
        legs = trader.live_positions[1]["positions"]
        short_leg = next(l for l in legs if l["side"] == "S")
        long_leg = next(l for l in legs if l["side"] == "B")
        self.assertEqual(short_leg["entry_price"], 17.90)
        self.assertEqual(short_leg["entry_price_source"], "ibkr_avg_cost")
        self.assertEqual(long_leg["entry_price"], 5.32)  # >500 divided by multiplier
        self.assertEqual(long_leg["entry_price_source"], "ibkr_avg_cost")
        # Labeling is neutral: no fee/commission interpretation is asserted.
        self.assertNotIn("commission", capture.text().lower())


class CloseAndPnlTests(unittest.TestCase):
    def test_close_books_quote_estimate_with_source_label(self):
        # Realized P&L intentionally stays quote-based in this patch; the
        # source label makes the estimate explicit and no fill is consulted.
        trader = make_trader(fill_price=7.77)
        chain, positions = open_cycle(trader)
        short = next(p for p in positions if p["side"] == "S")

        trader.conn_mgr.wrapper.filled_orders.clear()
        trader._place_option_order = lambda position, quantity: 9001
        # Even if a close fill shows up instantly, this patch must not book it.
        trader.conn_mgr.wrapper.filled_orders[9001] = {"status": "Filled", "filled": 1, "avg_price": 0.01}

        waits_before = trader.conn_mgr.wait_for_fill_calls
        closed = trader._close_position(short, chain, reason="test")
        self.assertEqual(trader.conn_mgr.wait_for_fill_calls, waits_before)  # no wait added

        col = "call_ask" if short["type"] == "CE" else "put_ask"
        expected_ask = float(chain.loc[chain["strike"] == short["strike"], col].iloc[0])
        self.assertEqual(closed["close_price"], expected_ask)
        self.assertEqual(closed["close_price_source"], "ask")
        self.assertAlmostEqual(closed["pnl"], (7.77 - expected_ask) * 100)

    def test_close_unknown_price_fails_as_before(self):
        trader = make_trader(fill_price=7.77)
        _, positions = open_cycle(trader)
        short = next(p for p in positions if p["side"] == "S")

        # Chain row with no usable price columns at all.
        empty_chain = pd.DataFrame([{"strike": short["strike"], "expiry": "20261120"}])
        closed = trader._close_position(short, empty_chain, reason="test")
        self.assertIsNone(closed)
        self.assertNotEqual(short["status"], "CLOSED")

    def test_cycle_pnl_math_unchanged_and_sources_logged(self):
        trader = make_trader(fill_price=7.77)
        chain, positions = open_cycle(trader)
        cycle_id = positions[0]["cycle_id"]
        pos_data = trader.live_positions[cycle_id]

        capture = LogCapture()
        pt_logger = logging.getLogger("paper_trading")
        pt_logger.addHandler(capture)
        try:
            total = trader._calculate_current_cycle_pnl(chain, pos_data)
        finally:
            pt_logger.removeHandler(capture)

        expected = 0.0
        for pos in positions:
            row = chain[chain["strike"] == pos["strike"]].iloc[0]
            close_side = "B" if pos["side"] == "S" else "S"
            curr, _ = trader._option_price_with_source(row, pos["type"], close_side)
            expected += (7.77 - curr) * 100 if pos["side"] == "S" else (curr - 7.77) * 100
        self.assertAlmostEqual(total, expected)

        leg_lines = [m for m in capture.messages if m.startswith("  Leg ")]
        self.assertEqual(len(leg_lines), 4)
        for line in leg_lines:
            self.assertIn("entry_price=7.77 (source=ibkr_fill)", line)
            curr_part = line.split("curr_price=")[1]
            self.assertTrue("source=ask" in curr_part or "source=bid" in curr_part, line)

    def test_cycle_pnl_unknown_price_uses_entry_fallback_label(self):
        trader = make_trader(fill_price=7.77)
        _, positions = open_cycle(trader)
        cycle_id = positions[0]["cycle_id"]
        pos_data = trader.live_positions[cycle_id]

        empty_chain = pd.DataFrame(columns=["strike"])
        capture = LogCapture()
        pt_logger = logging.getLogger("paper_trading")
        pt_logger.addHandler(capture)
        try:
            total = trader._calculate_current_cycle_pnl(empty_chain, pos_data)
        finally:
            pt_logger.removeHandler(capture)

        self.assertEqual(total, 0.0)  # curr falls back to entry, so zero P&L
        for line in [m for m in capture.messages if m.startswith("  Leg ")]:
            self.assertIn("source=entry_price_fallback", line)


if __name__ == "__main__":
    unittest.main(verbosity=2)
