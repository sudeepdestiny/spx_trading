"""
SPX Strategy Portfolio dashboard.

Run from the repository root:
    streamlit run live_trading/views/view_strategy_portfolio.py

Current quote provider:
    ManualQuoteProvider

Replace `get_quote_provider()` with IBKRQuoteAdapter after you wire the exact
IBKRClient method into `live_trading/portfolio/market_data.py`.
"""

import os
import sys
from datetime import date

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import sys
import os

# Add the project root to sys.path so absolute imports work at runtime
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from live_trading.portfolio.models import Strategy, Fill, StrategyStatus
from live_trading.portfolio.ledger import LedgerError
from live_trading.portfolio.market_data import ManualQuoteProvider
from live_trading.portfolio.payoff import build_payoff_curves
from live_trading.portfolio.repository import PortfolioRepository
from live_trading.portfolio.valuation import value_open_lots, portfolio_summary


st.set_page_config(
    page_title="SPX Strategy Portfolio",
    layout="wide",
)

DB_PATH = "data/portfolio.duckdb"


@st.cache_resource
def get_repo() -> PortfolioRepository:
    repo = PortfolioRepository(DB_PATH)
    repo.init_schema()
    return repo


@st.cache_resource
def get_quote_provider() -> ManualQuoteProvider:
    """
    Manual provider until live IBKR adapter is connected.

    Future version:
        return IBKRQuoteAdapter(st.session_state.ibkr_client)
    """
    return ManualQuoteProvider()


def render_debug_panel(
    repo: PortfolioRepository,
    strategy_id: str,
    ledger,
) -> None:
    with st.expander("Debug: portfolio storage", expanded=False):
        st.write("Database path:", os.path.abspath(DB_PATH))
        st.write("Selected strategy ID:", strategy_id)
        st.write("Loaded fills:", len(ledger.fills))
        st.write("Loaded lots:", len(ledger.lots))
        st.write("Open lots:", len(ledger.open_lots()))

        lots = [
            {
                "lot_id": lot.lot_id,
                "expiry": str(lot.expiry),
                "strike": lot.strike,
                "right": lot.right.value,
                "original_quantity": lot.original_quantity,
                "open_quantity": lot.open_quantity,
                "entry_price": lot.entry_price,
                "manual_mark": lot.manual_mark,
            }
            for lot in ledger.lots.values()
        ]

        st.json(lots)


def render_editable_open_legs(
    repo: PortfolioRepository,
    ledger,
    strategy_id: str,
    quote_provider,
) -> None:
    st.subheader("Open legs")

    open_lots = ledger.open_lots()

    if not open_lots:
        st.info("No open legs in this strategy.")
        return

    valuations = value_open_lots(
        open_lots,
        quote_provider.get_quote,
    )

    rows = []

    for lot, valuation in zip(open_lots, valuations):
        rows.append(
            {
                "Lot ID": lot.lot_id,
                "Expiry": lot.expiry.isoformat(),
                "Strike": lot.strike,
                "Right": lot.right.value,
                "Open Qty": lot.open_quantity,
                "Entry Price": lot.entry_price,
                "Manual Mark": lot.manual_mark,
                "Live Mark": valuation.mark,
                "Mark Source": valuation.mark_source,
                "Unrealized P&L": valuation.unrealized_pnl,
                "Delta": valuation.delta,
                "Theta": valuation.theta,
            }
        )

    original_df = pd.DataFrame(rows)

    edited_df = st.data_editor(
        original_df,
        key=f"open_legs_editor_{strategy_id}",
        width="stretch",
        hide_index=True,
        disabled=[
            "Lot ID",
            "Expiry",
            "Strike",
            "Right",
            "Open Qty",
            "Live Mark",
            "Mark Source",
            "Unrealized P&L",
            "Delta",
            "Theta",
        ],
        column_config={
            "Entry Price": st.column_config.NumberColumn(
                "Entry Price",
                min_value=0.0,
                step=0.05,
                format="%.2f",
                help="Correct an opening fill price only. Do not use this as an exit price.",
            ),
            "Manual Mark": st.column_config.NumberColumn(
                "Manual Mark",
                min_value=0.0,
                step=0.05,
                format="%.2f",
                help="Optional. Leave blank to use live IBKR midpoint/last price.",
            ),
            "Live Mark": st.column_config.NumberColumn(
                "Live Mark",
                format="%.2f",
            ),
            "Unrealized P&L": st.column_config.NumberColumn(
                "Unrealized P&L",
                format="$%.2f",
            ),
        },
    )

    save_col, clear_col = st.columns(2)

    with save_col:
        save_changes = st.button(
            "Save price / mark corrections",
            type="primary",
            width="stretch",
        )

    with clear_col:
        clear_marks = st.button(
            "Clear all manual marks",
            width="stretch",
        )

    if save_changes:
        changed_count = 0

        for _, edited_row in edited_df.iterrows():
            lot_id = edited_row["Lot ID"]
            lot = ledger.lots[lot_id]

            new_entry_price = float(edited_row["Entry Price"])
            raw_manual_mark = edited_row["Manual Mark"]

            new_manual_mark = (
                None
                if pd.isna(raw_manual_mark)
                else float(raw_manual_mark)
            )

            entry_changed = round(lot.entry_price, 6) != round(new_entry_price, 6)
            mark_changed = lot.manual_mark != new_manual_mark

            if entry_changed or mark_changed:
                lot.entry_price = new_entry_price
                lot.manual_mark = new_manual_mark
                repo.save_lot(lot)
                changed_count += 1

        repo.con.commit()
        st.success(f"Saved corrections for {changed_count} open leg(s).")
        st.rerun()

    if clear_marks:
        cleared = 0

        for lot in open_lots:
            if lot.manual_mark is not None:
                lot.manual_mark = None
                repo.save_lot(lot)
                cleared += 1

        repo.con.commit()
        st.success(f"Cleared manual marks from {cleared} leg(s).")
        st.rerun()

    render_close_leg_form(repo, ledger, open_lots)
    render_delete_erroneous_lot(repo, open_lots)


