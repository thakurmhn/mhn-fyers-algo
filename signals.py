# # ===== signals.py =====
# import logging
# from setup import spot_price, hist_data
# from indicators import momentum_ok, resample_to_15m, check_bias
# from config import CANDLE_BODY_RANGE, ATR_VALUE

# # ===========================================================
# # ANSI COLORS for order logs
# RESET   = "\033[0m"
# GREEN   = "\033[92m"
# YELLOW  = "\033[93m"
# RED     = "\033[91m"
# MAGENTA = "\033[95m"
# GRAY    = "\033[90m"
# # ===========================================================

# logging.info(
#     f"{YELLOW}[BIAS CHECK] bias={bias}{RESET}"
#     )


# def detect_signal(cpr_levels, traditional_levels, camarilla_levels, atr, candles_3m_):
#     logging.info(f"{YELLOW}[DETECT_SIGNAL CALLED] candles={len(candles_3m_)} atr={atr}{RESET}")

#     # ---- Guards ----
#     if len(candles_3m_) < 2 or atr is None:
#         return None

#     # ---- Volatility Regime Filter ----
#     if atr < ATR_VALUE:  #                     
#         logging.info(f"{MAGENTA}[SIGNAL FILTERED] ATR too low ({atr:.2f}), skipping trade{RESET}")
#         return None
#     if atr > 120:
#         logging.info(f"{MAGENTA}[SIGNAL FILTERED] ATR too high ({atr:.2f}), skipping trade{RESET}")
#         return None

#     last = candles_3m_.iloc[-1]
#     prev = candles_3m_.iloc[-2]

#     body = abs(last.close - last.open)
#     rng  = last.high - last.low
#     if rng == 0:
#         return None

#     # ---- Levels ----
#     pivot = traditional_levels["pivot"]
#     r1, s1, r2, s2 = (
#         traditional_levels["r1"], traditional_levels["s1"],
#         traditional_levels["r2"], traditional_levels["s2"],
#     )
#     r3, r4, s3, s4 = (
#         camarilla_levels["r3"], camarilla_levels["r4"],
#         camarilla_levels["s3"], camarilla_levels["s4"],
#     )
#     tc, bc = cpr_levels["tc"], cpr_levels["bc"]

#     # ---- Strength + Momentum ----
#     def strong(side):
#         mom_ok, momentum = momentum_ok(candles_3m_, side)
#         strength_ok = (body / rng) > CANDLE_BODY_RANGE               
#         return strength_ok and mom_ok, momentum

#     call_ok, call_momentum = strong("CALL")
#     put_ok,  put_momentum  = strong("PUT")

#     # ---- DEBUG LOG ----
#     logging.info(
#         f"{YELLOW}[SIGNAL CHECK] "
#         f"close={last.close:.2f} spot={spot_price:.2f} "
#         f"ATR={atr:.2f} body/range={body/rng:.2f} "
#         f"CALL_mom={call_momentum:.2f} PUT_mom={put_momentum:.2f}{RESET}"
#     )

#     # ===============================
#     # Priority 1: CPR
#     # ===============================
#     if last.close > tc + 0.1 * atr and call_ok:
#         hist_data_15m = resample_to_15m(hist_data)
#         bias = check_bias(hist_data_15m)
#         logging.info(f"[BIAS CHECK] side=CALL bias={bias}")
#         if bias == "BULLISH":
#             return "CALL", "BREAKOUT_CPR_TC"
#         else:
#             logging.info("[SIGNAL BLOCKED] CALL skipped due to bias mismatch")
#             return None

#     if last.close < bc - 0.1 * atr and put_ok:
#         hist_data_15m = resample_to_15m(hist_data)
#         bias = check_bias(hist_data_15m)
#         logging.info(f"[BIAS CHECK] side=PUT bias={bias}")
#         if bias == "BEARISH":
#             return "PUT", "BREAKOUT_CPR_BC"
#         else:
#             logging.info("[SIGNAL BLOCKED] PUT skipped due to bias mismatch")
#             return None
        
#     # ===============================
#     # Priority 2: Camarilla
#     # ===============================
#     if last.close > r3 + 0.1 * atr and call_ok:
#         return "CALL", "BREAKOUT_R3"
#     if last.close > r4 + 0.1 * atr and call_ok:
#         return "CALL", "BREAKOUT_R4"
#     if last.close < s3 - 0.1 * atr and put_ok:
#         return "PUT", "BREAKOUT_S3"
#     if last.close < s4 - 0.1 * atr and put_ok:
#         return "PUT", "BREAKOUT_S4"

