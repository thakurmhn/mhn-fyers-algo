# ===== indicators.py part1 =====

import logging
import pandas as pd
import numpy as np
import pendulum as dt
import datetime

from config import time_zone, ATR_VALUE
from setup import spot_price
from tickdb import TickDatabase
tick_db = TickDatabase()
from tickdb import tick_db

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
CYAN    = "\033[96m"
# ===========================================================


# ===== Pivot Calculations =====

def calculate_cpr(prev_high, prev_low, prev_close):
    pivot = (prev_high + prev_low + prev_close) / 3
    bc = (prev_high + prev_low) / 2
    tc = (pivot - bc) + pivot
    if round(tc, 2) == round(bc, 2):
        tc = pivot + 0.0005 * pivot
        bc = pivot - 0.0005 * pivot
    return {"pivot": round(pivot, 2), "bc": round(bc, 2), "tc": round(tc, 2)}

def calculate_traditional_pivots(prev_high, prev_low, prev_close):
    pivot = (prev_high + prev_low + prev_close) / 3
    r1 = (2 * pivot) - prev_low
    s1 = (2 * pivot) - prev_high
    r2 = pivot + (prev_high - prev_low)
    s2 = pivot - (prev_high - prev_low)
    if prev_high == prev_low:
        r1 = pivot + 0.0005 * pivot
        s1 = pivot - 0.0005 * pivot
        r2 = pivot + 0.001 * pivot
        s2 = pivot - 0.001 * pivot
    return {"pivot": round(pivot, 2), "r1": round(r1, 2), "s1": round(s1, 2),
            "r2": round(r2, 2), "s2": round(s2, 2)}

def calculate_camarilla_pivots(prev_high, prev_low, prev_close):
    range_val = prev_high - prev_low
    if range_val == 0:
        range_val = 0.001 * prev_close
    r3 = prev_close + (range_val * 1.1 / 4)
    r4 = prev_close + (range_val * 1.1 / 2)
    s3 = prev_close - (range_val * 1.1 / 4)
    s4 = prev_close - (range_val * 1.1 / 2)
    return {"r3": round(r3, 2), "r4": round(r4, 2),
            "s3": round(s3, 2), "s4": round(s4, 2)}


# # ===== Pivot Calculations =====
# def calculate_cpr(high, low, close):
#     pivot = (high + low + close) / 3
#     bc = (high + low) / 2
#     tc = (pivot - bc) + pivot
#     return {"pivot": round(pivot, 2), "bc": round(bc, 2), "tc": round(tc, 2)}

# def calculate_traditional_pivots(high, low, close):
#     pivot = (high + low + close) / 3
#     r1 = (2 * pivot) - low
#     s1 = (2 * pivot) - high
#     r2 = pivot + (high - low)
#     s2 = pivot - (high - low)
#     return {"pivot": round(pivot, 2), "r1": round(r1, 2), "s1": round(s1, 2),
#             "r2": round(r2, 2), "s2": round(s2, 2)}

# def calculate_camarilla_pivots(high, low, close):
#     range_val = high - low
#     r3 = close + (range_val * 1.1 / 4)
#     r4 = close + (range_val * 1.1 / 2)
#     s3 = close - (range_val * 1.1 / 4)
#     s4 = close - (range_val * 1.1 / 2)
#     return {"r3": round(r3, 2), "r4": round(r4, 2),
#             "s3": round(s3, 2), "s4": round(s4, 2)}

# # ===== ATR =====

def calculate_atr(candles: pd.DataFrame, period: int = 14):
    """
    Calculate ATR using a rolling window of `period` candles.
    Returns the latest ATR value.
    """
    highs = candles['high'].astype(float)
    lows = candles['low'].astype(float)
    closes = candles['close'].astype(float)

    # True Range (TR)
    prev_close = closes.shift(1)
    tr = pd.concat([
        highs - lows,
        (highs - prev_close).abs(),
        (lows - prev_close).abs()
    ], axis=1).max(axis=1)

    # Average True Range (ATR)
    atr = tr.rolling(period).mean()

    return atr.iloc[-1] if not atr.empty else None


def resolve_atr(candles_3m, daily_atr=None, period=14):
    """
    Resolve ATR value for signal detection.
    - If daily_atr is provided, use it.
    - Otherwise, calculate rolling ATR from candles_3m up to latest candle.
    """
    if daily_atr is not None:
        try:
            return float(daily_atr), "daily override"
        except Exception:
            return None, "daily override invalid"

    if candles_3m is not None and isinstance(candles_3m, pd.DataFrame):
        atr_val = calculate_atr(candles_3m, period=period)
        if atr_val is not None:
            logging.debug(f"[ATR CALC] period={period} value={atr_val:.2f}")
            return atr_val, "calculated"
        else:
            return None, "calculation failed"

    return None, "unavailable"

