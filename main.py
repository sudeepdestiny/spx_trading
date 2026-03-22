import pandas as pd
from duckdb_loader import SPXDuckDB
from chain_builder import ChainBuilder
from strategy import DeltaNeutralStrategy
from config import STRATEGY_CONFIG


def run_for_date_range():
    print("Loading SPX data...")

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
        print(f"\n=== Processing {trade_date} ===")

        try:
            ##save the option chain only if not available, otherwise load from disk
            option_chain_df, saved_file = builder.load_or_build_option_chain(
                trade_date,
                interval=interval
             )
            if option_chain_df.empty:
                print(f"Option chain is empty for {trade_date}")
                all_summaries.append({
                    "trade_date": trade_date,
                    "status": "empty_chain",
                    "trade_count": 0,
                    "cycle_count": 0,
                    "total_pnl": 0.0
                })
                continue

            
            print(f"Saved processed chain to: {saved_file}")

            spot_proxy = float(option_chain_df["strike"].median())

            strategy = DeltaNeutralStrategy()
            trades_df, summary = strategy.run_backtest(
                option_chain_df,
                spot_price=spot_proxy,
                trade_date=trade_date
            )

            summary["trade_date"] = trade_date
            all_summaries.append(summary)

            print("Summary:", summary)

            if not trades_df.empty:
                trades_df["trade_date"] = trade_date
                all_trades.append(trades_df)

        except Exception as e:
            print(f"Error on {trade_date}: {e}")
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

    print("\n=== Range Summary ===")
    if not summaries_df.empty:
        print(summaries_df.to_string(index=False))
        summaries_df.to_csv("backtest_daily_summary.csv", index=False)
        print("\nSaved backtest_daily_summary.csv")

    if not final_trades_df.empty:
        print("\n=== Trades Sample ===")
        print(final_trades_df.head(20).to_string(index=False))
        final_trades_df.to_csv("backtest_trades.csv", index=False)
        print("\nSaved backtest_trades.csv")

        print("\n=== Aggregate Stats ===")
        print({
            "days_processed": int(len(summaries_df)),
            "days_with_trades": int(final_trades_df["trade_date"].nunique()),
            "total_trades": int(len(final_trades_df)),
            "total_pnl": float(final_trades_df["pnl"].sum())
        })
    else:
        print("\nNo trades generated for the selected date range.")


if __name__ == "__main__":
    run_for_date_range()
