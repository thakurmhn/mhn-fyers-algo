# ===== orchestration.py ============

import logging
import pandas as pd
import sqlite3
import os
from datetime import datetime, timedelta

from candle_builder import build_3min_candle
from tickdb import tick_db
from indicators import (
    calculate_cpr,
    calculate_traditional_pivots,
    calculate_camarilla_pivots,
    resolve_atr,
    calculate_ema,
    calculate_adx,
    calculate_cci,
    supertrend,
    compute_rsi
)
from signals import detect_signal, classify_volatility, signal_confidence, bias_from_indicators
from setup import fyers_async

RESET   = "\033[0m"
GREEN   = "\033[92m"
CYAN    = "\033[96m"

BASE_PATH = r"C:\SQLite\ticks"

def fmt(val):
    """Format numeric values safely for logs."""
    return f"{val:.2f}" if val is not None and not pd.isna(val) else "NA"


def fetch_ticks_from_db(symbol, date_str):
    """Fetch ticks directly from SQLite DB for a given date."""
    db_path = os.path.join(BASE_PATH, f"ticks_{date_str}.db")
    logging.info(f"[DB PATH] Using database at {db_path}")
    if not os.path.exists(db_path):
        logging.warning(f"[DB] No DB file found for {date_str}")
        return pd.DataFrame()

    try:
        conn = sqlite3.connect(db_path)
        tables = pd.read_sql_query("SELECT name FROM sqlite_master WHERE type='table'", conn)
        if "ticks" not in tables["name"].values:
            logging.warning(f"[DB WARN] {db_path} has no ticks table")
            conn.close()
            return pd.DataFrame()

        df = pd.read_sql_query("SELECT * FROM ticks WHERE symbol=?", conn, params=[symbol])
        conn.close()

        if df is None or df.empty:
            return pd.DataFrame()

        # Normalize numeric fields
        for col in ["last_price", "volume", "bid", "ask"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        return df
    except Exception as e:
        logging.error(f"[DB ERROR] Failed to fetch ticks: {e}")
        return pd.DataFrame()


def ensure_tables_exist():
    """Ensure required tables exist in the current tick_db connection."""
    try:
        tables = pd.read_sql_query("SELECT name FROM sqlite_master WHERE type='table'", tick_db.conn)
        required = {"ticks", "candles_3m_ist", "candles_15m_ist"}
        missing = required - set(tables["name"].values)
        if missing:
            logging.warning(f"[DB WARN] Missing tables {missing}, creating now")
            tick_db._create_tables()
    except Exception as e:
        logging.error(f"[DB ERROR] Failed to ensure tables exist: {e}")


def build_indicator_dataframe(symbol, df, interval="3m"):
    """Enrich candles with indicators (EMA, ADX, CCI, ATR, Supertrend, RSI, Bias/Confidence)."""
    if df is None or df.empty:
        logging.warning(f"[INDICATORS] No {interval} candles available for {symbol}")
        return pd.DataFrame()

    df = df.copy()

    # --- EMA ---
    df["ema20"] = calculate_ema(df, column="close", period=20)
    df["ema50"] = calculate_ema(df, column="close", period=50)

    # --- ADX ---
    if len(df) >= 14:
        try:
            df["adx14"] = calculate_adx(df)
        except Exception as e:
            logging.error(f"[ADX ERROR] {e}")
            df["adx14"] = float("nan")
    else:
        df["adx14"] = float("nan")
        logging.warning(f"[INDICATORS] {symbol} insufficient {interval} bars ({len(df)}) for ADX")

    # --- CCI ---
    if len(df) >= 20:
        try:
            df["cci20"] = calculate_cci(df)
        except Exception as e:
            logging.error(f"[CCI ERROR] {e}")
            df["cci20"] = float("nan")
    else:
        df["cci20"] = float("nan")
        logging.warning(f"[INDICATORS] {symbol} insufficient {interval} bars ({len(df)}) for CCI")

    # --- Fill missing values for continuity (both forward and backward) ---
    df["adx14"] = df["adx14"].bfill().ffill()
    df["cci20"] = df["cci20"].bfill().ffill()

    # --- ATR ---
    try:
        atr, _ = resolve_atr(df, daily_atr=None)
    except Exception as e:
        logging.error(f"[ATR ERROR] {e}")
        atr = float("nan")

    # --- Supertrend ---
    try:
        bias, slope = supertrend(df, atr_val=atr)
        df.loc[df.index[-1], "supertrend_bias"] = bias
        df.loc[df.index[-1], "supertrend_slope"] = slope
    except Exception as e:
        logging.error(f"[SUPERTREND ERROR] {e}")
        df.loc[df.index[-1], "supertrend_bias"] = "NEUTRAL"
        df.loc[df.index[-1], "supertrend_slope"] = "FLAT"

    # --- RSI ---
    try:
        df["rsi14"] = compute_rsi(df["close"], period=14)
    except Exception as e:
        logging.error(f"[RSI ERROR] {e}")
        df["rsi14"] = float("nan")

    # --- Bias & Confidence ---
    try:
        bias_reason, bias_score = bias_from_indicators(df.iloc[-1])
        vol_regime = classify_volatility(atr)
        conf_bucket = signal_confidence(vol_regime, bias_score, bias_reason)

        df.loc[df.index[-1], "bias_reason"] = bias_reason
        df.loc[df.index[-1], "bias_score"] = bias_score
        df.loc[df.index[-1], "vol_regime"] = vol_regime
        df.loc[df.index[-1], "confidence"] = conf_bucket
    except Exception as e:
        logging.error(f"[BIAS/CONFIDENCE ERROR] {e}")
        df.loc[df.index[-1], "bias_reason"] = "HOLD"
        df.loc[df.index[-1], "bias_score"] = 0.0
        df.loc[df.index[-1], "vol_regime"] = "UNKNOWN"
        df.loc[df.index[-1], "confidence"] = "LOW"

    # --- Logging ---
    last_row = df.iloc[-1]
    logging.info(
        f"{CYAN}[INDICATOR DF] {symbol} {interval} "
        f"ema20={fmt(last_row['ema20'])} ema50={fmt(last_row['ema50'])} "
        f"adx14={fmt(last_row['adx14'])} cci20={fmt(last_row['cci20'])} "
        f"rsi14={fmt(last_row['rsi14'])} "
        f"supertrend_bias={last_row['supertrend_bias']} slope={last_row['supertrend_slope']} "
        f"bias={last_row['bias_reason']} score={last_row['bias_score']} "
        f"vol={last_row['vol_regime']} confidence={last_row['confidence']}{RESET}"
    )

    return df


def fetch_previous_3m(symbol, base_path=r"C:\SQLite\ticks", lookback_days=7, min_days=3):
    """
    Fetch previous trading candles for the 3m interval from SQL DB for bootstrapping indicators.
    By default, fetches multiple days (min_days=3) to ensure enough rows for ADX/CCI.
    """
    today = datetime.now().date()
    collected = []

    for i in range(1, lookback_days + 1):
        candidate = today - timedelta(days=i)
        db_file = os.path.join(base_path, f"ticks_{candidate}.db")
        if not os.path.exists(db_file):
            continue

        try:
            conn = sqlite3.connect(db_file)
            df = pd.read_sql_query(
                "SELECT * FROM candles_3m_ist WHERE symbol=? ORDER BY trade_date, ist_slot",
                conn,
                params=[symbol]
            )
            conn.close()

            if df is None or df.empty:
                continue

            df_prev = df[df["trade_date"] == str(candidate)].copy()
            if df_prev.empty:
                continue

            # ✅ Add unified 'time' column
            df_prev["time"] = df_prev["trade_date"] + " " + df_prev["ist_slot"]

            collected.append(df_prev)
            logging.info(f"[CONTINUITY] {symbol} 3m: fetched {len(df_prev)} candles for {candidate}")

            # Stop once we have min_days worth of continuity
            if len(collected) >= min_days:
                break

        except Exception as e:
            logging.error(f"[CONTINUITY ERROR] Failed to fetch 3m candles for {symbol} on {candidate}: {e}")
            continue

    if collected:
        # ✅ Concatenate multiple days
        df_all = pd.concat(collected, ignore_index=True)
        logging.info(f"[CONTINUITY] {symbol} 3m: total {len(df_all)} candles from {len(collected)} days")

        # ✅ Enrich once on the full continuity set
        df_all = build_indicator_dataframe(symbol, df_all, interval="3m")
        return df_all

    logging.warning(f"[CONTINUITY] No 3m candles found for {symbol} in last {lookback_days} days")
    return pd.DataFrame(columns=["trade_date","ist_slot","time","open","high","low","close","volume","symbol"])



def fetch_previous_15m(symbol, interval="15m", base_path=r"C:\SQLite\ticks", lookback_days=7, min_days=3):
    """
    Fetch previous trading candles for the given interval (3m or 15m).
    By default, fetches multiple days (min_days=3) to ensure enough rows for ADX/CCI.
    """
    today = datetime.now().date()
    collected = []

    for i in range(1, lookback_days + 1):
        candidate = today - timedelta(days=i)
        db_file = os.path.join(base_path, f"ticks_{candidate}.db")
        if not os.path.exists(db_file):
            continue

        try:
            conn = sqlite3.connect(db_file)
            df = pd.read_sql_query(
                f"SELECT * FROM candles_{interval}_ist WHERE symbol=? ORDER BY trade_date, ist_slot",
                conn,
                params=[symbol]
            )
            conn.close()

            if df is None or df.empty:
                continue

            df_prev = df[df["trade_date"] == str(candidate)].copy()
            if df_prev.empty:
                continue

            # ✅ Add unified 'time' column
            df_prev["time"] = df_prev["trade_date"] + " " + df_prev["ist_slot"]

            collected.append(df_prev)
            logging.info(f"[CONTINUITY] {symbol} {interval}: fetched {len(df_prev)} candles for {candidate}")

            # Stop once we have min_days worth of continuity
            if len(collected) >= min_days:
                break

        except Exception as e:
            logging.error(f"[CONTINUITY ERROR] Failed to fetch {interval} candles for {symbol} on {candidate}: {e}")
            continue

    if collected:
        # ✅ Concatenate multiple days
        df_all = pd.concat(collected, ignore_index=True)
        logging.info(f"[CONTINUITY] {symbol} {interval}: total {len(df_all)} candles from {len(collected)} days")

        # ✅ Enrich once on the full continuity set
        df_all = build_indicator_dataframe(symbol, df_all, interval=interval)
        return df_all

    logging.warning(f"[CONTINUITY] No {interval} candles found for {symbol} in last {lookback_days} days")
    return pd.DataFrame(columns=["trade_date","ist_slot","time","open","high","low","close","volume","symbol"])


def merge_candles(prev_df, today_df, symbol, interval, prev_date, today_str, include_partial=True):
    """Merge previous day + today candles, drop duplicates, return merged DataFrame.
       include_partial=False will drop rows flagged as is_partial.
    """

    def ensure_time(df):
        if df is not None and not df.empty:
            df = df.copy()
            # Case 1: Already has trade_date + ist_slot
            if {"trade_date", "ist_slot"} <= set(df.columns):
                if "time" not in df.columns:
                    df["time"] = df["trade_date"] + " " + df["ist_slot"]
            # Case 2: Has 'ts' column from tick_db.build_candles_from_ticks
            elif "ts" in df.columns:
                df["trade_date"] = df["ts"].dt.strftime("%Y-%m-%d")
                df["ist_slot"] = df["ts"].dt.strftime("%H:%M:%S")
                df["symbol"] = symbol
                df["time"] = df["ts"].dt.strftime("%Y-%m-%d %H:%M:%S")
        return df

    prev_df = ensure_time(prev_df)
    today_df = ensure_time(today_df)

    # 🔍 Drop partial rows if requested
    if not include_partial:
        if prev_df is not None and "is_partial" in prev_df.columns:
            prev_df = prev_df.loc[~prev_df["is_partial"]]
        if today_df is not None and "is_partial" in today_df.columns:
            today_df = today_df.loc[~today_df["is_partial"]]

    if prev_df is not None and not prev_df.empty and today_df is not None and not today_df.empty:
        merged_df = pd.concat([prev_df, today_df], ignore_index=True).drop_duplicates(
            subset=["trade_date", "ist_slot", "symbol"], keep="last"
        ).sort_values(by=["trade_date", "ist_slot"])
        logging.info(
            f"[MERGE] {symbol} {interval} continuity merged from {prev_date} + {today_str}: "
            f"prev={len(prev_df)} | today={len(today_df)} | merged={len(merged_df)}"
        )
        logging.info(f"[MERGE SAMPLE] {symbol} {interval} merged tail:\n{merged_df.tail(3)}")
        return merged_df.reset_index(drop=True)

    elif today_df is not None and not today_df.empty:
        logging.info(
            f"[MERGE] {symbol} {interval} continuity not available, using today only ({len(today_df)})"
        )
        logging.info(f"[MERGE SAMPLE] {symbol} {interval} today tail:\n{today_df.tail(3)}")
        return today_df.reset_index(drop=True)

    elif prev_df is not None and not prev_df.empty:
        logging.info(
            f"[MERGE] {symbol} {interval} no today candles yet, using continuity only ({len(prev_df)})"
        )
        logging.info(f"[MERGE SAMPLE] {symbol} {interval} continuity tail:\n{prev_df.tail(3)}")
        return prev_df.reset_index(drop=True)

    else:
        logging.warning(f"[MERGE] No {interval} candles available for {symbol}")
        return pd.DataFrame(columns=[
            "trade_date","ist_slot","time","open","high","low","close","volume","symbol","is_partial"
        ])
    

# def update_candles_and_signals(symbol, spot_price=None, base_path=r"C:\SQLite\ticks", use_higher_tf=False):
#     """
#     Update loop with previous trading day candles included (3m + 15m).
#     Uses fetch_previous_3m() and fetch_previous_15m() helpers for continuity.
#     Runs signal detection only on 3m candles unless use_higher_tf=True.
#     Returns (signal, df_3m, df_15m).
#     """
#     try:
#         today_str = datetime.now().strftime("%Y-%m-%d")

#         # --- Fetch today's ticks ---
#         df_ticks_today = fetch_ticks_from_db(symbol, today_str)
#         if df_ticks_today is None or df_ticks_today.empty:
#             logging.warning(f"[UPDATE] No ticks found for {symbol}, bootstrapping from continuity only")
#             df_3m_prev = fetch_previous_3m(symbol, base_path=base_path)
#             df_15m_prev = fetch_previous_15m(symbol, interval="15m", base_path=base_path)
#             logging.info(f"[SUMMARY BOOTSTRAP] {symbol}: continuity only -> 3m={len(df_3m_prev)} candles, 15m={len(df_15m_prev)} candles")
#             return None, df_3m_prev, df_15m_prev

#         # --- Build today's 3m candles ---
#         df_3m_today = build_3min_candle(df_ticks_today, symbol)
#         if df_3m_today is None or df_3m_today.empty:
#             df_3m_today = pd.DataFrame()
#         else:
#             df_3m_today = build_indicator_dataframe(symbol, df_3m_today, interval="3m")

#         # --- Build today's 15m candles ---
#         df_15m_today = tick_db.build_candles_from_ticks(symbol, interval="15m")
#         if df_15m_today is None or df_15m_today.empty:
#             df_15m_today = pd.DataFrame()
#         else:
#             df_15m_today = df_15m_today.copy()
#             # Normalize schema for merge compatibility
#             if "trade_date" not in df_15m_today.columns:
#                 df_15m_today["trade_date"] = pd.to_datetime(df_15m_today.index).strftime("%Y-%m-%d")
#             if "ist_slot" not in df_15m_today.columns:
#                 df_15m_today["ist_slot"] = pd.to_datetime(df_15m_today.index).strftime("%H:%M:%S")
#             if "symbol" not in df_15m_today.columns:
#                 df_15m_today["symbol"] = symbol
#             df_15m_today["time"] = df_15m_today["trade_date"] + " " + df_15m_today["ist_slot"]

#             df_15m_today = build_indicator_dataframe(symbol, df_15m_today, interval="15m")

#         # --- Fetch previous trading day candles ---
#         df_3m_prev = fetch_previous_3m(symbol, base_path=base_path)
#         df_15m_prev = fetch_previous_15m(symbol, interval="15m", base_path=base_path)

#         # --- Merge continuity + today ---
#         df_3m = merge_candles(df_3m_prev, df_3m_today, symbol, "3m", prev_date="2026-02-16", today_str=today_str)
#         df_15m = merge_candles(df_15m_prev, df_15m_today, symbol, "15m", prev_date="2026-02-16", today_str=today_str)

#         # --- Enrich merged sets ---
#         if df_3m is not None and not df_3m.empty:
#             df_3m = build_indicator_dataframe(symbol, df_3m, interval="3m")
#         if df_15m is not None and not df_15m.empty:
#             df_15m = build_indicator_dataframe(symbol, df_15m, interval="15m")

#         logging.info(f"[SUMMARY] Update complete for {symbol}: 3m={len(df_3m)} candles, 15m={len(df_15m)} candles")
#         logging.info(
#             f"[DASHBOARD] {symbol} counts -> prev_3m={len(df_3m_prev)} today_3m={len(df_3m_today)} merged_3m={len(df_3m)} | "
#             f"prev_15m={len(df_15m_prev)} today_15m={len(df_15m_today)} merged_15m={len(df_15m)}"
#         )

#         # --- Resolve spot price ---
#         if spot_price is None:
#             try:
#                 quote = fyers_async.quotes({"symbols": symbol})
#                 spot_price = quote["d"][0]["v"].get("lp")
#                 logging.info(f"[SPOT] {symbol} resolved via quotes API: {spot_price}")
#             except Exception as e:
#                 logging.warning(f"[SPOT FALLBACK] {symbol} quotes API failed: {e}")
#                 latest_tick = tick_db.get_latest_tick(symbol)
#                 if latest_tick is not None:
#                     spot_price = latest_tick.get("last_price")
#                     logging.info(f"[SPOT] {symbol} fallback to tick last_price: {spot_price}")

#         if spot_price is None:
#             logging.warning(f"[UPDATE] No spot price available for {symbol}")
#             return None, df_3m, df_15m

#         # --- Compute levels from last 3m candle ---
#         if df_3m is None or df_3m.empty:
#             logging.warning(f"[UPDATE] No 3m candles available for {symbol}")
#             return None, df_3m, df_15m

#         last_candle = df_3m.iloc[-1]
#         cpr_levels = calculate_cpr(last_candle.high, last_candle.low, last_candle.close)
#         traditional_levels = calculate_traditional_pivots(last_candle.high, last_candle.low, last_candle.close)
#         camarilla_levels = calculate_camarilla_pivots(last_candle.high, last_candle.low, last_candle.close)

#         atr = last_candle.get("atr")
#         bias_score = last_candle.get("bias_score")
#         rsi_val = last_candle.get("rsi14")

#         # --- Detect signal (3m only unless use_higher_tf=True) ---
#         signal = detect_signal(
#             cpr_levels,
#             traditional_levels,
#             camarilla_levels,
#             df_3m,
#             atr=atr,
#             bias=bias_score,
#             higher_tf=df_15m if (df_15m is not None and not df_15m.empty and use_higher_tf) else None
#         )

#         if signal:
#             logging.info(
#                 f"{GREEN}[SIGNAL FIRED] {symbol} side={signal['side']} reason={signal['reason']} "
#                 f"PeakMomentum={fmt(signal['peak_momentum'])} ATR={fmt(atr)} RSI={fmt(rsi_val)}{RESET}"
#             )
#         else:
#             logging.debug(f"[SIGNAL CHECK] No signal for {symbol}")

#         return signal, df_3m, df_15m

#     except Exception as e:
#         logging.error(f"[UPDATE ERROR] {symbol}: {e}")
#         return None, pd.DataFrame(), pd.DataFrame()

def update_candles_and_signals(symbol, spot_price=None, base_path=r"C:\SQLite\ticks", use_higher_tf=False):
    """
    Update loop with previous trading day candles included (3m + 15m).
    - Fetch today's ticks
    - Build today's 3m and 15m candles
    - Merge with previous day continuity
    - Enrich merged sets with indicators
    - Detect signals on 3m candles, with optional 15m bias confirmation
    Returns (signal, df_3m, df_15m).
    """
    try:
        today_str = datetime.now().strftime("%Y-%m-%d")

        # --- Fetch today's ticks ---
        df_ticks_today = fetch_ticks_from_db(symbol, today_str)
        if df_ticks_today is None or df_ticks_today.empty:
            logging.warning(f"[UPDATE] No ticks found for {symbol}, bootstrapping from continuity only")
            df_3m_prev = fetch_previous_3m(symbol, base_path=base_path)
            df_15m_prev = fetch_previous_15m(symbol, interval="15m", base_path=base_path)
            logging.info(f"[SUMMARY BOOTSTRAP] {symbol}: continuity only -> 3m={len(df_3m_prev)} candles, 15m={len(df_15m_prev)} candles")
            return None, df_3m_prev, df_15m_prev

        # --- Build today's 3m candles ---
        df_3m_today = build_3min_candle(df_ticks_today, symbol)
        if df_3m_today is None or df_3m_today.empty:
            df_3m_today = pd.DataFrame()

        # --- Build today's 15m candles ---
        df_15m_today = tick_db.build_candles_from_ticks(symbol, interval="15m")
        if df_15m_today is None or df_15m_today.empty:
            df_15m_today = pd.DataFrame()
        else:
            # Normalize schema for merge compatibility
            df_15m_today = df_15m_today.copy()
            if "trade_date" not in df_15m_today.columns:
                df_15m_today["trade_date"] = pd.to_datetime(df_15m_today.index).strftime("%Y-%m-%d")
            if "ist_slot" not in df_15m_today.columns:
                df_15m_today["ist_slot"] = pd.to_datetime(df_15m_today.index).strftime("%H:%M:%S")
            if "symbol" not in df_15m_today.columns:
                df_15m_today["symbol"] = symbol
            df_15m_today["time"] = df_15m_today["trade_date"] + " " + df_15m_today["ist_slot"]

        # --- Fetch previous trading day candles ---
        df_3m_prev = fetch_previous_3m(symbol, base_path=base_path)
        df_15m_prev = fetch_previous_15m(symbol, interval="15m", base_path=base_path)

        # --- Merge continuity + today ---
        df_3m = merge_candles(df_3m_prev, df_3m_today, symbol, "3m", prev_date="2026-02-16", today_str=today_str)
        df_15m = merge_candles(df_15m_prev, df_15m_today, symbol, "15m", prev_date="2026-02-16", today_str=today_str)

        # --- Enrich merged sets (not today-only) ---
        if df_3m is not None and not df_3m.empty:
            df_3m = build_indicator_dataframe(symbol, df_3m, interval="3m")
        if df_15m is not None and not df_15m.empty:
            df_15m = build_indicator_dataframe(symbol, df_15m, interval="15m")

        logging.info(f"[SUMMARY] Update complete for {symbol}: 3m={len(df_3m)} candles, 15m={len(df_15m)} candles")
        logging.info(
            f"[DASHBOARD] {symbol} counts -> prev_3m={len(df_3m_prev)} today_3m={len(df_3m_today)} merged_3m={len(df_3m)} | "
            f"prev_15m={len(df_15m_prev)} today_15m={len(df_15m_today)} merged_15m={len(df_15m)}"
        )

        # --- Resolve spot price ---
        if spot_price is None:
            try:
                quote = fyers_async.quotes({"symbols": symbol})
                spot_price = quote["d"][0]["v"].get("lp")
                logging.info(f"[SPOT] {symbol} resolved via quotes API: {spot_price}")
            except Exception as e:
                logging.warning(f"[SPOT FALLBACK] {symbol} quotes API failed: {e}")
                latest_tick = tick_db.get_latest_tick(symbol)
                if latest_tick is not None:
                    spot_price = latest_tick.get("last_price")
                    logging.info(f"[SPOT] {symbol} fallback to tick last_price: {spot_price}")

        if spot_price is None:
            logging.warning(f"[UPDATE] No spot price available for {symbol}")
            return None, df_3m, df_15m

        # --- Compute levels from last 3m candle ---
        if df_3m is None or df_3m.empty:
            logging.warning(f"[UPDATE] No 3m candles available for {symbol}")
            return None, df_3m, df_15m

        last_candle = df_3m.iloc[-1]
        cpr_levels = calculate_cpr(last_candle.high, last_candle.low, last_candle.close)
        traditional_levels = calculate_traditional_pivots(last_candle.high, last_candle.low, last_candle.close)
        camarilla_levels = calculate_camarilla_pivots(last_candle.high, last_candle.low, last_candle.close)

        atr = last_candle.get("atr")
        bias_score = last_candle.get("bias_score")
        rsi_val = last_candle.get("rsi14")

        # --- Detect signal (3m only unless use_higher_tf=True) ---
        signal = detect_signal(
            cpr_levels,
            traditional_levels,
            camarilla_levels,
            df_3m,
            atr=atr,
            bias=bias_score,
            higher_tf=df_15m if (df_15m is not None and not df_15m.empty and use_higher_tf) else None,
            include_partial=False  # ✅ ensure trades only fire after 3m candle close
        )

        if signal:
            logging.info(
                f"{GREEN}[SIGNAL FIRED] {symbol} side={signal['side']} reason={signal['reason']} "
                f"PeakMomentum={fmt(signal['peak_momentum'])} ATR={fmt(atr)} RSI={fmt(rsi_val)}{RESET}"
            )
        else:
            logging.debug(f"[SIGNAL CHECK] No signal for {symbol}")

        return signal, df_3m, df_15m

    except Exception as e:
        logging.error(f"[UPDATE ERROR] {symbol}: {e}")
        return None, pd.DataFrame(), pd.DataFrame()
    

if __name__ == "__main__":
    import os
    import sqlite3
    import pandas as pd

    SYMBOL = "NSE:NIFTY50-INDEX"
    INTERVAL = "15m"
    BASE_PATH = r"C:\SQLite\ticks"

    def fetch_candles(db_file, interval="15m"):
        conn = sqlite3.connect(db_file)
        df = pd.read_sql_query(
            f"SELECT * FROM candles_{interval}_ist WHERE symbol=? ORDER BY trade_date, ist_slot",
            conn,
            params=[SYMBOL]
        )
        conn.close()
        return df

    # continuity (Feb 16) + today (Feb 17)
    prev_file = os.path.join(BASE_PATH, "ticks_2026-02-16.db")
    today_file = os.path.join(BASE_PATH, "ticks_2026-02-17.db")

    df_prev = fetch_candles(prev_file, INTERVAL)
    df_today = fetch_candles(today_file, INTERVAL)

    print(f"[DEBUG] Prev candles={len(df_prev)} Today candles={len(df_today)}")
    print("[DEBUG] Today columns:", df_today.columns.tolist())
    print(df_today.head(3))

    merged = merge_candles(df_prev, df_today, SYMBOL, INTERVAL, prev_date="2026-02-16", today_str="2026-02-17")

    print(f"[RESULT] Merged candles={len(merged)}")
    print(merged.tail(5))

    # ✅ Enrich merged candles with indicators
    enriched = build_indicator_dataframe(SYMBOL, merged.copy(), interval=INTERVAL)

    print("[ENRICHED SAMPLE] Tail of merged + indicators:")
    print(enriched.tail(5))