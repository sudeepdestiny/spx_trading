from pathlib import Path
from duckdb_loader import SPXDuckDB
from config import PROCESSED_PATH
import pandas as pd
import yfinance as yf
import logging
import numpy as np
from math import log, sqrt, exp, erf, pi
from pandas.tseries.offsets import BusinessDay
from config import apikey_config,DATA_PATH, LOG_PATH, STRATEGY_CONFIG,PROCESSED_PATH

DATA_PATH = Path(DATA_PATH)         # change if needed
OUTPUT_PATH = Path(PROCESSED_PATH)  # change if needed
OUTPUT_PATH.mkdir(parents=True, exist_ok=True)
class ChainBuilder:
    def __init__(self):
        self.db = SPXDuckDB()
        self.db.create_view()
        
    # ...existing code...
    def load_spx_intraday_bars(self, trade_date: str, data_path: Path = DATA_PATH) -> pd.DataFrame:
        file_name = f"spx_5mins_{trade_date.replace('-', '_')}.parquet"
        file_path = data_path / file_name

        if not file_path.exists():
            raise FileNotFoundError(f"Missing SPX intraday file: {file_path}")

        df = pd.read_parquet(file_path).copy()
        if df.empty:
            raise ValueError(f"Empty SPX intraday file: {file_path}")

        df["Date"] = pd.to_datetime(df["Date"])
        df = df.sort_values("Date").reset_index(drop=True)

        required = ["Date", "Open", "High", "Low", "Close"]
        missing = [c for c in required if c not in df.columns]
        if missing:
            raise ValueError(f"SPX intraday file missing columns: {missing}")

        return df.rename(columns={
            "Date": "timestamp",
            "Open": "spx_open",
            "High": "spx_high",
            "Low": "spx_low",
            "Close": "spx_close",
        })


    def load_underlying_iv(self, trade_date: str, data_path: Path = DATA_PATH) -> float:
        file_name = f"spx_1day_option_implied_volatility_{trade_date.replace('-', '_')}.parquet"
        file_path = data_path / file_name

        if not file_path.exists():
            raise FileNotFoundError(f"Missing IV file: {file_path}")

        df = pd.read_parquet(file_path).copy()
        if df.empty:
            raise ValueError(f"Empty IV file: {file_path}")

        if "Date" in df.columns:
            df["Date"] = pd.to_datetime(df["Date"])
            df = df.sort_values("Date").reset_index(drop=True)

        iv_col = "Close" if "Close" in df.columns else df.columns[-1]
        iv_series = pd.to_numeric(df[iv_col], errors="coerce").dropna()

        if iv_series.empty:
            raise ValueError(f"No usable IV values found in {file_path}")

        iv = float(iv_series.iloc[-1])

        if iv > 3:
            iv = iv / 100.0

        return iv


    def norm_cdf(self, x: float) -> float:
        return 0.5 * (1.0 + erf(x / sqrt(2.0)))


    def norm_pdf(self, x: float) -> float:
        return (1.0 / sqrt(2.0 * pi)) * exp(-0.5 * x * x)


    def black_scholes(self, S: float, K: float, T: float, r: float, sigma: float, option_type: str):
        if any(pd.isna(v) for v in [S, K, T, r, sigma]):
            return {
                "price": np.nan,
                "delta": np.nan,
                "gamma": np.nan,
                "vega": np.nan,
                "theta": np.nan,
                "rho": np.nan,
                "d1": np.nan,
                "d2": np.nan,
            }

        if S <= 0 or K <= 0 or T <= 0 or sigma <= 0:
            return {
                "price": np.nan,
                "delta": np.nan,
                "gamma": np.nan,
                "vega": np.nan,
                "theta": np.nan,
                "rho": np.nan,
                "d1": np.nan,
                "d2": np.nan,
            }

        d1 = (log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * sqrt(T))
        d2 = d1 - sigma * sqrt(T)

        Nd1 = self.norm_cdf(d1)
        Nd2 = self.norm_cdf(d2)
        Nmd1 = self.norm_cdf(-d1)
        Nmd2 = self.norm_cdf(-d2)
        nd1 = self.norm_pdf(d1)

        if option_type.upper() == "C":
            price = S * Nd1 - K * exp(-r * T) * Nd2
            delta = Nd1
            rho = K * T * exp(-r * T) * Nd2 / 100.0
            theta = (-(S * nd1 * sigma) / (2 * sqrt(T)) - r * K * exp(-r * T) * Nd2) / 365.0
        else:
            price = K * exp(-r * T) * Nmd2 - S * Nmd1
            delta = Nd1 - 1.0
            rho = -K * T * exp(-r * T) * Nmd2 / 100.0
            theta = (-(S * nd1 * sigma) / (2 * sqrt(T)) + r * K * exp(-r * T) * Nmd2) / 365.0

        gamma = nd1 / (S * sigma * sqrt(T))
        vega = S * nd1 * sqrt(T) / 100.0

        return {
            "price": price,
            "delta": delta,
            "gamma": gamma,
            "vega": vega,
            "theta": theta,
            "rho": rho,
            "d1": d1,
            "d2": d2,
        }


    def generate_strike_grid(self, spot_series: pd.Series, strike_step: int = 5, pct_band: float = 0.10):
        spot_ref = float(spot_series.median())
        low = int(np.floor((spot_ref * (1 - pct_band)) / strike_step) * strike_step)
        high = int(np.ceil((spot_ref * (1 + pct_band)) / strike_step) * strike_step)
        return np.arange(low, high + strike_step, strike_step)


    def build_bs_option_chain(
        self,
        trade_date: str,
        data_path: Path = DATA_PATH,
        output_path: Path = OUTPUT_PATH,
        risk_free_rate: float = 0.045,
        strike_step: int = 5,
        pct_band: float = 0.10,
        expiry_hour: int = 15,
        dte_days: int = 0,
        expiry_date: str = None
    ):
        """
        Build Black-Scholes option chain
        
        Args:
            trade_date: Trade date (YYYY-MM-DD)
            dte_days: Days to expiry. 0 = same day expiry
            expiry_hour: Hour of expiry (0-23). For 0 DTE, typically 15 (3 PM ET)
        """
        spx_df = self.load_spx_intraday_bars(trade_date, data_path=data_path)
        base_iv = self.load_underlying_iv(trade_date, data_path=data_path)

        # Calculate expiry date based on DTE
        trade_ts = pd.Timestamp(expiry_date)
        
        # if dte_days == 0:
        #     # Same day expiry - expiry is today at expiry_hour
        #     expiry_date = trade_ts.date()
        #     logging.info(f"0 DTE mode: Expiry on same day ({expiry_date}) at {expiry_hour}:00 ET")
        # else:
        #     # N DTE - add N business days
        #     from pandas.tseries.offsets import BusinessDay
        #     expiry_date = (trade_ts + BusinessDay(dte_days)).date()
        #     logging.info(f"{dte_days} DTE mode: Expiry on {expiry_date} at {expiry_hour}:00 ET")

        #     # Verify expiry_date is a business day
        #     if pd.Timestamp(expiry_date).weekday() >= 5:  # Saturday=5, Sunday=6
        #         logging.warning(f"Calculated expiry {expiry_date} is not a business day")
        #         # Roll forward to next business day
        #         expiry_ts = expiry_ts + BusinessDay(1)
        #         expiry_date = expiry_ts.date()
                
        expiry_ts = pd.Timestamp(expiry_date, tz='US/Eastern') + pd.Timedelta(hours=expiry_hour)


        strike_grid = self.generate_strike_grid(
            spot_series=spx_df["spx_close"],
            strike_step=strike_step,
            pct_band=pct_band
        )

        rows = []
        
        ##convert spx_df timetsamp to US Eastern Time (ET) for accurate time to expiry calculations
        # Convert spx_df timestamp to US Eastern Time (ET) for accurate time to expiry calculations
        # Handle both tz-naive and tz-aware timestamps
        if spx_df["timestamp"].dt.tz is None:
            # If naive, assume UTC and convert to US/Eastern
            spx_df["timestamp"] = pd.to_datetime(spx_df["timestamp"], utc=True).dt.tz_convert("US/Eastern")
        else:
            # If already tz-aware, convert to US/Eastern
            spx_df["timestamp"] = spx_df["timestamp"].dt.tz_convert("US/Eastern")
        #spx_df["timestamp"] = pd.to_datetime(spx_df["timestamp"], utc=True).dt.tz_convert("US/Eastern")

        for _, bar in spx_df.iterrows():
            ts = pd.Timestamp(bar["timestamp"])
            spot = float(bar["spx_close"])

            # Calculate time to expiry
            

            time_diff = (expiry_ts - ts).total_seconds()
            
            # For 0 DTE, ensure we don't go negative (cap at 0)
            if dte_days == 0 and time_diff < 0:
                logging.warning(f"Timestamp {ts} is after 0 DTE expiry {expiry_ts}. Skipping.")
                continue
            
            time_to_expiry_years = max(time_diff, 0.0) / (365.0 * 24 * 60 * 60)

            for strike in strike_grid:
                call = self.black_scholes(
                    S=spot,
                    K=float(strike),
                    T=time_to_expiry_years,
                    r=risk_free_rate,
                    sigma=base_iv,
                    option_type="C"
                )
                put = self.black_scholes(
                    S=spot,
                    K=float(strike),
                    T=time_to_expiry_years,
                    r=risk_free_rate,
                    sigma=base_iv,
                    option_type="P"
                )
                rows.append({
                    "trade_date": trade_ts.date(),
                    "timestamp": ts,
                    "expiry_date": expiry_date,
                    "dte": dte_days,
                    "spot_price": spot,
                    "spx_open": float(bar["spx_open"]),
                    "spx_high": float(bar["spx_high"]),
                    "spx_low": float(bar["spx_low"]),
                    "spx_close": float(bar["spx_close"]),
                    "strike": float(strike),
                    "underlying_iv": float(base_iv),
                    "risk_free_rate": float(risk_free_rate),
                    "time_to_expiry_years": float(time_to_expiry_years),
                    "moneyness": float(strike) / spot if spot > 0 else np.nan,

                    "bs_call_price": call["price"],
                    "call_delta": call["delta"],
                    "call_gamma": call["gamma"],
                    "call_vega": call["vega"],
                    "call_theta": call["theta"],
                    "call_rho": call["rho"],
                    "call_d1": call["d1"],
                    "call_d2": call["d2"],

                    "bs_put_price": put["price"],
                    "put_delta": put["delta"],
                    "put_gamma": put["gamma"],
                    "put_vega": put["vega"],
                    "put_theta": put["theta"],
                    "put_rho": put["rho"],
                    "put_d1": put["d1"],
                    "put_d2": put["d2"],
                })

        chain_df = pd.DataFrame(rows)

        if chain_df.empty:
            logging.warning(f"Option chain is empty for {trade_date}")
            return chain_df, None

        output_file = output_path / f"option_chain_{dte_days}dte_5minute_{trade_date}.parquet"
        # chain_df.to_parquet(output_file)

        return chain_df, output_file
    
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
