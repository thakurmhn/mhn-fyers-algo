# ===== indicators.py =====
import logging
import pandas as pd
import numpy as np
import pendulum as dt

from config import time_zone, profit_loss_point, hard_stop_points
from setup import spot_price, hist_data

# globals (must exist once in your script)
ticks_buffer = []
candles_3m = pd.DataFrame(columns=["open","high","low","close","time"])
current_3m_start = None

# ===========================================================
# ANSI COLORS for order logs
RESET   = "\033[0m"
GREEN   = "\033[92m"
YELLOW  = "\033[93m"
RED     = "\033[91m"
MAGENTA = "\033[95m"
GRAY    = "\033[90m"

#===========================================================


# def calculate_cpr(high, low, close):
#     pivot = (high + low + close) / 3
#     bc = (high + low) / 2
#     tc = (pivot - bc) + pivot
#     return {
#         "pivot": round(pivot, 2),
#         "bc": round(bc, 2),
#         "tc": round(tc, 2)
#     }

# def calculate_traditional_pivots(high, low, close):
#     pivot = (high + low + close) / 3
#     r1 = (2 * pivot) - low
#     s1 = (2 * pivot) - high
#     r2 = pivot + (high - low)
#     s2 = pivot - (high - low)
#     return {
#         "pivot": round(pivot, 2),
#         "r1": round(r1, 2),
#         "s1": round(s1, 2),
#         "r2": round(r2, 2),
#         "s2": round(s2, 2)
#     }

# def calculate_camarilla_pivots(high, low, close):
#     range_val = high - low
#     r3 = close + (range_val * 1.1 / 4)
#     r4 = close + (range_val * 1.1 / 2)
#     s3 = close - (range_val * 1.1 / 4)
#     s4 = close - (range_val * 1.1 / 2)
#     return {
#         "r3": round(r3, 2),
#         "r4": round(r4, 2),
#         "s3": round(s3, 2),
#         "s4": round(s4, 2)
#     }

# # ===== ATR =====
# def calculate_atr(df_, period=14):
#     if len(df_) < period + 1:
#         return None

#     hl = df_["high"] - df_["low"]
#     hc = (df_["high"] - df_["close"].shift()).abs()
#     lc = (df_["low"] - df_["close"].shift()).abs()

#     tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
#     return float(tr.rolling(period).mean().iloc[-1])

# def resolve_atr(candles_3m_, daily_atr_):
#     """
#     Priority:
#     1. Live 3m ATR (after enough candles)
#     2. Daily ATR
#     3. Bootstrap range (temporary)
#     """
#     atr_3m = calculate_atr(candles_3m_)

#     if atr_3m is not None:
#         return atr_3m, "ATR_3M"

#     if daily_atr_ is not None:
#         return daily_atr_, "ATR_DAILY"

#     # Emergency bootstrap (first few minutes only)
#     if len(candles_3m_) >= 2:
#         atr_boot = candles_3m_["high"].max() - candles_3m_["low"].min()
#         logging.warning(f"[BOOTSTRAP ATR] using range={atr_boot:.2f}")
#         return atr_boot, "ATR_BOOTSTRAP"

#     return None, None

def calculate_cpr(high: float, low: float, close: float):
    pivot = (high + low + close) / 3
    bc = (high + low) / 2
    tc = (pivot - bc) + pivot
    return {
        "pivot": round(pivot, 2),
        "bc": round(bc, 2),
        "tc": round(tc, 2)
    }

def calculate_traditional_pivots(high: float, low: float, close: float):
    pivot = (high + low + close) / 3
    r1 = (2 * pivot) - low
    s1 = (2 * pivot) - high
    r2 = pivot + (high - low)
    s2 = pivot - (high - low)
    return {
        "pivot": round(pivot, 2),
        "r1": round(r1, 2),
        "s1": round(s1, 2),
        "r2": round(r2, 2),
        "s2": round(s2, 2)
    }

def calculate_camarilla_pivots(high: float, low: float, close: float):
    range_val = high - low
    r3 = close + (range_val * 1.1 / 4)
    r4 = close + (range_val * 1.1 / 2)
    s3 = close - (range_val * 1.1 / 4)
    s4 = close - (range_val * 1.1 / 2)
    return {
        "r3": round(r3, 2),
        "r4": round(r4, 2),
        "s3": round(s3, 2),
        "s4": round(s4, 2)
    }

