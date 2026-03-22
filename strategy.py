import pandas as pd
from config import STRATEGY_CONFIG
from greeks_engine import GreeksEngine


class DeltaNeutralStrategy:
    def __init__(self, strategy_config=None):
        self.config = strategy_config or STRATEGY_CONFIG
        self.trade_log = []
        self.open_positions = []
        
    def compute_intraday_dte(self, bucket_ts):
        ts = pd.Timestamp(bucket_ts)
        market_close = ts.normalize() + pd.Timedelta(hours=16)
        remaining_seconds = max((market_close - ts).total_seconds(), 60)
        session_seconds = 6.5 * 60 * 60
        return remaining_seconds / session_seconds

    def add_deltas(self, option_chain: pd.DataFrame, spot_price: float, dte_days: float):
        df = option_chain.copy()
        t = max(dte_days / 365.0, 1e-6)

        df["call_delta"] = df["strike"].apply(
            lambda strike: GreeksEngine.delta(
                spot=spot_price,
                strike=float(strike),
                time_to_expiry=t,
                rate=self.config["risk_free_rate"],
                vol=self.config["default_iv"],
                option_type="C"
            )
        )
        df["put_delta"] = df["strike"].apply(
            lambda strike: GreeksEngine.delta(
                spot=spot_price,
                strike=float(strike),
                time_to_expiry=t,
                rate=self.config["risk_free_rate"],
                vol=self.config["default_iv"],
                option_type="P"
            )
        )
        return df

    def _find_row_by_strike(self, bucket_df: pd.DataFrame, strike: float):
        row = bucket_df[bucket_df["strike"] == strike]
        if row.empty:
            return None
        return row.iloc[0]

    def select_strikes(self, bucket_df: pd.DataFrame):
        sell_delta = self.config["sell_delta"]
        hedge_dist = self.config["hedge_dist"]

        call_df = bucket_df.dropna(subset=["call_mid", "call_delta"]).copy()
        put_df = bucket_df.dropna(subset=["put_mid", "put_delta"]).copy()

        if call_df.empty or put_df.empty:
            return None

        call_df["delta_diff"] = (call_df["call_delta"] - sell_delta).abs()
        put_df["delta_diff"] = (put_df["put_delta"].abs() - sell_delta).abs()

        short_call = call_df.sort_values("delta_diff").iloc[0]
        short_put = put_df.sort_values("delta_diff").iloc[0]

        hedge_call_strike = float(short_call["strike"]) + hedge_dist
        hedge_put_strike = float(short_put["strike"]) - hedge_dist

        hedge_call_df = bucket_df[bucket_df["strike"] == hedge_call_strike]
        hedge_put_df = bucket_df[bucket_df["strike"] == hedge_put_strike]

        if hedge_call_df.empty or hedge_put_df.empty:
            return None

        return {
            "short_call": short_call,
            "short_put": short_put,
            "hedge_call": hedge_call_df.iloc[0],
            "hedge_put": hedge_put_df.iloc[0],
        }
    def add_deltas_for_bucket(self, bucket_df: pd.DataFrame, spot_price: float, bucket_ts):
        df = bucket_df.copy()
        dte_days = self.compute_intraday_dte(bucket_ts)
        t = max(dte_days / 365.0, 1e-6)

        df["call_delta"] = df["strike"].apply(
            lambda strike: GreeksEngine.delta(
                spot=spot_price,
                strike=float(strike),
                time_to_expiry=t,
                rate=self.config["risk_free_rate"],
                vol=self.config["default_iv"],
                option_type="C"
            )
        )

        df["put_delta"] = df["strike"].apply(
            lambda strike: GreeksEngine.delta(
                spot=spot_price,
                strike=float(strike),
                time_to_expiry=t,
                rate=self.config["risk_free_rate"],
                vol=self.config["default_iv"],
                option_type="P"
            )
        )
        return df

    def open_cycle_positions(self, bucket_ts, selection, expiry_date, cycle_id):
        self.open_positions = [
            {
                "cycle_id": cycle_id,
                "entry_time": bucket_ts,
                "expiry_date": expiry_date,
                "type": "CE",
                "side": "S",
                "strike": float(selection["short_call"]["strike"]),
                "entry_price": float(selection["short_call"]["call_mid"]),
                "entry_delta": float(selection["short_call"]["call_delta"]),
                "close_price": None,
                "close_time": None,
                "close_reason": None,
                "status": "OPEN",
            },
            {
                "cycle_id": cycle_id,
                "entry_time": bucket_ts,
                "expiry_date": expiry_date,
                "type": "PE",
                "side": "S",
                "strike": float(selection["short_put"]["strike"]),
                "entry_price": float(selection["short_put"]["put_mid"]),
                "entry_delta": float(selection["short_put"]["put_delta"]),
                "close_price": None,
                "close_time": None,
                "close_reason": None,
                "status": "OPEN",
            },
            {
                "cycle_id": cycle_id,
                "entry_time": bucket_ts,
                "expiry_date": expiry_date,
                "type": "CE",
                "side": "B",
                "strike": float(selection["hedge_call"]["strike"]),
                "entry_price": float(selection["hedge_call"]["call_mid"]),
                "entry_delta": float(selection["hedge_call"]["call_delta"]),
                "close_price": None,
                "close_time": None,
                "close_reason": None,
                "status": "OPEN",
            },
            {
                "cycle_id": cycle_id,
                "entry_time": bucket_ts,
                "expiry_date": expiry_date,
                "type": "PE",
                "side": "B",
                "strike": float(selection["hedge_put"]["strike"]),
                "entry_price": float(selection["hedge_put"]["put_mid"]),
                "entry_delta": float(selection["hedge_put"]["put_delta"]),
                "close_price": None,
                "close_time": None,
                "close_reason": None,
                "status": "OPEN",
            },
        ]

    def calculate_position_delta(self, bucket_df: pd.DataFrame):
        total_delta = 0.0
        for pos in self.open_positions:
            if pos["status"] != "OPEN":
                continue
            row = self._find_row_by_strike(bucket_df, pos["strike"])
            if row is None:
                continue
            delta = row["call_delta"] if pos["type"] == "CE" else row["put_delta"]
            total_delta += -delta if pos["side"] == "S" else delta
        return total_delta

    def calculate_current_pnl(self, bucket_df: pd.DataFrame):
        pnl = 0.0
        lot_size = self.config["lot_size"]

        for pos in self.open_positions:
            if pos["status"] != "OPEN":
                continue
            row = self._find_row_by_strike(bucket_df, pos["strike"])
            if row is None:
                continue

            current_price = row["call_mid"] if pos["type"] == "CE" else row["put_mid"]
            if pd.isna(current_price):
                continue

            pnl += (pos["entry_price"] - current_price) * lot_size if pos["side"] == "S" else (current_price - pos["entry_price"]) * lot_size

        return pnl

    def initial_net_credit(self):
        if not self.open_positions:
            return 0.0

        total = 0.0
        for pos in self.open_positions:
            total += pos["entry_price"] if pos["side"] == "S" else -pos["entry_price"]
        return total

    def close_all_positions(self, bucket_ts, bucket_df: pd.DataFrame, reason: str, highest_pnl: float, lowest_pnl: float):
        lot_size = self.config["lot_size"]

        for pos in self.open_positions:
            if pos["status"] != "OPEN":
                continue

            row = self._find_row_by_strike(bucket_df, pos["strike"])
            if row is None:
                close_price=0.05
            else:
                close_price = row["call_mid"] if pos["type"] == "CE" else row["put_mid"]
                if pd.isna(close_price):
                    continue

            pos["close_price"] = float(close_price)
            pos["close_time"] = bucket_ts
            pos["close_reason"] = reason
            pos["status"] = "CLOSED"
            pos["highest_pnl_cycle"] = highest_pnl
            pos["lowest_pnl_cycle"] = lowest_pnl
            pos["pnl"] = ((pos["entry_price"] - close_price) * lot_size) if pos["side"] == "S" else ((close_price - pos["entry_price"]) * lot_size)
            self.trade_log.append(pos.copy())

        self.open_positions = []

    def run_backtest(self, option_chain: pd.DataFrame, spot_price: float, trade_date=None, dte_days: float = 1.0):
        if option_chain.empty:
            return pd.DataFrame(), {"status": "empty_chain"}

        df = option_chain.copy()
        df = df.sort_values("bucket_ts").reset_index(drop=True)
        grouped = list(df.groupby("bucket_ts"))

        cycle_id = 0
        adjustments = 0
        highest_pnl = 0.0
        lowest_pnl = 0.0

        max_adjusts = self.config["max_adjusts"]
        pos_delta_thresh = self.config["pos_delta_thresh"]
        closing_diff = self.config.get("closing_diff", 0)

        expiry_date = str(trade_date) if trade_date is not None else "same_day"

        for idx, (bucket_ts, bucket_df) in enumerate(grouped):
            bucket_df = self.add_deltas_for_bucket(bucket_df, spot_price=spot_price, bucket_ts=bucket_ts)

            if not self.open_positions:
                selection = self.select_strikes(bucket_df)
                if selection is None:
                    continue

                cycle_id += 1
                self.open_cycle_positions(bucket_ts, selection, expiry_date, cycle_id)
                highest_pnl = 0.0
                lowest_pnl = 0.0
                adjustments = 0
                continue

            current_delta = self.calculate_position_delta(bucket_df)
            current_pnl = self.calculate_current_pnl(bucket_df)

            highest_pnl = max(highest_pnl, current_pnl)
            lowest_pnl = min(lowest_pnl, current_pnl)

            net_credit = self.initial_net_credit()
            lower_breakeven = spot_price - net_credit
            upper_breakeven = spot_price + net_credit

            current_spot_proxy = float(bucket_df["strike"].median())
            outside_breakeven = current_spot_proxy < lower_breakeven or current_spot_proxy > upper_breakeven

            last_bucket = idx == len(grouped) - 1

            if last_bucket:
                self.close_all_positions(bucket_ts, bucket_df, "expiry_close", highest_pnl, lowest_pnl)
                continue

            if abs(current_delta) > pos_delta_thresh and adjustments < max_adjusts and outside_breakeven:
                self.close_all_positions(bucket_ts, bucket_df, "delta_adjustment", highest_pnl, lowest_pnl)
                selection = self.select_strikes(bucket_df)
                if selection is not None:
                    cycle_id += 1
                    self.open_cycle_positions(bucket_ts, selection, expiry_date, cycle_id)
                    adjustments += 1
                    highest_pnl = 0.0
                    lowest_pnl = 0.0
                    continue

            if closing_diff and abs(current_pnl) >= abs(closing_diff):
                self.close_all_positions(bucket_ts, bucket_df, "pnl_close", highest_pnl, lowest_pnl)

        result_df = pd.DataFrame(self.trade_log)
        summary = {
            "status": "ok",
            "trade_date": str(trade_date) if trade_date is not None else None,
            "trade_count": len(result_df),
            "cycle_count": result_df["cycle_id"].nunique() if not result_df.empty else 0,
            "total_pnl": float(result_df["pnl"].sum()) if not result_df.empty else 0.0,
            "max_profit_seen": highest_pnl,
            "max_loss_seen": lowest_pnl,
        }
        return result_df, summary
