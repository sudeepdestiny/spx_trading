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
from datetime import date, datetime, time as dtime
from zoneinfo import ZoneInfo
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
    complete: bool = True   # False when any requested strike lacks OI or IV
    vix: Optional[float] = None        # VIX level from the same feed; None when not delivered
    vix_basis: Optional[str] = None    # "quote" (last index value) or "previous-close"
    vix_time: Optional[datetime] = None  # feed quote time when IBKR supplies one
    vix_md_type: Optional[str] = None  # actual market-data type IBKR reports for the ticker


# ---------------------------------------------------------------------------
# Live IBKR provider
# ---------------------------------------------------------------------------

def _ticker_inputs_delivered(ticker, right) -> bool:
    """True when a streaming ticker has delivered everything the dashboard
    needs for its side of one strike.

    ib_insync leaves option open interest at NaN until the tickSize 27/28
    event arrives (wrapper.py assigns the raw value), so NaN means "never
    delivered" and a genuine delivered 0 still counts as delivered.
    modelGreeks stays None until a tickOptionComputation event arrives.
    When model greeks are absent, a two-sided quote is enough: the view
    backs IV out of the mid price as a fallback.
    """
    oi = ticker.callOpenInterest if right == "C" else ticker.putOpenInterest
    if oi != oi:   # NaN: OI tick never arrived
        return False
    if ticker.modelGreeks is not None:
        return True
    return bool(ticker.bid > 0 and ticker.ask > 0)


def _years_to_expiry(expiry: date, now_et: Optional[datetime] = None) -> float:
    """Years until 16:00 ET on the expiry date (same convention as the view)."""
    now_et = now_et or datetime.now(ZoneInfo("America/New_York"))
    close_et = datetime.combine(expiry, dtime(16, 0), tzinfo=ZoneInfo("America/New_York"))
    years = (close_et - now_et).total_seconds() / (365.0 * 24.0 * 3600.0)
    return max(years, 1.0 / (365.0 * 24.0 * 60.0))  # one-minute floor


def _side_fallback_price(row, side: str):
    """The price the view's IV fallback would use: two-sided mid, else last."""
    bid, ask, last = (row.get(f"{side}_{k}") for k in ("bid", "ask", "last"))
    if (
        bid is not None and pd.notna(bid) and bid > 0
        and ask is not None and pd.notna(ask) and ask > 0
    ):
        return (bid + ask) / 2.0
    if last is not None and pd.notna(last) and last > 0:
        return last
    return None


def _iv_solvable(price, spot, strike, right, t_years, rate=0.045) -> bool:
    """True when the view's mid/last-price IV inversion actually solves.

    A quote existing is not enough: the bisection solver in gamma_exposure
    returns None when the price falls outside the no-arbitrage bracket, and
    those rows would silently lose GEX downstream. Lazy import keeps this
    module usable without the exposure module's dependencies.
    """
    if price is None or not np.isfinite(price) or price <= 0:
        return False
    from live_trading.portfolio.gamma_exposure import implied_vol
    try:
        iv = implied_vol(
            price=float(price), spot=float(spot), strike=float(strike),
            time_to_expiry=t_years, rate=rate, option_type=right,
        )
    except Exception:
        return False
    return iv is not None and np.isfinite(iv) and iv > 0


def _row_side_iv_ok(row, side, spot, t_years, rate=0.045) -> bool:
    """One side of one strike is covered: model IV, or a fallback price the
    IV solver can actually invert."""
    iv = row.get(f"{side}_iv")
    if iv is not None and pd.notna(iv):
        return True
    price = _side_fallback_price(row, side)
    if price is None:
        return False
    return _iv_solvable(
        price, spot, float(row["strike"]),
        "C" if side == "call" else "P", t_years, rate,
    )


