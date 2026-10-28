STRATEGY_CONFIG = {
    'symbol': '^SPX',
    'sell_delta': 0.15,
    'pos_delta_thresh': 0.20,
    'hedge_dist': 80,
    'lot_size': 100,
    'dte_days': 6,  # ← Change to 0 for same-day expiry
    'start_date': '2026-03-02',
    'end_date': '2026-03-06',  # ← Single day for 0 DTE
    'volatility': 0.15,
    'risk_free_rate': 0.05,
    "default_iv": 0.20,
    "snapshot_interval": "5 minute",
    "max_adjusts": 3,
    "closing_diff": 0,
    "strike_step": 5,
    "pct_band": 0.10,
    "expiry_hour": 15,  # ← Earlier for 0 DTE (before market close)
    "price_bar_size": "5 mins",    # ← Add
    "iv_bar_size": "30 mins",
    "max_loss_stop_loss": 250.0,  # Max allowable cycle loss ($) before triggering stop-loss exit
}

import os
import platform
from pathlib import Path

system = platform.system()

if system == "Windows":
    ROOT = Path("E:/Study/trading 2025/Trading2025")
elif system == "Linux":
    ROOT = Path("/home/opc/dev/spx_trading")
elif system == "Darwin":
    ROOT = Path("/Users/sudeepdas/Documents/Study/spx_trading")
else:
    raise EnvironmentError(f"Unsupported system: {system}")

DATA_PATH = ROOT / "data" / "spx"
LOG_PATH = ROOT / "logs"
PROCESSED_PATH = ROOT / "processed"
DB_PATH = ROOT / "data" / "spx.duckdb"

for p in [DATA_PATH, LOG_PATH, PROCESSED_PATH]:
    p.mkdir(parents=True, exist_ok=True)

print(f"Running on {system}")
print(f"DATA_PATH = {DATA_PATH}")
print(f"DB_PATH = {DB_PATH}")

apikey_config = {
}

if system == "Linux":
    apikey_config["ibkr_paper_host"] = "127.0.0.1"
    apikey_config["ibkr_live_host"] = "127.0.0.1"
    apikey_config["ibkr_paper_port"] = 4002
    apikey_config["ibkr_live_port"] = 4001
    apikey_config["ibkr_paper_client_id"] = 1
    apikey_config["ibkr_live_client_id"] = 2
    apikey_config["ibkr_live_account"] = "U24990147"  # Override account for live orders
elif system == "Windows":
    apikey_config["ibkr_paper_host"] = "127.0.0.1"
    apikey_config["ibkr_live_host"] = "127.0.0.1"
    apikey_config["ibkr_paper_port"] = 7497
    apikey_config["ibkr_live_port"] = 7496
    apikey_config["ibkr_paper_client_id"] = 1
    apikey_config["ibkr_live_client_id"] = 2
    apikey_config["ibkr_live_account"] = "U24990147"  # Override account for live orders
elif system == "Darwin":
    apikey_config["ibkr_paper_host"] = "127.0.0.1"
    apikey_config["ibkr_live_host"] = "127.0.0.1"
    apikey_config["ibkr_paper_port"] = 7497
    apikey_config["ibkr_live_port"] = 7496
    apikey_config["ibkr_paper_client_id"] = 1
    apikey_config["ibkr_live_client_id"] = 2
    apikey_config["ibkr_live_account"] = "U24990147"  # Override account for live orders

apikey_config["ibkr_trading_mode"] = os.getenv("IBKR_TRADING_MODE", "live").lower()


# US Market Holidays 2026
# Source: NYSE/IBKR market holiday calendar
US_MARKET_HOLIDAYS_2026 = {
    "20260101",  # New Year's Day
    "20260119",  # Martin Luther King Jr. Day
    "20260216",  # Presidents' Day
    "20260525",  # Memorial Day
    "20260703",  # Independence Day (observed)
    "20260907",  # Labor Day
    "20261126",  # Thanksgiving
    "20261225",  # Christmas
}

# US Market Holidays 2027 (for forward planning)
US_MARKET_HOLIDAYS_2027 = {
    "20270101",  # New Year's Day
    "20270118",  # Martin Luther King Jr. Day
    "20270215",  # Presidents' Day
    "20270531",  # Memorial Day
    "20270705",  # Independence Day (observed)
    "20270906",  # Labor Day
    "20271125",  # Thanksgiving
    "20271224",  # Christmas (observed)
}

# Combined holiday set
US_MARKET_HOLIDAYS = US_MARKET_HOLIDAYS_2026 | US_MARKET_HOLIDAYS_2027
