"""
Singleton IBKR Connection Manager with auto-retry on different client IDs
Updated with:
- WhatIf combo margin checking
- Contract ID resolution helper
- openOrder callback for WhatIf orders
"""

import threading
import time
import logging
import random
import platform
import socket
import os
from pathlib import Path
from datetime import datetime
from typing import Optional, Tuple, Dict, List

try:
    from ibapi.client import EClient
    from ibapi.wrapper import EWrapper
    from ibapi.contract import Contract, ComboLeg
    from ibapi.order import Order
except ImportError:
    raise ImportError("ibapi not installed. Install with: pip install ibapi")

from config import apikey_config, LOG_PATH

# Setup logging
log_dir = Path(LOG_PATH)
log_dir.mkdir(parents=True, exist_ok=True)
timestamp = datetime.now().strftime("%Y%m%d")
log_file = log_dir / f"ibkr_connection_{timestamp}.log"

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(log_file),
        logging.StreamHandler()
    ]
)

logger = logging.getLogger(__name__)


class ConnectionWrapper(EWrapper):
    """Minimal wrapper for connection status"""
    
    def __init__(self):
        EWrapper.__init__(self)
        self.connected = False
        self.account_id = None
        self.managed_accounts_list = []
        self.next_valid_order_id = None
        self.connection_event = threading.Event()
        self.connect_ack_event = threading.Event()
        self.errors = []
        self.hist_data = []
        self.done_event = threading.Event()
        self.positions_data = []
        self.positions_end_event = threading.Event()
        self.account_summary_data = {}
        self.account_summary_end_event = threading.Event()
        self.filled_orders = {}
        self.contract_details = {}  # For resolving conId
        self.what_if_order_states = {}  # For margin checks
        self.what_if_events = {}  # Events for WhatIf responses
    
    def connectAck(self):
        """Called when the API connection is acknowledged."""
        self.connect_ack_event.set()
        logger.info("connectAck received")
    
    def managedAccounts(self, accountsList: str):
        """Called when connection established with account list"""
        self.managed_accounts_list = accountsList.split(",")
        self.account_id = self.managed_accounts_list[0] if self.managed_accounts_list else None
        logger.info(f"Account(s): {self.managed_accounts_list}")
        self.connected = True
        self.connection_event.set()
    
    def nextValidId(self, orderId: int):
        """Called when connection established with next order ID"""
        self.next_valid_order_id = orderId
        logger.info(f"Next Valid Order ID: {orderId}")
    
    def position(self, account: str, contract, position: float, avgCost: float):
        """Called for each position held in the account"""
        self.positions_data.append({
            "account": account,
            "symbol": contract.symbol,
            "secType": contract.secType,
            "strike": contract.strike,
            "right": contract.right,
            "expiry": contract.lastTradeDateOrContractMonth,
            "position": position,
            "avgCost": avgCost
        })
    
    def positionEnd(self):
        """Called when all positions have been transmitted"""
        logger.info("Finished fetching positions from IBKR")
        self.positions_end_event.set()
    
    def accountSummary(self, reqId: int, account: str, tag: str, value: str, currency: str):
        """Called for account summary data"""
        self.account_summary_data[tag] = value
    
    def accountSummaryEnd(self, reqId: int):
        """Called when account summary is complete"""
        self.account_summary_end_event.set()
    
    def orderStatus(self, orderId: int, status: str, filled: float, remaining: float, avgFillPrice: float, permId: int, parentId: int, lastFillPrice: float, clientId: int, whyHeld: str, mktCapPrice: float):
        """Order status callback"""
        self.filled_orders[orderId] = {
            "status": status,
            "filled": filled,
            "avg_price": avgFillPrice
        }
        logger.info(f"Order {orderId} status={status} filled={filled} avgPrice={avgFillPrice}")
    
    def openOrder(self, orderId: int, contract: Contract, order: Order, orderState):
        """
        Called when an order is opened.
        For WhatIf orders, this provides margin information.
        """
        if order.whatIf:
            self.what_if_order_states[orderId] = orderState
            event = self.what_if_events.get(orderId)
            if event:
                event.set()
            logger.info(
                f"WhatIf order {orderId}: initMarginChange={getattr(orderState, 'initMarginChange', None)}, "
                f"maintMarginChange={getattr(orderState, 'maintMarginChange', None)}"
            )
    
    def contractDetails(self, reqId: int, contractDetails):
        """Store contract details for conId resolution"""
        self.contract_details[reqId] = contractDetails
    
    def contractDetailsEnd(self, reqId: int):
        """End of contract details"""
        pass
    
    def error(self, reqId: int, errorCode: int, errorString: str):
        """Error callback"""
        msg = f"Error {errorCode}: {errorString}"
        self.errors.append((reqId, errorCode, errorString))
        
        # Completely suppress specific errors that are common or benign
        if errorCode in (200, 10167, 10090):
            return
        logger.error(msg)
    
    def connectionClosed(self):
        """Called when connection closes"""
        self.connected = False
        self.connection_event.clear()
        logger.warning("Connection closed by server")
    
    def historicalData(self, reqId, bar):
        """Store historical data"""
        self.hist_data.append({
            "Date": bar.date,
            "Open": bar.open,
            "High": bar.high,
            "Low": bar.low,
            "Close": bar.close,
            "Volume": getattr(bar, "volume", None),
            "WAP": getattr(bar, "wap", None),
            "Count": getattr(bar, "barCount", None),
        })
    
    def historicalDataEnd(self, reqId, start, end):
        """Signal end of historical data"""
        logger.info(f"HistoricalDataEnd. ReqId={reqId}")
        self.done_event.set()


