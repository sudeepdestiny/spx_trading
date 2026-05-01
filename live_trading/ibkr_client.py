import pandas as pd
import logging
import threading
import time
from typing import Dict, List, Optional
from queue import Queue

class IBKRClient:
    """Wrapper for Interactive Brokers API (TWS/Gateway)"""
    
    def __init__(self, host: str = "127.0.0.1", port: int = 7497, client_id: int = 4, is_paper: bool = True):
        self.host = host
        self.port = port
        self.client_id = client_id
        self.is_paper = is_paper
        self.ib = None
        self.logger = logging.getLogger(__name__)
        self.next_order_id = None
        self.next_req_id = 10000
        self.contract_details = {}
        self.live_prices = {}
        self.market_data_lock = threading.Lock()
        self.connection_event = threading.Event()  # Signal when connected
        self.is_connected = False
    
    def connect(self):
        """Connect to TWS/Gateway and wait for handshake"""
        try:
            from ibapi.client import EClient
            from ibapi.wrapper import EWrapper
            from ibapi.contract import Contract
            
            class IBWrapper(EWrapper):
                def __init__(self, client):
                    self.client = client
                
                def nextValidId(self, orderId: int):
                    """Called when connection is ready"""
                    self.client.next_order_id = orderId
                    self.client.is_connected = True
                    self.client.connection_event.set()  # Signal connection ready
                    self.client.logger.info(f"✓ Connected! Next order ID: {orderId}")
                
                def tickPrice(self, reqId: int, tickType: int, price: float, attrib):
                    """Handle price ticks"""
                    with self.client.market_data_lock:
                        if reqId not in self.client.live_prices:
                            self.client.live_prices[reqId] = {}
                        tick_names = {1: "bid", 2: "ask", 4: "last", 6: "high", 7: "low", 9: "close"}
                        if tickType in tick_names:
                            self.client.live_prices[reqId][tick_names[tickType]] = price
                
                def contractDetails(self, reqId: int, contractDetails):
                    """Handle contract details"""
                    with self.client.market_data_lock:
                        self.client.contract_details[reqId] = contractDetails
                
                def contractDetailsEnd(self, reqId: int):
                    """End of contract details stream"""
                    self.client.logger.info(f"Contract details complete for reqId {reqId}")
                
                def error(self, reqId: int, errorCode: int, errorString: str):
                    self.client.logger.error(f"Error {errorCode} (reqId {reqId}): {errorString}")
                
                def connectionClosed(self):
                    self.client.is_connected = False
                    self.client.connection_event.clear()
                    self.client.logger.warning("Connection closed")
            
            self.logger.info(f"Connecting to IBKR at {self.host}:{self.port} (Paper={self.is_paper})...")
            
            self.ib = EClient(IBWrapper(self))
            self.ib.connect(self.host, self.port, self.client_id)
            
            # Start message processing thread
            thread = threading.Thread(target=self.ib.run, daemon=True)
            thread.start()
            
            # Wait for nextValidId callback (max 5 seconds)
            connected = self.connection_event.wait(timeout=5.0)
            
            if not connected:
                raise RuntimeError(
                    f"Failed to connect to IBKR at {self.host}:{self.port}. "
                    "Make sure TWS or Gateway is running and listening on this port."
                )
            
            self.logger.info(f"✓ Connected to IBKR TWS at {self.host}:{self.port}")
        
        except ImportError as e:
            self.logger.error(f"ibapi not installed: {e}. Install with: pip install ibapi")
            raise
        except Exception as e:
            self.logger.error(f"Connection failed: {e}")
            raise
    
    def is_ready(self) -> bool:
        """Check if client is ready for requests"""
        return self.is_connected and self.next_order_id is not None
    
    def wait_for_ready(self, timeout: int = 5):
        """Wait until client is ready"""
        if not self.connection_event.wait(timeout=timeout):
            raise RuntimeError("IBKR client connection timeout")
    
    def disconnect(self):
        """Disconnect from TWS/Gateway"""
        if self.ib:
            self.ib.disconnect()
            self.is_connected = False
            self.logger.info("Disconnected from IBKR")
    
    def get_option_chain(self, symbol: str = "SPX", expiry: str = None, strike_range: tuple = None) -> pd.DataFrame:
        """
        Fetch live option chain from IBKR market data
        
        Args:
            symbol: Underlying symbol (e.g., "SPX")
            expiry: Expiry date (YYYYMMDD format). If None, uses next Friday
            strike_range: Tuple (low_strike, high_strike) to limit strikes
        
        Returns:
            DataFrame with columns: strike, call_bid, call_ask, call_mid, put_bid, put_ask, put_mid
        """
        from ibapi.contract import Contract
        import time as time_module
        
        # Validate connection
        if not self.is_ready():
            self.logger.error("IBKR client not ready. Call connect() first.")
            return pd.DataFrame()
        
        # Default to next Friday if expiry not provided
        if expiry is None:
            from pandas.tseries.offsets import BusinessDay
            next_fri = pd.Timestamp.now() + BusinessDay(4)
            expiry = next_fri.strftime("%Y%m%d")
        
        self.logger.info(f"Requesting option chain for {symbol} expiry {expiry}")
        
        try:
            # Step 1: Get SPX spot price
            spot_contract = Contract()
            spot_contract.symbol = symbol
            spot_contract.secType = "IND"
            spot_contract.exchange = "CBOE"
            spot_contract.currency = "USD"
            
            spot_req_id = self.next_req_id
            self.next_req_id += 1
            
            self.logger.info(f"Requesting spot price (reqId {spot_req_id})...")
            self.ib.reqMktData(spot_req_id, spot_contract, "", False, False, [])
            time_module.sleep(1.0)  # Wait for data
            
            spot_price = self.live_prices.get(spot_req_id, {}).get("last")
            self.ib.cancelMktData(spot_req_id)
            
            if not spot_price:
                self.logger.error(f"Failed to get {symbol} spot price")
                return pd.DataFrame()
            
            self.logger.info(f"✓ Spot price: {spot_price}")
            
            # Step 2: Request all option chains for expiry
            option_contract = Contract()
            option_contract.symbol = symbol
            option_contract.secType = "OPT"
            option_contract.exchange = "CBOE"
            option_contract.currency = "USD"
            option_contract.lastTradeDateOrContractMonth = expiry
            
            chain_req_id = self.next_req_id
            self.next_req_id += 1
            
            self.logger.info(f"Requesting contract details (reqId {chain_req_id})...")
            self.ib.reqContractDetails(chain_req_id, option_contract)
            time_module.sleep(2.0)  # Wait for contract details
            
            # Step 3: Extract strikes from contract details
            strikes = set()
            with self.market_data_lock:
                for req_id, contract_detail in self.contract_details.items():
                    if contract_detail.contract.lastTradeDateOrContractMonth == expiry:
                        if contract_detail.contract.strike > 0:
                            strikes.add(contract_detail.contract.strike)
            
            if not strikes:
                self.logger.warning(f"No strikes found for {symbol} {expiry}")
                return pd.DataFrame()
            
            strikes = sorted(list(strikes))
            self.logger.info(f"✓ Found {len(strikes)} strikes")
            
            # Filter by strike range if provided
            if strike_range:
                low, high = strike_range
                strikes = [s for s in strikes if low <= s <= high]
                self.logger.info(f"Filtered to {len(strikes)} strikes in range {strike_range}")
            
            # Step 4: Request market data for calls and puts
            option_data = {}
            
            self.logger.info(f"Requesting market data for {len(strikes)} strikes...")
            for strike in strikes:
                # Call
                call_contract = Contract()
                call_contract.symbol = symbol
                call_contract.secType = "OPT"
                call_contract.exchange = "CBOE"
                call_contract.currency = "USD"
                call_contract.strike = strike
                call_contract.right = "C"
                call_contract.lastTradeDateOrContractMonth = expiry
                
                call_req_id = self.next_req_id
                self.next_req_id += 1
                option_data[call_req_id] = {"strike": strike, "right": "C"}
                self.ib.reqMktData(call_req_id, call_contract, "", False, False, [])
                
                # Put
                put_contract = Contract()
                put_contract.symbol = symbol
                put_contract.secType = "OPT"
                put_contract.exchange = "CBOE"
                put_contract.currency = "USD"
                put_contract.strike = strike
                put_contract.right = "P"
                put_contract.lastTradeDateOrContractMonth = expiry
                
                put_req_id = self.next_req_id
                self.next_req_id += 1
                option_data[put_req_id] = {"strike": strike, "right": "P"}
                self.ib.reqMktData(put_req_id, put_contract, "", False, False, [])
            
            # Wait for market data to populate
            time_module.sleep(3.0)
            
            # Step 5: Aggregate into DataFrame
            rows = []
            
            with self.market_data_lock:
                for strike in strikes:
                    call_bid = None
                    call_ask = None
                    put_bid = None
                    put_ask = None
                    
                    for req_id, data in option_data.items():
                        if data["strike"] == strike and req_id in self.live_prices:
                            prices = self.live_prices[req_id]
                            if data["right"] == "C":
                                call_bid = prices.get("bid")
                                call_ask = prices.get("ask")
                            else:
                                put_bid = prices.get("bid")
                                put_ask = prices.get("ask")
                    
                    if call_bid and call_ask and put_bid and put_ask:
                        call_mid = (call_bid + call_ask) / 2
                        put_mid = (put_bid + put_ask) / 2
                        
                        rows.append({
                            "strike": strike,
                            "spot_price": spot_price,
                            "call_bid": call_bid,
                            "call_ask": call_ask,
                            "call_mid": call_mid,
                            "put_bid": put_bid,
                            "put_ask": put_ask,
                            "put_mid": put_mid,
                            "timestamp": pd.Timestamp.now()
                        })
            
            # Cancel all market data subscriptions
            for req_id in option_data.keys():
                self.ib.cancelMktData(req_id)
            
            df = pd.DataFrame(rows)
            self.logger.info(f"✓ Option chain retrieved: {len(df)} rows")
            
            return df
        
        except Exception as e:
            self.logger.error(f"Error fetching option chain: {e}", exc_info=True)
            return pd.DataFrame()
    
    def get_contract_price(self, symbol: str) -> float:
        """Get current market price for symbol"""
        from ibapi.contract import Contract
        import time as time_module
        
        if not self.is_ready():
            self.logger.error("IBKR client not ready")
            return None
        
        contract = Contract()
        contract.symbol = symbol
        contract.secType = "IND"
        contract.exchange = "CBOE"
        contract.currency = "USD"
        
        req_id = self.next_req_id
        self.next_req_id += 1
        
        self.ib.reqMktData(req_id, contract, "", False, False, [])
        time_module.sleep(1.0)
        
        price = self.live_prices.get(req_id, {}).get("last")
        self.ib.cancelMktData(req_id)
        
        self.logger.info(f"Price for {symbol}: {price}")
        return price
    
    def place_order(self, order_dict: Dict) -> int:
        """Place an order and return order ID"""
        from ibapi.order import Order
        
        if not self.is_ready():
            self.logger.error("IBKR client not ready for orders")
            return None
        
        order_id = self.next_order_id
        self.next_order_id += 1
        
        contract = self._dict_to_contract(order_dict["contract"])
        
        order = Order()
        order.action = order_dict["action"]
        order.totalQuantity = order_dict["quantity"]
        order.orderType = order_dict["order_type"]
        
        self.ib.placeOrder(order_id, contract, order)
        self.logger.info(f"Order {order_id}: {order_dict}")
        
        return order_id
    
    def _dict_to_contract(self, contract_dict: Dict):
        """Convert dict to IBKR Contract object"""
        from ibapi.contract import Contract
        
        contract = Contract()
        contract.symbol = contract_dict["symbol"]
        contract.secType = contract_dict["secType"]
        contract.exchange = contract_dict["exchange"]
        contract.currency = contract_dict["currency"]
        
        if contract_dict["secType"] == "OPT":
            contract.strike = contract_dict["strike"]
            contract.right = contract_dict["right"]
            contract.lastTradeDateOrContractMonth = contract_dict["expiry"]
        
        return contract
    
    def cancel_order(self, order_id: int):
        """Cancel an open order"""
        if self.ib and self.is_ready():
            self.ib.cancelOrder(order_id)
            self.logger.info(f"Cancelled order {order_id}")