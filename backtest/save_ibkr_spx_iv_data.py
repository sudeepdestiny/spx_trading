# -*- coding: utf-8 -*-
"""
IB API - Backfill SPX price history and SPX OPTION_IMPLIED_VOLATILITY history
using singleton connection manager and save as daily parquet files.
"""

import pandas as pd
import threading
import time
from pathlib import Path
from datetime import datetime, timedelta
import logging
import os
import sys

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from config import apikey_config, DATA_PATH, LOG_PATH, STRATEGY_CONFIG
from IBKRConnectionManager import get_ibkr_connection
from ibapi.contract import Contract

DATA_PATH = Path(DATA_PATH)
DATA_PATH.mkdir(parents=True, exist_ok=True)

LOG_FOLDER = STRATEGY_CONFIG.get("log_folder", "logs")
os.makedirs(LOG_FOLDER, exist_ok=True)
timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
filepath = Path(LOG_PATH) / f"spx_iv_data_{timestamp}.log"

logging.basicConfig(
    filename=filepath,
    filemode='w',
    level=logging.INFO,
    format='%(asctime)s - %(message)s',
    datefmt='%d-%b-%y %H:%M:%S'
)


def spx_index():
    """Create SPX index contract"""
    contract = Contract()
    contract.symbol = "SPX"
    contract.secType = "IND"
    contract.currency = "USD"
    contract.exchange = "CBOE"
    return contract


def step_size_for_bar_size(bar_size):
    """Map bar size to duration for IBKR API"""
    mapping = {
        "1 min": "1 D",
        "2 mins": "10 D",
        "3 mins": "1 W",
        "5 mins": "20 D",
        "10 mins": "1 M",
        "15 mins": "1 M",
        "30 mins": "1 M",
        "1 hour": "1 M",
        "1 day": "1 Y",
        "1 week": "1 Y",
    }
    return mapping.get(bar_size, "20 D")


def format_end_datetime(dt_obj):
    """Format datetime for IBKR API"""
    return dt_obj.strftime("%Y%m%d %H:%M:%S")


def parse_ib_datetime(series):
    """Parse IBKR datetime strings (handles both intraday and daily formats)"""
    s = series.astype(str).str.strip()

    intraday = pd.to_datetime(s, format="%Y%m%d %H:%M:%S", errors="coerce")
    if intraday.notna().any():
        return intraday.fillna(pd.to_datetime(s, errors="coerce"))

    daily = pd.to_datetime(s, format="%Y%m%d", errors="coerce")
    return daily.fillna(pd.to_datetime(s, errors="coerce"))


def save_daily_parquet(df, data_path, prefix, start_date, end_date):
    """Save dataframe as daily parquet files (format: YYYY-MM-DD)"""
    df = df.copy()
    df["Date"] = pd.to_datetime(df["Date"])
    df = df.sort_values("Date").reset_index(drop=True)
    df["trade_date"] = df["Date"].dt.strftime("%Y_%m_%d")

    saved_files = []
    for trade_date, g in df.groupby("trade_date"):
        file_name = f"{prefix}_{trade_date}.parquet"
        file_path = Path(data_path) / file_name
        g.drop(columns=["trade_date"]).to_parquet(file_path, index=False)
        saved_files.append(file_path)
        logging.info(f"Saved: {file_path} ({len(g)} rows)")

    return saved_files


def backfill_history(wrapper, client, contract, start_date, end_date, bar_size, what_to_show, use_rth):
    """
    Backfill historical data from IBKR using active connection
    
    Args:
        wrapper: Connection wrapper from connection manager
        client: EClient instance from connection manager
        contract: IBKR Contract object
        start_date: Start date (YYYY-MM-DD)
        end_date: End date (YYYY-MM-DD)
        bar_size: Bar size (e.g., "5 mins", "30 mins", "1 day")
        what_to_show: Data type ("TRADES", "OPTION_IMPLIED_VOLATILITY", etc.)
        use_rth: Use regular trading hours (1=yes, 0=no)
    
    Returns:
        DataFrame with historical data
    """
    start_dt = datetime.strptime(start_date, "%Y_%m_%d")
    end_dt = datetime.strptime(end_date, "%Y_%m_%d") + timedelta(days=1) - timedelta(seconds=1)
    duration_str = step_size_for_bar_size(bar_size)

    logging.info(f"Starting backfill: {start_date} to {end_date}, bar_size={bar_size}, whatToShow={what_to_show}")

    chunks = []
    req_id = 1

    while end_dt > start_dt:
        wrapper.hist_data = []
        wrapper.done_event = threading.Event()

        logging.info(
            f"Requesting: reqId={req_id}, whatToShow={what_to_show}, "
            f"barSize={bar_size}, end={end_dt.date()}"
        )

        try:
            client.reqHistoricalData(
                reqId=req_id,
                contract=contract,
                endDateTime=format_end_datetime(end_dt),
                durationStr=duration_str,
                barSizeSetting=bar_size,
                whatToShow=what_to_show,
                useRTH=use_rth,
                formatDate=1,
                keepUpToDate=0,
                chartOptions=[]
            )

            if not wrapper.done_event.wait(timeout=120):
                logging.error(f"Timeout for reqId={req_id} at {end_dt}")
                raise TimeoutError(f"Timed out waiting for historical data")

            if not wrapper.hist_data:
                logging.warning(f"No data returned for reqId={req_id}")
                break

            df = pd.DataFrame(wrapper.hist_data)
            df["Date"] = parse_ib_datetime(df["Date"])
            df = df.dropna(subset=["Date"])
            df = df[df["Date"] >= start_dt]

            if df.empty:
                logging.warning(f"Dataframe empty after filtering")
                break

            df["BarSize"] = bar_size
            chunks.append(df)

            earliest = df["Date"].min()
            logging.info(f"Fetched: {len(df)} rows, earliest={earliest.date()}")

            end_dt = earliest - timedelta(seconds=1)
            req_id += 1

            time.sleep(1.2)  # Rate limit

        except Exception as e:
            logging.error(f"Error in request {req_id}: {e}", exc_info=True)
            raise

    if not chunks:
        raise ValueError(f"No data returned from IBKR for {what_to_show}")

    full_df = (
        pd.concat(chunks, ignore_index=True)
        .drop_duplicates(subset=["Date"])
        .sort_values("Date")
        .reset_index(drop=True)
    )
    
    logging.info(f"✓ Backfill complete: {len(full_df)} rows")
    return full_df


