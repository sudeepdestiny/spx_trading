# -*- coding: utf-8 -*-
"""
IB API - Extract Historical Data of expired option contracts using black scholes formula

@author: Mayank Rasu (http://rasuquant.com/wp/)
"""

# Import libraries
from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract
import pandas as pd
import numpy as np
import datetime as dt
from scipy import stats
import threading
import time

tickers = ["FB","AMZN","CSCO"]
rights = ["C","C","P"]
strikes = [320,3380,60]
expiry = "20211001"

class TradeApp(EWrapper, EClient): 
    def __init__(self): 
        EClient.__init__(self, self) 
        self.hist_data = {}
        self.impVol_data = {}
        
    def historicalData(self, reqId, bar):
        if reqId < 100:
            if tickers[reqId] not in self.hist_data:
                self.hist_data[tickers[reqId]] = [{"Date":bar.date,"Open":bar.open,"High":bar.high,"Low":bar.low,"Close":bar.close}]
            else:
                self.hist_data[tickers[reqId]].append({"Date":bar.date,"Open":bar.open,"High":bar.high,"Low":bar.low,"Close":bar.close})
        else:
            if tickers[reqId-100] not in self.impVol_data:
                self.impVol_data[tickers[reqId-100]] = [{"Date":bar.date,"Open":bar.open,"High":bar.high,"Low":bar.low,"Close":bar.close}]
            else:
                self.impVol_data[tickers[reqId-100]].append({"Date":bar.date,"Open":bar.open,"High":bar.high,"Low":bar.low,"Close":bar.close})
            
        print("reqID:{}, date:{}, open:{}, high:{}, low:{}, close:{}".format(reqId,bar.date,bar.open,bar.high,bar.low,bar.close))
        
    def historicalDataEnd(self, reqId, start, end):
        super().historicalDataEnd(reqId, start, end)
        print("HistoricalDataEnd. ReqId:", reqId, "from", start, "to", end)
        if reqId < 100:
            self.hist_data[tickers[reqId]] = pd.DataFrame(self.hist_data[tickers[reqId]])
            self.hist_data[tickers[reqId]].set_index("Date",inplace=True)
        else:
            self.impVol_data[tickers[reqId-100]] = pd.DataFrame(self.impVol_data[tickers[reqId-100]])
            self.impVol_data[tickers[reqId-100]].set_index("Date",inplace=True)
        hist_data_event.set()
        

def usTechStk(symbol,sec_type="STK",currency="USD",exchange="ISLAND"):
    contract = Contract()
    contract.symbol = symbol
    contract.secType = sec_type
    contract.currency = currency
    contract.exchange = exchange
    return contract 

def histData(req_num,contract,duration,candle_size):
    """extracts historical data"""
    app.reqHistoricalData(reqId=req_num, 
                          contract=contract,
                          endDateTime='',
                          durationStr=duration,
                          barSizeSetting=candle_size,
                          whatToShow='ADJUSTED_LAST',
                          useRTH=1,
                          formatDate=1,
                          keepUpToDate=0,
                          chartOptions=[])	 # EClient function to request contract details
    
def impVolData(req_num,contract,duration,candle_size):
    """extracts historical data"""
    app.reqHistoricalData(reqId=req_num, 
                          contract=contract,
                          endDateTime='',
                          durationStr=duration,
                          barSizeSetting=candle_size,
                          whatToShow='OPTION_IMPLIED_VOLATILITY',
                          useRTH=1,
                          formatDate=1,
                          keepUpToDate=0,
                          chartOptions=[])	

def websocket_con():
    app.run()
    
app = TradeApp()
app.connect(host='127.0.0.1', port=7497, clientId=23) #port 4002 for ib gateway paper trading/7497 for TWS paper trading
con_thread = threading.Thread(target=websocket_con, daemon=True)
con_thread.start()
time.sleep(1) # some latency added to ensure that the connection is established

hist_data_event = threading.Event()

for ticker in tickers:
    contract = usTechStk(ticker)
    hist_data_event.clear()
    histData(tickers.index(ticker),contract,'200 D', '1 day')
    hist_data_event.wait()
    hist_data_event.clear()
    impVolData(100+tickers.index(ticker),contract,'200 D', '1 day')
    hist_data_event.wait()
    

#extract and store historical data in dataframe
historicalData = app.hist_data
impVolData = app.impVol_data


#################Black Scholes Option pricing Model############################
def black_scholes(stock_price,strike_price,vol,time,rate,right="Call"):
    d1 = (np.log(stock_price/strike_price) + (rate + 0.5* vol**2)*time)/(vol*np.sqrt(time))
    d2 = (np.log(stock_price/strike_price) + (rate - 0.5* vol**2)*time)/(vol*np.sqrt(time))
    nd1 = stats.norm.cdf(d1)
    nd2 = stats.norm.cdf(d2)
    n_d1 = stats.norm.cdf(-1*d1)
    n_d2 = stats.norm.cdf(-1*d2)
    if right.capitalize()[0] == "C":
        return round((stock_price*nd1) - (strike_price*np.exp(-1*rate*time)*nd2),2)
    else:
        return round((strike_price*np.exp(-1*rate*time)*n_d2) - (stock_price*n_d1),2)

 
def dayCount(DF,expiry):
    """function to calculate days to maturity for each day in past"""
    DF["maturity"] = ((dt.datetime.strptime(expiry, "%Y%m%d") - pd.to_datetime(DF.index)).days)/365


#calculate historical option price using Black Scholes Formula    
option_prices = {}
for ticker in tickers:
    local_symbol = ticker+" "+expiry[2:]+rights[tickers.index(ticker)]+str(strikes[tickers.index(ticker)])
    option_prices[local_symbol] = pd.DataFrame(columns=["Open","High","Low","Close"])
    dayCount(historicalData[ticker],expiry)
    dayCount(impVolData[ticker],expiry)
    historicalData[ticker] = historicalData[ticker][historicalData[ticker]["maturity"] > 0]
    impVolData[ticker] = impVolData[ticker][impVolData[ticker]["maturity"] > 0]
    for column in ["Open","High","Low","Close"]:
        option_prices[local_symbol][column] = black_scholes(historicalData[ticker][column],
                                                            strikes[tickers.index(ticker)],
                                                            impVolData[ticker][column],
                                                            historicalData[ticker]["maturity"],
                                                            0.03,
                                                            rights[tickers.index(ticker)])

