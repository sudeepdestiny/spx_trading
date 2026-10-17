"""
Adapter between the portfolio module and live_trading/ibkr_client.py.

IMPORTANT: I could not read the exact method signatures of your IBKRClient
class in this session, so this adapter is written against a small, obvious
interface and isolates ALL ibkr_client-specific calls to the three methods
marked TODO below. Everything else in the portfolio package only depends on
this adapter, not on IBKRClient directly -- so fixing these three call sites
to match your real client is the only integration work required.

Expected existing pieces on your live-trading branch:
    live_trading/ibkr_client.py -> class IBKRClient
    live_trading/live_trader.py imports: from ibkr_client import IBKRClient, IBKRWrapper

Typical ib_insync / ibapi patterns are used as the default guess.
Adjust the three TODO methods to match your actual client.
"""

from __future__ import annotations

from datetime import date
from typing import Optional

from live_trading.portfolio.models import Quote, Right, MarkSource


class IBKRQuoteAdapter:
    """Wraps an existing IBKRClient instance to satisfy the get_quote(...) interface."""

    def __init__(self, ibkr_client, underlying: str = "SPX"):
        self.client = ibkr_client
        self.underlying = underlying
        self._cache = {}

    def get_quote(self, expiry: date, strike: float, right: Right) -> Optional[Quote]:
        key = (expiry, strike, right)
        try:
            raw = self._fetch_from_ibkr(expiry, strike, right)
        except Exception:
            return self._cache.get(key)

        if raw is None:
            return self._cache.get(key)

        quote = Quote(
            expiry=expiry, strike=strike, right=right,
            bid=raw.get("bid"), ask=raw.get("ask"), last=raw.get("last"),
            iv=raw.get("iv"), delta=raw.get("delta"), gamma=raw.get("gamma"),
            theta=raw.get("theta"), vega=raw.get("vega"),
            source=MarkSource.MID,
        )
        self._cache[key] = quote
        return quote

    def _fetch_from_ibkr(self, expiry: date, strike: float, right: Right) -> Optional[dict]:
        """
        TODO: point this at your real IBKRClient method(s).

        If IBKRClient exposes something like:
            self.client.get_option_quote(symbol, expiry_str, strike, right_str)
        or a snapshot/ticker dict from ib_insync, adapt the call below.

        Expected return shape (all optional keys):
            {"bid": float, "ask": float, "last": float, "iv": float,
             "delta": float, "gamma": float, "theta": float, "vega": float}
        """
        expiry_str = expiry.strftime("%Y%m%d")
        right_str = "C" if right == Right.CALL else "P"

        if hasattr(self.client, "get_option_quote"):
            return self.client.get_option_quote(
                symbol=self.underlying, expiry=expiry_str, strike=strike, right=right_str
            )
        if hasattr(self.client, "get_snapshot"):
            return self.client.get_snapshot(self.underlying, expiry_str, strike, right_str)

        raise NotImplementedError(
            "IBKRQuoteAdapter._fetch_from_ibkr: wire this to your actual "
            "live_trading/ibkr_client.py method name."
        )

    def get_underlying_spot(self) -> Optional[float]:
        """TODO: point this at your real spot/underlying price method."""
        if hasattr(self.client, "get_underlying_price"):
            return self.client.get_underlying_price(self.underlying)
        if hasattr(self.client, "get_spot"):
            return self.client.get_spot(self.underlying)
        return None


class ManualQuoteProvider:
    """Fallback provider for testing/manual entry when IBKR is not connected."""

    def __init__(self):
        self._quotes = {}

    def set_quote(self, expiry: date, strike: float, right: Right, **kwargs):
        self._quotes[(expiry, strike, right)] = Quote(
            expiry=expiry, strike=strike, right=right, source=MarkSource.MANUAL, **kwargs
        )

    def get_quote(self, expiry: date, strike: float, right: Right) -> Optional[Quote]:
        return self._quotes.get((expiry, strike, right))
