"""
SPX option-chain selector.

Run:
    streamlit run live_trading/views/view_option_chain.py
"""

from __future__ import annotations

import os
import sys
from datetime import date

import streamlit as st

# Add project root to sys.path
sys.path.insert(
    0,
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")),
)

from live_trading.ibkr_client import IBKRClient
from live_trading.portfolio.option_chain_service import SPXOptionChainService


st.set_page_config(
    page_title="SPX Option Chain",
    page_icon="📊",
    layout="wide",
)


@st.cache_resource
def get_ibkr_client():
    """
    Initializes and connects the IBKR client.
    """
    client = IBKRClient()
    client.connect()
    return client


@st.cache_resource
def get_chain_service():
    return SPXOptionChainService(
        ibkr_client=get_ibkr_client(),
        symbol="SPX",
        cache_seconds=8,
        strike_padding=300,
    )


def initialise_state():
    if "selected_chain_legs" not in st.session_state:
        st.session_state.selected_chain_legs = {}


def add_leg(
    expiry: date,
    strike: float,
    right: str,
    quantity: int,
    estimated_price: float,
):
    key = f"{expiry.isoformat()}::{strike:.2f}::{right}"

    st.session_state.selected_chain_legs[key] = {
        "expiry": expiry,
        "strike": strike,
        "right": right,
        "quantity": quantity,
        "estimated_price": estimated_price,
    }


def display_price(
    value,
) -> str:
    if value is None:
        return "—"

    return f"{value:.2f}"


def render_leg_buttons(
    expiry: date,
    strike: float,
    right: str,
    bid,
    ask,
    prefix: str,
):
    """
    Buy uses Ask; Sell uses Bid.

    This is correct for draft-trade modelling. Let the user edit the final
    entry price in the selected-leg basket before saving it as a Fill.
    """
    buy_col, sell_col = st.columns(2)

    with buy_col:
        if st.button(
            "B",
            key=f"{prefix}_buy_{expiry}_{strike}_{right}",
            width="stretch",
            disabled=ask is None,
        ):
            add_leg(
                expiry=expiry,
                strike=strike,
                right=right,
                quantity=1,
                estimated_price=ask,
            )
            st.rerun()

    with sell_col:
        if st.button(
            "S",
            key=f"{prefix}_sell_{expiry}_{strike}_{right}",
            width="stretch",
            disabled=bid is None,
        ):
            add_leg(
                expiry=expiry,
                strike=strike,
                right=right,
                quantity=-1,
                estimated_price=bid,
            )
            st.rerun()


def render_option_chain(
    chain,
    spot: float,
):
    headers = st.columns(
        [0.8, 0.9, 0.9, 1.3, 0.9, 0.8, 1.3, 0.9, 0.9, 0.8]
    )

    labels = [
        "Call Δ",
        "Call Bid",
        "Call Ask",
        "Call",
        "Strike",
        "IV",
        "Put",
        "Put Bid",
        "Put Ask",
        "Put Δ",
    ]

    for column, label in zip(headers, labels):
        with column:
            st.markdown(f"**{label}**")

    for _, row in chain.iterrows():
        strike = float(row["strike"])
        is_atm = abs(strike - spot) < 5.0

        row_cols = st.columns(
            [0.8, 0.9, 0.9, 1.3, 0.9, 0.8, 1.3, 0.9, 0.9, 0.8]
        )

        with row_cols[0]:
            st.caption(
                "—"
                if row["call_delta"] is None
                else f"{row['call_delta']:.2f}"
            )

        with row_cols[1]:
            st.write(display_price(row["call_bid"]))

        with row_cols[2]:
            st.write(display_price(row["call_ask"]))

        with row_cols[3]:
            render_leg_buttons(
                expiry=row["expiry"],
                strike=strike,
                right="C",
                bid=row["call_bid"],
                ask=row["call_ask"],
                prefix="call",
            )

        with row_cols[4]:
            label = f"**{strike:.0f}**" if is_atm else f"{strike:.0f}"
            st.markdown(label)

        with row_cols[5]:
            iv = row["call_iv"] or row["put_iv"]

            st.caption(
                "—"
                if iv is None
                else f"{iv * 100:.1f}%"
            )

        with row_cols[6]:
            render_leg_buttons(
                expiry=row["expiry"],
                strike=strike,
                right="P",
                bid=row["put_bid"],
                ask=row["put_ask"],
                prefix="put",
            )

        with row_cols[7]:
            st.write(display_price(row["put_bid"]))

        with row_cols[8]:
            st.write(display_price(row["put_ask"]))

        with row_cols[9]:
            st.caption(
                "—"
                if row["put_delta"] is None
                else f"{row['put_delta']:.2f}"
            )


def main():
    initialise_state()

    st.title("SPX Option Chain")

    controls = st.columns(4)

    with controls[0]:
        spot = st.number_input(
            "SPX spot",
            value=6560.25,
            step=0.25,
        )

    with controls[1]:
        selected_expiry = st.date_input(
            "Expiry",
            value=date.today(),
        )

    with controls[2]:
        width = st.number_input(
            "Strike window ± points",
            value=300,
            min_value=50,
            step=25,
        )

    with controls[3]:
        force_refresh = st.button(
            "Refresh prices",
            type="primary",
            width="stretch",
        )

    service = get_chain_service()
    service.strike_padding = width

    try:
        chain = service.get_chain(
            expiry=selected_expiry,
            spot=spot,
            force_refresh=force_refresh,
        )
    except Exception as exc:
        st.error(
            "Could not retrieve the SPX option chain from IBKR."
        )
        st.exception(exc)
        st.stop()

    if chain.empty:
        st.warning(
            "IBKR returned no chain rows. Confirm that TWS or IB Gateway is "
            "connected, API access is enabled, the expiry is valid, and your "
            "account has SPX option market-data permissions."
        )
        st.stop()

    st.caption(
        f"{len(chain)} strikes loaded | "
        "Buy defaults to Ask | Sell defaults to Bid"
    )

    render_option_chain(
        chain=chain,
        spot=spot,
    )

    with st.expander("Debug: raw normalised option chain"):
        st.write(chain.columns.tolist())
        st.dataframe(chain, width="stretch")

    st.divider()

    selected = list(
        st.session_state.selected_chain_legs.values()
    )

    st.subheader(f"{len(selected)} legs selected")

    if selected:
        st.dataframe(
            selected,
            width="stretch",
            hide_index=True,
        )

        if st.button("Clear selected legs"):
            st.session_state.selected_chain_legs = {}
            st.rerun()

    else:
        st.info(
            "Select Buy or Sell for a call or put to build a strategy."
        )


if __name__ == "__main__":
    main()