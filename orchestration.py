# ===== orchestration.py =====

import logging
import pandas as pd
import pendulum as dt
from candle_builder import build_3min_candle, build_15m_candles
from tickdb import tick_db
from indicators import (
    calculate_cpr,
    calculate_traditional_pivots,
    calculate_camarilla_pivots,
    resolve_atr,
    daily_atr,
    calculate_ema, 
    calculate_atr, 
    calculate_adx, 
    calculate_cci, 
    supertrend
)

from signals import detect_signal, evaluate_candle
from signals import bias_from_indicators 
from config import symbols, time_zone
from setup import fyers_async
from datetime import timedelta

# ANSI COLORS
RESET   = "\033[0m"
GREEN   = "\033[92m"
YELLOW  = "\033[93m"
RED     = "\033[91m"
MAGENTA = "\033[95m"
GRAY    = "\033[90m"
CYAN    = "\033[96m"


def build_indicator_dataframe(symbol, interval="3m", df=None, df_15m=None):
    """
    Build an enriched indicator DataFrame for the given symbol and interval.
    If df is provided, use it directly; otherwise fetch candles from DB.
    Optionally pass df_15m for ATR resolution and bias context.
    """

    # --- Use provided DF or fetch from DB ---
    if df is None:
        df = tick_db.fetch_candles(resolution=interval, symbol=symbol)
    if df is None or df.empty:
        logging.warning(f"[INDICATORS] No {interval} candles for {symbol}")
        return pd.DataFrame()

    # --- Append latest in-progress candle from ticks ---
    latest_tick = tick_db.get_latest_tick(symbol)
    if latest_tick is not None:
        in_progress = {
            "open": latest_tick["last_price"],
            "high": latest_tick["last_price"],
            "low": latest_tick["last_price"],
            "close": latest_tick["last_price"],
            "volume": latest_tick.get("volume", 0),
            "trade_date": latest_tick["trade_date"],
            "ist_slot": pd.to_datetime(latest_tick["timestamp"])
                          .tz_localize("UTC")
                          .tz_convert("Asia/Kolkata")
                          .strftime("%H:%M:%S"),
            "symbol": symbol,
            "in_progress": True,
        }
        df = pd.concat([df, pd.DataFrame([in_progress])], ignore_index=True)

    # --- Indicators ---
    df["ema20"] = calculate_ema(df, column="close", period=20)
    df["ema50"] = calculate_ema(df, column="close", period=50)

    if len(df) >= 20:
        df["adx14"] = calculate_adx(df)
        df["cci20"] = calculate_cci(df)
    else:
        df["adx14"] = float("nan")
        df["cci20"] = float("nan")
        logging.warning(f"[INDICATORS] {symbol} {interval} insufficient bars ({len(df)}) for ADX/CCI")

    # --- ATR resolution for Supertrend ---
    daily_val = daily_atr(df_15m) if df_15m is not None and not df_15m.empty else None
    atr, atr_source = resolve_atr(df, daily_atr=daily_val)

    # --- Supertrend bias/slope assignment (last row only) ---
    try:
        bias, slope = supertrend(df, atr_val=atr)
        df.loc[df.index[-1], "supertrend_bias"] = bias
        df.loc[df.index[-1], "supertrend_slope"] = slope
    except Exception as e:
        logging.error(f"[SUPERTREND ERROR] {e}")
        df.loc[df.index[-1], "supertrend_bias"] = "NEUTRAL"
        df.loc[df.index[-1], "supertrend_slope"] = "FLAT"

    # --- Enrich with bias/signal ---
    df["signal"], df["confidence"] = zip(*df.apply(
        lambda row: bias_from_indicators(row, df_15m), axis=1
    ))

    # --- Debug log ---
    last_row = df.iloc[-1]
    progress_tag = "LIVE" if last_row.get("in_progress", False) else "FINAL"
    logging.info(
        f"{CYAN}[INDICATOR DF] {symbol} {interval} ({progress_tag}) "
        f"ema20={last_row['ema20']:.2f} ema50={last_row['ema50']:.2f} "
        f"adx14={last_row['adx14'] if not pd.isna(last_row['adx14']) else 'NA'} "
        f"cci20={last_row['cci20'] if not pd.isna(last_row['cci20']) else 'NA'} "
        f"supertrend_bias={last_row['supertrend_bias']} "
        f"slope={last_row['supertrend_slope']} "
        f"signal={last_row['signal']} confidence={last_row['confidence']}{RESET}"
    )

    return df


def build_multi_symbol_indicators(symbols=None, interval="3m"):
    if symbols is None:
        symbols = symbols

    result = {}
    for sym in symbols:
        df_15m = build_indicator_dataframe(sym, interval="15m")
        df = build_indicator_dataframe(sym, interval=interval, df_15m=df_15m)
        result[sym] = df
        logging.info(f"[INDICATORS] Built {interval} DataFrame for {sym} with {len(df)} rows")

    return result

