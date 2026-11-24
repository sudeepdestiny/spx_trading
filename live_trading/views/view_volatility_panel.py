"""
SPX volatility panel: VIX and SPX at-the-money implied volatility sampled
over time, so a fast IV/VIX drop is visible as a trend instead of a single
reading.

Read-only: it pulls market data and draws charts. It has no order code and
never touches the trading or order-placement modules.

History: a sample is taken each time you press "Sample now" (or on each
auto-refresh tick). Samples accumulate in the page session only, and are
lost on restart unless CSV saving is enabled below - nothing is read back
from disk, so the panel always shows what it captured itself, with the
capture times.

Run:
    streamlit run live_trading/views/view_volatility_panel.py

Pick "IBKR live" in the sidebar with TWS / IB Gateway running, or "Demo"
to preview the layout with synthetic data.
"""

from __future__ import annotations

import csv
import os
import sys
import time
from datetime import datetime
from typing import List
from zoneinfo import ZoneInfo

import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st

# Add project root to sys.path
sys.path.insert(
    0,
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")),
)

from live_trading.portfolio.volatility_data import (
    VolSample,
    make_demo_sample,
    sample_live_volatility,
    samples_to_frame,
)


ET = ZoneInfo("America/New_York")

_rerun = getattr(st, "rerun", None) or st.experimental_rerun

st.set_page_config(
    page_title="SPX Volatility Panel",
    page_icon="📉",
    layout="wide",
)


def _md_suffix(md_name):
    return {
        "live": "",
        "delayed": " (delayed)",
        "delayed-frozen": " (delayed)",
        "frozen": " (frozen)",
    }.get(md_name, "")


def vix_tile_label(sample: VolSample) -> str:
    if sample.vix is None:
        return "VIX"
    if sample.vix_basis == "previous-close":
        return "VIX (previous close)"
    if sample.vix_md_type:
        return "VIX" + _md_suffix(sample.vix_md_type)
    return f"VIX (requested {sample.market_data_type or 'live'})"


def append_csv(path: str, sample: VolSample) -> None:
    """Append one sample row. Creates the file with a header if missing."""
    row = {
        "sampled_at_et": sample.sampled_at.strftime("%Y-%m-%d %H:%M:%S"),
        "source": sample.source,
        "expiry": sample.expiry.isoformat() if sample.expiry else "",
        "spot": sample.spot,
        "atm_strike": sample.atm_strike,
        "call_iv": sample.call_iv,
        "put_iv": sample.put_iv,
        "iv_basis": sample.iv_basis,
        "vix": sample.vix,
        "vix_basis": sample.vix_basis,
        "vix_feed_time": (
            sample.vix_time.strftime("%Y-%m-%d %H:%M:%S %Z")
            if sample.vix_time else ""
        ),
        "vix_md_type": sample.vix_md_type,
        "market_data_type": sample.market_data_type,
    }
    new_file = not os.path.exists(path)
    with open(path, "a", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(row))
        if new_file:
            writer.writeheader()
        writer.writerow(row)


def render_chart(df):
    fig = make_subplots(specs=[[{"secondary_y": True}]])

    fig.add_trace(
        go.Scatter(
            x=df["sampled_at"], y=df["vix"],
            mode="lines+markers", name="VIX",
            line=dict(color="rgba(255, 140, 60, 0.95)"),
            customdata=df[["vix_basis", "vix_feed_time"]],
            hovertemplate=(
                "%{x|%H:%M:%S} ET<br>VIX %{y:.2f}"
                "<br>basis %{customdata[0]}"
                "<br>feed time %{customdata[1]}<extra></extra>"
            ),
        ),
        secondary_y=False,
    )
    fig.add_trace(
        go.Scatter(
            x=df["sampled_at"], y=df["atm_iv_pct"],
            mode="lines+markers", name="ATM IV %",
            line=dict(color="rgba(120, 160, 255, 0.95)"),
            customdata=df[["iv_basis", "strike"]],
            hovertemplate=(
                "%{x|%H:%M:%S} ET<br>ATM IV %{y:.2f}%"
                "<br>strike %{customdata[1]:,.0f}"
                "<br>basis %{customdata[0]}<extra></extra>"
            ),
        ),
        secondary_y=True,
    )
    fig.update_layout(
        title="VIX and SPX ATM implied volatility, per sample",
        height=440,
        margin=dict(l=20, r=20, t=60, b=20),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
        hovermode="x unified",
    )
    fig.update_xaxes(title_text="Sample time (ET)")
    fig.update_yaxes(title_text="VIX", secondary_y=False)
    fig.update_yaxes(title_text="IV %", secondary_y=True)
    return fig


