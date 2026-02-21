# ========= candle_builder.py ===========

import logging
import pandas as pd
import pendulum as dt
from config import time_zone

# ANSI COLORS
RESET   = "\033[0m"
GREEN   = "\033[92m"
YELLOW  = "\033[93m"
RED     = "\033[91m"
MAGENTA = "\033[95m"
GRAY    = "\033[90m"
CYAN    = "\033[96m"

from indicators import (
    resolve_atr,
    supertrend, 
    calculate_ema, 
    calculate_adx,
    calculate_cci,
    compute_rsi
)




def build_3min_candle(df_ticks, symbol):
    """Build 3m candles from tick data for a given symbol (no DB persistence)."""
    try:
        if df_ticks.empty or not isinstance(df_ticks, pd.DataFrame):
            logging.debug(f"[CANDLE BUILDER] No tick data available for {symbol}, skipping")
            return pd.DataFrame()

        required_cols = {"timestamp", "last_price", "volume"}
        if not required_cols.issubset(df_ticks.columns):
            logging.error(f"[CANDLE BUILDER] Missing required columns in tick data for {symbol}")
            return pd.DataFrame()

        # Clean timestamps
        df_ticks['timestamp'] = pd.to_datetime(df_ticks['timestamp'], errors='coerce')
        df_ticks = df_ticks.dropna(subset=['timestamp'])
        if df_ticks.empty:
            logging.debug(f"[CANDLE BUILDER] No valid rows after cleaning for {symbol}, skipping")
            return pd.DataFrame()

        df_ticks.set_index('timestamp', inplace=True)
        df_ticks['last_price'] = pd.to_numeric(df_ticks['last_price'], errors='coerce')
        df_ticks['volume'] = pd.to_numeric(df_ticks['volume'], errors='coerce').fillna(0)

        # Resample into 3m OHLCV
        ohlcv = df_ticks['last_price'].resample('3min').ohlc()
        ohlcv['volume'] = df_ticks['volume'].resample('3min').sum()

        # Add metadata
        ohlcv["trade_date"] = ohlcv.index.date.astype(str)
        ohlcv["ist_slot"] = ohlcv.index.strftime("%H:%M:%S")
        ohlcv["symbol"] = symbol

        # ✅ Add unified 'time' column for downstream strategy
        ohlcv["time"] = ohlcv.index.strftime("%Y-%m-%d %H:%M:%S")

        # --- Indicators (updated to EMA9/EMA13, no bias logic) ---
        ohlcv["ema9"] = calculate_ema(ohlcv, column="close", period=9)
        ohlcv["ema13"] = calculate_ema(ohlcv, column="close", period=13)

        ohlcv["adx14"] = calculate_adx(ohlcv)
        ohlcv["cci20"] = calculate_cci(ohlcv)

        atr, _ = resolve_atr(ohlcv, daily_atr=None)
        bias, slope = supertrend(ohlcv, atr_val=atr)
        ohlcv.loc[ohlcv.index[-1], "supertrend_bias"] = bias
        ohlcv.loc[ohlcv.index[-1], "supertrend_slope"] = slope

        logging.info(f"[CANDLE BUILDER] Built {len(ohlcv)} 3m candles for {symbol}")
        return ohlcv

    except Exception as e:
        logging.error(f"[CANDLE BUILDER ERROR] {e}")
        return pd.DataFrame(columns=[
            "trade_date","ist_slot","time","open","high","low","close","volume","symbol"
        ])