def daily_atr(df_daily, period=7):
    """
    Daily ATR calculation with NaN handling.
    """
    if df_daily is None or len(df_daily) < period + 1:
        return None

    hl = df_daily["high"] - df_daily["low"]
    hc = (df_daily["high"] - df_daily["close"].shift()).abs()
    lc = (df_daily["low"] - df_daily["close"].shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)

    atr_series = tr.rolling(period).mean().dropna()
    if atr_series.empty:
        return None
    val = atr_series.iloc[-1]
    return None if pd.isna(val) else float(val)

# ===== Momentum =====
def momentum_ok(candles, side):
    if len(candles) < 2:
        return False, 0
    last, prev = candles.iloc[-1], candles.iloc[-2]
    momentum = last.close - prev.close
    ok = momentum > 0 if side == "CALL" else momentum < 0
    return ok, momentum

# ===== Indicators =====
def calculate_ema(df, period=20, column="close"):
    """Exponential Moving Average (EMA) from a DataFrame column."""
    if df is None or df.empty or column not in df.columns:
        logging.error("[EMA] Empty or invalid DataFrame")
        return pd.Series(dtype=float, index=df.index if df is not None else None)

    series = df[column].dropna()
    return series.ewm(span=period, adjust=False).mean()

def calculate_adx(df, period=14):
    """Calculate ADX (Average Directional Index) from a DataFrame."""
    if df is None or df.empty or not {"high","low","close"}.issubset(df.columns):
        logging.warning("[ADX] No data")
        return pd.Series(dtype=float, index=df.index if df is not None else None)

    high, low, close = df["high"], df["low"], df["close"]

    plus_dm = high.diff()
    minus_dm = low.diff().abs()
    plus_dm[plus_dm < 0] = 0
    minus_dm[minus_dm < 0] = 0

    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs()
    ], axis=1).max(axis=1)

    atr = tr.rolling(period).mean()
    plus_di = 100 * (plus_dm.rolling(period).mean() / atr)
    minus_di = 100 * (minus_dm.rolling(period).mean() / atr)
    adx = 100 * (abs(plus_di - minus_di) / (plus_di + minus_di).replace(0, np.nan)).rolling(period).mean()

    return adx

def calculate_cci(df, period=20):
    """Commodity Channel Index (CCI) as a Series."""
    if df is None or df.empty or not {"high","low","close"}.issubset(df.columns):
        logging.warning("[CCI] No data")
        return pd.Series(dtype=float, index=df.index if df is not None else None)

    tp = (df['high'] + df['low'] + df['close']) / 3
    ma = tp.rolling(period).mean()
    md = (tp - ma).abs().rolling(period).mean()

    cci = (tp - ma) / (0.015 * md)
    return cci


def cci_indicator(df, period=20):
    """Return the latest CCI value for bias checks."""
    cci_series = calculate_cci(df, period=period)
    if cci_series is None or cci_series.empty:
        logging.warning("[CCI INDICATOR] No CCI available")
        return np.nan

    latest_val = cci_series.iloc[-1]
    logging.debug(f"[CCI INDICATOR] Latest CCI={latest_val:.2f}")
    return latest_val

def ema_bias(df, period=20):
    """EMA bias: compares last close vs EMA."""
    if df is None or df.empty or "close" not in df.columns:
        return "NEUTRAL"
    ema = df["close"].ewm(span=period).mean()
    if ema.empty or pd.isna(ema.iloc[-1]):
        return "NEUTRAL"
    last_close = df["close"].iloc[-1]
    return "BULLISH" if last_close > ema.iloc[-1] else "BEARISH"


# def cci_bias(df, period=20, threshold=100):
#     tp = (df['high'] + df['low'] + df['close']) / 3
#     ma = tp.rolling(period).mean()
#     md = (tp - ma).abs().rolling(period).mean()
#     cci = (tp - ma) / (0.015 * md)
#     last_cci = cci.iloc[-1]
#     if last_cci > threshold:
#         return "BULLISH"
#     elif last_cci < -threshold:
#         return "BEARISH"
#     else:
#         return "NEUTRAL"

