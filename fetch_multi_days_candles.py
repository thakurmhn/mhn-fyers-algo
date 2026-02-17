import os
import sqlite3
import pandas as pd
from orchestration import build_indicator_dataframe  # <-- import your enrichment function

BASE_PATH = r"C:\SQLite\ticks"
SYMBOL = "NSE:NIFTY50-INDEX"
INTERVAL = "15m"
N_FILES = 3   # how many most recent DB files to fetch

def fetch_last_n_files(symbol, interval="15m", base_path=BASE_PATH, n_files=3):
    files = [f for f in os.listdir(base_path) if f.startswith("ticks_") and f.endswith(".db")]
    if not files:
        print("[ERROR] No DB files found")
        return pd.DataFrame()

    files_sorted = sorted(files, key=lambda x: x.replace("ticks_", "").replace(".db", ""))
    recent_files = files_sorted[-n_files:]

    collected = []
    for fname in recent_files:
        db_file = os.path.join(base_path, fname)
        try:
            conn = sqlite3.connect(db_file)
            df = pd.read_sql_query(
                f"SELECT * FROM candles_{interval}_ist WHERE symbol=? ORDER BY trade_date, ist_slot",
                conn,
                params=[symbol]
            )
            conn.close()

            if df is None or df.empty:
                print(f"[EMPTY] {fname} has no {interval} candles for {symbol}")
                continue

            df["time"] = df["trade_date"] + " " + df["ist_slot"]
            collected.append(df)
            print(f"[FETCHED] {symbol} {interval}: {len(df)} candles from {fname}")

        except Exception as e:
            print(f"[ERROR] Failed to read {fname}: {e}")
            continue

    if collected:
        df_all = pd.concat(collected, ignore_index=True)
        print(f"[FINAL] {symbol} {interval}: total {len(df_all)} candles from {len(collected)} files")

        # ✅ Enrich with indicators
        df_all = build_indicator_dataframe(symbol, df_all, interval=interval)

        # Show last few enriched rows
        print(df_all.tail(5))
        return df_all

    print("[WARNING] No candles fetched")
    return pd.DataFrame()

if __name__ == "__main__":
    df_all = fetch_last_n_files(SYMBOL, INTERVAL, BASE_PATH, N_FILES)