def download_spx_data(
    start_date,
    end_date,
    bar_size="5 mins",
    what_to_show="TRADES",
    use_rth=1,
    data_path=None,
    use_active_connection=True,
    timeout=10,
    max_retries=5
):
    """
    Download and save SPX data using singleton connection manager
    
    Args:
        start_date: Start date (YYYY-MM-DD)
        end_date: End date (YYYY-MM-DD)
        bar_size: Bar size (default "5 mins")
        what_to_show: Data type (default "TRADES")
        use_rth: Regular trading hours (default 1)
        data_path: Where to save files (default from config)
        use_active_connection: Use singleton connection manager (default True)
        timeout: Connection timeout in seconds
        max_retries: Max retries with different client IDs
    
    Returns:
        List of saved file paths
    """
    # Use defaults from config if not provided
    if data_path is None:
        data_path = DATA_PATH
    
    logging.info(f"Starting download: {start_date} to {end_date}")
    logging.info(f"Bar size: {bar_size}, Data type: {what_to_show}")
    
    # Get singleton connection manager
    conn_mgr = get_ibkr_connection()
    
    # Ensure connected with auto-retry
    if not conn_mgr.ensure_connected(timeout=timeout):
        logging.error("Failed to establish connection to IBKR")
        raise ConnectionError("Failed to connect to IBKR API")
    
    # Get active connection components
    wrapper = conn_mgr.get_wrapper()
    client = conn_mgr.get_client()
    account = conn_mgr.get_account()
    current_client_id = conn_mgr.get_current_client_id()
    
    if not wrapper or not client:
        logging.error("Failed to get wrapper/client from connection manager")
        raise RuntimeError("Connection manager not properly initialized")
    
    logging.info(f"✓ Using active connection")
    logging.info(f"  Account: {account}")
    logging.info(f"  Client ID: {current_client_id}")
    logging.info(f"  Host: {conn_mgr.host}:{conn_mgr.port}")
    
    try:
        # Fetch historical data using active connection
        contract = spx_index()
        history_df = backfill_history(
            wrapper=wrapper,
            client=client,
            contract=contract,
            start_date=start_date,
            end_date=end_date,
            bar_size=bar_size,
            what_to_show=what_to_show,
            use_rth=use_rth
        )
        
        # Save to parquet files
        saved_files = save_daily_parquet(history_df, data_path,
                                         prefix=f"spx_5mins",
            start_date=start_date,
            end_date=end_date)
        
        logging.info(f"✓ Saved {len(saved_files)} files")
        return saved_files
    
    except Exception as e:
        logging.error(f"Error during backfill: {e}", exc_info=True)
        raise


