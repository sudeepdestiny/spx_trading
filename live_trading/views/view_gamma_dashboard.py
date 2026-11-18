"""
SPX gamma dashboard: call wall, put wall, gamma flip and per-strike gamma
exposure, plus the time-of-day windows from the framework the user shared.

Read-only: it pulls market data and draws charts. It has no order code and
never touches the trading or order-placement modules.

Run:
    streamlit run live_trading/views/view_gamma_dashboard.py

Pick "IBKR live" in the sidebar with TWS / IB Gateway running, or "Demo"
to preview the layout with synthetic data.
"""

from __future__ import annotations

import os
import sys
from datetime import date, datetime, time as dtime
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

# Add project root to sys.path
sys.path.insert(
    0,
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")),
)

from live_trading.portfolio.gamma_data import (
    GammaInputs,
    fetch_live_gamma_inputs,
    make_demo_chain,
)
from live_trading.portfolio.gamma_exposure import (
    gamma_flip,
    gamma_walls,
    implied_vol,
    per_strike_gex,
    session_window,
    MIN_TIME_TO_EXPIRY,
)


ET = ZoneInfo("America/New_York")

st.set_page_config(
    page_title="SPX Gamma Dashboard",
    page_icon="📈",
    layout="wide",
)


def time_to_expiry_years(expiry: date, now_et: datetime) -> float:
    """Years until 16:00 ET on the expiry date (SPX options stop at the close)."""
    close_et = datetime.combine(expiry, dtime(16, 0), tzinfo=ET)
    seconds = (close_et - now_et).total_seconds()
    years = seconds / (365.0 * 24.0 * 3600.0)
    return max(years, MIN_TIME_TO_EXPIRY)


def fill_missing_iv(
    chain: pd.DataFrame,
    spot: float,
    rate: float,
    t: float,
) -> pd.DataFrame:
    """
    Where IBKR gave no model IV, try to solve IV from the mid price.
    Rows that cannot be solved stay None - never guessed.
    """
    df = chain.copy()

    for side in ("call", "put"):
        iv_col = f"{side}_iv"
        solved = []
        for _, row in df.iterrows():
            iv = row.get(iv_col)
            if iv is not None and pd.notna(iv):
                solved.append(iv)
                continue

            bid = row.get(f"{side}_bid")
            ask = row.get(f"{side}_ask")
            last = row.get(f"{side}_last")
            mid = None
            if bid is not None and pd.notna(bid) and ask is not None and pd.notna(ask):
                mid = (bid + ask) / 2.0
            elif last is not None and pd.notna(last):
                mid = last

            solved.append(
                implied_vol(
                    price=mid,
                    spot=spot,
                    strike=float(row["strike"]),
                    time_to_expiry=t,
                    rate=rate,
                    option_type="C" if side == "call" else "P",
                ) if mid else None
            )
        df[iv_col] = solved

    return df


def format_level(value) -> str:
    return "—" if value is None else f"{value:,.0f}"


def render_gex_profile(gex_chain, spot, walls, flip):
    fig = go.Figure()

    plot_df = gex_chain.dropna(subset=["call_gex", "put_gex"], how="all")

    fig.add_trace(go.Bar(
        x=plot_df["strike"],
        y=plot_df["call_gex"],
        name="Call GEX",
        marker_color="rgba(0, 160, 90, 0.75)",
    ))
    fig.add_trace(go.Bar(
        x=plot_df["strike"],
        y=plot_df["put_gex"],
        name="Put GEX",
        marker_color="rgba(210, 60, 60, 0.75)",
    ))

    def vline(x, label, color, dash="dash", y=1.0):
        if x is None:
            return
        fig.add_vline(x=x, line_color=color, line_dash=dash, line_width=2)
        fig.add_annotation(
            x=x, y=y, yref="paper", text=label,
            showarrow=False, font_color=color, yanchor="bottom",
        )

    vline(spot, f"Spot {spot:,.0f}", "#1a1a1a", "solid", y=1.0)
    vline(walls.call_wall, f"Call wall {format_level(walls.call_wall)}", "orange", y=1.0)
    vline(walls.put_wall, f"Put wall {format_level(walls.put_wall)}", "deepskyblue", y=1.0)
    vline(flip, f"Gamma flip {format_level(flip)}", "magenta", y=0.93)

    fig.update_layout(
        title="Per-strike gamma exposure (signed, $ per 1% spot move)",
        barmode="relative",
        height=420,
        margin=dict(l=20, r=20, t=60, b=20),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
        xaxis_title="Strike",
        yaxis_title="GEX ($)",
    )
    return fig


