"""
Sampling providers for the SPX volatility panel.

Two sources:

* ``sample_live_volatility`` - one VIX and SPX at-the-money IV reading from
  IBKR with ib_insync, following the same connection pattern as
  ``gamma_data.fetch_live_gamma_inputs``. Read-only market data: it never
  places, modifies or cancels an order.

  A sample is taken per call; the panel accumulates samples while its page
  session is open. When IBKR does not deliver a value (model greeks, a
  quote, a VIX level) the field stays None - it is never guessed.

* ``make_demo_sample`` - synthetic random-walk sample for previewing the
  panel without a broker connection. Every value is made up; the panel
  labels this loudly as demo data.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime
from typing import List, Optional
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd


ET = ZoneInfo("America/New_York")


@dataclass
class VolSample:
    """One VIX / SPX ATM implied-volatility reading."""

    sampled_at: datetime             # ET request time
    source: str                      # "ibkr" or "demo"
    expiry: Optional[date] = None
    spot: Optional[float] = None
    atm_strike: Optional[float] = None
    call_iv: Optional[float] = None  # decimal, e.g. 0.14
    put_iv: Optional[float] = None
    iv_basis: Optional[str] = None   # "model" / "price-fallback" / None
    vix: Optional[float] = None
    vix_basis: Optional[str] = None  # "quote" (last index value) or "previous-close"
    vix_time: Optional[datetime] = None  # feed quote time when IBKR supplies one
    vix_md_type: Optional[str] = None    # actual market-data type IBKR reports
    opt_time: Optional[datetime] = None  # older of the call/put feed quote times
    opt_md_type: Optional[str] = None    # option quote type IBKR reports ("mixed" if sides differ)
    market_data_type: Optional[str] = None  # requested md type, e.g. "live"
    client_id: Optional[int] = None    # API client ID the sample was taken with
    warnings: List[str] = field(default_factory=list)

    @property
    def atm_iv(self) -> Optional[float]:
        """Mean of the ATM call and put IV, skipping missing sides."""
        values = [v for v in (self.call_iv, self.put_iv) if v is not None]
        if not values:
            return None
        return float(sum(values) / len(values))


# ---------------------------------------------------------------------------
# Aggregation helpers (pure; unit-tested)
# ---------------------------------------------------------------------------

def pct_change(current: Optional[float], reference: Optional[float]) -> Optional[float]:
    """Percent change of current vs reference; None when either is missing
    or the reference is zero."""
    if current is None or reference is None or reference == 0:
        return None
    return (current - reference) / reference * 100.0


def samples_to_frame(samples: List[VolSample]) -> pd.DataFrame:
    """Samples as a table with per-series change since the first sample.

    The first sample in the session is the VIX baseline, labelled
    honestly in the view - it is not the market open unless sampling
    started there. The ATM IV baseline is the first sample *on the same IV
    basis* as the row: model greeks and price-derived IV are different
    measures, so a change computed across a basis switch would read the
    switch itself as a volatility move. The first sample of each basis
    shows 0% change.
    """
    rows = []
    first_vix = next((s.vix for s in samples if s.vix is not None), None)
    first_iv_by_basis: dict = {}
    for s in samples:
        atm_iv = s.atm_iv
        basis_key = s.iv_basis or "unavailable"
        if atm_iv is not None and basis_key not in first_iv_by_basis:
            first_iv_by_basis[basis_key] = atm_iv
        rows.append({
            "sampled_at": s.sampled_at,
            "spot": s.spot,
            "strike": s.atm_strike,
            "expiry": s.expiry,
            "vix": s.vix,
            "vix_basis": s.vix_basis,
            "vix_feed_time": s.vix_time,
            "vix_md_type": s.vix_md_type,
            "opt_feed_time": s.opt_time,
            "opt_md_type": s.opt_md_type,
            "call_iv_pct": None if s.call_iv is None else s.call_iv * 100.0,
            "put_iv_pct": None if s.put_iv is None else s.put_iv * 100.0,
            "atm_iv_pct": None if atm_iv is None else atm_iv * 100.0,
            "iv_basis": s.iv_basis,
            "client_id": s.client_id,
            "vix_change_pct": pct_change(s.vix, first_vix),
            "atm_iv_change_pct": pct_change(
                atm_iv, first_iv_by_basis.get(basis_key)),
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Live IBKR provider
# ---------------------------------------------------------------------------

def _positive(value) -> bool:
    return value is not None and np.isfinite(value) and value > 0


def _connect(ib, host: str, port: int, client_id: int) -> int:
    """Connect to TWS / IB Gateway, returning the client ID used.

    client_id 0 picks a free ID automatically: the gateway allows one
    connection per client ID and TWS itself retries IDs 1-31, so a random
    ID from 32-999 is tried instead of a fixed default that can collide
    with another session. A fixed nonzero ID is used as given and any
    connection failure propagates unchanged.
    """
    if client_id == 0:
        candidates = random.sample(range(32, 1000), 10)
    else:
        candidates = [client_id]
    last_exc: Optional[Exception] = None
    for candidate in candidates:
        try:
            ib.connect(host, port, clientId=candidate, timeout=10)
            return candidate
        except (ConnectionError, OSError, TimeoutError) as exc:
            last_exc = exc
    raise ConnectionError(
        "Could not connect to IBKR: no free API client ID after "
        f"{len(candidates)} attempts. Another session may hold the "
        "requested ID."
    ) from last_exc


def _years_to_expiry(expiry: date, now_et: Optional[datetime] = None) -> float:
    """Years until 16:00 ET on the expiry date (same convention as the view)."""
    now_et = now_et or datetime.now(ET)
    close_et = datetime.combine(expiry, dtime(16, 0), tzinfo=ET)
    years = (close_et - now_et).total_seconds() / (365.0 * 24.0 * 3600.0)
    return max(years, 1.0 / (365.0 * 24.0 * 60.0))  # one-minute floor


def _fallback_iv(price, spot, strike, right, expiry, rate):
    """IV solved from an option price; None when the solver has no bracket."""
    if price is None or not np.isfinite(price) or price <= 0:
        return None
    from live_trading.portfolio.gamma_exposure import implied_vol
    try:
        return implied_vol(
            price=float(price), spot=float(spot), strike=float(strike),
            time_to_expiry=_years_to_expiry(expiry), rate=rate,
            option_type=right,
        )
    except Exception:
        return None


def sample_live_volatility(
    expiry: Optional[date] = None,
    rate: float = 0.045,
    host: str = "127.0.0.1",
    port: int = 7497,
    client_id: int = 0,
    market_data_type: int = 1,
    max_wait_seconds: float = 20.0,
) -> VolSample:
    """
    Take one VIX + SPX at-the-money IV reading from IBKR.

    market_data_type: 1 live, 2 frozen, 3 delayed, 4 delayed-frozen.
    Delayed modes may omit greeks and/or quotes depending on the account's
    subscriptions; anything not delivered stays None.

    client_id 0 (the default) picks a free API client ID automatically per
    sample; see _connect. The sample records the ID actually used so the
    panel can show it.

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
        used_client_id = _connect(ib, host, port, client_id)
        ib.reqMarketDataType(market_data_type)

        spx = Index("SPX", "CBOE")
        ib.qualifyContracts(spx)

        # Stream and poll instead of a one-shot snapshot: outside market
        # hours the data farms can take over ten seconds to wake, and a
        # snapshot request ends empty before the first tick arrives.
        spot_ticker = ib.reqMktData(spx, "", False, False)
        spot = float("nan")
        for _ in range(25):
            ib.sleep(1)
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

        # VIX level: same connection as the SPX chain. The basis
        # ("quote" vs "previous-close"), the feed's quote time and the
        # actual market-data type IBKR reports are kept so the view can
        # label the value honestly; a previous close is never shown as a
        # live quote.
        vix_value = vix_basis = vix_time = vix_md_type = None
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
            vix_value = vix_basis = vix_time = vix_md_type = None
        if vix_value is None:
            warnings.append(
                "IBKR did not deliver a VIX level (the account may lack the "
                "CBOE index data permission, or the feed may be quiet)."
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
        atm_strike = min(all_strikes, key=lambda s: abs(s - spot))

        # The trading class is required: bare "SPX" options on CBOE are
        # ambiguous between classes (SPXW, SPXO, SPX) and IBKR rejects the
        # requests with error 200, so qualify by the chain's own class.
        contracts = [
            Option("SPX", expiry_str, atm_strike, right, "CBOE",
                   currency="USD", tradingClass=chain_def.tradingClass)
            for right in ("C", "P")
        ]
        qualified = ib.qualifyContracts(*contracts)
        if len(qualified) != 2:
            raise RuntimeError(
                f"IBKR did not qualify the ATM SPX contracts for trading "
                f"class {chain_def.tradingClass} expiry {expiry_str} "
                f"strike {atm_strike}."
            )
        by_right = {c.right: c for c in qualified}

        call_iv = put_iv = None
        iv_basis = None
        opt_times: List[datetime] = []
        opt_md_types: List[str] = []
        for right in ("C", "P"):
            contract = by_right[right]
            ticker = ib.reqMktData(contract, "", False, False)
            waited = 0.0
            while waited < max_wait_seconds:
                ib.sleep(0.5)
                waited += 0.5
                if ticker.modelGreeks is not None:
                    break
                if _positive(ticker.bid) and _positive(ticker.ask):
                    break
            feed_time = getattr(ticker, "time", None)
            if isinstance(feed_time, datetime):
                opt_times.append(feed_time)
            try:
                md_actual = md_type_names.get(
                    int(getattr(ticker, "marketDataType", None)))
            except (TypeError, ValueError):
                md_actual = None
            if md_actual:
                opt_md_types.append(md_actual)
            ib.cancelMktData(contract)

            greeks = ticker.modelGreeks
            if greeks is not None and _positive(greeks.impliedVol):
                iv = float(greeks.impliedVol)
                basis = "model"
            else:
                mid = None
                if _positive(ticker.bid) and _positive(ticker.ask):
                    mid = (float(ticker.bid) + float(ticker.ask)) / 2.0
                elif _positive(ticker.last):
                    mid = float(ticker.last)
                iv = _fallback_iv(
                    mid, float(spot), float(atm_strike), right,
                    expiry_date, rate,
                )
                basis = "price-fallback" if iv is not None else None

            if iv is not None:
                iv_basis = iv_basis or basis
            if right == "C":
                call_iv = iv
            else:
                put_iv = iv

        opt_time = min(opt_times) if opt_times else None
        opt_md_type = (
            opt_md_types[0]
            if opt_md_types and all(t == opt_md_types[0] for t in opt_md_types)
            else ("mixed" if opt_md_types else None)
        )
        if opt_md_type in ("delayed", "delayed-frozen", "frozen", "mixed"):
            warnings.append(
                f"Option quotes are {opt_md_type}, not live; the implied "
                "volatility can lag the market."
            )
        if opt_time is None and (call_iv is not None or put_iv is not None):
            warnings.append(
                "The feed supplied no option quote time, so IV freshness "
                "cannot be confirmed."
            )

        if call_iv is None and put_iv is None:
            warnings.append(
                "IBKR delivered no model greeks and no usable price for the "
                "ATM contracts, so no implied volatility could be derived "
                "(delayed data may omit greeks; try a different market-data "
                "type or market hours)."
            )
        elif iv_basis == "price-fallback":
            warnings.append(
                "IV is solved from option mid/last prices, not IBKR model "
                "greeks; treat it as an estimate."
            )

        return VolSample(
            sampled_at=datetime.now(ET),
            source="ibkr",
            expiry=expiry_date,
            spot=float(spot),
            atm_strike=float(atm_strike),
            call_iv=call_iv,
            put_iv=put_iv,
            iv_basis=iv_basis,
            vix=vix_value,
            vix_basis=vix_basis,
            vix_time=vix_time,
            vix_md_type=vix_md_type,
            opt_time=opt_time,
            opt_md_type=opt_md_type,
            market_data_type=md_type_names.get(
                market_data_type, str(market_data_type)),
            client_id=used_client_id,
            warnings=warnings,
        )
    finally:
        ib.disconnect()


# ---------------------------------------------------------------------------
# Demo provider (synthetic preview data - NOT from IBKR)
# ---------------------------------------------------------------------------

def make_demo_sample(
    previous: Optional[VolSample] = None,
    spot0: float = 6650.0,
    vix0: float = 20.0,
    iv0: float = 0.18,
    expiry: Optional[date] = None,
    seed: Optional[int] = None,
) -> VolSample:
    """
    Synthetic random-walk sample so the panel can be previewed with no
    broker. Continues from ``previous`` when given so a demo session shows
    a trend. Numbers are invented; they are not market data.
    """
    rng = np.random.default_rng(seed)

    if previous is not None and previous.source == "demo":
        spot = float(previous.spot) + float(rng.normal(0, 4))
        vix = float(np.clip(previous.vix + rng.normal(0, 0.3), 9.0, 60.0))
        iv = float(np.clip(
            (previous.atm_iv or iv0) + rng.normal(0, 0.004), 0.05, 0.8))
        strike = previous.atm_strike
        expiry = previous.expiry
    else:
        spot = spot0
        vix = vix0
        iv = iv0
        strike = float(round(spot / 5) * 5)
        if expiry is None:
            expiry = date.today()

    return VolSample(
        sampled_at=datetime.now(ET),
        source="demo",
        expiry=expiry,
        spot=spot,
        atm_strike=strike,
        call_iv=iv,
        put_iv=iv,
        iv_basis="model",
        vix=vix,
        vix_basis="quote",
        vix_time=datetime.now(ET),
        vix_md_type="live",
        opt_time=datetime.now(ET),
        opt_md_type="live",
        market_data_type=None,
        warnings=[
            "Demo mode: every value in this sample is synthetic preview "
            "data, not from IBKR and not market data."
        ],
    )
