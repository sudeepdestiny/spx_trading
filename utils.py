import pandas as pd

def find_strike_value(df_chain, strike, strike_type, retry_count=0):
    if retry_count > 6:
        return pd.DataFrame()
    df_temp = df_chain[df_chain['StrikePrice'] == strike]
    if len(df_temp) == 0 or (df_temp['CallLTP'].iloc[0] == 0 and df_temp['PutLTP'].iloc[0] == 0):
        step = -5 if strike_type == 'PE' else 5
        return find_strike_value(df_chain, strike + step, strike_type, retry_count + 1)
    return df_temp

def compute_positional_delta(df_chain, option_book):
    pos_delta = 0
    for pos in option_book:
        if pos['close'] != 0:
            continue
        df_temp = find_strike_value(df_chain, pos['strike'], pos['type'])
        if len(df_temp) == 0:
            continue
        delta_val = df_temp[f"{'Call' if pos['type'] == 'CE' else 'Put'}Delta"].iloc[0]
        if pos['bor_s'] == 'S':
            delta_val *= -1 if pos['type'] == 'CE' else 1
        else:
            delta_val *= 1 if pos['type'] == 'PE' else -1
        pos_delta += delta_val
    return abs(pos_delta)

def get_current_premium(df_chain, option_book, lot_size):
    premium = 0
    for pos in option_book:
        if pos['close'] != 0:
            continue
        df_temp = find_strike_value(df_chain, pos['strike'], pos['type'])
        if len(df_temp) == 0:
            continue
        ltp = df_temp[f"{'Call' if pos['type'] == 'CE' else 'Put'}LTP"].iloc[0]
        premium += (pos['entry'] - ltp) if pos['bor_s'] == 'S' else (ltp - pos['entry'])
    return premium * lot_size
