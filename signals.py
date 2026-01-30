# ===== signals.py =====

import logging
import pandas as pd
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


def evaluate_candle(ts, row, candles, resolution, spot, side, candles_15m=None):
    try:
        # --- ATR from current resolution candles (e.g. 3m) ---
        atr_val = calculate_atr(candles)
        atr_str = f"{atr_val:.2f}" if atr_val is not None else "NA"
        logging.info(f"[SIGNAL EVAL][{resolution}] candle={ts} candles={len(candles)} atr={atr_str} source=ATR_{resolution.upper()}")

        # --- Bias check only if 15m candles provided ---
        bias = None
        if candles_15m is not None and isinstance(candles_15m, pd.DataFrame):
            logging.debug(f"[BIAS PREVIEW @EVAL]\n{candles_15m.tail(3)}")
            daily_val = daily_atr(candles_15m)  # ✅ correct float override
            bias = check_bias(candles_15m, daily_atr=daily_val)
            logging.debug(
                f"[BIAS CALL GUARD @98] candles_15m type={type(candles_15m)} "
                f"len={len(candles_15m) if isinstance(candles_15m, pd.DataFrame) else 'NA'} "
                f"daily_atr type={type(daily_val)}"
            )

            logging.info(f"[BIAS RESULT @EVAL] {bias if bias else 'NA'}")
        else:
            logging.warning("[BIAS SKIPPED @EVAL] No valid 15m candles provided")

        # --- Candle strength + momentum ---
        call_ok, call_momentum = candle_strength(candles, "CALL")
        put_ok, put_momentum   = candle_strength(candles, "PUT")

        # --- Oscillator filters ---
        if call_ok and not apply_oscillator_filter("CALL", candles):
            logging.info("[OSC FILTER] CALL rejected by oscillator")
            call_ok = False
        if put_ok and not apply_oscillator_filter("PUT", candles):
            logging.info("[OSC FILTER] PUT rejected by oscillator")
            put_ok = False

        # --- Evaluate side ---
        if side.upper() == "CALL" and call_ok:
            logging.info(f"[CANDLE EVAL] CALL momentum={call_momentum} bias={bias}")
        elif side.upper() == "PUT" and put_ok:
            logging.info(f"[CANDLE EVAL] PUT momentum={put_momentum} bias={bias}")
        else:
            logging.debug("[CANDLE EVAL] No valid side conditions met")

    except Exception as e:
        logging.error(f"[EVAL ERROR] {resolution} candle {ts}: {e}")

# ===== Bias Resolution =====
def resolve_bias(candles_15m, daily_atr=None):
    """
    Resolve bias using 15m candles and optional daily ATR override.
    - candles_15m must be a DataFrame
    - daily_atr must be a float (optional override)
    """

    # --- Debug guard: log input types ---
    logging.debug(
        f"[RESOLVE BIAS INPUT] candles_15m type={type(candles_15m)} "
        f"daily_atr type={type(daily_atr)}"
    )

    # --- Guard: must be a DataFrame ---
    if candles_15m is None or not isinstance(candles_15m, pd.DataFrame):
        logging.error("[RESOLVE BIAS] Invalid input: candles_15m is not a DataFrame")
        return None

    # --- Call bias check safely ---
    try:
        bias = check_bias(candles_15m, daily_atr=daily_atr)
        if bias == "NEUTRAL":
            logging.debug(
            f"[BIAS CALL GUARD @147] candles_15m type={type(candles_15m)} "
            f"len={len(candles_15m) if isinstance(candles_15m, pd.DataFrame) else 'NA'} "
            f"daily_atr type={type(daily_atr)}"
            )
            logging.info("[SIGNAL FILTERED] Bias neutral, skipping trade")
            return None
        return bias
    except Exception as e:
        logging.error(f"[RESOLVE BIAS ERROR] Failed bias resolution: {e}")
        return None


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