def render_close_leg_form(
    repo: PortfolioRepository,
    ledger,
    open_lots,
) -> None:
    st.subheader("Close / reduce a leg")

    lot_labels = {
        (
            f"{lot.expiry.isoformat()} | "
            f"{lot.strike:.0f}{lot.right.value} | "
            f"Open {lot.open_quantity:+d} | "
            f"Entry {lot.entry_price:.2f}"
        ): lot
        for lot in open_lots
    }

    with st.form("close_leg_form"):
        selected_label = st.selectbox(
            "Leg to close",
            list(lot_labels.keys()),
        )

        selected_lot = lot_labels[selected_label]

        col_1, col_2, col_3 = st.columns(3)

        with col_1:
            close_qty = st.number_input(
                "Contracts to close",
                min_value=1,
                max_value=abs(selected_lot.open_quantity),
                value=abs(selected_lot.open_quantity),
                step=1,
            )

        with col_2:
            exit_price = st.number_input(
                "Exit price",
                min_value=0.0,
                value=float(selected_lot.entry_price),
                step=0.05,
            )

        with col_3:
            close_commission = st.number_input(
                "Close commission",
                min_value=0.0,
                value=1.20,
                step=0.10,
            )

        submit_close = st.form_submit_button(
            "Book close",
            type="primary",
        )

        if submit_close:
            try:
                allocations = ledger.close_quantity(
                    expiry=selected_lot.expiry,
                    strike=selected_lot.strike,
                    right=selected_lot.right,
                    quantity_to_close=int(close_qty),
                    exit_price=float(exit_price),
                    commission=float(close_commission),
                )

                repo.persist_ledger_delta(
                    ledger=ledger,
                    new_fills=[],
                    new_lots=[],
                    new_closes=allocations,
                )

                repo.con.commit()

                realised = sum(
                    allocation.realized_pnl
                    for allocation in allocations
                )

                st.success(
                    f"Booked realised P&L: ${realised:,.2f}"
                )

                st.rerun()

            except LedgerError as exc:
                st.error(str(exc))