#     if last.low <= s3 and (last.close - last.low) > 0.5 * rng and call_ok:
#         return "CALL", "REJECTION_S3"
#     if last.low <= s4 and (last.close - last.low) > 0.5 * rng and call_ok:
#         return "CALL", "REJECTION_S4"
#     if last.high >= r3 and (last.high - last.close) > 0.5 * rng and put_ok:
#         return "PUT", "REJECTION_R3"
#     if last.high >= r4 and (last.high - last.close) > 0.5 * rng and put_ok:
#         return "PUT", "REJECTION_R4"

#     # ===============================
#     # Continuation helpers
#     # ===============================
#     def continuation_long(level):
#         return last.low <= level and last.close > level + 0.05 * atr
#     def continuation_short(level):
#         return last.high >= level and last.close < level - 0.05 * atr

#     # Continuation signals
#     if continuation_long(r4) and call_ok:
#         return "CALL", "CONTINUATION_R4"
#     if continuation_short(s4) and put_ok:
#         return "PUT", "CONTINUATION_S4"

#     # ===============================
#     # Priority 3: Traditional
#     # ===============================
#     if last.close > r2 + 0.1 * atr and call_ok:
#         return "CALL", "BREAKOUT_R2"
#     if last.close < s2 - 0.1 * atr and put_ok:
#         return "PUT", "BREAKOUT_S2"

#     if last.low <= s1 and (last.close - last.low) > 0.5 * rng and call_ok:
#         return "CALL", "REJECTION_S1"
#     if last.high >= r1 and (last.high - last.close) > 0.5 * rng and put_ok:
#         return "PUT", "REJECTION_R1"

#     # ===============================
#     # Priority 4: Pivot
#     # ===============================
#     if prev.close < pivot and last.close > pivot + 0.1 * atr and call_ok:
#         return "CALL", "BREAKOUT_PIVOT"
#     if prev.close > pivot and last.close < pivot - 0.1 * atr and put_ok:
#         return "PUT", "BREAKOUT_PIVOT"

#     return None

# ===== signals.py =====
import logging
import pandas as pd
from setup import spot_price, hist_data
from indicators import momentum_ok, check_bias
from config import CANDLE_BODY_RANGE, ATR_VALUE
from indicators import momentum_ok, check_bias, get_today_15m_candles


# ===========================================================
# ANSI COLORS for order logs
RESET   = "\033[0m"
GREEN   = "\033[92m"
YELLOW  = "\033[93m"
RED     = "\033[91m"
MAGENTA = "\033[95m"
GRAY    = "\033[90m"
# ===========================================================