def render_change_chart(df):
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=df["sampled_at"], y=df["vix_change_pct"],
        mode="lines+markers", name="VIX",
        line=dict(color="rgba(255, 140, 60, 0.95)"),
    ))
    fig.add_trace(go.Scatter(
        x=df["sampled_at"], y=df["atm_iv_change_pct"],
        mode="lines+markers", name="ATM IV",
        line=dict(color="rgba(120, 160, 255, 0.95)"),
    ))
    fig.add_hline(y=0, line_color="gray", line_dash="dot")
    fig.update_layout(
        title="Change since the first sample this session (%)",
        height=300,
        margin=dict(l=20, r=20, t=60, b=20),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
        hovermode="x unified",
        yaxis_title="% change",
    )
    return fig


def main():
    st.title("SPX Volatility Panel")

    with st.sidebar:
        st.header("Data source")
        source = st.radio(
            "Source",
            ["Demo (synthetic preview)", "IBKR live"],
            index=1,
            help="Demo uses invented numbers so you can review the layout "
                 "without a broker connection.",
        )

        if source == "IBKR live":
            host = st.text_input("TWS / Gateway host", value="127.0.0.1")
            port = st.number_input("Port", value=4001, step=1)
            client_id = st.number_input(
                "Client ID", value=11, step=1,
                help="The gamma dashboard also defaults to 11. If the "
                     "trader or the gamma dashboard is connected at the "
                     "same time, pick a different ID here or a request "
                     "may time out.",
            )
            md_type_label = st.selectbox(
                "Market data type",
                ["Live", "Frozen", "Delayed", "Delayed-frozen"],
            )
            market_data_type = ["Live", "Frozen", "Delayed", "Delayed-frozen"].index(md_type_label) + 1
            expiry = st.date_input("Expiry", value=datetime.now(ET).date())
        else:
            demo_spot = st.number_input("Demo spot", value=6650.0, step=25.0)
            expiry = datetime.now(ET).date()

        st.header("Model")
        rate_pct = st.number_input("Risk-free rate %", value=4.5, step=0.25)

        st.header("Sampling")
        sample_now = st.button("Sample now", type="primary", width="stretch")
        auto = st.checkbox("Auto-sample", value=False)
        interval = st.number_input(
            "Auto-sample interval (seconds)", value=60, min_value=15, step=15,
        )

        st.header("History")
        save_csv = st.checkbox(
            "Also save samples to CSV on this machine", value=False,
            help="Off by default: samples live in this page session only. "
                 "When on, each sample is appended to the file below so "
                 "history survives a restart. The file is never read "
                 "back, so the panel only ever shows what it captured.",
        )
        csv_path = st.text_input(
            "CSV file", value="volatility_samples.csv", disabled=not save_csv,
        )
        clear = st.button("Clear samples", width="stretch")

    if clear:
        st.session_state.pop("vol_samples", None)

    samples: List[VolSample] = st.session_state.setdefault("vol_samples", [])

    if sample_now or auto:
        try:
            if source == "IBKR live":
                sample = sample_live_volatility(
                    expiry=expiry,
                    rate=rate_pct / 100.0,
                    host=host,
                    port=int(port),
                    client_id=int(client_id),
                    market_data_type=market_data_type,
                )
            else:
                sample = make_demo_sample(
                    previous=samples[-1] if samples else None,
                    spot0=float(demo_spot),
                    expiry=expiry,
                )
            samples.append(sample)
            if save_csv:
                try:
                    append_csv(csv_path, sample)
                except OSError as exc:
                    st.error(f"Could not write the CSV file: {exc}")
        except Exception as exc:
            st.error("Could not take a sample.")
            st.exception(exc)
            st.stop()

    if not samples:
        st.info(
            "No samples yet. Press **Sample now** to take a VIX and ATM IV "
            "reading, or switch on auto-sampling. The trend appears after "
            "two or more samples."
        )
        if auto:
            time.sleep(int(interval))
            _rerun()
        return

    latest = samples[-1]

    if latest.source == "demo":
        st.warning(
            "DEMO DATA - synthetic preview. None of these numbers come from "
            "IBKR or any market. Use this only to review the layout.",
            icon="⚠️",
        )

    for message in latest.warnings:
        if latest.source != "demo":
            st.warning(message)

    df = samples_to_frame(samples)

    cols = st.columns(4)
    vix_text = "unavailable" if latest.vix is None else f"{latest.vix:,.2f}"
    vix_delta = df["vix_change_pct"].iloc[-1]
    cols[0].metric(
        vix_tile_label(latest),
        vix_text,
        None if vix_delta is None else f"{vix_delta:+.1f}% vs first sample",
        help="VIX index level from the same IBKR connection as the option "
             "quotes. The label says whether it is the previous session's "
             "close, a quote whose data type IBKR reported (live, delayed "
             "or frozen), or a quote labeled with the requested type.",
    )
    atm_iv_pct = df["atm_iv_pct"].iloc[-1]
    iv_delta = df["atm_iv_change_pct"].iloc[-1]
    cols[1].metric(
        f"ATM IV ({latest.expiry} {latest.atm_strike:,.0f})"
        if latest.atm_strike is not None else "ATM IV",
        "unavailable" if atm_iv_pct is None else f"{atm_iv_pct:.1f}%",
        None if iv_delta is None else f"{iv_delta:+.1f}% vs first sample",
        help="Mean of the at-the-money call and put implied vol for the "
             "selected expiry, from IBKR model greeks or solved from the "
             "mid/last price (the table shows which).",
    )
    cols[2].metric(
        "SPX spot",
        "unavailable" if latest.spot is None else f"{latest.spot:,.2f}",
    )
    cols[3].metric("Samples this session", len(samples))

    st.caption(
        f"Last sample {latest.sampled_at:%Y-%m-%d %H:%M:%S} ET (request "
        "time, not necessarily the last quote time)."
        + (
            f" VIX feed quote time: {latest.vix_time:%Y-%m-%d %H:%M:%S %Z}."
            if latest.vix_time else
            " The feed supplied no VIX quote time; delayed or frozen data "
            "can lag the market."
        )
    )

    if len(samples) >= 2:
        st.plotly_chart(render_chart(df), width="stretch")
        st.plotly_chart(render_change_chart(df), width="stretch")
    else:
        st.info("One sample so far - the trend charts appear after the next one.")

    with st.expander("Samples"):
        st.dataframe(df, width="stretch", hide_index=True)

    with st.expander("What this panel is - and is not"):
        st.markdown(
            "- **VIX** is the CBOE volatility index from the same IBKR "
            "connection. **ATM IV** is the implied vol of the SPX option "
            "nearest spot for the chosen expiry. The two move together but "
            "are not the same measure, so a drop in one without the other "
            "is normal.\n"
            "- **ATM follows spot**: each sample re-centres on the strike "
            "nearest spot. When spot crosses to a new strike the series "
            "switches contracts, which can shift the IV level by itself - "
            "the samples table lists the strike behind each point.\n"
            "- **Change since first sample** compares against the first "
            "reading this session captured, not the market open or "
            "yesterday's close.\n"
            "- Samples are taken only when this page asks for them. "
            "Between samples the panel shows nothing, so a crash between "
            "samples appears as a line jump, not when it happened.\n"
            "- Delayed or frozen market data lags the market; the labels "
            "above each reading say which basis IBKR delivered.\n"
            "- This panel is read-only. It places no orders and nothing "
            "here is investment advice."
        )

    if auto:
        time.sleep(int(interval))
        _rerun()


if __name__ == "__main__":
    main()
