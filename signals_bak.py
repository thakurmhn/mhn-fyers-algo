# ===== signals.py =====

import logging
import pandas as pd
import numpy as np
from setup import spot_price
from config import CANDLE_BODY_RANGE, ATR_VALUE
from indicators import (
    calculate_atr,
    resolve_atr,
    daily_atr,
    momentum_ok,
    williams_r,
    supertrend,
    calculate_cci,
    compute_rsi
)

from entry_logic import check_entry_condition


# ANSI COLORS
RESET   = "\033[0m"
GREEN   = "\033[92m"
YELLOW  = "\033[93m"
RED     = "\033[91m"
MAGENTA = "\033[95m"
GRAY    = "\033[90m"
CYAN    = "\033[96m"



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


# ===== CPR Detection =====
def detect_cpr(last, atr, cpr_levels, call_ok, put_ok, bias=None):
    pivot, bc, tc = cpr_levels["pivot"], cpr_levels["bc"], cpr_levels["tc"]

    # Breakouts
    if call_ok and last.close > tc + 0.01 * atr:
        logging.debug(f"[CPR] BREAKOUT_TC close={last.close} tc={tc} atr={atr}")
        return "CALL", "BREAKOUT_CPR_TC"
    if put_ok and last.close < bc - 0.01 * atr:
        logging.debug(f"[CPR] BREAKOUT_BC close={last.close} bc={bc} atr={atr}")
        return "PUT", "BREAKOUT_CPR_BC"

    # Acceptance
    if call_ok and abs(last.close - tc) <= 0.5 * atr:
        logging.debug(f"[CPR] ACCEPTANCE_TC close={last.close} tc={tc} atr={atr}")
        return "CALL", "ACCEPTANCE_CPR_TC"
    if put_ok and abs(last.close - bc) <= 0.5 * atr:
        logging.debug(f"[CPR] ACCEPTANCE_BC close={last.close} bc={bc} atr={atr}")
        return "PUT", "ACCEPTANCE_CPR_BC"

    # Continuation (bias‑aware)
    if bc < last.close < tc:
        logging.debug(f"[CPR] CONTINUATION close={last.close} bc={bc} tc={tc} bias={bias}")
        if bias == "BULLISH" and call_ok:
            return "CALL", "CONTINUATION_CPR"
        elif bias == "BEARISH" and put_ok:
            return "PUT", "CONTINUATION_CPR"
        else:
            return "HOLD", "CONTINUATION_CPR"

    logging.debug(f"[CPR] NO SIGNAL close={last.close} bc={bc} tc={tc} atr={atr}")
    return None


# ===== Camarilla Detection =====
def detect_camarilla(last, rng, atr, camarilla_levels, call_ok, put_ok, bias=None):
    r3, r4, s3, s4 = camarilla_levels["r3"], camarilla_levels["r4"], camarilla_levels["s3"], camarilla_levels["s4"]

    # Breakouts
    if call_ok and last.close > r3 + 0.01 * atr:
        logging.debug(f"[CAM] BREAKOUT_R3 close={last.close} r3={r3} atr={atr}")
        return "CALL", "BREAKOUT_R3"
    if call_ok and last.close > r4 + 0.01 * atr:
        logging.debug(f"[CAM] BREAKOUT_R4 close={last.close} r4={r4} atr={atr}")
        return "CALL", "BREAKOUT_R4"
    if put_ok and last.close < s3 - 0.01 * atr:
        logging.debug(f"[CAM] BREAKOUT_S3 close={last.close} s3={s3} atr={atr}")
        return "PUT", "BREAKOUT_S3"
    if put_ok and last.close < s4 - 0.01 * atr:
        logging.debug(f"[CAM] BREAKOUT_S4 close={last.close} s4={s4} atr={atr}")
        return "PUT", "BREAKOUT_S4"

    # Acceptance
    if call_ok and abs(last.close - r3) <= 0.5 * atr:
        logging.debug(f"[CAM] ACCEPTANCE_R3 close={last.close} r3={r3} atr={atr}")
        return "CALL", "ACCEPTANCE_R3"
    if put_ok and abs(last.close - s3) <= 0.5 * atr:
        logging.debug(f"[CAM] ACCEPTANCE_S3 close={last.close} s3={s3} atr={atr}")
        return "PUT", "ACCEPTANCE_S3"

    # Continuation (bias‑aware)
    if s3 < last.close < r3:
        logging.debug(f"[CAM] CONTINUATION close={last.close} s3={s3} r3={r3} bias={bias}")
        if bias == "BULLISH" and call_ok:
            return "CALL", "CONTINUATION_CAM"
        elif bias == "BEARISH" and put_ok:
            return "PUT", "CONTINUATION_CAM"
        else:
            return "HOLD", "CONTINUATION_CAM"

    # Rejections
    if call_ok and last.low <= s3 and (last.close - last.low) > 0.2 * rng:
        logging.debug(f"[CAM] REJECTION_S3 close={last.close} low={last.low} s3={s3} rng={rng}")
        return "CALL", "REJECTION_S3"
    if put_ok and last.high >= r3 and (last.high - last.close) > 0.2 * rng:
        logging.debug(f"[CAM] REJECTION_R3 close={last.close} high={last.high} r3={r3} rng={rng}")
        return "PUT", "REJECTION_R3"

    logging.debug(f"[CAM] NO SIGNAL close={last.close} r3={r3} s3={s3} atr={atr} rng={rng}")
    return None


