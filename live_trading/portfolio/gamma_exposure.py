"""
Gamma exposure (GEX) calculations for the SPX gamma dashboard.

Pure maths, no Streamlit and no broker connection, so it can be tested on
its own. Conventions follow the Black-Scholes model already used by
backtest/greeks_engine.py.

Important modelling caveats (these are estimates, not measurements):

* Open interest has no "side". The sign convention below assumes dealers are
  long calls and short puts (customers overwrite calls and buy protection).
  That assumption cannot be verified from public data, and vendors disagree.
  Flip it with ``dealer_calls_long=False`` for the opposite convention.
* Walls are not uniquely defined. "oi" picks the strike with the largest
  call / put open interest; "gex" picks the largest absolute per-side gamma
  exposure. Different choices give different levels.
* The gamma flip is a spot price where total signed GEX crosses zero after
  repricing every option at a hypothetical spot (IV held fixed), not the
  strike where the static per-strike GEX changes sign. A crossing may not
  exist, and several may exist; the one nearest the current spot is
  reported as the flip.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional

import numpy as np
import pandas as pd


CONTRACT_MULTIPLIER = 100  # SPX options: 100 shares of the index per contract
MIN_TIME_TO_EXPIRY = 1.0 / (365.0 * 24.0 * 60.0)  # one minute, avoids div-by-zero


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def bs_gamma(
    spot: float,
    strike: float,
    time_to_expiry: float,
    rate: float,
    vol: float,
) -> float:
    """Black-Scholes gamma (same for calls and puts)."""
    if spot <= 0 or strike <= 0 or vol <= 0:
        return 0.0

    t = max(time_to_expiry, MIN_TIME_TO_EXPIRY)
    d1 = (
        math.log(spot / strike)
        + (rate + 0.5 * vol * vol) * t
    ) / (vol * math.sqrt(t))

    return _norm_pdf(d1) / (spot * vol * math.sqrt(t))


def bs_price(
    spot: float,
    strike: float,
    time_to_expiry: float,
    rate: float,
    vol: float,
    option_type: str,
) -> float:
    """Black-Scholes price, used only to back out IV when IBKR IV is missing."""
    if spot <= 0 or strike <= 0 or vol <= 0:
        return 0.0

    t = max(time_to_expiry, MIN_TIME_TO_EXPIRY)
    d1 = (
        math.log(spot / strike)
        + (rate + 0.5 * vol * vol) * t
    ) / (vol * math.sqrt(t))
    d2 = d1 - vol * math.sqrt(t)
    norm_cdf = lambda x: 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

    if option_type.upper() == "C":
        return spot * norm_cdf(d1) - strike * math.exp(-rate * t) * norm_cdf(d2)
    return strike * math.exp(-rate * t) * norm_cdf(-d2) - spot * norm_cdf(-d1)


def implied_vol(
    price: float,
    spot: float,
    strike: float,
    time_to_expiry: float,
    rate: float,
    option_type: str,
    min_vol: float = 0.01,
    max_vol: float = 5.0,
) -> Optional[float]:
    """Bisection IV solve. Returns None when no bracket exists (do not guess)."""
    if price is None or price <= 0:
        return None

    low_price = bs_price(spot, strike, time_to_expiry, rate, min_vol, option_type)
    high_price = bs_price(spot, strike, time_to_expiry, rate, max_vol, option_type)

    if not (low_price <= price <= high_price):
        return None

    lo, hi = min_vol, max_vol
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        mid_price = bs_price(spot, strike, time_to_expiry, rate, mid, option_type)
        if mid_price < price:
            lo = mid
        else:
            hi = mid

    return 0.5 * (lo + hi)


def signed_gex(
    gamma: float,
    open_interest: float,
    spot: float,
    sign: float,
) -> float:
    """
    Dealer gamma exposure in dollars per 1% spot move.

    gamma * OI * multiplier * spot^2 * 1% is the standard GEX formula.
    ``sign`` carries the dealer-position assumption (+1 long, -1 short).
    """
    return sign * gamma * open_interest * CONTRACT_MULTIPLIER * spot * spot * 0.01


def per_strike_gex(
    chain: pd.DataFrame,
    spot: float,
    rate: float,
    time_to_expiry: float,
    dealer_calls_long: bool = True,
) -> pd.DataFrame:
    """
    Compute per-strike gamma exposure from a normalised chain.

    Required columns: strike, call_iv, put_iv, call_oi, put_oi.
    Rows with missing IV or missing OI keep None GEX values - a missing
    input is never silently treated as zero.
    """
    df = chain.copy()
    call_sign = 1.0 if dealer_calls_long else -1.0
    put_sign = -call_sign

    call_gammas = []
    put_gammas = []
    call_gex = []
    put_gex = []

    for _, row in df.iterrows():
        strike = float(row["strike"])

        c_iv = row.get("call_iv")
        p_iv = row.get("put_iv")
        c_oi = row.get("call_oi")
        p_oi = row.get("put_oi")

        c_gamma = (
            bs_gamma(spot, strike, time_to_expiry, rate, float(c_iv))
            if c_iv is not None and pd.notna(c_iv)
            else None
        )
        p_gamma = (
            bs_gamma(spot, strike, time_to_expiry, rate, float(p_iv))
            if p_iv is not None and pd.notna(p_iv)
            else None
        )

        call_gammas.append(c_gamma)
        put_gammas.append(p_gamma)

        call_gex.append(
            signed_gex(c_gamma, float(c_oi), spot, call_sign)
            if c_gamma is not None and c_oi is not None and pd.notna(c_oi)
            else None
        )
        put_gex.append(
            signed_gex(p_gamma, float(p_oi), spot, put_sign)
            if p_gamma is not None and p_oi is not None and pd.notna(p_oi)
            else None
        )

    df["call_gamma"] = call_gammas
    df["put_gamma"] = put_gammas
    df["call_gex"] = call_gex
    df["put_gex"] = put_gex
    df["net_gex"] = [
        (c or 0.0) + (p or 0.0) if c is not None or p is not None else None
        for c, p in zip(call_gex, put_gex)
    ]

    return df


@dataclass
class GammaWalls:
    call_wall: Optional[float]
    put_wall: Optional[float]
    method: str


def gamma_walls(
    gex_chain: pd.DataFrame,
    method: str = "oi",
) -> GammaWalls:
    """
    Pick the call and put walls.

    method="oi":  largest call OI strike / largest put OI strike.
    method="gex": largest absolute call-GEX strike / largest absolute put-GEX
                  strike (uses the signed GEX already on the frame).
    Strikes with missing inputs are skipped, never counted as zero.
    """
    if gex_chain.empty:
        return GammaWalls(None, None, method)

    if method == "gex":
        call_col, put_col = "call_gex", "put_gex"
        key = lambda s: s.abs()
    else:
        call_col, put_col = "call_oi", "put_oi"
        key = lambda s: s

    calls = gex_chain.dropna(subset=[call_col])
    puts = gex_chain.dropna(subset=[put_col])

    call_wall = None
    put_wall = None

    if not calls.empty:
        call_wall = float(calls.loc[key(calls[call_col]).idxmax(), "strike"])
    if not puts.empty:
        put_wall = float(puts.loc[key(puts[put_col]).idxmax(), "strike"])

    return GammaWalls(call_wall, put_wall, method)


def total_gex_at_spot(
    chain: pd.DataFrame,
    hypothetical_spot: float,
    rate: float,
    time_to_expiry: float,
    dealer_calls_long: bool = True,
) -> Optional[float]:
    """
    Total signed dealer GEX after repricing every option's gamma at a
    hypothetical spot (IV held fixed). Returns None if no usable rows.
    """
    call_sign = 1.0 if dealer_calls_long else -1.0
    put_sign = -call_sign
    total = 0.0
    used = 0

    for _, row in chain.iterrows():
        strike = float(row["strike"])

        c_iv = row.get("call_iv")
        p_iv = row.get("put_iv")
        c_oi = row.get("call_oi")
        p_oi = row.get("put_oi")

        if c_iv is not None and pd.notna(c_iv) and c_oi is not None and pd.notna(c_oi):
            g = bs_gamma(hypothetical_spot, strike, time_to_expiry, rate, float(c_iv))
            total += signed_gex(g, float(c_oi), hypothetical_spot, call_sign)
            used += 1

        if p_iv is not None and pd.notna(p_iv) and p_oi is not None and pd.notna(p_oi):
            g = bs_gamma(hypothetical_spot, strike, time_to_expiry, rate, float(p_iv))
            total += signed_gex(g, float(p_oi), hypothetical_spot, put_sign)
            used += 1

    return total if used else None


@dataclass
class GammaFlip:
    flip: Optional[float]
    crossings: List[float]
    grid_spots: List[float]
    grid_gex: List[Optional[float]]


def gamma_flip(
    chain: pd.DataFrame,
    spot: float,
    rate: float,
    time_to_expiry: float,
    window: float = 400.0,
    steps: int = 81,
    dealer_calls_long: bool = True,
) -> GammaFlip:
    """
    Find the zero-gamma spot by repricing total GEX over a grid of
    hypothetical spots around the current spot. All zero crossings are
    reported; ``flip`` is the crossing nearest the current spot, or None
    when the total never crosses zero inside the window.
    """
    grid_spots = np.linspace(spot - window, spot + window, steps).tolist()
    grid_gex = [
        total_gex_at_spot(
            chain, s, rate, time_to_expiry,
            dealer_calls_long=dealer_calls_long,
        )
        for s in grid_spots
    ]

    # Filter the raw sign changes. At very short expiries the aggregate
    # curve wiggles around zero in the wings and near wall strikes, which
    # creates flicker crossings that are not meaningful flips. Keep a
    # crossing only when (a) each side's sign persists for at least
    # ``min_run`` of the next ``probe`` grid points, and (b) at least one
    # side is a non-trivial fraction of the largest total on the grid
    # (drops far-wing float noise where every option's gamma is tiny).
    magnitudes = [abs(g) for g in grid_gex if g is not None]
    max_abs = max(magnitudes) if magnitudes else 0.0
    noise_floor = 0.005 * max_abs
    probe = 5
    min_run = 3

    def _sign(value) -> int:
        if value is None or value == 0.0:
            return 0
        return 1 if value > 0 else -1

    def _persists(start: int, step: int, sign: int) -> bool:
        checked = 0
        held = 0
        j = start
        while 0 <= j < len(grid_gex) and checked < probe:
            s = _sign(grid_gex[j])
            if s != 0:
                checked += 1
                if s == sign:
                    held += 1
            j += step
        return held >= min(min_run, max(checked, 1))

    crossings: List[float] = []
    for i in range(len(grid_spots) - 1):
        g0, g1 = grid_gex[i], grid_gex[i + 1]
        if g0 is None or g1 is None:
            continue
        if g0 * g1 >= 0:
            continue
        if max(abs(g0), abs(g1)) < noise_floor:
            continue
        if not _persists(i - 1, -1, _sign(g0)):
            continue
        if not _persists(i + 2, 1, _sign(g1)):
            continue
        # Linear interpolation between grid points.
        frac = abs(g0) / (abs(g0) + abs(g1))
        crossings.append(
            grid_spots[i] + frac * (grid_spots[i + 1] - grid_spots[i])
        )

    flip = min(crossings, key=lambda c: abs(c - spot)) if crossings else None

    return GammaFlip(
        flip=flip,
        crossings=crossings,
        grid_spots=grid_spots,
        grid_gex=grid_gex,
    )


@dataclass
class SessionWindow:
    name: str
    description: str
    start: str
    end: str


# Time-of-day framework from the shared post. These are the post author's
# labels, shown for orientation only - they are not validated signals and
# not trading advice.
SESSION_WINDOWS_ET = [
    SessionWindow(
        "Opening bell trap",
        "Post framework: no trade; locate the gamma flip around 9:35.",
        "09:30", "10:00",
    ),
    SessionWindow(
        "Walls set",
        "Post framework: call/put walls become reliable; decay is slowest.",
        "10:00", "12:00",
    ),
    SessionWindow(
        "Quiet hour",
        "Post framework: volume drops; watch, do not act.",
        "12:00", "13:00",
    ),
    SessionWindow(
        "Credit spread window",
        "Post framework: decay accelerates; the post discusses credit "
        "spreads near the walls here.",
        "13:00", "15:00",
    ),
    SessionWindow(
        "Danger zone",
        "Post framework: gamma highest; post says no new credit spreads.",
        "15:00", "16:00",
    ),
]


def session_window(now_et) -> Optional[SessionWindow]:
    """Return the post's window for an ET datetime, or None outside RTH."""
    clock = now_et.strftime("%H:%M")
    for window in SESSION_WINDOWS_ET:
        if window.start <= clock < window.end:
            return window
    return None
