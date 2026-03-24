from pathlib import Path
from duckdb_loader import SPXDuckDB
from config import PROCESSED_PATH
import pandas as pd
import yfinance as yf
import logging

class ChainBuilder:
    def __init__(self):
        self.db = SPXDuckDB()
        self.db.create_view()

    def get_daily_spot_price(self, trade_date: str):
        try:
            start_date = pd.to_datetime(trade_date)
            end_date = start_date + pd.Timedelta(days=1)

            spx_data = yf.download(
                "^SPX",
                start=start_date,
                end=end_date,
                interval="1d",
                progress=False,
                auto_adjust=False
            )

            if spx_data.empty:
                return None

            return float(spx_data["Open"].iloc[0])
        except Exception as e:
            logging.info(f"Error fetching daily SPX spot for {trade_date}: {e}")
            return None

    def build_snapshot(self, trade_date: str, interval: str = "5 minute"):
        query = f"""
        WITH base AS (
            SELECT
                ticker,
                trade_date,
                strike,
                opt_type,
                price,
                bid,
                ask,
                size,
                side,
                to_timestamp(sip_timestamp / 1000000000.0) AS ts,
                date_trunc('minute', to_timestamp(sip_timestamp / 1000000000.0)) AS minute_ts
            FROM spx_trades
            WHERE trade_date = DATE '{trade_date}'
        ),
        bucketed AS (
            SELECT
                *,
                CASE
                    WHEN '{interval}' = '1 minute' THEN minute_ts
                    ELSE
                        date_trunc('hour', ts)
                        + FLOOR(EXTRACT(MINUTE FROM ts) / 5) * INTERVAL 5 MINUTE
                END AS bucket_ts
            FROM base
        ),
        ranked AS (
            SELECT
                *,
                ROW_NUMBER() OVER (
                    PARTITION BY trade_date, bucket_ts, strike, opt_type
                    ORDER BY ts DESC
                ) AS rn
            FROM bucketed
        )
        SELECT
            trade_date,
            bucket_ts,
            strike,
            opt_type,
            price AS last_price,
            bid,
            ask,
            (bid + ask) / 2.0 AS mid_price,
            size,
            side,
            ticker
        FROM ranked
        WHERE rn = 1
        ORDER BY bucket_ts, strike, opt_type
        """
        return self.db.con.execute(query).df()

    def build_option_chain(self, trade_date: str, interval: str = "5 minute"):
        query = f"""
        WITH snapshot AS (
            WITH base AS (
                SELECT
                    ticker,
                    trade_date,
                    strike,
                    opt_type,
                    price,
                    bid,
                    ask,
                    size,
                    side,
                    to_timestamp(sip_timestamp / 1000000000.0) AS ts,
                    date_trunc('minute', to_timestamp(sip_timestamp / 1000000000.0)) AS minute_ts
                FROM spx_trades
                WHERE trade_date = DATE '{trade_date}'
            ),
            bucketed AS (
                SELECT
                    *,
                    CASE
                        WHEN '{interval}' = '1 minute' THEN minute_ts
                        ELSE
                            date_trunc('hour', ts)
                            + FLOOR(EXTRACT(MINUTE FROM ts) / 5) * INTERVAL 5 MINUTE
                    END AS bucket_ts
                FROM base
            ),
            ranked AS (
                SELECT
                    *,
                    ROW_NUMBER() OVER (
                        PARTITION BY trade_date, bucket_ts, strike, opt_type
                        ORDER BY ts DESC
                    ) AS rn
                FROM bucketed
            )
            SELECT
                trade_date,
                bucket_ts,
                strike,
                opt_type,
                price AS last_price,
                bid,
                ask,
                (bid + ask) / 2.0 AS mid_price
            FROM ranked
            WHERE rn = 1
        )
        SELECT
            trade_date,
            bucket_ts,
            strike,
            MAX(CASE WHEN opt_type = 'C' THEN last_price END) AS call_price,
            MAX(CASE WHEN opt_type = 'C' THEN bid END) AS call_bid,
            MAX(CASE WHEN opt_type = 'C' THEN ask END) AS call_ask,
            MAX(CASE WHEN opt_type = 'C' THEN mid_price END) AS call_mid,
            MAX(CASE WHEN opt_type = 'P' THEN last_price END) AS put_price,
            MAX(CASE WHEN opt_type = 'P' THEN bid END) AS put_bid,
            MAX(CASE WHEN opt_type = 'P' THEN ask END) AS put_ask,
            MAX(CASE WHEN opt_type = 'P' THEN mid_price END) AS put_mid
        FROM snapshot
        GROUP BY trade_date, bucket_ts, strike
        ORDER BY bucket_ts, strike
        """
        option_chain_df = self.db.con.execute(query).df()

        if option_chain_df.empty:
            return option_chain_df

        daily_spot = self.get_daily_spot_price(trade_date)

        if daily_spot is not None:
            option_chain_df["spot_price"] = float(daily_spot)
            logging.info(f"Added daily spot_price={daily_spot} to option chain for {trade_date}")
        else:
            fallback_spot = float(option_chain_df["strike"].median())
            option_chain_df["spot_price"] = fallback_spot
            logging.info(f"Yahoo spot unavailable for {trade_date}, using median strike={fallback_spot}")

        return option_chain_df

    def save_option_chain(self, trade_date: str, interval: str = "5 minute"):
        output_file = Path(PROCESSED_PATH) / f"option_chain_{interval.replace(' ', '')}_{trade_date}.parquet"
        option_chain_df = self.build_option_chain(trade_date, interval=interval)

        if option_chain_df.empty:
            return output_file

        option_chain_df.to_parquet(output_file, index=False)
        return output_file

    def load_or_build_option_chain(self, trade_date, interval="5 minute", processed_dir="processed"):
        processed_path = Path(processed_dir)
        processed_path.mkdir(parents=True, exist_ok=True)

        filename = f"option_chain_{interval.replace(' ', '')}_{trade_date}.parquet"
        file_path = processed_path / filename

        if file_path.exists():
            logging.info(f"Loading cached option chain: {file_path}")
            return pd.read_parquet(file_path), str(file_path)

        logging.info(f"Building option chain for {trade_date}...")
        option_chain_df = self.build_option_chain(trade_date, interval=interval)

        if option_chain_df.empty:
            return option_chain_df, str(file_path)

        option_chain_df.to_parquet(file_path, index=False)
        logging.info(f"Saved processed chain to: {file_path}")
        return option_chain_df, str(file_path)
