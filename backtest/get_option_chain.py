from ib_insync import *
import pandas as pd
from datetime import datetime
import nest_asyncio
nest_asyncio.apply()

ib = IB()
try:
    ib.connect('127.0.0.1', 7497, clientId=3)
    ib.reqMarketDataType(3)

    spx = Index('SPX', 'CBOE')
    ib.qualifyContracts(spx)
    spot_ticker = ib.reqTickers(spx)[0]
    spot_price = spot_ticker.last or spot_ticker.close or 6605.87
    #print(f'Spot: {spot_price}')

    chains = ib.reqSecDefOptParams(spx.symbol, '', spx.secType, spx.conId)
    chain = next((c for c in chains if c.exchange == 'CBOE'), None)
    expiry = chain.expirations[0]
    
    all_strikes = sorted(chain.strikes)
    atm_idx = min(range(len(all_strikes)), key=lambda i: abs(all_strikes[i] - spot_price))
    strikes = all_strikes[max(0, atm_idx-5):atm_idx+6]
    #print(f'Expiry: {expiry}, Strikes: {strikes}')

    # Calls first, then puts for LTP/Delta
    call_contracts = [Option('SPX', expiry, s, 'C', 'CBOE') for s in strikes]
    put_contracts = [Option('SPX', expiry, s, 'P', 'CBOE') for s in strikes]
    all_contracts = call_contracts + put_contracts
    ib.qualifyContracts(*call_contracts, *put_contracts)

    # Use reqMktData for Greeks (snapshots miss modelGreeks often)
    call_tickers = [ib.reqMktData(c, '', False, False) for c in call_contracts]
    put_tickers = [ib.reqMktData(c, '', False, False) for c in put_contracts]
    ib.sleep(2)  # Wait for data/Greeks
    # Cancel by CONTRACT (not ticker)
    for c in all_contracts:
        ib.cancelMktData(c)

    data = []
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    for i, strike in enumerate(strikes):
        # Call
        call = call_tickers[i]
        call_ltp = call.last or 0
        call_delta = (call.modelGreeks.delta if call.modelGreeks else call.lastGreeks.delta if call.lastGreeks else 0) or 0
        # Put
        put = put_tickers[i]
        put_ltp = put.last or 0
        put_delta = (put.modelGreeks.delta if put.modelGreeks else put.lastGreeks.delta if put.lastGreeks else 0) or 0
        
        data.append({
            'Date': now.split(' ')[0],
            'Time': now.split(' ')[1],
            'Strike': strike,
            'CallLtp': round(call_ltp, 2),
            'PutLtp': round(put_ltp, 2),
            'CallDelta': round(call_delta, 4),
            'PutDelta': round(put_delta, 4),
            'Spot_Price': round(spot_price, 2)
        })
    
    df = pd.DataFrame(data)
    #print(df.to_string(index=False))
    df.to_csv('spx_optionchain.csv', index=False)
    #print('\nSaved spx_optionchain.csv')
except Exception as e:
    print(f'Error: {e}')
finally:
    ib.disconnect()
