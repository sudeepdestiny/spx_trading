from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import DATA_PATH, STRATEGY_CONFIG

LEVEL_NAMES = ("PDO", "PDH", "PDL", "PDC", "PWO", "PWH", "PWL", "PWC", "CWO")


def _read_bars(path: Path) -> pd.DataFrame:
    df = pd.read_parquet(path)
    df.columns = [c.lower() for c in df.columns]
    time_col = next(c for c in ("date", "datetime", "timestamp", "time") if c in df.columns)
    df["ts"] = pd.to_datetime(df[time_col], utc=True).dt.tz_convert("US/Eastern")
    for src, dst in (("open", "open"), ("high", "high"), ("low", "low"), ("close", "close")):
        if src not in df.columns:
            df[src] = df.get("spot_price", df.get("close"))
    return df.sort_values("ts")


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
    day = pd.Timestamp(trade_date)
    files = sorted(data_path.glob("spx_5mins_*.parquet"))
    dated = []
    for path in files:
        stamp = path.stem.replace("spx_5mins_", "")
        try:
            dated.append((pd.to_datetime(stamp, format="%Y_%m_%d"), path))
        except ValueError:
            continue
    prior = [(d, p) for d, p in dated if d.normalize() < day.normalize()]
    if not prior:
        return {}
    prev_day, prev_path = prior[-1]
    daily = _ohlc(_read_bars(prev_path))
    levels = {"PDO": daily["open"], "PDH": daily["high"], "PDL": daily["low"], "PDC": daily["close"]}

    week_start = day.normalize() - pd.Timedelta(days=day.dayofweek)
    prev_week = [(d, p) for d, p in prior if d < week_start]
    if prev_week:
        start = prev_week[-1][0] - pd.Timedelta(days=prev_week[-1][0].dayofweek)
        week_files = [p for d, p in prev_week if d >= start]
        bars = pd.concat([_read_bars(p) for p in week_files], ignore_index=True)
        weekly = _ohlc(bars)
        levels.update({"PWO": weekly["open"], "PWH": weekly["high"], "PWL": weekly["low"], "PWC": weekly["close"]})

    this_week = [p for d, p in dated if week_start <= d.normalize() < day.normalize()]
    if this_week:
        levels["CWO"] = _ohlc(_read_bars(this_week[0]))["open"]
    return levels


def nearest_level(spot: float, levels: dict, side: str, buffer: float = 1.0):
    if side == "long":
        above = sorted(v for v in levels.values() if v > spot + buffer)
        return above[0] if above else None
    below = sorted((v for v in levels.values() if v < spot - buffer), reverse=True)
    return below[0] if below else None


def level_blocks_entry(or_high: float, or_low: float, side: str, levels: dict, close: float, buffer: float = 1.0) -> bool:
    """Block a break that starts inside a nearby level, unless price has closed through it."""
    if side == "long":
        resistance = nearest_level(or_high, levels, "long", buffer=0)
        if resistance is None:
            return False
        if or_low <= resistance <= or_high and close <= resistance:
            return True
        return False
    support = nearest_level(or_low, levels, "short", buffer=0)
    if support is None:
        return False
    return or_low <= support <= or_high and close >= support