# ===== Traditional Detection =====
def detect_traditional_acceptance(last, atr, traditional_levels, call_ok, put_ok):
    r2, s2 = traditional_levels["r2"], traditional_levels["s2"]

    if call_ok and last.close > r2 + 0.01 * atr:
        logging.debug(f"[TRAD] BREAKOUT_R2 close={last.close} r2={r2} atr={atr}")
        return "CALL", "BREAKOUT_R2"
    if put_ok and last.close < s2 - 0.01 * atr:
        logging.debug(f"[TRAD] BREAKOUT_S2 close={last.close} s2={s2} atr={atr}")
        return "PUT", "BREAKOUT_S2"

    if call_ok and abs(last.close - r2) <= 0.5 * atr:
        logging.debug(f"[TRAD] ACCEPTANCE_R2 close={last.close} r2={r2} atr={atr}")
        return "CALL", "ACCEPTANCE_R2"
    if put_ok and abs(last.close - s2) <= 0.5 * atr:
        logging.debug(f"[TRAD] ACCEPTANCE_S2 close={last.close} s2={s2} atr={atr}")
        return "PUT", "ACCEPTANCE_S2"

    logging.debug(f"[TRAD] NO SIGNAL close={last.close} r2={r2} s2={s2} atr={atr}")
    return None


def detect_traditional_rejection(last, rng, traditional_levels, call_ok, put_ok):
    r1, s1 = traditional_levels["r1"], traditional_levels["s1"]

    if call_ok and last.low <= s1 and (last.close - last.low) > 0.2 * rng:
        logging.debug(f"[TRAD] REJECTION_S1 close={last.close} low={last.low} s1={s1} rng={rng}")
        return "CALL", "REJECTION_S1"
    if put_ok and last.high >= r1 and (last.high - last.close) > 0.2 * rng:
        logging.debug(f"[TRAD] REJECTION_R1 close={last.close} high={last.high} r1={r1} rng={rng}")
        return "PUT", "REJECTION_R1"

    logging.debug(f"[TRAD] NO SIGNAL close={last.close} r1={r1} s1={s1} rng={rng}")
    return None


def detect_traditional_continuation(last, atr, traditional_levels, call_ok, put_ok, bias=None):
    r2, s2 = traditional_levels["r2"], traditional_levels["s2"]

    if call_ok and last.low <= r2 and last.close > r2 + 0.03 * atr:
        logging.debug(f"[TRAD] CONTINUATION_R2 close={last.close} r2={r2} atr={atr} bias={bias}")
        return "CALL", "CONTINUATION_R2"
    if put_ok and last.high >= s2 and last.close < s2 - 0.03 * atr:
        logging.debug(f"[TRAD] CONTINUATION_S2 close={last.close} s2={s2} atr={atr} bias={bias}")
        return "PUT", "CONTINUATION_S2"

    logging.debug(f"[TRAD] NO CONTINUATION close={last.close} r2={r2} s2={s2} atr={atr}")
    return None


# ===== Pivot Detection =====
def detect_pivot_acceptance(last, prev, atr, traditional_levels, call_ok, put_ok):
    pivot = traditional_levels["pivot"]

    # Breakouts
    if call_ok and prev.close < pivot and last.close > pivot + 0.01 * atr:
        logging.debug(f"[PIVOT] BREAKOUT_CALL close={last.close} prev={prev.close} pivot={pivot} atr={atr}")
        return "CALL", "BREAKOUT_PIVOT"
    if put_ok and prev.close > pivot and last.close < pivot - 0.01 * atr:
        logging.debug(f"[PIVOT] BREAKOUT_PUT close={last.close} prev={prev.close} pivot={pivot} atr={atr}")
        return "PUT", "BREAKOUT_PIVOT"

    # Acceptance
    if call_ok and abs(last.close - pivot) <= 0.5 * atr:
        logging.debug(f"[PIVOT] ACCEPTANCE_CALL close={last.close} pivot={pivot} atr={atr}")
        return "CALL", "ACCEPTANCE_PIVOT"
    if put_ok and abs(last.close - pivot) <= 0.5 * atr:
        logging.debug(f"[PIVOT] ACCEPTANCE_PUT close={last.close} pivot={pivot} atr={atr}")
        return "PUT", "ACCEPTANCE_PIVOT"

    logging.debug(f"[PIVOT] NO ACCEPTANCE close={last.close} pivot={pivot} atr={atr}")
    return None