def render_delete_erroneous_lot(
    repo: PortfolioRepository,
    open_lots,
) -> None:
    with st.expander("Correct an erroneous manual entry"):
        st.warning(
            "Use this only for a test or accidental manual entry that was never a real trade. "
            "Do not delete a position that has booked close P&L."
        )

        lot_labels = {
            (
                f"{lot.expiry.isoformat()} | "
                f"{lot.strike:.0f}{lot.right.value} | "
                f"Qty {lot.open_quantity:+d} | "
                f"Lot {lot.lot_id[-8:]}"
            ): lot.lot_id
            for lot in open_lots
        }

        selected_label = st.selectbox(
            "Erroneous leg to delete",
            list(lot_labels.keys()),
            key="delete_erroneous_lot_select",
        )

        confirmed = st.checkbox(
            "I confirm this was an incorrect manual entry, not an executed trade.",
            key="confirm_delete_erroneous_lot",
        )

        if st.button(
            "Delete erroneous leg",
            type="secondary",
            disabled=not confirmed,
        ):
            try:
                repo.delete_untraded_lot(
                    lot_labels[selected_label]
                )
                repo.con.commit()
                st.success("The erroneous manual leg was deleted.")
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))


def render_add_leg_form(
    repo: PortfolioRepository,
    ledger,
    strategy_id: str,
) -> None:
    st.subheader("Add leg (opening fill)")

    with st.form("add_leg_form", clear_on_submit=True):
        col_1, col_2, col_3, col_4, col_5, col_6 = st.columns(6)

        with col_1:
            expiry = st.date_input(
                "Expiry",
                value=date.today(),
            )

        with col_2:
            strike = st.number_input(
                "Strike",
                min_value=0.0,
                value=6500.0,
                step=5.0,
            )

        with col_3:
            right = st.selectbox(
                "Right",
                ["P", "C"],
            )

        with col_4:
            quantity = st.number_input(
                "Quantity (+long / -short)",
                value=-1,
                step=1,
            )

        with col_5:
            entry_price = st.number_input(
                "Entry price",
                min_value=0.0,
                value=10.00,
                step=0.05,
            )

        with col_6:
            commission = st.number_input(
                "Open commission",
                min_value=0.0,
                value=1.20,
                step=0.10,
            )

        submit_add = st.form_submit_button(
            "Add leg",
            type="primary",
        )

        if submit_add:
            if int(quantity) == 0:
                st.error("Quantity cannot be zero.")
                return

            fill = Fill.create(
                strategy_id=strategy_id,
                expiry=expiry,
                strike=float(strike),
                right=right,
                quantity=int(quantity),
                price=float(entry_price),
                commission=float(commission),
                source="MANUAL",
            )

            lot = ledger.apply_fill(fill)

            repo.persist_ledger_delta(
                ledger=ledger,
                new_fills=[fill],
                new_lots=[lot],
                new_closes=[],
            )

            repo.con.commit()

            st.success(
                f"Added {quantity:+d} x SPX {strike:.0f}{right} "
                f"expiring {expiry} at {entry_price:.2f}."
            )

            st.rerun()


