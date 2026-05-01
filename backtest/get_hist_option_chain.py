# -*- coding: utf-8 -*-
"""
IB API - Extract Historical Data of expired SPX option contracts using Black-Scholes
"""

from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract
import pandas as pd
import numpy as np
import datetime as dt
from scipy import stats
import threading
import time

ticker = "SPX"
right = "C"          # "C" for call, "P" for put
strike = 6590        # example strike
expiry = "20260325"  # YYYYMMDD

class TradeApp(EWrapper, EClient):
    def __init__(self):
        EClient.__init__(self, self)
        self.hist_data = None
        self.impVol_data = None

    def historicalData(self, reqId, bar):
        row = {"Date": bar.date, "Open": bar.open, "High": bar.high, "Low": bar.low, "Close": bar.close}
        if reqId == 0:
            if self.hist_data is None:
                self.hist_data = [row]
            else:
                self.hist_data.append(row)
        elif reqId == 100:
            if self.impVol_data is None:
                self.impVol_data = [row]
            else:
                self.impVol_data.append(row)
        print(f"reqID:{reqId}, date:{bar.date}, open:{bar.open}, high:{bar.high}, low:{bar.low}, close:{bar.close}")

    def historicalDataEnd(self, reqId, start, end):
        super().historicalDataEnd(reqId, start, end)
        print("HistoricalDataEnd. ReqId:", reqId, "from", start, "to", end)
        global hist_data_event
        hist_data_event.set()

def spx_index():
    contract = Contract()
    contract.symbol = "SPX"
    contract.secType = "IND"
    contract.currency = "USD"
    contract.exchange = "CBOE"
    return contract

def histData(req_num, contract, duration, candle_size):
    app.reqHistoricalData(
        reqId=req_num,
        contract=contract,
        endDateTime='',
        durationStr=duration,
        barSizeSetting=candle_size,
        whatToShow='ADJUSTED_LAST',
        useRTH=1,
        formatDate=1,
        keepUpToDate=0,
        chartOptions=[]
    )

def impVolData(req_num, contract, duration, candle_size):
    app.reqHistoricalData(
        reqId=req_num,
        contract=contract,
        endDateTime='',
        durationStr=duration,
        barSizeSetting=candle_size,
        whatToShow='OPTION_IMPLIED_VOLATILITY',
        useRTH=1,
        formatDate=1,
        keepUpToDate=0,
        chartOptions=[]
    )

def websocket_con():
    app.run()

app = TradeApp()
app.connect(host='127.0.0.1', port=7497, clientId=4)

con_thread = threading.Thread(target=websocket_con, daemon=True)
con_thread.start()
time.sleep(1)

hist_data_event = threading.Event()

contract = spx_index()

hist_data_event.clear()
histData(0, contract, '5 D', '5 mins')
hist_data_event.wait()

hist_data_event.clear()
impVolData(100, contract, '5 D', '5 mins')
hist_data_event.wait()

historicalData = pd.DataFrame(app.hist_data).set_index("Date")
impVolDataDF = pd.DataFrame(app.impVol_data).set_index("Date")

def black_scholes(stock_price, strike_price, vol, time_to_expiry, rate, right="Call"):
    d1 = (np.log(stock_price / strike_price) + (rate + 0.5 * vol**2) * time_to_expiry) / (vol * np.sqrt(time_to_expiry))
    d2 = d1 - vol * np.sqrt(time_to_expiry)
    nd1 = stats.norm.cdf(d1)
    nd2 = stats.norm.cdf(d2)
    n_d1 = stats.norm.cdf(-d1)
    n_d2 = stats.norm.cdf(-d2)
    if right.capitalize()[0] == "C":
        return (stock_price * nd1) - (strike_price * np.exp(-rate * time_to_expiry) * nd2)
    else:
        return (strike_price * np.exp(-rate * time_to_expiry) * n_d2) - (stock_price * n_d1)

def dayCount(DF, expiry):
    DF["maturity"] = ((dt.datetime.strptime(expiry, "%Y%m%d") - pd.to_datetime(DF.index)).days) / 365

dayCount(historicalData, expiry)
dayCount(impVolDataDF, expiry)

historicalData = historicalData[historicalData["maturity"] > 0]
impVolDataDF = impVolDataDF[impVolDataDF["maturity"] > 0]

option_symbol = f"{ticker} {expiry[2:]}{right}{strike}"
option_prices = pd.DataFrame(index=historicalData.index, columns=["Open", "High", "Low", "Close"])

for column in ["Open", "High", "Low", "Close"]:
    option_prices[column] = black_scholes(
        historicalData[column].astype(float),
        strike,
        impVolDataDF[column].astype(float),
        historicalData["maturity"].astype(float),
        0.03,
        right
    ).round(2)

print(option_symbol)
print(option_prices.head())
