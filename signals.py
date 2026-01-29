# ===== signals.py =====

import logging
from setup import spot_price
from config import CANDLE_BODY_RANGE, ATR_VALUE
from indicators import (
    calculate_atr,
    resolve_atr,
    daily_atr,
    check_bias,
    momentum_ok,
    oscillator_entry_filter,
    oscillator_exit_trigger
)


# ANSI COLORS
RESET   = "\033[0m"
GREEN   = "\033[92m"
YELLOW  = "\033[93m"
RED     = "\033[91m"
MAGENTA = "\033[95m"
GRAY    = "\033[90m"



def atr_based_levels(entry_price, atr_val, side, sl_factor=1.0, pt_factor=2.0, tg_factor=1.5):
    if side == "CALL":
        sl = entry_price - atr_val * sl_factor
        pt = entry_price + atr_val * pt_factor
        tg = entry_price + atr_val * tg_factor
    elif side == "PUT":
        sl = entry_price + atr_val * sl_factor
        pt = entry_price - atr_val * pt_factor
        tg = entry_price - atr_val * tg_factor
    else:
        return None, None, None
    logging.info(f"[ATR LEVELS] side={side} Entry={entry_price:.2f} ATR={atr_val:.2f} SL={sl:.2f} PT={pt:.2f} TG={tg:.2f}")
    return sl, pt, tg


def evaluate_candle(ts, row, candles, resolution="3m", spot=None, side="CALL"):
    # ATR
    atr_val = calculate_atr(candles)  # ✅ float
    logging.info(
        f"[SIGNAL EVAL][{resolution.upper()}] candle={ts} candles={len(candles)} "
        f"atr={atr_val:.2f} source=ATR_{resolution.upper()}"
    )

    # Bias check
    bias = check_bias(candles, daily_atr=atr_val)
    logging.info(f"[BIAS RESULT][{resolution.upper()}] {bias}")

    # Momentum check
    body_range = (row.close - row.open) / (row.high - row.low + 1e-9)
    call_mom = body_range * atr_val
    put_mom = body_range * atr_val
    logging.info(
        f"[SIGNAL CHECK] close={row.close:.2f} spot={spot:.2f if spot else 0} "
        f"ATR={atr_val:.2f} body/range={body_range:.2f} "
        f"CALL_mom={call_mom:.2f} PUT_mom={put_mom:.2f}"
    )

    # ATR levels
    sl, pt, tg = atr_based_levels(row.close, atr_val, side)
    logging.info(
        f"[ATR LEVELS] Entry={row.close:.2f} ATR={atr_val:.2f} "
        f"SL={sl:.2f} PT={pt:.2f} TG={tg:.2f}"
    )

    # Oscillator entry filter (only for 3m candles)
    if resolution == "3m":
        allowed = oscillator_entry_filter(side, candles)
        if not allowed:
            logging.info(f"[ENTRY BLOCKED][{resolution.upper()}][OSC] side={side}")

    # Oscillator exit trigger (only for 15m candles)
    if resolution == "15m":
        exit_signal, reason = oscillator_exit_trigger(side, candles)
        if exit_signal:
            logging.info(f"[EXIT TRIGGERED][{resolution.upper()}][OSC] {reason}")

    return bias


# ===== ATR Resolution =====
def resolve_signal_atr(candles_3m, daily_atr=None):
    atr, atr_source = resolve_atr(candles_3m, daily_atr)
    if atr is None or len(candles_3m) < 2:
        logging.info("[SIGNAL FILTERED] Not enough candles or ATR unavailable")
        return None, None
    if atr < ATR_VALUE:
        logging.info(f"[SIGNAL FILTERED] ATR too low ({atr:.2f}), skipping trade")
        return None, None
    if atr > 120:
        logging.info(f"[SIGNAL FILTERED] ATR too high ({atr:.2f}), skipping trade")
        return None, None
    return atr, atr_source


# ===== Bias Resolution =====
def resolve_bias(candles_15m, daily_atr=None):
    bias = check_bias(candles_15m, daily_atr=daily_atr)
    if bias == "NEUTRAL":
        logging.info("[SIGNAL FILTERED] Bias neutral, skipping trade")
        return None
    return bias


# ===== Candle Strength + Momentum =====
def candle_strength(candles_3m, side):
    last = candles_3m.iloc[-1]
    prev = candles_3m.iloc[-2]
    body = abs(last.close - last.open)
    rng  = last.high - last.low
    if rng == 0:
        return False, 0
    mom_ok, momentum = momentum_ok(candles_3m, side)
    strength_ok = (body / rng) > CANDLE_BODY_RANGE
    return strength_ok and mom_ok, momentum


# ===== Oscillator Entry Filter =====
def apply_oscillator_filter(side, candles_3m):
    if not oscillator_entry_filter(side, candles_3m):
        logging.info(f"[SIGNAL FILTERED] {side} blocked by oscillator")
        return False
    return True


# ===== CPR Detection =====
def detect_cpr(last, atr, cpr_levels, call_ok, put_ok):
    if last.close > cpr_levels["tc"] + 0.1 * atr and call_ok:
        return "CALL", "BREAKOUT_CPR_TC"
    if last.close < cpr_levels["bc"] - 0.1 * atr and put_ok:
        return "PUT", "BREAKOUT_CPR_BC"
    return None


