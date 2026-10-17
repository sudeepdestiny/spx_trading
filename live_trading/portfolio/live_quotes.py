"""
Live price adapter using the existing IBKRClient.get_option_chain() method.

This adapter caches a single option-chain snapshot per expiry. The strategy
dashboard asks it for individual option quotes, but the adapter only refreshes
the full chain once every refresh interval.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Dict, Optional

import pandas as pd

from live_trading.portfolio.models import Quote, Right, MarkSource


class LiveSPXQuoteProvider:
    def __init__(
        self,
        trader,
        underlying: str = "SPX",
        refresh_seconds: int = 10,
        strike_padding: float = 300.0,
    ):
        self.trader = trader
        self.underlying = underlying
        self.refresh_seconds = refresh_seconds
        self.strike_padding = strike_padding
        self._chain_by_expiry: Dict[str, pd.DataFrame] = {}
        self._refreshed_at: Dict[str, datetime] = {}

    def refresh_chain(
        self,
        expiry: date,
        spot: float,
        force: bool = False,
    ) -> pd.DataFrame:
        expiry_key = expiry.strftime("%Y%m%d")
        last_refresh = self._refreshed_at.get(expiry_key)

        needs_refresh = (
            force
            or expiry_key not in self._chain_by_expiry
            or last_refresh is None
            or datetime.utcnow() - last_refresh
            >= timedelta(seconds=self.refresh_seconds)
        )

        if not needs_refresh:
            return self._chain_by_expiry[expiry_key]

        raw_chain = self.trader.get_live_option_chain(
            underlying=self.underlying,
            expiry=expiry_key,
        )

        if raw_chain is None:
            raw_chain = pd.DataFrame()

        self._chain_by_expiry[expiry_key] = raw_chain.copy()
        self._refreshed_at[expiry_key] = datetime.utcnow()

        return self._chain_by_expiry[expiry_key]

    def get_quote(
        self,
        expiry: date,
        strike: float,
        right: Right,
    ) -> Optional[Quote]:
        """
        Look up a contract quote in the most recently refreshed chain.

        Call `refresh_chain()` before valuing portfolio legs.
        """
        expiry_key = expiry.strftime("%Y%m%d")
        chain = self._chain_by_expiry.get(expiry_key)

        if chain is None or chain.empty:
            return None

        if "strike" not in chain.columns:
            return None

        rows = chain.loc[
            pd.to_numeric(
                chain["strike"],
                errors="coerce",
            )
            == float(strike)
        ]

        if rows.empty:
            return None

        row = rows.iloc[0]

        if right == Right.CALL:
            bid = self._number(
                row,
                ["call_bid", "CallBid", "callBid"],
            )
            ask = self._number(
                row,
                ["call_ask", "CallAsk", "callAsk"],
            )
            last = self._number(
                row,
                [
                    "call_mid",
                    "call_price",
                    "CallLTP",
                    "bs_call_price",
                ],
            )
            iv = self._number(
                row,
                [
                    "call_iv",
                    "call_implied_volatility",
                    "iv",
                    "implied_volatility",
                ],
            )
            delta = self._number(
                row,
                ["call_delta", "CallDelta"],
            )
            gamma = self._number(
                row,
                ["call_gamma", "CallGamma"],
            )
            theta = self._number(
                row,
                ["call_theta", "CallTheta"],
            )
            vega = self._number(
                row,
                ["call_vega", "CallVega"],
            )
        else:
            bid = self._number(
                row,
                ["put_bid", "PutBid", "putBid"],
            )
            ask = self._number(
                row,
                ["put_ask", "PutAsk", "putAsk"],
            )
            last = self._number(
                row,
                [
                    "put_mid",
                    "put_price",
                    "PutLTP",
                    "bs_put_price",
                ],
            )
            iv = self._number(
                row,
                [
                    "put_iv",
                    "put_implied_volatility",
                    "iv",
                    "implied_volatility",
                ],
            )
            delta = self._number(
                row,
                ["put_delta", "PutDelta"],
            )
            gamma = self._number(
                row,
                ["put_gamma", "PutGamma"],
            )
            theta = self._number(
                row,
                ["put_theta", "PutTheta"],
            )
            vega = self._number(
                row,
                ["put_vega", "PutVega"],
            )

        if bid is None and ask is None and last is None:
            return None

        return Quote(
            expiry=expiry,
            strike=strike,
            right=right,
            bid=bid,
            ask=ask,
            last=last,
            iv=iv,
            delta=delta,
            gamma=gamma,
            theta=theta,
            vega=vega,
            source=MarkSource.MID,
        )

    @staticmethod
    def _number(
        row: pd.Series,
        candidate_columns: list,
    ) -> Optional[float]:
        for column in candidate_columns:
            if column not in row.index:
                continue

            value = row[column]

            if pd.isna(value):
                continue

            try:
                value = float(value)
            except (TypeError, ValueError):
                continue

            if value > 0:
                return value

        return None