def cci_bias(df, period=20, threshold=60):
    """CCI bias: checks last CCI value against thresholds."""
    if df is None or df.empty or not {"high","low","close"}.issubset(df.columns):
        return "NEUTRAL"

    tp = (df["high"] + df["low"] + df["close"]) / 3
    ma = tp.rolling(period).mean()
    md = (tp - ma).abs().rolling(period).mean()
    cci = (tp - ma) / (0.015 * md)

    if cci.empty or pd.isna(cci.iloc[-1]):
        return "NEUTRAL"

    last_cci = cci.iloc[-1]
    if last_cci > threshold:
        return "BULLISH"
    elif last_cci < -threshold:
        return "BEARISH"
    else:
        return "NEUTRAL"


def supertrend(df, atr_val=None, period=7, multiplier=3, slope_lookback=5):
    """
    Compute Supertrend bias and slope.
    - df: DataFrame with 'high','low','close'
    - atr_val: optional float ATR override
    - period: ATR period if computing internally
    - multiplier: Supertrend multiplier
    - slope_lookback: number of candles to measure slope
    Returns dict: {"bias": "BULLISH/BEARISH/NEUTRAL", "slope": "UP/DOWN/FLAT"}
    """

    if df is None or df.empty:
        logging.warning("[SUPERTREND] No candles provided")
        return {"bias": "NEUTRAL", "slope": "FLAT"}

    # --- ATR resolution ---
    if atr_val is None or pd.isna(atr_val):
        high_low   = df['high'] - df['low']
        high_close = (df['high'] - df['close'].shift()).abs()
        low_close  = (df['low'] - df['close'].shift()).abs()
        tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        atr_series = tr.rolling(period).mean().dropna()
        if atr_series.empty:
            logging.warning("[SUPERTREND] ATR unavailable")
            return {"bias": "NEUTRAL", "slope": "FLAT"}
        atr_val = float(atr_series.iloc[-1])
        logging.debug(f"[SUPERTREND] ATR calculated={atr_val:.2f}")
    else:
        try:
            atr_val = float(atr_val)
            logging.debug(f"[SUPERTREND] ATR override={atr_val:.2f}")
        except Exception:
            logging.error("[SUPERTREND] Invalid ATR override")
            return {"bias": "NEUTRAL", "slope": "FLAT"}

    # --- Supertrend bands ---
    hl2 = (df['high'] + df['low']) / 2
    upperband = hl2 + (multiplier * atr_val)
    lowerband = hl2 - (multiplier * atr_val)

    # --- Bias decision ---
    last = df.iloc[-1]
    if last.close > upperband.iloc[-1]:
        bias = "BULLISH"
    elif last.close < lowerband.iloc[-1]:
        bias = "BEARISH"
    else:
        bias = "NEUTRAL"

    # --- Slope detection ---
    if len(hl2) >= slope_lookback:
        slope_val = hl2.iloc[-1] - hl2.iloc[-slope_lookback]
        threshold = 0.2 * atr_val  # avoid noise
        if abs(slope_val) <= threshold:
            slope = "FLAT"
        elif slope_val > 0:
            slope = "UP"
        else:
            slope = "DOWN"
    else:
        slope = "FLAT"

    logging.info(f"[SUPERTREND] Bias={bias} Slope={slope}")
    return {"bias": bias, "slope": slope}


def calculate_adx(df, period=14):
    """Average Directional Index (ADX) that always returns a Series."""
    if not {"high","low","close"}.issubset(df.columns):
        logging.error("[ADX] Missing required columns")
        return pd.Series(dtype=float)

    df = df.copy()

    high_low = df['high'] - df['low']
    high_close = (df['high'] - df['close'].shift()).abs()
    low_close = (df['low'] - df['close'].shift()).abs()
    df['TR'] = np.maximum.reduce([high_low, high_close, low_close])

    df['+DM'] = df['high'].diff()
    df['-DM'] = df['low'].diff().abs()
    df['+DM'] = np.where((df['+DM'] > df['-DM']) & (df['+DM'] > 0), df['+DM'], 0.0)
    df['-DM'] = np.where((df['-DM'] > df['+DM']) & (df['-DM'] > 0), df['-DM'], 0.0)

    df['TR14'] = df['TR'].rolling(window=period).sum()
    df['+DM14'] = df['+DM'].rolling(window=period).sum()
    df['-DM14'] = df['-DM'].rolling(window=period).sum()

    df['+DI14'] = 100 * (df['+DM14'] / df['TR14'].replace(0, np.nan))
    df['-DI14'] = 100 * (df['-DM14'] / df['TR14'].replace(0, np.nan))

    df['DX'] = (abs(df['+DI14'] - df['-DI14']) /
                (df['+DI14'] + df['-DI14']).replace(0, np.nan)) * 100

    adx = df['DX'].rolling(window=period).mean()

    # Ensure ADX is always a Series
    if isinstance(adx, (float, int)):
        adx = pd.Series([adx], index=df.index[-1:])
    elif not isinstance(adx, pd.Series):
        adx = pd.Series(dtype=float)

    return adx

