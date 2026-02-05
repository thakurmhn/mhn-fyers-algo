import sqlite3
import pandas as pd
import logging
import os

from indicators import (
    calculate_cpr,
    calculate_traditional_pivots,
    calculate_camarilla_pivots,
    calculate_atr,
    calculate_ema,
    calculate_adx,
    calculate_cci,
    supertrend,
    daily_atr,
    check_bias,
    resolve_atr   # <-- updated rolling ATR function
)

from orchestration import build_indicator_dataframe
from signals import detect_signal
from setup import fyers_async

logging.basicConfig(level=logging.INFO)

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
    db_path = os.path.join(r"C:\\SQLite\\ticks", f"ticks_{date}.db")
    logging.info(f"[DB PATH] Using database at {db_path}")

    df_15m = fetch_candles(db_path, "candles_15m_ist", date)
    df_3m  = fetch_candles(db_path, "candles_3m_ist", date)

    # --- Filter for symbol ---
    df_15m = df_15m[df_15m["symbol"] == symbol]
    df_3m  = df_3m[df_3m["symbol"] == symbol]

    if df_15m.empty or df_3m.empty:
        logging.warning(f"[REPLAY] Missing candles for {date} {symbol}")
        return

    logging.info(f"[REPLAY] Loaded {len(df_15m)} 15m candles and {len(df_3m)} 3m candles for {symbol} on {date}")

    # --- Compute levels from last 15m candle ---
    last_15m = df_15m.iloc[-1]
    cpr_levels         = calculate_cpr(last_15m.high, last_15m.low, last_15m.close)
    traditional_levels = calculate_traditional_pivots(last_15m.high, last_15m.low, last_15m.close)
    camarilla_levels   = calculate_camarilla_pivots(last_15m.high, last_15m.low, last_15m.close)

    # --- Enrich 15m DF with indicators ---
    df_15m = build_indicator_dataframe(symbol, interval="15m")

    # --- Sequential replay over 3m candles ---
    for i in range(2, len(df_3m) + 1):  # start from 2nd candle (need prev + last)
        df_slice = df_3m.iloc[:i]  # progressively larger slice
        df_slice = build_indicator_dataframe(symbol, interval="3m", df_15m=df_15m)

        # --- Rolling ATR per slice ---
        atr_val, source = resolve_atr(df_slice, None, period=14)
        if atr_val is None:
            logging.warning(f"[REPLAY] ATR unavailable at candle {i}")
            continue

        # Debug log to confirm ATR evolution
        logging.info(f"[ATR CALC] candle={i} period=14 value={atr_val:.2f} source={source}")

        try:
            signal = detect_signal(
                cpr_levels=cpr_levels,
                traditional_levels=traditional_levels,
                camarilla_levels=camarilla_levels,
                candles_3m=df_slice,
                candles_15m=df_15m,
                spot_price=None,
                daily_atr=atr_val  # <-- evolving ATR per slice
            )
            if signal:
                side, reason, targets, confidence = signal
                ts = df_slice.iloc[-1]["ist_slot"]
                logging.info(
                    f"[REPLAY SIGNAL] {ts} side={side} reason={reason} "
                    f"SL={targets['SL']:.2f} PT={targets['PT']:.2f} TG={targets['TG']:.2f} "
                    f"Confidence={confidence} ATR={atr_val:.2f}"
                )
        except Exception as e:
            logging.error(f"[REPLAY ERROR] Candle {i}: {e}")

if __name__ == "__main__":
    date = "2026-02-05"
    symbol = "NSE:NIFTY50-INDEX"
    replay_signals(symbol, date)