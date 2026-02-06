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
    check_bias,
    momentum_ok,
    oscillator_entry_filter,
    oscillator_exit_trigger,
    williams_r,
    supertrend
)


# ANSI COLORS
RESET   = "\033[0m"
GREEN   = "\033[92m"
YELLOW  = "\033[93m"
RED     = "\033[91m"
MAGENTA = "\033[95m"
GRAY    = "\033[90m"
CYAN    = "\033[96m"


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
    logging.info(f"{CYAN}[ATR LEVELS] side={side} Entry={entry_price:.2f} ATR={atr_val:.2f} SL={sl:.2f} PT={pt:.2f} TG={tg:.2f}{RESET}")
    return sl, pt, tg


def evaluate_candle(ts, row, candles, resolution, spot, side, candles_15m=None):
    try:
        # --- ATR from current resolution candles (e.g. 3m) ---
        atr_val = calculate_atr(candles)  # rolling ATR up to this candle
        atr_str = f"{atr_val:.2f}" if atr_val is not None else "NA"
        logging.info(f"[SIGNAL EVAL][{resolution}] candle={ts} candles={len(candles)} atr={atr_str} source=ATR_{resolution.upper()}")

        # --- Bias check only if 15m candles provided ---
        bias = None
        if candles_15m is not None and isinstance(candles_15m, pd.DataFrame):
            logging.debug(f"[BIAS PREVIEW @EVAL]\n{candles_15m.tail(3)}")
            # ✅ Use intraday ATR instead of recomputing daily ATR
            bias = check_bias(candles_15m, daily_atr=atr_val)
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


def resolve_bias(candles_15m, daily_atr=None, fallback_atr=None):
    """
    Resolve bias using 15m candles and ATR.
    - daily_atr: float (optional override, e.g. daily ATR)
    - fallback_atr: float (intraday ATR to use if daily_atr is None)
    """
    logging.debug(
        f"[RESOLVE BIAS INPUT] candles_15m type={type(candles_15m)} "
        f"daily_atr type={type(daily_atr)} fallback_atr type={type(fallback_atr)}"
    )

    if candles_15m is None or not isinstance(candles_15m, pd.DataFrame):
        logging.error("[RESOLVE BIAS] Invalid input: candles_15m is not a DataFrame")
        return None

    try:
        # ✅ Always ensure ATR is passed (daily override or fallback intraday)
        atr_val = daily_atr if daily_atr is not None else fallback_atr
        bias = check_bias(candles_15m, daily_atr=atr_val)

        if bias == "NEUTRAL":
            logging.debug(
                f"[BIAS CALL GUARD] candles_15m len={len(candles_15m)} ATR used={atr_val}"
            )
            logging.info("[SIGNAL FILTERED] Bias neutral, skipping trade")
            return None

        logging.info(f"[BIAS RESULT] {bias} (ATR={atr_val if atr_val else 'NA'})")
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
        logging.info(f"{CYAN}[SIGNAL FILTERED] {side} blocked by oscillator{RESET}")
        return False
    return True

import logging

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


def bias_from_indicators(row, df_15m=None):
    reason, score = [], 0

    # EMA crossover
    if row['ema20'] > row['ema50']:
        reason.append("EMA20>EMA50"); score += 20
    elif row['ema20'] < row['ema50']:
        reason.append("EMA20<EMA50"); score += 20

    # Supertrend (3m bias + slope)
    st_bias = row.get('supertrend_bias')
    st_slope = row.get('supertrend_slope')
    if st_bias == "BULLISH":
        reason.append("3m Supertrend=UP"); score += 20
    elif st_bias == "BEARISH":
        reason.append("3m Supertrend=DOWN"); score += 20
    if st_slope:
        reason.append(f"Slope={st_slope}")

    # Confidence boost/penalty based on slope
    if st_slope == "UP" and st_bias == "BULLISH":
        score += 10
    elif st_slope == "DOWN" and st_bias == "BEARISH":
        score += 10
    elif st_slope == "FLAT":
        score -= 10

    # ADX
    if row['adx14'] > 20:
        reason.append("ADX strong"); score += 20

    # CCI
    if row['cci20'] > 50:
        reason.append("CCI>50"); score += 20
    elif row['cci20'] < -50:
        reason.append("CCI<-50"); score += 20

    # Multi-timeframe Supertrend (15m bias + slope)
    st_15m_bias, st_15m_slope = None, None
    if df_15m is not None and not df_15m.empty:
        st_15m_bias = df_15m.iloc[-1].get('supertrend_bias')
        st_15m_slope = df_15m.iloc[-1].get('supertrend_slope')
        if st_15m_bias:
            reason.append(f"15m Supertrend={st_15m_bias}")
        if st_15m_slope:
            reason.append(f"15m Slope={st_15m_slope}")

    # Decision with slope filter
    if (st_bias == "BULLISH" and st_slope == "UP" and st_15m_bias == "BULLISH" and st_15m_slope == "UP"
        and row['close'] > row['ema20']
        and row['cci20'] > 50
        and row['adx14'] > 20):
        return "CALL BUY | " + ", ".join(reason), score

    elif (st_bias == "BEARISH" and st_slope == "DOWN" and st_15m_bias == "BEARISH" and st_15m_slope == "DOWN"
          and row['close'] < row['ema20']
          and row['cci20'] < -50
          and row['adx14'] > 20):
        return "PUT SELL | " + ", ".join(reason), score

    else:
        return "HOLD | " + ", ".join(reason), score

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


