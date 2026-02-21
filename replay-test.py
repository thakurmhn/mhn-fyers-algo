import pandas as pd
import time

from orchestration import build_indicator_dataframe
import sqlite3

def load_ticks_from_db(db_path, symbol="NSE:NIFTY50-INDEX"):
    conn = sqlite3.connect(db_path)
    query = f"SELECT timestamp, last_price, volume FROM ticks WHERE symbol='{symbol}'"
    df = pd.read_sql(query, conn)
    conn.close()
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    return df

def build_candles_from_ticks(df, interval="3min"):
    df.set_index("timestamp", inplace=True)
    ohlc = df["last_price"].resample(interval).ohlc()
    ohlc["volume"] = df["volume"].resample(interval).sum()
    ohlc.reset_index(inplace=True)
    ohlc.rename(columns={"timestamp":"date"}, inplace=True)
    return ohlc

def replay_supertrend(db_path, symbol="NSE:NIFTY50-INDEX", interval="3m", delay=0.05):
    ticks = load_ticks_from_db(db_path, symbol)
    candles = build_candles_from_ticks(ticks, interval="3min")

    for i in range(20, len(candles)):  # skip ATR warm-up
        sub_df = candles.iloc[:i+1]
        enriched = build_indicator_dataframe(symbol, sub_df, interval=interval)
        last = enriched.iloc[-1]
        print(
            f"{last.get('date','NA')} | close={last.get('close','NA')} "
            f"line={last.get('supertrend_line','NA')} "
            f"bias={last.get('supertrend_bias','NA')} slope={last.get('supertrend_slope','NA')}"
        )
        time.sleep(delay)

if __name__ == "__main__":
    replay_supertrend("C:/SQLite/ticks/ticks_2026-02-18.db", symbol="NSE:NIFTY50-INDEX", interval="3m")


