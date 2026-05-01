import sys
from pathlib import Path

# Add backtest folder to path so we can import strategy/config
sys.path.insert(0, str(Path(__file__).parent.parent / "backtest"))


import pandas as pd
import logging
from datetime import datetime
from pathlib import Path
from ibkr_client import IBKRClient
from strategy import DeltaNeutralStrategy  # Use relative import if strategy is in the same package
from config import STRATEGY_CONFIG, LOG_PATH, PROCESSED_PATH

class PaperTrader:
    """Live/paper trading execution engine for Delta Neutral Strategy"""
    
    def __init__(self, ibkr_client: IBKRClient, is_paper: bool = True):
        self.client = ibkr_client
        self.is_paper = is_paper
        self.strategy = DeltaNeutralStrategy()
        self.live_positions = {}  # Track open IBKR positions
        self.trade_log = []
        self.logger = logging.getLogger(__name__)
        
    def connect(self):
        """Connect to IBKR TWS/Gateway"""
        self.client.connect()
        self.logger.info(f"Connected to IBKR (Paper={self.is_paper})")
    
    def get_live_option_chain(self, contract_symbol: str = "SPX", expiry: str = None, strike_range: tuple = None) -> pd.DataFrame:
    
        try:
            chain = self.client.get_option_chain(
                symbol=contract_symbol,
                expiry=expiry,
                strike_range=strike_range
            )
            self.logger.info(f"Fetched live {contract_symbol} option chain: {len(chain)} strikes")
            return chain
        except Exception as e:
            self.logger.error(f"Error fetching option chain: {e}")
            return pd.DataFrame()
    
    def get_live_spot_price(self, contract_symbol: str = "SPX"):
        """Get current SPX price"""
        try:
            spot = self.client.get_contract_price(contract_symbol)
            self.logger.info(f"Live {contract_symbol} price: {spot}")
            return spot
        except Exception as e:
            self.logger.error(f"Error fetching spot price: {e}")
            return None
        
    def select_and_open_positions(self, option_chain: pd.DataFrame):
        """Select strikes and place initial orders"""
        try:
            # Use backtest strategy logic to select strikes
            selection = self.strategy.select_strikes(option_chain)
            
            if not selection:
                self.logger.warning("No valid strike selection returned")
                return False
            
            bucket_ts = pd.Timestamp.now()
            cycle_id = len(self.live_positions) + 1
            
            # Open 4-leg position (short call, short put, long call hedge, long put hedge)
            orders = self._build_orders(selection, cycle_id, bucket_ts)
            
            leg_data = {}
            for order in orders:
                order_id = self.client.place_order(order)
                leg_data[order_id] = {
                    "leg_type": order["leg_type"],
                    "strike": order["contract"]["strike"],
                    "right": order["contract"]["right"],
                    "action": order["action"],
                    "status": "OPEN",
                    "fill_price": None
                }
                self.logger.info(f"Placed order {order_id}: {order}")
            
            self.live_positions[cycle_id] = {
                "entry_time": bucket_ts,
                "leg_orders": leg_data,  # Track each leg separately
                "selection": selection,
                "status": "OPEN",
                "adjustments_count": 0
            }
            
            return True
        except Exception as e:
            self.logger.error(f"Error opening positions: {e}")
            return False

    def _build_close_orders(self, cycle_id: int, adjustment: dict) -> list:
        """Build orders to close over-exposed legs"""
        close_orders = []
        pos_data = self.live_positions.get(cycle_id)
        
        if not pos_data or "close_leg" not in adjustment:
            return close_orders
        
        close_leg = adjustment["close_leg"]
        # Reverse the action (SELL becomes BUY, BUY becomes SELL)
        reverse_action = "BUY" if close_leg["action"] == "SELL" else "SELL"
        
        close_orders.append({
            "contract": self._create_option_contract("SPX", close_leg["strike"], close_leg["right"]),
            "action": reverse_action,
            "quantity": 1,
            "order_type": "MKT",
            "cycle_id": cycle_id,
            "leg_type": f"close_{close_leg['leg_type']}"
        })
        
        return close_orders

    def _build_open_orders(self, cycle_id: int, adjustment: dict) -> list:
        """Build orders to open new hedge legs"""
        open_orders = []
        
        if "open_leg" not in adjustment:
            return open_orders
        
        open_leg = adjustment["open_leg"]
        
        open_orders.append({
            "contract": self._create_option_contract("SPX", open_leg["strike"], open_leg["right"]),
            "action": open_leg["action"],
            "quantity": 1,
            "order_type": "MKT",
            "cycle_id": cycle_id,
            "leg_type": f"adjust_{open_leg['leg_type']}"
        })
        
        return open_orders
    def monitor_and_adjust(self, option_chain: pd.DataFrame):
        """Monitor positions and perform adjustments"""
        try:
            for cycle_id, pos_data in self.live_positions.items():
                if pos_data["status"] != "OPEN":
                    continue
                
                # Calculate current delta
                current_delta = self.strategy.calculate_position_delta(option_chain)
                current_pnl = self.strategy.calculate_current_pnl(option_chain)
                
                self.logger.info(f"Cycle {cycle_id} - Delta: {current_delta}, PnL: {current_pnl}")
                
                # Check adjustment thresholds
                max_adjusts = STRATEGY_CONFIG["max_adjusts"]
                pos_delta_thresh = STRATEGY_CONFIG["pos_delta_thresh"]
                
                if abs(current_delta) > pos_delta_thresh:
                    self.logger.info(f"Delta threshold breached: {current_delta}. Adjusting...")
                    self._perform_adjustment(cycle_id, option_chain, current_delta)
        
        except Exception as e:
            self.logger.error(f"Error monitoring positions: {e}")
    
    def _perform_adjustment(self, cycle_id: int, option_chain: pd.DataFrame, current_delta: float):
        """Execute adjustment trades"""
        try:
            adjustment = self.strategy.select_adjustment_short(option_chain, current_delta)
            
            if not adjustment:
                self.logger.warning(f"No adjustment available for cycle {cycle_id}")
                return
            
            # Close over-exposed leg and open new one
            close_orders = self._build_close_orders(cycle_id, adjustment)
            open_orders = self._build_open_orders(cycle_id, adjustment)
            
            for order in close_orders + open_orders:
                order_id = self.client.place_order(order)
                self.logger.info(f"Adjustment order {order_id}: {order}")
            
            self.trade_log.append({
                "cycle_id": cycle_id,
                "timestamp": pd.Timestamp.now(),
                "type": "adjustment",
                "orders": close_orders + open_orders
            })
        
        except Exception as e:
            self.logger.error(f"Error performing adjustment: {e}")
    
    def close_positions_at_expiry(self, cycle_id: int):
        """Close position at expiry"""
        try:
            self.logger.info(f"Closing cycle {cycle_id} at expiry")
            
            close_orders = []
            # Build close orders for all 4 legs
            pos_data = self.live_positions[cycle_id]
            selection = pos_data["selection"]
            
            # Close short call
            close_orders.append(self._build_close_order(selection["short_call"], "C", "BUY"))
            # Close short put
            close_orders.append(self._build_close_order(selection["short_put"], "P", "BUY"))
            # Close long call
            close_orders.append(self._build_close_order(selection["hedge_call"], "C", "SELL"))
            # Close long put
            close_orders.append(self._build_close_order(selection["hedge_put"], "P", "SELL"))
            
            for order in close_orders:
                order_id = self.client.place_order(order)
                self.logger.info(f"Close order {order_id}: {order}")
            
            self.live_positions[cycle_id]["status"] = "CLOSED"
            self.live_positions[cycle_id]["close_time"] = pd.Timestamp.now()
        
        except Exception as e:
            self.logger.error(f"Error closing positions: {e}")
    
    def _build_orders(self, selection: dict, cycle_id: int, entry_time) -> list:
        """Build IBKR order objects from strike selection"""
        orders = []
        
        # Short call
        orders.append({
            "contract": self._create_option_contract("SPX", selection["short_call"]["strike"], "C"),
            "action": "SELL",
            "quantity": 1,
            "order_type": "MKT",
            "cycle_id": cycle_id,
            "leg_type": "short_call"
        })
        
        # Short put
        orders.append({
            "contract": self._create_option_contract("SPX", selection["short_put"]["strike"], "P"),
            "action": "SELL",
            "quantity": 1,
            "order_type": "MKT",
            "cycle_id": cycle_id,
            "leg_type": "short_put"
        })
        
        # Long call hedge
        orders.append({
            "contract": self._create_option_contract("SPX", selection["hedge_call"]["strike"], "C"),
            "action": "BUY",
            "quantity": 1,
            "order_type": "MKT",
            "cycle_id": cycle_id,
            "leg_type": "long_call"
        })
        
        # Long put hedge
        orders.append({
            "contract": self._create_option_contract("SPX", selection["hedge_put"]["strike"], "P"),
            "action": "BUY",
            "quantity": 1,
            "order_type": "MKT",
            "cycle_id": cycle_id,
            "leg_type": "long_put"
        })
        
        return orders
    
    def _create_option_contract(self, symbol: str, strike: float, right: str):
        """Create IBKR option contract"""
        return {
            "symbol": symbol,
            "secType": "OPT",
            "exchange": "CBOE",
            "currency": "USD",
            "strike": float(strike),
            "right": right,  # "C" or "P"
            "expiry": self._get_next_friday()  # 5 DTE expiry
        }
    
    def _get_next_friday(self) -> str:
        """Get next Friday date (YYYYMMDD format)"""
        from pandas.tseries.offsets import BusinessDay
        next_fri = pd.Timestamp.now() + BusinessDay(4)  # 5 DTE
        return next_fri.strftime("%Y%m%d")
    
    def _build_close_order(self, leg: dict, right: str, action: str) -> dict:
        """Build order to close a single leg"""
        return {
            "contract": self._create_option_contract("SPX", leg["strike"], right),
            "action": action,
            "quantity": 1,
            "order_type": "MKT"
        }
    
    
    
    def save_trade_log(self, output_path: Path = PROCESSED_PATH):
        """Save executed trades to CSV"""
        if not self.trade_log:
            self.logger.warning("No trades to save")
            return
        
        trades_df = pd.DataFrame(self.trade_log)
        output_file = Path(output_path) / f"paper_trades_{pd.Timestamp.now().strftime('%Y%m%d_%H%M%S')}.csv"
        trades_df.to_csv(output_file, index=False)
        self.logger.info(f"Saved paper trades to {output_file}")