def _assess_completeness(df, all_qualified: bool, spot: float, t_years: float,
                         rate: float = 0.045) -> bool:
    """Complete means: every requested contract qualified, and every strike
    has open interest AND a usable IV (model or solvable fallback) on BOTH
    sides. Nothing less certifies walls/flip/GEX."""
    if not all_qualified or df.empty:
        return False
    has_oi = df[["call_oi", "put_oi"]].notna().all(axis=1)
    call_ok = df.apply(lambda r: _row_side_iv_ok(r, "call", spot, t_years, rate), axis=1)
    put_ok = df.apply(lambda r: _row_side_iv_ok(r, "put", spot, t_years, rate), axis=1)
    return bool((has_oi & call_ok & put_ok).all())


def _select_strikes(all_strikes, spot: float, strike_count: int) -> list:
    """Return strike_count levels below/above the strike nearest spot."""
    nearest = min(
        range(len(all_strikes)),
        key=lambda i: abs(all_strikes[i] - spot),
    )
    lo = max(0, nearest - strike_count)
    hi = min(len(all_strikes), nearest + strike_count + 1)
    return list(all_strikes[lo:hi])


def fetch_live_gamma_inputs(
    expiry: Optional[date] = None,
    strike_count: int = 20,
    rate: float = 0.045,
    host: str = "127.0.0.1",
    port: int = 7497,
    client_id: int = 11,
    market_data_type: int = 1,
    max_wait_seconds: float = 30.0,
    strikes: Optional[List[float]] = None,
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

        # VIX level, supplementary to the gamma inputs: same connection as
        # the SPX chain. The basis ("quote" vs "previous-close"), the feed's
        # quote time and the actual market-data type IBKR reports for the
        # ticker are kept so the view can label the value honestly; a
        # previous close is never presented as current, and when the actual
        # type is not reported the view says "requested", never "live".
        # Any failure here only marks VIX unavailable - it never fails the
        # load.
        vix_value = None
        vix_basis = None
        vix_time = None
        vix_md_type = None
        try:
            vix_contract = Index("VIX", "CBOE")
            ib.qualifyContracts(vix_contract)
            vix_ticker = ib.reqMktData(vix_contract, "", False, False)
            for _ in range(8):
                ib.sleep(1)
                if _positive(vix_ticker.last):
                    vix_value = float(vix_ticker.last)
                    vix_basis = "quote"
                    break
                if _positive(vix_ticker.close):
                    vix_value = float(vix_ticker.close)
                    vix_basis = "previous-close"
                    break
            if vix_value is not None:
                feed_time = getattr(vix_ticker, "time", None)
                if isinstance(feed_time, datetime):
                    vix_time = feed_time
                try:
                    vix_md_type = md_type_names.get(
                        int(getattr(vix_ticker, "marketDataType", None))
                    )
                except (TypeError, ValueError):
                    vix_md_type = None
            ib.cancelMktData(vix_contract)
        except Exception:
            vix_value = None
            vix_basis = None
            vix_time = None
            vix_md_type = None
        if vix_value is None:
            warnings.append(
                "IBKR did not deliver a VIX level (the account may lack the "
                "CBOE index data permission, or the feed may be quiet). VIX "
                "is shown as unavailable; the gamma levels are unaffected."
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
        if strikes is None:
            strikes = _select_strikes(all_strikes, spot, strike_count)
            if not strikes:
                raise RuntimeError(
                    f"No SPX strikes around spot {spot:.2f} on the "
                    f"{expiry_str} chain."
                )
        else:
            unknown = [s for s in strikes if s not in all_strikes]
            if unknown:
                raise RuntimeError(
                    f"Strikes {unknown} are not listed on the {expiry_str} "
                    "SPX chain."
                )
            strikes = sorted(strikes)

        # The trading class is required: bare "SPX" options on CBOE are
        # ambiguous between classes (SPXW, SPXO, SPX) and IBKR rejects the
        # requests with error 200, so qualify by the chain's own class.
        calls = [
            Option("SPX", expiry_str, s, "C", "CBOE",
                   currency="USD", tradingClass=chain_def.tradingClass)
            for s in strikes
        ]
        puts = [
            Option("SPX", expiry_str, s, "P", "CBOE",
                   currency="USD", tradingClass=chain_def.tradingClass)
            for s in strikes
        ]
        contracts = calls + puts
        requested_count = len(contracts)
        qualified = ib.qualifyContracts(*contracts)
        all_qualified = len(qualified) == requested_count
        if not all_qualified:
            warnings.append(
                f"IBKR did not qualify {len(contracts) - len(qualified)} of "
                f"{len(contracts)} option contracts; those strikes are "
                "excluded, not treated as zero."
            )
        by_key = {(c.right, c.strike): c for c in qualified}
        strikes = sorted(
            {c.strike for c in qualified if c.right == "C"}
            & {c.strike for c in qualified if c.right == "P"}
        )
        if not strikes:
            raise RuntimeError(
                "IBKR did not qualify any usable SPX option contracts for "
                f"trading class {chain_def.tradingClass} expiry {expiry_str}."
            )
        contracts = (
            [by_key[("C", s)] for s in strikes]
            + [by_key[("P", s)] for s in strikes]
        )

        # Generic ticks: 100 = option volume, 101 = option open interest.
        # Open interest is only delivered on streaming requests.
        # Request in batches under the account's simultaneous-ticker cap:
        # streaming more contracts than the cap rejects the excess with
        # error 101 ("max number of tickers"), silently dropping strikes.
        batch_size = 80
        tickers = []
        for start in range(0, len(contracts), batch_size):
            batch = contracts[start:start + batch_size]
            batch_tickers = [
                ib.reqMktData(c, "100,101", False, False) for c in batch
            ]
            # Poll until every ticker has delivered its required inputs,
            # up to max_wait_seconds per batch. The cap is not a promise:
            # anything still missing stays NaN/None (never zero) and marks
            # the run incomplete below.
            waited = 0.0
            while waited < max_wait_seconds:
                ib.sleep(0.5)
                waited += 0.5
                if all(
                    _ticker_inputs_delivered(t, c.right)
                    for t, c in zip(batch_tickers, batch)
                ):
                    break
            for c in batch:
                ib.cancelMktData(c)
            tickers.extend(batch_tickers)

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
                "Open interest is missing for some strikes; the walls, "
                "flip and GEX are withheld rather than computed from "
                "partial data. Missing values are never treated as zero."
            )

        if df["call_iv"].isna().all() and df["put_iv"].isna().all():
            warnings.append(
                "IBKR delivered no model IV. The dashboard will try to back "
                "IV out of mid prices where possible."
            )

        complete = _assess_completeness(
            df, all_qualified, float(spot), _years_to_expiry(expiry_date),
            rate=rate,
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
            fetched_at=datetime.now(ZoneInfo("America/New_York")),
            market_data_type=md_type_names.get(market_data_type, str(market_data_type)),
            warnings=warnings,
            complete=complete,
            vix=vix_value,
            vix_basis=vix_basis,
            vix_time=vix_time,
            vix_md_type=vix_md_type,
        )
    finally:
        ib.disconnect()


# ---------------------------------------------------------------------------
# Demo provider (synthetic preview data - NOT from IBKR)
# ---------------------------------------------------------------------------

def make_demo_chain(
    spot: float = 6650.0,
    strike_count: int = 20,
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
        round(spot / 5) * 5 - strike_count * 5,
        round(spot / 5) * 5 + strike_count * 5 + 5,
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
        vix=float(np.clip(16.0 + rng.uniform(-4, 4), 9.0, 60.0)),
        vix_basis="quote",
        vix_time=datetime.now(),
        warnings=[
            "Demo mode: every value on this page is synthetic preview data, "
            "not from IBKR and not market data."
        ],
  )
