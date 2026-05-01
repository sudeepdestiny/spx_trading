"""
Singleton IBKR Connection Manager with auto-retry on different client IDs
"""

import threading
import time
import logging
import random
import platform
from pathlib import Path
from datetime import datetime
from typing import Optional

try:
    from ibapi.client import EClient
    from ibapi.wrapper import EWrapper
except ImportError:
    raise ImportError("ibapi not installed. Install with: pip install ibapi")

from config import apikey_config, LOG_PATH


# Setup logging
LOG_PATH = Path(LOG_PATH)
LOG_PATH.mkdir(parents=True, exist_ok=True)
timestamp = datetime.now().strftime("%Y%m%d")
log_file = LOG_PATH / f"ibkr_connection_{timestamp}.log"

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
        self.errors = []
        self.hist_data = []
        self.done_event = threading.Event()
    
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
    
    def error(self, reqId: int, errorCode: int, errorString: str):
        """Error callback"""
        msg = f"Error {errorCode}: {errorString}"
        self.errors.append((reqId, errorCode, errorString))
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
        
        # Set host based on OS (default to localhost)
        if system == "Linux":
            self.host = "127.0.0.1"
            self.port = 4002  # Paper trading port on Linux
        elif system == "Windows":
            self.host = "127.0.0.1"
            self.port = 7497  # Paper trading port on Windows
        elif system == "Darwin":  # macOS
            self.host = "127.0.0.1"
            self.port = 7497  # Paper trading port on macOS
        else:
            self.host = "127.0.0.1"
            self.port = 7497
        
        # Override with config if provided
        if apikey_config.get("ibkr_host"):
            self.host = apikey_config.get("ibkr_host")
        if apikey_config.get("ibkr_port"):
            self.port = apikey_config.get("ibkr_port")
        
        # Get primary client ID from config
        self.primary_client_id = apikey_config.get("ibkr_client_id", 1)
        
        # Client ID range for retry attempts (1-32, excluding primary)
        self.client_id_range = list(range(1, 32))
        
        self.wrapper = None
        self.client = None
        self.thread = None
        self.is_connected = False
        self.connect_lock = threading.Lock()
        self.current_client_id = self.primary_client_id
        
        logger.info(f"IBKRConnectionManager initialized")
        logger.info(f"  OS: {system}")
        logger.info(f"  Host: {self.host}")
        logger.info(f"  Port: {self.port}")
        logger.info(f"  Primary Client ID: {self.primary_client_id}")
    
    @classmethod
    def get_instance(cls) -> 'IBKRConnectionManager':
        """Get singleton instance"""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance
    
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
            
            # Start message processing thread
            self.thread = threading.Thread(target=self.client.run, daemon=True)
            self.thread.start()
            
            logger.info(f"Waiting for connection confirmation (timeout={timeout}s)...")
            
            # Wait for managedAccounts callback
            connected = self.wrapper.connection_event.wait(timeout=timeout)
            
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
            return {"connected": False}
        
        return {
            "connected": self.is_connected,
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
        
        logger.info("=" * 70)


# Convenience function to get singleton
def get_ibkr_connection() -> IBKRConnectionManager:
    """
    Get the singleton IBKR connection manager
    
    Example:
        conn_mgr = get_ibkr_connection()
        if conn_mgr.connect(timeout=10, max_retries=5):
            account = conn_mgr.get_account()
            client_id = conn_mgr.get_current_client_id()
            print(f"Connected to {account} with client_id={client_id}")
        else:
            print("Connection failed")
    """
    return IBKRConnectionManager.get_instance()