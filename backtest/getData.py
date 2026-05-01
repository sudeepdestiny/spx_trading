import os
import requests
import pandas as pd
from config import apikey_config,DATA_PATH
from pathlib import Path


API_KEY = apikey_config["tiingo_api_key"]

ticker = "SPY"
start_date = "2024-01-01"
end_date = "2026-03-27"
frequency = "5min"   # Tiingo endpoint uses lowercase granularity names
spx_factor = 10.0


url = f"https://api.tiingo.com/iex/{ticker}/prices"
params = {
    "startDate": start_date,
    "endDate": end_date,
    "resampleFreq": frequency,
}
headers = {
    "Content-Type": "application/json",
    "Authorization": f"Token {API_KEY}",
}

resp = requests.get(url, params=params, headers=headers, timeout=30)
resp.raise_for_status()
data = resp.json()

if not data:
    raise ValueError("No data returned from Tiingo")

df = pd.DataFrame(data)

if "date" not in df.columns:
    raise ValueError("Expected a 'date' column in Tiingo response")

df["date"] = pd.to_datetime(df["date"])
df = df.sort_values("date").reset_index(drop=True)

df["spx_proxy_open"] = df["open"] * spx_factor
df["spx_proxy_high"] = df["high"] * spx_factor
df["spx_proxy_low"] = df["low"] * spx_factor
df["spx_proxy_close"] = df["close"] * spx_factor

df = df.round(2)
df["trade_date"] = df["date"].dt.strftime("%Y_%m_%d")

saved_files = []
for trade_date, g in df.groupby("trade_date"):
    file_name = f"spx_5mins_{trade_date}.parquet"
    file_path =  Path(DATA_PATH) / file_name
    g.drop(columns=["trade_date"]).to_parquet(file_path, index=False)
    saved_files.append(file_path)

manifest = pd.DataFrame({"file_path": saved_files})
manifest.to_csv( Path(DATA_PATH) / f"spx_parquet_manifest_{start_date}_{end_date}.csv", index=False)


print("Saved files:")
for f in saved_files:
    print(f)