class IBKRConnectionManager:
    """
    Singleton IBKR Connection Manager with auto-retry
    
    Features:
    - Attempts connection with primary client ID from config
    - Auto-retries with random client IDs (1-32) if primary fails
    - Exponential backoff between retries
    - Thread-safe singleton pattern
    - OS-aware host/port selection
    - WhatIf combo margin checking
    
    Use: IBKRConnectionManager.get_instance()
    """
    
    _instance: Optional['IBKRConnectionManager'] = None
    _lock = threading.Lock()
    
    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self):
        """Initialize singleton (only runs once)"""
        if hasattr(self, '_initialized'):
            return
        
        self._initialized = True
        
        # Get OS type
        system = platform.system()
        
        # Set default endpoints by OS
        if system == "Linux":
            default_paper_port = 4002
            default_live_port = 4001
        elif system == "Windows":
            default_paper_port = 7497
            default_live_port = 7496
        elif system == "Darwin":  # macOS
            default_paper_port = 7497
            default_live_port = 7496
        else:
            default_paper_port = 7497
            default_live_port = 7496
        
        def _env_int(name: str) -> Optional[int]:
            val = os.getenv(name)
            if val is None or val == "":
                return None
            try:
                return int(val)
            except ValueError:
                logger.warning(f"Ignoring invalid int env var {name}={val!r}")
                return None
        
        base_host = os.getenv("IBKR_HOST") or apikey_config.get("ibkr_host") or "127.0.0.1"
        self.paper_host = os.getenv("IBKR_PAPER_HOST") or apikey_config.get("ibkr_paper_host") or base_host
        self.live_host = os.getenv("IBKR_LIVE_HOST") or apikey_config.get("ibkr_live_host") or base_host
        
        base_port = _env_int("IBKR_PORT") or apikey_config.get("ibkr_port")
        self.paper_port = _env_int("IBKR_PAPER_PORT") or apikey_config.get("ibkr_paper_port") or base_port or default_paper_port
        self.live_port = _env_int("IBKR_LIVE_PORT") or apikey_config.get("ibkr_live_port") or base_port or default_live_port
        
        base_client_id = _env_int("IBKR_CLIENT_ID") or apikey_config.get("ibkr_client_id")
        self.paper_client_id = _env_int("IBKR_PAPER_CLIENT_ID") or apikey_config.get("ibkr_paper_client_id") or base_client_id or 1
        self.live_client_id = _env_int("IBKR_LIVE_CLIENT_ID") or apikey_config.get("ibkr_live_client_id") or base_client_id or 2
        
        self.trading_mode = (os.getenv("IBKR_TRADING_MODE") or apikey_config.get("ibkr_trading_mode") or "paper").lower()
        self._apply_trading_mode(self.trading_mode)
        
        # Client ID range for retry attempts (1-32, excluding primary)
        self.client_id_range = list(range(1, 32))
        
        self.wrapper = None
        self.client = None
        self.thread = None
        self.is_connected = False
        self.connect_lock = threading.Lock()
        self.current_client_id = self.primary_client_id
        self._next_req_id_counter = 10000
        
        logger.info(f"IBKRConnectionManager initialized")
        logger.info(f"  OS: {system}")
        logger.info(f"  Mode: {self.trading_mode.upper()}")
        logger.info(f"  Host: {self.host}")
        logger.info(f"  Port: {self.port}")
        logger.info(f"  Primary Client ID: {self.primary_client_id}")
    
    @classmethod
    def get_instance(cls) -> 'IBKRConnectionManager':
        """Get singleton instance"""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance
    
    def _apply_trading_mode(self, trading_mode: str):
        mode = (trading_mode or "paper").lower()
        if mode not in ("paper", "live"):
            raise ValueError(f"Unsupported IBKR trading mode: {trading_mode}")
        
        self.trading_mode = mode
        if mode == "live":
            self.host = self.live_host
            self.port = self.live_port
            self.primary_client_id = self.live_client_id
        else:
            self.host = self.paper_host
            self.port = self.paper_port
            self.primary_client_id = self.paper_client_id
        
        self.current_client_id = self.primary_client_id
    
    def set_trading_mode(self, trading_mode: str = None, is_paper: bool = None):
        """
        Select paper or live IBKR endpoint before connecting.
        """
        mode = trading_mode
        if mode is None and is_paper is not None:
            mode = "paper" if is_paper else "live"
        
        if mode is None:
            return
        
        if self.is_connected:
            current_mode = getattr(self, 'trading_mode', None)
            if current_mode and current_mode.upper() == mode.upper():
                return
            raise RuntimeError("Cannot change IBKR trading mode while connected")
        
        old_mode = getattr(self, "trading_mode", None)
        self._apply_trading_mode(mode)
        
        if old_mode != self.trading_mode:
            logger.info(
                f"IBKR trading mode set to {self.trading_mode.upper()} "
                f"({self.host}:{self.port}, client_id={self.primary_client_id})"
            )
    
    def _tcp_probe(self, host: str, port: int, timeout: float = 2.0) -> Tuple[bool, str]:
        """
        Lightweight TCP reachability check for host:port.
        
        Returns:
            (ok, message)
        """
        try:
            with socket.create_connection((host, int(port)), timeout=timeout):
                return True, f"TCP check OK: {host}:{port} is reachable"
        except socket.timeout:
            return False, f"TCP check TIMEOUT: {host}:{port} not reachable (timed out)"
        except OSError as e:
            err = getattr(e, "errno", None)
            if err is not None:
                return False, f"TCP check FAILED: {host}:{port} ({e.strerror}, errno={err})"
            return False, f"TCP check FAILED: {host}:{port} ({e})"
    
    def _log_endpoint_hints(self):
        mode = self.trading_mode.upper()
        logger.error("IBKR API endpoint appears unreachable.")
        logger.error(f"Configured endpoint: {self.host}:{self.port} (mode={mode})")
        logger.error("Common API ports (defaults):")
        logger.error("  IB Gateway: paper=4002, live=4001")
        logger.error("  TWS:       paper=7497, live=7496")
        logger.error("Overrides supported via env vars: IBKR_PAPER_HOST/PORT, IBKR_LIVE_HOST/PORT, IBKR_TRADING_MODE.")
        logger.error("If TWS/IB Gateway runs on another machine, set the host accordingly or use an SSH tunnel.")
    
    def _log_api_settings_hints(self):
        logger.error("If the port is reachable but IB API still won't connect, check in TWS/IB Gateway:")
        logger.error("  - API enabled (Enable ActiveX and Socket Clients)")
        logger.error("  - Socket port matches (paper vs live)")
        logger.error("  - Trusted IPs / localhost-only allow this client")
        logger.error("  - No other session uses the same clientId")
    
    def _try_connect(self, client_id: int, timeout: int = 5) -> bool:
        """
        Try to connect with specific client ID
        
        Args:
            client_id: Client ID to attempt
            timeout: Connection timeout in seconds
        
        Returns:
            True if successful, False otherwise
        """
        try:
            logger.info(f"Attempting connection with client_id={client_id}...")
            
            # Create new wrapper and client
            self.wrapper = ConnectionWrapper()
            self.client = EClient(self.wrapper)
            
            # Attempt connection
            self.client.connect(self.host, self.port, client_id)
            if not self.client.isConnected():
                logger.warning(
                    f"Could not open IBKR API session at {self.host}:{self.port} "
                    f"with client_id={client_id}. Check Gateway/TWS is running, "
                    "API is enabled, and the configured paper/live port is correct."
                )
                self._log_api_settings_hints()
                return False
            
            # Start message processing thread
            self.thread = threading.Thread(target=self.client.run, daemon=True)
            self.thread.start()
            
            logger.info(f"Waiting for connection confirmation (timeout={timeout}s)...")
            
            # Wait for managedAccounts and nextValidId to ensure API is fully ready
            deadline = time.time() + timeout
            connected = False
            while time.time() < deadline:
                if self.wrapper.connection_event.is_set() and self.wrapper.next_valid_order_id is not None:
                    connected = True
                    break
                time.sleep(0.05)
            
            if connected:
                self.current_client_id = client_id
                logger.info(f"✓ Successfully connected with client_id={client_id}")
                return True
            else:
                logger.warning(f"Connection timeout with client_id={client_id} after {timeout}s")
                try:
                    self.client.disconnect()
                except:
                    pass
                self._log_api_settings_hints()
                return False
        
        except Exception as e:
            logger.warning(f"Connection attempt failed with client_id={client_id}: {e}")
            try:
                if self.client:
                    self.client.disconnect()
            except:
                pass
            return False
    
    def connect(self, timeout: int = 10, force_reconnect: bool = False, max_retries: int = 5) -> bool:
        """
        Connect to IBKR with auto-retry on different client IDs
        
        Args:
            timeout: Connection timeout per attempt in seconds
            force_reconnect: Force reconnect even if already connected
            max_retries: Maximum number of retry attempts with different client IDs
        
        Returns:
            True if connected, False otherwise
        """
        with self.connect_lock:
            # Already connected
            if self.is_connected and not force_reconnect:
                logger.info("Already connected to IBKR")
                return True
            
            # Disconnect if needed
            if self.is_connected:
                self.disconnect()
            
            logger.info("=" * 70)
            logger.info(f"Connecting to IBKR at {self.host}:{self.port}")
            logger.info(f"Max retries: {max_retries}")
            logger.info("=" * 70)
            
            ok, probe_msg = self._tcp_probe(self.host, self.port, timeout=min(2.0, max(0.5, timeout / 10)))
            if ok:
                logger.info(probe_msg)
            else:
                logger.error(probe_msg)
                self._log_endpoint_hints()
                self.is_connected = False
                return False
            
            # Build list of client IDs to try
            client_ids_to_try = [self.primary_client_id]
            
            # Add random client IDs for retries (avoid duplicates)
            available_ids = [cid for cid in self.client_id_range if cid != self.primary_client_id]
            
            if available_ids:
                random_ids = random.sample(available_ids, min(max_retries - 1, len(available_ids)))
                client_ids_to_try.extend(random_ids)
            
            logger.info(f"Client IDs to try: {client_ids_to_try}")
            
            # Try each client ID
            for attempt, client_id in enumerate(client_ids_to_try, 1):
                logger.info(f"\n[Attempt {attempt}/{len(client_ids_to_try)}] client_id={client_id}")
                
                if self._try_connect(client_id, timeout=timeout):
                    self.is_connected = True
                    
                    logger.info("\n" + "=" * 70)
                    logger.info(f"✓ Successfully connected to IBKR")
                    logger.info("=" * 70)
                    logger.info(f"  Account: {self.wrapper.account_id}")
                    logger.info(f"  Managed Accounts: {self.wrapper.managed_accounts_list}")
                    logger.info(f"  Next Order ID: {self.wrapper.next_valid_order_id}")
                    logger.info(f"  Client ID Used: {self.current_client_id}")
                    logger.info("=" * 70)
                    
                    return True
                
                # Exponential backoff before retry
                if attempt < len(client_ids_to_try):
                    wait_time = 2 * attempt  # 2s, 4s, 6s, etc.
                    logger.info(f"Waiting {wait_time}s before retry...")
                    time.sleep(wait_time)
            
            logger.error("\n" + "=" * 70)
            logger.error(f"✗ Failed to connect after {len(client_ids_to_try)} attempts")
            logger.error("=" * 70)
            logger.error("Make sure TWS or IB Gateway is running on the configured host:port")
            self._log_api_settings_hints()
            
            self.is_connected = False
            return False
    
    def disconnect(self):
        """Disconnect from IBKR"""
        with self.connect_lock:
            try:
                if self.client and self.client.isConnected():
                    self.client.disconnect()
                    logger.info("Disconnecting from IBKR...")
                    time.sleep(1)
                
                self.is_connected = False
                logger.info("✓ Disconnected from IBKR")
            
            except Exception as e:
                logger.error(f"Error during disconnect: {e}")
    
    def ensure_connected(self, timeout: int = 10) -> bool:
        """
        Ensure connection is active, reconnect if needed
        
        Args:
            timeout: Connection timeout in seconds
        
        Returns:
            True if connected
        """
        if not self.is_connected:
            return self.connect(timeout=timeout)
        return True
    
    def get_all_positions(self, timeout: int = 5) -> list:
        """
        Fetch all current positions from the connected IBKR account
        """
        if not self.is_connected:
            logger.error("Cannot fetch positions: Not connected")
            return []
        
        self.wrapper.positions_data = []
        self.wrapper.positions_end_event.clear()
        
        logger.info("Requesting all positions from IBKR...")
        self.client.reqPositions()
        
        if not self.wrapper.positions_end_event.wait(timeout=timeout):
            logger.warning(f"Position fetch timed out after {timeout}s")
        
        # Cancel subscription to stop receiving updates (standard IBKR practice)
        self.client.cancelPositions()
        
        return self.wrapper.positions_data
    
    def get_margin_summary(self, timeout: int = 5) -> dict:
        """Fetch account margin and available funds from IBKR"""
        if not self.is_connected:
            logger.error("IBKR connection not ready")
            return {}
        
        self.wrapper.account_summary_data = {}
        self.wrapper.account_summary_end_event.clear()
        
        req_id = self._next_req_id_counter
        self._next_req_id_counter += 1
        
        logger.info("Requesting account margin summary...")
        self.client.reqAccountSummary(
            req_id,
            "All",
            "InitMarginReq,MaintMarginReq,AvailableFunds,ExcessLiquidity,NetLiquidation"
        )
        
        received = self.wrapper.account_summary_end_event.wait(timeout=timeout)
        if not received:
            logger.warning("Account summary timed out")
        
        self.client.cancelAccountSummary(req_id)
        
        summary = dict(self.wrapper.account_summary_data)
        logger.info(f"Margin summary: {summary}")
        return summary
    
    def resolve_option_conid(self, leg: dict, timeout: float = 5.0) -> Optional[int]:
        """
        Resolve an option leg to its IBKR contract ID (conId).
        
        Args:
            leg: Dict with symbol, secType, exchange, currency, strike, right, expiry
            timeout: Timeout in seconds
        
        Returns:
            conId (int) or None
        """
        if not self.is_connected:
            logger.error("IBKR not connected for conId resolution")
            return None
        
        client = self.get_client()
        if client is None:
            return None
        
        req_id = self._next_req_id_counter
        self._next_req_id_counter += 1
        
        contract = Contract()
        contract.symbol = leg["symbol"]
        contract.secType = "OPT"
        contract.exchange = leg.get("exchange", "CBOE")
        contract.currency = leg.get("currency", "USD")
        contract.lastTradeDateOrContractMonth = leg["expiry"]
        contract.strike = float(leg["strike"])
        contract.right = leg["right"]
        contract.multiplier = str(leg.get("multiplier", "100"))
        
        self.wrapper.contract_details[req_id] = None
        client.reqContractDetails(req_id, contract)
        
        deadline = time.time() + timeout
        while time.time() < deadline:
            details = self.wrapper.contract_details.get(req_id)
            if details:
                con_id = details.contract.conId
                logger.info(f"Resolved conId={con_id} for {leg['right']} {leg['strike']} {leg['expiry']}")
                return con_id
            time.sleep(0.05)
        
        logger.error(f"Failed to resolve conId for leg: {leg}")
        return None
    
    def check_combo_margin(
        self,
        legs: List[Dict],
        net_price: float,
        action: str = "SELL",
        quantity: int = 1,
        timeout: float = 10.0,
    ) -> dict:
        """
        Use a WhatIf combo order to preview margin impact.
        
        Args:
            legs: List of leg dicts (same format as place_combo_order)
            net_price: Net limit price for the combo
            action: "BUY" or "SELL" for the combo
            quantity: Number of combo units
            timeout: Timeout in seconds
        
        Returns:
            dict with:
                - can_trade: bool
                - init_margin_change: float
                - maint_margin_change: float
                - equity_with_loan_change: float
                - warning_text: str
                - available_funds: float
                - excess_liquidity: float
                - estimated_requirement: float
        """
        if not self.is_connected:
            return {
                "can_trade": False,
                "reason": "IBKR not connected",
            }
        
        client = self.get_client()
        wrapper = self.get_wrapper()
        if client is None or wrapper is None:
            return {
                "can_trade": False,
                "reason": "Client or wrapper not available",
            }
        
        # Build BAG contract
        combo = Contract()
        combo.symbol = legs[0]["symbol"]
        combo.secType = "BAG"
        combo.exchange = legs[0].get("exchange", "CBOE")
        combo.currency = legs[0].get("currency", "USD")
        
        combo_legs = []
        for leg in legs:
            con_id = leg.get("conId")
            if con_id is None:
                con_id = self.resolve_option_conid(leg)
                if con_id is None:
                    logger.error(f"Cannot build combo: failed to resolve conId for leg {leg}")
                    return {
                        "can_trade": False,
                        "reason": f"Failed to resolve conId for leg {leg}",
                    }
                leg["conId"] = con_id  # Cache for future use
            
            combo_leg = ComboLeg()
            combo_leg.conId = int(con_id)
            combo_leg.ratio = int(leg.get("ratio", 1))
            combo_leg.action = leg["action"].upper()
            combo_leg.exchange = leg.get("exchange", "CBOE")
            
            combo_legs.append(combo_leg)
        
        combo.comboLegs = combo_legs
        
        # Build WhatIf order
        order_id = self.get_next_order_id()
        if order_id is None:
            return {
                "can_trade": False,
                "reason": "No valid order ID",
            }
        
        event = threading.Event()
        wrapper.what_if_events[order_id] = event
        wrapper.what_if_order_states[order_id] = None
        
        order = Order()
        order.action = action.upper()
        order.totalQuantity = int(quantity)
        order.orderType = "LMT"
        order.lmtPrice = abs(float(net_price))
        order.tif = "DAY"
        order.whatIf = True  # ← KEY: margin preview only
        
        logger.info(
            f"Checking combo margin (WhatIf): action={order.action} qty={quantity} price={net_price} legs={len(combo_legs)}"
        )
        
        client.placeOrder(order_id, combo, order)
        
        received = event.wait(timeout=timeout)
        
        order_state = wrapper.what_if_order_states.pop(order_id, None)
        wrapper.what_if_events.pop(order_id, None)
        
        if not received or order_state is None:
            return {
                "can_trade": False,
                "reason": "WhatIf margin response timed out",
            }
        
        def numeric(value):
            try:
                return float(value)
            except (TypeError, ValueError):
                return None
        
        init_margin_change = numeric(getattr(order_state, "initMarginChange", None))
        maint_margin_change = numeric(getattr(order_state, "maintMarginChange", None))
        equity_with_loan_change = numeric(getattr(order_state, "equityWithLoanChange", None))
        
        account_margin = self.get_margin_summary(timeout=5)
        
        available_funds = numeric(account_margin.get("AvailableFunds"))
        excess_liquidity = numeric(account_margin.get("ExcessLiquidity"))
        
        # Conservative safety test
        estimated_requirement = max(
            abs(init_margin_change or 0.0),
            abs(maint_margin_change or 0.0),
        )
        
        safety_buffer = 0.90
        usable_funds = (
            available_funds * safety_buffer
            if available_funds is not None
            else None
        )
        
        can_trade = (
            usable_funds is not None
            and estimated_requirement <= usable_funds
        )
        
        result = {
            "can_trade": can_trade,
            "order_id": order_id,
            "init_margin_change": init_margin_change,
            "maint_margin_change": maint_margin_change,
            "equity_with_loan_change": equity_with_loan_change,
            "estimated_requirement": estimated_requirement,
            "available_funds": available_funds,
            "excess_liquidity": excess_liquidity,
            "usable_funds": usable_funds,
            "warning_text": getattr(order_state, "warningText", ""),
        }
        
        if can_trade:
            logger.info(
                f"✓ Combo margin check passed: requirement=${estimated_requirement:,.2f} available=${available_funds:,.2f}"
            )
        else:
            logger.warning(
                f"✗ Combo margin check failed: requirement=${estimated_requirement:,.2f} "
                f"available=${available_funds:,.2f} warning={result['warning_text']}"
            )
        
        return result
    
    def wait_for_fill(self, order_id: int, timeout: float = 10.0) -> bool:
        """Block until order is Filled or timeout expires."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            status = self.wrapper.filled_orders.get(order_id, {}).get("status", "")
            if status == "Filled":
                return True
            time.sleep(0.2)
        logger.warning(f"Order {order_id} did not fill within {timeout}s")
        return False
    
    def cancel_order(self, order_id: int):
        if self.is_connected:
            self.client.cancelOrder(order_id)
            logger.info(f"Cancelled order {order_id}")
    
    def get_wrapper(self) -> Optional[ConnectionWrapper]:
        """Get wrapper instance (for callbacks)"""
        if self.is_connected:
            return self.wrapper
        return None
    
    def get_client(self) -> Optional[EClient]:
        """Get EClient instance"""
        if self.is_connected:
            return self.client
        return None
    
    def get_account(self) -> Optional[str]:
        """Get current account ID"""
        if self.is_connected and self.wrapper:
            return self.wrapper.account_id
        return None
    
    def get_next_order_id(self) -> Optional[int]:
        """Get next valid order ID"""
        if self.is_connected and self.wrapper:
            return self.wrapper.next_valid_order_id
        return None
    
    def get_current_client_id(self) -> int:
        """Get current client ID being used"""
        return self.current_client_id
    
    def get_primary_client_id(self) -> int:
        """Get primary client ID from config"""
        return self.primary_client_id
    
    def get_connection_info(self) -> dict:
        """Get current connection information"""
        if not self.is_connected:
            return {
                "connected": False,
                "mode": self.trading_mode,
                "host": self.host,
                "port": self.port,
                "primary_client_id": self.primary_client_id,
            }
        
        return {
            "connected": self.is_connected,
            "mode": self.trading_mode,
            "host": self.host,
            "port": self.port,
            "primary_client_id": self.primary_client_id,
            "current_client_id": self.current_client_id,
            "account": self.wrapper.account_id if self.wrapper else None,
            "managed_accounts": self.wrapper.managed_accounts_list if self.wrapper else [],
            "next_order_id": self.wrapper.next_valid_order_id if self.wrapper else None,
            "errors": len(self.wrapper.errors) if self.wrapper else 0
        }
    
    def print_connection_status(self):
        """Print formatted connection status"""
        info = self.get_connection_info()
        
        logger.info("=" * 70)
        logger.info("IBKR CONNECTION STATUS")
        logger.info("=" * 70)
        
        if info.get("connected"):
            logger.info(f"Status:             ✓ CONNECTED")
            logger.info(f"Mode:               {info['mode'].upper()}")
            logger.info(f"Host:               {info['host']}:{info['port']}")
            logger.info(f"Primary Client ID:  {info['primary_client_id']}")
            logger.info(f"Current Client ID:  {info['current_client_id']}")
            logger.info(f"Account:            {info['account']}")
            logger.info(f"Managed Accts:      {', '.join(info['managed_accounts'])}")
            logger.info(f"Next Order ID:      {info['next_order_id']}")
            if info['errors']:
                logger.info(f"Errors:             {info['errors']}")
        else:
            logger.info(f"Status:             ✗ NOT CONNECTED")
            logger.info(f"Mode:               {info['mode'].upper()}")
            logger.info(f"Host:               {info['host']}:{info['port']}")
            logger.info(f"Primary Client ID:  {info['primary_client_id']}")
        
        logger.info("=" * 70)


# Convenience function to get singleton
def get_ibkr_connection(is_paper: bool = None, trading_mode: str = None) -> IBKRConnectionManager:
    """
    Get the singleton IBKR connection manager
    
    Example:
        conn_mgr = get_ibkr_connection(is_paper=True)
        if conn_mgr.connect(timeout=10, max_retries=5):
            account = conn_mgr.get_account()
            client_id = conn_mgr.get_current_client_id()
            print(f"Connected to {account} with client_id={client_id}")
        else:
            print("Connection failed")
    """
    conn_mgr = IBKRConnectionManager.get_instance()
    conn_mgr.set_trading_mode(trading_mode=trading_mode, is_paper=is_paper)
    return conn_mgr