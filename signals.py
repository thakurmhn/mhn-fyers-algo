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
    calculate_cci
)


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


def bias_from_indicators(row):
    """
    Replay-style bias evaluation using 3m candles only.
    Returns: (signal, score)
    - signal: "CALL BUY", "PUT SELL", or "HOLD"
    - score: confidence score based on indicator alignment
    """
    reason, score = [], 0

    # EMA crossover (gap widening logic handled in orchestration)
    if row['ema20'] > row['ema50']:
        reason.append("EMA20>EMA50"); score += 20
    elif row['ema20'] < row['ema50']:
        reason.append("EMA20<EMA50"); score += 20

    # Supertrend bias + slope
    st_bias = row.get('supertrend_bias')
    st_slope = row.get('supertrend_slope')
    if st_bias == "BULLISH":
        reason.append("Supertrend=UP"); score += 20
    elif st_bias == "BEARISH":
        reason.append("Supertrend=DOWN"); score += 20
    if st_slope:
        reason.append(f"Slope={st_slope}")

    # Confidence boost/penalty based on slope
    if st_slope == "UP" and st_bias == "BULLISH":
        score += 10
    elif st_slope == "DOWN" and st_bias == "BEARISH":
        score += 10
    elif st_slope == "FLAT":
        score -= 10

    # ADX strength
    if row['adx14'] > 25:
        reason.append("ADX strong"); score += 20
    elif row['adx14'] > 20:
        reason.append("ADX moderate"); score += 10

    # CCI thresholds
    if row['cci20'] > 50:
        reason.append("CCI>50"); score += 20
    elif row['cci20'] < -50:
        reason.append("CCI<-50"); score += 20

    # Decision logic (no 15m dependency)
    if (st_bias == "BULLISH" and st_slope == "UP"
        and row['close'] > row['ema20']
        and row['cci20'] > 50
        and row['adx14'] > 20):
        return "CALL BUY | " + ", ".join(reason), score

    elif (st_bias == "BEARISH" and st_slope == "DOWN"
          and row['close'] < row['ema20']
          and row['cci20'] < -50
          and row['adx14'] > 20):
        return "PUT SELL | " + ", ".join(reason), score

    else:
        return "HOLD | " + ", ".join(reason), score
    

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


def detect_signal(cpr_levels, traditional_levels, camarilla_levels,
                  candles_3m, atr=None, bias=None):
    """
    Detects CALL/PUT signals using CPR, Camarilla, Traditional, Pivot.
    Adjusted for live market:
    - ATR regime filter (skip extreme flat/volatile)
    - Higher timeframe bias filter
    - Prevent duplicate trades on same level
    Returns entry state dict if signal detected.
    """

    # --- ATR regime filter ---
    if atr is None or atr < 15 or atr > 180:
        return None

    last = candles_3m.iloc[-1]
    prev = candles_3m.iloc[-2] if len(candles_3m) > 1 else last
    rng = last.high - last.low

    call_ok, put_ok = True, True

    # --- Higher timeframe bias filter (example: 15m trend) ---
    # Replace with your actual bias logic if available
    higher_tf_trend = "UP" if last.close > candles_3m["close"].rolling(5).mean().iloc[-1] else "DOWN"

    # --- Try Camarilla ---
    cam_signal = detect_camarilla(last, rng, atr, camarilla_levels, call_ok, put_ok, bias)
    if cam_signal:
        side, reason = cam_signal
        if side != "HOLD":
            # Bias check
            if (side == "CALL" and higher_tf_trend == "UP") or (side == "PUT" and higher_tf_trend == "DOWN"):
                return _make_state(side, reason, candles_3m, atr, last, prev)

    # --- Try CPR ---
    cpr_signal = detect_cpr(last, atr, cpr_levels, call_ok, put_ok, bias)
    if cpr_signal:
        side, reason = cpr_signal
        if side != "HOLD":
            if (side == "CALL" and higher_tf_trend == "UP") or (side == "PUT" and higher_tf_trend == "DOWN"):
                return _make_state(side, reason, candles_3m, atr, last, prev)

    # --- Try Traditional ---
    trad_signal = (
        detect_traditional_acceptance(last, atr, traditional_levels, call_ok, put_ok)
        or detect_traditional_rejection(last, rng, traditional_levels, call_ok, put_ok)
        or detect_traditional_continuation(last, atr, traditional_levels, call_ok, put_ok, bias)
    )
    if trad_signal:
        side, reason = trad_signal
        if side != "HOLD":
            if (side == "CALL" and higher_tf_trend == "UP") or (side == "PUT" and higher_tf_trend == "DOWN"):
                return _make_state(side, reason, candles_3m, atr, last, prev)

    # --- Try Pivot ---
    pivot_signal = (
        detect_pivot_acceptance(last, prev, atr, traditional_levels, call_ok, put_ok)
        or detect_pivot_rejection(last, rng, traditional_levels, call_ok, put_ok)
        or detect_pivot_continuation(last, atr, traditional_levels, call_ok, put_ok, bias)
    )
    if pivot_signal:
        side, reason = pivot_signal
        if side != "HOLD":
            if (side == "CALL" and higher_tf_trend == "UP") or (side == "PUT" and higher_tf_trend == "DOWN"):
                return _make_state(side, reason, candles_3m, atr, last, prev)

    return None


