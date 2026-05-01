import logging
import sys
from pathlib import Path
from datetime import datetime
import pandas as pd


# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

sys.path.insert(0, str(Path(__file__).parent))

from download_data import download_price_only, download_spx_iv_data

from config import STRATEGY_CONFIG, DATA_PATH, PROCESSED_PATH ,LOG_PATH
from chain_builder import ChainBuilder
from strategy import DeltaNeutralStrategy
from duckdb_loader import SPXDuckDB

from IBKRConnectionManager import get_ibkr_connection

from common import (
    calculate_expiry_date,
    create_expiry_timestamp,
    get_trading_dates,
    validate_date_range,
    log_trading_session_start,
    log_trading_session_end
)
# Simple connection
conn_mgr = get_ibkr_connection()
if conn_mgr.connect(timeout=10, max_retries=5):
    print(f"Connected! Account: {conn_mgr.get_account()}")
    print(f"Using client_id: {conn_mgr.get_current_client_id()}")
else:
    print("Connection failed")

# Check connection status
conn_mgr.print_connection_status()

# Ensure connected (reconnect if needed)
conn_mgr.ensure_connected()

# Get connection info
info = conn_mgr.get_connection_info()
print(f"Primary ID: {info['primary_client_id']}, Current ID: {info['current_client_id']}")

def check_and_download_missing_data(start_date, end_date):
    """
    Check if data files exist. If not, download from IBKR
    
    Args:
        start_date: Start date (YYYY-MM-DD)
        end_date: End date (YYYY-MM-DD)
    
    Returns:
        Tuple of (price_files, iv_files) that were downloaded or verified
    """
    logger.info(f"Checking data for {start_date} to {end_date}")
    
    trade_dates = pd.bdate_range(start=start_date, end=end_date)
    
    if len(trade_dates) == 0:
        logger.error(f"No business dates between {start_date} and {end_date}")
        return [], []
    
    missing_price_dates = []
    missing_iv_dates = []
    
    # Check which dates are missing
    for dt in trade_dates:
        trade_date = dt.strftime("%Y_%m_%d")
        
        # Check price data (format: YYYY-MM-DD_1minute.parquet)
        price_file = DATA_PATH / f"spx_5mins_{trade_date}.parquet"
        if not price_file.exists():
            missing_price_dates.append(trade_date)
        
        # Check IV data (format: spx_iv_30_mins_YYYY-MM-DD.parquet)
        iv_file = DATA_PATH / f"spx_1day_option_implied_volatility_{trade_date}.parquet"
        if not iv_file.exists():
            missing_iv_dates.append(trade_date)
    
    price_files = []
    iv_files = []
    
    # Download missing price data
    if missing_price_dates:
        logger.warning(f"Missing price data for {len(missing_price_dates)} dates")
        logger.info(f"Downloading price data: {missing_price_dates[0]} to {missing_price_dates[-1]}")
        
        try:
            price_files = download_price_only(
                start_date=missing_price_dates[0],
                end_date=missing_price_dates[-1],
                bar_size="5 mins"
            )
            logger.info(f"✓ Downloaded {len(price_files)} price files")
        except Exception as e:
            logger.error(f"✗ Failed to download price data: {e}")
            logger.warning("Proceeding with existing price data...")
    else:
        logger.info(f"✓ Price data exists for all {len(trade_dates)} dates")
    
    # Download missing IV data
    if missing_iv_dates:
        logger.warning(f"Missing IV data for {len(missing_iv_dates)} dates")
        logger.info(f"Downloading IV data: {missing_iv_dates[0]} to {missing_iv_dates[-1]}")
        
        try:
            iv_files = download_spx_iv_data(
                start_date=missing_iv_dates[0],
                end_date=missing_iv_dates[-1],
                bar_size="30 mins"
            )
            logger.info(f"✓ Downloaded {len(iv_files)} IV files")
        except Exception as e:
            logger.error(f"✗ Failed to download IV data: {e}")
            logger.warning("Proceeding with existing IV data...")
    else:
        logger.info(f"✓ IV data exists for all {len(trade_dates)} dates")
    
    return price_files, iv_files


