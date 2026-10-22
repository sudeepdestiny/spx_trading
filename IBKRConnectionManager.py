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
from typing import Optional, Tuple, List, Dict, Any
import math

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
# lofgging.getLogger('IBKRConnectionManager').setLevel(logging.WARNING)

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
        self.margin_lock = threading.RLock()
        self.order_id_lock = threading.Lock()
        self.what_if_order_states = {}
        self.what_if_events = {}
        self.contract_details = {}
        self.contract_details_events = {}
        self.request_errors = {}

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
    
    def openOrder(self, orderId, contract, order, orderState):
        
        # Log before any filter or early return.
        logger.info(
            "openOrder received: wrapper=%s orderId=%s "
            "whatIf=%r status=%r initMarginAfter=%r "
            "warning=%r",
            id(self),
            orderId,
            getattr(order, "whatIf", None),
            getattr(orderState, "status", None),
            getattr(orderState, "initMarginAfter", None),
            getattr(orderState, "warningText", None),
        )

        with self.margin_lock:
            event = self.what_if_events.get(orderId)

            logger.info(
                "openOrder matching: orderId=%s event_found=%s "
                "pending_ids=%s",
                orderId,
                event is not None,
                list(self.what_if_events),
            )

            # Match requests explicitly registered as previews.
            if event is None:
                return

            if not getattr(order, "whatIf", False):
                self.request_errors[orderId] = (
                    "Received an unexpected non-what-if response "
                    "for a registered margin-preview request"
                )
            else:
                self.what_if_order_states[orderId] = orderState

        event.set()

    
    def contractDetails(self, reqId, contractDetails):
        with self.margin_lock:
            if reqId in self.contract_details:
                self.contract_details[reqId].append(contractDetails)

    def contractDetailsEnd(self, reqId):
        with self.margin_lock:
            event = self.contract_details_events.get(reqId)
            if event:
                event.set()
    
    def error(self, reqId, *args):
        if len(args) >= 3 and isinstance(args[1], int):
            # Newer API: errorTime, errorCode, errorString, ...
            error_code = args[1]
            error_string = args[2]
        elif len(args) >= 2:
            # Older API: errorCode, errorString, ...
            error_code = args[0]
            error_string = args[1]
        else:
            return
        message = f"IBKR {error_code}: {error_string}"
        self.errors.append((reqId, error_code, str(error_string)))
        with self.margin_lock:
            events = [
                self.what_if_events.get(reqId),
                self.contract_details_events.get(reqId),
            ]
            if any(event is not None for event in events):
                # Conservatively block the preview on request-specific messages.
                self.request_errors[reqId] = message
                for event in events:
                    if event:
                        event.set()
        logger.error("Request %s: %s", reqId, message)
    
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
        self._request_id_lock = threading.Lock()
        self.margin_preview_lock = threading.RLock()
        
        logger.info(f"IBKRConnectionManager initialized")
        logger.info(f"  OS: {system}")
        logger.info(f"  Mode: {self.trading_mode.upper()}")
        logger.info(f"  Host: {self.host}")
        logger.info(f"  Port: {self.port}")
        logger.info(f"  Primary Client ID: {self.primary_client_id}")

    def allocate_request_id(self):
        with self._request_id_lock:
            req_id = self._next_req_id_counter
            self._next_req_id_counter += 1
            return req_id
    
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
            # Note: ibapi's EClient.connect() does not return a success boolean (returns None).
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

        req_id = self.allocate_request_id()

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
    
    def resolve_option_conid(self, leg, timeout=10.0):
        if not self.is_connected:
            raise RuntimeError("IBKR is not connected")
        wrapper = self.get_wrapper()
        client = self.get_client()
        contract = Contract()
        contract.symbol = leg["symbol"]
        contract.secType = "OPT"
        contract.exchange = leg.get("exchange", "SMART")
        contract.currency = leg.get("currency", "USD")
        contract.lastTradeDateOrContractMonth = leg["expiry"]
        contract.strike = float(leg["strike"])
        contract.right = leg["right"]
        contract.multiplier = str(leg.get("multiplier", "100"))
        # Require an explicit trading class to avoid SPX/SPXW ambiguity.
        contract.tradingClass = leg["tradingClass"]

        req_id = self.allocate_request_id()
        event = threading.Event()
        with wrapper.margin_lock:
            wrapper.contract_details[req_id] = []
            wrapper.contract_details_events[req_id] = event
        try:
            client.reqContractDetails(req_id, contract)
            if not event.wait(timeout):
                raise TimeoutError(
                    f"Contract resolution timed out: {leg}"
                )
            with wrapper.margin_lock:
                error = wrapper.request_errors.get(req_id)
                matches = list(wrapper.contract_details[req_id])
            if error:
                raise RuntimeError(error)
            if len(matches) != 1:
                raise ValueError(
                    f"Expected one exact contract; got {len(matches)}: {leg}"
                )
            resolved = matches[0].contract
            if resolved.conId <= 0:
                raise ValueError(f"Invalid conId for {leg}")
            return resolved.conId
        finally:
            with wrapper.margin_lock:
                wrapper.contract_details.pop(req_id, None)
                wrapper.contract_details_events.pop(req_id, None)
                wrapper.request_errors.pop(req_id, None)

    def check_combo_margin(
        self,
        legs,
        net_price,
        action="BUY",
        quantity=1,
        timeout=20.0,
        account=None,
        min_initial_headroom=0.0,
        min_maintenance_headroom=0.0,
    ):
        """
        Preview only. Leg actions describe the position obtained by BUYING the BAG.
        With that convention:
        BUY + negative price = receive credit.
        BUY + positive price = pay debit.
        Returned margin amounts are in account base currency.
        """
        def margin_value(state, name):
            raw = getattr(state, name, "")
            value = float(raw)
            if not math.isfinite(value) or abs(value) >= 1e100:
                raise ValueError(f"Unavailable margin field {name}: {raw!r}")
            return value

        order_id = None
        wrapper = None
        try:
            with self.margin_preview_lock:
                if not self.is_connected:
                    raise RuntimeError("IBKR not connected")
                if not legs:
                    raise ValueError("No combination legs supplied")
                if type(quantity) is not int or quantity < 1:
                    raise ValueError("Quantity must be a positive integer")
                if action != "BUY":
                    raise ValueError(
                        "Use BUY with explicit leg directions and signed price"
                    )
                if not math.isfinite(float(net_price)):
                    raise ValueError("Invalid combination price")

                account = account or self.get_account()
                wrapper = self.get_wrapper()
                client = self.get_client()
                if account not in wrapper.managed_accounts_list:
                    raise ValueError("Account unavailable to this connection")

                symbols = {leg["symbol"] for leg in legs}
                currencies = {leg.get("currency", "USD") for leg in legs}
                expiries = {leg["expiry"] for leg in legs}
                if len(symbols) != 1 or len(currencies) != 1:
                    raise ValueError("Mixed symbols/currencies unsupported here")
                if len(expiries) != 1:
                    raise ValueError("This preview expects a same-expiry package")

                combo = Contract()
                combo.symbol = next(iter(symbols))
                combo.secType = "BAG"
                combo.exchange = "SMART"
                combo.currency = next(iter(currencies))
                combo.comboLegs = []

                for leg in legs:
                    leg_action = leg["action"].upper()
                    if leg_action not in ("BUY", "SELL"):
                        raise ValueError("Invalid leg action")
                    ratio = leg.get("ratio", 1)
                    if type(ratio) is not int or ratio < 1:
                        raise ValueError("Invalid leg ratio")

                    # Resolve from exact contract details rather than trusting
                    # a potentially stale conId on a reused dictionary.
                    con_id = self.resolve_option_conid(leg, timeout=timeout)
                    combo_leg = ComboLeg()
                    combo_leg.conId = con_id
                    combo_leg.ratio = ratio
                    combo_leg.action = leg_action
                    combo_leg.exchange = "SMART"
                    combo.comboLegs.append(combo_leg)

                order = Order()
                order.account = account
                order.action = "BUY"
                order.totalQuantity = quantity
                order.orderType = "LMT"
                order.lmtPrice = float(net_price)  # Do not use abs().
                order.tif = "DAY"
                order.whatIf = True
                order.transmit = True
                order.eTradeOnly = False
                order.firmQuoteOnly = False

                order_id = self.get_next_order_id()
                event = threading.Event()
                with wrapper.margin_lock:
                    wrapper.what_if_events[order_id] = event

                client.placeOrder(order_id, combo, order)
                if not event.wait(timeout):
                    raise TimeoutError("IBKR margin preview timed out")

                with wrapper.margin_lock:
                    error = wrapper.request_errors.get(order_id)
                    state = wrapper.what_if_order_states.get(order_id)
                if error:
                    raise RuntimeError(error)
                if state is None:
                    raise ValueError("IBKR returned no margin state")

                fields = [
                    "initMarginBefore",
                    "initMarginChange",
                    "initMarginAfter",
                    "maintMarginBefore",
                    "maintMarginChange",
                    "maintMarginAfter",
                    "equityWithLoanBefore",
                    "equityWithLoanChange",
                    "equityWithLoanAfter",
                ]
                values = {
                    name: margin_value(state, name)
                    for name in fields
                }
                initial_headroom = (
                    values["equityWithLoanAfter"] - values["initMarginAfter"]
                )
                maintenance_headroom = (
                    values["equityWithLoanAfter"] - values["maintMarginAfter"]
                )
                warning = getattr(state, "warningText", "") or ""
                can_trade = (
                    initial_headroom > float(min_initial_headroom)
                    and maintenance_headroom > float(min_maintenance_headroom)
                    and not warning.strip()
                )
                return {
                    "can_trade": can_trade,
                    "account": account,
                    "preview_only": True,
                    "reason": (
                        "Configured margin checks passed"
                        if can_trade
                        else "Insufficient headroom or broker warning"
                    ),
                    "warning_text": warning,
                    "init_margin_change": values["initMarginChange"],
                    "maint_margin_change": values["maintMarginChange"],
                    "equity_with_loan_change": values["equityWithLoanChange"],
                    "initial_headroom": initial_headroom,
                    "maintenance_headroom": maintenance_headroom,
                    **values,
                }
        except Exception as exc:
            logger.exception("Combination margin preview failed")
            return {
                "can_trade": False,
                "preview_only": True,
                "reason": str(exc),
            }
        finally:
            if wrapper is not None and order_id is not None:
                with wrapper.margin_lock:
                    wrapper.what_if_events.pop(order_id, None)
                    wrapper.what_if_order_states.pop(order_id, None)
                    wrapper.request_errors.pop(order_id, None)

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

    def get_next_order_id(self):
        wrapper = self.get_wrapper()
        if wrapper is None:
            raise RuntimeError("IBKR wrapper unavailable")
        with wrapper.order_id_lock:
            order_id = wrapper.next_valid_order_id
            if order_id is None:
                raise RuntimeError("IBKR next valid order ID unavailable")
            wrapper.next_valid_order_id += 1
            return order_id
    
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