def to_scalar(val):
    """Convert Series/DataFrame cell to scalar float if possible."""
    if val is None:
        return None
    if isinstance(val, pd.Series):
        return float(val.iloc[-1])
    if isinstance(val, pd.DataFrame):
        return float(val.values[-1])
    if isinstance(val, (list, tuple)):
        return float(val[-1])
    try:
        return float(val)
    except Exception:
        return val

def detect_signal(cpr_levels, traditional_levels, camarilla_levels,
                  candles_3m, candles_15m, spot_price=None, daily_atr=None):

    # --- Guard clauses ---
    if candles_3m is None or len(candles_3m) < 2:
        logging.warning("[SIGNAL] Not enough 3m candles to evaluate")
        return None
    if candles_15m is None or not isinstance(candles_15m, pd.DataFrame) or candles_15m.empty:
        logging.warning("[SIGNAL] No 15m candles available for bias check")
        return None

    # --- Resolve ATR ---
    atr, atr_source = resolve_atr(candles_3m, daily_atr)
    atr = to_scalar(atr)
    if atr is None:
        logging.info("[SIGNAL FILTERED] ATR unavailable, skipping")
        return None
    logging.debug(f"[DEBUG] ATR type={type(atr)} value={atr}")
    atr_str = f"{atr:.2f}" if atr is not None else "NA"
    logging.info(f"[ATR] {atr_str} (source={atr_source})")

    # --- Bias check (always use 15m candles) ---
    bias = None
    try:
        logging.debug(f"[BIAS CALL GUARD] candles_15m type={type(candles_15m)} len={len(candles_15m)} daily_atr type={type(daily_atr)}")
        logging.debug(f"[BIAS PREVIEW @SIGNAL]\n{candles_15m.tail(3)}")
        bias = check_bias(candles_15m, daily_atr=daily_atr)
        logging.debug(
            f"[BIAS CALL GUARD @359] candles_15m type={type(candles_15m)} "
            f"len={len(candles_15m) if isinstance(candles_15m, pd.DataFrame) else 'NA'} "
            f"daily_atr type={type(daily_atr)}"
        )
        logging.info(f"[BIAS RESULT] {bias if bias else 'NA'}")
    except Exception as e:
        logging.error(f"[BIAS ERROR] Failed bias check: {e}")
        return None

    if bias is None or bias == "NEUTRAL":
        logging.info("[SIGNAL FILTERED] Bias neutral, skipping trade")
        return None

    # --- Candle references ---
    last = candles_3m.iloc[-1]
    prev = candles_3m.iloc[-2]
    rng  = to_scalar(last.high) - to_scalar(last.low)

    # --- Preview last 3 rows of 3m candles ---
    logging.debug(f"[SIGNAL PREVIEW @3M]\n{candles_3m.tail(3)}")

    # --- Candle strength + momentum ---
    call_ok, call_momentum = candle_strength(candles_3m, "CALL")
    put_ok,  put_momentum  = candle_strength(candles_3m, "PUT")

    # --- Oscillator entry filters ---
    if call_ok and not apply_oscillator_filter("CALL", candles_3m):
        logging.info("[OSC FILTER] CALL rejected by oscillator")
        call_ok = False
    if put_ok and not apply_oscillator_filter("PUT", candles_3m):
        logging.info("[OSC FILTER] PUT rejected by oscillator")
        put_ok = False

    # --- Priority order ---
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
        spot_price = to_scalar(spot_price)
        logging.debug(f"[DEBUG] Spot type={type(spot_price)} value={spot_price}")
        spot_str = f"{spot_price:.2f}" if spot_price is not None else "NA"
        logging.info(
            f"[SIGNAL FOUND] side={side} reason={reason} "
            f"Bias={bias} ATR={atr_str} spot={spot_str}"
        )
        return side, reason

    logging.info("[NO SIGNAL] No valid breakout/rejection detected")
    return None

