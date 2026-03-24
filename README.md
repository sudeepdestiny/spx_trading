# SPX Trading

Systematic SPX options trading and backtesting framework in Python.  
This project focuses on building, testing, and iterating SPX options strategies with clean data handling, reproducible research, and configurable live/ paper trading workflows. [page:1]

## Features

- Daily SPX options data ingestion and preprocessing. [page:1]  
- Option chain construction and filtering via a configurable **chain** builder. [page:1]  
- Strategy engine that generates trades based on rules (delta, DTE, time window, etc.). [page:1]  
- Greeks and PnL calculations for risk-aware backtesting. [page:1]  
- CSV outputs for trades and daily equity curve for further analysis (e.g. in notebooks). [page:1]  
- Jupyter notebook (`analysis.ipynb`) for exploratory analysis and visualization. [page:1]  

## Project structure

```text
spx_trading/
├── __pycache__/          # Python cache files (ignored in Git)
├── data/                 # Raw / downloaded market data
├── logs/                 # Application and backtest logs
├── processed/            # Cleaned / transformed datasets
├── analysis.ipynb        # Research & visualization notebook
├── api_client.py         # Broker / data API client
├── backtest_daily_summary.csv  # Equity curve & daily metrics
├── backtest_trades.csv         # Executed trades from backtests
├── chain_builder.py      # Option chain construction logic
├── config.py             # Configuration (symbols, dates, params)
├── data.py               # Data loading and preprocessing
├── duckdb_loader.py      # DuckDB integration for local storage
├── greeks_engine.py      # Greeks and pricing utilities
├── main.py               # Main entry point for running backtests
├── requirements.txt      # Python dependencies
├── strategy.py           # Strategy rules and position sizing
└── utils.py              # Shared helpers and utilities
Installation
Clone the repository:

bash
git clone git@github.com:sudeepdestiny/spx_trading.git
cd spx_trading
(Recommended) Create and activate a virtual environment:

bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
Install dependencies:

bash
pip install -r requirements.txt
[page:1]

Usage
Run a backtest
The typical entry point is main.py, which wires together data loading, chain building, strategy logic, and result export. [page:1]

bash
python main.py
By default, configuration (e.g. date range, symbol, strategy parameters) is controlled via config.py. Edit this file to change:

Backtest start / end dates

Underlying symbol (e.g. SPX)

Strategy-specific parameters (delta bands, DTE, lot size, etc.) [page:1]

Explore results
Inspect backtest_trades.csv to see individual trades.

Inspect backtest_daily_summary.csv for daily equity, PnL, and risk metrics.

Open analysis.ipynb in Jupyter / VS Code to run additional charts and analytics. [page:1]

Configuration
config.py is the central place to configure: [page:1]

Data paths (raw and processed)

API keys / environment variables (if used)

Default strategy and risk parameters

Logging level and output locations

Sensitive values (e.g. API keys) should be set via environment variables and not committed to Git.

Roadmap
Planned improvements (subject to change):

Additional SPX strategies (iron condors, butterflies, intraday scalping).

More robust execution simulation (slippage, commissions, partial fills).

Performance dashboards (web UI or Streamlit).

Automated daily data downloads and scheduled backtests.

Contributing
This is currently a personal research project, but issues and ideas are welcome via GitHub Issues. [page:1]

If you fork this repo:

Keep strategy logic modular inside strategy.py.

Add new data sources as separate modules under data/ or new loaders.

Update this README and requirements.txt when you extend functionality.
