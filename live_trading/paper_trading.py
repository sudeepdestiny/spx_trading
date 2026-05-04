import logging
import sys
import pandas as pd
import threading
import time as time_module
from pathlib import Path
from datetime import datetime

logger = logging.getLogger(__name__)

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "backtest"))

from strategy import DeltaNeutralStrategy


class PaperTrader:
    """Paper trading engine using live IBKR data"""
    
    def __init__(self, conn_mgr=None, is_paper=True):
        """
        Initialize paper trader
        
        Args:
            conn_mgr: IBKRConnectionManager instance (required)
            is_paper: Boolean, True for paper trading, False for live
        
        Raises:
            ValueError: If conn_mgr is not provided
        """
        if conn_mgr is None:
            raise ValueError("conn_mgr (IBKRConnectionManager) is required")
        
        self.conn_mgr = conn_mgr
        self.is_paper = is_paper
        self.strategy = DeltaNeutralStrategy()
        self.live_positions = {}  # Active positions: {cycle_id: position_data}
        self.trade_log = []
        self.cycle_id = 0
        self.next_req_id = 50000
        
        logger.info(f"✓ PaperTrader initialized (mode: {'PAPER' if is_paper else 'LIVE'})")
    
    
    def get_live_option_chain(self, underlying="SPX", dte_days=5):
        """
        Fetch live option chain from IBKR
        
        Args:
            underlying: Underlying symbol (e.g., "SPX")
            strike_range: Ignored; strikes are derived as ±15 strikes from spot
            dte_days: Target calendar days to expiry
        
        Returns:
            DataFrame with option prices and greeks
        """
        try:
            if not self.conn_mgr.is_connected:
                logger.error("Not connected to IBKR")
                return pd.DataFrame()
            
            logger.info(f"Fetching live {underlying} option chain ({dte_days} DTE, spot ±15 strikes)...")

            from ibapi.contract import Contract

            client = self.conn_mgr.get_client()
            wrapper = self.conn_mgr.get_wrapper()
            if client is None or wrapper is None:
                logger.error("IBKR client/wrapper unavailable")
                return pd.DataFrame()

            client.reqMarketDataType(3)

            if not hasattr(wrapper, "market_data_lock"):
                wrapper.market_data_lock = threading.Lock()
            wrapper.live_prices = {}
            wrapper.price_events = {}
            wrapper.greeks = {}

            def tick_price(reqId, tickType, price, attrib):
                if price is None or price < 0:
                    return
                tick_names = {
                    1: "bid", 2: "ask", 4: "last", 6: "high", 7: "low", 9: "close",
                    37: "mark", 66: "bid", 67: "ask", 68: "last", 72: "high",
                    73: "low", 75: "close", 76: "open",
                }
                tick_name = tick_names.get(tickType)
                if tick_name:
                    with wrapper.market_data_lock:
                        wrapper.live_prices.setdefault(reqId, {})[tick_name] = price
                        event = wrapper.price_events.get(reqId)
                    if event:
                        event.set()

            wrapper.tickPrice = tick_price

            def clean_float(value):
                if value is None or value < -1e100 or value > 1e100:
                    return None
                return float(value)

            def tick_option_computation(reqId, tickType, tickAttrib, impliedVol, delta, optPrice, pvDividend, gamma, vega, theta, undPrice):
                greek_values = {
                    "implied_vol": clean_float(impliedVol),
                    "delta": clean_float(delta),
                    "opt_price": clean_float(optPrice),
                    "pv_dividend": clean_float(pvDividend),
                    "gamma": clean_float(gamma),
                    "vega": clean_float(vega),
                    "theta": clean_float(theta),
                    "und_price": clean_float(undPrice),
                }
                with wrapper.market_data_lock:
                    wrapper.greeks.setdefault(reqId, {})[tickType] = greek_values
                    event = wrapper.price_events.get(reqId)
                if event:
                    event.set()

            wrapper.tickOptionComputation = tick_option_computation

            def best_market_price(ticks):
                for key in ("last", "mark", "close"):
                    price = ticks.get(key)
                    if price is not None and price > 0:
                        return price
                bid = ticks.get("bid")
                ask = ticks.get("ask")
                if bid is not None and ask is not None and bid > 0 and ask > 0:
                    return (bid + ask) / 2
                for key in ("bid", "ask", "high", "low", "open"):
                    price = ticks.get(key)
                    if price is not None and price > 0:
                        return price
                return None

            def best_greek_model(greeks):
                return greeks.get(13) or greeks.get(83)

            def fetch_spot_from_history(contract):
                req_id = self.next_req_id
                self.next_req_id += 1
                wrapper.hist_data = []
                wrapper.done_event = threading.Event()

                client.reqHistoricalData(
                    reqId=req_id,
                    contract=contract,
                    endDateTime="",
                    durationStr="1 D",
                    barSizeSetting="1 min",
                    whatToShow="TRADES",
                    useRTH=0,
                    formatDate=1,
                    keepUpToDate=0,
                    chartOptions=[]
                )

                if not wrapper.done_event.wait(timeout=10):
                    client.cancelHistoricalData(req_id)
                    return None

                if not wrapper.hist_data:
                    return None

                hist_df = pd.DataFrame(wrapper.hist_data)
                closes = pd.to_numeric(hist_df["Close"], errors="coerce").dropna()
                return float(closes.iloc[-1]) if not closes.empty else None

            now_et = pd.Timestamp.now(tz="US/Eastern")
            expiry_ts = now_et.normalize() + pd.Timedelta(days=dte_days, hours=16)
            while expiry_ts.weekday() >= 5:
                expiry_ts += pd.Timedelta(days=1)
            expiry = expiry_ts.strftime("%Y%m%d")
            effective_dte_days = max((expiry_ts - now_et).total_seconds() / 86400, 1 / 365)
            option_trading_class = "SPXW" if underlying.upper() == "SPX" else underlying
            option_exchange = "SMART" if underlying.upper() == "SPX" else "SMART"

            spot_contract = Contract()
            spot_contract.symbol = underlying
            spot_contract.secType = "IND"
            spot_contract.exchange = "CBOE"
            spot_contract.currency = "USD"

            spot_req_id = self.next_req_id
            self.next_req_id += 1
            wrapper.price_events[spot_req_id] = threading.Event()
            client.reqMktData(spot_req_id, spot_contract, "", False, False, [])
            wrapper.price_events[spot_req_id].wait(timeout=6.0)

            with wrapper.market_data_lock:
                spot_ticks = wrapper.live_prices.get(spot_req_id, {})
            client.cancelMktData(spot_req_id)
            wrapper.price_events.pop(spot_req_id, None)

            spot_price = best_market_price(spot_ticks)
            if not spot_price:
                logger.warning(f"No {underlying} spot tick received; trying historical fallback")
                spot_price = fetch_spot_from_history(spot_contract)
                if not spot_price:
                    logger.error(f"Failed to fetch {underlying} spot price")
                    return pd.DataFrame()

            strike_step = 5
            strike_count = 45
            atm_strike = int(round(float(spot_price) / strike_step) * strike_step)
            low = atm_strike - (strike_count * strike_step)
            high = atm_strike + (strike_count * strike_step)
            strike_range = (low, high)
            strikes = list(range(int(low), int(high) + 1, 5))
            logger.info(f"Using strike range {low}-{high} from spot={spot_price:.2f}, atm={atm_strike}")
            option_requests = {}
            option_contracts = []

            for strike in strikes:
                for right in ("C", "P"):
                    contract = Contract()
                    contract.symbol = underlying
                    contract.secType = "OPT"
                    contract.exchange = option_exchange
                    contract.currency = "USD"
                    contract.lastTradeDateOrContractMonth = expiry
                    contract.strike = float(strike)
                    contract.right = right
                    contract.multiplier = "100"
                    contract.tradingClass = option_trading_class

                    req_id = self.next_req_id
                    self.next_req_id += 1
                    option_requests[req_id] = (float(strike), right)
                    option_contracts.append((req_id, contract))

            max_market_data_lines = 90
            for chunk_start in range(0, len(option_contracts), max_market_data_lines):
                chunk = option_contracts[chunk_start:chunk_start + max_market_data_lines]
                logger.info(f"Requesting option market data batch {chunk_start // max_market_data_lines + 1}: {len(chunk)} contracts")

                for req_id, contract in chunk:
                    wrapper.price_events[req_id] = threading.Event()
                    client.reqMktData(req_id, contract, "", False, False, [])

                deadline = time_module.monotonic() + 6.0
                chunk_req_ids = [req_id for req_id, _ in chunk]
                while time_module.monotonic() < deadline:
                    with wrapper.market_data_lock:
                        ready = sum(
                            1 for req_id in chunk_req_ids
                            if best_market_price(wrapper.live_prices.get(req_id, {})) or best_greek_model(wrapper.greeks.get(req_id, {}))
                        )
                    if ready >= len(chunk_req_ids):
                        break
                    time_module.sleep(0.1)

                for req_id in chunk_req_ids:
                    client.cancelMktData(req_id)
                    wrapper.price_events.pop(req_id, None)

                time_module.sleep(0.25)

            logger.info(
                "Finished option market data batches: "
                f"{len(option_contracts)} contracts requested with max {max_market_data_lines} simultaneous lines"
            )

            with wrapper.market_data_lock:
                missing_count = sum(
                    1 for req_id in option_requests
                    if not best_market_price(wrapper.live_prices.get(req_id, {})) and not best_greek_model(wrapper.greeks.get(req_id, {}))
                    )
            if missing_count:
                logger.warning(f"No option price/greeks received for {missing_count} contracts")

            rows = {}
            with wrapper.market_data_lock:
                for req_id, (strike, right) in option_requests.items():
                    ticks = wrapper.live_prices.get(req_id, {})
                    model_greeks = best_greek_model(wrapper.greeks.get(req_id, {}))
                    bid = ticks.get("bid")
                    ask = ticks.get("ask")
                    last = best_market_price(ticks)
                    if bid is not None and ask is not None and bid > 0 and ask > 0:
                        mid = (bid + ask) / 2
                    else:
                        mid = last

                    row = rows.setdefault(strike, {
                        "underlying": underlying,
                        "expiry": expiry,
                        "dte_days": effective_dte_days,
                        "strike": strike,
                        "spot_price": spot_price,
                    })
                    prefix = "call" if right == "C" else "put"
                    row[f"{prefix}_bid"] = bid
                    row[f"{prefix}_ask"] = ask
                    row[f"{prefix}_mid"] = mid
                    if model_greeks:
                        row[f"{prefix}_delta_ibkr"] = model_greeks["delta"]
                        row[f"{prefix}_gamma"] = model_greeks["gamma"]
                        row[f"{prefix}_theta"] = model_greeks["theta"]
                        row[f"{prefix}_vega"] = model_greeks["vega"]
                        row[f"{prefix}_iv"] = model_greeks["implied_vol"]
                        row[f"{prefix}_model_price"] = model_greeks["opt_price"]
                        row[f"{prefix}_underlying_price"] = model_greeks["und_price"]

            df = pd.DataFrame(rows.values()).sort_values("strike").reset_index(drop=True)
            if df.empty:
                logger.warning("No option market data returned")
                return df

            df["timestamp"] = pd.Timestamp.now()
            df = self.strategy.add_deltas(df, float(spot_price), dte_days=effective_dte_days)
            if "call_delta_ibkr" in df.columns:
                df["call_delta"] = df["call_delta_ibkr"].combine_first(df["call_delta"])
            if "put_delta_ibkr" in df.columns:
                df["put_delta"] = df["put_delta_ibkr"].combine_first(df["put_delta"])
            logger.info(f"✓ Live option chain fetched: {len(df)} strikes, spot={spot_price:.2f}, expiry={expiry}, dte={effective_dte_days:.2f}")
            return df
        
        except Exception as e:
            logger.error(f"Error fetching option chain: {e}", exc_info=True)
            return pd.DataFrame()

    def _build_option_contract(self, underlying, expiry, strike, right):
        from ibapi.contract import Contract

        contract = Contract()
        contract.symbol = underlying
        contract.secType = "OPT"
        contract.exchange = "SMART"
        contract.currency = "USD"
        contract.lastTradeDateOrContractMonth = expiry
        contract.strike = float(strike)
        contract.right = right
        contract.multiplier = "100"
        contract.tradingClass = "SPXW" if underlying.upper() == "SPX" else underlying
        return contract

    def _next_order_id(self):
        wrapper = self.conn_mgr.get_wrapper()
        if wrapper is None or wrapper.next_valid_order_id is None:
            raise RuntimeError("IBKR next order ID is not available")

        order_id = wrapper.next_valid_order_id
        wrapper.next_valid_order_id += 1
        return order_id

    def _place_option_order(self, position, quantity):
        from ibapi.order import Order

        client = self.conn_mgr.get_client()
        if client is None:
            raise RuntimeError("IBKR client is not available")

        contract = self._build_option_contract(
            position["underlying"],
            position["expiry"],
            position["strike"],
            position["right"],
        )

        order = Order()
        order.action = position["action"]
        order.totalQuantity = int(quantity)
        order.orderType = "LMT"
        order.lmtPrice = float(position["price"])
        order.tif = "DAY"
        order.transmit = True
        order.eTradeOnly = False
        order.firmQuoteOnly = False

        order_id = self._next_order_id()
        client.placeOrder(order_id, contract, order)
        return order_id

    def _option_price(self, row, option_type, side):
        prefix = "call" if option_type == "CE" else "put"
        preferred = f"{prefix}_bid" if side == "S" else f"{prefix}_ask"
        for column in (preferred, f"{prefix}_mid", f"{prefix}_model_price"):
            if column in row.index and pd.notna(row[column]) and float(row[column]) > 0:
                return round(float(row[column]), 2)
        return None

    def _find_hedge_row(self, chain, short_strike, option_type, hedge_dist, log_fallback=True):
        price_col = "call_mid" if option_type == "CE" else "put_mid"
        target_strike = short_strike + hedge_dist if option_type == "CE" else short_strike - hedge_dist

        if option_type == "CE":
            candidates = chain[(chain["strike"] > short_strike) & chain[price_col].notna()].copy()
        else:
            candidates = chain[(chain["strike"] < short_strike) & chain[price_col].notna()].copy()

        if candidates.empty:
            return None

        exact = candidates[candidates["strike"] == target_strike]
        if not exact.empty:
            return exact.iloc[0]

        candidates["hedge_distance"] = (candidates["strike"] - target_strike).abs()
        hedge = candidates.loc[candidates["hedge_distance"].idxmin()]
        if log_fallback:
            logger.warning(f"Exact {option_type} hedge {target_strike:.0f} unavailable; using {hedge['strike']:.0f}")
        return hedge

    def _select_short_with_hedge(self, chain, candidates, option_type, target_delta, hedge_dist):
        delta_col = "call_delta" if option_type == "CE" else "put_delta"
        ranked = candidates.copy()
        ranked["target_distance"] = (ranked[delta_col].abs() - target_delta).abs()

        for _, short_row in ranked.sort_values("target_distance").iterrows():
            hedge_row = self._find_hedge_row(
                chain,
                float(short_row["strike"]),
                option_type,
                hedge_dist,
                log_fallback=False,
            )
            if hedge_row is not None:
                expected_hedge = float(short_row["strike"]) + hedge_dist if option_type == "CE" else float(short_row["strike"]) - hedge_dist
                if float(hedge_row["strike"]) != expected_hedge:
                    logger.warning(f"Exact {option_type} hedge {expected_hedge:.0f} unavailable; using {hedge_row['strike']:.0f}")
                return short_row, hedge_row

        return None, None
    
    
    def select_and_open_positions(self, option_chain, target_delta=0.30, quantity=1, hedge_dist=None, submit_orders=True):
        """
        Select and open delta-neutral positions
        
        Args:
            option_chain: DataFrame with option chain data
            target_delta: Target absolute delta for short call and short put
            quantity: Number of option contracts per leg
            hedge_dist: Hedge distance in SPX points; defaults to strategy config
            submit_orders: If True, submit limit orders to IBKR
        
        Returns:
            List of opened positions
        """
        try:
            if option_chain.empty:
                logger.warning("Option chain is empty, cannot open positions")
                return []
            
            self.cycle_id += 1
            logger.info(f"Opening positions for cycle {self.cycle_id}...")

            chain = option_chain.copy()
            hedge_dist = hedge_dist if hedge_dist is not None else self.strategy.config.get("hedge_dist", 150)

            if "call_delta" not in chain.columns or "put_delta" not in chain.columns:
                spot_price = float(chain["spot_price"].dropna().iloc[0])
                dte_days = float(chain["dte_days"].dropna().iloc[0]) if "dte_days" in chain.columns else 5
                chain = self.strategy.add_deltas(chain, spot_price, dte_days=dte_days)

            if "call_delta_ibkr" in chain.columns:
                chain["call_delta"] = chain["call_delta_ibkr"].combine_first(chain["call_delta"])
            if "put_delta_ibkr" in chain.columns:
                chain["put_delta"] = chain["put_delta_ibkr"].combine_first(chain["put_delta"])

            call_candidates = chain[
                chain["call_delta"].notna() &
                chain["call_mid"].notna() &
                (chain["call_delta"] > 0)
            ].copy()
            put_candidates = chain[
                chain["put_delta"].notna() &
                chain["put_mid"].notna() &
                (chain["put_delta"] < 0)
            ].copy()

            if call_candidates.empty or put_candidates.empty:
                logger.warning("No valid call/put candidates with deltas and prices")
                return []

            short_call, hedge_call = self._select_short_with_hedge(chain, call_candidates, "CE", target_delta, hedge_dist)
            short_put, hedge_put = self._select_short_with_hedge(chain, put_candidates, "PE", target_delta, hedge_dist)

            if hedge_call is None or hedge_put is None:
                logger.warning("Could not find hedge legs for selected short strikes")
                return []

            legs = [
                ("S", "SELL", "CE", "C", short_call, float(short_call["call_delta"])),
                ("S", "SELL", "PE", "P", short_put, float(short_put["put_delta"])),
                ("B", "BUY", "CE", "C", hedge_call, float(hedge_call["call_delta"])),
                ("B", "BUY", "PE", "P", hedge_put, float(hedge_put["put_delta"])),
            ]

            positions = []
            for side, action, option_type, right, row, delta in legs:
                price = self._option_price(row, option_type, side)
                if price is None:
                    logger.warning(f"No usable {option_type} price for strike {row['strike']}")
                    return []

                positions.append({
                    "cycle_id": self.cycle_id,
                    "underlying": row.get("underlying", "SPX"),
                    "expiry": str(row["expiry"]),
                    "strike": float(row["strike"]),
                    "type": option_type,
                    "right": right,
                    "side": side,
                    "action": action,
                    "quantity": int(quantity),
                    "price": price,
                    "entry_price": price,
                    "delta": delta,
                    "order_id": None,
                    "status": "PENDING_SUBMIT" if submit_orders else "DRY_RUN",
                })
            
            if not positions:
                logger.warning(f"No positions selected for cycle {self.cycle_id}")
                return []
            
            for pos in positions:
                logger.info(f"  Opening: {pos['action']} {pos['quantity']} {pos['type']} {pos['strike']:.0f} {pos['expiry']} @ {pos['price']:.2f} (delta={pos['delta']:.3f})")
                if submit_orders:
                    pos["order_id"] = self._place_option_order(pos, quantity)
                    pos["status"] = "SUBMITTED"
                    logger.info(f"    Submitted IBKR order_id={pos['order_id']}")

            self.live_positions[self.cycle_id] = {
                "cycle_id": self.cycle_id,
                "entry_time": pd.Timestamp.now(),
                "target_delta": target_delta,
                "hedge_dist": hedge_dist,
                "quantity": int(quantity),
                "adjustments": 0,
                "status": "OPEN" if submit_orders else "DRY_RUN",
                "positions": positions
            }
            
            self.trade_log.append({
                "cycle_id": self.cycle_id,
                "action": "OPEN",
                "timestamp": pd.Timestamp.now(),
                "target_delta": target_delta,
                "quantity": int(quantity),
                "positions": len(positions)
            })
            
            logger.info(f"✓ Cycle {self.cycle_id} opened with {len(positions)} positions")
            return positions
        
        except Exception as e:
            logger.error(f"Error opening positions: {e}", exc_info=True)
            return []

    def _find_chain_row(self, option_chain, strike):
        rows = option_chain[option_chain["strike"] == float(strike)]
        if rows.empty:
            return None
        return rows.iloc[0]

    def _current_leg_delta(self, option_chain, pos):
        row = self._find_chain_row(option_chain, pos["strike"])
        if row is None:
            return None

        delta_col = "call_delta" if pos["type"] == "CE" else "put_delta"
        if delta_col not in row.index or pd.isna(row[delta_col]):
            return None

        return float(row[delta_col])

    def _calculate_current_portfolio_delta(self, option_chain, positions):
        total_delta = 0.0
        missing = []

        for pos in positions:
            if pos.get("status") == "CLOSED":
                continue

            delta = self._current_leg_delta(option_chain, pos)
            if delta is None:
                missing.append(f"{pos['type']} {pos['strike']:.0f}")
                delta = float(pos.get("delta", 0.0))

            side_multiplier = -1 if pos["side"] == "S" else 1
            total_delta += delta * side_multiplier * int(pos.get("quantity", 1))

        if missing:
            logger.warning(f"Using stored deltas for missing current legs: {', '.join(missing)}")

        return total_delta

    def _find_open_short(self, positions, option_type):
        for pos in positions:
            if pos.get("status") != "CLOSED" and pos["side"] == "S" and pos["type"] == option_type:
                return pos
        return None

    def _find_open_hedge(self, positions, short_pos, hedge_dist):
        hedge_strike = short_pos["strike"] + hedge_dist if short_pos["type"] == "CE" else short_pos["strike"] - hedge_dist
        hedge_candidates = []

        for pos in positions:
            if (
                pos.get("status") != "CLOSED" and
                pos["side"] == "B" and
                pos["type"] == short_pos["type"]
            ):
                if pos["strike"] == hedge_strike:
                    return pos
                if short_pos["type"] == "CE" and pos["strike"] > short_pos["strike"]:
                    hedge_candidates.append(pos)
                elif short_pos["type"] == "PE" and pos["strike"] < short_pos["strike"]:
                    hedge_candidates.append(pos)

        if hedge_candidates:
            return min(hedge_candidates, key=lambda pos: abs(pos["strike"] - hedge_strike))

        return None

    def _select_adjustment_short(self, option_chain, option_type, current_delta):
        delta_col = "call_delta" if option_type == "CE" else "put_delta"
        price_col = "call_mid" if option_type == "CE" else "put_mid"
        required_delta = abs(current_delta)

        candidates = option_chain[
            option_chain[delta_col].notna() &
            option_chain[price_col].notna() &
            (option_chain[delta_col].abs() >= required_delta)
        ].copy()

        if candidates.empty:
            return None

        candidates["excess_delta"] = candidates[delta_col].abs() - required_delta
        return candidates.loc[candidates["excess_delta"].idxmin()]

    def _position_from_row(self, row, side, action, option_type, quantity, cycle_id):
        right = "C" if option_type == "CE" else "P"
        price = self._option_price(row, option_type, side)
        if price is None:
            return None

        delta_col = "call_delta" if option_type == "CE" else "put_delta"
        return {
            "cycle_id": cycle_id,
            "underlying": row.get("underlying", "SPX"),
            "expiry": str(row["expiry"]),
            "strike": float(row["strike"]),
            "type": option_type,
            "right": right,
            "side": side,
            "action": action,
            "quantity": int(quantity),
            "price": price,
            "entry_price": price,
            "delta": float(row[delta_col]),
            "order_id": None,
            "status": "PENDING_SUBMIT",
        }

    def _close_position(self, pos, option_chain, reason):
        row = self._find_chain_row(option_chain, pos["strike"])
        close_side = "B" if pos["side"] == "S" else "S"
        close_action = "BUY" if pos["side"] == "S" else "SELL"

        if row is None:
            logger.warning(f"Cannot close {pos['type']} {pos['strike']:.0f}: missing chain row")
            return None

        close_price = self._option_price(row, pos["type"], close_side)
        if close_price is None:
            logger.warning(f"Cannot close {pos['type']} {pos['strike']:.0f}: missing close price")
            return None

        close_order = pos.copy()
        close_order["action"] = close_action
        close_order["side"] = close_side
        close_order["price"] = close_price
        order_id = self._place_option_order(close_order, int(pos.get("quantity", 1)))

        multiplier = self.strategy.config.get("lot_size", 100)
        entry_price = float(pos.get("entry_price", pos.get("price", 0)))
        pnl = (entry_price - close_price) * multiplier if pos["side"] == "S" else (close_price - entry_price) * multiplier

        pos["status"] = "CLOSED"
        pos["close_time"] = pd.Timestamp.now()
        pos["close_reason"] = reason
        pos["close_price"] = close_price
        pos["close_order_id"] = order_id
        pos["pnl"] = pnl

        logger.info(f"  Closing: {close_action} {pos['quantity']} {pos['type']} {pos['strike']:.0f} @ {close_price:.2f} order_id={order_id}")
        return pos

    def _submit_new_position(self, pos):
        order_id = self._place_option_order(pos, int(pos.get("quantity", 1)))
        pos["order_id"] = order_id
        pos["status"] = "SUBMITTED"
        logger.info(f"  Opening adjustment: {pos['action']} {pos['quantity']} {pos['type']} {pos['strike']:.0f} @ {pos['price']:.2f} order_id={order_id}")
        return pos
    
    
    def monitor_and_adjust(self, option_chain):
        """
        Monitor open positions and make adjustments if needed
        
        Args:
            option_chain: DataFrame with current option chain data
        
        Returns:
            Dict with adjustment results
        """
        try:
            if option_chain.empty:
                logger.warning("Option chain is empty, cannot monitor positions")
                return {}
            
            results = {}
            
            for cycle_id, pos_data in list(self.live_positions.items()):
                if pos_data["status"] != "OPEN":
                    continue
                
                logger.info(f"Monitoring cycle {cycle_id} ({len(pos_data['positions'])} legs)...")

                current_delta = self._calculate_current_portfolio_delta(option_chain, pos_data["positions"])
                pos_delta_thresh = self.strategy.config.get("pos_delta_thresh", 0.25)
                max_adjusts = self.strategy.config.get("max_adjusts", 3)
                adjustments = pos_data.get("adjustments", 0)

                logger.info(
                    f"  Cycle {cycle_id}: current_delta={current_delta:.4f}, "
                    f"threshold={pos_delta_thresh:.4f}, adjustments={adjustments}/{max_adjusts}"
                )

                if abs(current_delta) <= pos_delta_thresh:
                    results[cycle_id] = {"status": "within_threshold", "current_delta": current_delta}
                    continue

                if adjustments >= max_adjusts:
                    logger.warning(f"  Cycle {cycle_id}: max adjustments reached")
                    results[cycle_id] = {"status": "max_adjusts_reached", "current_delta": current_delta}
                    continue

                short_type = "CE" if current_delta > 0 else "PE"
                short_pos = self._find_open_short(pos_data["positions"], short_type)
                if short_pos is None:
                    logger.warning(f"  Cycle {cycle_id}: no open {short_type} short to adjust")
                    results[cycle_id] = {"status": "missing_short", "current_delta": current_delta}
                    continue

                hedge_dist = pos_data.get("hedge_dist", self.strategy.config.get("hedge_dist", 150))
                hedge_pos = self._find_open_hedge(pos_data["positions"], short_pos, hedge_dist)
                if hedge_pos is None:
                    logger.warning(f"  Cycle {cycle_id}: no matching {short_type} hedge for strike {short_pos['strike']:.0f}")
                    results[cycle_id] = {"status": "missing_hedge", "current_delta": current_delta}
                    continue

                new_short = self._select_adjustment_short(option_chain, short_type, current_delta)
                if new_short is None:
                    logger.warning(f"  Cycle {cycle_id}: no replacement {short_type} short found")
                    results[cycle_id] = {"status": "no_replacement_short", "current_delta": current_delta}
                    continue

                opposite_short = self._find_open_short(pos_data["positions"], "PE" if short_type == "CE" else "CE")
                if opposite_short is not None:
                    new_strike = float(new_short["strike"])
                    if (
                        (new_strike > opposite_short["strike"] and opposite_short["type"] == "CE") or
                        (new_strike < opposite_short["strike"] and opposite_short["type"] == "PE")
                    ):
                        logger.warning(f"  Cycle {cycle_id}: replacement {short_type} {new_strike:.0f} crosses opposite short; skipping")
                        results[cycle_id] = {"status": "replacement_crosses_opposite_short", "current_delta": current_delta}
                        continue

                hedge_row = self._find_hedge_row(option_chain, float(new_short["strike"]), short_type, hedge_dist)
                if hedge_row is None:
                    logger.warning(f"  Cycle {cycle_id}: no replacement {short_type} hedge found")
                    results[cycle_id] = {"status": "no_replacement_hedge", "current_delta": current_delta}
                    continue

                logger.info(
                    f"  Cycle {cycle_id}: rolling {short_type} pair from {short_pos['strike']:.0f}/"
                    f"{hedge_pos['strike']:.0f} to {new_short['strike']:.0f}/{hedge_row['strike']:.0f}"
                )

                closed_short = self._close_position(short_pos, option_chain, "delta_adjust")
                closed_hedge = self._close_position(hedge_pos, option_chain, "delta_adjust")
                if closed_short is None or closed_hedge is None:
                    results[cycle_id] = {"status": "close_failed", "current_delta": current_delta}
                    continue

                quantity = pos_data.get("quantity", 1)
                replacement_short = self._position_from_row(new_short, "S", "SELL", short_type, quantity, cycle_id)
                replacement_hedge = self._position_from_row(hedge_row, "B", "BUY", short_type, quantity, cycle_id)
                if replacement_short is None or replacement_hedge is None:
                    results[cycle_id] = {"status": "open_replacement_failed", "current_delta": current_delta}
                    continue

                self._submit_new_position(replacement_short)
                self._submit_new_position(replacement_hedge)
                pos_data["positions"].extend([replacement_short, replacement_hedge])
                pos_data["adjustments"] = adjustments + 1

                self.trade_log.append({
                    "cycle_id": cycle_id,
                    "action": "ADJUST",
                    "timestamp": pd.Timestamp.now(),
                    "current_delta": current_delta,
                    "short_type": short_type,
                    "closed_short": short_pos["strike"],
                    "closed_hedge": hedge_pos["strike"],
                    "opened_short": replacement_short["strike"],
                    "opened_hedge": replacement_hedge["strike"],
                    "adjustments": pos_data["adjustments"],
                })

                results[cycle_id] = {
                    "status": "adjusted",
                    "current_delta": current_delta,
                    "short_type": short_type,
                    "adjustments": pos_data["adjustments"],
                }
            
            return results
        
        except Exception as e:
            logger.error(f"Error monitoring positions: {e}", exc_info=True)
            return {}
    
    
    def close_positions_at_expiry(self, cycle_id, option_chain=None, reason="expiry"):
        """
        Close all positions in a cycle at expiry
        
        Args:
            cycle_id: Cycle ID to close
            option_chain: DataFrame with current option chain data (optional)
            reason: Close reason label
        
        Returns:
            Dict with close results (pnl, close_prices, etc.)
        """
        try:
            if cycle_id not in self.live_positions:
                logger.warning(f"Cycle {cycle_id} not found in live_positions")
                return {}
            
            pos_data = self.live_positions[cycle_id]
            
            if pos_data["status"] != "OPEN":
                logger.warning(f"Cycle {cycle_id} is already {pos_data['status']}")
                return {}
            
            logger.info(f"Closing cycle {cycle_id} ({len(pos_data['positions'])} positions)...")

            if option_chain is None or getattr(option_chain, "empty", True):
                underlying = "SPX"
                if pos_data.get("positions"):
                    underlying = pos_data["positions"][0].get("underlying", "SPX")
                option_chain = self.get_live_option_chain(underlying=underlying)

            if option_chain is None or option_chain.empty:
                logger.error("Cannot close positions: option_chain unavailable/empty")
                return {}

            total_pnl = 0.0
            close_details = []

            for pos in pos_data["positions"]:
                if pos.get("status") == "CLOSED":
                    continue

                closed = self._close_position(pos, option_chain, reason=reason)
                if closed is None:
                    continue

                pnl = float(closed.get("pnl", 0.0) or 0.0)
                total_pnl += pnl
                close_details.append(
                    {
                        "side": closed["side"],
                        "type": closed["type"],
                        "strike": closed["strike"],
                        "close_price": closed.get("close_price"),
                        "pnl": pnl,
                    }
                )
            
            # Update position status
            pos_data["status"] = "CLOSED"
            pos_data["close_time"] = pd.Timestamp.now()
            pos_data["pnl"] = total_pnl
            pos_data["close_details"] = close_details
            
            self.trade_log.append({
                "cycle_id": cycle_id,
                "action": "CLOSE",
                "timestamp": pd.Timestamp.now(),
                "pnl": total_pnl,
                "positions_count": len(pos_data["positions"])
            })
            
            logger.info(f"✓ Cycle {cycle_id} closed | Total PnL: ${total_pnl:,.2f}")
            
            return {
                "cycle_id": cycle_id,
                "total_pnl": total_pnl,
                "close_count": len(close_details),
                "close_details": close_details
            }
        
        except Exception as e:
            logger.error(f"Error closing positions: {e}", exc_info=True)
            return {}
    
    
    def calculate_portfolio_delta(self, positions):
        """
        Calculate total portfolio delta
        
        Args:
            positions: List of position dicts
        
        Returns:
            Float representing total delta
        """
        try:
            total_delta = 0.0
            
            for pos in positions:
                delta = pos.get("delta", 0.0)
                side_multiplier = -1 if pos["side"] == "S" else 1  # Short = negative delta
                total_delta += delta * side_multiplier
            
            logger.debug(f"Portfolio delta: {total_delta:.4f}")
            return total_delta
        
        except Exception as e:
            logger.error(f"Error calculating portfolio delta: {e}")
            return 0.0
    
    
    def save_trade_log(self):
        """
        Save trade log to CSV
        
        Returns:
            Path to saved file or None if error
        """
        try:
            if not self.trade_log:
                logger.info("No trades to save")
                return None
            
            log_dir = Path(__file__).parent.parent / "logs" / "paper_trading"
            log_dir.mkdir(parents=True, exist_ok=True)
            
            log_file = log_dir / f"paper_trade_log_{pd.Timestamp.now().strftime('%Y%m%d_%H%M%S')}.csv"
            
            log_df = pd.DataFrame(self.trade_log)
            log_df.to_csv(log_file, index=False)
            
            logger.info(f"✓ Trade log saved: {log_file}")
            return log_file
        
        except Exception as e:
            logger.error(f"Error saving trade log: {e}", exc_info=True)
            return None
    
    
    def get_position_summary(self):
        """
        Get summary of all positions (open and closed)
        
        Returns:
            Dict with position summary
        """
        open_count = sum(1 for p in self.live_positions.values() if p["status"] == "OPEN")
        closed_count = sum(1 for p in self.live_positions.values() if p["status"] == "CLOSED")
        total_pnl = sum(p.get("pnl", 0) for p in self.live_positions.values() if p["status"] == "CLOSED")
        
        return {
            "total_cycles": len(self.live_positions),
            "open_cycles": open_count,
            "closed_cycles": closed_count,
            "total_pnl": total_pnl,
            "trade_log_entries": len(self.trade_log)
        }
