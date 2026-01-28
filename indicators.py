# ===== indicators.py =====

import logging
import pandas as pd
import numpy as np
import pendulum as dt
import datetime

from config import time_zone, ATR_VALUE
from setup import spot_price, fyers

# ===========================================================
# Globals
ticks_buffer = []
candles_3m = pd.DataFrame(columns=["open","high","low","close","time"])
current_3m_start = None

# ANSI COLORS
RESET   = "\033[0m"
GREEN   = "\033[92m"
YELLOW  = "\033[93m"
RED     = "\033[91m"
MAGENTA = "\033[95m"
GRAY    = "\033[90m"
# ===========================================================

# ===== Pivot Calculations =====
def calculate_cpr(high, low, close):
    pivot = (high + low + close) / 3
    bc = (high + low) / 2
    tc = (pivot - bc) + pivot
    return {"pivot": round(pivot, 2), "bc": round(bc, 2), "tc": round(tc, 2)}

def calculate_traditional_pivots(high, low, close):
    pivot = (high + low + close) / 3
    r1 = (2 * pivot) - low
    s1 = (2 * pivot) - high
    r2 = pivot + (high - low)
    s2 = pivot - (high - low)
    return {"pivot": round(pivot, 2), "r1": round(r1, 2), "s1": round(s1, 2),
            "r2": round(r2, 2), "s2": round(s2, 2)}

def calculate_camarilla_pivots(high, low, close):
    range_val = high - low
    r3 = close + (range_val * 1.1 / 4)
    r4 = close + (range_val * 1.1 / 2)
    s3 = close - (range_val * 1.1 / 4)
    s4 = close - (range_val * 1.1 / 2)
    return {"r3": round(r3, 2), "r4": round(r4, 2),
            "s3": round(s3, 2), "s4": round(s4, 2)}

