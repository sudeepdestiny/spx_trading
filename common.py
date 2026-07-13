"""
Common utilities for SPX trading backtest
Includes: expiry date calculations, date validation, trading day checks
"""

import logging
import pandas as pd
from pandas.tseries.offsets import BusinessDay
from pandas.tseries.holiday import (
    AbstractHolidayCalendar, Holiday, nearest_workday,
    USMartinLutherKingJr, USPresidentsDay, GoodFriday,
    USMemorialDay, USLaborDay, USThanksgivingDay
)
from datetime import datetime, date

logger = logging.getLogger(__name__)

# ========== HOLIDAY & TRADING DAY UTILITIES ==========

class NYSEHolidayCalendar(AbstractHolidayCalendar):
    """NYSE/CBOE Trading Calendar for SPX"""
    rules = [
        Holiday("New Years Day", month=1, day=1, observance=nearest_workday),
        USMartinLutherKingJr,
        USPresidentsDay,
        GoodFriday,
        USMemorialDay,
        Holiday("Juneteenth", month=6, day=19, observance=nearest_workday),
        Holiday("Independence Day", month=7, day=4, observance=nearest_workday),
        USLaborDay,
        USThanksgivingDay,
        Holiday("Christmas", month=12, day=25, observance=nearest_workday),
    ]

us_holidays = NYSEHolidayCalendar()


def is_trading_day(check_date: pd.Timestamp) -> bool:
    """
    Check if date is a trading day (not weekend or US market holiday)
    
    Args:
        check_date: pd.Timestamp to check
    
    Returns:
        True if trading day, False otherwise
    
    Example:
        >>> is_trading_day(pd.Timestamp("2026-03-02"))  # Monday
        True
        >>> is_trading_day(pd.Timestamp("2026-03-07"))  # Saturday
        False
    """
    # Check weekend (Saturday=5, Sunday=6)
    if check_date.weekday() >= 5:
        return False
    
    # Check US Federal holidays
    holidays = us_holidays.holidays(
        start=check_date - pd.Timedelta(days=1),
        end=check_date + pd.Timedelta(days=1)
    )
    if check_date in holidays:
        return False
    
    return True


def get_next_trading_day(start_date: pd.Timestamp) -> pd.Timestamp:
    """
    Get the next trading day from given date
    
    Args:
        start_date: Starting date as pd.Timestamp
    
    Returns:
        Next trading day as pd.Timestamp
    
    Example:
        >>> get_next_trading_day(pd.Timestamp("2026-03-06"))  # Friday
        Timestamp('2026-03-09 00:00:00')  # Monday (skip weekend)
    """
    next_day = start_date + pd.Timedelta(days=1)
    while not is_trading_day(next_day):
        next_day += pd.Timedelta(days=1)
    return next_day


def get_previous_trading_day(start_date: pd.Timestamp) -> pd.Timestamp:
    """
    Get the previous trading day from given date
    
    Args:
        start_date: Starting date as pd.Timestamp
    
    Returns:
        Previous trading day as pd.Timestamp
    
    Example:
        >>> get_previous_trading_day(pd.Timestamp("2026-03-09"))  # Monday
        Timestamp('2026-03-06 00:00:00')  # Friday (skip weekend)
    """
    prev_day = start_date - pd.Timedelta(days=1)
    while not is_trading_day(prev_day):
        prev_day -= pd.Timedelta(days=1)
    return prev_day


# ========== EXPIRY DATE CALCULATIONS ==========

def calculate_expiry_date(trade_date: str, dte_days: int) -> tuple:
    """
    Calculate the expiry date for options given trade date and DTE
    
    Uses BusinessDay offset to automatically skip weekends.
    If calculated date falls on holiday/weekend, rolls forward to next trading day.
    
    Args:
        trade_date: Trade date (YYYY-MM-DD format)
        dte_days: Days to expiry (0 = same day, 1+ = future dates)
    
    Returns:
        Tuple of (expiry_date_obj, expiry_date_str)
        - expiry_date_obj: datetime.date object
        - expiry_date_str: String format "YYYY-MM-DD"
    
    Raises:
        ValueError: If trade_date format is invalid
    
    Example:
        >>> calculate_expiry_date("2026-03-02", 0)
        (datetime.date(2026, 3, 2), "2026-03-02")
        
        >>> calculate_expiry_date("2026-03-02", 5)
        (datetime.date(2026, 3, 9), "2026-03-09")  # Skips weekend
    """
    try:
        trade_ts = pd.Timestamp(trade_date)
    except Exception as e:
        raise ValueError(f"Invalid trade_date format: {trade_date}. Use YYYY-MM-DD") from e
    
    if dte_days == 0:
        # Same day expiry
        expiry_date = trade_ts.date()
        logger.info(f"0 DTE mode: Expiry on same day ({expiry_date})")
    else:
        # N DTE - add N business days (automatically skips weekends via BusinessDay)
        expiry_ts = trade_ts + BusinessDay(dte_days)
        expiry_date = expiry_ts.date()
        
        # Verify expiry_date is a trading day (not holiday or weekend)
        while not is_trading_day(pd.Timestamp(expiry_date)):
            logger.warning(f"Calculated expiry {expiry_date} is not a trading day (weekend or holiday)")
            # Roll forward to next trading day
            expiry_date = get_next_trading_day(pd.Timestamp(expiry_date)).date()
        
        day_name = pd.Timestamp(expiry_date).day_name()
        logger.info(f"{dte_days} DTE mode: Expiry on {expiry_date} ({day_name})")
    
    return expiry_date, str(expiry_date)


