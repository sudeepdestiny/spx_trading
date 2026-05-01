STRATEGY_CONFIG = {
    'symbol': '^SPX',
    'sell_delta': 0.15,
    'pos_delta_thresh': 0.25,
    'hedge_dist': 100,
    'lot_size': 100,
    'dte_days': 4,  # ← Change to 0 for same-day expiry
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
    "iv_bar_size": "30 mins",      # ← Add
}

import platform
from pathlib import Path

system = platform.system()

if system == "Windows":
    ROOT = Path("E:/Study/trading 2025/Trading2025")
elif system == "Linux":
    ROOT = Path("/home/opc/spx_trading")
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

if system == "Linux":
    apikey_config["ibkr_client_id"] = 1
    apikey_config["ibkr_host"] = ""
    apikey_config["ibkr_port"]=4002
elif system == "Windows":
    apikey_config["ibkr_client_id"] = 1
    apikey_config["ibkr_host"] = ""
    apikey_config["ibkr_port"]=7497
elif system == "Darwin":
    apikey_config["ibkr_client_id"] = 3
    apikey_config["ibkr_host"] = ""
    apikey_config["ibkr_port"]=7497