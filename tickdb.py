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

        # Merged candle storage
        self.cursor.execute("""
        CREATE TABLE IF NOT EXISTS merged_candles (
            ist_candle TEXT PRIMARY KEY,
            open REAL,
            high REAL,
            low REAL,
            close REAL
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


# ===== Standalone Helpers =====

def load_last_historical_candle(trade_date=None):
    if trade_date is None:
        trade_date = datetime.now().strftime("%Y-%m-%d")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("""
        SELECT ist_candle, open, high, low, close
        FROM candles_15m_ist
        WHERE date(ist_candle) = ?
        ORDER BY ist_candle DESC
        LIMIT 1;
    """, (trade_date,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def merge_with_live_ticks(live_ticks, trade_date=None):
    if trade_date is None:
        trade_date = datetime.now().strftime("%Y-%m-%d")
    last_hist = load_last_historical_candle(trade_date)
    if not last_hist:
        raise RuntimeError("No historical candle found for today")
    prices = [tick['last_price'] for tick in live_ticks]
    if not prices:
        raise RuntimeError("No live ticks provided at 09:15 IST")
    return {
        "ist_candle": f"{trade_date} 09:15",
        "open": prices[0],
        "high": max(prices),
        "low": min(prices),
        "close": prices[-1]
    }


def write_candle_to_db(candle):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS merged_candles (
            ist_candle TEXT PRIMARY KEY,
            open REAL,
            high REAL,
            low REAL,
            close REAL
        )
    """)
    cur.execute("""
        INSERT OR REPLACE INTO merged_candles (ist_candle, open, high, low, close)
        VALUES (?, ?, ?, ?, ?)
    """, (candle["ist_candle"], candle["open"], candle["high"], candle["low"], candle["close"]))
    conn.commit()
    conn.close()