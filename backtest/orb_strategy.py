"""5-minute SPX Opening Range Breakout, long premium.

Deck rules:
- Opening range is the first 5 minutes after 09:30 ET.
- Buy a call above that high. Buy a put below that low.
- One attempt per day. Skip a range under 2 points.

Added rules:
- Mark PDO, PDH, PDL, PDC, PWO, PWH, PWL, PWC, and current-week open.
- Skip a break that starts inside one of those levels unless price has closed through it.
- First target is the nearest level in the trade direction, if it is within 30 points.
- Stop is a 5-minute close through the 9 EMA. Until 9 closes exist, the stop is the
  opposite side of the opening range.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

from config import DATA_PATH, STRATEGY_CONFIG

logger = logging.getLogger(__name__)

DEFAULTS = {
    "orb_minutes": 5,
    "ema_span": 9,
    "session_open": "09:30",
    "session_close": "15:45",
    "max_attempts": 1,
    "min_range_points": 2.0,
    "max_target_points": 30.0,
    "level_buffer": 1.0,
    "contracts": 1,
    "multiplier": 100,
    "strike_step": 5,
}


class ORBStrategy:
    def __init__(self, strategy_config=None):
        self.config = {**DEFAULTS, **(strategy_config or STRATEGY_CONFIG)}
        self.trade_log = []

    def run_backtest(self, option_chain: pd.DataFrame, trade_date=None, dte_days: int = 0, expiry_date=None):
        self.trade_log = []
        empty = {"status": "empty", "trade_count": 0, "total_pnl": 0.0}
        if option_chain is None or option_chain.empty or trade_date is None:
            return pd.DataFrame(), empty

        bars = self._session_bars(str(trade_date), option_chain)
        if bars.empty:
            return pd.DataFrame(), {"status": "no_rth_data", "trade_count": 0, "total_pnl": 0.0}

        bars = add_ema9(bars, span=int(self.config["ema_span"]))
        or_high, or_low, or_end = opening_range(bars, int(self.config["orb_minutes"]))
        if or_high is None:
            return pd.DataFrame(), {"status": "no_opening_range", "trade_count": 0, "total_pnl": 0.0}

        rng = or_high - or_low
        levels = load_levels(str(trade_date), DATA_PATH)
        if rng < float(self.config["min_range_points"]):
            return pd.DataFrame(), {
                "status": "dead_open",
                "trade_count": 0,
                "total_pnl": 0.0,
                "or_high": or_high,
                "or_low": or_low,
                "levels": levels,
            }

        chain = option_chain.copy()
        chain["bucket_ts"] = to_et(chain["timestamp"])
        after = bars[bars["ts"] > or_end]
        position = None
        attempts = 0

        for row in after.itertuples(index=False):
            if position is None:
                if attempts >= int(self.config["max_attempts"]):
                    break
                side = "long" if row.high > or_high else "short" if row.low < or_low else None
                if side is None:
                    continue
                if level_blocks_entry(or_high, or_low, side, levels, float(row.close), float(self.config["level_buffer"])):
                    continue
                leg = self._pick_leg(chain, row.ts, float(row.close), side)
                if leg is None:
                    continue
                attempts += 1
                position = {
                    "entry_time": row.ts,
                    "side": side,
                    "right": "C" if side == "long" else "P",
                    "strike": leg["strike"],
                    "entry_price": leg["price"],
                    "entry_spot": float(row.close),
                    "or_high": or_high,
                    "or_low": or_low,
                    "target": trade_target(float(row.close), side, levels, float(self.config["max_target_points"]), float(self.config["level_buffer"])),
                }
                continue

            reason = None
            if target_hit(position, row):
                reason = "level_target"
            elif ema_stop_hit(position, row):
                reason = "ema9_stop" if bool(row.ema_ready) else "range_stop"
            elif row.ts == after["ts"].iloc[-1]:
                reason = "session_close"

            if reason:
                exit_price = self._mark(chain, row.ts, position["strike"], position["right"])
                self._close(position, row.ts, 0.05 if exit_price is None else exit_price, reason, expiry_date, trade_date)
                position = None

        trades = pd.DataFrame(self.trade_log)
        pnl = float(trades["pnl"].sum()) if not trades.empty else 0.0
        return trades, {
            "status": "ok",
            "trade_date": str(trade_date),
            "dte": dte_days,
            "expiry_date": str(expiry_date) if expiry_date else None,
            "or_high": or_high,
            "or_low": or_low,
            "range_points": rng,
            "levels": levels,
            "trade_count": len(trades),
            "total_pnl": pnl,
            "win_rate": float((trades["pnl"] > 0).mean()) if not trades.empty else 0.0,
        }

    def _session_bars(self, trade_date: str, option_chain: pd.DataFrame) -> pd.DataFrame:
        path = price_path(trade_date)
        if path.exists():
            bars = read_bars(path)
        else:
            bars = bars_from_chain(option_chain)
        open_t = pd.to_datetime(self.config["session_open"]).time()
        close_t = pd.to_datetime(self.config["session_close"]).time()
        bars = bars[bars["ts"].dt.time.between(open_t, close_t)]
        return bars.sort_values("ts").reset_index(drop=True)

    def _pick_leg(self, chain: pd.DataFrame, ts, spot: float, side: str):
        step = float(self.config.get("strike_step", 5))
        strike = round(spot / step) * step
        snap = chain[chain["bucket_ts"] == ts]
        if snap.empty:
            return None
        row = snap.iloc[(snap["strike"] - strike).abs().argsort()[:1]]
        if row.empty:
            return None
        rec = row.iloc[0]
        price = rec["call_mid" if side == "long" else "put_mid"]
        if pd.isna(price) or float(price) <= 0:
            return None
        return {"strike": float(rec["strike"]), "price": float(price)}

    def _mark(self, chain: pd.DataFrame, ts, strike: float, right: str):
        snap = chain[(chain["bucket_ts"] == ts) & (chain["strike"] == strike)]
        if snap.empty:
            return None
        price = snap.iloc[0]["call_mid" if right == "C" else "put_mid"]
        return None if pd.isna(price) else float(price)

    def _close(self, position, ts, exit_price, reason, expiry_date, trade_date):
        qty = int(self.config["contracts"]) * int(self.config["multiplier"])
        self.trade_log.append({
            "trade_date": trade_date,
            "expiry_date": expiry_date,
            "entry_time": position["entry_time"],
            "exit_time": ts,
            "side": position["side"],
            "right": position["right"],
            "strike": position["strike"],
            "entry_price": position["entry_price"],
            "exit_price": float(exit_price),
            "target": position["target"],
            "pnl": (float(exit_price) - position["entry_price"]) * qty,
            "close_reason": reason,
            "or_high": position["or_high"],
            "or_low": position["or_low"],
        })


def to_et(series: pd.Series) -> pd.Series:
    ts = pd.to_datetime(series)
    if not pd.api.types.is_datetime64tz_dtype(ts):
        ts = ts.dt.tz_localize("UTC")
    return ts.dt.tz_convert("US/Eastern")


def price_path(trade_date: str) -> Path:
    stamp = pd.Timestamp(trade_date).strftime("%Y_%m_%d")
    return DATA_PATH / f"spx_5mins_{stamp}.parquet"


def read_bars(path: Path) -> pd.DataFrame:
    df = pd.read_parquet(path)
    df.columns = [str(c).lower() for c in df.columns]
    time_col = next(c for c in ("date", "datetime", "timestamp", "time") if c in df.columns)
    out = pd.DataFrame()
    out["ts"] = to_et(df[time_col])
    for col in ("open", "high", "low", "close"):
        out[col] = df[col] if col in df.columns else df.get("spot_price", df.get("close"))
    return out.dropna(subset=["close"]).sort_values("ts")


def bars_from_chain(option_chain: pd.DataFrame) -> pd.DataFrame:
    df = option_chain.copy()
    df["ts"] = to_et(df["timestamp"])
    spot_col = "spot_price" if "spot_price" in df.columns else "strike"
    bars = df.groupby("ts", as_index=False)[spot_col].median().rename(columns={spot_col: "close"})
    bars["open"] = bars["close"]
    bars["high"] = bars["close"]
    bars["low"] = bars["close"]
    return bars


def add_ema9(bars: pd.DataFrame, span: int = 9) -> pd.DataFrame:
    out = bars.copy()
    out["ema9"] = out["close"].ewm(span=span, adjust=False).mean()
    out["ema_ready"] = out["close"].expanding().count() >= span
    return out


def opening_range(bars: pd.DataFrame, minutes: int):
    if bars.empty:
        return None, None, None
    start = bars["ts"].iloc[0]
    or_end = start + pd.Timedelta(minutes=minutes)
    window = bars[bars["ts"] < or_end]
    if window.empty:
        window = bars.iloc[:1]
        or_end = window["ts"].iloc[0]
    return float(window["high"].max()), float(window["low"].min()), or_end


def _ohlc(df: pd.DataFrame) -> dict:
    rth = df[df["ts"].dt.time.between(pd.to_datetime("09:30").time(), pd.to_datetime("16:00").time())]
    src = rth if not rth.empty else df
    return {
        "open": float(src["open"].iloc[0]),
        "high": float(src["high"].max()),
        "low": float(src["low"].min()),
        "close": float(src["close"].iloc[-1]),
    }


def load_levels(trade_date: str, data_path: Path = DATA_PATH) -> dict:
    day = pd.Timestamp(trade_date).normalize()
    dated = []
    for path in sorted(data_path.glob("spx_5mins_*.parquet")):
        try:
            dated.append((pd.to_datetime(path.stem.replace("spx_5mins_", ""), format="%Y_%m_%d"), path))
        except ValueError:
            continue
    prior = [(d, p) for d, p in dated if d.normalize() < day]
    if not prior:
        return {}

    daily = _ohlc(read_bars(prior[-1][1]))
    levels = {"PDO": daily["open"], "PDH": daily["high"], "PDL": daily["low"], "PDC": daily["close"]}

    week_start = day - pd.Timedelta(days=day.dayofweek)
    prev_week = [(d, p) for d, p in prior if d.normalize() < week_start]
    if prev_week:
        prev_start = prev_week[-1][0].normalize() - pd.Timedelta(days=prev_week[-1][0].dayofweek)
        week_files = [p for d, p in prev_week if d.normalize() >= prev_start]
        weekly = _ohlc(pd.concat([read_bars(p) for p in week_files], ignore_index=True))
        levels.update({"PWO": weekly["open"], "PWH": weekly["high"], "PWL": weekly["low"], "PWC": weekly["close"]})

    this_week = [p for d, p in dated if week_start <= d.normalize() < day]
    if this_week:
        levels["CWO"] = _ohlc(read_bars(this_week[0]))["open"]
    return levels


def nearest_level(spot: float, levels: dict, side: str, buffer: float):
    if not levels:
        return None
    if side == "long":
        above = sorted(v for v in levels.values() if v > spot + buffer)
        return above[0] if above else None
    below = sorted((v for v in levels.values() if v < spot - buffer), reverse=True)
    return below[0] if below else None


def level_blocks_entry(or_high: float, or_low: float, side: str, levels: dict, close: float, buffer: float) -> bool:
    if side == "long":
        resistance = nearest_level(or_high, levels, "long", buffer=0)
        return resistance is not None and or_low - buffer <= resistance <= or_high + buffer and close <= resistance
    support = nearest_level(or_low, levels, "short", buffer=0)
    return support is not None and or_low - buffer <= support <= or_high + buffer and close >= support


def trade_target(spot: float, side: str, levels: dict, max_points: float, buffer: float):
    level = nearest_level(spot, levels, side, buffer)
    if level is None or abs(level - spot) > max_points:
        return None
    return level


def target_hit(position: dict, bar) -> bool:
    target = position["target"]
    if target is None:
        return False
    if position["side"] == "long":
        return bar.high >= target
    return bar.low <= target


def ema_stop_hit(position: dict, bar) -> bool:
    if not bool(bar.ema_ready) or pd.isna(bar.ema9):
        if position["side"] == "long":
            return bar.low < position["or_low"]
        return bar.high > position["or_high"]
    if position["side"] == "long":
        return bar.close < bar.ema9
    return bar.close > bar.ema9