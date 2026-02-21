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

def calculate_cci(df, period=20):
    """
    Commodity Channel Index (CCI) as a Series.

    Fixes vs original:
    - min_periods = period//4 (=5): produces values from bar 9 instead of bar 39.
      Eliminates NA at live-mode startup when < 39 bars exist.
    - md.replace(0, np.nan): eliminates division-by-zero when all prices identical
      (e.g. end-of-session flat close). Returns NaN instead of 0/0.
    """
    if df is None or df.empty or not {"high","low","close"}.issubset(df.columns):
        logging.warning("[CCI] No data")
        return pd.Series(dtype=float, index=df.index if df is not None else None)

    min_p = max(period // 4, 3)          # 5 for period=20 → valid from bar 9
    tp    = (df["high"] + df["low"] + df["close"]) / 3
    ma    = tp.rolling(period, min_periods=min_p).mean()
    md    = (tp - ma).abs().rolling(period, min_periods=min_p).mean()
    return  (tp - ma) / (0.015 * md.replace(0, np.nan))

def ema_bias(df, period=20):
    """EMA bias: compares last close vs EMA."""
    if df is None or df.empty or "close" not in df.columns:
        return "NEUTRAL"
    ema = df["close"].ewm(span=period).mean()
    if ema.empty or pd.isna(ema.iloc[-1]):
        return "NEUTRAL"
    last_close = df["close"].iloc[-1]
    return "BULLISH" if last_close > ema.iloc[-1] else "BEARISH"

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
    - atr_val: optional float ATR override (from resolve_atr)
    - period: ATR period if computing internally
    - multiplier: Supertrend multiplier
    - slope_lookback: number of candles to measure slope
    Returns tuple: (bias, slope)
    """

    if df is None or df.empty:
        logging.warning("[SUPERTREND] No candles provided")
        return "NEUTRAL", "FLAT"

    # --- ATR resolution ---
    if atr_val is None or pd.isna(atr_val):
        high_low   = df['high'] - df['low']
        high_close = (df['high'] - df['close'].shift()).abs()
        low_close  = (df['low'] - df['close'].shift()).abs()
        tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        atr_series = tr.rolling(period).mean().dropna()
        if atr_series.empty:
            logging.warning("[SUPERTREND] ATR unavailable (internal calc)")
            return "NEUTRAL", "FLAT"
        atr_val = float(atr_series.iloc[-1])
        logging.debug(f"[SUPERTREND] ATR calculated={atr_val:.2f}")
    else:
        try:
            atr_val = float(atr_val)
            logging.debug(f"[SUPERTREND] ATR override={atr_val:.2f}")
        except Exception:
            logging.error("[SUPERTREND] Invalid ATR override")
            return "NEUTRAL", "FLAT"

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

    logging.info(f"[SUPERTREND] Bias={bias} Slope={slope} (ATR={atr_val:.2f})")
    return bias, slope

def calculate_adx(df, period=14):
    """
    Average Directional Index (ADX) using Wilder EWM smoothing.

    Fixes vs original:
    - Wilder EWM (alpha=1/period) instead of rolling sum × 2.
      Original needed 2×14=28 bars before first non-NaN.
      Wilder EWM needs period+1=15 bars. Matches TradingView/Bloomberg.
      Eliminates NA at live-mode startup when < 28 bars exist.
    """
    if not {"high", "low", "close"}.issubset(df.columns):
        logging.error("[ADX] Missing required columns")
        return pd.Series(dtype=float, index=df.index)

    if len(df) < period + 1:
        return pd.Series([float("nan")] * len(df), index=df.index)

    df    = df.copy()
    alpha = 1.0 / period

    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift(1)).abs(),
        (df["low"]  - df["close"].shift(1)).abs(),
    ], axis=1).max(axis=1)

    up   = df["high"].diff()
    dn   = (-df["low"].diff())
    plus_dm  = pd.Series(np.where((up > dn) & (up > 0),   up,  0.0), index=df.index)
    minus_dm = pd.Series(np.where((dn > up) & (dn > 0),   dn,  0.0), index=df.index)

    tr_s     = tr.ewm(alpha=alpha,       adjust=False).mean()
    pdm_s    = plus_dm.ewm(alpha=alpha,  adjust=False).mean()
    mdm_s    = minus_dm.ewm(alpha=alpha, adjust=False).mean()

    plus_di  = 100 * pdm_s  / tr_s.replace(0, np.nan)
    minus_di = 100 * mdm_s  / tr_s.replace(0, np.nan)
    dx       = 100 * (plus_di - minus_di).abs() /                (plus_di + minus_di).replace(0, np.nan)
    adx      = dx.ewm(alpha=alpha, adjust=False).mean()
    adx.iloc[:period] = float("nan")   # blank first (period) rows — insufficient history
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


def compute_rsi(series, period=14):
    """
    Compute Relative Strength Index (RSI).
    series: pandas Series of closing prices
    period: lookback period (default 14)
    Returns: pandas Series of RSI values
    """
    delta = series.diff()

    # Separate gains and losses
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    # Use exponential moving average for smoothing
    avg_gain = gain.ewm(alpha=1/period, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1/period, min_periods=period).mean()

    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))

    return rsi