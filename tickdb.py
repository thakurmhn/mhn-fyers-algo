import sqlite3
from datetime import datetime
import pandas as pd
import os, glob, logging

class TickDatabase:
    def __init__(self, base_path=r"C:\SQLite\ticks"):
        base_path = os.path.abspath(base_path)
        os.makedirs(base_path, exist_ok=True)
        trade_date = datetime.now().strftime("%Y-%m-%d")
        db_file = os.path.join(base_path, f"ticks_{trade_date}.db")
        self.conn = sqlite3.connect(db_file, check_same_thread=False)
        self.cursor = self.conn.cursor()
        self._create_tables()
        logging.info(f"[DB PATH] Using database at {db_file}")

    def _create_tables(self):
        # Raw ticks
        self.cursor.execute("""
        CREATE TABLE IF NOT EXISTS ticks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME NOT NULL,
            trade_date DATE NOT NULL,
            symbol TEXT NOT NULL,
            bid REAL, ask REAL, last_price REAL, volume REAL
        )""")
        self.cursor.execute("CREATE INDEX IF NOT EXISTS idx_symbol_time ON ticks(symbol, timestamp)")

        # 3-minute candles
        self.cursor.execute("""
        CREATE TABLE IF NOT EXISTS candles_3m_ist (
            trade_date TEXT, ist_slot TEXT,
            open REAL, high REAL, low REAL, close REAL, volume REAL,
            PRIMARY KEY (trade_date, ist_slot)
        )""")

        # 15-minute candles
        self.cursor.execute("""
        CREATE TABLE IF NOT EXISTS candles_15m_ist (
            trade_date TEXT, ist_slot TEXT,
            open REAL, high REAL, low REAL, close REAL, volume REAL,
            PRIMARY KEY (trade_date, ist_slot)
        )""")

        self.conn.commit()

    # ===== Tick persistence =====
    def insert_tick(self, symbol, bid, ask, last_price, volume):
        ts = datetime.utcnow().isoformat()
        trade_date = datetime.now().strftime("%Y-%m-%d")
        try:
            self.cursor.execute("""
                INSERT INTO ticks (timestamp, trade_date, symbol, bid, ask, last_price, volume)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                str(ts), str(trade_date), str(symbol),
                float(bid) if bid is not None else None,
                float(ask) if ask is not None else None,
                float(last_price) if last_price is not None else None,
                float(volume) if volume is not None else None
            ))
            self.conn.commit()
        except Exception as e:
            logging.error(f"[DB ERROR] Failed to insert tick: {e}")

    # ===== Candle persistence =====
    def insert_3m_candle(self, trade_date, ist_slot, open_, high, low, close, volume):
        try:
            self.cursor.execute("""
                INSERT OR REPLACE INTO candles_3m_ist (trade_date, ist_slot, open, high, low, close, volume)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                str(trade_date), str(ist_slot),
                float(open_) if open_ is not None else None,
                float(high) if high is not None else None,
                float(low) if low is not None else None,
                float(close) if close is not None else None,
                float(volume) if volume is not None else None
            ))
            self.conn.commit()
        except Exception as e:
            logging.error(f"[DB ERROR] Failed to insert 3m candle: {e}")

    def insert_15m_candle(self, trade_date, ist_slot, open_, high, low, close, volume):
        try:
            self.cursor.execute("""
                INSERT OR REPLACE INTO candles_15m_ist (trade_date, ist_slot, open, high, low, close, volume)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                str(trade_date), str(ist_slot),
                float(open_) if open_ is not None else None,
                float(high) if high is not None else None,
                float(low) if low is not None else None,
                float(close) if close is not None else None,
                float(volume) if volume is not None else None
            ))
            self.conn.commit()
        except Exception as e:
            logging.error(f"[DB ERROR] Failed to insert 15m candle: {e}")

    # ===== Tick retrieval =====
    def fetch_ticks(self, symbol, start_time=None, end_time=None):
        query = "SELECT timestamp, last_price, volume FROM ticks WHERE symbol=?"
        params = [symbol]
        if start_time:
            query += " AND timestamp >= ?"
            params.append(start_time)
        if end_time:
            query += " AND timestamp <= ?"
            params.append(end_time)

        try:
            df = pd.read_sql_query(query, self.conn, params=params)
            if df.empty:
                return pd.DataFrame(columns=["timestamp", "last_price", "volume"])
            df['last_price'] = pd.to_numeric(df['last_price'], errors='coerce')
            df['volume'] = pd.to_numeric(df['volume'], errors='coerce')
            return df
        except Exception as e:
            logging.error(f"[DB ERROR] Failed to fetch ticks: {e}")
            return pd.DataFrame(columns=["timestamp", "last_price", "volume"])

    def replay_ticks(self, symbol):
        try:
            df = pd.read_sql_query(
                "SELECT * FROM ticks WHERE symbol=? ORDER BY timestamp ASC",
                self.conn, params=[symbol]
            )
            df['last_price'] = pd.to_numeric(df['last_price'], errors='coerce')
            df['volume'] = pd.to_numeric(df['volume'], errors='coerce')
            return df
        except Exception as e:
            logging.error(f"[DB ERROR] Failed to replay ticks: {e}")
            return pd.DataFrame()

    def fetch_candles(self, resolution="3m", start_time=None, end_time=None):
        table_map = {
            "3m": "candles_3m_ist",
            "15m": "candles_15m_ist"
        }
        table = table_map.get(resolution)
        if not table:
            logging.error(f"[DB ERROR] Unsupported resolution: {resolution}")
            return pd.DataFrame()

        query = f"SELECT * FROM {table}"
        params = []
        if start_time:
            query += " WHERE ist_slot >= ?"
            params.append(start_time)
        if end_time:
            if "WHERE" in query:
                query += " AND ist_slot <= ?"
            else:
                query += " WHERE ist_slot <= ?"
            params.append(end_time)

        try:
            df = pd.read_sql_query(query, self.conn, params=params)
            if df.empty:
                return pd.DataFrame(columns=["trade_date","ist_slot","open","high","low","close","volume"])
            for col in ["open","high","low","close","volume"]:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors="coerce")
            return df
        except Exception as e:
            logging.error(f"[DB ERROR] Failed to fetch {resolution} candles: {e}")
            return pd.DataFrame(columns=["trade_date","ist_slot","open","high","low","close","volume"])

    @staticmethod
    def load_sessions(base_path=r"C:\SQLite\ticks"):
        base_path = os.path.abspath(base_path)
        db_files = sorted(glob.glob(os.path.join(base_path, "ticks_*.db")))
        dfs = []
        for db_file in db_files:
            try:
                conn = sqlite3.connect(db_file)
                df = pd.read_sql_query("SELECT * FROM ticks", conn)
                df['last_price'] = pd.to_numeric(df['last_price'], errors='coerce')
                df['volume'] = pd.to_numeric(df['volume'], errors='coerce')
                dfs.append(df)
                conn.close()
            except Exception as e:
                logging.error(f"[DB ERROR] Failed to load session {db_file}: {e}")
        if dfs:
            return pd.concat(dfs, ignore_index=True)
        return pd.DataFrame()


# ✅ Global instance for import
tick_db = TickDatabase()