def detect_pivot_rejection(last, rng, traditional_levels, call_ok, put_ok):
    pivot = traditional_levels["pivot"]

    if call_ok and last.low <= pivot and (last.close - last.low) > 0.2 * rng:
        logging.debug(f"[PIVOT] REJECTION_CALL close={last.close} low={last.low} pivot={pivot} rng={rng}")
        return "CALL", "REJECTION_PIVOT"
    if put_ok and last.high >= pivot and (last.high - last.close) > 0.2 * rng:
        logging.debug(f"[PIVOT] REJECTION_PUT close={last.close} high={last.high} pivot={pivot} rng={rng}")
        return "PUT", "REJECTION_PIVOT"

    logging.debug(f"[PIVOT] NO REJECTION close={last.close} pivot={pivot} rng={rng}")
    return None


def detect_pivot_continuation(last, atr, traditional_levels, call_ok, put_ok, bias=None):
    pivot = traditional_levels["pivot"]

    # Continuation (bias‑aware)
    if abs(last.close - pivot) <= 0.5 * atr:
        logging.debug(f"[PIVOT] CONTINUATION close={last.close} pivot={pivot} atr={atr} bias={bias}")
        if bias == "BULLISH" and call_ok:
            return "CALL", "CONTINUATION_PIVOT"
        elif bias == "BEARISH" and put_ok:
            return "PUT", "CONTINUATION_PIVOT"
        else:
            return "HOLD", "CONTINUATION_PIVOT"

    logging.debug(f"[PIVOT] NO CONTINUATION close={last.close} pivot={pivot} atr={atr} bias={bias}")
    return None

   

def to_scalar(val):
    """Convert Pandas/NumPy scalar to plain Python float/int, or None."""
    try:
        if val is None:
            return None
        if hasattr(val, "item"):
            return val.item()
        return float(val)
    except Exception:
        return None
    

def classify_volatility(atr_val, close_price=None, thresholds=(0.5, 1.0)):
    """
    Classify volatility regime based on ATR.
    If close_price is provided, normalize ATR as % of price.
    thresholds = (low%, high%)
    """
    if atr_val is None:
        return "UNKNOWN"
    if close_price:
        atr_pct = (atr_val / close_price) * 100
    else:
        atr_pct = atr_val

    if atr_pct < thresholds[0]:
        return "LOW"
    elif atr_pct < thresholds[1]:
        return "MEDIUM"
    else:
        return "HIGH"
    

def dynamic_targets(entry_price, atr, side, sl_factor=1.5, pt_factor=1.0, tg_factor=2.0):
    """
    ATR-based targets for SL/PT/TG.
    Returns dict with absolute price levels.
    """
    if side == "CALL":
        return {
            "SL": entry_price - atr * sl_factor,
            "PT": entry_price + atr * pt_factor,
            "TG": entry_price + atr * tg_factor
        }
    elif side == "PUT":
        return {
            "SL": entry_price + atr * sl_factor,
            "PT": entry_price - atr * pt_factor,
            "TG": entry_price - atr * tg_factor
        }
    return {}

def signal_confidence(vol_regime, bias_score, reason):
    """
    Assign confidence based on volatility regime + bias score.
    - vol_regime: LOW/MEDIUM/HIGH
    - bias_score: numeric score from bias_from_indicators
    - reason: string describing signal type
    """
    if vol_regime == "HIGH" and "BREAKOUT" in reason and bias_score >= 60:
        return "STRONG"
    elif vol_regime == "LOW" and "CONTINUATION" in reason and bias_score < 40:
        return "WEAK"
    elif bias_score >= 50:
        return "MEDIUM-HIGH"
    else:
        return "MEDIUM"



# Global counters for replay diagnostics
signal_blockers = {
    "ATR": 0,
    "SUPER_TREND": 0,
    "CCI": 0,
    "EMA": 0,
    "PIVOT": 0
}


def _make_state(side, reason, candles_3m, atr, last, prev):
    ok, momentum = momentum_ok(candles_3m, side)
    if not ok:
        return None
    return {
        "side": side,
        "reason": reason,
        "entry_candle": len(candles_3m) - 1,
        "atr_entry": atr,
        "prev_gap": abs(last.close - prev.close),
        "momentum": momentum
    }


