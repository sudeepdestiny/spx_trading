import pandas as pd
import logging
import sys
from pathlib import Path

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Add both current directory and parent directory to path
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))


from config import STRATEGY_CONFIG
from greeks_engine import GreeksEngine
import logging

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
        """Select short strikes with delta >= sell_delta (closest match), then hedges."""
        sell_delta = self.config["sell_delta"]
        hedge_dist = self.config["hedge_dist"]
        
        
        # bucket_df = bucket_df.rename(columns={"call_mid":"bs_call_price", "put_mid":"bs_put_price"})

        call_df = bucket_df.dropna(subset=["call_mid", "call_delta"]).copy()
        put_df = bucket_df.dropna(subset=["put_mid", "put_delta"]).copy()
        
        ##sort putdf with delta ascending (most negative first) to ensure we pick the most negative delta put for the same strike as call
        # put_df = put_df.sort_values(by="put_delta", ascending=True)

        if call_df.empty or put_df.empty:
            logging.info(f"No valid calls ({len(call_df)}) or puts ({len(put_df)})")
            return None

        # Filter calls/puts where delta >= sell_delta, then pick closest
        viable_calls = call_df[call_df["call_delta"] >= sell_delta]
        viable_puts = put_df[put_df["put_delta"].abs() >= sell_delta]

        if viable_calls.empty or viable_puts.empty:
            logging.info(f"No viable calls δ>={sell_delta} ({len(viable_calls)}) or puts ({len(viable_puts)})")
            return None

        # Closest = smallest excess delta (guaranteed >= target)
        short_call = viable_calls.loc[viable_calls["call_delta"].idxmin()]
        short_put = viable_puts.loc[viable_puts["put_delta"].abs().idxmin()]

        hedge_call_strike = float(short_call["strike"]) + hedge_dist
        hedge_put_strike = float(short_put["strike"]) - hedge_dist

        hedge_call_df = bucket_df[bucket_df["strike"] == hedge_call_strike]
        hedge_put_df = bucket_df[bucket_df["strike"] == hedge_put_strike]

        if hedge_call_df.empty or hedge_put_df.empty:
            logging.info(f"No hedges at {hedge_call_strike:.1f}/{hedge_put_strike:.1f}")
            return None

        selection = {
            "short_call": short_call,
            "short_put": short_put,
            "hedge_call": hedge_call_df.iloc[0],
            "hedge_put": hedge_put_df.iloc[0],
        }

        logging.info(f"Selected short_call δ={short_call['call_delta']:.3f}@{short_call['strike']:.1f}, "
            f"short_put δ={short_put['put_delta']:.3f}@{short_put['strike']:.1f}")

        return selection

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
    def filter_main_strike_cluster(self, bucket_df: pd.DataFrame, min_density=0.1):
        """Filter to densest strike cluster (handles outliers like 35/36 vs 6570-7200)."""
        strikes = bucket_df["strike"].sort_values().values
        
        # Find gaps > 2% of median spacing
        median_spacing = pd.Series(strikes[1:] - strikes[:-1]).median()
        gap_threshold = median_spacing * 3  # 3x median gap = cluster break
        
        # Find cluster breaks
        gaps = strikes[1:] - strikes[:-1]
        breaks = gaps > gap_threshold
        
        # Split into clusters and pick largest
        cluster_sizes = []
        starts = [0]
        for i, brk in enumerate(breaks):
            if brk:
                cluster_sizes.append(i + 1 - starts[-1])
                starts.append(i + 1)
        cluster_sizes.append(len(strikes) - starts[-1])
        
        main_cluster_idx = cluster_sizes.index(max(cluster_sizes))
        main_start = starts[main_cluster_idx]
        main_end = main_start + cluster_sizes[main_cluster_idx]
        
        main_min, main_max = strikes[main_start], strikes[main_end - 1]
        
        filtered_df = bucket_df[
            (bucket_df["strike"] >= main_min) & 
            (bucket_df["strike"] <= main_max)
        ]
        
        # logging.info(f"Main cluster [{main_min:.0f}, {main_max:.0f}] ({len(filtered_df)}/{len(bucket_df)} strikes)")
        return filtered_df
    
    def get_synthetic_spot(self, bucket_df: pd.DataFrame):
        """Estimate spot as strike with minimal |call_mid - put_mid| difference."""
        valid_strikes = bucket_df.dropna(subset=["call_mid", "put_mid"])
        
        if valid_strikes.empty:
            return float(bucket_df["strike"].median())
        
        # Find strike with smallest call-put price difference (closest to ATM)
        valid_strikes["price_diff"] = abs(valid_strikes["call_mid"] - valid_strikes["put_mid"])
        atm_strike = valid_strikes.loc[valid_strikes["price_diff"].idxmin(), "strike"]
        
        # logging.info(f"Synthetic spot: {atm_strike:.2f} (min call-put diff: {valid_strikes['price_diff'].min():.2f})")
        return float(atm_strike)
    
    def find_profitable_hedge_leg(self, bucket_df, current_delta):
        """Find hedge leg with highest PnL (profitable offset)."""
        best_hedge = None
        best_pnl = float('-inf')
        
        for pos in self.open_positions:
            if pos["side"] != "B" or pos["status"] != "OPEN":  # Only long hedges
                continue
                
            row = self._find_row_by_strike(bucket_df, pos["strike"])
            if row is None:
                continue
                
            # Current PnL = entry_price - current_price (for long)
            entry_price = pos["entry_price"]
            current_price = row["call_mid" if pos["type"] == "CE" else "put_mid"]
            pnl = entry_price - current_price  # Positive = profitable
            
            if pnl > best_pnl:
                best_pnl = pnl
                best_hedge = pos
        
        return best_hedge if best_pnl > 0 else None
    
    def select_delta_adjust_short(self, bucket_df, positive_delta_needed, short_type):
        """Select new short leg to offset delta drift."""
        sell_delta_target = self.config["sell_delta"]
        
        if short_type == "CE":
            viable_shorts = bucket_df[
                (bucket_df["call_delta"] >= sell_delta_target) &
                bucket_df["call_mid"].notna()
            ]
            if viable_shorts.empty:
                return None
            # Pick closest to target (smallest excess delta)
            return viable_shorts.loc[(viable_shorts["call_delta"] - sell_delta_target).idxmin()]
        
        else:  # PE
            viable_shorts = bucket_df[
                (bucket_df["put_delta"].abs() >= sell_delta_target) &
                bucket_df["put_mid"].notna()
            ]
            if viable_shorts.empty:
                return None
            return viable_shorts.loc[(bucket_df["put_delta"].abs() - sell_delta_target).idxmin()]
    def close_single_position(self, bucket_ts,cycle_id, strike, reason):
        """Close specific position and log PnL."""
        for pos in self.open_positions[:]:
            if (pos["cycle_id"] == cycle_id and 
                pos["strike"] == strike and 
                pos["status"] == "OPEN"):
                
                # Find current price for PnL calc
                # (implement using bucket_df as in previous example)
                
                pos["status"] = "CLOSED"
                pos["close_reason"] = reason
                pos["close_time"] = bucket_ts  # Pass bucket_ts
                self.trade_log.append(pos.copy())
                self.open_positions.remove(pos)
                break
    def open_single_position(self, bucket_ts, leg_data, expiry_date, cycle_id, side, type_):
        """Open single leg."""
        price_col = "call_mid" if type_ == "CE" else "put_mid"
        delta_col = "call_delta" if type_ == "CE" else "put_delta"
        
        pos = {
            "cycle_id": cycle_id,
            "entry_time": bucket_ts,
            "expiry_date": expiry_date,
            "type": type_,
            "side": side,
            "strike": float(leg_data["strike"]),
            "entry_price": float(leg_data[price_col]),
            "entry_delta": float(leg_data[delta_col]),
            "close_price": None,
            "close_time": None,
            "close_reason": None,
            "status": "OPEN",
        }
        self.open_positions.append(pos)
    def find_matching_hedge(self, short_pos):
        """Find hedge position matching short (usually hedge_dist away)."""
        hedge_dist = self.config["hedge_dist"]
        short_strike = short_pos["strike"]
        short_type = short_pos["type"]
        
        # Hedge is typically hedge_dist away in same direction
        hedge_strike = short_strike + hedge_dist if short_type == "CE" else short_strike - hedge_dist
        
        for pos in self.open_positions:
            if (pos["strike"] == hedge_strike and 
                pos["type"] == short_type and 
                pos["side"] == "B" and  # Long hedge
                pos["status"] == "OPEN"):
                return pos
        return None
    
    def calc_leg_pnl(self, pos, bucket_df):
        """Calculate current PnL for single leg."""
        row = self._find_row_by_strike(bucket_df, pos["strike"])
        if row is None:
            return 0.0
        
        price_col = "call_mid" if pos["type"] == "CE" else "put_mid"
        entry_price = pos["entry_price"]
        current_price = row[price_col]
        
        if pos["side"] == "S":  # Short: profit = entry - current
            return entry_price - current_price
        else:  # Long: profit = current - entry
            return current_price - entry_price


        
    def find_profitable_short_leg(self, bucket_df):
        """Find short leg pair with highest net PnL."""
        best_short = None
        best_net_pnl = float('-inf')
        
        # Group shorts + their hedges by strike/type
        for short_pos in [p for p in self.open_positions if p["side"] == "S" and p["status"] == "OPEN"]:
            leg_strike = short_pos["strike"]
            leg_type = short_pos["type"]
            
            # Find matching hedge (usually hedge_dist away)
            hedge_pos = self.find_matching_hedge(short_pos)
            if not hedge_pos:
                continue
                
            # Net PnL: short PnL (negative if profitable) + hedge PnL (positive if profitable)
            short_pnl = self.calc_leg_pnl(short_pos, bucket_df)  # negative for short profit
            hedge_pnl = self.calc_leg_pnl(hedge_pos, bucket_df)   # positive for long profit
            
            net_pnl = abs(short_pnl) + hedge_pnl  # Total profit locked
            
            if net_pnl > best_net_pnl:
                best_net_pnl = net_pnl
                best_short = short_pos
        
        return best_short
    def close_leg_pair(self,bucket_df, bucket_ts,short_strike, short_type, reason):
        """Close short + matching hedge for given leg."""
        strikes_to_close = [short_strike]
        lot_size = self.config["lot_size"]
        
        # Find hedge strike
        hedge_strike = short_strike + self.config["hedge_dist"] * (-1 if short_type == "PE" else 1)
        strikes_to_close.append(hedge_strike)
        
        for pos in self.open_positions[:]:
            if pos["strike"] in strikes_to_close and pos["status"] == "OPEN":
                
                row = self._find_row_by_strike(bucket_df, pos["strike"])
                if row is None:
                    close_price=0.05
                else:
                    close_price = row["call_mid"] if short_type == "CE" else row["put_mid"]
                    if pd.isna(close_price):
                        continue

                pos["close_price"] = float(close_price)
                pos["pnl"] = ((pos["entry_price"] - close_price) * lot_size) if pos["side"] == "S" else ((close_price - pos["entry_price"]) * lot_size)

                pos["close_reason"] = reason
                pos["close_time"] = bucket_ts
                pos["status"] = "CLOSED"
                self.trade_log.append(pos.copy())
                self.open_positions.remove(pos)

    def select_adjustment_short(self, bucket_df: pd.DataFrame, hedge_type: str,current_delta: float):
        """Select replacement short leg after closing hedge (target sell_delta range)."""
        sell_delta = self.config["sell_delta"]
        current_delta=abs(current_delta)
        adjust_delta_range = self.config.get("adjust_delta_range", [sell_delta * 0.8, sell_delta * 1.2])
        
        if hedge_type == "CE":
            # Calls with delta in target range
            viable_shorts = bucket_df[
                (bucket_df["call_delta"] >= current_delta ) &
                bucket_df["call_mid"].notna()
            ]
            if viable_shorts.empty:
                return None
            # Pick closest to exact sell_delta
            # Closest = smallest excess delta (guaranteed >= target)
        # short_call = viable_calls.loc[viable_calls["call_delta"].idxmin()]
        # short_put = viable_puts.loc[viable_puts["put_delta"].abs().idxmin()]
            return viable_shorts.loc[viable_shorts["call_delta"].idxmin()]
        
        else:  # PE
            viable_shorts = bucket_df[
                (bucket_df["put_delta"].abs() >= current_delta) &
                bucket_df["put_mid"].notna()
            ]
            if viable_shorts.empty:
                return None
            return viable_shorts.loc[viable_shorts["put_delta"].idxmin()]
    def find_imbalanced_short_leg(self, bucket_df, current_delta):
        """Target short leg on imbalanced side using existing current_delta.
        CE for neg delta, PE for pos delta. Largest contributor.
        """
        if not self.open_positions or abs(current_delta) < 0.01:
            return None
        
        target_side = 'CE' if current_delta > 0 else 'PE'
        
        candidates = []
        for pos in self.open_positions:
            if (pos['status'] == 'OPEN' and pos['side'] == 'S' and pos['type'] == target_side):
                return pos


    def run_backtest(self, option_chain: pd.DataFrame, trade_date=None, dte_days: int = 1,expiry_date=None):
       
        
        if option_chain.empty:
            return pd.DataFrame(), {"status": "empty_chain"}

        df = option_chain.copy()      

        df["bucket_ts"] = pd.to_datetime(df["timestamp"])
        df = df.sort_values("bucket_ts").reset_index(drop=True)
        # Ensure timestamps are timezone-aware before converting to Eastern
        if not pd.api.types.is_datetime64tz_dtype(df["bucket_ts"]):
            df["bucket_ts"] = df["bucket_ts"].dt.tz_localize("UTC")
        # Convert tz-aware UTC timestamps to Eastern and filter RTH only
        df["bucket_ts_et"] = df["bucket_ts"].dt.tz_convert("US/Eastern")
        
        df = df.set_index("bucket_ts_et")
        df = df.between_time("10:00", "15:45").reset_index()

        if df.empty:
            return pd.DataFrame(), {"status": "no_rth_data"}

        grouped = list(df.groupby("bucket_ts_et"))

        cycle_id = 0
        adjustments = 0
        highest_pnl = 0.0
        lowest_pnl = 0.0
        last_bucket = None

        for idx, (bucket_ts, bucket_df) in enumerate(grouped):
            last_bucket = (bucket_ts, bucket_df)

            if "spot_price" in bucket_df.columns and bucket_df["spot_price"].notna().any():
                current_spot = float(bucket_df["spot_price"].dropna().iloc[0])
            else:
                current_spot = float(bucket_df["strike"].median())
            
            # Replace the spot extraction block with:
            
             # In run_backtest loop or select_strikes
            bucket_df = self.filter_main_strike_cluster(bucket_df)
        
            # current_spot = self.get_synthetic_spot(bucket_df)

            # bucket_df = self.add_deltas_for_bucket(
            #     bucket_df,
            #     spot_price=current_spot,
            #     bucket_ts=bucket_ts
            # )

            if not self.open_positions:
                selection = self.select_strikes(bucket_df)
                if selection is None:
                    continue

                cycle_id += 1
                ##add dte_days to trade_date and convert to string for logging
                # expiry_date=pd.to_datetime(trade_date) + pd.Timedelta(days=dte_days) if trade_date is not None else "same_day"
                
                self.open_cycle_positions(bucket_ts, selection, expiry_date, cycle_id)
                adjustments = 0
                highest_pnl = 0.0
                lowest_pnl = 0.0
                
                net_credit = self.initial_net_credit()
                lower_breakeven = current_spot - net_credit
                upper_breakeven = current_spot + net_credit
                
                continue

            current_delta = self.calculate_position_delta(bucket_df)
            current_pnl = self.calculate_current_pnl(bucket_df)

            highest_pnl = max(highest_pnl, current_pnl)
            lowest_pnl = min(lowest_pnl, current_pnl)

            
            outside_breakeven = current_spot < lower_breakeven or current_spot > upper_breakeven

            max_adjusts = self.config["max_adjusts"]
            pos_delta_thresh = self.config["pos_delta_thresh"]
            closing_diff = self.config.get("closing_diff", 0)

            is_last_bucket = idx == len(grouped) - 1
            if is_last_bucket:
                self.close_all_positions(
                    bucket_ts,
                    bucket_df,
                    "expiry_close",
                    highest_pnl,
                    lowest_pnl
                )
                continue

            if abs(current_delta) > pos_delta_thresh and adjustments < max_adjusts:
                
                # # Roll entire profitable short leg (short + hedge)
                # profitable_short_leg = self.find_profitable_short_leg(bucket_df)
                
                # if profitable_short_leg is None:
                #     continue
                
                # short_strike = profitable_short_leg["strike"]
                # short_type = profitable_short_leg["type"]  # CE or PE
                
                imbalanced_short_leg = self.find_imbalanced_short_leg(bucket_df, current_delta)
                if imbalanced_short_leg is None:
                    continue
                
                short_strike = imbalanced_short_leg['strike']
                short_type = 'CE' if current_delta > 0 else 'PE'
                
                # Close BOTH: short + matching hedge
                self.close_leg_pair(bucket_df,bucket_ts,short_strike, short_type, "roll_profitable")
                
                # Add NEW short leg to rebalance
                new_short = self.select_adjustment_short(bucket_df, short_type,current_delta)
                
                ## get the open position from bucket_df and where the stiketype != short_type
                for pos in self.open_positions:
                    if (pos['status'] == 'OPEN' and pos['side'] == 'S' and pos['type'] != short_type):
                        open_strike = pos['strike']
                        open_type = pos['type']
                        
                if new_short is None:
                    continue
                ##close all the positions if new short is greater than other side
                
                elif (new_short["strike"] > open_strike and open_type == "CE") or (new_short["strike"] < open_strike and open_type == "PE"):
                    # Close BOTH: short + matching hedge
                    self.close_leg_pair(bucket_df,bucket_ts,open_strike, open_type, "Close as strike imbalance")
                    break
                
                # expiry_date = str(trade_date) if trade_date else "same_day"
                cycle_id += 1
                
                self.open_single_position(
                    bucket_ts, new_short, expiry_date, cycle_id, "S", short_type
                )
                # Auto-add hedge for new short (same logic as initial)
                hedge_strike = float(new_short["strike"]) + self.config["hedge_dist"] * (-1 if short_type == "PE" else 1)
                hedge_leg = self._find_row_by_strike(bucket_df, hedge_strike)
                if hedge_leg is not None:
                    self.open_single_position(
                        bucket_ts, hedge_leg, expiry_date, cycle_id, "B", short_type
                    )
                
                logging.info(f"Rolled {short_type} leg: closed {short_strike}, opened {new_short['strike']:.0f}")
                adjustments += 1
                continue


            # if abs(current_pnl) > abs(closing_diff):
            #     self.close_all_positions(
            #         bucket_ts,
            #         bucket_df,
            #         "pnl_close",
            #         highest_pnl,
            #         lowest_pnl
            #     )

        result_df = pd.DataFrame(self.trade_log)
        
        
        summary = {
            "status": "ok",
            "trade_count": len(result_df),
            "cycle_count": result_df["cycle_id"].nunique() if not result_df.empty else 0,
            "total_pnl": float(result_df["pnl"].sum()) if not result_df.empty else 0.0,
            "max_profit_seen": highest_pnl,
            "max_loss_seen": lowest_pnl,
        }
        return result_df, summary