def run_for_date_range():
    """Main backtest function"""
    
    logger.info("=" * 70)
    logger.info("SPX TRADING BACKTEST")
    logger.info("=" * 70)
    
    # Parse configuration
    start_date_str = STRATEGY_CONFIG.get("start_date", "2026-03-20")
    end_date_str = STRATEGY_CONFIG.get("end_date", "2026-03-26")
    dte_days = STRATEGY_CONFIG.get("dte_days", 0)
    strike_step = STRATEGY_CONFIG.get("strike_step", 5)
    pct_band = STRATEGY_CONFIG.get("pct_band", 0.10)
    risk_free_rate = STRATEGY_CONFIG.get("risk_free_rate", 0.045)
    expiry_hour = STRATEGY_CONFIG.get("expiry_hour", 15)
    
    start_date = pd.to_datetime(start_date_str).date()
    end_date = pd.to_datetime(end_date_str).date()
    
    # Validate dates
    if start_date > end_date:
        raise ValueError(f"start_date ({start_date}) must be <= end_date ({end_date})")
    
    logger.info(f"Date range: {start_date} to {end_date}")
    logger.info(f"DTE mode: {dte_days} days to expiry")
    logger.info(f"Config: strike_step={strike_step}, pct_band={pct_band}, expiry_hour={expiry_hour}:00")
    
    # ========== STEP 1: DOWNLOAD MISSING DATA ==========
    logger.info("\n[STEP 1/4] Checking and downloading data...")
    logger.info("-" * 70)
    
    try:
        price_files, iv_files = check_and_download_missing_data(
            start_date=start_date_str,
            end_date=end_date_str
        )
    except Exception as e:
        logger.error(f"Data check failed: {e}")
        logger.warning("Continuing with available data...")
    
    # ========== STEP 2: BUILD OPTION CHAINS ==========
    logger.info("\n[STEP 2/4] Building option chains...")
    logger.info("-" * 70)
    
    db = SPXDuckDB()
    db.create_view()
    
    builder = ChainBuilder()
    trade_dates = pd.bdate_range(start=start_date, end=end_date)
    
    if len(trade_dates) == 0:
        logger.error(f"No business dates found between {start_date} and {end_date}")
        return pd.DataFrame(), pd.DataFrame()
    
    window_size = max(1, dte_days)
    all_trades = []
    all_summaries = []
    
    i = 0
    while i < len(trade_dates):
        window_end_idx = min(i + window_size, len(trade_dates))
        window_dates = trade_dates[i:window_end_idx]
        
        if len(window_dates) == 0:
            i += window_size
            continue
        
        window_start_str = window_dates[0].strftime("%Y-%m-%d")
        window_end_str = window_dates[-1].strftime("%Y-%m-%d")
        
        # ========== CALCULATE EXPIRY DATE ONCE ==========
        expiry_date, expiry_date_str = calculate_expiry_date(window_start_str, dte_days)
        expiry_ts = create_expiry_timestamp(expiry_date_str, expiry_hour)
        
        dte_label = "same-day" if dte_days == 0 else f"{dte_days}-day"
        logger.info(f"Window: {window_start_str} to {window_end_str} ({dte_label})")
        
        strategy = DeltaNeutralStrategy()
        
        try:
            combined_chain = []
            
            for dt in window_dates:
                trade_date = dt.strftime("%Y-%m-%d")
                
                                
                logger.info(f"  Building chain for {trade_date}...")
                
                option_chain_df, saved_file = builder.build_bs_option_chain(
                    trade_date=trade_date,
                    data_path=DATA_PATH,
                    output_path=PROCESSED_PATH,
                    risk_free_rate=risk_free_rate,
                    strike_step=strike_step,
                    pct_band=pct_band,
                    expiry_hour=expiry_hour,
                    dte_days=dte_days,
                    expiry_date=expiry_date_str,  # <-- PASS CALCULATED EXPIRY
                )
                
                if option_chain_df.empty:
                    logger.warning(f"  Empty chain for {trade_date}")
                    continue
                
                logger.info(f"  ✓ {len(option_chain_df)} rows, {len(option_chain_df['strike'].unique())} strikes")
                
                option_chain_df = option_chain_df.rename(
                    columns={"bs_call_price": "call_mid", "bs_put_price": "put_mid"}
                )
                combined_chain.append(option_chain_df)
            
            if not combined_chain:
                logger.warning(f"No valid chains for window")
                all_summaries.append({
                    "window_start": window_start_str,
                    "window_end": window_end_str,
                    "dte": dte_days,
                    "status": "no_chains",
                    "trade_count": 0,
                    "total_pnl": 0.0
                })
                i += window_size
                continue
            
            combined_option_chain = pd.concat(combined_chain, ignore_index=True)
            logger.info(f"Combined: {len(combined_option_chain)} rows")
            
            # ========== STEP 3: RUN BACKTEST ==========
            logger.info(f"Running backtest for window...")
            
            trades_df, summary = strategy.run_backtest(
                combined_option_chain,
                trade_date=window_start_str,
                dte_days=dte_days,
                expiry_date=expiry_date,  # <-- PASS CALCULATED EXPIRY
            )
            
            summary["window_start"] = window_start_str
            summary["window_end"] = window_end_str
            summary["dte"] = dte_days
            summary["strike_step"] = strike_step
            all_summaries.append(summary)
            
            logger.info(f"✓ Backtest complete: {summary}")
            
            if not trades_df.empty:
                trades_df["window_start"] = window_start_str
                trades_df["window_end"] = window_end_str
                trades_df["dte"] = dte_days
                all_trades.append(trades_df)
        
        except Exception as e:
            logger.error(f"Error processing window: {e}", exc_info=True)
            all_summaries.append({
                "window_start": window_start_str,
                "window_end": window_end_str,
                "dte": dte_days,
                "status": "error",
                "trade_count": 0,
                "total_pnl": 0.0,
                "error": str(e)
            })
        
        i += window_size
    
    # ========== STEP 4: SAVE RESULTS ==========
    logger.info("\n[STEP 4/4] Saving results...")
    logger.info("-" * 70)
    
    summaries_df = pd.DataFrame(all_summaries)
    
    if all_trades:
        trades_full_df = pd.concat(all_trades, ignore_index=True)
    else:
        trades_full_df = pd.DataFrame()
    
    dte_label = "0dte" if dte_days == 0 else f"{dte_days}dte"
    timestamp = pd.Timestamp.now().strftime("%Y%m%d_%H%M%S")
    
    summary_file = LOG_PATH / f"backtest_{dte_label}_summary_{timestamp}.csv"
    summaries_df.to_csv(summary_file, index=False)
    logger.info(f"✓ Saved: {summary_file}")
    
    if not trades_full_df.empty:
        trades_file = LOG_PATH / f"backtest_{dte_label}_trades_{timestamp}.csv"
        trades_full_df.to_csv(trades_file, index=False)
        logger.info(f"✓ Saved: {trades_file}")
    
    # ========== SUMMARY ==========
    logger.info("\n" + "=" * 70)
    logger.info("BACKTEST SUMMARY")
    logger.info("=" * 70)
    logger.info(f"Total windows: {len(all_summaries)}")
    logger.info(f"Total trades: {len(trades_full_df)}")
    if len(trades_full_df) > 0:
        logger.info(f"Total PnL: ${trades_full_df['pnl'].sum():,.2f}")
    logger.info("=" * 70 + "\n")
    
    return trades_full_df, summaries_df


if __name__ == "__main__":
    try:
        trades, summaries = run_for_date_range()
        print("\n✓ Backtest completed successfully!")
        print(summaries)
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        print(f"\n✗ Backtest failed: {e}")
        sys.exit(1)
        
    finally:
        try:
            conn_mgr.disconnect()
            logger.info("Disconnected from IBKR")
        except Exception as e:
            logger.error(f"Error during disconnect: {e}")