def adx_bias(df, period=14, threshold=20):
    """ADX bias: compares +DI vs -DI with ADX strength check."""
    if df is None or df.empty or not {"high","low","close"}.issubset(df.columns):
        logging.warning("[ADX BIAS] No data")
        return "NEUTRAL"

    high, low, close = df["high"], df["low"], df["close"]

    plus_dm = high.diff()
    minus_dm = low.diff().abs()
    plus_dm[plus_dm < 0] = 0
    minus_dm[minus_dm < 0] = 0

    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs()
    ], axis=1).max(axis=1)

    atr = tr.rolling(period).mean().dropna()
    if atr.empty:
        logging.warning("[ADX BIAS] ATR unavailable")
        return "NEUTRAL"

    plus_di = 100 * (plus_dm.rolling(period).mean() / atr)
    minus_di = 100 * (minus_dm.rolling(period).mean() / atr)
    adx = 100 * (abs(plus_di - minus_di) / (plus_di + minus_di).replace(0, np.nan)).rolling(period).mean()

    if adx.empty or pd.isna(adx.iloc[-1]):
        logging.warning("[ADX BIAS] ADX unavailable")
        return "NEUTRAL"

    adx_val = adx.iloc[-1]
    logging.debug(f"[ADX BIAS] +DI={plus_di.iloc[-1]:.2f}, -DI={minus_di.iloc[-1]:.2f}, ADX={adx_val:.2f}")

    if adx_val < threshold:
        return "NEUTRAL"
    return "BULLISH" if plus_di.iloc[-1] > minus_di.iloc[-1] else "BEARISH"

def check_bias(df_15m, daily_atr=None):
    if df_15m is None or df_15m.empty:
        logging.warning("[BIAS] No 15m candles available")
        return None

    row = df_15m.iloc[-1]

    # Supertrend normalization
    st_val = row.get("supertrend", "NEUTRAL")
    if isinstance(st_val, (int, float)):
        st_val = "BULLISH" if st_val > 0 else "BEARISH"
    elif str(st_val).lower() == "up":
        st_val = "BULLISH"
    elif str(st_val).lower() == "down":
        st_val = "BEARISH"
    else:
        st_val = "NEUTRAL"

    ema_val = "BULLISH" if row["ema20"] > row["ema50"] else "BEARISH"

    adx_val = row.get("adx14", None)
    if adx_val is None or pd.isna(adx_val):
        adx_bias = "NEUTRAL"
    elif adx_val > 25:
        adx_bias = ema_val
    elif adx_val < 15:
        adx_bias = "BEARISH" if ema_val == "BEARISH" else "NEUTRAL"
    else:
        adx_bias = "NEUTRAL"

    cci_val = row.get("cci20", None)
    if cci_val is None or pd.isna(cci_val):
        cci_bias = "NEUTRAL"
    elif cci_val > 50:
        cci_bias = "BULLISH"
    elif cci_val < -50:
        cci_bias = "BEARISH"
    else:
        cci_bias = "NEUTRAL"

    scores = {"BULLISH": 0, "BEARISH": 0}
    scores[ema_val] += 2
    if adx_bias in scores:
        scores[adx_bias] += 2 if adx_val and adx_val > 25 else 1
    if cci_bias in scores:
        scores[cci_bias] += 1
    if st_val in ("BULLISH", "BEARISH"):
        scores[st_val] += 2

    # ATR contribution
    if daily_atr is not None and row["close"]:
        atr_ratio = daily_atr / row["close"]
        if atr_ratio > 0.005:  # >0.5% volatility
            scores[ema_val] += 1

    logging.info(
        f"[BIAS CHECK] ATR={daily_atr if daily_atr else 'NA'} "
        f"Supertrend={st_val} EMA={ema_val} ADX={adx_bias}({adx_val}) CCI={cci_bias}({cci_val})"
    )
    logging.info(f"[BIAS SCORES] BULLISH={scores['BULLISH']} BEARISH={scores['BEARISH']}")

    if scores["BULLISH"] > scores["BEARISH"]:
        logging.info("[BIAS RESULT] BULLISH (weighted)")
        return "BULLISH"
    elif scores["BEARISH"] > scores["BULLISH"]:
        logging.info("[BIAS RESULT] BEARISH (weighted)")
        return "BEARISH"
    else:
        logging.info("[BIAS RESULT] NEUTRAL (tie)")
        return "NEUTRAL"
    