# ===== Camarilla Detection =====
def detect_camarilla(last, rng, atr, camarilla_levels, call_ok, put_ok):
    r3, r4, s3, s4 = (camarilla_levels["r3"], camarilla_levels["r4"],
                      camarilla_levels["s3"], camarilla_levels["s4"])
    if last.close > r3 + 0.1 * atr and call_ok:
        return "CALL", "BREAKOUT_R3"
    if last.close > r4 + 0.1 * atr and call_ok:
        return "CALL", "BREAKOUT_R4"
    if last.close < s3 - 0.1 * atr and put_ok:
        return "PUT", "BREAKOUT_S3"
    if last.close < s4 - 0.1 * atr and put_ok:
        return "PUT", "BREAKOUT_S4"
    if last.low <= s3 and (last.close - last.low) > 0.5 * rng and call_ok:
        return "CALL", "REJECTION_S3"
    if last.low <= s4 and (last.close - last.low) > 0.5 * rng and call_ok:
        return "CALL", "REJECTION_S4"
    if last.high >= r3 and (last.high - last.close) > 0.5 * rng and put_ok:
        return "PUT", "REJECTION_R3"
    if last.high >= r4 and (last.high - last.close) > 0.5 * rng and put_ok:
        return "PUT", "REJECTION_R4"
    return None


# ===== Traditional Detection =====
def detect_traditional_acceptance(last, atr, traditional_levels, call_ok, put_ok):
    r2, s2 = traditional_levels["r2"], traditional_levels["s2"]
    if last.close > r2 + 0.1 * atr and call_ok:
        return "CALL", "BREAKOUT_R2"
    if last.close < s2 - 0.1 * atr and put_ok:
        return "PUT", "BREAKOUT_S2"
    return None

def detect_traditional_rejection(last, rng, traditional_levels, call_ok, put_ok):
    r1, s1 = traditional_levels["r1"], traditional_levels["s1"]
    if last.low <= s1 and (last.close - last.low) > 0.5 * rng and call_ok:
        return "CALL", "REJECTION_S1"
    if last.high >= r1 and (last.high - last.close) > 0.5 * rng and put_ok:
        return "PUT", "REJECTION_R1"
    return None

def detect_traditional_continuation(last, atr, traditional_levels, call_ok, put_ok):
    r2, s2 = traditional_levels["r2"], traditional_levels["s2"]
    if last.low <= r2 and last.close > r2 + 0.05 * atr and call_ok:
        return "CALL", "CONTINUATION_R2"
    if last.high >= s2 and last.close < s2 - 0.05 * atr and put_ok:
        return "PUT", "CONTINUATION_S2"
    return None


# ===== Pivot Detection =====
def detect_pivot_acceptance(last, prev, atr, traditional_levels, call_ok, put_ok):
    pivot = traditional_levels["pivot"]
    if prev.close < pivot and last.close > pivot + 0.1 * atr and call_ok:
        return "CALL", "BREAKOUT_PIVOT"
    if prev.close > pivot and last.close < pivot - 0.1 * atr and put_ok:
        return "PUT", "BREAKOUT_PIVOT"
    return None

def detect_pivot_rejection(last, rng, traditional_levels, call_ok, put_ok):
    pivot = traditional_levels["pivot"]
    if last.low <= pivot and (last.close - last.low) > 0.5 * rng and call_ok:
        return "CALL", "REJECTION_PIVOT"
    if last.high >= pivot and (last.high - last.close) > 0.5 * rng and put_ok:
        return "PUT", "REJECTION_PIVOT"
    return None

def detect_pivot_continuation(last, atr, traditional_levels, call_ok, put_ok):
    pivot = traditional_levels["pivot"]
    if last.low <= pivot and last.close > pivot + 0.05 * atr and call_ok:
        return "CALL", "CONTINUATION_PIVOT"
    if last.high >= pivot and last.close < pivot - 0.05 * atr and put_ok:
        return "PUT", "CONTINUATION_PIVOT"
    return None


# ===== Orchestration =====
# ===== Orchestration =====
def detect_signal(cpr_levels, traditional_levels, camarilla_levels,
                  candles_3m, candles_15m, spot_price=None, daily_atr=None):

    # Resolve ATR (float + source string)
    atr, atr_source = resolve_atr(candles_3m, daily_atr)
    if atr is None:
        return None

    # Bias check
    bias = check_bias(candles_15m, daily_atr=daily_atr)
    if bias is None or bias == "NEUTRAL":
        logging.info("[SIGNAL FILTERED] Bias neutral, skipping trade")
        return None

    # Candle references
    last = candles_3m.iloc[-1]
    prev = candles_3m.iloc[-2]
    rng  = last.high - last.low

    # Candle strength + momentum
    call_ok, call_momentum = candle_strength(candles_3m, "CALL")
    put_ok,  put_momentum  = candle_strength(candles_3m, "PUT")

    # Oscillator entry filters
    if call_ok and not apply_oscillator_filter("CALL", candles_3m):
        return None
    if put_ok and not apply_oscillator_filter("PUT", candles_3m):
        return None

    # Priority order
    signal = (
        detect_cpr(last, atr, cpr_levels, call_ok, put_ok) or
        detect_camarilla(last, rng, atr, camarilla_levels, call_ok, put_ok) or
        detect_traditional_acceptance(last, atr, traditional_levels, call_ok, put_ok) or
        detect_traditional_rejection(last, rng, traditional_levels, call_ok, put_ok) or
        detect_traditional_continuation(last, atr, traditional_levels, call_ok, put_ok) or
        detect_pivot_acceptance(last, prev, atr, traditional_levels, call_ok, put_ok) or
        detect_pivot_rejection(last, rng, traditional_levels, call_ok, put_ok) or
        detect_pivot_continuation(last, atr, traditional_levels, call_ok, put_ok)
    )

    if signal:
        side, reason = signal
        logging.info(
            f"[SIGNAL FOUND] side={side} reason={reason} "
            f"Bias={bias} ATR={atr:.2f} spot={spot_price if spot_price else 'NA'}"
        )
        return side, reason

    logging.info("[NO SIGNAL] No valid breakout/rejection detected")
    return None