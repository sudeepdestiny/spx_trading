from __future__ import annotations

from datetime import date
from typing import Optional

import numpy as np
import pandas as pd


REQUIRED_COLUMNS = [
    "expiry",
    "strike",
    "call_bid",
    "call_ask",
    "call_last",
    "call_mid",
    "call_iv",
    "call_delta",
    "call_gamma",
    "call_theta",
    "call_vega",
    "put_bid",
    "put_ask",
    "put_last",
    "put_mid",
    "put_iv",
    "put_delta",
    "put_gamma",
    "put_theta",
    "put_vega",
]


def _first_value(
    row: pd.Series,
    candidate_columns: list[str],
    default=None,
):
    """Pick the first usable field from possible names in older/newer chain formats."""
    for column in candidate_columns:
        if column not in row.index:
            continue

        value = row[column]

        if pd.isna(value):
            continue

        return value

    return default


def _number(
    value,
    default: Optional[float] = None,
) -> Optional[float]:
    """Convert valid IBKR number to float; discard NaN, zero, and negative sentinels."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return default

    if not np.isfinite(value) or value <= 0:
        return default

    return value


def _mid(
    bid: Optional[float],
    ask: Optional[float],
    last: Optional[float],
) -> Optional[float]:
    """Prefer midpoint, then fall back to last price."""
    if bid is not None and ask is not None:
        return round((bid + ask) / 2.0, 4)

    return last


def normalise_ibkr_chain(
    raw_chain: pd.DataFrame,
    expiry: date,
) -> pd.DataFrame:
    """
    Convert the output of your existing `IBKRClient.get_option_chain()` into
    one stable schema used by the Streamlit UI and strategy portfolio.

    It supports the field names already used across your repo, including:
    call_bid, call_mid, call_price, CallLTP, call_delta, bs_call_price,
    put_bid, put_mid, put_price, PutLTP, put_delta, bs_put_price.
    """
    if raw_chain is None or raw_chain.empty:
        return pd.DataFrame(columns=REQUIRED_COLUMNS)

    if "strike" not in raw_chain.columns:
        raise ValueError(
            "IBKR option chain does not contain a 'strike' column. "
            f"Available columns: {raw_chain.columns.tolist()}"
        )

    chain = raw_chain.copy()

    chain["strike"] = pd.to_numeric(
        chain["strike"],
        errors="coerce",
    )

    chain = chain.dropna(
        subset=["strike"],
    ).sort_values(
        "strike",
    )

    normalised_rows = []

    for _, row in chain.iterrows():
        call_bid = _number(
            _first_value(
                row,
                ["call_bid", "CallBid", "callBid"],
            )
        )

        call_ask = _number(
            _first_value(
                row,
                ["call_ask", "CallAsk", "callAsk"],
            )
        )

        call_last = _number(
            _first_value(
                row,
                [
                    "call_last",
                    "call_price",
                    "CallLTP",
                    "call_mid",
                    "bs_call_price",
                ],
            )
        )

        put_bid = _number(
            _first_value(
                row,
                ["put_bid", "PutBid", "putBid"],
            )
        )

        put_ask = _number(
            _first_value(
                row,
                ["put_ask", "PutAsk", "putAsk"],
            )
        )

        put_last = _number(
            _first_value(
                row,
                [
                    "put_last",
                    "put_price",
                    "PutLTP",
                    "put_mid",
                    "bs_put_price",
                ],
            )
        )

        normalised_rows.append(
            {
                "expiry": expiry,
                "strike": float(row["strike"]),

                "call_bid": call_bid,
                "call_ask": call_ask,
                "call_last": call_last,
                "call_mid": _mid(
                    call_bid,
                    call_ask,
                    call_last,
                ),
                "call_iv": _number(
                    _first_value(
                        row,
                        [
                            "call_iv",
                            "call_implied_volatility",
                            "iv",
                            "implied_volatility",
                        ],
                    )
                ),
                "call_delta": _number(
                    _first_value(
                        row,
                        ["call_delta", "CallDelta"],
                    )
                ),
                "call_gamma": _number(
                    _first_value(
                        row,
                        ["call_gamma", "CallGamma"],
                    )
                ),
                "call_theta": _number(
                    _first_value(
                        row,
                        ["call_theta", "CallTheta"],
                    )
                ),
                "call_vega": _number(
                    _first_value(
                        row,
                        ["call_vega", "CallVega"],
                    )
                ),

                "put_bid": put_bid,
                "put_ask": put_ask,
                "put_last": put_last,
                "put_mid": _mid(
                    put_bid,
                    put_ask,
                    put_last,
                ),
                "put_iv": _number(
                    _first_value(
                        row,
                        [
                            "put_iv",
                            "put_implied_volatility",
                            "iv",
                            "implied_volatility",
                        ],
                    )
                ),
                "put_delta": _number(
                    _first_value(
                        row,
                        ["put_delta", "PutDelta"],
                    )
                ),
                "put_gamma": _number(
                    _first_value(
                        row,
                        ["put_gamma", "PutGamma"],
                    )
                ),
                "put_theta": _number(
                    _first_value(
                        row,
                        ["put_theta", "PutTheta"],
                    )
                ),
                "put_vega": _number(
                    _first_value(
                        row,
                        ["put_vega", "PutVega"],
                    )
                ),
            }
        )

    return pd.DataFrame(
        normalised_rows,
        columns=REQUIRED_COLUMNS,
    )
from datetime import datetime, timedelta
from typing import Dict


class SPXOptionChainService:
    """
    Calls the existing IBKRClient once per expiry/spot window and caches
    normalised data briefly to avoid excessive TWS market-data requests.
    """

    def __init__(
        self,
        ibkr_client,
        symbol: str = "SPX",
        cache_seconds: int = 8,
        strike_padding: int = 300,
    ):
        self.ibkr_client = ibkr_client
        self.symbol = symbol
        self.cache_seconds = cache_seconds
        self.strike_padding = strike_padding

        self._cache: Dict[str, pd.DataFrame] = {}
        self._cache_time: Dict[str, datetime] = {}

    def get_chain(
        self,
        expiry: date,
        spot: float,
        force_refresh: bool = False,
    ) -> pd.DataFrame:
        expiry_key = expiry.strftime("%Y%m%d")

        cached_at = self._cache_time.get(
            expiry_key,
        )

        cache_valid = (
            not force_refresh
            and expiry_key in self._cache
            and cached_at is not None
            and datetime.utcnow() - cached_at
            < timedelta(seconds=self.cache_seconds)
        )

        if cache_valid:
            return self._cache[expiry_key].copy()

        low_strike = int(
            spot - self.strike_padding,
        )

        high_strike = int(
            spot + self.strike_padding,
        )

        raw_chain = self.ibkr_client.get_option_chain(
            symbol=self.symbol,
            expiry=expiry_key,
            strike_range=(
                low_strike,
                high_strike,
            ),
        )

        normalised_chain = normalise_ibkr_chain(
            raw_chain=raw_chain,
            expiry=expiry,
        )

        self._cache[expiry_key] = normalised_chain
        self._cache_time[expiry_key] = datetime.utcnow()

        return normalised_chain.copy()

    def clear_cache(self) -> None:
        self._cache = {}
        self._cache_time = {}