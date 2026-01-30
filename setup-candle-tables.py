import sqlite3
import logging

logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(message)s")

def setup_candle_tables(db_path):
    """
    Ensure both 3m and 15m candle tables exist with symbol column and indexes.
    """
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        # Create 3m table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS candles_3m_ist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_date TEXT NOT NULL,
                ist_slot TEXT NOT NULL,
                symbol TEXT NOT NULL,
                open REAL,
                high REAL,
                low REAL,
                close REAL,
                volume REAL
            )
        """)

        # Create 15m table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS candles_15m_ist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_date TEXT NOT NULL,
                ist_slot TEXT NOT NULL,
                symbol TEXT NOT NULL,
                open REAL,
                high REAL,
                low REAL,
                close REAL,
                volume REAL
            )
        """)

        # Indexes for fast lookups
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_candles_3m_symbol_slot
            ON candles_3m_ist (symbol, trade_date, ist_slot)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_candles_15m_symbol_slot
            ON candles_15m_ist (symbol, trade_date, ist_slot)
        """)

        conn.commit()
        logging.info("[SETUP] 3m and 15m candle tables ready with symbol column and indexes")

    except Exception as e:
        logging.error(f"[SETUP ERROR] {e}")
    finally:
        conn.close()


if __name__ == "__main__":
    setup_candle_tables(r"C:\SQLite\ticks\ticks_2026-01-30.db")