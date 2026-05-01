import logging
import sys
from pathlib import Path
from datetime import datetime, time
import pandas as pd
import time as time_module
import random

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Add backtest to path
sys.path.insert(0, str(Path(__file__).parent.parent / "backtest"))

from ibkr_client import IBKRClient
from paper_trading import PaperTrader

def is_market_hours():
    """Check if market is open (9:30 AM - 4:00 PM ET)"""
    now = datetime.now().time()
    return time(9, 30) <= now <= time(18, 0)  # Corrected to 16:00 (4 PM)

def run_paper_trader():
    """Main paper trading loop"""
    
    # Use unique client ID (avoid conflicts with other connections)
    unique_client_id = random.randint(1, 32767)
    logger.info(f"Using client ID: {unique_client_id}")
    
    try:
        # Initialize IBKR client with unique ID
        ibkr = IBKRClient(
            host="127.0.0.1",
            port=7497,  # Paper trading port
            client_id=unique_client_id,
            is_paper=True
        )
        
        logger.info("Attempting to connect to IBKR TWS...")
        ibkr.connect()
        
        # Wait for ready state
        ibkr.wait_for_ready(timeout=10)
        
        trader = PaperTrader(ibkr, is_paper=True)
        trader.connect()
        
        logger.info("✓ Paper trader started. Monitoring SPX option positions...")
        
        positions_opened_today = False
        last_open_time = None
        
        while True:
            try:
                if not is_market_hours():
                    logger.debug("Market closed. Waiting...")
                    time_module.sleep(60)
                    positions_opened_today = False  # Reset for next day
                    continue
                
                # Only open positions once per day
                if len(trader.live_positions) == 0 and not positions_opened_today:
                    logger.info("Opening new cycle positions...")
                    
                    # Fetch live option chain
                    option_chain = trader.get_live_option_chain("SPX", strike_range=(6800, 7200))
                    
                    if option_chain.empty:
                        logger.warning("No option chain data. Retrying...")
                        time_module.sleep(30)
                        continue
                    
                    logger.info(f"Option chain loaded: {len(option_chain)} strikes")
                    trader.select_and_open_positions(option_chain)
                    positions_opened_today = True
                    last_open_time = pd.Timestamp.now()
                
                # Monitor and adjust open positions every 5 minutes
                if positions_opened_today:
                    logger.info("Checking positions for adjustments...")
                    
                    option_chain = trader.get_live_option_chain("SPX", strike_range=(6800, 7200))
                    if not option_chain.empty:
                        trader.monitor_and_adjust(option_chain)
                    
                    # Check for expiry closures
                    for cycle_id, pos_data in list(trader.live_positions.items()):
                        if pos_data["status"] == "OPEN":
                            entry_time = pd.Timestamp(pos_data["entry_time"])
                            dte_remaining = (pd.Timestamp.now() - entry_time).total_seconds() / (24 * 3600)
                            
                            logger.info(f"Cycle {cycle_id}: DTE remaining = {dte_remaining:.2f} days")
                            
                            # Close if past expiry or near end-of-day
                            close_hour = 15  # 3 PM ET
                            if dte_remaining > 5 or datetime.now().hour >= close_hour:
                                logger.info(f"Closing cycle {cycle_id} (DTE={dte_remaining:.2f})")
                                trader.close_positions_at_expiry(cycle_id)
                
                # Sleep before next check (5 minute intervals)
                time_module.sleep(300)
            
            except Exception as loop_error:
                logger.error(f"Error in main loop: {loop_error}", exc_info=True)
                time_module.sleep(60)
    
    except KeyboardInterrupt:
        logger.info("Shutting down paper trader (Ctrl+C)...")
        trader.save_trade_log()
    
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
    
    finally:
        try:
            ibkr.disconnect()
            logger.info("IBKR disconnected")
        except Exception as disc_error:
            logger.error(f"Error during disconnect: {disc_error}")

if __name__ == "__main__":
    run_paper_trader()