# def cci_indicator(candles, period=20):
#     if len(candles) < period:
#         logging.warning("[CCI] Not enough candles")
#         return np.nan
#     tp = (candles['high'] + candles['low'] + candles['close']) / 3
#     ma = tp.tail(period).mean()
#     md = (tp.tail(period) - ma).abs().mean()
#     if md == 0:
#         logging.warning("[CCI] Mean deviation = 0")
#         return np.nan
#     cci = (tp.iloc[-1] - ma) / (0.015 * md)
#     logging.debug(f"[CCI] tp={tp.iloc[-1]:.2f}, ma={ma:.2f}, md={md:.2f}, CCI={cci:.2f}")
#     return cci

def williams_r(candles, period=14):
    """
    Compute Williams %R oscillator.
    - candles: DataFrame with 'high','low','close'
    - period: lookback period (default=14)
    Returns float W%R value in range [-100, 0].
    """

    if candles is None or candles.empty or len(candles) < period:
        logging.warning("[W%R] Not enough candles")
        return np.nan

    # --- Highest high and lowest low over lookback ---
    highest_high = candles['high'].tail(period).max()
    lowest_low   = candles['low'].tail(period).min()
    last_close   = candles['close'].iloc[-1]

    if highest_high == lowest_low:
        logging.warning("[W%R] Invalid range (high == low)")
        return np.nan

    # --- Williams %R formula ---
    wr = ((highest_high - last_close) / (highest_high - lowest_low)) * -100

    logging.debug(
        f"[W%R] high={highest_high:.2f}, low={lowest_low:.2f}, "
        f"close={last_close:.2f}, W%R={wr:.2f}"
    )
    return wr

def oscillator_entry_filter(side, candles_3m):
    if len(candles_3m) < 20:
        logging.warning(f"{CYAN}[ENTRY FILTER][OSC] Not enough candles, allowing entry{RESET}")
        return True
    wr  = williams_r(candles_3m)
    cci = cci_indicator(candles_3m)
    if np.isnan(wr) or np.isnan(cci):
        logging.warning(f"{CYAN}[ENTRY FILTER][OSC] Oscillator NaN, allowing entry{RESET}")
        return True
    if side == "CALL" and (wr >= -10 or cci >= 200):
        logging.info(f"{CYAN}[ENTRY BLOCKED][OSC] CALL skipped (W%R={wr:.2f}, CCI={cci:.2f}){RESET}")
        return False
    if side == "PUT" and (wr <= -90 or cci <= -200):
        logging.info(f"{CYAN}[ENTRY BLOCKED][OSC] PUT skipped (W%R={wr:.2f}, CCI={cci:.2f}){RESET}")
        return False
    return True


def oscillator_exit_trigger(side, candles_15m):
    if len(candles_15m) < 20:
        logging.warning("[EXIT FILTER][OSC] Not enough candles")
        return False, ""
    wr  = williams_r(candles_15m)
    cci = cci_indicator(candles_15m)
    if np.isnan(wr) or np.isnan(cci):
        logging.warning("[EXIT FILTER][OSC] Oscillator NaN, no exit")
        return False, ""
    if side == "CALL":
        if wr >= -5:
            logging.info(f"{YELLOW}[EXIT SIGNAL][OSC] CALL exit W%R={wr:.2f}{RESET}")
            return True, "W%R near 0 (overbought extreme)"
        if cci >= 200:
            logging.info(f"{YELLOW}[EXIT SIGNAL][OSC] CALL exit CCI={cci:.2f}{RESET}")
            return True, f"CCI={cci:.2f} >= 200 (bullish extreme)"
    elif side == "PUT":
        if wr <= -95:
            logging.info(f"{YELLOW}[EXIT SIGNAL][OSC] PUT exit W%R={wr:.2f}{RESET}")
            return True, "W%R near -100 (oversold extreme)"
        if cci <= -200:
            logging.info(f"{YELLOW}[EXIT SIGNAL][OSC] PUT exit CCI={cci:.2f}{RESET}")
            return True, f"CCI={cci:.2f} <= -200 (bearish extreme)"
    return False, ""