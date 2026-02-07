import pandas as pd
import sqlite3
import logging
import os
from datetime import datetime

from indicators import resolve_atr
from signals import detect_signal, check_exit_condition
from previous_day_pivot_testing import compute_levels
from execution import run_strategy   # <-- import your orchestration loop

# --- Colors ---
RESET   = "\033[0m"
GREEN   = "\033[92m"

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(levelname)s - %(message)s")

def fmt(val):
    try:
        if val is None or (isinstance(val, float) and pd.isna(val)):
            return "NA"
        return f"{val:.2f}"
    except Exception:
        return str(val) if val is not None else "NA"

def replay_run_strategy(symbol, date):
    # --- Connect to DBs ---
    db_path_prev = os.path.join(r"C:\\SQLite\\ticks", f"ticks_2026-02-05.db")
    db_path_curr = os.path.join(r"C:\\SQLite\\ticks", f"ticks_{date}.db")

    conn_prev = sqlite3.connect(db_path_prev)
    conn_curr = sqlite3.connect(db_path_curr)

    # --- Query 15m candles for daily OHLC ---
    query_15m = f"""
    SELECT trade_date, ist_slot, open, high, low, close
    FROM candles_15m_ist
    WHERE symbol = '{symbol}'
    """
    df_prev_15m = pd.read_sql(query_15m, conn_prev)
    df_curr_15m = pd.read_sql(query_15m, conn_curr)
    df_15m = pd.concat([df_prev_15m, df_curr_15m], ignore_index=True)

    df_15m["datetime"] = pd.to_datetime(df_15m["trade_date"] + " " + df_15m["ist_slot"])
    df_15m.set_index("datetime", inplace=True)

    # --- Build daily OHLC ---
    df_daily = df_15m.resample("1D").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last"
    }).dropna()

    prev_day = df_daily.iloc[-2]
    prev_high, prev_low, prev_close = prev_day.high, prev_day.low, prev_day.close

    # --- Compute levels ---
    traditional_levels, cpr_levels, camarilla_levels = compute_levels(prev_high, prev_low, prev_close)
    logging.info(f"[LEVELS] Traditional={traditional_levels} CPR={cpr_levels} Camarilla={camarilla_levels}")

    # --- Query 3m candles (current day only) ---
    query_3m = f"""
    SELECT trade_date, ist_slot, open, high, low, close
    FROM candles_3m_ist
    WHERE symbol = '{symbol}'
      AND trade_date = '{date}'
    """
    df_3m = pd.read_sql(query_3m, conn_curr)
    df_3m["datetime"] = pd.to_datetime(df_3m["trade_date"] + " " + df_3m["ist_slot"])
    df_3m.set_index("datetime", inplace=True)

    # --- Replay orchestration ---
    state = None
    counters = {"CALL": 0, "PUT": 0}

    for i in range(len(df_3m)):
        df_slice = df_3m.iloc[: i + 1]

        # Warm-up guard
        if len(df_slice) < 20:
            continue

        atr_val, _ = resolve_atr(df_slice, None)
        if atr_val is None:
            continue

        # Entry detection
        if state is None:
            signal = detect_signal(
                cpr_levels=cpr_levels,
                traditional_levels=traditional_levels,
                camarilla_levels=camarilla_levels,
                candles_3m=df_slice,
                atr=atr_val,
                bias=None
            )
            if signal:
                state = signal
                counters[state["side"]] += 1
                print(f"{GREEN}[ENTRY] Candle {i}: side={state['side']} reason={state['reason']} ATR={fmt(atr_val)}{RESET}")
            continue

        # Exit detection
        if state and check_exit_condition(df_slice, state):
            print(f"{GREEN}[EXIT] Candle {i}: side={state['side']} exit triggered "
                  f"Peak={fmt(state['peak_momentum'])} at Candle {state['peak_candle']}{RESET}")
            state = None

    # --- Summary counters ---
    logging.info(f"[SUMMARY] CALL trades={counters['CALL']} PUT trades={counters['PUT']}")

if __name__ == "__main__":
    date = "2026-02-06"
    symbol = "NSE:NIFTY50-INDEX"
    replay_run_strategy(symbol, date)