def calculate_atr(df_: pd.DataFrame, period: int = 14):
    if len(df_) < period + 1:
        return None
    hl = df_["high"] - df_["low"]
    hc = (df_["high"] - df_["close"].shift()).abs()
    lc = (df_["low"] - df_["close"].shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    return float(tr.rolling(period).mean().iloc[-1])

def resolve_atr(candles_3m_: pd.DataFrame, daily_atr_: float):
    """
    Priority:
    1. Live 3m ATR (after enough candles)
    2. Daily ATR
    3. Bootstrap range (temporary)
    """
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

# ===== Momentum =====
def momentum_ok(candles, side):
    last = candles.iloc[-1]
    prev = candles.iloc[-2]

    momentum = last.close - prev.close

    if side == "CALL":
        ok = momentum > 0
    else:
        ok = momentum < 0

    return ok, momentum

# ===== Candle builder =====
# def build_3min_candle(price):
#     global ticks_buffer, candles_3m, current_3m_start

#     if price is None or pd.isna(price):
#         return

#     ct = dt.now(time_zone)

#     # --- 1️⃣ Initialize first candle aligned to 3-minute boundary ---
#     if current_3m_start is None:
#         minute_bucket = (ct.minute // 3) * 3
#         current_3m_start = ct.replace(
#             minute=minute_bucket,
#             second=0,
#             microsecond=0
#         )
#         ticks_buffer.clear()
#         return

#     # --- 2️⃣ Accumulate ticks ---
#     ticks_buffer.append(float(price))

#     # --- 3️⃣ Close candle ONLY after full 3 minutes elapsed ---
#     if ct >= current_3m_start + dt.duration(minutes=3):

#         if len(ticks_buffer) > 0:
#             candle = {
#                 "open": ticks_buffer[0],
#                 "high": max(ticks_buffer),
#                 "low":  min(ticks_buffer),
#                 "close": ticks_buffer[-1],
#                 "time": current_3m_start
#             }

#             candles_3m.loc[len(candles_3m)] = candle

#             logging.info(
#                 f"{YELLOW}[3M CANDLE CLOSED] {current_3m_start.strftime('%H:%M:%S')} | "
#                 f"O={candle['open']} H={candle['high']} "
#                 f"L={candle['low']} C={candle['close']}{RESET}"
#             )

#         # --- 4️⃣ Advance to next 3-minute window ---
#         current_3m_start += dt.duration(minutes=3)

#         # --- 5️⃣ Reset buffer ---
#         ticks_buffer.clear()

def build_3min_candle(price):
    global ticks_buffer, candles_3m, current_3m_start, daily_atr

    if price is None or pd.isna(price):
        return None

    ct = dt.now(time_zone)

    # --- 1️⃣ Initialize first candle aligned to 3-minute boundary ---
    if current_3m_start is None:
        minute_bucket = (ct.minute // 3) * 3
        current_3m_start = ct.replace(
            minute=minute_bucket,
            second=0,
            microsecond=0
        )
        ticks_buffer.clear()
        return None

    # --- 2️⃣ Accumulate ticks ---
    ticks_buffer.append(float(price))

    # --- 3️⃣ Close candle ONLY after full 3 minutes elapsed ---
    if ct >= current_3m_start + dt.duration(minutes=3):

        if len(ticks_buffer) > 0:
            candle = {
                "open": ticks_buffer[0],
                "high": max(ticks_buffer),
                "low":  min(ticks_buffer),
                "close": ticks_buffer[-1],
                "time": current_3m_start
            }

            candles_3m.loc[len(candles_3m)] = candle

            logging.info(
                f"{YELLOW}[3M CANDLE CLOSED] {current_3m_start.strftime('%H:%M:%S')} | "
                f"O={candle['open']} H={candle['high']} "
                f"L={candle['low']} C={candle['close']}{RESET}"
            )

        # --- 4️⃣ Advance to next 3-minute window ---
        current_3m_start += dt.duration(minutes=3)

        # --- 5️⃣ Reset buffer ---
        ticks_buffer.clear()

        # --- 6️⃣ Compute pivots and ATR ---
        last = candles_3m.iloc[-1]
        cpr_levels = calculate_cpr(last['high'], last['low'], last['close'])
        traditional_levels = calculate_traditional_pivots(last['high'], last['low'], last['close'])
        camarilla_levels = calculate_camarilla_pivots(last['high'], last['low'], last['close'])
        atr, atr_source = resolve_atr(candles_3m, daily_atr)

        logging.info(f"[ATR] source={atr_source} value={atr}")

        # --- 7️⃣ Return tuple for trading pipeline ---
        return candles_3m, cpr_levels, traditional_levels, camarilla_levels, atr

    return None



# ===== Build levels once (optional print) + Daily ATR =====
prev_day = hist_data.iloc[-1]
prev_high, prev_low, prev_close = float(prev_day['high']), float(prev_day['low']), float(prev_day['close'])

cpr_levels_base = calculate_cpr(prev_high, prev_low, prev_close)
traditional_levels_base = calculate_traditional_pivots(prev_high, prev_low, prev_close)
camarilla_levels_base = calculate_camarilla_pivots(prev_high, prev_low, prev_close)

print(
    f"CPR: Pivot={cpr_levels_base['pivot']}, TC={cpr_levels_base['tc']}, BC={cpr_levels_base['bc']}\n"
    f"Traditional: Pivot={traditional_levels_base['pivot']}, R1={traditional_levels_base['r1']}, S1={traditional_levels_base['s1']}, "
    f"R2={traditional_levels_base['r2']}, S2={traditional_levels_base['s2']}\n"
    f"Camarilla: R3={camarilla_levels_base['r3']}, R4={camarilla_levels_base['r4']}, S3={camarilla_levels_base['s3']}, S4={camarilla_levels_base['s4']}"
)

daily_atr = calculate_atr(hist_data, period=14)

logging.info(
    f"[INIT] Daily ATR loaded = {daily_atr:.2f}"
    if daily_atr is not None else
    "[INIT] Daily ATR unavailable"
)

def get_dynamic_target(side, entry_price, pivots, cpr, camarilla, method="auto"):
    """
    Decide dynamic target based on method and side.
    side: "CALL" or "PUT"
    entry_price: option entry price
    pivots: dict with classic pivot levels {"pivot":..., "r1":..., "s1":..., ...}
    cpr: dict with CPR levels {"tc":..., "bc":..., "pivot":...}
    camarilla: dict with camarilla levels {"r3":..., "r4":..., "s3":..., "s4":...}
    method: "classic", "cpr", "camarilla", or "auto"
    """

    target = None

    if method == "classic":
        target = pivots.get("r1") if side == "CALL" else pivots.get("s1")

    elif method == "cpr":
        # For option BUY (CALL or PUT), premium profits when price rises → use tc
        target = cpr.get("tc", entry_price + profit_loss_point)

    elif method == "camarilla":
        target = camarilla.get("r3") if side == "CALL" else camarilla.get("s3")

    elif method == "auto":
        atr = pivots.get("atr", 0)
        if atr < 20:
            target = cpr.get("tc", entry_price + profit_loss_point)
        elif atr < 40:
            target = pivots.get("r1") if side == "CALL" else pivots.get("s1")
        else:
            target = camarilla.get("r3") if side == "CALL" else camarilla.get("s3")

    # Fallback
    if target is None:
        target = entry_price + profit_loss_point

    return target

def get_dynamic_stop(side, entry_price, pivots, cpr, camarilla, method="auto"):
    """
    Decide dynamic stoploss based on method and side.
    """
    stop = None

    if method == "classic":
        stop = pivots.get("s1") if side == "CALL" else pivots.get("r1")

    elif method == "cpr":
        stop = cpr.get("bc", entry_price - hard_stop_points)

    elif method == "camarilla":
        stop = camarilla.get("s3") if side == "CALL" else camarilla.get("r3")

    elif method == "auto":
        atr = pivots.get("atr", 0)
        if atr < 20:
            stop = cpr.get("bc", entry_price - hard_stop_points)
        elif atr < 40:
            stop = pivots.get("s1") if side == "CALL" else pivots.get("r1")
        else:
            stop = camarilla.get("s3") if side == "CALL" else camarilla.get("r3")

    # Fallback
    if stop is None:
        stop = entry_price - hard_stop_points

    return stop



def calculate_cci(
    df: pd.DataFrame,
    length: int = 20,
    smoothing: str = "EMA",
    smoothing_length: int = 20
):
    """
    Calculate Commodity Channel Index (CCI) with optional smoothing.

    Required columns in df: ['high', 'low', 'close']

    Returns:
        DataFrame with columns:
        - CCI
        - CCI_SMOOTH (EMA-smoothed CCI)
    """

    required_cols = {"high", "low", "close"}
    if not required_cols.issubset(df.columns):
        raise ValueError(f"DataFrame must contain {required_cols}")

    # Typical Price
    tp = (df["high"] + df["low"] + df["close"]) / 3

    # SMA of TP
    sma_tp = tp.rolling(length).mean()

    # Mean Deviation
    mean_dev = (
        tp.rolling(length)
        .apply(lambda x: np.mean(np.abs(x - x.mean())), raw=False)
    )

    # CCI
    cci = (tp - sma_tp) / (0.015 * mean_dev)
    df["CCI"] = cci

    # Smoothing
    if smoothing.upper() == "EMA":
        df["CCI_SMOOTH"] = df["CCI"].ewm(
            span=smoothing_length, adjust=False
        ).mean()
    elif smoothing.upper() == "SMA":
        df["CCI_SMOOTH"] = df["CCI"].rolling(smoothing_length).mean()
    else:
        df["CCI_SMOOTH"] = df["CCI"]

    return df

def cci_cross_signal(df):
    """
    Detect CCI crossing its EMA.

    Returns:
        "BULLISH" | "BEARISH" | None
    """

    if len(df) < 2:
        return None

    prev = df.iloc[-2]
    curr = df.iloc[-1]

    # Safety check
    if any(pd.isna(x) for x in [
        prev.CCI, prev.CCI_SMOOTH,
        curr.CCI, curr.CCI_SMOOTH
    ]):
        return None

    # Bullish cross: below -> above
    if prev.CCI < prev.CCI_SMOOTH and curr.CCI > curr.CCI_SMOOTH:
        return "BULLISH"

    # Bearish cross: above -> below
    if prev.CCI > prev.CCI_SMOOTH and curr.CCI < curr.CCI_SMOOTH:
        return "BEARISH"

    return None

def cci_cross_signal(df):
    """
    Detect CCI crossing its EMA.

    Returns:
        "BULLISH" | "BEARISH" | None
    """

    if len(df) < 2:
        return None

    prev = df.iloc[-2]
    curr = df.iloc[-1]

    # Safety check
    if any(pd.isna(x) for x in [
        prev.CCI, prev.CCI_SMOOTH,
        curr.CCI, curr.CCI_SMOOTH
    ]):
        return None

    # Bullish cross: below -> above
    if prev.CCI < prev.CCI_SMOOTH and curr.CCI > curr.CCI_SMOOTH:
        return "BULLISH"

    # Bearish cross: above -> below
    if prev.CCI > prev.CCI_SMOOTH and curr.CCI < curr.CCI_SMOOTH:
        return "BEARISH"

    return None

def cci_cross_up(df, margin=0):
    """
    Detect bullish CCI crossover with optional margin.
    Returns True if CCI has crossed above EMA by at least `margin`.
    """
    if len(df) < 2:
        return False
    p, c = df.iloc[-2], df.iloc[-1]
    return (
        p.CCI < p.CCI_SMOOTH and
        c.CCI > c.CCI_SMOOTH and
        (c.CCI - c.CCI_SMOOTH) > margin
    )

def cci_cross_up_strict(df, margin=0):
    if len(df) < 2:
        return False
    p, c = df.iloc[-2], df.iloc[-1]
    return (
        p.CCI < p.CCI_SMOOTH and
        c.CCI > c.CCI_SMOOTH and
        (c.CCI - c.CCI_SMOOTH) > margin
    )

def cci_cross_down(df, margin=0):
    """
    Detect bearish CCI crossover with optional margin.
    Returns True if CCI has crossed below EMA by at least `margin`.
    """
    if len(df) < 2:
        return False
    p, c = df.iloc[-2], df.iloc[-1]
    return (
        p.CCI > p.CCI_SMOOTH and
        c.CCI < c.CCI_SMOOTH and
        (p.CCI - c.CCI) > margin
    )

def cci_cross_down_strict(df, margin=0):
    if len(df) < 2:
        return False
    p, c = df.iloc[-2], df.iloc[-1]
    return (
        p.CCI > p.CCI_SMOOTH and
        c.CCI < c.CCI_SMOOTH and
        (p.CCI - c.CCI) > margin
    )

def dynamic_body_ratio_threshold(atr, cci_diff):
    """
    Adaptive body ratio threshold:
    - High ATR or strong CCI diff allows weaker candles.
    - Otherwise require stricter body ratio.
    """
    if atr > 28 and cci_diff >= 5:
        return 0.3   # allow shallow candles in high volatility
    elif atr > 22 and cci_diff >= 10:
        return 0.4   # strong crossover compensates
    else:
        return 0.6   # default strict threshold


def strong_trade_with_cci(df, side, momentum, rng, atr, body_ratio, margin=5):
    """
    Extended strong trade check with adaptive body ratio and CCI crossover.
    """
    # --- CCI crossover check ---
    if side == "CALL":
        cci_ok = cci_cross_up(df, margin=margin)
    else:
        cci_ok = cci_cross_down(df, margin=margin)

    # --- Calculate CCI diff for adaptive threshold ---
    p, c = df.iloc[-2], df.iloc[-1]
    cci_diff = abs(c.CCI - c.CCI_SMOOTH)

    # --- Adaptive body ratio threshold ---
    min_body_ratio = dynamic_body_ratio_threshold(atr, cci_diff)
    strength_ok = body_ratio > min_body_ratio

    # --- Momentum check ---
    mom_ok = momentum > 0

    # --- ATR filter ---
    atr_ok = atr > 18

    # Final decision
    ok = strength_ok and mom_ok and cci_ok and atr_ok
    reason = None if ok else f"strength={strength_ok}, mom={mom_ok}, cci={cci_ok}, atr={atr_ok}, min_body={min_body_ratio:.2f}, cci_diff={cci_diff:.2f}"
    return ok, reason

# ========================================================== Candle Patterns ==============================================================================

def is_hammer(candle):
    """Hammer or Inverted Hammer (Pin Bar style)."""
    body = abs(candle['close'] - candle['open'])
    upper_wick = candle['high'] - max(candle['close'], candle['open'])
    lower_wick = min(candle['close'], candle['open']) - candle['low']
    return lower_wick >= 2 * body and upper_wick <= body


def is_shooting_star(candle):
    """Shooting Star (bearish rejection)."""
    body = abs(candle['close'] - candle['open'])
    upper_wick = candle['high'] - max(candle['close'], candle['open'])
    lower_wick = min(candle['close'], candle['open']) - candle['low']
    return upper_wick >= 2 * body and lower_wick <= body


def is_bullish_engulfing(prev, curr):
    """Bullish Engulfing pattern."""
    return (prev['close'] < prev['open'] and
            curr['close'] > curr['open'] and
            curr['close'] > prev['open'] and
            curr['open'] < prev['close'])


def is_bearish_engulfing(prev, curr):
    """Bearish Engulfing pattern."""
    return (prev['close'] > prev['open'] and
            curr['close'] < curr['open'] and
            curr['close'] < prev['open'] and
            curr['open'] > prev['close'])


def is_doji(candle, tolerance=0.1):
    """Doji (indecision candle)."""
    body = abs(candle['close'] - candle['open'])
    rng = candle['high'] - candle['low']
    return body <= tolerance * rng


def is_marubozu(candle, tolerance=0.1):
    """Marubozu (strong breakout candle)."""
    body = abs(candle['close'] - candle['open'])
    rng = candle['high'] - candle['low']
    upper_wick = candle['high'] - max(candle['close'], candle['open'])
    lower_wick = min(candle['close'], candle['open']) - candle['low']
    return body >= (1 - tolerance) * rng and upper_wick <= tolerance * rng and lower_wick <= tolerance * rng


def is_morning_star(c1, c2, c3):
    """Morning Star (bullish 3-candle reversal)."""
    return (c1['close'] < c1['open'] and
            is_doji(c2) and
            c3['close'] > c3['open'] and
            c3['close'] > (c1['open'] + c1['close']) / 2)


def is_evening_star(c1, c2, c3):
    """Evening Star (bearish 3-candle reversal)."""
    return (c1['close'] > c1['open'] and
            is_doji(c2) and
            c3['close'] < c3['open'] and
            c3['close'] < (c1['open'] + c1['close']) / 2)

def is_tweezer_bottom(prev, curr):
    """Tweezer Bottom: equal lows, bullish reversal."""
    return abs(prev['low'] - curr['low']) < 0.1 * (curr['high'] - curr['low']) and curr['close'] > curr['open']

def is_tweezer_top(prev, curr):
    """Tweezer Top: equal highs, bearish reversal."""
    return abs(prev['high'] - curr['high']) < 0.1 * (curr['high'] - curr['low']) and curr['close'] < curr['open']

def is_three_white_soldiers(candles, atr):
    """Three White Soldiers: 3 consecutive bullish candles with strong bodies."""
    return all(c['close'] > c['open'] for c in candles) and sum(abs(c['close'] - c['open']) for c in candles) > 2 * atr

def is_three_black_crows(candles, atr):
    """Three Black Crows: 3 consecutive bearish candles with strong bodies."""
    return all(c['close'] < c['open'] for c in candles) and sum(abs(c['close'] - c['open']) for c in candles) > 2 * atr

def is_pin_bar(candle, ratio=2.0):
    """
    Detect Pin Bar (Hammer or Shooting Star style).
    ratio: wick-to-body ratio threshold (default 2.0).
    """
    body = abs(candle['close'] - candle['open'])
    rng = candle['high'] - candle['low']
    upper_wick = candle['high'] - max(candle['close'], candle['open'])
    lower_wick = min(candle['close'], candle['open']) - candle['low']

    # Avoid division by zero
    if body < 1e-6:
        return False

    # Bullish Pin Bar (Hammer): long lower wick
    bullish = lower_wick >= ratio * body and upper_wick <= body

    # Bearish Pin Bar (Shooting Star): long upper wick
    bearish = upper_wick >= ratio * body and lower_wick <= body

    return bullish or bearish

def is_spinning_top(open_: float, high: float, low: float, close: float,
                    body_ratio: float = 0.5, shadow_ratio: float = 0.25):
    """
    Detect spinning top candle:
    - Small body relative to total range
    - Both upper and lower shadows significant
    """
    body = abs(close - open_)
    rng = high - low
    if rng == 0:
        return False

    upper_shadow = high - max(open_, close)
    lower_shadow = min(open_, close) - low

    body_condition = body <= body_ratio * rng
    shadow_condition = (upper_shadow >= shadow_ratio * rng and
                        lower_shadow >= shadow_ratio * rng)

    return body_condition and shadow_condition

# ============= Unified Function ==================================

def detect_candle_pattern_at_pivot(candles, camarilla_levels, atr, tolerance=0.15):
    """
    Detect candlestick reversal/breakout patterns at Camarilla pivot levels.
    Returns (signal, reason, score) or None if no pattern found.
    """

    # Ensure keys are lowercase
    camarilla_levels = {k.lower(): v for k, v in camarilla_levels.items()}

    last = candles.iloc[-1]
    prev = candles.iloc[-2] if len(candles) >= 2 else None
    prev2 = candles.iloc[-3] if len(candles) >= 3 else None

    rng = last['high'] - last['low']

    def near_level(price, level):
        return abs(price - level) <= max(tolerance * atr, 0.05 * rng)

    candidates = []

    # --- Rejection at s3/s4 (bullish) ---
    if near_level(last['low'], camarilla_levels['s3']) or near_level(last['low'], camarilla_levels['s4']):
        if is_pin_bar(last):
            candidates.append(("CALL", "PinBar rejection at support", 3))
        if prev and is_bullish_engulfing(prev, last):
            candidates.append(("CALL", "Bullish Engulfing at support", 4))
        if prev2 and prev and is_morning_star(prev2, prev, last):
            candidates.append(("CALL", "Morning Star at support", 5))
        if is_doji(last):
            candidates.append(("CALL", "Doji indecision at support", 2))
        if prev and is_tweezer_bottom(prev, last):
            candidates.append(("CALL", "Tweezer Bottom at support", 3))
        if len(candles) >= 4 and is_three_white_soldiers(candles.iloc[-3:], atr):
            candidates.append(("CALL", "Three White Soldiers bullish continuation", 6))

    # --- Rejection at r3/r4 (bearish) ---
    if near_level(last['high'], camarilla_levels['r3']) or near_level(last['high'], camarilla_levels['r4']):
        if is_pin_bar(last):
            candidates.append(("PUT", "PinBar rejection at resistance", 3))
        if prev and is_bearish_engulfing(prev, last):
            candidates.append(("PUT", "Bearish Engulfing at resistance", 4))
        if prev2 and prev and is_evening_star(prev2, prev, last):
            candidates.append(("PUT", "Evening Star at resistance", 5))
        if is_doji(last):
            candidates.append(("PUT", "Doji indecision at resistance", 2))
        if prev and is_tweezer_top(prev, last):
            candidates.append(("PUT", "Tweezer Top at resistance", 3))
        if len(candles) >= 4 and is_three_black_crows(candles.iloc[-3:], atr):
            candidates.append(("PUT", "Three Black Crows bearish continuation", 6))

    # --- Breakouts ---
    if last['close'] > camarilla_levels['r4'] and is_marubozu(last):
        candidates.append(("CALL", "Breakout Marubozu above r4", 7))

    if last['close'] < camarilla_levels['s4'] and is_marubozu(last):
        candidates.append(("PUT", "Breakout Marubozu below s4", 7))

    # --- Return best candidate ---
    if candidates:
        best = max(candidates, key=lambda x: x[2])  # highest score wins
        logging.info(f"[PATTERN DETECTED] {best[1]} | Score={best[2]}")
        return best

    logging.info("[PATTERN CHECK] No valid candlestick pattern at Camarilla pivots")
    return None


def detect_confluence(cpr_levels, traditional_levels, camarilla_levels, atr, tolerance=0.15):
    """
    Detect overlapping pivot zones (confluence) between CPR, Traditional, and Camarilla levels.
    Returns a list of confluence zones with their strength.
    """

    # Normalize all keys to lowercase
    cpr_levels = {k.lower(): v for k, v in cpr_levels.items()}
    traditional_levels = {k.lower(): v for k, v in traditional_levels.items()}
    camarilla_levels = {k.lower(): v for k, v in camarilla_levels.items()}

    zones = []
    levels = {
        "CPR_TC": cpr_levels["tc"],
        "CPR_BC": cpr_levels["bc"],
        "Pivot": traditional_levels["pivot"],
        "R1": traditional_levels["r1"],
        "S1": traditional_levels["s1"],
        "R2": traditional_levels["r2"],
        "S2": traditional_levels["s2"],
        "R3": camarilla_levels["r3"],
        "R4": camarilla_levels["r4"],
        "S3": camarilla_levels["s3"],
        "S4": camarilla_levels["s4"],
    }

    def near(a, b):
        return abs(a - b) <= tolerance * atr

    keys = list(levels.keys())
    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            if near(levels[keys[i]], levels[keys[j]]):
                zones.append((keys[i], keys[j], (levels[keys[i]] + levels[keys[j]]) / 2))

    return zones

def is_strong_trade(signal, atr, momentum, spot, candle_close):
    """
    Strong trade filter:
    - ATR within acceptable band
    - Adaptive momentum threshold for decisive breakdowns
    - Dynamic spot alignment gate vs ATR
    Uses expiry-day overrides from config.py automatically.
    """
    if not signal or atr is None:
        return False

    side, reason = signal

    # thresholds from config.py (with sensible defaults)
    base_min_momentum = getattr(config, "MIN_MOMENTUM", 25)
    atr_min = 20
    atr_max = getattr(config, "ATR_MAX", 80)

    # ATR gate
    if atr < atr_min or atr > atr_max:
        logging.info(f"[STRONG REJECT] ATR gate atr={atr:.2f} not in [{atr_min},{atr_max}]")
        return False

    # Adaptive momentum gate:
    adaptive_min = base_min_momentum
    if reason in ("CONSOLIDATION_BREAKOUT", "BREAKOUT_S3", "BREAKOUT_S4", "BREAKOUT_CPR_BC"):
        adaptive_min = max(10, base_min_momentum - 8)

    if momentum < adaptive_min:
        logging.info(f"{CYAN}[STRONG REJECT] momentum={momentum:.2f} < min={adaptive_min}{RESET}")
        return False

    # Spot vs candle alignment (dynamic gate vs ATR)
    dyn_spot_gate = max(10, 0.75 * atr)
    if abs(spot - candle_close) > dyn_spot_gate:
        logging.info(f"{CYAN}[STRONG REJECT] spot_div={abs(spot - candle_close):.2f} > {dyn_spot_gate:.2f}{RESET}")
        return False

    return True