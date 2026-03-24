# SPX Trading

Systematic SPX options trading and backtesting framework in Python.  
The project focuses on building, testing, and iterating SPX options strategies with clean data handling, reproducible research, and configurable live/paper trading workflows.

## Features

- Daily SPX options data ingestion and preprocessing.  
- Option chain construction and filtering via a configurable chain builder.  
- Strategy engine that generates trades based on rules (delta, DTE, time window, etc.).  
- Greeks and PnL calculations for risk-aware backtesting.  
- CSV outputs for trades and daily equity curve for further analysis (e.g. in notebooks).  
- Jupyter notebook (`analysis.ipynb`) for exploratory analysis and visualization.  

## Project structure

```text
spx_trading/
├── __pycache__/                # Python cache files (ignored in Git)
├── data/                       # Raw / downloaded market data
├── logs/                       # Application and backtest logs
├── processed/                  # Cleaned / transformed datasets
├── analysis.ipynb              # Research & visualization notebook
├── api_client.py               # Broker / data API client
├── backtest_daily_summary.csv  # Equity curve & daily metrics
├── backtest_trades.csv         # Executed trades from backtests
├── chain_builder.py            # Option chain construction logic
├── config.py                   # Configuration (symbols, dates, params)
├── data.py                     # Data loading and preprocessing
├── duckdb_loader.py            # DuckDB integration for local storage
├── greeks_engine.py            # Greeks and pricing utilities
├── main.py                     # Main entry point for running backtests
├── requirements.txt            # Python dependencies
├── strategy.py                 # Strategy rules and position sizing
└── utils.py                    # Shared helpers and utilities
```
## Installation

Clone the repository:

```bash
git clone git@github.com:sudeepdestiny/spx_trading.git
cd spx_trading
```

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
```

Install dependencies:

```bash
pip install -r requirements.txt
```

**Usage**
Run a backtest

The typical entry point is main.py, which wires together data loading, chain building, strategy logic, and result export.

```bash
python main.py
```

By default, configuration (e.g. date range, symbol, strategy parameters) is controlled via config.py. Edit this file to change:

Backtest start/end dates

Underlying symbol (e.g. SPX)

Strategy-specific parameters (delta bands, DTE, lot size, etc.)

Explore results

Inspect backtest_trades.csv to see individual trades.

Inspect backtest_daily_summary.csv for daily equity, PnL, and risk metrics.

Open analysis.ipynb in Jupyter / VS Code to run additional charts and analytics.

**Configuration**

config.py is the central place to configure:

Data paths (raw and processed)

API keys / environment variables (if used)

Default strategy and risk parameters

Logging level and output locations

Sensitive values (e.g. API keys) should be set via environment variables and not committed to Git.

Strategy details – Delta-neutral adjustment
The default strategy implements an intraday, delta-aware four-leg structure on SPX options. For each trading day, it:

Builds a filtered option chain and estimates a synthetic spot using call–put parity around ATM.

Selects short call and put strikes whose deltas are near a target sell_delta, then adds long call and put hedges at a fixed hedge_dist away from the shorts, creating a short strangle with wings.

Opens all four legs as one “cycle” and tracks net credit, breakeven band, and per-cycle PnL.

During the session (between configured start and end times), the engine periodically recomputes portfolio delta and PnL using updated option prices and Black–Scholes deltas. If absolute delta stays within a configurable band, the position is left untouched; otherwise, the strategy performs a delta-based adjustment.

When |delta| exceeds pos_delta_thresh and the cycle has remaining adjustment capacity, the strategy:

Identifies the dominant short leg (call if delta > 0, put if delta < 0) that is driving the imbalance.

Closes that short leg together with its paired hedge (short_strike ± hedge_dist).

Chooses a new short on the same side with a delta that better offsets the current portfolio delta and re-establishes a new hedge at the same distance from the new short strike.

Optionally closes the opposite-side short+hedge if strikes cross or overlap after the adjustment to prevent an inverted structure.

Each cycle records intraday max and min PnL, total number of adjustments, and a final outcome. At the end of the day (or when the configured exit condition is met), all open legs are closed, and the full trade history plus daily summary are written to backtest_trades.csv and backtest_daily_summary.csv for analysis.

**Roadmap**

Planned improvements (subject to change):

Additional SPX strategies (iron condors, butterflies, intraday scalping).

More robust execution simulation (slippage, commissions, partial fills).

Performance dashboards (web UI or Streamlit).

Automated daily data downloads and scheduled backtests.

**Contributing**

This is currently a personal research project, but issues and ideas are welcome via GitHub Issues.

If you fork this repo:

Keep strategy logic modular inside strategy.py.

Add new data sources as separate modules under data/ or via new loaders.

Update this README and requirements.txt when you extend functionality.