def render_flip_profile(flip_result, spot):
    fig = go.Figure()

    fig.add_trace(go.Scatter(
        x=flip_result.grid_spots,
        y=flip_result.grid_gex,
        mode="lines",
        name="Total GEX",
        line=dict(color="rgba(120, 160, 255, 0.9)"),
    ))
    fig.add_hline(y=0, line_color="gray", line_dash="dot")

    fig.add_vline(x=spot, line_color="#1a1a1a", line_width=2)
    fig.add_annotation(
        x=spot, y=1.0, yref="paper", text=f"Spot {spot:,.0f}",
        showarrow=False, font_color="#1a1a1a", yanchor="bottom",
    )

    for crossing in flip_result.crossings:
        fig.add_vline(x=crossing, line_color="magenta", line_dash="dash", line_width=2)

    # Only annotate the selected flip; other crossings are listed in the
    # caption so labels cannot overlap.
    if flip_result.flip is not None:
        fig.add_annotation(
            x=flip_result.flip, y=0.92, yref="paper",
            text=f"Zero gamma {flip_result.flip:,.0f}",
            showarrow=False, font_color="magenta", yanchor="bottom",
        )

    fig.update_layout(
        title="Total gamma exposure vs hypothetical spot (IV held fixed)",
        height=300,
        margin=dict(l=20, r=20, t=60, b=20),
        xaxis_title="Hypothetical SPX spot",
        yaxis_title="Total GEX ($)",
        showlegend=False,
    )
    return fig


def render_session_banner(now_et: datetime):
    window = session_window(now_et)
    clock = now_et.strftime("%H:%M ET")

    if window is None:
        st.info(f"{clock} - outside regular hours (9:30-16:00 ET).")
        return

    st.info(
        f"{clock} - **{window.name}** ({window.start}-{window.end} ET). "
        f"{window.description}"
    )


