# ===== tickdb.py =====

import sqlite3
from datetime import datetime
import pandas as pd
import os
import glob

class TickDatabase:
    def __init__(self, base_path=r"C:\SQLite\ticks"):
        # Ensure base folder exists
        os.makedirs(base_path, exist_ok=True)

        # Use trading date (YYYY-MM-DD) as filename
        trade_date = datetime.now().strftime("%Y-%m-%d")
        db_file = os.path.join(base_path, f"ticks_{trade_date}.db")

        # Connect to SQLite DB file
        self.conn = sqlite3.connect(db_file, check_same_thread=False)
        self.cursor = self.conn.cursor()
        self._create_table()

    def _create_table(self):
        self.cursor.execute("""
        CREATE TABLE IF NOT EXISTS ticks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME NOT NULL,
            trade_date DATE NOT NULL,
            symbol TEXT NOT NULL,
            bid REAL,
            ask REAL,
            last_price REAL,
            volume REAL
        )
        """)
        self.cursor.execute("CREATE INDEX IF NOT EXISTS idx_symbol_time ON ticks(symbol, timestamp)")
        self.conn.commit()

    def insert_tick(self, symbol, bid, ask, last_price, volume):
        ts = datetime.utcnow().isoformat()
        trade_date = datetime.now().strftime("%Y-%m-%d")
        try:
            self.cursor.execute("""
                INSERT INTO ticks (timestamp, trade_date, symbol, bid, ask, last_price, volume)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (ts, trade_date, symbol, bid, ask, last_price, volume))
            self.conn.commit()
        except Exception as e:
            print(f"[ERROR] Failed to insert tick: {e}")

    def fetch_ticks(self, symbol, start_time=None, end_time=None):
        query = "SELECT timestamp, last_price, volume FROM ticks WHERE symbol=?"
        params = [symbol]
        if start_time:
            query += " AND timestamp >= ?"
            params.append(start_time)
        if end_time:
            query += " AND timestamp <= ?"
            params.append(end_time)
        return pd.read_sql_query(query, self.conn, params=params)

    def generate_candles(self, symbol, interval="15min"):
        df = self.fetch_ticks(symbol)
        if df.empty:
            return pd.DataFrame()
        df['timestamp'] = pd.to_datetime(df['timestamp'])
        df.set_index('timestamp', inplace=True)
        ohlcv = df['last_price'].resample(interval).ohlc()
        ohlcv['volume'] = df['volume'].resample(interval).sum()
        return ohlcv.reset_index()

    def replay_ticks(self, symbol):
        return pd.read_sql_query(
            "SELECT * FROM ticks WHERE symbol=? ORDER BY timestamp ASC",
            self.conn, params=[symbol]
        )

    def close(self):
        try:
            self.conn.close()
        except:
            pass

    # ===== NEW: Multi-session loader =====
    @staticmethod
    def load_sessions(base_path=r"C:\SQLite\ticks"):
        """
        Load ticks from all session DB files in base_path into one DataFrame.
        """
        db_files = sorted(glob.glob(os.path.join(base_path, "ticks_*.db")))
        dfs = []
        for db_file in db_files:
            conn = sqlite3.connect(db_file)
            df = pd.read_sql_query("SELECT * FROM ticks", conn)
            dfs.append(df)
            conn.close()
        if dfs:
            return pd.concat(dfs, ignore_index=True)
        return pd.DataFrame()