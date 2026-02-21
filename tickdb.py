import sqlite3
import pandas as pd
import logging
import os, glob
from datetime import datetime, timedelta
import pendulum as dt
from datetime import datetime, timedelta
import pytz
time_zone = pytz.timezone("Asia/Kolkata")
today = datetime.now(time_zone).date()

import sqlite3
import pandas as pd
import logging
import os
from datetime import datetime, timedelta
import pytz

time_zone = pytz.timezone("Asia/Kolkata")

def fmt(val):
    """Format numeric values safely for logs."""
    return f"{val:.2f}" if val is not None and not pd.isna(val) else "NA"

class TickDatabase:
    def __init__(self, base_path=r"C:\SQLite\ticks", max_lookback=5):
        base_path = os.path.abspath(base_path)
        os.makedirs(base_path, exist_ok=True)

        # Always create/use today's DB file for persistence
        today_str = datetime.now().strftime("%Y-%m-%d")
        db_file = os.path.join(base_path, f"ticks_{today_str}.db")

        # Ensure file exists and tables are created
        self.conn = sqlite3.connect(db_file, check_same_thread=False)
        self.cursor = self.conn.cursor()
        self._create_tables()

        logging.info(f"[DB PATH] Using database at {db_file}")

        # Store base_path, db_path and max_lookback for continuity fetches
        self.base_path  = base_path
        self.db_path    = db_file       # ← exposes current DB path to run_strategy
        self.max_lookback = max_lookback

    def _get_latest_db_file(self, base_path, today_str, max_lookback):
        """
        Find the most recent DB file up to max_lookback days before today.
        Used for continuity lookbacks, not for persistence.
        """
        base_date = datetime.strptime(today_str, "%Y-%m-%d")
        for d in range(0, max_lookback + 1):
            check_date = (base_date - timedelta(days=d)).strftime("%Y-%m-%d")
            db_file = os.path.join(base_path, f"ticks_{check_date}.db")
            if os.path.exists(db_file):
                return db_file
        return None

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
            is_partial INTEGER DEFAULT 0,
            PRIMARY KEY (trade_date, ist_slot, symbol)
        )""")

        # 15-minute candles
        self.cursor.execute("""
        CREATE TABLE IF NOT EXISTS candles_15m_ist (
            trade_date TEXT NOT NULL,
            ist_slot TEXT NOT NULL,
            symbol TEXT NOT NULL,
            open REAL, high REAL, low REAL, close REAL, volume REAL,
            is_partial INTEGER DEFAULT 0,
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

    # # ===== Candle persistence =====
    # def insert_3m_candle(self, trade_date, ist_slot,
    #                      open_price, high_price, low_price, close_price, volume, symbol):
    #     """Insert a 3m candle into candles_3m_ist table with symbol included."""
    #     try:
    #         self.cursor.execute("""
    #             INSERT OR REPLACE INTO candles_3m_ist
    #             (trade_date, ist_slot, symbol, open, high, low, close, volume)
    #             VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    #         """, (
    #             str(trade_date), str(ist_slot), str(symbol),
    #             float(open_price) if open_price is not None else None,
    #             float(high_price) if high_price is not None else None,
    #             float(low_price) if low_price is not None else None,
    #             float(close_price) if close_price is not None else None,
    #             float(volume) if volume is not None else None
    #         ))
    #         self.conn.commit()
    #         logging.debug(f"[DB] Inserted 3m candle {trade_date} {ist_slot} {symbol}")
    #     except Exception as e:
    #         logging.error(f"[DB ERROR] Failed to insert 3m candle for {symbol}: {e}")

    # def insert_15m_candle(self, trade_date, ist_slot,
    #                       open_, high, low, close, volume, symbol):
    #     """Insert a 15m candle into candles_15m_ist table with symbol included."""
    #     try:
    #         self.cursor.execute("""
    #             INSERT OR REPLACE INTO candles_15m_ist
    #             (trade_date, ist_slot, symbol, open, high, low, close, volume)
    #             VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    #         """, (
    #             str(trade_date), str(ist_slot), str(symbol),
    #             float(open_) if open_ is not None else None,
    #             float(high) if high is not None else None,
    #             float(low) if low is not None else None,
    #             float(close) if close is not None else None,
    #             float(volume) if volume is not None else None
    #         ))
    #         self.conn.commit()
    #         logging.debug(f"[DB] Inserted 15m candle {trade_date} {ist_slot} {symbol}")
    #     except Exception as e:
    #         logging.error(f"[DB ERROR] Failed to insert 15m candle for {symbol}: {e}")

    def insert_3m_candle(self, trade_date, ist_slot,
                     open_price, high_price, low_price, close_price, volume, symbol,
                     is_partial=False):
        """Insert a 3m candle into candles_3m_ist table with symbol + partial flag."""
        try:
            self.cursor.execute("""
                INSERT OR REPLACE INTO candles_3m_ist
                (trade_date, ist_slot, symbol, open, high, low, close, volume, is_partial)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                str(trade_date), str(ist_slot), str(symbol),
                float(open_price) if open_price is not None else None,
                float(high_price) if high_price is not None else None,
                float(low_price) if low_price is not None else None,
                float(close_price) if close_price is not None else None,
                float(volume) if volume is not None else None,
                int(is_partial)
            ))
            self.conn.commit()
            logging.debug(f"[DB] Inserted 3m candle {trade_date} {ist_slot} {symbol} partial={is_partial}")
        except Exception as e:
            logging.error(f"[DB ERROR] Failed to insert 3m candle for {symbol}: {e}")

    def insert_15m_candle(self, trade_date, ist_slot,
                        open_, high, low, close, volume, symbol,
                        is_partial=False):
        """Insert a 15m candle into candles_15m_ist table with symbol + partial flag."""
        try:
            self.cursor.execute("""
                INSERT OR REPLACE INTO candles_15m_ist
                (trade_date, ist_slot, symbol, open, high, low, close, volume, is_partial)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                str(trade_date), str(ist_slot), str(symbol),
                float(open_) if open_ is not None else None,
                float(high) if high is not None else None,
                float(low) if low is not None else None,
                float(close) if close is not None else None,
                float(volume) if volume is not None else None,
                int(is_partial)
            ))
            self.conn.commit()
            logging.debug(f"[DB] Inserted 15m candle {trade_date} {ist_slot} {symbol} partial={is_partial}")
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

    def get_latest_tick(self, symbol):
        """Fetch the most recent tick for a symbol."""
        try:
            df = pd.read_sql_query(
                "SELECT * FROM ticks WHERE symbol=? ORDER BY timestamp DESC LIMIT 1",
                self.conn, params=[symbol]
            )
            if df.empty:
                return None
            # Normalize numeric fields
            for col in ["last_price", "volume", "bid", "ask"]:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors="coerce")
            return df.iloc[0].to_dict()
        except Exception as e:
            logging.error(f"[DB ERROR] Failed to fetch latest tick for {symbol}: {e}")
            return None


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

    # def build_candles_from_ticks(self, symbol, interval="3m"):
    #     # Fetch raw ticks for the symbol
    #     df = self.fetch_ticks(symbol)
    #     if df.empty:
    #         logging.warning(f"[CANDLES] No ticks available for {symbol}, skipping candle build")
    #         return

    #     # Convert timestamp to IST
    #     df['ts'] = pd.to_datetime(df['timestamp'], errors='coerce')
    #     df = df.dropna(subset=['ts'])  # drop rows with invalid timestamps
    #     df['ts'] = df['ts'].dt.tz_localize('UTC').dt.tz_convert('Asia/Kolkata')

    #     # Determine resample rule (use 'min' instead of deprecated 'T')
    #     if interval == "3m":
    #         rule = "3min"
    #     elif interval == "15m":
    #         rule = "15min"
    #     else:
    #         logging.error(f"[CANDLES] Unsupported interval={interval}, must be '3m' or '15m'")
    #         return

    #     # Resample ticks into OHLCV
    #     ohlc = (
    #         df.resample(rule, on='ts')
    #         .agg({
    #             'last_price': ['first', 'max', 'min', 'last'],
    #             'volume': 'sum'
    #         })
    #         .dropna()
    #     )

    #     # Flatten multi-index columns
    #     ohlc.columns = ['open', 'high', 'low', 'close', 'volume']
    #     ohlc.reset_index(inplace=True)

    #     # Persist each candle into DB
    #     for _, row in ohlc.iterrows():
    #         trade_date = row['ts'].date().isoformat()
    #         ist_slot = row['ts'].strftime('%H:%M:%S')

    #         if interval == "3m":
    #             self.insert_3m_candle(
    #                 trade_date, ist_slot,
    #                 row['open'], row['high'], row['low'], row['close'], row['volume'], symbol
    #             )
    #         else:  # 15m
    #             self.insert_15m_candle(
    #                 trade_date, ist_slot,
    #                 row['open'], row['high'], row['low'], row['close'], row['volume'], symbol
    #             )

    #     logging.info(f"[CANDLES] Built {interval} candles for {symbol} ({len(ohlc)} rows)")

    def build_candles_from_ticks(self, symbol, interval="3m"):
        df = self.fetch_ticks(symbol)
        if df.empty:
            logging.warning(f"[CANDLES] No ticks available for {symbol}, skipping candle build")
            return

        # Convert timestamp to IST
        df['ts'] = pd.to_datetime(df['timestamp'], errors='coerce')
        df = df.dropna(subset=['ts'])
        df['ts'] = df['ts'].dt.tz_localize('UTC').dt.tz_convert('Asia/Kolkata')

        # Resample rule
        rule = "3min" if interval == "3m" else "15min" if interval == "15m" else None
        if rule is None:
            logging.error(f"[CANDLES] Unsupported interval={interval}")
            return

        # Resample ticks into OHLCV (keep partial bins too)
        ohlc = (
            df.resample(rule, on='ts')
            .agg({
                'last_price': ['first', 'max', 'min', 'last'],
                'volume': 'sum'
            })
        )
        ohlc.columns = ['open', 'high', 'low', 'close', 'volume']
        ohlc.reset_index(inplace=True)

        # Fill forward for partial bars
        ohlc['open']   = ohlc['open'].ffill()
        ohlc['high']   = ohlc['high'].fillna(ohlc['open'])
        ohlc['low']    = ohlc['low'].fillna(ohlc['open'])
        ohlc['close']  = ohlc['close'].fillna(ohlc['open'])
        ohlc['volume'] = ohlc['volume'].fillna(0)

        # Add flag: mark last row as partial if slot not yet closed
        now = pd.Timestamp.now(tz="Asia/Kolkata")
        ohlc['is_partial'] = False
        if not ohlc.empty:
            last_slot = ohlc.iloc[-1]['ts']
            # If current time is still inside this slot window, mark as partial
            if (interval == "3m" and (now - last_slot).seconds < 180) or \
            (interval == "15m" and (now - last_slot).seconds < 900):
                ohlc.at[ohlc.index[-1], 'is_partial'] = True

        # Persist each candle
        for _, row in ohlc.iterrows():
            trade_date = row['ts'].date().isoformat()
            ist_slot   = row['ts'].strftime('%H:%M:%S')

            if interval == "3m":
                self.insert_3m_candle(trade_date, ist_slot,
                    row['open'], row['high'], row['low'], row['close'], row['volume'], symbol,
                    is_partial=row['is_partial'])
            else:
                self.insert_15m_candle(trade_date, ist_slot,
                    row['open'], row['high'], row['low'], row['close'], row['volume'], symbol,
                    is_partial=row['is_partial'])

        # ✅ Debug: show latest slot values
        latest = ohlc.iloc[-1]
        logging.info(
            f"[LIVE {interval.upper()}] Latest slot {latest['ts']} "
            f"O={fmt(latest['open'])} H={fmt(latest['high'])} "
            f"L={fmt(latest['low'])} C={fmt(latest['close'])} V={fmt(latest['volume'])} "
            f"Partial={latest['is_partial']}"
        )

        logging.info(f"[CANDLES] Built {interval} candles for {symbol} ({len(ohlc)} rows incl. partial)")


    def rebuild_candles_from_db(self, symbol, interval="3m"):
        """Rebuild candles from raw ticks in DB for a given symbol and interval."""
        df = self.fetch_ticks(symbol)
        if df.empty:
            logging.warning(f"[REBUILD] No ticks found for {symbol}")
            return

        # Convert timestamp to IST safely
        df['ts'] = pd.to_datetime(df['timestamp'], errors='coerce')
        df = df.dropna(subset=['ts'])
        df['ts'] = df['ts'].dt.tz_localize('UTC').dt.tz_convert('Asia/Kolkata')

        # Choose resample rule (use 'min' instead of deprecated 'T')
        if interval == "3m":
            rule = "3min"
        elif interval == "15m":
            rule = "15min"
        else:
            logging.error(f"[REBUILD] Unsupported interval={interval}, must be '3m' or '15m'")
            return

        # Aggregate ticks into OHLCV
        ohlc = (
            df.resample(rule, on='ts')
            .agg({
                'last_price': ['first', 'max', 'min', 'last'],
                'volume': 'sum'
            })
            .dropna()
        )

        # Flatten multi-index columns
        ohlc.columns = ['open', 'high', 'low', 'close', 'volume']
        ohlc.reset_index(inplace=True)

        # Persist each candle
        for _, row in ohlc.iterrows():
            trade_date = row['ts'].date().isoformat()
            ist_slot = row['ts'].strftime('%H:%M:%S')

            if interval == "3m":
                self.insert_3m_candle(
                    trade_date, ist_slot,
                    row['open'], row['high'], row['low'], row['close'], row['volume'], symbol
                )
            else:  # 15m
                self.insert_15m_candle(
                    trade_date, ist_slot,
                    row['open'], row['high'], row['low'], row['close'], row['volume'], symbol
                )

        logging.info(f"[REBUILD] Rebuilt {interval} candles for {symbol} ({len(ohlc)} rows)")

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

        from datetime import datetime, timedelta
        import pytz, os, sqlite3
        time_zone = pytz.timezone("Asia/Kolkata")
        today = datetime.now(time_zone).date()

        # --- Pick trade_date ---
        if use_yesterday:
            offset = 1
            while offset <= 5:  # safety cap
                candidate = today - timedelta(days=offset)
                trade_date = candidate.isoformat()
                query = f"SELECT * FROM {table} WHERE trade_date = ?"
                params = [trade_date]
                if symbol:
                    query += " AND symbol = ?"
                    params.append(symbol)

                try:
                    # Try current connection first
                    df = pd.read_sql_query(query, self.conn, params=params)
                    if df.empty:
                        # If empty, try opening the snapshot DB for that date
                        db_path = os.path.join(self.base_path, f"ticks_{trade_date}.db")
                        if os.path.exists(db_path):
                            with sqlite3.connect(db_path) as alt_conn:
                                df = pd.read_sql_query(query, alt_conn, params=params)
                                logging.debug(f"[DB INFO] Loaded {resolution} candles from {db_path}")
                    if not df.empty:
                        break
                except Exception as e:
                    logging.error(f"[DB ERROR] Failed to fetch {resolution} candles: {e}")
                offset += 1
            else:
                return pd.DataFrame(columns=["trade_date","ist_slot","open","high","low","close","volume","symbol"])
        else:
            trade_date = today.isoformat()
            query = f"SELECT * FROM {table} WHERE trade_date = ?"
            params = [trade_date]
            if symbol:
                query += " AND symbol = ?"
                params.append(symbol)

            if start_time:
                query += " AND ist_slot >= ?"
                params.append(start_time)
            if end_time:
                query += " AND ist_slot <= ?"
                params.append(end_time)

            df = pd.read_sql_query(query, self.conn, params=params)

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
                with sqlite3.connect(db_file) as conn:
                    df = pd.read_sql_query("SELECT * FROM ticks", conn)

                if df.empty:
                    continue

                # Normalize numeric columns
                for col in ["last_price", "volume", "bid", "ask"]:
                    if col in df.columns:
                        df[col] = pd.to_numeric(df[col], errors="coerce")

                dfs.append(df)
                logging.info(f"[LOAD] Loaded {len(df)} ticks from {db_file}")

            except Exception as e:
                logging.error(f"[DB ERROR] Failed to load session {db_file}: {e}")

        if dfs:
            return pd.concat(dfs, ignore_index=True)
        else:
            logging.warning("[LOAD] No tick data found in sessions")
            return pd.DataFrame(columns=["timestamp","trade_date","symbol","bid","ask","last_price","volume"])


# Instantiate global tick_db
tick_db = TickDatabase()

