import sqlite3
import pandas as pd
import os

# Paths to your DB files
base_path = r"C:\SQLite\ticks"
db_file_6 = os.path.join(base_path, "ticks_2026-02-06.db")
db_file_9 = os.path.join(base_path, "ticks_2026-02-09.db")

symbol = "NSE:NIFTY50-INDEX"

def fetch_candles(db_file, table_name, symbol):
    """Fetch candles from a given DB file and table."""
    if not os.path.exists(db_file):
        print(f"[ERROR] DB file not found: {db_file}")
        return pd.DataFrame()

    conn = sqlite3.connect(db_file)
    try:
        df = pd.read_sql_query(
            f"SELECT * FROM {table_name} WHERE symbol=? ORDER BY trade_date, ist_slot",
            conn, params=[symbol]
        )
        print(f"[INFO] {db_file} {table_name}: {len(df)} rows fetched")
    except Exception as e:
        print(f"[ERROR] Failed to fetch from {db_file} {table_name}: {e}")
        df = pd.DataFrame()
    finally:
        conn.close()
    return df

# --- Fetch from Feb 6 ---
df_3m_6 = fetch_candles(db_file_6, "candles_3m_ist", symbol)
df_15m_6 = fetch_candles(db_file_6, "candles_15m_ist", symbol)

# --- Fetch from Feb 9 ---
df_3m_9 = fetch_candles(db_file_9, "candles_3m_ist", symbol)
df_15m_9 = fetch_candles(db_file_9, "candles_15m_ist", symbol)

# --- Merge ---
df_3m_merged = pd.concat([df_3m_6, df_3m_9], ignore_index=True, sort=False)
df_15m_merged = pd.concat([df_15m_6, df_15m_9], ignore_index=True, sort=False)

print(f"[MERGE] 3m continuity merged: Feb 6 ({len(df_3m_6)}) + Feb 9 ({len(df_3m_9)}) = {len(df_3m_merged)}")
print(f"[MERGE] 15m continuity merged: Feb 6 ({len(df_15m_6)}) + Feb 9 ({len(df_15m_9)}) = {len(df_15m_merged)}")

# --- Optional: inspect merged head ---
print(df_3m_merged.head())
print(df_15m_merged.head())