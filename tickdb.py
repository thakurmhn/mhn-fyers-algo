# ===== tickdb.py =====

import sqlite3
from datetime import datetime
import pandas as pd
import os
import glob
import logging

DB_PATH = "ticks.db"


class TickDatabase:
    def __init__(self, base_path=r"C:\SQLite\ticks"):
        # Normalize and ensure base folder exists
        base_path = os.path.abspath(base_path)
        os.makedirs(base_path, exist_ok=True)

        # Use trading date (YYYY-MM-DD) as filename
        trade_date = datetime.now().strftime("%Y-%m-%d")
        db_file = os.path.join(base_path, f"ticks_{trade_date}.db")
        db_file = os.path.normpath(db_file)

        # Connect to SQLite DB file
        self.conn = sqlite3.connect(db_file, check_same_thread=False)
        self.cursor = self.conn.cursor()
        self._create_tables()

        logging.info(f"[DB PATH] Using database at {db_file}")

    def _create_tables(self):
        # Raw tick storage
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

        # Merged candle storage (legacy)
        self.cursor.execute("""
        CREATE TABLE IF NOT EXISTS merged_candles (
            ist_candle TEXT PRIMARY KEY,
            open REAL,
            high REAL,
            low REAL,
            close REAL
        )
        """)

        # ✅ New 15m candle storage
        self.cursor.execute("""
        CREATE TABLE IF NOT EXISTS candles_15m_ist (
            trade_date TEXT,
            ist_slot TEXT,
            open REAL,
            high REAL,
            low REAL,
            close REAL,
            volume REAL,
            PRIMARY KEY (trade_date, ist_slot)
        )
        """)
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
            logging.error(f"[ERROR] Failed to insert tick: {e}")

    def insert_15m_candle(self, trade_date, ist_slot, open_, high, low, close, volume):
        try:
            self.cursor.execute("""
                INSERT OR REPLACE INTO candles_15m_ist (trade_date, ist_slot, open, high, low, close, volume)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (trade_date, ist_slot, open_, high, low, close, volume))
            self.conn.commit()
        except Exception as e:
            logging.error(f"[ERROR] Failed to insert 15m candle: {e}")

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
        ohlcv = ohlcv.dropna()

        # ✅ Persist into candles_15m_ist
        for ts, row in ohlcv.iterrows():
            trade_date = ts.date().isoformat()
            ist_slot = ts.strftime("%H:%M")
            self.insert_15m_candle(trade_date, ist_slot,
                                   row['open'], row['high'], row['low'], row['close'], row['volume'])

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

    @staticmethod
    def load_sessions(base_path=r"C:\SQLite\ticks"):
        """Load ticks from all session DB files in base_path into one DataFrame."""
        base_path = os.path.abspath(base_path)
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