# def detect_signal(cpr_levels, traditional_levels, camarilla_levels,
#                   candles_3m, candles_15m, spot_price=None, daily_atr=None):

#     # --- Guard clauses ---
#     if candles_3m is None or len(candles_3m) < 2:
#         logging.warning(f"{CYAN}[SIGNAL] Not enough 3m candles to evaluate{RESET}")
#         return None
#     if candles_15m is None or not isinstance(candles_15m, pd.DataFrame) or candles_15m.empty:
#         logging.warning(f"{CYAN}[SIGNAL] No 15m candles available for bias check{RESET}")
#         return None

#     # --- Resolve ATR ---
#     atr, atr_source = resolve_atr(candles_3m, daily_atr)
#     atr = to_scalar(atr)
#     if atr is None:
#         logging.info(f"{CYAN}[SIGNAL FILTERED] ATR unavailable, skipping{RESET}")
#         return None
#     atr_str = f"{atr:.2f}" if atr is not None else "NA"
#     logging.info(f"{CYAN}[ATR] {atr_str} (source={atr_source}){RESET}")

#     # --- Bias check (always use 15m candles) ---
#     try:
#         bias = check_bias(candles_15m, daily_atr=daily_atr)
#         logging.info(f"{CYAN}[BIAS RESULT] {bias if bias else 'NA'}{RESET}")
#     except Exception as e:
#         logging.error(f"{RED}[BIAS ERROR] Failed bias check: {e}{RESET}")
#         return None

#     if bias is None or bias == "NEUTRAL":
#         logging.info(f"{CYAN}[SIGNAL FILTERED] Bias neutral, skipping trade{RESET}")
#         return None

#     # --- Supertrend slope diagnostic ---
#     try:
#         st_result = supertrend(candles_15m, atr_val=daily_atr)
#         st_bias = st_result.get("bias", "NEUTRAL")
#         st_slope = st_result.get("slope", "FLAT")
#         logging.info(f"{CYAN}[SUPERTREND] Bias={st_bias} Slope={st_slope}{RESET}")
#     except Exception as e:
#         logging.error(f"[SUPERTREND ERROR] {e}")
#         st_slope = "FLAT"

#     # --- Candle references ---
#     last = candles_3m.iloc[-1]
#     prev = candles_3m.iloc[-2]
#     rng  = to_scalar(last.high) - to_scalar(last.low)

#     logging.debug(f"{CYAN}[SIGNAL PREVIEW @3M]\n{candles_3m.tail(3)}{RESET}")

#     # --- Candle strength + momentum ---
#     call_ok, call_momentum = candle_strength(candles_3m, "CALL")
#     put_ok,  put_momentum  = candle_strength(candles_3m, "PUT")

#     # --- Williams %R entry blocking ---
#     wr = williams_r(candles_3m, period=14)
#     if not np.isnan(wr):
#         if bias == "BULLISH" and wr > -50:  # overbought
#             logging.info(f"{CYAN}[OSC FILTER] CALL blocked by W%R={wr:.2f} (overbought){RESET}")
#             call_ok = False
#         if bias == "BEARISH" and wr < -50:  # oversold
#             logging.info(f"{CYAN}[OSC FILTER] PUT blocked by W%R={wr:.2f} (oversold){RESET}")
#             put_ok = False

