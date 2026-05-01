"""
Unified script to download both SPX price and IV data for specific dates
"""

from save_ibkr_spx_data import download_spx_data
from save_ibkr_spx_iv_data import download_spx_data, download_spx_iv_data
from config import STRATEGY_CONFIG
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def download_all_spx_data(
    start_date,
    end_date,
    price_bar_size="5 mins",
    iv_bar_size="30 mins",
    download_price=True,
    download_iv=True
):
    """
    Download both SPX price and IV data
    
    Args:
        start_date: Start date (YYYY-MM-DD)
        end_date: End date (YYYY-MM-DD)
        price_bar_size: Price bar size (default "5 mins")
        iv_bar_size: IV bar size (default "30 mins")
        download_price: Download price data (default True)
        download_iv: Download IV data (default True)
    
    Returns:
        Tuple of (price_files, iv_files)
    """
    logger.info("=" * 70)
    logger.info(f"Starting SPX data download: {start_date} to {end_date}")
    logger.info(f"Price bar: {price_bar_size}, IV bar: {iv_bar_size}")
    logger.info("=" * 70)
    
    price_files = []
    iv_files = []
    
    try:
        # Download price data
        if download_price:
            logger.info("\n[STEP 1/2] Downloading SPX PRICE data...")
            logger.info(f"Bar size: {price_bar_size}")
            
            price_files = download_spx_data(
                start_date=start_date,
                end_date=end_date,
                bar_size=price_bar_size,
                what_to_show="TRADES",
                use_rth=1
            )
            
            logger.info(f"✓ Price data download complete: {len(price_files)} files")
            for f in price_files:
                logger.info(f"  - {f}")
        
        # Download IV data
        if download_iv:
            logger.info("\n[STEP 2/2] Downloading SPX IV data...")
            logger.info(f"Bar size: {iv_bar_size}")
            
            iv_files = download_spx_iv_data(
                start_date=start_date,
                end_date=end_date,
                bar_size=iv_bar_size
            )
            
            logger.info(f"✓ IV data download complete: {len(iv_files)} files")
            for f in iv_files:
                logger.info(f"  - {f}")
        
        # Summary
        logger.info("\n" + "=" * 70)
        logger.info("DOWNLOAD SUMMARY")
        logger.info("=" * 70)
        logger.info(f"Price files: {len(price_files)}")
        logger.info(f"IV files: {len(iv_files)}")
        logger.info(f"Total files: {len(price_files) + len(iv_files)}")
        logger.info("✓ All downloads completed successfully!")
        logger.info("=" * 70)
        
        return price_files, iv_files
    
    except Exception as e:
        logger.error("=" * 70)
        logger.error(f"✗ DOWNLOAD FAILED: {e}")
        logger.error("=" * 70)
        raise


def download_price_only(start_date, end_date, bar_size="5 mins"):
    """Download only SPX price data"""
    logger.info(f"Downloading SPX PRICE data: {start_date} to {end_date}")
    
    try:
        files = download_spx_data(
            start_date=start_date,
            end_date=end_date,
            bar_size=bar_size,
            what_to_show="TRADES",
            use_rth=1
        )
        logger.info(f"✓ Saved {len(files)} price files")
        return files
    
    except Exception as e:
        logger.error(f"✗ Failed to download price data: {e}")
        raise


def download_iv_only(start_date, end_date, bar_size="30 mins"):
    """Download only SPX IV data"""
    logger.info(f"Downloading SPX IV data: {start_date} to {end_date}")
    
    try:
        files = download_spx_iv_data(
            start_date=start_date,
            end_date=end_date,
            bar_size=bar_size
        )
        logger.info(f"✓ Saved {len(files)} IV files")
        return files
    
    except Exception as e:
        logger.error(f"✗ Failed to download IV data: {e}")
        raise


def download_from_config(download_price=True, download_iv=True):
    """Download using dates and parameters from STRATEGY_CONFIG"""
    start_date = STRATEGY_CONFIG.get("start_date", "2026-03-20")
    end_date = STRATEGY_CONFIG.get("end_date", "2026-03-26")
    price_bar = STRATEGY_CONFIG.get("price_bar_size", "5 mins")
    iv_bar = STRATEGY_CONFIG.get("iv_bar_size", "30 mins")
    
    logger.info(f"Using config: {start_date} to {end_date}")
    logger.info(f"Price bar: {price_bar}, IV bar: {iv_bar}")
    
    return download_all_spx_data(
        start_date=start_date,
        end_date=end_date,
        price_bar_size=price_bar,
        iv_bar_size=iv_bar,
        download_price=download_price,
        download_iv=download_iv
    )


if __name__ == "__main__":
    
    if len(sys.argv) == 4:
        # Usage: python download_all_data.py 2026-03-20 2026-03-26 all
        start_date = sys.argv[1]
        end_date = sys.argv[2]
        data_type = sys.argv[3].lower()
        
        if data_type == "all":
            download_all_spx_data(start_date, end_date)
        elif data_type == "price":
            download_price_only(start_date, end_date)
        elif data_type == "iv":
            download_iv_only(start_date, end_date)
        else:
            print("Usage: python download_all_data.py START_DATE END_DATE [all|price|iv]")
            print("Example: python download_all_data.py 2026-03-20 2026-03-26 all")
            sys.exit(1)
    
    elif len(sys.argv) == 3:
        # Usage: python download_all_data.py 2026-03-20 2026-03-26
        start_date = sys.argv[1]
        end_date = sys.argv[2]
        download_all_spx_data(start_date, end_date)
    
    else:
        # Use config defaults
        print("No arguments provided. Using STRATEGY_CONFIG...")
        download_from_config()