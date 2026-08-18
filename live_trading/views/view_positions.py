import streamlit as st
import sys
from pathlib import Path
import pandas as pd

# Add the project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from IBKRConnectionManager import get_ibkr_connection
from paper_trading import PaperTrader

# Initialize connection manager and trader
conn_mgr = get_ibkr_connection(is_paper=False)
trader = PaperTrader(conn_mgr=conn_mgr, is_paper=False)

# Streamlit app
st.title("SPX Paper Trader - Open Positions Viewer")

# Connect to IBKR
if not conn_mgr.is_connected:
    st.warning("Connecting to IBKR...")
    if not conn_mgr.connect(timeout=10, max_retries=5):
        st.error("Failed to connect to IBKR. Please check your connection.")
        st.stop()

st.success("Connected to IBKR!")

# Add a refresh button
if st.button("Refresh Positions"):
    st.rerun()  # Reload the app to fetch the latest data

# Fetch and display live positions
st.header("Live Positions")
if trader.has_open_positions():
    grid_data = []
    
    # Gather open strikes and expirys to fetch live prices
    open_strikes = list(set([leg.get("strike") for cycle in trader.live_positions.values() for leg in cycle.get("positions", []) if leg.get("status") == "OPEN"]))
    open_expirys = list(set([leg.get("expiry") for cycle in trader.live_positions.values() for leg in cycle.get("positions", []) if leg.get("status") == "OPEN"]))
    
    option_chain = None
    if open_strikes and open_expirys:
        with st.spinner("Fetching live market prices..."):
            option_chain = trader.get_live_option_chain(required_strikes=open_strikes, expiry=open_expirys[0])

    for cycle_id, cycle_data in trader.live_positions.items():
        if cycle_data.get("status") == "OPEN":
            entry_time = cycle_data.get("entry_time")
            if hasattr(entry_time, "strftime"):
                entry_time = entry_time.strftime("%Y-%m-%d %H:%M:%S")
            
            for leg in cycle_data.get("positions", []):
                if leg.get("status") == "OPEN":
                    current_price = None
                    if option_chain is not None and not option_chain.empty:
                        row = option_chain[option_chain['strike'] == leg.get("strike")]
                        if not row.empty:
                            prefix = "call" if leg.get("type") == "CE" else "put"
                            current_price = row.iloc[0].get(f"{prefix}_mid")

                    mtm = None
                    if current_price is not None and current_price > 0:
                        multiplier = 100
                        qty = float(leg.get("quantity", 0))
                        entry_price = float(leg.get("price", 0))
                        if leg.get("action") == "BUY":
                            mtm = (current_price - entry_price) * qty * multiplier
                        else:
                            mtm = (entry_price - current_price) * qty * multiplier

                    grid_data.append({
                        "Cycle ID": cycle_id,
                        "Entry Time": entry_time,
                        "Instrument": f"SPX {leg.get('expiry')} {leg.get('strike')} {leg.get('type')}",
                        "Position": leg.get("action"),
                        "Qty": leg.get("quantity"),
                        "Avg Price": leg.get("price"),
                        "LTP": current_price,
                        "MTM": mtm,
                        "Realized PnL": leg.get("pnl", 0.0)
                    })
    
    if grid_data:
        df = pd.DataFrame(grid_data)
        
        # Calculate Total MTM
        total_mtm = df["MTM"].sum() if "MTM" in df and not df["MTM"].isna().all() else 0.0
        
        # Display MTM in metrics
        col1, col2, col3 = st.columns(3)
        col1.metric("Total MTM", f"${total_mtm:,.2f}")
        col2.metric("Realized PnL", f"${trader.get_position_summary().get('total_pnl', 0.0):,.2f}")
        
        st.dataframe(df.style.format({
            "Avg Price": "{:.2f}",
            "LTP": "{:.2f}",
            "MTM": "{:.2f}",
            "Realized PnL": "{:.2f}"
        }), use_container_width=True)
    else:
        st.info("No open legs found.")
else:
    st.info("No open positions currently.")

# Fetch and display position summary
st.header("Position Summary")
summary = trader.get_position_summary()

col1, col2, col3, col4 = st.columns(4)
col1.metric("Total Cycles", summary.get("total_cycles", 0))
col2.metric("Open Cycles", summary.get("open_cycles", 0))
col3.metric("Closed Cycles", summary.get("closed_cycles", 0))
col4.metric("Realized PnL", f"${summary.get('total_pnl', 0.0):,.2f}")

st.subheader("Raw Summary Data")
st.json(summary)

# Disconnect on app exit
# if st.button("Disconnect"):
#     conn_mgr.disconnect()
#     st.success("Disconnected from IBKR.")