#     # --- Priority order ---
#     signal = (
#         detect_cpr(last, atr, cpr_levels, call_ok, put_ok) or
#         detect_camarilla(last, rng, atr, camarilla_levels, call_ok, put_ok) or
#         detect_traditional_acceptance(last, atr, traditional_levels, call_ok, put_ok) or
#         detect_traditional_rejection(last, rng, traditional_levels, call_ok, put_ok) or
#         detect_traditional_continuation(last, atr, traditional_levels, call_ok, put_ok) or
#         detect_pivot_acceptance(last, prev, atr, traditional_levels, call_ok, put_ok) or
#         detect_pivot_rejection(last, rng, traditional_levels, call_ok, put_ok) or
#         detect_pivot_continuation(last, atr, traditional_levels, call_ok, put_ok)
#     )

#     if signal:
#         side, reason = signal
#         spot_price = to_scalar(spot_price)
#         spot_str = f"{spot_price:.2f}" if spot_price is not None else "NA"
#         logging.info(
#             f"{YELLOW}[SIGNAL FOUND] side={side} reason={reason}{RESET} "
#             f"{YELLOW}Bias={bias} ATR={atr_str} W%R={wr:.2f if not np.isnan(wr) else 'NA'}{RESET} "
#             f"{YELLOW}SupertrendSlope={st_slope} spot={spot_str}{RESET}"
#         )
#         # --- Exit trigger diagnostic ---
#         if side == "CALL" and wr > -10:
#             logging.info(f"{CYAN}[EXIT TRIGGER] CALL exit due to W%R={wr:.2f} (overbought){RESET}")
#         if side == "PUT" and wr < -90:
#             logging.info(f"{CYAN}[EXIT TRIGGER] PUT exit due to W%R={wr:.2f} (oversold){RESET}")
#         return side, reason

#     logging.info(f"{CYAN}[NO SIGNAL] No valid breakout/rejection detected{RESET}")
#     return None

def classify_volatility(atr_val, thresholds=(15, 30)):
    """
    Classify volatility regime based on ATR.
    thresholds = (low, high)
    """
    if atr_val < thresholds[0]:
        return "LOW"
    elif atr_val < thresholds[1]:
        return "MEDIUM"
    else:
        return "HIGH"

def dynamic_targets(atr, side):
    # ATR multiples for different levels
    sl_val = atr * 1.5
    pt_val = atr * 1.0   # partial target (closer)
    tg_val = atr * 2.0   # final target (further)

    return {
        "SL": sl_val,
        "PT": pt_val,    # partial target
        "TG": tg_val     # final target
    }


def signal_confidence(vol_regime, reason):
    """
    Assign confidence score based on volatility regime and signal type.
    """
    if vol_regime == "HIGH" and "BREAKOUT" in reason:
        return "STRONG"
    elif vol_regime == "LOW" and "CONTINUATION" in reason:
        return "WEAK"
    return "MEDIUM"