def detect_signal(candles_3m, candles_15m,
                  cpr_levels, camarilla_levels, traditional_levels,
                  atr=None, include_partial=False):
    """Unified signal detection: momentum+pivots + liquidity zone."""

    if "is_partial" in candles_3m.columns:
        if not include_partial and candles_3m.iloc[-1]["is_partial"]:
            logging.debug("[SIGNAL] Skipping partial candle")
            return None

    if atr is None or atr < 10 or atr > 200:
        signal_blockers["ATR"] += 1
        logging.debug(f"[SIGNAL] ATR filter failed: atr={atr}")
        return None

    last_3m = candles_3m.iloc[-1]
    prev_3m = candles_3m.iloc[-2] if len(candles_3m) > 1 else last_3m
    last_15m = candles_15m.iloc[-1]
    rng = last_3m.high - last_3m.low

    st_bias = last_15m.get("supertrend_bias", "NEUTRAL")
    logging.debug(f"[SIGNAL] Supertrend bias={st_bias}")

    # --- EMA confirmation helper ---
    def ema_confirm(side):
        if side == "CALL":
            closes_above = (
                candles_3m["close"].iloc[-2:] > candles_3m["ema9"].iloc[-2:]
            ) & (
                candles_3m["close"].iloc[-2:] > candles_3m["ema13"].iloc[-2:]
            )
            return closes_above.all()
        elif side == "PUT":
            closes_below = (
                candles_3m["close"].iloc[-2:] < candles_3m["ema9"].iloc[-2:]
            ) & (
                candles_3m["close"].iloc[-2:] < candles_3m["ema13"].iloc[-2:]
            )
            return closes_below.all()
        return False

    # --- Pivot helper ---
    def pivot_ok(side):
        if side == "CALL":
            return (
                detect_cpr(last_3m, atr, cpr_levels, True, False, st_bias)
                or detect_camarilla(last_3m, rng, atr, camarilla_levels, True, False, st_bias)
                or detect_traditional_acceptance(last_3m, atr, traditional_levels, True, False)
                or detect_pivot_acceptance(last_3m, prev_3m, atr, traditional_levels, True, False)
            )
        elif side == "PUT":
            return (
                detect_cpr(last_3m, atr, cpr_levels, False, True, st_bias)
                or detect_camarilla(last_3m, rng, atr, camarilla_levels, False, True, st_bias)
                or detect_traditional_rejection(last_3m, rng, traditional_levels, False, True)
                or detect_pivot_rejection(last_3m, rng, traditional_levels, False, True)
            )
        return None

    # --- Existing momentum + pivot logic ---
    if st_bias == "BULLISH":
        if last_15m["cci20"] > 60 and last_3m["cci20"] > 80:
            if ema_confirm("CALL"):
                pivot_signal = pivot_ok("CALL")
                if pivot_signal:
                    side, reason = pivot_signal
                    state = _make_state(side, reason, candles_3m, atr, last_3m, prev_3m)
                    if state:
                        state["source"] = "PIVOT"
                        return state
            else:
                signal_blockers["EMA"] += 1
        else:
            signal_blockers["CCI"] += 1

    if st_bias == "BEARISH":
        if last_15m["cci20"] < -60 and last_3m["cci20"] < -80:
            if ema_confirm("PUT"):
                pivot_signal = pivot_ok("PUT")
                if pivot_signal:
                    side, reason = pivot_signal
                    state = _make_state(side, reason, candles_3m, atr, last_3m, prev_3m)
                    if state:
                        state["source"] = "PIVOT"
                        return state
            else:
                signal_blockers["EMA"] += 1
        else:
            signal_blockers["CCI"] += 1

    # --- Liquidity zone fallback ---
    indicators = {
        "ema_fast": last_3m["ema9"],
        "ema_slow": last_3m["ema13"],
        "adx": last_3m["adx14"],
        "cci": last_3m["cci20"],
        "atr": last_3m["atr14"],
        "supertrend_line_3m": last_3m["supertrend_line"],
        "supertrend_line_15m": last_15m["supertrend_line"],
    }
    bias_15m = last_15m["supertrend_bias"]

    lz_signal = check_entry_condition(last_3m, indicators, bias_15m)
    if lz_signal["action"] in ["BUY", "SELL"]:
        side = "CALL" if lz_signal["action"] == "BUY" else "PUT"
        reason = f"{lz_signal['strength']} {lz_signal['zone_type']} zone + {lz_signal['reason']}"
        state = _make_state(side, reason, candles_3m, atr, last_3m, prev_3m)
        if state:
            state["source"] = "LIQUIDITY_ZONE"
            return state

    return None

