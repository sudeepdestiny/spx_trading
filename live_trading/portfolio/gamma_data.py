"""
Data providers for the SPX gamma dashboard.

Two sources:

* ``fetch_live_gamma_inputs`` - pulls the SPX chain, model greeks, open
  interest and volume from IBKR with ib_insync, following the same pattern
  as the repo's root get_option_chain.py. Read-only market data: it never
  places, modifies or cancels an order.

  Open interest needs streaming reqMktData with generic tick 101 (IBKR does
  not return it in snapshots). When IBKR does not deliver a value, the
  field stays None - it is never filled with zero, so the dashboard can
  show it as unavailable instead of drawing a wrong level.

* ``make_demo_chain`` - synthetic chain for previewing the dashboard
  without a broker connection. Every row is made up; the dashboard labels
  this loudly as demo data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import List, Optional
import re

import numpy as np
import pandas as pd


@dataclass
class GammaInputs:
    chain: pd.DataFrame
    spot: float
    expiry: date
    source: str                      # "ibkr" or "demo"
    fetched_at: datetime
    market_data_type: Optional[str] = None   # e.g. "live", "delayed-frozen"
    warnings: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Live IBKR provider
# ---------------------------------------------------------------------------

def fetch_live_gamma_inputs(
    expiry: Optional[date] = None,
    strike_padding: int = 300,
    host: str = "127.0.0.1",
    port: int = 7497,
    client_id: int = 11,
    market_data_type: int = 1,
    settle_seconds: float = 10.0,
) -> GammaInputs:
    """
    Pull the SPX option chain with greeks and open interest from IBKR.

    market_data_type: 1 live, 2 frozen, 3 delayed, 4 delayed-frozen.
    Delayed modes may omit greeks and/or open interest depending on the
    account's subscriptions; anything not delivered stays None.

    Requires TWS or IB Gateway running with the API enabled, and SPX/SPXW
    options market-data permissions for live values.
    """
    import nest_asyncio

    nest_asyncio.apply()
    from ib_insync import IB, Index, Option

    md_type_names = {1: "live", 2: "frozen", 3: "delayed", 4: "delayed-frozen"}
    warnings: List[str] = []

    ib = IB()
    try:
        ib.connect(host, port, clientId=client_id, timeout=10)
        ib.reqMarketDataType(market_data_type)

        # Surface the broker's own messages (permission, farm and request
        # errors) in the dashboard warnings instead of dropping them.
        # Account-like identifiers are scrubbed and messages capped; the
        # view renders warnings as plain markdown-escaped text.
        broker_messages: List[str] = []
        acctish = re.compile(r"\b[A-Za-z]{0,3}\d{6,}[A-Za-z0-9]*\b")

        def _on_error(*args):
            # ib_insync emits (reqId, errorCode, errorString[, contract]).
            if len(args) >= 3:
                msg = acctish.sub("[redacted]", str(args[2]))
                msg = " ".join(msg.split())[:300]
                text = f"IBKR {args[1]}: {msg}"
                if text not in broker_messages:
                    broker_messages.append(text)

        ib.errorEvent += _on_error

        spx = Index("SPX", "CBOE")
        ib.qualifyContracts(spx)

        def _positive(value):
            return value is not None and np.isfinite(value) and value > 0

        # Stream and poll instead of a one-shot snapshot: outside market
        # hours the data farms can take over ten seconds to wake, and a
        # snapshot request ends empty before the first tick arrives.
        spot_ticker = ib.reqMktData(spx, "", False, False)
        spot = float("nan")
        for _ in range(25):
            ib.sleep(1)
            # Outside market hours `last` is NaN, which is truthy - check
            # it explicitly so we fall back to the previous close.
            spot = (
                spot_ticker.last
                if _positive(spot_ticker.last)
                else spot_ticker.close
            )
            if _positive(spot):
                break
        ib.cancelMktData(spx)
        if not _positive(spot):
            raise RuntimeError(
                "IBKR did not return an SPX spot price after waiting. "
                "Check the index market-data subscription and the market "
                "data type."
            )

        chains = ib.reqSecDefOptParams(spx.symbol, "", spx.secType, spx.conId)
        chain_def = next((c for c in chains if c.exchange == "CBOE"), None)
        if chain_def is None:
            raise RuntimeError("IBKR returned no CBOE SPX option chain definition.")

        expirations = sorted(chain_def.expirations)
        if expiry is None:
            expiry_str = expirations[0]  # nearest expiry, 0DTE on trading days
        else:
            expiry_str = expiry.strftime("%Y%m%d")
            if expiry_str not in expirations:
                raise RuntimeError(
                    f"Expiry {expiry_str} is not listed on the SPX chain. "
                    f"Nearest available: {expirations[:5]}"
                )
        expiry_date = datetime.strptime(expiry_str, "%Y%m%d").date()

        all_strikes = sorted(chain_def.strikes)
        strikes = [
            s for s in all_strikes
            if spot - strike_padding <= s <= spot + strike_padding
        ]
        if not strikes:
            raise RuntimeError(
                f"No SPX strikes within +/-{strike_padding} points of spot "
                f"{spot:.2f} on the {expiry_str} chain. Widen the strike "
                "window or pick another expiry."
            )

        calls = [Option("SPX", expiry_str, s, "C", "CBOE") for s in strikes]
        puts = [Option("SPX", expiry_str, s, "P", "CBOE") for s in strikes]
        contracts = calls + puts
        ib.qualifyContracts(*contracts)

        # Generic ticks: 100 = option volume, 101 = option open interest.
        # Open interest is only delivered on streaming requests.
        tickers = [
            ib.reqMktData(c, "100,101", False, False) for c in contracts
        ]
        ib.sleep(settle_seconds)
        for c in contracts:
            ib.cancelMktData(c)

        def _usable(value):
            return value is not None and np.isfinite(value) and value >= 0

        rows = []
        for i, strike in enumerate(strikes):
            call_t, put_t = tickers[i], tickers[i + len(strikes)]
            call_g = call_t.modelGreeks
            put_g = put_t.modelGreeks

            call_oi = call_t.callOpenInterest if _usable(call_t.callOpenInterest) else None
            put_oi = put_t.putOpenInterest if _usable(put_t.putOpenInterest) else None

            rows.append({
                "strike": strike,
                "call_bid": call_t.bid if _usable(call_t.bid) else None,
                "call_ask": call_t.ask if _usable(call_t.ask) else None,
                "call_last": call_t.last if _usable(call_t.last) else None,
                "call_iv": call_g.impliedVol if call_g and _usable(call_g.impliedVol) else None,
                "call_gamma_model": call_g.gamma if call_g and _usable(call_g.gamma) else None,
                "call_oi": call_oi,
                "call_volume": call_t.volume if _usable(call_t.volume) else None,
                "put_bid": put_t.bid if _usable(put_t.bid) else None,
                "put_ask": put_t.ask if _usable(put_t.ask) else None,
                "put_last": put_t.last if _usable(put_t.last) else None,
                "put_iv": put_g.impliedVol if put_g and _usable(put_g.impliedVol) else None,
                "put_gamma_model": put_g.gamma if put_g and _usable(put_g.gamma) else None,
                "put_oi": put_oi,
                "put_volume": put_t.volume if _usable(put_t.volume) else None,
            })

        df = pd.DataFrame(rows)

        if df["call_oi"].isna().all() and df["put_oi"].isna().all():
            warnings.append(
                "IBKR delivered no open interest. OI needs streaming market "
                "data (generic tick 101), a session after the exchange has "
                "published OI, and the right market-data permissions. Walls "
                "and GEX levels are unavailable without it."
            )
        elif df[["call_oi", "put_oi"]].isna().any().any():
            warnings.append(
                "Open interest is missing for some strikes; those strikes "
                "are excluded from the walls and GEX, not treated as zero."
            )

        if df["call_iv"].isna().all() and df["put_iv"].isna().all():
            warnings.append(
                "IBKR delivered no model IV. The dashboard will try to back "
                "IV out of mid prices where possible."
            )

        if broker_messages:
            warnings.append(
                "Broker messages during the request: "
                + " | ".join(broker_messages[:12])
            )

        return GammaInputs(
            chain=df,
            spot=float(spot),
            expiry=expiry_date,
            source="ibkr",
            fetched_at=datetime.now(),
            market_data_type=md_type_names.get(market_data_type, str(market_data_type)),
            warnings=warnings,
        )
    finally:
        ib.disconnect()


# ---------------------------------------------------------------------------
# Demo provider (synthetic preview data - NOT from IBKR)
# ---------------------------------------------------------------------------

def make_demo_chain(
    spot: float = 6650.0,
    strike_padding: int = 300,
    expiry: Optional[date] = None,
    seed: int = 7,
) -> GammaInputs:
    """
    Synthetic chain so the dashboard can be previewed with no broker.
    Numbers are invented to look plausible; they are not market data.
    """
    rng = np.random.default_rng(seed)

    if expiry is None:
        expiry = date.today()

    strikes = np.arange(
        round(spot - strike_padding),
        round(spot + strike_padding) + 5,
        5.0,
    )

    # Invented OI: a put wall below spot, a call wall above spot.
    put_peak = round(spot - 120)
    call_peak = round(spot + 100)
    dist = (strikes - spot) / 60.0

    call_oi = (
        4000
        + 26000 * np.exp(-0.5 * ((strikes - call_peak) / 35.0) ** 2)
        + 6000 * np.exp(-0.5 * dist ** 2)
        + rng.uniform(0, 1500, len(strikes))
    ).round()
    put_oi = (
        4000
        + 28000 * np.exp(-0.5 * ((strikes - put_peak) / 35.0) ** 2)
        + 6000 * np.exp(-0.5 * dist ** 2)
        + rng.uniform(0, 1500, len(strikes))
    ).round()

    # Invented IV smile, higher for downside strikes.
    iv = 0.16 + 0.05 * np.exp(-dist) + rng.uniform(-0.004, 0.004, len(strikes))
    iv = np.clip(iv, 0.05, 0.8)

    rows = []
    for strike, c_oi, p_oi, v in zip(strikes, call_oi, put_oi, iv):
        rows.append({
            "strike": float(strike),
            "call_bid": None,
            "call_ask": None,
            "call_last": None,
            "call_iv": float(v),
            "call_gamma_model": None,
            "call_oi": float(c_oi),
            "call_volume": float(rng.uniform(200, 8000)),
            "put_bid": None,
            "put_ask": None,
            "put_last": None,
            "put_iv": float(v),
            "put_gamma_model": None,
            "put_oi": float(p_oi),
            "put_volume": float(rng.uniform(200, 8000)),
        })

    return GammaInputs(
        chain=pd.DataFrame(rows),
        spot=float(spot),
        expiry=expiry,
        source="demo",
        fetched_at=datetime.now(),
        market_data_type=None,
        warnings=[
            "Demo mode: every value on this page is synthetic preview data, "
            "not from IBKR and not market data."
        ],
  )