def render_summary_and_payoff(
    ledger,
    quote_provider,
    current_spot: float,
    target_date: date,
) -> None:
    realised = ledger.realized_pnl()
    open_lots = ledger.open_lots()

    valuations = (
        value_open_lots(
            open_lots,
            quote_provider.get_quote,
        )
        if open_lots
        else []
    )

    summary = portfolio_summary(
        valuations,
        realised,
    )

    st.subheader("Strategy summary")

    metric_1, metric_2, metric_3, metric_4, metric_5 = st.columns(5)

    metric_1.metric(
        "Realized P&L",
        f"${summary['realized_pnl']:,.2f}",
    )

    metric_2.metric(
        "Unrealized P&L",
        f"${summary['unrealized_pnl']:,.2f}",
    )

    metric_3.metric(
        "Total P&L",
        f"${summary['total_pnl']:,.2f}",
    )

    metric_4.metric(
        "Net Delta",
        f"{summary['net_delta']:.2f}",
    )

    metric_5.metric(
        "Net Theta/day",
        f"${summary['net_theta']:,.2f}",
    )

    st.subheader("Payoff graph")

    if not open_lots:
        st.info("Add an open leg to see the payoff graph.")
        return
    with st.expander("Debug: payoff inputs", expanded=False):
        st.write("Realized P&L:", realised)
        st.write("Current SPX spot:", current_spot)

        st.dataframe(
            [
                {
                    "Lot": lot.lot_id[-8:],
                    "Expiry": str(lot.expiry),
                    "Strike": lot.strike,
                    "Right": lot.right.value,
                    "Open Qty": lot.open_quantity,
                    "Entry Price": lot.entry_price,
                    "Multiplier": lot.multiplier,
                    "Manual Mark": lot.manual_mark,
                }
                for lot in open_lots
            ],
            width="stretch",
            hide_index=True,
        )


    curves = build_payoff_curves(
        open_lots=open_lots,
        realized_pnl=realised,
        current_spot=current_spot,
        target_date=target_date,
        greeks_engine=None,
    )

    chart = go.Figure()

    chart.add_trace(
        go.Scatter(
            x=curves.spot_grid,
            y=curves.expiry_pnl,
            mode="lines",
            name="On expiry",
            line={"color": "green", "width": 3},
        )
    )

    chart.add_trace(
        go.Scatter(
            x=curves.spot_grid,
            y=curves.target_pnl,
            mode="lines",
            name="Target date",
            line={"color": "royalblue", "dash": "dot", "width": 3},
        )
    )

    chart.add_hline(
        y=0,
        line_color="black",
        line_width=1,
    )

    chart.add_vline(
        x=current_spot,
        line_color="gray",
        line_dash="dash",
        annotation_text=f"SPX {current_spot:,.2f}",
    )

    for breakeven in curves.breakevens_expiry:
        chart.add_vline(
            x=breakeven,
            line_color="orange",
            line_dash="dot",
        )

    chart.update_layout(
        xaxis_title="SPX price",
        yaxis_title="P&L ($)",
        legend={"orientation": "h"},
        height=500,
        margin={"l": 20, "r": 20, "t": 20, "b": 20},
    )

    st.plotly_chart(
        chart,
        width="stretch",
    )

    stat_1, stat_2, stat_3 = st.columns(3)

    stat_1.metric(
        "Max profit in displayed range",
        f"${curves.max_profit:,.2f}",
    )

    stat_2.metric(
        "Max loss in displayed range",
        f"${curves.max_loss:,.2f}",
    )

    stat_3.metric(
        "Expiry breakevens",
        ", ".join(
            f"{value:,.2f}"
            for value in curves.breakevens_expiry
        )
        or "n/a",
    )


def main() -> None:
    repo = get_repo()
    quote_provider = get_quote_provider()

    st.title("SPX Strategy Portfolio")

    with st.sidebar:
        st.header("Strategies")

        strategies = repo.list_strategies()

        new_name = st.text_input(
            "New strategy name",
            value=f"SPX-DN-{date.today().isoformat()}",
        )

        if st.button("Create strategy"):
            strategy = Strategy.create(
                name=new_name,
                underlying="SPX",
            )
            repo.save_strategy(strategy)
            repo.con.commit()
            st.rerun()

        if not strategies:
            st.info("Create a strategy to begin.")
            st.stop()

        strategy_lookup = {
            f"{strategy.name} — {strategy.status.value}": strategy
            for strategy in strategies
        }

        selected_label = st.selectbox(
            "Open strategy",
            list(strategy_lookup.keys()),
        )

        current_strategy = strategy_lookup[selected_label]

        current_spot = st.number_input(
            "Current SPX spot",
            min_value=0.0,
            value=6560.25,
            step=0.25,
        )

        target_date = st.date_input(
            "Target-date curve",
            value=date.today(),
        )

    ledger = repo.load_ledger(
        current_strategy.strategy_id,
    )

    render_debug_panel(
        repo=repo,
        strategy_id=current_strategy.strategy_id,
        ledger=ledger,
    )

    render_add_leg_form(
        repo=repo,
        ledger=ledger,
        strategy_id=current_strategy.strategy_id,
    )

    render_editable_open_legs(
        repo=repo,
        ledger=ledger,
        strategy_id=current_strategy.strategy_id,
        quote_provider=quote_provider,
    )

    render_summary_and_payoff(
        ledger=ledger,
        quote_provider=quote_provider,
        current_spot=current_spot,
        target_date=target_date,
    )

    if (
        current_strategy.status == StrategyStatus.OPEN
        and not ledger.open_lots()
        and ledger.closes
    ):
        if st.button("Close strategy (all legs flat)"):
            repo.close_strategy(
                current_strategy.strategy_id,
            )
            repo.con.commit()
            st.rerun()


if __name__ == "__main__":
    main()