def _make_state(side, reason, candles_3m, atr, last, prev):
    ok, momentum = momentum_ok(candles_3m, side)
    if not ok or momentum is None:
        return None
    return {
        "side": side,
        "reason": reason,
        "entry_candle": len(candles_3m) - 1,
        "peak_momentum": abs(momentum),
        "peak_candle": len(candles_3m) - 1,
        "prev_gap": abs(last.close - prev.close),
        "atr_entry": atr,
        "plateau_count": 0
    }

# def check_exit_condition(df_slice, state):
#     """
#     Polls trade state and decides exit.
#     Returns True if exit triggered, else False.
#     """

#     i = len(df_slice) - 1
#     side = state["side"]

#     # Minimum hold period
#     if i - state["entry_candle"] < 5:
#         return False

#     # EMA gap
#     ema9 = df_slice["close"].ewm(span=9, adjust=False).mean().iloc[-1]
#     ema13 = df_slice["close"].ewm(span=13, adjust=False).mean().iloc[-1]
#     ema_gap = abs(ema9 - ema13)

#     ok, momentum = momentum_ok(df_slice, side)
#     if not ok or momentum is None:
#         momentum = 0

#     # Update peak momentum if gap widens
#     if ema_gap > state["prev_gap"]:
#         state["prev_gap"] = ema_gap
#         if abs(momentum) > state["peak_momentum"]:
#             state["peak_momentum"] = abs(momentum)
#             state["peak_candle"] = i
#         state["plateau_count"] = 0
#         return False

#     # Plateau detection
#     if ema_gap <= state["prev_gap"]:
#         state["plateau_count"] += 1
#     else:
#         state["plateau_count"] = 0

#     # Exit condition: sustained plateau + momentum drop
#     if state["plateau_count"] >= 2 and abs(momentum) < state["peak_momentum"] * 0.4:
#         logging.info(f"[EXIT] EMA plateau + sustained momentum drop side={side} "
#                      f"peak={state['peak_momentum']} current={momentum}")
#         return True

#     # Fallback exit: CCI/W%R
#     cci_series = calculate_cci(df_slice)
#     cci_val = cci_series.iloc[-1] if not cci_series.empty else None
#     wr_val = williams_r(df_slice)

#     if side == "CALL" and ((cci_val is not None and cci_val > 120) or (wr_val is not None and wr_val < -85)):
#         logging.info(f"[EXIT] CCI/W%R trigger side={side} CCI={cci_val} W%R={wr_val}")
#         return True
#     if side == "PUT" and ((cci_val is not None and cci_val < -120) or (wr_val is not None and wr_val > -15)):
#         logging.info(f"[EXIT] CCI/W%R trigger side={side} CCI={cci_val} W%R={wr_val}")
#         return True

#     return False


