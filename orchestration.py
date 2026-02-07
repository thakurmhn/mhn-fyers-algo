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
    supertrend
)
from signals import detect_signal, classify_volatility, signal_confidence, bias_from_indicators
from setup import fyers_async

RESET   = "\033[0m"
GREEN   = "\033[92m"
CYAN    = "\033[96m"

def fmt(val):
    return f"{val:.2f}" if val is not None and not pd.isna(val) else "NA"

def fetch_ticks_from_db(symbol, date_str):
    """Fetch ticks directly from SQLite DB for a given date."""
    db_path = os.path.join(r"C:\\SQLite\\ticks", f"ticks_{date_str}.db")
    logging.info(f"[DB PATH] Using database at {db_path}")
    conn = sqlite3.connect(db_path)
    df = pd.read_sql_query("SELECT * FROM ticks", conn)
    conn.close()
    df = df[df["symbol"] == symbol]
    return df

def fetch_previous_day_candles(symbol, date_str, lookback_days=1):
    """Fetch previous day's 3m candles for continuity."""
    base_date = datetime.strptime(date_str, "%Y-%m-%d")
    dfs = []
    for d in range(1, lookback_days+1):
        prev_date = (base_date - timedelta(days=d)).strftime("%Y-%m-%d")
        df_ticks_prev = fetch_ticks_from_db(symbol, prev_date)
        if not df_ticks_prev.empty:
            df_prev_3m = build_3min_candle(df_ticks_prev, symbol)
            logging.info(f"[PREV DAY] Built {len(df_prev_3m)} 3m candles for {symbol} on {prev_date}")
            dfs.append(df_prev_3m)
    return pd.concat(dfs, ignore_index=True) if dfs else pd.DataFrame()

def build_indicator_dataframe(symbol, df):
    """Enrich candles with indicators."""
    df = df.copy()
    df["ema20"] = calculate_ema(df, column="close", period=20)
    df["ema50"] = calculate_ema(df, column="close", period=50)
    if len(df) >= 20:
        df["adx14"] = calculate_adx(df)
        df["cci20"] = calculate_cci(df)
    else:
        df["adx14"] = float("nan")
        df["cci20"] = float("nan")
        logging.warning(f"[INDICATORS] {symbol} insufficient bars ({len(df)}) for ADX/CCI")

    atr, _ = resolve_atr(df, None)
    try:
        bias, slope = supertrend(df, atr_val=atr)
        df.loc[df.index[-1], "supertrend_bias"] = bias
        df.loc[df.index[-1], "supertrend_slope"] = slope
    except Exception as e:
        logging.error(f"[SUPERTREND ERROR] {e}")
        df.loc[df.index[-1], "supertrend_bias"] = "NEUTRAL"
        df.loc[df.index[-1], "supertrend_slope"] = "FLAT"

    bias_reason, bias_score = bias_from_indicators(df.iloc[-1])
    vol_regime = classify_volatility(atr)
    conf_bucket = signal_confidence(vol_regime, bias_score, bias_reason)

    df.loc[df.index[-1], "bias_reason"] = bias_reason
    df.loc[df.index[-1], "bias_score"] = bias_score
    df.loc[df.index[-1], "vol_regime"] = vol_regime
    df.loc[df.index[-1], "confidence"] = conf_bucket

    last_row = df.iloc[-1]
    logging.info(
        f"{CYAN}[INDICATOR DF] {symbol} 3m "
        f"ema20={fmt(last_row['ema20'])} ema50={fmt(last_row['ema50'])} "
        f"adx14={fmt(last_row['adx14'])} cci20={fmt(last_row['cci20'])} "
        f"supertrend_bias={last_row['supertrend_bias']} slope={last_row['supertrend_slope']} "
        f"bias={last_row['bias_reason']} score={last_row['bias_score']} "
        f"vol={last_row['vol_regime']} confidence={last_row['confidence']}{RESET}"
    )
    return df

def update_candles_and_signals(symbol, spot_price=None, lookback_days=2):
    """
    Update loop with previous day candles included.
    Ensures enough historical candles are fetched for indicator warm-up.
    """

    try:
        today_str = datetime.now().strftime("%Y-%m-%d")

        # --- Fetch today's ticks ---
        df_ticks_today = fetch_ticks_from_db(symbol, today_str)
        if df_ticks_today.empty:
            logging.warning(f"[UPDATE] No ticks found for {symbol}")
            return None, None

        df_3m_today = build_3min_candle(df_ticks_today, symbol)

        # --- Fetch previous day candles for continuity ---
        df_3m_prev = fetch_previous_day_candles(symbol, today_str, lookback_days=lookback_days)

        # --- Concatenate prev + today ---
        df_3m = pd.concat([df_3m_prev, df_3m_today], ignore_index=True)
        if df_3m.empty:
            logging.warning(f"[UPDATE] No 3m candles built for {symbol}")
            return None, None

        # --- Enrich with indicators ---
        df_3m = build_indicator_dataframe(symbol, df=df_3m)

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
                    spot_price = latest_tick.get("ltp")
                    logging.info(f"[SPOT] {symbol} fallback to tick LTP: {spot_price}")

        if spot_price is None:
            logging.warning(f"[UPDATE] No spot price available for {symbol}")
            return None, df_3m

        # --- Compute levels from last candle ---
        last_candle = df_3m.iloc[-1]
        cpr_levels = calculate_cpr(last_candle.high, last_candle.low, last_candle.close)
        traditional_levels = calculate_traditional_pivots(last_candle.high, last_candle.low, last_candle.close)
        camarilla_levels = calculate_camarilla_pivots(last_candle.high, last_candle.low, last_candle.close)

        atr = last_candle.get("atr")
        bias_score = last_candle.get("bias_score")
        confidence = last_candle.get("confidence")

        # --- Detect signal with full history ---
        signal = detect_signal(
            cpr_levels,
            traditional_levels,
            camarilla_levels,
            df_3m,
            atr=atr,
            bias=bias_score  # bias-aware continuation
        )

        if signal:
            logging.info(
                f"{GREEN}[SIGNAL FIRED] {symbol} side={signal['side']} reason={signal['reason']} "
                f"PeakMomentum={fmt(signal['peak_momentum'])} ATR={fmt(atr)} Confidence={confidence}{RESET}"
            )
            return signal, df_3m
        else:
            logging.debug(f"[SIGNAL CHECK] No signal for {symbol}")
            return None, df_3m

    except Exception as e:
        logging.error(f"[UPDATE ERROR] {symbol}: {e}")
        return None, None