def create_expiry_timestamp(expiry_date: str, expiry_hour: int = 15) -> pd.Timestamp:
    """
    Create a timezone-aware expiry timestamp at US/Eastern time
    
    Args:
        expiry_date: Expiry date (YYYY-MM-DD format)
        expiry_hour: Hour of expiry in 24-hour format (0-23). Default 15 (3 PM)
    
    Returns:
        pd.Timestamp with US/Eastern timezone
    
    Raises:
        ValueError: If expiry_hour is not 0-23
    
    Example:
        >>> create_expiry_timestamp("2026-03-09", 15)
        Timestamp('2026-03-09 15:00:00-0500', tz='US/Eastern')
    """
    if not 0 <= expiry_hour <= 23:
        raise ValueError(f"expiry_hour must be 0-23, got {expiry_hour}")
    
    expiry_ts = pd.Timestamp(expiry_date, tz='US/Eastern') + pd.Timedelta(hours=expiry_hour)
    return expiry_ts


def get_trading_dates(start_date: str, end_date: str) -> list:
    """
    Get all trading dates (business days) between start and end dates
    
    Args:
        start_date: Start date (YYYY-MM-DD)
        end_date: End date (YYYY-MM-DD)
    
    Returns:
        List of pd.Timestamp objects for all trading days
    
    Example:
        >>> get_trading_dates("2026-03-02", "2026-03-06")
        [Timestamp('2026-03-02'), Timestamp('2026-03-03'), Timestamp('2026-03-04'),
         Timestamp('2026-03-05'), Timestamp('2026-03-06')]
    """
    all_dates = pd.bdate_range(start=start_date, end=end_date)
    
    # Filter out holidays
    trading_dates = [d for d in all_dates if is_trading_day(d)]
    
    return trading_dates


def validate_date_range(start_date: str, end_date: str) -> bool:
    """
    Validate that start_date <= end_date and both are valid dates
    
    Args:
        start_date: Start date (YYYY-MM-DD)
        end_date: End date (YYYY-MM-DD)
    
    Returns:
        True if valid
    
    Raises:
        ValueError: If dates are invalid or start > end
    """
    try:
        start = pd.to_datetime(start_date).date()
        end = pd.to_datetime(end_date).date()
    except Exception as e:
        raise ValueError(f"Invalid date format: {e}") from e
    
    if start > end:
        raise ValueError(f"start_date ({start}) must be <= end_date ({end})")
    
    return True


# ========== TIME CALCULATIONS ==========

def calculate_time_to_expiry(current_ts: pd.Timestamp, expiry_ts: pd.Timestamp) -> dict:
    """
    Calculate time remaining until expiry
    
    Args:
        current_ts: Current timestamp (must be tz-aware, US/Eastern)
        expiry_ts: Expiry timestamp (must be tz-aware, US/Eastern)
    
    Returns:
        Dict with keys: seconds, years, days, hours, minutes
    
    Example:
        >>> current = pd.Timestamp("2026-03-09 09:30:00", tz="US/Eastern")
        >>> expiry = pd.Timestamp("2026-03-09 15:00:00", tz="US/Eastern")
        >>> calculate_time_to_expiry(current, expiry)
        {'seconds': 19800, 'years': 0.0543, 'days': 0.25, 'hours': 5.5, 'minutes': 330}
    """
    if current_ts.tz is None or expiry_ts.tz is None:
        raise ValueError("Both timestamps must be timezone-aware")
    
    time_diff = (expiry_ts - current_ts).total_seconds()
    
    return {
        "seconds": max(time_diff, 0.0),
        "years": max(time_diff, 0.0) / (365.0 * 24 * 60 * 60),
        "days": max(time_diff, 0.0) / (24 * 60 * 60),
        "hours": max(time_diff, 0.0) / (60 * 60),
        "minutes": max(time_diff, 0.0) / 60,
    }


# ========== LOGGING UTILITIES ==========

def log_trading_session_start(trade_date: str, dte_days: int, expiry_date: str):
    """Log the start of a trading session"""
    logger.info("=" * 70)
    logger.info("SPX TRADING SESSION")
    logger.info("=" * 70)
    logger.info(f"Trade Date: {trade_date}")
    logger.info(f"DTE: {dte_days} days")
    logger.info(f"Expiry Date: {expiry_date}")
    logger.info("=" * 70)


def log_trading_session_end(total_pnl: float, trade_count: int):
    """Log the end of a trading session"""
    logger.info("=" * 70)
    logger.info("TRADING SESSION COMPLETE")
    logger.info("=" * 70)
    logger.info(f"Total Trades: {trade_count}")
    logger.info(f"Total PnL: ${total_pnl:,.2f}")
    logger.info("=" * 70)