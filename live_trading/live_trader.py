import logging
import time
import pandas as pd
from pathlib import Path
from datetime import datetime, time as dt_time
from paper_trading import PaperTrader
from ibkr_client import IBKRClient
from chain_builder import ChainBuilder
from config import STRATEGY_CONFIG, LOG_PATH, DATA_PATH, PROCESSED_PATH
from config import apikey_config


def run_live_trading(is_paper: bool = True):
    """Main live/paper trading loop"""
    
    # Setup logging
    log_file = Path(LOG_PATH) / f"live_trading_{pd.Timestamp.now().strftime('%Y%m%d_%H%M%S')}.log"
    logging.basicConfig(
        filename=log_file,
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )
    logger = logging.getLogger(__name__)
    logger.info(f"Starting live trading (Paper={is_paper})")

    mode = apikey_config["ibkr_trading_mode"]

    client = IBKRClient(
        host=apikey_config[f"ibkr_{mode}_host"],
        port=apikey_config[f"ibkr_{mode}_port"],
        client_id=apikey_config[f"ibkr_{mode}_client_id"],
        is_paper=is_paper
    )
    client.connect()
    
    # Initialize paper trader
    trader = PaperTrader(client, is_paper=is_paper)
    
    # Initialize chain builder for live data
    builder = ChainBuilder()
    
    # Trading parameters
    market_open = dt_time(10, 0)  # 10:00 ET
    market_close = dt_time(15, 45)  # 15:45 ET
    rebalance_interval = 300  # 5 minutes in seconds
    
    positions_opened = False
    last_rebalance = time.time()
    
    try:
        while True:
            now = datetime.now().time()
            
            # Skip if outside market hours
            if now < market_open or now > market_close:
                logger.debug(f"Outside market hours: {now}")
                time.sleep(60)
                continue
            
            # Open positions at market open
            if not positions_opened and now >= market_open:
                logger.info("Opening positions at market open")
                
                # Fetch live option chain
                option_chain = trader.get_live_option_chain("SPX")
                
                if not option_chain.empty:
                    trader.select_and_open_positions(option_chain)
                    positions_opened = True
                else:
                    logger.warning("Failed to fetch option chain")
            
            # Rebalance every 5 minutes
            if time.time() - last_rebalance > rebalance_interval and positions_opened:
                logger.info("Running rebalance check")
                
                option_chain = trader.get_live_option_chain("SPX")
                if not option_chain.empty:
                    trader.monitor_and_adjust(option_chain)
                
                last_rebalance = time.time()
            
            # Close positions at market close
            if positions_opened and now >= market_close:
                logger.info("Closing positions at market close")
                
                for cycle_id in trader.live_positions.keys():
                    if trader.live_positions[cycle_id]["status"] == "OPEN":
                        trader.close_positions_at_expiry(cycle_id)
                
                trader.save_trade_log()
                positions_opened = False
            
            time.sleep(10)  # Check every 10 seconds
    
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
    finally:
        logger.info("Shutting down")
        client.disconnect()

if __name__ == "__main__":
    # Run in paper trading mode
    run_live_trading(is_paper=False)