# ==== candle_builder.py =======

import logging
import pandas as pd
import numpy as np
import pendulum as dt
from tickdb import tick_db
from config import time_zone
from indicators import (
    calculate_atr,
    daily_atr,
    resolve_atr,
    calculate_camarilla_pivots,
    calculate_cpr,
    calculate_traditional_pivots,
    check_bias,
    oscillator_entry_filter,
    oscillator_exit_trigger
)

# ===== Candle Builder (3m) =====
def build_3min_candle(spot_price, symbol="NSE:NIFTY50-INDEX"):
    try:
        df = tick_db.fetch_ticks(symbol)
        if df.empty or not isinstance(df, pd.DataFrame):
            logging.warning("[CANDLE BUILDER] No tick data available, skipping")
            return pd.DataFrame()

        df['timestamp'] = pd.to_datetime(df['timestamp'], errors='coerce')
        df = df.dropna(subset=['timestamp'])
        if df.empty:
            logging.warning("[CANDLE BUILDER] No valid rows after cleaning, skipping")
            return pd.DataFrame()

        df.set_index('timestamp', inplace=True)
        df['last_price'] = pd.to_numeric(df['last_price'], errors='coerce')
        df['volume'] = pd.to_numeric(df['volume'], errors='coerce')

        ohlcv = df['last_price'].resample('3min').ohlc()
        ohlcv['volume'] = df['volume'].resample('3min').sum()

        logging.info(f"[CANDLE BUILDER] Built {len(ohlcv)} 3m candles")

        # Persist all candles
        for ts, row in ohlcv.iterrows():
            trade_date = ts.date().isoformat()
            ist_slot = ts.strftime("%H:%M")
            tick_db.insert_3m_candle(
                trade_date, ist_slot,
                row['open'], row['high'], row['low'], row['close'], row['volume']
            )

        # Return candles for orchestration layer
        return ohlcv

    except Exception as e:
        logging.error(f"[CANDLE BUILDER ERROR] {e}")
        return pd.DataFrame()


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


def persist_15m_candle(ts, row):
    trade_date = ts.date().isoformat()
    ist_slot = ts.strftime("%H:%M")
    tick_db.insert_15m_candle(
        trade_date, ist_slot,
        row['open'], row['high'], row['low'], row['close'], row['volume']
    )


def build_15m_candles(df_intraday, target_date=None):
    try:
        df = prepare_intraday(df_intraday, target_date)
        df_15m = resample_15m(df)
        if df_15m.empty:
            return pd.DataFrame()

        logging.info(f"[CANDLE BUILDER] Built {len(df_15m)} 15m candles")

        # Persist all candles
        for ts, row in df_15m.iterrows():
            persist_15m_candle(ts, row)

        # Return candles for orchestration layer
        return df_15m

    except Exception as e:
        logging.error(f"[CANDLE BUILDER ERROR] {e}")
        return pd.DataFrame()

def get_today_15m_candles(hist_data):
    """
    Helper to extract today's 15m candles from historical intraday data.
    Wraps candle_builder.prepare_intraday + resample_15m.
    """
    if hist_data is None or hist_data.empty:
        logging.warning("[get_today_15m_candles] No historical data provided")
        return pd.DataFrame()

    try:
        today = dt.now(time_zone).date()
        df = prepare_intraday(hist_data, target_date=today)
        return resample_15m(df)
    except Exception as e:
        logging.error(f"[get_today_15m_candles ERROR] {e}")
        return pd.DataFrame()