def flip_signal(candles_3m, candles_15m, spot_price, atr, pivots,
                prev_day_high=None, prev_day_low=None):

    last3 = candles_3m.iloc[-1]
    last15 = candles_15m.iloc[-1]
    side = "NO_SIGNAL"
    score = 0

    # --- Stage 1: Trend foundation ---
    ema_bull = last3["ema20"] > last3["ema50"] and last15["ema20"] > last15["ema50"]
    ema_bear = last3["ema20"] < last3["ema50"] and last15["ema20"] < last15["ema50"]

    st_bull = (
        last3["supertrend_bias"] == "BULLISH" and
        last3["supertrend_slope"] == "UP" and
        last15["supertrend_bias"] == "BULLISH" and
        last15["supertrend_slope"] == "UP"
    )

    st_bear = (
        last3["supertrend_bias"] == "BEARISH" and
        last3["supertrend_slope"] == "DOWN" and
        last15["supertrend_bias"] == "BEARISH" and
        last15["supertrend_slope"] == "DOWN"
    )

    if ema_bull and st_bull:
        score += 4
        trend_bias = "BULLISH"
    elif ema_bear and st_bear:
        score += 4
        trend_bias = "BEARISH"
    else:
        trend_bias = "NEUTRAL"

    # --- Stage 2: Spot confirmation (pivot-based) ---
    pivot = pivots.get("pivot")
    if pivot is not None:
        if spot_price > pivot:
            score += 1
            spot_bias = "BULLISH"
        elif spot_price < pivot:
            score += 1
            spot_bias = "BEARISH"
        else:
            spot_bias = "NEUTRAL"
    else:
        spot_bias = "NEUTRAL"

    # --- Stage 3: Oscillator confirmation ---
    wr = williams_r(candles_3m, period=14)

    if atr < 20:
        cci_bull = last3["cci20"] > 70
        cci_bear = last3["cci20"] < -70
        wr_bull = wr < -60
        wr_bear = wr > -40
    else:
        cci_bull = last3["cci20"] > 30
        cci_bear = last3["cci20"] < -30
        wr_bull = wr < -40
        wr_bear = wr > -60

    if cci_bull and wr_bull:
        score += 2
        osc_bias = "BULLISH"
    elif cci_bear and wr_bear:
        score += 2
        osc_bias = "BEARISH"
    else:
        osc_bias = "NEUTRAL"

    # --- Stage 4: Volatility filter ---
    if atr > 15:
        score += 1

    # --- Stage 5: ADX filter ---
    adx_val = last3["adx14"]
    if not pd.isna(adx_val):
        if adx_val > 25:
            score += 2
        elif adx_val > 20 and (last3["adx14"] - candles_3m.iloc[-2]["adx14"]) > 0:
            score += 2

    # --- Structural zone gate ---
    key_levels = [
        pivots.get("bc"), pivots.get("tc"),
        pivots.get("r1"), pivots.get("s1"),
        pivots.get("r2"), pivots.get("s2"),
        pivots.get("r3"), pivots.get("s3"),
        prev_day_high, prev_day_low
    ]
    key_levels = [lvl for lvl in key_levels if lvl is not None]

    if key_levels:
        dist = min(abs(spot_price - lvl) for lvl in key_levels)
        if dist > 0.3 * atr:
            return "NO_SIGNAL", "NONE", score

    # --- Final decision ---
    if trend_bias == "BULLISH" and spot_bias == "BULLISH" and osc_bias == "BULLISH":
        side = "CALL"
    elif trend_bias == "BEARISH" and spot_bias == "BEARISH" and osc_bias == "BEARISH":
        side = "PUT"

    # --- Confidence bucket ---
    if score >= 10:
        confidence = "HIGH"
    elif score >= 6:
        confidence = "MEDIUM"
    elif score >= 3:
        confidence = "LOW"
    else:
        confidence = "NONE"

    if confidence in ("LOW", "NONE"):
        side = "NO_SIGNAL"

    return side, confidence, score



last_signal = "NONE"          # persistent state
last_eval_candle_time = None  # candle close tracking

def is_new_3m_candle_close(candles_3m):
    global last_eval_candle_time
    # ✅ use index (DatetimeIndex) instead of ["time"]
    current_time = candles_3m.index[-1]

    if last_eval_candle_time is None:
        last_eval_candle_time = current_time
        return True

    if current_time != last_eval_candle_time:
        last_eval_candle_time = current_time
        return True

    return False


