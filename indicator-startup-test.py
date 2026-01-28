import sqlite3
import pandas as pd
from datetime import datetime
from oscillator_filters import williams_r, cci_indicator

DB_PATH = r"C:\SQLite\ticks\ticks_{}.db".format(datetime.now().strftime("%Y-%m-%d"))

def fetch_last_20_candles(symbol="NSE:NIFTY50-INDEX"):
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query("""
    SELECT ist_slot AS timestamp, open, high, low, close
    FROM candles_15m_ist
    WHERE trade_date = date('now')
    ORDER BY ist_slot DESC
    LIMIT 20
""", conn)
    conn.close()
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    return df.sort_values('timestamp')

# --- Run validation ---
candles = fetch_last_20_candles()

print("Last 20 historical 15m candles:")
print(candles)

# Calculate indicators for each candle
wr_values = []
cci_values = []
for i in range(len(candles)):
    sub_df = candles.iloc[:i+1]  # rolling window up to current candle
    wr = williams_r(sub_df, period=14)
    cci = cci_indicator(sub_df, period=20)
    wr_values.append(wr)
    cci_values.append(cci)

candles['W%R'] = wr_values
candles['CCI'] = cci_values

print("\nCandles with Oscillator values:")
print(candles[['timestamp','open','high','low','close','W%R','CCI']])