# def check_exit_condition(df_slice, state):
#     """
#     Hybrid exit logic:
#     - ATR-based dynamic levels (stop-loss, partial/full targets, trailing stop)
#     - EMA plateau + momentum drop + oscillator confirmation
#     - Time-based guard
#     Returns: (bool, reason_str)
#     """

#     i = len(df_slice) - 1
#     side = state["side"]
#     entry_price = state.get("buy_price")
#     current_price = df_slice["close"].iloc[-1]

#     # Minimum hold period
#     if i - state["entry_candle"] < 5:
#         return False, None

#     # EMA gap
#     ema9 = df_slice["close"].ewm(span=9, adjust=False).mean().iloc[-1]
#     ema13 = df_slice["close"].ewm(span=13, adjust=False).mean().iloc[-1]
#     ema_gap = abs(ema9 - ema13)

#     ok, momentum = momentum_ok(df_slice, side)
#     if not ok or momentum is None:
#         momentum = 0

#     # ATR-based dynamic levels (must be precomputed at entry)
#     stop = state.get("stop")
#     pt = state.get("pt")
#     tg = state.get("tg")
#     trail_start = state.get("trail_start")
#     trail_step = state.get("trail_step")

#     # --- ATR exits ---
#     if stop and side == "CALL" and current_price <= stop:
#         logging.info(f"[EXIT] ATR Stop Loss hit side={side} stop={stop} current={current_price}")
#         return True, "ATR_EXIT"
#     if stop and side == "PUT" and current_price >= stop:
#         logging.info(f"[EXIT] ATR Stop Loss hit side={side} stop={stop} current={current_price}")
#         return True, "ATR_EXIT"

#     if tg and side == "CALL" and current_price >= tg:
#         logging.info(f"[EXIT] ATR Full Target hit side={side} target={tg} current={current_price}")
#         return True, "ATR_EXIT"
#     if tg and side == "PUT" and current_price <= tg:
#         logging.info(f"[EXIT] ATR Full Target hit side={side} target={tg} current={current_price}")
#         return True, "ATR_EXIT"

#     # Partial target reached → activate trailing stop
#     if pt:
#         if side == "CALL" and current_price >= pt:
#             state["stop"] = update_trailing_stop(current_price, entry_price, state["stop"],
#                                                 trail_start, trail_step)
#         if side == "PUT" and current_price <= pt:
#             state["stop"] = update_trailing_stop(entry_price, current_price, state["stop"],
#                                                 trail_start, trail_step)

#     # --- Momentum exits ---
#     if ema_gap > state["prev_gap"]:
#         state["prev_gap"] = ema_gap
#         if abs(momentum) > state["peak_momentum"]:
#             state["peak_momentum"] = abs(momentum)
#             state["peak_candle"] = i
#         state["plateau_count"] = 0
#         return False, None

#     if ema_gap <= state["prev_gap"]:
#         state["plateau_count"] += 1
#     else:
#         state["plateau_count"] = 0

#     if state["plateau_count"] >= 2 and abs(momentum) < state["peak_momentum"] * 0.4:
#         cci_series = calculate_cci(df_slice)
#         cci_val = cci_series.iloc[-1] if not cci_series.empty else None
#         wr_val = williams_r(df_slice)

#         if side == "CALL" and ((cci_val is not None and cci_val > 120) or (wr_val is not None and wr_val < -85)):
#             logging.info(f"[EXIT] EMA plateau + momentum drop + oscillator confirm side={side} peak={state['peak_momentum']} current={momentum}")
#             return True, "MOMENTUM_EXIT"
#         if side == "PUT" and ((cci_val is not None and cci_val < -120) or (wr_val is not None and wr_val > -15)):
#             logging.info(f"[EXIT] EMA plateau + momentum drop + oscillator confirm side={side} peak={state['peak_momentum']} current={momentum}")
#             return True, "MOMENTUM_EXIT"

#     # --- Time guard ---
#     if i - state["entry_candle"] >= 15:
#         logging.info(f"[EXIT] Max hold time exceeded side={side} entry_candle={state['entry_candle']} current_candle={i}")
#         return True, "TIME_EXIT"

#     return False, None