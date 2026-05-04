import logging
import sys
from pathlib import Path
from datetime import datetime, time
import pandas as pd
import time as time_module
import random

logger = logging.getLogger(__name__)

# Add backtest to path
# Add both current directory and parent directory to path
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))


from IBKRConnectionManager import get_ibkr_connection
from paper_trading import PaperTrader
from config import LOG_PATH

# Setup logging
LOG_PATH = Path(LOG_PATH)
LOG_PATH.mkdir(parents=True, exist_ok=True)
timestamp = datetime.now().strftime("%Y%m%d")
log_file = LOG_PATH / f"run_paper_trader_{timestamp}.log"

def setup_logging():
    """Configure logging to write to both console and a file."""
    
    

    formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.handlers.clear()

    file_handler = logging.FileHandler(log_file)
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setLevel(logging.INFO)
    stream_handler.setFormatter(formatter)

    root_logger.addHandler(file_handler)
    root_logger.addHandler(stream_handler)

    for noisy_logger in ("ibapi.client", "ibapi.wrapper"):
        logging.getLogger(noisy_logger).setLevel(logging.WARNING)

    logger.info(f"Logging to {log_file}")


def is_market_hours():
    """Check if market is open (9:30 AM - 4:00 PM ET)"""
    now = datetime.now().time()
    return time(9, 30) <= now <= time(21, 0)  # 9:30 AM - 4:00 PM ET


def run_paper_trader():
    """Main paper trading loop"""
    setup_logging()
    
    logger.info("=" * 70)
    logger.info("SPX PAPER TRADING")
    logger.info("=" * 70)
    
    # Get connection manager
    conn_mgr = get_ibkr_connection(is_paper=True)
    trader = None  # <-- INITIALIZE TO NONE
    
    try:
        # Connect to IBKR
        logger.info("Attempting to connect to IBKR...")
        if not conn_mgr.connect(timeout=10, max_retries=5):
            logger.error("Failed to connect to IBKR")
            return
        
        logger.info(f"✓ Connected! Account: {conn_mgr.get_account()}")
        logger.info(f"  Client ID: {conn_mgr.get_current_client_id()}")
        
        # Verify connection
        conn_mgr.print_connection_status()
        conn_mgr.ensure_connected()
        
        info = conn_mgr.get_connection_info()
        logger.info(f"  Primary ID: {info['primary_client_id']}, Current ID: {info['current_client_id']}")
        
        # Initialize paper trader with connection manager
        logger.info("\nInitializing paper trader...")
        trader = PaperTrader(conn_mgr=conn_mgr, is_paper=True)  # <-- NOW HAS conn_mgr PARAMETER
        
        logger.info("✓ Paper trader started. Monitoring SPX option positions...")
        logger.info("=" * 70 + "\n")
        
        positions_opened_today = False
        last_open_time = None
        cycle_count = 0
        
        while True:
            try:
                # Check market hours
                # if not is_market_hours():
                #     logger.debug(f"Market closed ({datetime.now().strftime('%H:%M:%S')}). Waiting...")
                #     time_module.sleep(60)
                #     positions_opened_today = False
                #     continue
                
                # Ensure connection is alive
                if not conn_mgr.is_connected:
                    logger.warning("Connection lost. Attempting to reconnect...")
                    conn_mgr.ensure_connected()
                    if not conn_mgr.is_connected:
                        logger.error("Failed to reconnect. Retrying in 30 seconds...")
                        time_module.sleep(30)
                        continue
                
                # Only open positions once per day
                if len(trader.live_positions) == 0 and not positions_opened_today:
                    cycle_count += 1
                    logger.info(f"\n[Cycle {cycle_count}] Opening new positions...")
                    
                    try:
                        # Fetch live option chain
                        option_chain = trader.get_live_option_chain(
                            underlying="SPX",
                        )
                        
                        if option_chain.empty:
                            logger.warning("No option chain data. Retrying in 30 seconds...")
                            time_module.sleep(30)
                            continue
                        
                        logger.info(f"✓ Option chain loaded: {len(option_chain)} strikes")
                        
                        # Select and open positions
                        trader.select_and_open_positions(option_chain)
                        positions_opened_today = True
                        last_open_time = pd.Timestamp.now()
                        
                        logger.info(f"✓ Positions opened at {last_open_time.strftime('%H:%M:%S')}")
                    
                    except Exception as open_error:
                        logger.error(f"Error opening positions: {open_error}", exc_info=True)
                        time_module.sleep(30)
                        continue
                
                # Monitor and adjust open positions every 5 minutes
                if positions_opened_today and len(trader.live_positions) > 0:
                    logger.info(f"\n[{datetime.now().strftime('%H:%M:%S')}] Monitoring {len(trader.live_positions)} open positions...")
                    
                    try:
                        # Fetch current option chain
                        option_chain = trader.get_live_option_chain(
                            underlying="SPX",
                        )
                        
                        if not option_chain.empty:
                            # Monitor and adjust
                            trader.monitor_and_adjust(option_chain)
                            
                            # Check each position for expiry
                            for cycle_id, pos_data in list(trader.live_positions.items()):
                                if pos_data["status"] == "OPEN":
                                    entry_time = pd.Timestamp(pos_data["entry_time"])
                                    elapsed_time = (pd.Timestamp.now() - entry_time).total_seconds()
                                    dte_remaining = elapsed_time / (24 * 3600)
                                    
                                    logger.info(f"  Cycle {cycle_id}: DTE remaining = {dte_remaining:.2f} days, Status = {pos_data['status']}")
                                    
                                    # Close if past expiry or near end-of-day (3 PM ET)
                                    close_hour = 15
                                    current_hour = datetime.now().hour
                                    
                                    if current_hour >= close_hour:
                                        logger.info(f"  Closing cycle {cycle_id} (end of trading day)")
                                        trader.close_positions_at_expiry(cycle_id, option_chain=option_chain, reason="eod")
                                        positions_opened_today = False
                        else:
                            logger.warning("Could not fetch option chain for monitoring")
                    
                    except Exception as monitor_error:
                        logger.error(f"Error monitoring positions: {monitor_error}", exc_info=True)
                
                # Sleep before next check (5 minute intervals during market hours)
                logger.debug("Waiting 5 minutes before next check...")
                time_module.sleep(300)
            
            except KeyboardInterrupt:
                logger.info("\nShutting down paper trader (Ctrl+C)...")
                break
            
            except Exception as loop_error:
                logger.error(f"Error in main loop: {loop_error}", exc_info=True)
                logger.info("Retrying in 60 seconds...")
                time_module.sleep(60)
    
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
    
    finally:
        logger.info("\nCleaning up...")
        
        # Save trade log if trader was created
        if trader is not None:  # <-- CHECK IF TRADER WAS INITIALIZED
            try:
                log_file = trader.save_trade_log()
                if log_file:
                    logger.info("✓ Trade log saved")
                
                # Print summary
                summary = trader.get_position_summary()
                logger.info(f"\nSession Summary: {summary}")
            
            except Exception as save_error:
                logger.error(f"Error saving trade log: {save_error}")
        
        try:
            # Disconnect from IBKR
            conn_mgr.disconnect()
            logger.info("✓ IBKR disconnected")
        except Exception as disc_error:
            logger.error(f"Error during disconnect: {disc_error}")
        
        logger.info("=" * 70)
        logger.info("Paper trader stopped")
        logger.info("=" * 70)


if __name__ == "__main__":
    run_paper_trader()
