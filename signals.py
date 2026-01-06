# ===== signals.py =====
import logging
from setup import spot_price
from indicators import momentum_ok

def detect_signal(cpr_levels, traditional_levels, camarilla_levels, atr, candles_3m_):
    logging.info(
        f"[DETECT_SIGNAL CALLED] candles={len(candles_3m_)} atr={atr}"
    )

    # ---- Guards ----
    if len(candles_3m_) < 2 or atr is None:
        return None

    last = candles_3m_.iloc[-1]
    prev = candles_3m_.iloc[-2]

    body = abs(last.close - last.open)
    rng  = last.high - last.low
    if rng == 0:
        return None

    # ---- Levels ----
    pivot = traditional_levels["Pivot"]
    r1, s1, r2, s2 = (
        traditional_levels["R1"],
        traditional_levels["S1"],
        traditional_levels["R2"],
        traditional_levels["S2"],
    )
    r3, r4, s3, s4 = (
        camarilla_levels["R3"],
        camarilla_levels["R4"],
        camarilla_levels["S3"],
        camarilla_levels["S4"],
    )
    tc, bc = cpr_levels["TC"], cpr_levels["BC"]

    # ---- Strength + Momentum ----
    def strong(side):
        mom_ok, momentum = momentum_ok(candles_3m_, side)
        strength_ok = (body / rng) > 0.6
        return strength_ok and mom_ok, momentum

    call_ok, call_momentum = strong("CALL")
    put_ok,  put_momentum  = strong("PUT")

    # ---- DEBUG LOG ----
    logging.info(
        f"[SIGNAL CHECK] "
        f"close={last.close:.2f} spot={spot_price:.2f} "
        f"ATR={atr:.2f} body/range={body/rng:.2f} "
        f"CALL_mom={call_momentum:.2f} PUT_mom={put_momentum:.2f}"
    )

    # ===============================
    # Priority 1: CPR
    # ===============================
    if last.close > tc + 0.1 * atr and call_ok:
        return "CALL", "BREAKOUT_CPR_TC"

    if last.close < bc - 0.1 * atr and put_ok:
        return "PUT", "BREAKOUT_CPR_BC"

    # ===============================
    # Priority 2: Camarilla
    # ===============================
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
    
    # ===============================
    # Continuation helpers
    # ===============================
    def continuation_long(level):
        return last.low <= level and last.close > level + 0.05 * atr

    def continuation_short(level):
        return last.high >= level and last.close < level - 0.05 * atr

    # ===============================
    # Continuation signals
    # ===============================
    if continuation_long(r4) and call_ok:
        return "CALL", "CONTINUATION_R4"

    if continuation_short(s4) and put_ok:
        return "PUT", "CONTINUATION_S4"

    # ===============================
    # Priority 3: Traditional
    # ===============================
    if last.close > r2 + 0.1 * atr and call_ok:
        return "CALL", "BREAKOUT_R2"

    if last.close < s2 - 0.1 * atr and put_ok:
        return "PUT", "BREAKOUT_S2"

    if last.low <= s1 and (last.close - last.low) > 0.5 * rng and call_ok:
        return "CALL", "REJECTION_S1"

    if last.high >= r1 and (last.high - last.close) > 0.5 * rng and put_ok:
        return "PUT", "REJECTION_R1"

    # ===============================
    # Priority 4: Pivot
    # ===============================
    if prev.close < pivot and last.close > pivot + 0.1 * atr and call_ok:
        return "CALL", "BREAKOUT_PIVOT"

    if prev.close > pivot and last.close < pivot - 0.1 * atr and put_ok:
        return "PUT", "BREAKOUT_PIVOT"

    return None