def download_spx_iv_data(
    start_date,
    end_date,
    bar_size="30 mins",
    data_path=None,
    timeout=10,
    max_retries=5
):
    """
    Download SPX implied volatility data using singleton connection manager
    
    Args:
        start_date: Start date (YYYY-MM-DD)
        end_date: End date (YYYY-MM-DD)
        bar_size: Bar size (default "30 mins")
        data_path: Where to save (default from config)
        timeout: Connection timeout in seconds
        max_retries: Max retries with different client IDs
    
    Returns:
        List of saved file paths
    """
    if data_path is None:
        data_path = DATA_PATH
    
    logging.info(f"Starting IV download: {start_date} to {end_date}, bar_size={bar_size}")
    
    # Get singleton connection manager
    conn_mgr = get_ibkr_connection()
    
    # Ensure connected with auto-retry
    if not conn_mgr.ensure_connected(timeout=timeout):
        logging.error("Failed to establish connection to IBKR")
        raise ConnectionError("Failed to connect to IBKR API")
    
    # Get active connection components
    wrapper = conn_mgr.get_wrapper()
    client = conn_mgr.get_client()
    account = conn_mgr.get_account()
    current_client_id = conn_mgr.get_current_client_id()
    
    if not wrapper or not client:
        logging.error("Failed to get wrapper/client from connection manager")
        raise RuntimeError("Connection manager not properly initialized")
    
    logging.info(f"✓ Using active connection")
    logging.info(f"  Account: {account}")
    logging.info(f"  Client ID: {current_client_id}")
    logging.info(f"  Host: {conn_mgr.host}:{conn_mgr.port}")
    
    try:
        contract = spx_index()
        iv_df = backfill_history(
            wrapper=wrapper,
            client=client,
            contract=contract,
            start_date=start_date,
            end_date=end_date,
            bar_size=bar_size,
            what_to_show="OPTION_IMPLIED_VOLATILITY",
            use_rth=1
        )
        
        iv_files = save_daily_parquet(
            iv_df,
            data_path,
            prefix=f"spx_1day_option_implied_volatility",
            start_date=start_date,
            end_date=end_date
        )
        
        logging.info(f"✓ Saved {len(iv_files)} IV files")
        return iv_files
    
    except Exception as e:
        logging.error(f"Error during IV download: {e}", exc_info=True)
        raise


def download_all_spx_data(
    start_date,
    end_date,
    price_bar_size="5 mins",
    iv_bar_size="30 mins",
    data_path=None,
    timeout=10,
    max_retries=5
):
    """
    Download both SPX price and IV data using singleton connection manager
    
    Args:
        start_date: Start date (YYYY-MM-DD)
        end_date: End date (YYYY-MM-DD)
        price_bar_size: Price bar size (default "5 mins")
        iv_bar_size: IV bar size (default "30 mins")
        data_path: Where to save (default from config)
        timeout: Connection timeout in seconds
        max_retries: Max retries with different client IDs
    
    Returns:
        Tuple of (price_files, iv_files)
    """
    if data_path is None:
        data_path = DATA_PATH
    
    logging.info("=" * 70)
    logging.info(f"Starting combined download: {start_date} to {end_date}")
    logging.info("=" * 70)
    
    # Get singleton connection manager
    conn_mgr = get_ibkr_connection()
    
    # Connect once for both downloads
    if not conn_mgr.connect(timeout=timeout, max_retries=max_retries):
        logging.error("Failed to connect to IBKR")
        raise ConnectionError("Failed to establish IBKR connection")
    
    conn_mgr.print_connection_status()
    
    try:
        # Download price data
        logging.info("\n[STEP 1/2] Downloading price data...")
        logging.info("-" * 70)
        price_files = download_spx_price_data(
            start_date=start_date,
            end_date=end_date,
            bar_size=price_bar_size,
            data_path=data_path,
            timeout=timeout,
            max_retries=max_retries
        )
        logging.info(f"✓ Downloaded {len(price_files)} price files")
        
        # Download IV data
        logging.info("\n[STEP 2/2] Downloading IV data...")
        logging.info("-" * 70)
        iv_files = download_spx_iv_data(
            start_date=start_date,
            end_date=end_date,
            bar_size=iv_bar_size,
            data_path=data_path,
            timeout=timeout,
            max_retries=max_retries
        )
        logging.info(f"✓ Downloaded {len(iv_files)} IV files")
        
        logging.info("\n" + "=" * 70)
        logging.info(f"✓ Combined download complete: {len(price_files)} price + {len(iv_files)} IV files")
        logging.info("=" * 70)
        
        return price_files, iv_files
    
    except Exception as e:
        logging.error(f"Error: {e}", exc_info=True)
        raise


if __name__ == "__main__":
    import sys
    
    start_date = STRATEGY_CONFIG.get("start_date", "2026-03-20")
    end_date = STRATEGY_CONFIG.get("end_date", "2026-04-09")
    price_bar_size = STRATEGY_CONFIG.get("price_bar_size", "5 mins")
    iv_bar_size = STRATEGY_CONFIG.get("iv_bar_size", "30 mins")
    
    logging.info(f'Started - downloading {start_date} to {end_date}')
    
    try:
        # Download all data using singleton connection
        price_files, iv_files = download_all_spx_data(
            start_date=start_date,
            end_date=end_date,
            price_bar_size=price_bar_size,
            iv_bar_size=iv_bar_size,
            timeout=10,
            max_retries=5
        )
        
        print(f"✓ Saved {len(price_files)} price files:")
        for f in price_files:
            print(f"  {f}")
        
        print(f"\n✓ Saved {len(iv_files)} IV files:")
        for f in iv_files:
            print(f"  {f}")
    
    except Exception as e:
        logging.error(f"Error: {e}", exc_info=True)
        print(f"✗ Error: {e}")
        sys.exit(1)