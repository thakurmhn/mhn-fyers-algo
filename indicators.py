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


def calculate_cpr(high, low, close):
    pivot = (high + low + close) / 3
    bc = (high + low) / 2
    tc = (pivot - bc) + pivot
    return {
        "pivot": round(pivot, 2),
        "bc": round(bc, 2),
        "tc": round(tc, 2)
    }

def calculate_traditional_pivots(high, low, close):
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

def calculate_camarilla_pivots(high, low, close):
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

    # Emergency bootstrap (first few minutes only)
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
def build_3min_candle(price):
    global ticks_buffer, candles_3m, current_3m_start

    if price is None or pd.isna(price):
        return

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
        return

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

def strong_trade_with_cci(df, side, momentum, rng, atr, body_ratio, margin=5):
    """
    Extended strong trade check that includes CCI crossover.
    """
    # --- Candle strength check (existing logic) ---
    strength_ok = body_ratio > 0.6
    
    mom_ok = momentum > 0  # or your momentum_ok logic

    # --- CCI crossover check ---
    if side == "CALL":
        cci_ok = cci_cross_up_strict(df, margin=margin)
    else:
        cci_ok = cci_cross_down_strict(df, margin=margin)

    # --- ATR filter ---
    atr_ok = atr > 20

    # Final decision
    ok = strength_ok and mom_ok and cci_ok and atr_ok
    reason = None if ok else f"strength={strength_ok}, mom={mom_ok}, cci={cci_ok}, atr={atr_ok}"
    return ok, reason

