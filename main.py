import pandas as pd
from duckdb_loader import SPXDuckDB
from chain_builder import ChainBuilder
from strategy import DeltaNeutralStrategy
from config import STRATEGY_CONFIG,LOG_PATH
from datetime import datetime  
import os
import logging
from pathlib import Path

# Add this near the top, after STRATEGY_CONFIG
LOG_FOLDER = STRATEGY_CONFIG.get("log_folder", "logs")  # or hardcoded "logs"
os.makedirs(LOG_FOLDER, exist_ok=True)
timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

filepath = Path(LOG_PATH) / f"Spx_trading_{timestamp}.log"


logging.basicConfig(filename=filepath,filemode='w', level=logging.INFO, format='%(asctime)s - %(message)s', datefmt='%d-%b-%y %H:%M:%S')
logging.info('Started')

def run_for_date_range():
    logging.info("Loading SPX data...")

    start_date = pd.to_datetime(STRATEGY_CONFIG["start_date"]).date()
    end_date = pd.to_datetime(STRATEGY_CONFIG["end_date"]).date()
    interval = STRATEGY_CONFIG.get("snapshot_interval", "5 minute")

    db = SPXDuckDB()
    db.create_view()

    builder = ChainBuilder()

    all_trades = []
    all_summaries = []

    trade_dates = pd.bdate_range(start=start_date, end=end_date)

    for dt in trade_dates:
        trade_date = dt.strftime("%Y-%m-%d")
        logging.info(f"\n=== Processing {trade_date} ===")
        logging.info(f"Processing {trade_date} with interval {interval}")

        try:
            ##save the option chain only if not available, otherwise load from disk
            option_chain_df, saved_file = builder.load_or_build_option_chain(
                trade_date,
                interval=interval
             )
            if option_chain_df.empty:
                logging.info(f"Option chain is empty for {trade_date}")
                all_summaries.append({
                    "trade_date": trade_date,
                    "status": "empty_chain",
                    "trade_count": 0,
                    "cycle_count": 0,
                    "total_pnl": 0.0
                })
                continue

            
            logging.info(f"Saved processed chain to: {saved_file}")

            spot_proxy = float(option_chain_df["strike"].median())

            strategy = DeltaNeutralStrategy()
            trades_df, summary = strategy.run_backtest(
                option_chain_df,
                trade_date=trade_date
            )

            summary["trade_date"] = trade_date
            all_summaries.append(summary)

            logging.info("Summary:", summary)

            if not trades_df.empty:
                trades_df["trade_date"] = trade_date
                all_trades.append(trades_df)

        except Exception as e:
            logging.info(f"Error on {trade_date}: {e}")
            all_summaries.append({
                "trade_date": trade_date,
                "status": "error",
                "trade_count": 0,
                "cycle_count": 0,
                "total_pnl": 0.0,
                "error": str(e)
            })

    summaries_df = pd.DataFrame(all_summaries)

    if all_trades:
        final_trades_df = pd.concat(all_trades, ignore_index=True)
    else:
        final_trades_df = pd.DataFrame()

    logging.info("\n=== Range Summary ===")

    # Replace the save section with this:
    if not summaries_df.empty:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        summary_filename = f"backtest_daily_summary_{timestamp}.csv"
        summary_path = os.path.join(LOG_FOLDER, summary_filename)
        
        logging.info(summaries_df.to_string(index=False))
        summaries_df.to_csv(summary_path, index=False)
        logging.info(f"\nSaved backtest_daily_summary.csv to: {summary_path}")

    if not final_trades_df.empty:
        
        trades_filename = f"backtest_trades_{timestamp}.csv"
        trades_path = os.path.join(LOG_FOLDER, trades_filename)
        
        logging.info("\n=== Trades Sample ===")
        #logging.info(final_trades_df.head(20).to_string(index=False))
        final_trades_df.to_csv(trades_path, index=False)
        logging.info(f"\nSaved backtest_trades.csv to: {trades_path}")

        logging.info("\n=== Aggregate Stats ===")
        logging.info({
            "days_processed": int(len(summaries_df)),
            "days_with_trades": int(final_trades_df["trade_date"].nunique()),
            "total_trades": int(len(final_trades_df)),
            "total_pnl": float(final_trades_df["pnl"].sum())
        })


if __name__ == "__main__":
    run_for_date_range()
