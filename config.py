STRATEGY_CONFIG  = {
    'symbol': '^SPX',
    'sell_delta': 0.30,
    'pos_delta_thresh': 0.25,
    'hedge_dist': 50,
    'lot_size': 100,
    'expiry_days': 7,
    'start_date': '2025-01-01',
    'end_date': '2026-02-19',
    'volatility': 0.15,
    'risk_free_rate': 0.05,
    "default_iv": 0.20,
    "snapshot_interval": "5 minute",
    "max_adjusts": 3,
    "closing_diff": 0
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