def detect_signal(cpr_levels, traditional_levels, camarilla_levels,
                  candles_3m, candles_15m, spot_price=None, daily_atr=None,
                  prev_day_high=None, prev_day_low=None):
    """
    Structure-first signal engine with:
    - Candle-close gating
    - Bias validity override
    - Structural zone gating
    - Oscillator exhaustion filter
    - Flip logic as fallback only
    - Signal state machine
    """

    global last_signal

    # --- Guard clauses ---
    if candles_3m is None or len(candles_3m) < 5:
        logging.warning("[SIGNAL] Not enough 3m candles to evaluate")
        return None

    if candles_15m is None or candles_15m.empty:
        logging.warning("[SIGNAL] No 15m candles available")
        return None
   
    # --- Candle close gate ---
    if not is_new_3m_candle_close(candles_3m):
        return None

    # --- Resolve ATR ---
    atr, atr_source = resolve_atr(candles_3m, None)
    atr = to_scalar(atr)

    if atr is None:
        logging.info("[SIGNAL FILTERED] ATR unavailable")
        return None

    logging.info(f"[ATR] {atr:.2f} (source={atr_source})")

    # --- Bias pipeline ---
    try:
        raw_bias = check_bias(candles_15m, daily_atr=atr)

        if (
            pd.isna(candles_15m.iloc[-1]["adx14"]) or
            pd.isna(candles_15m.iloc[-1]["cci20"])
        ):
            final_bias = "NEUTRAL"
            logging.info(f"[BIAS RAW] {raw_bias} → [BIAS FINAL] NEUTRAL (HTF invalid)")
        else:
            final_bias = raw_bias
            logging.info(f"[BIAS FINAL] {final_bias}")

    except Exception as e:
        logging.error(f"[BIAS ERROR] {e}")
        final_bias = "NEUTRAL"

    # --- Supertrend diagnostic ---
    try:
        st_bias, st_slope = supertrend(candles_15m, atr_val=daily_atr)
    except Exception:
        st_bias, st_slope = "NEUTRAL", "FLAT"

    logging.info(f"[SUPERTREND] Bias={st_bias} Slope={st_slope}")

    # --- Candle references ---
    last = candles_3m.iloc[-1]
    prev = candles_3m.iloc[-2]
    rng = to_scalar(last.high) - to_scalar(last.low)

    # --- Pivot debug ---
    logging.info(
        f"[PIVOT DEBUG] close={last.close:.2f} "
        f"CPR={cpr_levels} TRAD={traditional_levels} CAM={camarilla_levels}"
    )

    # --- Candle strength ---
    call_ok, call_momentum = candle_strength(candles_3m, "CALL")
    put_ok,  put_momentum  = candle_strength(candles_3m, "PUT")

    # --- Bias gating ---
    if final_bias == "BULLISH":
        put_ok = False
    elif final_bias == "BEARISH":
        call_ok = False

    # --- Oscillator exhaustion filter ---
    wr = williams_r(candles_3m, period=14)
    if not np.isnan(wr):
        if wr < -90 and final_bias == "BEARISH":
            logging.info("[EXHAUSTION FILTER] Oversold zone, skipping signal")
            return None

    # --- Structural signal detection ---
    signal = (
        detect_cpr(last, atr, cpr_levels, call_ok, put_ok, final_bias) or
        detect_camarilla(last, rng, atr, camarilla_levels, call_ok, put_ok, final_bias) or
        detect_traditional_acceptance(last, atr, traditional_levels, call_ok, put_ok) or
        detect_traditional_rejection(last, rng, traditional_levels, call_ok, put_ok) or
        detect_traditional_continuation(last, atr, traditional_levels, call_ok, put_ok, final_bias) or
        detect_pivot_acceptance(last, prev, atr, traditional_levels, call_ok, put_ok) or
        detect_pivot_rejection(last, rng, traditional_levels, call_ok, put_ok) or
        detect_pivot_continuation(last, atr, traditional_levels, call_ok, put_ok, final_bias)
    )

    # --- Structural zone gate ---
    key_levels = [
        cpr_levels.get("bc"), cpr_levels.get("tc"),
        traditional_levels.get("r1"), traditional_levels.get("s1"),
        traditional_levels.get("r2"), traditional_levels.get("s2"),
        camarilla_levels.get("r3"), camarilla_levels.get("s3"),
        prev_day_high, prev_day_low
    ]
    key_levels = [lvl for lvl in key_levels if lvl is not None]

    if key_levels:
        dist = min(abs(to_scalar(spot_price) - lvl) for lvl in key_levels)
        if dist > 0.3 * atr:
            logging.info("[ZONE FILTER] Price mid-range, NO_SIGNAL")
            return None

    # --- Flip logic (fallback only) ---
    flip_side, flip_confidence, flip_score = flip_signal(
        candles_3m, candles_15m, spot_price, atr, cpr_levels,
        prev_day_high=prev_day_high,
        prev_day_low=prev_day_low
    )

    if not signal and flip_side in ("CALL", "PUT") and flip_confidence in ("MEDIUM", "HIGH"):
        side = flip_side
        confidence = flip_confidence
        reason = f"FLIP(score={flip_score})"
        targets = dynamic_targets(atr, side)

    elif signal:
        side, reason, targets, confidence = signal
    else:
        logging.info("[NO SIGNAL] No valid breakout/rejection detected")
        return None

    # --- Confidence gate ---
    if confidence not in ("MEDIUM", "HIGH"):
        logging.info(f"[SIGNAL FILTERED] side={side} confidence={confidence}")
        return None

    # --- Signal state machine ---
    if side == last_signal or side == "NO_SIGNAL":
        logging.info(f"[STATE MACHINE] Ignored repeat/no signal side={side}")
        return None

    last_signal = side

    # --- Final logging ---
    spot_price = to_scalar(spot_price)
    spot_str = f"{spot_price:.2f}" if spot_price is not None else "NA"
    wr_str = f"{wr:.2f}" if not np.isnan(wr) else "NA"
    vol_regime = classify_volatility(atr)

    logging.info(
        f"[SIGNAL FOUND] side={side} reason={reason} "
        f"Bias={final_bias} ATR={atr:.2f} VolRegime={vol_regime} "
        f"SL={targets['SL']:.2f} PT={targets['PT']:.2f} TG={targets['TG']:.2f} "
        f"Confidence={confidence} W%R={wr_str} "
        f"SupertrendSlope={st_slope} FlipScore={flip_score} "
        f"FlipConf={flip_confidence} spot={spot_str}"
    )

    return side, reason, targets, confidence