def main():
    st.title("SPX Gamma Dashboard")

    with st.sidebar:
        st.header("Data source")
        source = st.radio(
            "Source",
            ["Demo (synthetic preview)", "IBKR live"],
            index=1,
            help="Demo uses invented numbers so you can review the layout "
                 "without a broker connection.",
        )

        strike_count = st.number_input(
            "Strike levels each side of spot", value=20, min_value=5, step=5,
        )

        if source == "IBKR live":
            host = st.text_input("TWS / Gateway host", value="127.0.0.1")
            port = st.number_input("Port", value=4001, step=1)
            client_id = st.number_input("Client ID", value=11, step=1)
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
        wall_method = st.selectbox(
            "Wall definition",
            ["oi", "gex"],
            help="oi: largest call/put open interest. gex: largest absolute "
                 "call/put gamma exposure. Vendors use both; there is no "
                 "single canonical definition.",
        )
        dealer_calls_long = st.checkbox(
            "Dealers long calls / short puts",
            value=True,
            help="The sign assumption behind the GEX numbers. It cannot be "
                 "verified from public data; untick for the opposite "
                 "convention and the profile flips sign.",
        )

        load = st.button("Load / refresh", type="primary", width="stretch")

    cache_key = "gamma_inputs"
    if load or cache_key not in st.session_state:
        try:
            if source == "IBKR live":
                inputs = fetch_live_gamma_inputs(
                    expiry=expiry,
                    strike_count=int(strike_count),
                    rate=rate_pct / 100.0,
                    host=host,
                    port=int(port),
                    client_id=int(client_id),
                    market_data_type=market_data_type,
                )
            else:
                inputs = make_demo_chain(
                    spot=float(demo_spot),
                    strike_count=int(strike_count),
                    expiry=expiry,
                )
            st.session_state[cache_key] = inputs
        except Exception as exc:
            st.error("Could not load gamma inputs.")
            st.exception(exc)
            st.stop()

    inputs: GammaInputs = st.session_state[cache_key]

    if inputs.source == "demo":
        st.warning(
            "DEMO DATA - synthetic preview. None of these numbers come from "
            "IBKR or any market. Use this only to review the layout.",
            icon="⚠️",
        )
    else:
        st.caption(
            f"IBKR {inputs.market_data_type} data, pulled "
            f"{inputs.fetched_at:%Y-%m-%d %H:%M:%S} ET (request time, not "
            "necessarily the last quote time). Open interest is the "
            "latest value the exchange published (updated once per session), "
            "not live intraday positioning."
        )

    for message in inputs.warnings:
        st.warning(message)

    now_et = datetime.now(ET)
    t = time_to_expiry_years(inputs.expiry, now_et)
    rate = rate_pct / 100.0

    chain = fill_missing_iv(inputs.chain, inputs.spot, rate, t)

    if inputs.source == "ibkr":
        # Final eligibility is assessed here, after the IV fallback ran at
        # the actual selected rate: a strike counts only with OI and a
        # solved IV on BOTH sides. inputs.complete (certified at fetch time
        # with the same rate) must also hold for qualification coverage.
        fully_covered = bool(
            inputs.complete
            and len(chain) > 0
            and chain[["call_oi", "put_oi"]].notna().all(axis=1).all()
            and chain[["call_iv", "put_iv"]].notna().all(axis=1).all()
        )
        if not fully_covered:
            st.error(
                "Data coverage is incomplete: IBKR did not deliver open "
                "interest plus a usable IV (model greeks, or a price that "
                "yields one at the selected rate) for every strike in the "
                "window, so the walls, flip and GEX are withheld instead of "
                "computed from partial data. Press Load / refresh to retry, "
                "or narrow the strike window."
            )
            st.stop()

    usable_iv = chain[["call_iv", "put_iv"]].notna().any(axis=1)
    usable_oi = chain[["call_oi", "put_oi"]].notna().any(axis=1)
    priced = chain[usable_iv & usable_oi]

    render_session_banner(now_et)

    if priced.empty:
        st.error(
            "No strikes have both IV and open interest, so the walls, flip "
            "and GEX profile cannot be computed. Check the data warnings "
            "above - missing inputs are shown as unavailable, never as zero."
        )
        st.stop()

    gex_chain = per_strike_gex(
        priced,
        spot=inputs.spot,
        rate=rate,
        time_to_expiry=t,
        dealer_calls_long=dealer_calls_long,
    )
    walls = gamma_walls(gex_chain, method=wall_method)
    flip = gamma_flip(
        priced,
        spot=inputs.spot,
        rate=rate,
        time_to_expiry=t,
        dealer_calls_long=dealer_calls_long,
    )

    vix_value = getattr(inputs, "vix", None)
    vix_basis = getattr(inputs, "vix_basis", None)
    vix_time = getattr(inputs, "vix_time", None)
    vix_md_actual = getattr(inputs, "vix_md_type", None)

    def _md_suffix(md_name):
        return {
            "live": "",
            "delayed": " (delayed)",
            "delayed-frozen": " (delayed)",
            "frozen": " (frozen)",
        }.get(md_name, "")

    if vix_value is None:
        vix_label, vix_text = "VIX", "unavailable"
    elif vix_basis == "previous-close":
        vix_label, vix_text = "VIX (previous close)", f"{vix_value:,.2f}"
    elif vix_md_actual:
        vix_label, vix_text = "VIX" + _md_suffix(vix_md_actual), f"{vix_value:,.2f}"
    else:
        vix_label = f"VIX (requested {inputs.market_data_type or 'live'})"
        vix_text = f"{vix_value:,.2f}"

    cols = st.columns(6)
    cols[0].metric("SPX spot", f"{inputs.spot:,.2f}")
    cols[1].metric(
        vix_label,
        vix_text,
        help="VIX index level from the same IBKR connection as the SPX "
             "chain. The label says whether it is the previous session's "
             "close, a quote whose data type IBKR reported (live, delayed "
             "or frozen), or a quote labeled with only the requested type "
             "when IBKR did not report the actual one - in that case live "
             "freshness is unverified. The caption below gives the feed's "
             "quote time when supplied. 'unavailable' means IBKR did not "
             "deliver it (for example a missing index data permission); "
             "the gamma levels are unaffected.",
    )
    cols[2].metric("Call wall", format_level(walls.call_wall))
    cols[3].metric("Put wall", format_level(walls.put_wall))
    cols[4].metric(
        "Gamma flip",
        format_level(flip.flip),
        help="Spot level where total signed GEX crosses zero, found by "
             "repricing the chain across hypothetical spots. None when "
             "there is no crossing in the displayed window.",
    )
    spot_vs_flip = (
        "—" if flip.flip is None
        else "above flip" if inputs.spot > flip.flip else "below flip"
    )
    cols[5].metric("Spot vs flip", spot_vs_flip)

    if vix_value is not None:
        if inputs.source == "demo":
            vix_note = "VIX shown is synthetic demo data."
        else:
            if vix_basis == "previous-close":
                vix_note = "VIX is the previous session's close, not a live quote."
            elif vix_md_actual:
                vix_note = (
                    f"VIX is a {vix_md_actual} quote, as reported by IBKR "
                    "for this ticker."
                )
            else:
                vix_note = (
                    "VIX is a quote fetched with the requested market-data "
                    f"type '{inputs.market_data_type or 'live'}'; IBKR did "
                    "not report this ticker's actual type, so live "
                    "freshness is unverified."
                )
            if vix_time is not None:
                vix_note += f" Feed quote time: {vix_time:%Y-%m-%d %H:%M:%S}"
                if vix_time.tzinfo is None:
                    vix_note += " (timezone not supplied by the feed)"
                else:
                    vix_note += f" {vix_time.strftime('%Z') or ''}".rstrip()
            else:
                vix_note += (
                    " The feed supplied no quote time; the pulled-at time "
                    "above is the request time."
                )
        st.caption(vix_note)

    if len(flip.crossings) > 1:
        st.caption(
            "Multiple zero-gamma crossings in range: "
            + ", ".join(f"{c:,.0f}" for c in flip.crossings)
            + ". Showing the one nearest spot."
        )

    st.plotly_chart(render_gex_profile(gex_chain, inputs.spot, walls, flip.flip), width="stretch")
    st.plotly_chart(render_flip_profile(flip, inputs.spot), width="stretch")

    with st.expander("What these numbers are - and are not"):
        st.markdown(
            "- **Call wall / put wall**: strike with the largest call / put "
            f"**{wall_method}** in the loaded window. Other definitions "
            "(net GEX, other expiry scopes) give different levels.\n"
            "- **Gamma flip**: the spot where total signed dealer gamma "
            "crosses zero when every option is repriced at that spot, IV "
            "held fixed. It is a model estimate, not a fetched level, and "
            "may not exist or may have several crossings.\n"
            "- **Dealer sign assumption**: open interest has no side. The "
            "signs here assume dealers are long calls and short puts; that "
            "cannot be verified from public data.\n"
            "- **Open interest** is published once per session by the "
            "exchange. It is not live intraday positioning, which matters "
            "most on 0DTE.\n"
            "- **Time windows** are the labels from the post you shared, "
            "shown for orientation. They are not validated signals and "
            "nothing here is investment advice.\n"
            "- These levels will not match GEX Edge or any other vendor: "
            "each vendor uses its own sign convention, expiry scope and "
            "data vintage."
        )

    with st.expander("Raw chain with GEX"):
        st.dataframe(gex_chain, width="stretch", hide_index=True)


if __name__ == "__main__":
    main()