def fetch_yesterday_15m(symbol):
    """Fetch yesterday's 15m candles from SQL DB for bootstrapping indicators."""
    try:
        df = tick_db.fetch_candles(resolution="15m", symbol=symbol)
        if df is None or df.empty:
            logging.warning(f"[BOOTSTRAP] No 15m candles found in DB for {symbol}")
            return pd.DataFrame()

        # Filter to yesterday’s date
        yesterday = (dt.now(time_zone) - dt.timedelta(days=1)).date()
        df_yday = df[df["trade_date"] == str(yesterday)]

        if df_yday.empty:
            logging.warning(f"[BOOTSTRAP] No 15m candles for {symbol} on {yesterday}")
            return pd.DataFrame()

        # Enrich with indicators (same as build_15m_candles)
        df_yday["ema20"] = calculate_ema(df_yday, column="close", period=20)
        df_yday["ema50"] = calculate_ema(df_yday, column="close", period=50)
        df_yday["adx14"] = calculate_adx(df_yday)
        df_yday["cci20"] = calculate_cci(df_yday)

        atr, atr_source = resolve_atr(df_yday, daily_val=None)
        bias, slope = supertrend(df_yday, atr_val=atr)
        df_yday.loc[df_yday.index[-1], "supertrend_bias"] = bias
        df_yday.loc[df_yday.index[-1], "supertrend_slope"] = slope

        logging.info(f"[BOOTSTRAP] Loaded {len(df_yday)} yesterday 15m candles for {symbol}")
        return df_yday

    except Exception as e:
        logging.error(f"[BOOTSTRAP ERROR] Failed to fetch yesterday 15m candles for {symbol}: {e}")
        return pd.DataFrame()


def update_candles_and_signals(symbol, spot_price=None):
    try:
        # --- Fetch latest ticks from DB ---
        df_ticks = tick_db.fetch_ticks(symbol)
        if df_ticks.empty:
            logging.warning(f"[UPDATE] No ticks found for {symbol}")
            return None, None

        # --- Fetch today’s 15m candles ---
        df_15m_today = tick_db.fetch_candles(resolution="15m", symbol=symbol)
        if df_15m_today.empty:
            logging.warning(f"[UPDATE] No 15m candles built for {symbol}")
            return None, None

        # --- Fetch yesterday’s 15m candles ---
        try:
            df_15m_yday = tick_db.fetch_candles(resolution="15m", symbol=symbol, use_yesterday=True)

            if df_15m_yday is not None and not df_15m_yday.empty:
                logging.debug(f"[BOOTSTRAP DEBUG] {symbol} available trade_dates: {df_15m_yday['trade_date'].unique()}")

                yesterday = (dt.now(time_zone) - timedelta(days=1)).strftime("%Y-%m-%d")
                df_15m_yday = df_15m_yday[df_15m_yday["trade_date"] == yesterday]

                if not df_15m_yday.empty:
                    logging.info(f"[BOOTSTRAP] Found {len(df_15m_yday)} yesterday 15m candles for {symbol}")

                    # --- Merge yesterday + today ---
                    merged_df = pd.concat([df_15m_yday, df_15m_today]).drop_duplicates(
                        subset=["trade_date", "ist_slot", "symbol"], keep="last"
                    )

                    # ✅ Build indicators on merged DF
                    df_15m = build_indicator_dataframe(symbol, interval="15m", df=merged_df)
                    logging.info(f"{CYAN}[BOOTSTRAP] Merged yesterday’s 15m candles with today for {symbol}{RESET}")
                else:
                    logging.warning(f"[BOOTSTRAP] No 15m candles found for {symbol} on {yesterday}")
                    df_15m = build_indicator_dataframe(symbol, interval="15m", df=df_15m_today)
            else:
                logging.warning(f"[BOOTSTRAP] No 15m candles in DB for {symbol}")
                df_15m = build_indicator_dataframe(symbol, interval="15m", df=df_15m_today)

        except Exception as e:
            logging.warning(f"[BOOTSTRAP ERROR] Could not fetch yesterday’s 15m candles: {e}")
            df_15m = build_indicator_dataframe(symbol, interval="15m", df=df_15m_today)

        # --- Build enriched 3m candles (with reference to 15m DF) ---
        df_3m = build_indicator_dataframe(symbol, interval="3m", df_15m=df_15m)
        if df_3m.empty:
            logging.warning(f"[UPDATE] No 3m candles built for {symbol}")
            return None, None

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

        # --- ATR calculation ---
        daily_val = daily_atr(df_15m)
        atr_value, atr_source = resolve_atr(df_15m, daily_atr=daily_val)
        atr_str = f"{atr_value:.2f}" if atr_value is not None else "NA"
        logging.info(f"[ATR] {symbol} source={atr_source} value={atr_str}")

        # --- Compute levels from last 15m candle ---
        last_candle_15m = df_15m.iloc[-1]
        cpr_levels = calculate_cpr(last_candle_15m.high, last_candle_15m.low, last_candle_15m.close)
        traditional_levels = calculate_traditional_pivots(last_candle_15m.high, last_candle_15m.low, last_candle_15m.close)
        camarilla_levels = calculate_camarilla_pivots(last_candle_15m.high, last_candle_15m.low, last_candle_15m.close)

        # --- Detect signal ---
        signal = detect_signal(
            cpr_levels,
            traditional_levels,
            camarilla_levels,
            df_3m,
            df_15m,
            spot_price=spot_price,
            daily_atr=daily_val
        )

        if signal:
            if isinstance(signal, (list, tuple)):
                if len(signal) == 4:
                    side, reason, targets, confidence = signal
                    logging.info(
                        f"{GREEN}[SIGNAL FIRED] {symbol} side={side} reason={reason} "
                        f"SL={targets['SL']:.2f} PT={targets['PT']:.2f} TG={targets['TG']:.2f} "
                        f"Confidence={confidence}{RESET}"
                    )
                elif len(signal) == 2:
                    side, reason = signal
                    logging.info(f"{GREEN}[SIGNAL FIRED] {symbol} side={side} reason={reason}{RESET}")
                else:
                    logging.error(f"[SIGNAL ERROR] Unexpected signal format: {signal}")
            else:
                logging.error(f"[SIGNAL ERROR] Signal not tuple/list: {signal}")

            return signal, df_3m
        else:
            logging.debug(f"[SIGNAL CHECK] No signal for {symbol}")
            return None, df_3m

    except Exception as e:
        logging.error(f"[UPDATE ERROR] {symbol}: {e}")
        return None, None
