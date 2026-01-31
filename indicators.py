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

def calculate_atr(df, period=14):
    """
    Calculate Average True Range (ATR) from a DataFrame with
    'high', 'low', 'close' columns.
    Returns a float ATR value or None if unavailable.
    """
    if df is None or df.empty:
        return None

    high_low   = df['high'] - df['low']
    high_close = (df['high'] - df['close'].shift()).abs()
    low_close  = (df['low'] - df['close'].shift()).abs()

    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    atr_series = tr.rolling(period).mean().dropna()

    if atr_series.empty:
        return None
    val = atr_series.iloc[-1]
    return None if pd.isna(val) else float(val)


def resolve_atr(candles_3m, daily_atr=None, period=14):
    """
    Resolve ATR value for signal detection.
    - If daily_atr is provided, use it (force float).
    - Otherwise, calculate from candles_3m.
    Always return (float atr, source string).
    """
    if daily_atr is not None:
        try:
            return float(daily_atr), "daily override"
        except Exception:
            return None, "daily override invalid"

    if candles_3m is not None and isinstance(candles_3m, pd.DataFrame):
        atr_val = calculate_atr(candles_3m, period=period)
        if atr_val is not None:
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
def calculate_ema(series, period=20):
    """Exponential Moving Average (EMA)."""
    if series is None or len(series) == 0:
        logging.error(f"{CYAN}[EMA] Empty or invalid series{RESET}")
        return pd.Series(dtype=float)
    if not isinstance(series, pd.Series):
        series = pd.Series(series)
    series = series.dropna()  # ✅ drop NaNs for cleaner EMA
    return series.ewm(span=period, adjust=False).mean()


def calculate_cci(df, period=20):
    """Commodity Channel Index (CCI)."""
    if not {"high","low","close"}.issubset(df.columns):
        logging.error(f"{CYAN}[CCI] Missing required columns{RESET}")
        return pd.Series(dtype=float)
    tp = (df["high"] + df["low"] + df["close"]) / 3
    ma = tp.rolling(period).mean()
    # ✅ vectorized mean deviation instead of lambda
    md = (tp.rolling(period).apply(lambda x: np.mean(np.abs(x - x.mean())), raw=True))
    md = md.replace(0, np.nan)
    cci = (tp - ma) / (0.015 * md)
    return cci

def ema_bias(df, period=20):
    """EMA bias: compares last close vs EMA."""
    if df is None or df.empty or "close" not in df.columns:
        return "NEUTRAL"
    ema = df["close"].ewm(span=period).mean()
    if ema.empty or pd.isna(ema.iloc[-1]):
        return "NEUTRAL"
    last_close = df["close"].iloc[-1]
    return "BULLISH" if last_close > ema.iloc[-1] else "BEARISH"


def cci_bias(df, period=20, threshold=100):
    tp = (df['high'] + df['low'] + df['close']) / 3
    ma = tp.rolling(period).mean()
    md = (tp - ma).abs().rolling(period).mean()
    cci = (tp - ma) / (0.015 * md)
    last_cci = cci.iloc[-1]
    if last_cci > threshold:
        return "BULLISH"
    elif last_cci < -threshold:
        return "BEARISH"
    else:
        return "NEUTRAL"

def cci_bias(df, period=20, threshold=100):
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


def supertrend(df, atr_val=None, period=10, multiplier=3, slope_lookback=5):
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


def check_bias(candles_15m, daily_atr=None):
    """
    Evaluate bias using multiple indicators (Supertrend, EMA, ADX, CCI).
    Weighted scoring system:
      - Supertrend: 2 points
      - ADX: 2 points
      - EMA: 1 point
      - CCI: 1 point
    Returns "BULLISH", "BEARISH", or "NEUTRAL".
    """

    if candles_15m is None or candles_15m.empty:
        logging.warning("[BIAS] No 15m candles available")
        return None

    # --- Resolve ATR value ---
    if isinstance(daily_atr, (float, int)):
        atr_val = float(daily_atr)
    elif isinstance(daily_atr, pd.Series) and not daily_atr.empty:
        atr_val = float(daily_atr.iloc[-1])
    elif isinstance(daily_atr, pd.DataFrame) and not daily_atr.empty:
        atr_val = float(daily_atr.values[-1])
    else:
        atr_val = None
    if pd.isna(atr_val):
        atr_val = None

    # --- Compute indicators ---
    try:
        st_result = supertrend(candles_15m, atr_val)  # dict: {"bias":..., "slope":...}
        st_val = st_result.get("bias", "NEUTRAL")
        st_slope = st_result.get("slope", "FLAT")
    except Exception as e:
        logging.error(f"[BIAS DEBUG] Supertrend calc failed: {e}")
        st_val, st_slope = "NEUTRAL", "FLAT"

    try:
        ema_val = ema_bias(candles_15m)
    except Exception as e:
        logging.error(f"[BIAS DEBUG] EMA calc failed: {e}")
        ema_val = "NEUTRAL"

    try:
        adx_val = adx_bias(candles_15m)
    except Exception as e:
        logging.error(f"[BIAS DEBUG] ADX calc failed: {e}")
        adx_val = "NEUTRAL"

    try:
        cci_val = cci_bias(candles_15m)
    except Exception as e:
        logging.error(f"[BIAS DEBUG] CCI calc failed: {e}")
        cci_val = "NEUTRAL"

    # --- Weighted scoring ---
    weights = {"Supertrend": 2, "ADX": 2, "EMA": 1, "CCI": 1}
    scores = {"BULLISH": 0, "BEARISH": 0}

    for name, val in [("Supertrend", st_val), ("EMA", ema_val), ("ADX", adx_val), ("CCI", cci_val)]:
        if val in scores:
            scores[val] += weights[name]

    logging.info(
        f"{YELLOW}[BIAS CHECK] ATR={atr_val} ADX={adx_val}{RESET} "
        f"{YELLOW}Supertrend={st_val} (Slope={st_slope}) EMA={ema_val} CCI={cci_val}{RESET}"
    )
    logging.info(f"{YELLOW}[BIAS SCORES] BULLISH={scores['BULLISH']} BEARISH={scores['BEARISH']}{RESET}")

    if scores["BULLISH"] > scores["BEARISH"]:
        logging.info(f"{YELLOW}[BIAS RESULT] BULLISH (weighted){RESET}")
        return "BULLISH"
    elif scores["BEARISH"] > scores["BULLISH"]:
        logging.info(f"{YELLOW}[BIAS RESULT] BEARISH (weighted){RESET}")
        return "BEARISH"
    else:
        logging.info(f"{CYAN}[BIAS RESULT] NEUTRAL (weighted tie){RESET}")
        return "NEUTRAL"


def cci_indicator(candles, period=20):
    if len(candles) < period:
        logging.warning("[CCI] Not enough candles")
        return np.nan
    tp = (candles['high'] + candles['low'] + candles['close']) / 3
    ma = tp.tail(period).mean()
    md = (tp.tail(period) - ma).abs().mean()
    if md == 0:
        logging.warning("[CCI] Mean deviation = 0")
        return np.nan
    cci = (tp.iloc[-1] - ma) / (0.015 * md)
    logging.debug(f"[CCI] tp={tp.iloc[-1]:.2f}, ma={ma:.2f}, md={md:.2f}, CCI={cci:.2f}")
    return cci

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