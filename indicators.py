# ===== indicators.py part1 =====
# import logging
# import pandas as pd
# import numpy as np
# import pendulum as dt
# import datetime

# from config import time_zone, ATR_VALUE
# from setup import spot_price
# from tickdb import TickDatabase
# tick_db = TickDatabase()
# from tickdb import tick_db
# from signals import detect_signal, evaluate_candle

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
    if len(df_) < period + 1:
        return None
    hl = df_["high"] - df_["low"]
    hc = (df_["high"] - df_["close"].shift()).abs()
    lc = (df_["low"] - df_["close"].shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    return float(tr.rolling(period).mean().iloc[-1])

def resolve_atr(candles_3m_, daily_atr_):
    atr_3m = calculate_atr(candles_3m_)
    if atr_3m is not None:
        return atr_3m, "ATR_3M"
    if daily_atr_ is not None:
        return daily_atr_, "ATR_DAILY"
    if len(candles_3m_) >= 2:
        atr_boot = candles_3m_["high"].max() - candles_3m_["low"].min()
        logging.warning(f"[BOOTSTRAP ATR] using range={atr_boot:.2f}")
        return atr_boot, "ATR_BOOTSTRAP"
    return None, None

def daily_atr(df_daily, period=14):
    if len(df_daily) < period + 1:
        return None
    hl = df_daily["high"] - df_daily["low"]
    hc = (df_daily["high"] - df_daily["close"].shift()).abs()
    lc = (df_daily["low"] - df_daily["close"].shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    return float(tr.rolling(period).mean().iloc[-1])

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
        logging.error("[EMA] Empty or invalid series")
        return pd.Series(dtype=float)
    if not isinstance(series, pd.Series):
        series = pd.Series(series)
    series = series.dropna()  # ✅ drop NaNs for cleaner EMA
    return series.ewm(span=period, adjust=False).mean()


def calculate_cci(df, period=20):
    """Commodity Channel Index (CCI)."""
    if not {"high","low","close"}.issubset(df.columns):
        logging.error("[CCI] Missing required columns")
        return pd.Series(dtype=float)
    tp = (df["high"] + df["low"] + df["close"]) / 3
    ma = tp.rolling(period).mean()
    # ✅ vectorized mean deviation instead of lambda
    md = (tp.rolling(period).apply(lambda x: np.mean(np.abs(x - x.mean())), raw=True))
    md = md.replace(0, np.nan)
    cci = (tp - ma) / (0.015 * md)
    return cci


def calculate_supertrend(df, period=8, multiplier=3):
    """Supertrend indicator."""
    if not {"high","low","close"}.issubset(df.columns):
        logging.error("[SUPERTREND] Missing required columns")
        return pd.Series(index=df.index, dtype="object")

    hl2 = (df['high'] + df['low']) / 2
    atr = calculate_atr(df, period)
    if atr is None or len(atr) == 0:
        logging.error("[SUPERTREND] ATR not available")
        return pd.Series(index=df.index, dtype="object")

    upperband = hl2 + (multiplier * atr)
    lowerband = hl2 - (multiplier * atr)

    supertrend = pd.Series(index=df.index, dtype="object")
    trend = "NEUTRAL"  # ✅ explicit initialization
    for i in range(period, len(df)):
        if df['close'].iloc[i] > upperband.iloc[i]:
            trend = "BULLISH"
        elif df['close'].iloc[i] < lowerband.iloc[i]:
            trend = "BEARISH"
        supertrend.iloc[i] = trend
    return supertrend


def calculate_adx(df, period=14):
    """Average Directional Index (ADX)."""
    if not {"high","low","close"}.issubset(df.columns):
        logging.error("[ADX] Missing required columns")
        return pd.Series(dtype=float)

    df = df.copy()

    # ✅ vectorized TR calculation
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
    return adx


# =================== Bias Check ===================================================

def check_bias(hist_data_15m, daily_atr=None, atr_threshold=15, adx_threshold=20, min_candles=20):
    if len(hist_data_15m) < min_candles:
        logging.warning("[BIAS CHECK] Not enough candles to determine bias")
        return "NEUTRAL"

    if not {"high","low","close"}.issubset(hist_data_15m.columns):
        logging.error("[BIAS CHECK] Missing required columns")
        return "NEUTRAL"

    # ATR
    try:
        atr_val = calculate_atr(hist_data_15m).iloc[-1]
    except Exception:
        atr_val = None
    atr_ok = atr_val is not None and atr_val > atr_threshold
    if daily_atr is not None:  # ✅ optional daily ATR override
        atr_ok = daily_atr > atr_threshold

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

    # ✅ Fixed logging
    atr_str = f"{atr_val:.2f}" if atr_val is not None else "NA"
    adx_str = f"{adx_val:.2f}" if adx_val is not None else "NA"
    logging.info(
        f"[BIAS CHECK] ATR={atr_str} ADX={adx_str} "
        f"Supertrend={supertrend_bias} EMA={ema_bias} CCI={cci_bias} => Bias={bias}"
    )
    return bias

bias_check = check_bias


# ============= Ocillator Filters ===============

def williams_r(candles, period=14):
    """Calculate Williams %R."""
    if len(candles) < period:
        logging.warning("[W%R] Not enough candles")
        return np.nan
    highest_high = candles['high'].tail(period).max()
    lowest_low   = candles['low'].tail(period).min()
    last_close   = candles['close'].iloc[-1]
    if highest_high == lowest_low:
        logging.warning("[W%R] Invalid range (high == low)")
        return np.nan
    return ((highest_high - last_close) / (highest_high - lowest_low)) * -100


def cci_indicator(candles, period=20):
    """Calculate Commodity Channel Index (CCI)."""
    if len(candles) < period:
        logging.warning("[CCI] Not enough candles")
        return np.nan
    tp = (candles['high'] + candles['low'] + candles['close']) / 3
    ma = tp.tail(period).mean()
    md = (tp.tail(period) - ma).abs().mean()
    if md == 0:
        logging.warning("[CCI] Mean deviation = 0")
        return np.nan
    return (tp.iloc[-1] - ma) / (0.015 * md)


def oscillator_entry_filter(side, candles_3m):
    """Block entries if oscillators show exhaustion on 3m timeframe."""
    if len(candles_3m) < 20:
        logging.warning("[ENTRY FILTER][OSC] Not enough candles, allowing entry")
        return True
    wr  = williams_r(candles_3m)
    cci = cci_indicator(candles_3m)
    if side == "CALL" and (wr >= -10 or cci >= 200):
        logging.info(f"[ENTRY BLOCKED][OSC] CALL skipped (W%R={wr:.2f}, CCI={cci:.2f})")
        return False
    if side == "PUT" and (wr <= -90 or cci <= -200):
        logging.info(f"[ENTRY BLOCKED][OSC] PUT skipped (W%R={wr:.2f}, CCI={cci:.2f})")
        return False
    return True


def oscillator_exit_trigger(side, candles_15m):
    """Trigger exits if oscillators hit extremes on 15m timeframe."""
    if len(candles_15m) < 20:
        logging.warning("[EXIT FILTER][OSC] Not enough candles")
        return False, ""

    wr  = williams_r(candles_15m)
    cci = cci_indicator(candles_15m)

    if side == "CALL":
        if wr >= -5:
            logging.info(f"[EXIT SIGNAL][OSC] CALL exit W%R={wr:.2f}")
            return True, "W%R near 0 (overbought extreme)"
        if cci >= 200:
            logging.info(f"[EXIT SIGNAL][OSC] CALL exit CCI={cci:.2f}")
            return True, f"CCI={cci:.2f} >= 200 (bullish extreme)"

    elif side == "PUT":
        if wr <= -95:
            logging.info(f"[EXIT SIGNAL][OSC] PUT exit W%R={wr:.2f}")
            return True, "W%R near -100 (oversold extreme)"
        if cci <= -200:
            logging.info(f"[EXIT SIGNAL][OSC] PUT exit CCI={cci:.2f}")
            return True, f"CCI={cci:.2f} <= -200 (bearish extreme)"

    return False, ""