def detect_signal(cpr_levels, traditional_levels, camarilla_levels, atr, candles_3m_, hist_yesterday_15m=None):
    """
    Detect trading signals based on pivots, ATR, and bias alignment.
    """

    # Build today's 15m candles from live 3m data
    hist_today_15m = get_today_15m_candles(candles_3m_)

    # Combine yesterday + today for full bias context
    if hist_yesterday_15m is not None and not hist_yesterday_15m.empty:
        full_hist = pd.concat([hist_yesterday_15m, hist_today_15m]).reset_index(drop=True)
    else:
        full_hist = hist_today_15m

    logging.info(f"{YELLOW}[DETECT_SIGNAL CALLED] 3m_candles={len(candles_3m_)} 15m_candles={len(full_hist)} atr={atr}{RESET}")

    # ---- Guards ----
    if len(candles_3m_) < 2 or atr is None:
        return None

    # ---- Volatility Regime Filter ----
    if atr < ATR_VALUE:
        logging.info(f"{MAGENTA}[SIGNAL FILTERED] ATR too low ({atr:.2f}), skipping trade{RESET}")
        return None
    if atr > 120:
        logging.info(f"{MAGENTA}[SIGNAL FILTERED] ATR too high ({atr:.2f}), skipping trade{RESET}")
        return None

    last = candles_3m_.iloc[-1]
    prev = candles_3m_.iloc[-2]

    body = abs(last.close - last.open)
    rng  = last.high - last.low
    if rng == 0:
        return None

    # ---- Levels ----
    pivot = traditional_levels["pivot"]
    r1, s1, r2, s2 = (
        traditional_levels["r1"], traditional_levels["s1"],
        traditional_levels["r2"], traditional_levels["s2"],
    )
    r3, r4, s3, s4 = (
        camarilla_levels["r3"], camarilla_levels["r4"],
        camarilla_levels["s3"], camarilla_levels["s4"],
    )
    tc, bc = cpr_levels["tc"], cpr_levels["bc"]

    # ---- Strength + Momentum ----
    def strong(side):
        mom_ok, momentum = momentum_ok(candles_3m_, side)
        strength_ok = (body / rng) > CANDLE_BODY_RANGE
        return strength_ok and mom_ok, momentum

    call_ok, call_momentum = strong("CALL")
    put_ok,  put_momentum  = strong("PUT")

    logging.info(
        f"{YELLOW}[SIGNAL CHECK] "
        f"close={last.close:.2f} spot={spot_price:.2f} "
        f"ATR={atr:.2f} body/range={body/rng:.2f} "
        f"CALL_mom={call_momentum:.2f} PUT_mom={put_momentum:.2f}{RESET}"
    )

    # Helper for bias validation
    def bias_ok(side, hist_data_15m):
        bias = check_bias(hist_data_15m)
        logging.info(f"{YELLOW}[BIAS CHECK] side={side} bias={bias}{RESET}")
        if bias == "NEUTRAL":
            return False
        return (side == "CALL" and bias == "BULLISH") or (side == "PUT" and bias == "BEARISH")

    # ===============================
    # Priority 1: CPR
    # ===============================
    if last.close > tc + 0.1 * atr and call_ok and bias_ok("CALL", full_hist):
        return "CALL", "BREAKOUT_CPR_TC"

    if last.close < bc - 0.1 * atr and put_ok and bias_ok("PUT", full_hist):
        return "PUT", "BREAKOUT_CPR_BC"

    # ===============================
    # Priority 2: Camarilla (Breakouts + Rejections)
    # ===============================
    if last.close > r3 + 0.1 * atr and call_ok and bias_ok("CALL", full_hist):
        return "CALL", "BREAKOUT_R3"

    if last.close > r4 + 0.1 * atr and call_ok and bias_ok("CALL", full_hist):
        return "CALL", "BREAKOUT_R4"

    if last.close < s3 - 0.1 * atr and put_ok and bias_ok("PUT", full_hist):
        return "PUT", "BREAKOUT_S3"

    if last.close < s4 - 0.1 * atr and put_ok and bias_ok("PUT", full_hist):
        return "PUT", "BREAKOUT_S4"

    # Rejections
    if last.low <= s3 and (last.close - last.low) > 0.5 * rng and call_ok and bias_ok("CALL", full_hist):
        return "CALL", "REJECTION_S3"

    if last.low <= s4 and (last.close - last.low) > 0.5 * rng and call_ok and bias_ok("CALL", full_hist):
        return "CALL", "REJECTION_S4"

    if last.high >= r3 and (last.high - last.close) > 0.5 * rng and put_ok and bias_ok("PUT", full_hist):
        return "PUT", "REJECTION_R3"

    if last.high >= r4 and (last.high - last.close) > 0.5 * rng and put_ok and bias_ok("PUT", full_hist):
        return "PUT", "REJECTION_R4"

    # ===============================
    # Continuation helpers
    # ===============================
    def continuation_long(level):
        return last.low <= level and last.close > level + 0.05 * atr

    def continuation_short(level):
        return last.high >= level and last.close < level - 0.05 * atr

    if continuation_long(r4) and call_ok and bias_ok("CALL", full_hist):
        return "CALL", "CONTINUATION_R4"

    if continuation_short(s4) and put_ok and bias_ok("PUT", full_hist):
        return "PUT", "CONTINUATION_S4"

    # ===============================
    # Priority 3: Traditional
    # ===============================
    if last.close > r2 + 0.1 * atr and call_ok and bias_ok("CALL", full_hist):
        return "CALL", "BREAKOUT_R2"

    if last.close < s2 - 0.1 * atr and put_ok and bias_ok("PUT", full_hist):
        return "PUT", "BREAKOUT_S2"

    if last.low <= s1 and (last.close - last.low) > 0.5 * rng and call_ok and bias_ok("CALL", full_hist):
        return "CALL", "REJECTION_S1"

    if last.high >= r1 and (last.high - last.close) > 0.5 * rng and put_ok and bias_ok("PUT", full_hist):
        return "PUT", "REJECTION_R1"

    # ===============================
    # Priority 4: Pivot
    # ===============================
    if prev.close < pivot and last.close > pivot + 0.1 * atr and call_ok and bias_ok("CALL", full_hist):
        return "CALL", "BREAKOUT_PIVOT"

    if prev.close > pivot and last.close < pivot - 0.1 * atr and put_ok and bias_ok("PUT", full_hist):
        return "PUT", "BREAKOUT_PIVOT"

    # ---- No signal matched ----
    return None