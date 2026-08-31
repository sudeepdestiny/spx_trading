import logging
import sys
from pathlib import Path
from datetime import datetime, time
import pandas as pd
import time as time_module
import pytz
from datetime import datetime, time

logger = logging.getLogger(__name__)

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

from IBKRConnectionManager import get_ibkr_connection
from paper_trading import PaperTrader
from config import LOG_PATH

# Setup logging
LOG_PATH = Path(LOG_PATH)
LOG_PATH.mkdir(parents=True, exist_ok=True)
timestamp = datetime.now().strftime("%Y%m%d")
log_file = LOG_PATH / f"live_trader_{timestamp}.log"


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
    """
    Check if market is open (9:30 AM - 4:00 PM ET) adjusted for server timezone.
    """
    # Define US Eastern Time market hours
    eastern = pytz.timezone("US/Eastern")
    market_open = eastern.localize(datetime.combine(datetime.now(eastern).date(), time(9, 30)))
    market_close = eastern.localize(datetime.combine(datetime.now(eastern).date(), time(16, 0)))

    # Get current US Eastern time
    now_eastern_time = datetime.now(eastern)

    # Check if current time is within market hours
    return market_open.time() <= now_eastern_time.time() <= market_close.time()


def run_live_trading(is_paper: bool = False):
    """Main live trading loop"""
    setup_logging()

    logger.info("=" * 70)
    logger.info(f"SPX {'PAPER' if is_paper else 'LIVE'} TRADING")
    logger.info("=" * 70)

    # Get connection manager and set to live (or paper) mode
    conn_mgr = get_ibkr_connection(is_paper=is_paper)
    trader = None

    try:
        # Connect to IBKR
        logger.info(f"Attempting to connect to IBKR ({'paper' if is_paper else 'live'})...")
        if not conn_mgr.connect(timeout=10, max_retries=5):
            logger.error("Failed to connect to IBKR")
            return

        logger.info(f"✓ Connected! Account: {conn_mgr.get_account()}")
        logger.info(f"  Client ID: {conn_mgr.get_current_client_id()}")

        conn_mgr.print_connection_status()
        conn_mgr.ensure_connected()

        info = conn_mgr.get_connection_info()
        logger.info(f"  Primary ID: {info['primary_client_id']}, Current ID: {info['current_client_id']}")

        # Check margin before trading
        margin = conn_mgr.get_margin_summary()
        available = float(margin.get("AvailableFunds", 0))
        excess = float(margin.get("ExcessLiquidity", 0))

        logger.info(f"Available funds: ${available:,.0f} | Excess liquidity: ${excess:,.0f}")

        # if excess < 10000:  # Safety threshold
        #     logger.error("Insufficient margin — aborting")
        #     conn_mgr.disconnect()
        #     import sys as _sys
        #     _sys.exit(1)

        # Initialize trader
        logger.info("\nInitializing trader...")
        trader = PaperTrader(conn_mgr=conn_mgr, is_paper=is_paper)

        logger.info("✓ Trader started. Monitoring SPX option positions...")
        logger.info("=" * 70 + "\n")

        positions_opened_today = False
        last_open_time = None
        cycle_count = 0

        while True:
            try:
                # Check market hours
                if not is_market_hours():
                    logger.info(f"Market closed ({datetime.now().strftime('%H:%M:%S')}). Exiting trading session.")
                    break

                # Ensure connection is alive
                if not conn_mgr.is_connected:
                    logger.warning("Connection lost. Attempting to reconnect...")
                    conn_mgr.ensure_connected()
                    if not conn_mgr.is_connected:
                        logger.error("Failed to reconnect. Retrying in 30 seconds...")
                        time_module.sleep(30)
                        continue

                # Only open positions once per day
                if not trader.has_open_positions() and not positions_opened_today:
                    cycle_count += 1
                    logger.info(f"\n[Cycle {cycle_count}] Opening new positions...")

                    try:
                        option_chain = trader.get_live_option_chain(underlying="SPX")

                        if option_chain.empty:
                            logger.warning("No option chain data. Retrying in 30 seconds...")
                            time_module.sleep(30)
                            continue

                        logger.info(f"✓ Option chain loaded: {len(option_chain)} strikes")

                        opened_positions = trader.select_and_open_positions(option_chain)
                        if opened_positions:
                            positions_opened_today = True
                            last_open_time = pd.Timestamp.now()
                            logger.info(f"✓ Positions opened at {last_open_time.strftime('%H:%M:%S')}")
                        else:
                            logger.warning("No positions were opened. Will retry in next check.")

                    except Exception as open_error:
                        logger.error(f"Error opening positions: {open_error}", exc_info=True)
                        time_module.sleep(30)
                        continue

                # Monitor and adjust any open positions
                if trader.has_open_positions():
                    logger.info(f"\n[{datetime.now().strftime('%H:%M:%S')}] Monitoring open positions...")

                    logger.info("\n--- Current Tracked Positions ---")
                    logger.info(trader.get_live_positions_details())
                    logger.info("------------------------------------")

                    try:
                        required_strikes = trader._get_all_open_strikes()

                        # Stay on the same expiry as open positions
                        open_expiry = None
                        for cycle_data in trader.live_positions.values():
                            if cycle_data["status"] == "OPEN" and cycle_data["positions"]:
                                open_expiry = cycle_data["positions"][0]["expiry"]
                                break

                        option_chain = trader.get_live_option_chain(
                            underlying="SPX",
                            required_strikes=required_strikes,
                            expiry=open_expiry,
                        )

                        if not option_chain.empty:
                            trader.monitor_and_adjust(option_chain)

                            # Check each cycle for expiry
                            for cycle_id, pos_data in list(trader.live_positions.items()):
                                if pos_data["status"] == "OPEN":
                                    expiry_str = pos_data["positions"][0]["expiry"]
                                    expiry_date = datetime.strptime(expiry_str, "%Y%m%d").date()
                                    today = datetime.now().date()

                                    if today >= expiry_date:
                                        logger.info(f"  Closing cycle {cycle_id} (reached expiry day: {expiry_str})")
                                        trader.close_positions_at_expiry(cycle_id, option_chain=option_chain, reason="expiry")
                                        positions_opened_today = False
                        else:
                            logger.warning("Could not fetch option chain for monitoring")

                    except Exception as monitor_error:
                        logger.error(f"Error monitoring positions: {monitor_error}", exc_info=True)

                # Sleep before next check (5-minute intervals)
                logger.debug("Waiting 5 minutes before next check...")
                time_module.sleep(300)

            except KeyboardInterrupt:
                logger.info("\nShutting down trader (Ctrl+C)...")
                break

            except Exception as loop_error:
                logger.error(f"Error in main loop: {loop_error}", exc_info=True)
                logger.info("Retrying in 60 seconds...")
                time_module.sleep(60)

    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)

    finally:
        logger.info("\nCleaning up...")

        if trader is not None:
            try:
                saved = trader.save_trade_log()
                if saved:
                    logger.info("✓ Trade log saved")

                summary = trader.get_position_summary()
                logger.info(f"\nSession Summary: {summary}")

            except Exception as save_error:
                logger.error(f"Error saving trade log: {save_error}")

        try:
            conn_mgr.disconnect()
            logger.info("✓ IBKR disconnected")
        except Exception as disc_error:
            logger.error(f"Error during disconnect: {disc_error}")

        logger.info("=" * 70)
        logger.info("Trader stopped")
        logger.info("=" * 70)


if __name__ == "__main__":
    run_live_trading(is_paper=False)