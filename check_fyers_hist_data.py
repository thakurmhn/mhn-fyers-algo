import logging
import sqlite3
import pandas as pd
import pytz
from datetime import datetime as dt, timedelta
from setup import client_id, access_token, fyers

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

def fetch_fyers_history(symbol, days=5):
    """Fetch last N days of 15m candles from Fyers history API."""
    ist = pytz.timezone("Asia/Kolkata")
    today = dt.now(ist).date()
    start_date = today - timedelta(days=days)

    hist_req = {
        "symbol": symbol,
        "resolution": "15",
        "date_format": "1",
        "range_from": start_date.strftime("%Y-%m-%d"),
        "range_to": today.strftime("%Y-%m-%d"),
        "cont_flag": "1"
    }
    response = fyers.history(data=hist_req)
    hist_data = pd.DataFrame(response["candles"])

    # Log raw columns before renaming
    logging.info(f"[FYERS RAW] Columns: {hist_data.columns.tolist()}")
    logging.info(f"[FYERS RAW] Sample:\n{hist_data.head(5)}")

    # Rename to OHLCV
    hist_data.columns = ["date","open","high","low","close","volume"]

    # Convert to IST datetime
    hist_data["date"] = pd.to_datetime(hist_data["date"], unit="s") \
                            .dt.tz_localize("UTC").dt.tz_convert(ist)

    # Drop today's partial bars
    hist_data = hist_data[hist_data["date"].dt.date < today]

    # Add schema fields for SQL
    hist_data["trade_date"] = hist_data["date"].dt.strftime("%Y-%m-%d")
    hist_data["ist_slot"] = hist_data["date"].dt.strftime("%H:%M:%S")
    hist_data["symbol"] = symbol
    hist_data["time"] = hist_data["trade_date"] + " " + hist_data["ist_slot"]

    logging.info(f"[FYERS CLEANED] Sample:\n{hist_data.head(5)}")
    return hist_data

def insert_into_sql(hist_data, db_path="ticks.db"):
    """Insert cleaned candles into SQL table candles_15m_ist."""
    conn = sqlite3.connect(db_path)
    hist_data.to_sql("candles_15m_ist", conn, if_exists="append", index=False)
    conn.close()
    logging.info(f"[SQL] Inserted {len(hist_data)} rows into candles_15m_ist")

def main():
    symbol = "NSE:NIFTY50-INDEX"
    days = 5
    db_path = "ticks_2026-02-18.db"

    logging.info(f"[BOOTSTRAP] Fetching {days} days of 15m candles for {symbol} from Fyers")
    hist_data = fetch_fyers_history(symbol, days=days)

    logging.info(f"[BOOTSTRAP] Inserting into SQL DB {db_path}")
    insert_into_sql(hist_data, db_path=db_path)

    logging.info("[BOOTSTRAP] Completed successfully")

if __name__ == "__main__":
    main()