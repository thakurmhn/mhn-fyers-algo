import os
import sqlite3
import pandas as pd
import logging

BASE_PATH = r"C:\SQLite\ticks"
SYMBOL = "NSE:NIFTY50-INDEX"

def rebuild_15m_candles_from_ticks(db_file, symbol):
    try:
        conn = sqlite3.connect(db_file)

        # Check if table already exists
        tables = pd.read_sql_query("SELECT name FROM sqlite_master WHERE type='table'", conn)
        if "candles_15m_ist" in tables["name"].values:
            logging.info(f"[SKIP] {db_file} already has candles_15m_ist")
            conn.close()
            return

        # Load ticks
        df_ticks = pd.read_sql_query(
            "SELECT * FROM ticks WHERE symbol=? ORDER BY ist_time",
            conn, params=[symbol]
        )
        if df_ticks.empty:
            logging.warning(f"[REBUILD] No ticks found in {db_file} for {symbol}")
            conn.close()
            return

        # Resample into 15m OHLCV
        df_ticks["ist_time"] = pd.to_datetime(df_ticks["ist_time"])
        df_ticks.set_index("ist_time", inplace=True)

        df_15m = df_ticks.resample("15T").agg({
            "ltp": "last",
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum"
        }).dropna()

        df_15m.reset_index(inplace=True)
        df_15m["trade_date"] = df_15m["ist_time"].dt.date
        df_15m["ist_slot"] = df_15m["ist_time"].dt.strftime("%H:%M:%S")
        df_15m["symbol"] = symbol

        # Write back to DB
        df_15m.to_sql("candles_15m_ist", conn, if_exists="replace", index=False)
        conn.close()

        logging.info(f"[REBUILD] Built {len(df_15m)} 15m candles for {symbol} in {db_file}")

    except Exception as e:
        logging.error(f"[REBUILD ERROR] Failed in {db_file}: {e}")


def batch_backfill(base_path=BASE_PATH, symbol=SYMBOL):
    for fname in os.listdir(base_path):
        if fname.startswith("ticks_") and fname.endswith(".db"):
            db_file = os.path.join(base_path, fname)
            rebuild_15m_candles_from_ticks(db_file, symbol)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    batch_backfill()