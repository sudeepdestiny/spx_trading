import yfinance as yf
import pandas as pd
import numpy as np
from scipy.stats import norm
from config import CONFIG

def get_spx_data(start_date, end_date, interval='1d'):
    data = yf.download(CONFIG['symbol'], start=start_date, end=end_date, interval=interval)
    data.columns = [c[0] if isinstance(c, tuple) else c for c in data.columns]
    return data.dropna()

def generate_mock_chain(spot_price, expiry_days=7):
    strikes = np.arange(spot_price - 300, spot_price + 300, 5)
    T = max(expiry_days / 365, 0.001)
    vol = CONFIG['volatility']
    r = CONFIG['risk_free_rate']
    chain = []
    for strike in strikes:
        d1 = (np.log(spot_price / strike) + (r + 0.5 * vol**2) * T) / (vol * np.sqrt(T))
        call_delta = norm.cdf(d1)
        put_delta = call_delta - 1
        call_price = max(0.1, abs(call_delta * spot_price * 0.01))
        put_price  = max(0.1, abs(put_delta  * spot_price * 0.01))
        chain.append({
            'StrikePrice': strike,
            'CallDelta':   round(call_delta, 4),
            'PutDelta':    round(put_delta,  4),
            'CallLTP':     round(call_price, 2),
            'PutLTP':      round(put_price,  2)
        })
    return pd.DataFrame(chain)