# ===== Candle Builder (15m) =====
def prepare_intraday(df_intraday, target_date=None):
    if not isinstance(df_intraday, pd.DataFrame):
        logging.error("[ERROR] Input is not a DataFrame")
        return pd.DataFrame()

    df = df_intraday.copy()

    # Detect time column
    if "timestamp" in df.columns:
        df["datetime"] = pd.to_datetime(df["timestamp"], errors="coerce")
    elif "last_traded_time" in df.columns:
        df["datetime"] = pd.to_datetime(df["last_traded_time"], unit="s", errors="coerce")
    elif "ist_slot" in df.columns:
        df["datetime"] = pd.to_datetime(df["ist_slot"], errors="coerce")
    elif "ist_candle" in df.columns:
        df["datetime"] = pd.to_datetime(df["ist_candle"], errors="coerce")
    else:
        logging.error("[ERROR] No valid time column found in DataFrame")
        return pd.DataFrame()

    df = df.dropna(subset=["datetime"]).set_index("datetime")

    if target_date is not None:
        df = df[df.index.date == target_date]

    # Enforce numeric dtypes
    for col in ["ltp","last_price","open","high","low","close","volume","vol_traded_today"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    return df


def resample_15m(df):
    if df.empty:
        return pd.DataFrame()

    if "last_price" in df.columns:
        df_15m = df["last_price"].resample("15min").ohlc()
        if "volume" in df.columns:
            df_15m["volume"] = df["volume"].resample("15min").sum()
    elif "ltp" in df.columns:
        df_15m = df["ltp"].resample("15min").ohlc()
        if "vol_traded_today" in df.columns:
            df_15m["volume"] = df["vol_traded_today"].resample("15min").sum()
        elif "volume" in df.columns:
            df_15m["volume"] = df["volume"].resample("15min").sum()
    elif {"open","high","low","close"} <= set(df.columns):
        agg_dict = {"open":"first","high":"max","low":"min","close":"last"}
        if "volume" in df.columns:
            agg_dict["volume"] = "sum"
        df_15m = df.resample("15min").agg(agg_dict)
    else:
        logging.error("[ERROR] No OHLC or tick columns found for resampling")
        return pd.DataFrame()

    df_15m = df_15m.dropna()
    df_15m = df_15m[(df_15m["high"] - df_15m["low"]) < 200]  # sanity filter
    return df_15m


def persist_15m_candle(tick_db, symbol, ts, row):
    trade_date = ts.date().isoformat()
    ist_slot = ts.strftime("%H:%M:%S")  # consistent format
    tick_db.insert_15m_candle(
        trade_date, ist_slot, row['open'], row['high'], row['low'], row['close'], row['volume'], symbol
    )

def build_15m_candles(df_intraday, tick_db, symbol, target_date=None):
    """Incrementally build and persist 15m candles for a given symbol."""
    try:
        df = prepare_intraday(df_intraday, target_date)
        if df.empty:
            logging.warning(f"[CANDLE BUILDER] No intraday data for {symbol}")
            return pd.DataFrame()

        # Restrict to last 1 day window
        end_time = df.index.max()
        start_time = end_time - pd.Timedelta(days=1)
        window = df.loc[start_time:end_time]

        # Resample into 15m OHLCV
        df_15m = resample_15m(window)
        if df_15m.empty:
            logging.warning(f"[CANDLE BUILDER] No 15m candles built for {symbol}")
            return pd.DataFrame()

        # Fetch already persisted 15m candles to avoid duplicates
        existing = tick_db.fetch_candles("15m", symbol=symbol)
        existing_slots = set(existing['ist_slot']) if not existing.empty else set()

        new_rows = []
        for ts, row in df_15m.iterrows():
            ist_slot = ts.strftime("%H:%M:%S")
            if ist_slot in existing_slots:
                continue
            persist_15m_candle(tick_db, symbol, ts, row)
            new_rows.append((ts, row))

        if new_rows:
            logging.info(f"{CYAN}[CANDLE BUILDER] Added {len(new_rows)} new 15m candles for {symbol}{RESET}")
        else:
            logging.debug(f"[CANDLE BUILDER] No new 15m candles for {symbol}")

        # Return candles for orchestration layer
        df_15m["trade_date"] = df_15m.index.date.astype(str)
        df_15m["ist_slot"] = df_15m.index.strftime("%H:%M:%S")
        df_15m["symbol"] = symbol

        # ✅ Add unified 'time' column for downstream strategy
        df_15m["time"] = df_15m.index.strftime("%Y-%m-%d %H:%M:%S")

        # ✅ Enrich with indicators (updated to EMA9/EMA13, no bias logic)
        df_15m["ema9"] = calculate_ema(df_15m, column="close", period=9)
        df_15m["ema13"] = calculate_ema(df_15m, column="close", period=13)
        df_15m["adx14"] = calculate_adx(df_15m)
        df_15m["cci20"] = calculate_cci(df_15m)

        atr, _ = resolve_atr(df_15m, daily_atr=None)
        bias, slope = supertrend(df_15m, atr_val=atr)
        df_15m.loc[df_15m.index[-1], "supertrend_bias"] = bias
        df_15m.loc[df_15m.index[-1], "supertrend_slope"] = slope

        return df_15m

    except Exception as e:
        logging.error(f"[CANDLE BUILDER ERROR] {e}")
        return pd.DataFrame(columns=[
            "trade_date","ist_slot","time","open","high","low","close","volume","symbol"
        ])
    

def get_today_15m_candles(hist_data):
    """Return today's 15m candles enriched with indicators and supertrend bias/slope."""
    if hist_data is None or hist_data.empty:
        logging.warning("[get_today_15m_candles] No historical data provided")
        return pd.DataFrame()

    try:
        today = dt.now(time_zone).date()
        df = prepare_intraday(hist_data, target_date=today)
        df_15m = resample_15m(df)
        if df_15m.empty:
            return pd.DataFrame()

        # Add metadata
        df_15m["trade_date"] = df_15m.index.date.astype(str)
        df_15m["ist_slot"] = df_15m.index.strftime("%H:%M:%S")
        df_15m["symbol"] = hist_data["symbol"].iloc[0] if "symbol" in hist_data.columns else "UNKNOWN"

        # ✅ Add unified 'time' column for downstream strategy
        df_15m["time"] = df_15m.index.strftime("%Y-%m-%d %H:%M:%S")

        # ✅ Enrich with indicators (updated to EMA9/EMA13, no bias logic)
        df_15m["ema9"] = calculate_ema(df_15m, column="close", period=9)
        df_15m["ema13"] = calculate_ema(df_15m, column="close", period=13)
        df_15m["adx14"] = calculate_adx(df_15m)
        df_15m["cci20"] = calculate_cci(df_15m)

        atr, _ = resolve_atr(df_15m, daily_atr=None)
        bias, slope = supertrend(df_15m, atr_val=atr)
        df_15m.loc[df_15m.index[-1], "supertrend_bias"] = bias
        df_15m.loc[df_15m.index[-1], "supertrend_slope"] = slope

        return df_15m

    except Exception as e:
        logging.error(f"{RED}[get_today_15m_candles ERROR] {e}{RESET}")
        return pd.DataFrame(columns=[
            "trade_date","ist_slot","time","open","high","low","close","volume","symbol"
        ])