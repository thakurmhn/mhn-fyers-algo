import sqlite3
import pandas as pd
import logging
import os, glob
from datetime import datetime, timedelta
import pendulum as dt


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
            trade_date TEXT NOT NULL,
            ist_slot TEXT NOT NULL,
            symbol TEXT NOT NULL,
            open REAL, high REAL, low REAL, close REAL, volume REAL,
            PRIMARY KEY (trade_date, ist_slot, symbol)
        )""")

        # 15-minute candles
        self.cursor.execute("""
        CREATE TABLE IF NOT EXISTS candles_15m_ist (
            trade_date TEXT NOT NULL,
            ist_slot TEXT NOT NULL,
            symbol TEXT NOT NULL,
            open REAL, high REAL, low REAL, close REAL, volume REAL,
            PRIMARY KEY (trade_date, ist_slot, symbol)
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
    def insert_3m_candle(self, trade_date, ist_slot,
                         open_price, high_price, low_price, close_price, volume, symbol):
        """Insert a 3m candle into candles_3m_ist table with symbol included."""
        try:
            self.cursor.execute("""
                INSERT OR REPLACE INTO candles_3m_ist
                (trade_date, ist_slot, symbol, open, high, low, close, volume)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                str(trade_date), str(ist_slot), str(symbol),
                float(open_price) if open_price is not None else None,
                float(high_price) if high_price is not None else None,
                float(low_price) if low_price is not None else None,
                float(close_price) if close_price is not None else None,
                float(volume) if volume is not None else None
            ))
            self.conn.commit()
            logging.debug(f"[DB] Inserted 3m candle {trade_date} {ist_slot} {symbol}")
        except Exception as e:
            logging.error(f"[DB ERROR] Failed to insert 3m candle for {symbol}: {e}")

    def insert_15m_candle(self, trade_date, ist_slot,
                          open_, high, low, close, volume, symbol):
        """Insert a 15m candle into candles_15m_ist table with symbol included."""
        try:
            self.cursor.execute("""
                INSERT OR REPLACE INTO candles_15m_ist
                (trade_date, ist_slot, symbol, open, high, low, close, volume)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                str(trade_date), str(ist_slot), str(symbol),
                float(open_) if open_ is not None else None,
                float(high) if high is not None else None,
                float(low) if low is not None else None,
                float(close) if close is not None else None,
                float(volume) if volume is not None else None
            ))
            self.conn.commit()
            logging.debug(f"[DB] Inserted 15m candle {trade_date} {ist_slot} {symbol}")
        except Exception as e:
            logging.error(f"[DB ERROR] Failed to insert 15m candle for {symbol}: {e}")

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

    def fetch_candles(self, resolution="3m", start_time=None, end_time=None,
                  use_yesterday=False, symbol=None):
        """Fetch candles for the current trading day or last available trading day.
        Resolution can be '3m' or '15m'."""
        table_map = {
            "3m": "candles_3m_ist",
            "15m": "candles_15m_ist"
        }
        table = table_map.get(resolution)
        if not table:
            logging.error(f"[DB ERROR] Unsupported resolution: {resolution}")
            return pd.DataFrame()

        time_zone = "Asia/Kolkata"
        today = dt.now(time_zone).date()

        # --- Pick trade_date ---
        if use_yesterday:
            # step back until a DB file has data
            offset = 1
            while offset <= 5:  # safety cap: look back max 5 days
                candidate = today - timedelta(days=offset)
                trade_date = candidate.isoformat()
                query = f"SELECT * FROM {table} WHERE trade_date = ?"
                params = [trade_date]
                if symbol:
                    query += " AND symbol = ?"
                    params.append(symbol)
                try:
                    df = pd.read_sql_query(query, self.conn, params=params)
                    if not df.empty:
                        break
                except Exception as e:
                    logging.error(f"[DB ERROR] Failed to fetch {resolution} candles: {e}")
                offset += 1
            else:
                # fallback: no data found
                return pd.DataFrame(columns=["trade_date","ist_slot","open","high","low","close","volume","symbol"])
        else:
            trade_date = today.isoformat()
            query = f"SELECT * FROM {table} WHERE trade_date = ?"
            params = [trade_date]
            if symbol:
                query += " AND symbol = ?"
                params.append(symbol)
            df = pd.read_sql_query(query, self.conn, params=params)

        # --- Apply time filters ---
        if start_time:
            query += " AND ist_slot >= ?"
            params.append(start_time)
        if end_time:
            query += " AND ist_slot <= ?"
            params.append(end_time)

        # --- Final cleanup ---
        if df.empty:
            return pd.DataFrame(columns=["trade_date","ist_slot","open","high","low","close","volume","symbol"])
        for col in ["open","high","low","close","volume"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        return df


    @staticmethod
    def load_sessions(base_path=r"C:\SQLite\ticks"):
        """
        Load all tick sessions from daily DB files into a single DataFrame.
        Useful for replay, backtesting, and audit.
        """
        base_path = os.path.abspath(base_path)
        db_files = sorted(glob.glob(os.path.join(base_path, "ticks_*.db")))
        dfs = []

        for db_file in db_files:
            try:
                conn = sqlite3.connect(db_file)
                df = pd.read_sql_query("SELECT * FROM ticks", conn)
                conn.close()

                if df.empty:
                    continue

                # Normalize numeric columns
                if "last_price" in df.columns:
                    df["last_price"] = pd.to_numeric(df["last_price"], errors="coerce")
                if "volume" in df.columns:
                    df["volume"] = pd.to_numeric(df["volume"], errors="coerce")

                dfs.append(df)
                logging.info(f"[LOAD] Loaded {len(df)} ticks from {db_file}")

            except Exception as e:
                logging.error(f"[DB ERROR] Failed to load session {db_file}: {e}")

        if dfs:
            return pd.concat(dfs, ignore_index=True)
        else:
            logging.warning("[LOAD] No tick data found in sessions")
            return pd.DataFrame(columns=["timestamp","trade_date","symbol","bid","ask","last_price","volume"])
 
        
tick_db = TickDatabase()