# ===== ATR =====
def calculate_atr(df_, period=14):
    if len(df_) < period + 1: return None
    hl = df_["high"] - df_["low"]
    hc = (df_["high"] - df_["close"].shift()).abs()
    lc = (df_["low"] - df_["close"].shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    return float(tr.rolling(period).mean().iloc[-1])

def resolve_atr(candles_3m_, daily_atr_):
    atr_3m = calculate_atr(candles_3m_)
    if atr_3m is not None: return atr_3m, "ATR_3M"
    if daily_atr_ is not None: return daily_atr_, "ATR_DAILY"
    if len(candles_3m_) >= 2:
        atr_boot = candles_3m_["high"].max() - candles_3m_["low"].min()
        logging.warning(f"[BOOTSTRAP ATR] using range={atr_boot:.2f}")
        return atr_boot, "ATR_BOOTSTRAP"
    return None, None

def daily_atr(df_daily, period=14):
    """
    Calculate ATR on daily candles.
    df_daily must have columns: ['high','low','close']
    """
    if len(df_daily) < period + 1:
        return None
    hl = df_daily["high"] - df_daily["low"]
    hc = (df_daily["high"] - df_daily["close"].shift()).abs()
    lc = (df_daily["low"] - df_daily["close"].shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    return float(tr.rolling(period).mean().iloc[-1])


# ===== Momentum =====
def momentum_ok(candles, side):
    if len(candles) < 2: return False, 0
    last, prev = candles.iloc[-1], candles.iloc[-2]
    momentum = last.close - prev.close
    ok = momentum > 0 if side == "CALL" else momentum < 0
    return ok, momentum

# ===== Candle builder =====

# ===== Candle builder =====

def build_3min_candle(df_intraday, target_date=None):
    df = df_intraday.copy()

    # logging.info(f"[DEBUG] build_3min_candle received columns: {df.columns.tolist()}")

    # Handle different timestamp column names
    if "timestamp" in df.columns:
        df["datetime"] = pd.to_datetime(df["timestamp"])
    elif "last_traded_time" in df.columns:
        df["datetime"] = pd.to_datetime(df["last_traded_time"], unit="s")
    elif "ist_slot" in df.columns:
        df["datetime"] = pd.to_datetime(df["ist_slot"])
    elif "ist_candle" in df.columns:
        df["datetime"] = pd.to_datetime(df["ist_candle"])
    else:
        logging.error("[ERROR] No valid time column found in DataFrame")
        return pd.DataFrame()

    df = df.set_index("datetime")

    if target_date is not None:
        df = df[df.index.date == target_date]

    # ✅ Ensure numeric columns are converted
    for col in ["ltp", "last_price", "open", "high", "low", "close", "volume", "vol_traded_today"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # If raw ticks (last_price or ltp + volume)
    if "last_price" in df.columns:
        df_3m = df["last_price"].resample("3min").ohlc()
        if "volume" in df.columns:
            df_3m["volume"] = df["volume"].resample("3min").sum()
    elif "ltp" in df.columns:
        df_3m = df["ltp"].resample("3min").ohlc()
        if "vol_traded_today" in df.columns:
            df_3m["volume"] = df["vol_traded_today"].resample("3min").sum()
        elif "volume" in df.columns:
            df_3m["volume"] = df["volume"].resample("3min").sum()
    elif {"open","high","low","close"} <= set(df.columns):
        # Already OHLC candles
        agg_dict = {"open": "first", "high": "max", "low": "min", "close": "last"}
        if "volume" in df.columns:
            agg_dict["volume"] = "sum"
        df_3m = df.resample("3min").agg(agg_dict)
    else:
        logging.error("[ERROR] No OHLC or tick columns found for resampling")
        return pd.DataFrame()

    df_3m = df_3m.dropna()

    # logging.info(f"[SUMMARY] Built {len(df_3m)} 3m candles for {target_date or 'full dataset'}")
    return df_3m


def build_15m_candles(df_intraday, target_date=None):
    df = df_intraday.copy()

    # Handle different timestamp column names
    if "timestamp" in df.columns:
        df["datetime"] = pd.to_datetime(df["timestamp"])
    elif "last_traded_time" in df.columns:
        df["datetime"] = pd.to_datetime(df["last_traded_time"], unit="s")
    elif "ist_slot" in df.columns:
        df["datetime"] = pd.to_datetime(df["ist_slot"])
    elif "ist_candle" in df.columns:
        df["datetime"] = pd.to_datetime(df["ist_candle"])
    else:
        logging.error("[ERROR] No valid time column found in DataFrame")
        return pd.DataFrame()

    df = df.set_index("datetime")

    if target_date is not None:
        df = df[df.index.date == target_date]

    # ✅ Ensure numeric columns are converted
    for col in ["ltp", "last_price", "open", "high", "low", "close", "volume", "vol_traded_today"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # ✅ Sanitize ticks: drop unrealistic LTP values (e.g., outside 20k–30k for NIFTY50)
    if "ltp" in df.columns:
        df = df[(df["ltp"] > 20000) & (df["ltp"] < 30000)]

    if "last_price" in df.columns:
        df = df[(df["last_price"] > 20000) & (df["last_price"] < 30000)]

    # Build 15m candles
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
        agg_dict = {"open": "first", "high": "max", "low": "min", "close": "last"}
        if "volume" in df.columns:
            agg_dict["volume"] = "sum"
        df_15m = df.resample("15min").agg(agg_dict)
    else:
        logging.error("[ERROR] No OHLC or tick columns found for resampling")
        return pd.DataFrame()

    df_15m = df_15m.dropna()

    # ✅ Sanity filter: drop candles with unrealistic ranges (>200 points for NIFTY50)
    df_15m = df_15m[(df_15m["high"] - df_15m["low"]) < 200]

    return df_15m



# ===== Historical Intraday & Resampling =====
def get_intraday_data(symbol, resolution="3", target_date=None):
    if target_date is None:
        target_date = datetime.date.today() - datetime.timedelta(days=1)
    start = datetime.datetime.combine(target_date, datetime.time(9,15))
    end   = datetime.datetime.combine(target_date, datetime.time(15,30))
    data = {"symbol": symbol, "resolution": resolution, "date_format": "0",
            "range_from": int(start.timestamp()), "range_to": int(end.timestamp()), "cont_flag": "0"}
    try:
        response = fyers.history(data=data)
        candles = response.get("candles", [])
        if not candles:
            logging.warning("[INTRADAY] No candles returned")
            return pd.DataFrame(columns=["timestamp","open","high","low","close","volume"])
        return pd.DataFrame(candles, columns=["timestamp","open","high","low","close","volume"])
    except Exception as e:
        logging.error(f"[INTRADAY] Failed to fetch data: {e}")
        return pd.DataFrame(columns=["timestamp","open","high","low","close","volume"])


def get_today_15m_candles(candles_3m):
    if not isinstance(candles_3m.index, pd.DatetimeIndex):
        if "time" not in candles_3m.columns:
            logging.error("[ERROR] Missing 'time' column for resampling")
            return pd.DataFrame()
        candles_3m = candles_3m.copy()
        candles_3m["datetime"] = pd.to_datetime(candles_3m["time"])
        candles_3m = candles_3m.set_index("datetime")

    agg_dict = {"open":"first","high":"max","low":"min","close":"last"}
    if "volume" in candles_3m.columns:
        agg_dict["volume"] = "sum"

    df_15m_today = candles_3m.resample("15min").agg(agg_dict).dropna()

    for ts, row in df_15m_today.iterrows():
        logging.info(f"[15M CANDLE BUILT TODAY] {ts} | O={row.open:.2f} H={row.high:.2f} "
                     f"L={row.low:.2f} C={row.close:.2f}")

    # logging.info(f"[SUMMARY] Built {len(df_15m_today)} live 15m candles so far")
    return df_15m_today


# ===== Indicators =====

def calculate_ema(series, period=20):
    if series is None or len(series) == 0:
        logging.error("[EMA] Empty or invalid series")
        return pd.Series(dtype=float)
    return series.ewm(span=period, adjust=False).mean()


def calculate_cci(df, period=20):
    if not {"high","low","close"}.issubset(df.columns):
        logging.error("[CCI] Missing required columns")
        return pd.Series(dtype=float)

    tp = (df["high"] + df["low"] + df["close"]) / 3
    ma = tp.rolling(period).mean()
    md = tp.rolling(period).apply(lambda x: np.mean(np.abs(x - x.mean())), raw=True)
    md = md.replace(0, np.nan)  # avoid division by zero
    cci = (tp - ma) / (0.015 * md)

    return cci


def calculate_supertrend(df, period=8, multiplier=3):
    if not {"high","low","close"}.issubset(df.columns):
        logging.error("[SUPERTREND] Missing required columns")
        return pd.Series(index=df.index, dtype="object")

    hl2 = (df['high'] + df['low']) / 2
    atr = calculate_atr(df, period)

    if atr is None:
        logging.error("[SUPERTREND] ATR not available")
        return pd.Series(index=df.index, dtype="object")

    upperband = hl2 + (multiplier * atr)
    lowerband = hl2 - (multiplier * atr)

    supertrend = pd.Series(index=df.index, dtype="object")
    trend = None
    for i in range(period, len(df)):
        if df['close'].iloc[i] > upperband.iloc[i-1] if hasattr(upperband, "iloc") else df['close'].iloc[i] > upperband:
            trend = "BULLISH"
        elif df['close'].iloc[i] < lowerband.iloc[i-1] if hasattr(lowerband, "iloc") else df['close'].iloc[i] < lowerband:
            trend = "BEARISH"
        supertrend.iloc[i] = trend

    return supertrend


def calculate_adx(df, period=14):
    if not {"high","low","close"}.issubset(df.columns):
        logging.error("[ADX] Missing required columns")
        return pd.Series(dtype=float)

    df = df.copy()  # avoid polluting input
    df['TR'] = df[['high','low','close']].apply(
        lambda x: max(x['high']-x['low'],
                      abs(x['high']-x['close']),
                      abs(x['low']-x['close'])), axis=1)

    df['+DM'] = df['high'].diff()
    df['-DM'] = df['low'].diff().abs()
    df['+DM'] = np.where((df['+DM'] > df['-DM']) & (df['+DM'] > 0), df['+DM'], 0.0)
    df['-DM'] = np.where((df['-DM'] > df['+DM']) & (df['-DM'] > 0), df['-DM'], 0.0)

    df['TR14'] = df['TR'].rolling(window=period).sum()
    df['+DM14'] = df['+DM'].rolling(window=period).sum()
    df['-DM14'] = df['-DM'].rolling(window=period).sum()

    df['+DI14'] = 100 * (df['+DM14'] / df['TR14'].replace(0, np.nan))
    df['-DI14'] = 100 * (df['-DM14'] / df['TR14'].replace(0, np.nan))

    df['DX'] = (abs(df['+DI14'] - df['-DI14']) / (df['+DI14'] + df['-DI14']).replace(0, np.nan)) * 100
    adx = df['DX'].rolling(window=period).mean()

    return adx


def get_recent_atr_history(db, n=30):
    """Fetch last n days of ATR values from DB or cache."""
    atr_values = []
    try:
        rows = db.conn.execute(
            "SELECT atr FROM daily_atr ORDER BY date DESC LIMIT ?", (n,)
        )
        atr_values = [row[0] for row in rows]
    except Exception as e:
        logging.warning(f"[ATR HISTORY] Failed to fetch: {e}")
    return atr_values if atr_values else [ATR_VALUE, 120]  # fallback


# ===== Bias Check =====
def check_bias(hist_data_15m, daily_atr=None, atr_threshold=15, adx_threshold=20, min_candles=20):
    if len(hist_data_15m) < min_candles:
        logging.warning("[BIAS CHECK] Not enough candles to determine bias")
        return "NEUTRAL"

    if not {"high","low","close"}.issubset(hist_data_15m.columns):
        logging.error("[BIAS CHECK] Missing required columns")
        return "NEUTRAL"

    # ATR
    atr_val = calculate_atr(hist_data_15m)
    atr_ok = atr_val is not None and atr_val > atr_threshold

    # ADX
    try:
        adx_val = calculate_adx(hist_data_15m).iloc[-1]
    except Exception:
        adx_val = None
    adx_ok = adx_val is not None and adx_val > adx_threshold

    # Supertrend
    supertrend_series = calculate_supertrend(hist_data_15m)
    supertrend_bias = supertrend_series.iloc[-1] if len(supertrend_series) else "NEUTRAL"

    # EMA
    ema20 = calculate_ema(hist_data_15m['close'], period=20).iloc[-1]
    ema_bias = "BULLISH" if hist_data_15m['close'].iloc[-1] > ema20 else "BEARISH"

    # CCI
    try:
        cci_val = calculate_cci(hist_data_15m).iloc[-1]
    except Exception:
        cci_val = 0
    cci_bias = "BULLISH" if cci_val > 50 else "BEARISH" if cci_val < -50 else "NEUTRAL"

    # Voting system
    votes = [supertrend_bias, ema_bias, cci_bias]
    bullish_votes = votes.count("BULLISH")
    bearish_votes = votes.count("BEARISH")

    if bullish_votes > bearish_votes and atr_ok and adx_ok:
        bias = "BULLISH"
    elif bearish_votes > bullish_votes and atr_ok and adx_ok:
        bias = "BEARISH"
    else:
        bias = "NEUTRAL"

    logging.info(f"[BIAS CHECK] ATR={atr_val:.2f if atr_val else 'NA'} ADX={adx_val:.2f if adx_val else 'NA'} "
                 f"Supertrend={supertrend_bias} EMA={ema_bias} CCI={cci_bias} => Bias={bias}")
    return bias

# Alias for compatibility
bias_check = check_bias