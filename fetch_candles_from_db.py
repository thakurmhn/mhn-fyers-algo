import logging
import sqlite3
import pandas as pd
import pendulum as dt

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

# Dynamically pick today's DB file
time_zone = "Asia/Kolkata"
today_str = dt.now(time_zone).to_date_string()   # e.g. "2026-01-29"
db_path = f"C:/SQLite/ticks/ticks_{today_str}.db"

logging.info(f"Using DB file: {db_path}")

# Connect to SQLite
conn = sqlite3.connect(db_path)

# --- Fetch 3m candles ---
df_3m = pd.read_sql(
    "SELECT * FROM candles_3m_ist WHERE trade_date = ? ORDER BY ist_slot ASC",
    conn,
    params=(today_str,)
)
df_3m_clean = df_3m.dropna(subset=["open", "high", "low", "close"])

# --- Fetch 15m candles ---
df_15m = pd.read_sql(
    "SELECT * FROM candles_15m_ist WHERE trade_date = ? ORDER BY ist_slot ASC",
    conn,
    params=(today_str,)
)
df_15m_clean = df_15m.dropna(subset=["open", "high", "low", "close"])

# --- Multi-timeframe analysis ---
logging.info("Last 5 valid 3m candles:\n%s", df_3m_clean.tail())
# logging.info("Last 5 valid 15m candles:\n%s", df_15m_clean.tail())
logging.info("All valid 15m candles:\n%s", df_15m_clean)




conn.close()