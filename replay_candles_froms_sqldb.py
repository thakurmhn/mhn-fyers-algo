import sqlite3
import pandas as pd
import logging
import os

from indicators import (
    calculate_cpr,
    calculate_traditional_pivots,
    calculate_camarilla_pivots,
    ema_bias,
    adx_bias,
    cci_bias,
    supertrend,
    calculate_atr,
    check_bias
)

from signals import detect_signal   # <-- your full production detect_signal

logging.basicConfig(level=logging.DEBUG)

def fetch_candles(db_path, table, date=None):
    conn = sqlite3.connect(db_path)
    query = f"SELECT * FROM {table}"
    if date:
        query += f" WHERE trade_date='{date}'"
    df = pd.read_sql_query(query, conn)
    conn.close()
    return df

def replay_signals(symbol, date):
    # --- Auto resolve DB path ---
    db_path = os.path.join(r"C:\SQLite\ticks", f"ticks_{date}.db")
    logging.info(f"[DB PATH] Using database at {db_path}")

    df_15m = fetch_candles(db_path, "candles_15m_ist", date)
    df_3m  = fetch_candles(db_path, "candles_3m_ist", date)

    if df_15m.empty or df_3m.empty:
        logging.warning(f"[REPLAY] Missing candles for {date}")
        return

    logging.info(f"[REPLAY] Loaded {len(df_15m)} 15m candles and {len(df_3m)} 3m candles for {symbol} on {date}")

    # --- Extract previous day's high/low/close from 15m candles ---
    prev_high  = df_15m['high'].iloc[-2]
    prev_low   = df_15m['low'].iloc[-2]
    prev_close = df_15m['close'].iloc[-2]

    # --- Compute levels dynamically ---
    cpr_levels         = calculate_cpr(prev_high, prev_low, prev_close)
    traditional_levels = calculate_traditional_pivots(prev_high, prev_low, prev_close)
    camarilla_levels   = calculate_camarilla_pivots(prev_high, prev_low, prev_close)

    try:
        signal = detect_signal(
            cpr_levels=cpr_levels,
            traditional_levels=traditional_levels,
            camarilla_levels=camarilla_levels,
            candles_3m=df_3m,
            candles_15m=df_15m,
            spot_price=None,
            daily_atr=None
        )
        logging.info(f"[REPLAY RESULT] Signal={signal}")
    except Exception as e:
        logging.error(f"[REPLAY ERROR] Signal detection failed: {e}")

if __name__ == "__main__":
    date = "2026-01-29"
    symbol = "NSE:NIFTY50-INDEX"
    replay_signals(symbol, date)