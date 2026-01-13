# ===== signals.py =====
import logging
from setup import spot_price
from indicators import momentum_ok
import datetime
import config
import numpy as np
from config import TEST_MODE

MODE = TEST_MODE  # Change mode for Backtesting 


# ===========================================================
# ANSI COLORS for order logs
RESET   = "\033[0m"
GREEN   = "\033[92m"
YELLOW  = "\033[93m"
RED     = "\033[91m"
MAGENTA = "\033[95m"
GRAY    = "\033[90m"
CYAN    = "\033[96m"

#===========================================================
# Previously working detect_signal()
#==============================================================

# def detect_signal(cpr_levels, traditional_levels, camarilla_levels, atr, candles_3m_):
#     logging.info(
#         f"{YELLOW}[DETECT_SIGNAL CALLED] candles={len(candles_3m_)} atr={atr}{RESET}"
#     )

#     # ---- Guards ----
#     if len(candles_3m_) < 2 or atr is None:
#         return None

#     last = candles_3m_.iloc[-1]
#     prev = candles_3m_.iloc[-2]

#     body = abs(last.close - last.open)
#     rng  = last.high - last.low
#     if rng == 0:
#         return None

#     # ---- Levels (lowercase keys) ----
#     pivot = traditional_levels["pivot"]
#     r1, s1, r2, s2 = (
#         traditional_levels["r1"],
#         traditional_levels["s1"],
#         traditional_levels["r2"],
#         traditional_levels["s2"],
#     )
#     r3, r4, s3, s4 = (
#         camarilla_levels["r3"],
#         camarilla_levels["r4"],
#         camarilla_levels["s3"],
#         camarilla_levels["s4"],
#     )
#     tc, bc = cpr_levels["tc"], cpr_levels["bc"]

#     # ---- Strength + Momentum ----
#     def strong(side):
#         mom_ok, momentum = momentum_ok(candles_3m_, side)
#         strength_ok = (body / rng) > 0.6
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
#         return "CALL", "BREAKOUT_CPR_TC"

#     if last.close < bc - 0.1 * atr and put_ok:
#         return "PUT", "BREAKOUT_CPR_BC"

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

#     # ===============================
#     # Continuation signals
#     # ===============================
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

# ================================================================================

def detect_consolidation_breakout(
    candles,
    atr,
    spot,
    side_hint=None,
    win=8,
    k_atr=0.5,
    alpha=0.1,
    max_spot_div=10
):
    """
    Detect breakout/breakdown after price consolidation.
    Returns (signal_tuple, breakout_meta) or None.

    signal_tuple = ('CALL'|'PUT', 'CONSOLIDATION_BREAKOUT')
    breakout_meta = {
        "win": int,                # consolidation window size
        "range_atr_ratio": float,  # consolidation range / ATR
        "upper": float,            # upper band
        "lower": float             # lower band
    }
    """
    if atr is None or len(candles) < win + 2:
        return None

    # Exclude last candle for consolidation window
    window = candles.iloc[-win-1:-1]
    highs = window['high'].values
    lows  = window['low'].values
    closes = window['close'].values
    opens  = window['open'].values

    upper = highs.max()
    lower = lows.min()
    rng = upper - lower
    body_ratio = np.median(np.abs(closes - opens) / (highs - lows + 1e-6))
    touches = ((highs >= upper * 0.999).sum() + (lows <= lower * 1.001).sum())

    # Consolidation gate
    if rng > k_atr * atr or body_ratio > 0.35:
        logging.info(f"{CYAN}[CONSOLIDATION REJECTED] rng={rng:.2f} ({rng/atr:.2f}*ATR) body_ratio={body_ratio:.2f}{RESET}")
        return None

    rng_atr_ratio = rng / atr
    logging.info(f"{CYAN}[CONSOLIDATION] win={win} upper={upper:.2f} lower={lower:.2f}"
                 f"range={rng:.2f} ({rng_atr_ratio:.2f}*ATR) touches={touches}{RESET}")

    last = candles.iloc[-1]
    call_ok, call_mom = momentum_ok(candles, "CALL")
    put_ok,  put_mom  = momentum_ok(candles, "PUT")

    # Breakout checks
    call_break = last.close > (upper + alpha * atr)
    put_break  = last.close < (lower - alpha * atr)

    # Spot alignment (relax threshold: allow up to 0.75*ATR divergence)
    spot_div = abs(spot - last.close)
    dyn_spot_gate = max(max_spot_div, 0.75 * atr)
    if spot_div > dyn_spot_gate:
        logging.info(f"{CYAN}[BREAKOUT REJECTED] spot_misaligned div={spot_div:.2f} > {dyn_spot_gate:.2f} (dyn gate){RESET}")
        return None

    breakout_meta = {
        "win": win,
        "range_atr_ratio": rng_atr_ratio,
        "upper": upper,
        "lower": lower
    }

    # Decide side with explicit logs
    if call_break and call_ok:
        logging.info(f"{CYAN}[BREAKOUT_UP ACCEPTED] CALL close={last.close:.2f} > upper+{alpha}*ATR "
                     f"({upper:.2f}+{alpha*atr:.2f}) mom={call_mom:.2f} spot_div={spot_div:.2f}{RESET}")
        return (("CALL", "CONSOLIDATION_BREAKOUT"), breakout_meta)

    if put_break and put_ok:
        logging.info(f"{CYAN}[BREAKDOWN ACCEPTED] PUT close={last.close:.2f} < lower-{alpha}*ATR "
                     f"({lower:.2f}-{alpha*atr:.2f}) mom={put_mom:.2f} spot_div={spot_div:.2f}{RESET}")
        return (("PUT", "CONSOLIDATION_BREAKOUT"), breakout_meta)

    logging.info(f"{CYAN}[BREAKOUT REJECTED] no clean close beyond band or momentum weak{RESET}")
    return None

# =================================================================================
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
    # Relax threshold for decisive expansion candles in breakout/breakdown contexts
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

def is_strong_trade_replay(momentum, rng, atr, body_ratio,
                           min_momentum=15,   # relaxed from 30
                           max_range_factor=3.0,  # relaxed from 1.5–2.0
                           min_body_ratio=0.4):   # relaxed from 0.6–0.8
    """
    Replay-mode filter: looser thresholds to validate detection logic.
    """
    if momentum < min_momentum:
        return False, f"[REPLAY] momentum={momentum} < min={min_momentum}"
    if body_ratio < min_body_ratio:
        return False, f"[REPLAY] body_ratio={body_ratio} < {min_body_ratio}"
    if rng > max_range_factor * atr:
        return False, f"[REPLAY] rng={rng:.2f} > {max_range_factor}*ATR"
    return True, "[REPLAY] accepted"


# =====================================================================================================
# MODE = "replay"

def detect_signal(cpr_levels, traditional_levels, camarilla_levels, atr, candles_3m_, prev_day_levels, strong_trade_fn=None):
    if strong_trade_fn is None:
        strong_trade_fn = is_strong_trade_replay if MODE == "replay" else is_strong_trade

    """
    above is adjustment for backtesting
    """

    logging.info(f"{YELLOW}[DETECT_SIGNAL CALLED] candles={len(candles_3m_)} atr={atr}{RESET}")

    # ---- Guards ----
    if atr is None or len(candles_3m_) < 2:
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
        traditional_levels["r1"],
        traditional_levels["s1"],
        traditional_levels["r2"],
        traditional_levels["s2"],
    )
    r3, r4, s3, s4 = (
        camarilla_levels["r3"],
        camarilla_levels["r4"],
        camarilla_levels["s3"],
        camarilla_levels["s4"],
    )
    tc, bc = cpr_levels["tc"], cpr_levels["bc"]

    pd_high = prev_day_levels["high"]
    pd_low  = prev_day_levels["low"]

    # ---- Strength + Momentum ----
    def strong(side):
        mom_ok, momentum = momentum_ok(candles_3m_, side)
        strength_ok = (body / rng) > 0.6
        return strength_ok and mom_ok, momentum

    call_ok, call_momentum = strong("CALL")
    put_ok,  put_momentum  = strong("PUT")

    logging.info(
        f"{YELLOW}[SIGNAL CHECK] "
        f"close={last.close:.2f} spot={spot_price:.2f} "
        f"ATR={atr:.2f} body/range={body/rng:.2f} "
        f"CALL_mom={call_momentum:.2f} PUT_mom={put_momentum:.2f}{RESET}"
    )

    signal = None
    breakout_meta = None

    # ===============================
    # Priority 0: Consolidation Breakout/Breakdown
    # ===============================
    cons_result = detect_consolidation_breakout(candles_3m_, atr, spot_price)
    if cons_result:
        cons_signal, breakout_meta = cons_result
        if is_strong_trade(cons_signal, atr, max(call_momentum, put_momentum), spot_price, last.close):
            side_tag = "BREAKOUT_UP" if cons_signal[0] == "CALL" else "BREAKDOWN"
            logging.info(f"{CYAN}[SIGNAL ACCEPTED][{side_tag}][FILTER] {cons_signal}{RESET}")
            return cons_signal, breakout_meta
        else:
            side_tag = "BREAKOUT_UP" if cons_signal[0] == "CALL" else "BREAKDOWN"
            logging.info(f"{CYAN}[SIGNAL REJECTED][{side_tag}][FILTER] {cons_signal}{RESET}")
            return None

    # ===============================
    # Priority 1: CPR
    # ===============================
    if last.close > tc + 0.1 * atr and call_ok:
        signal = ("CALL", "BREAKOUT_CPR_TC")
    elif last.close < bc - 0.1 * atr and put_ok:
        signal = ("PUT", "BREAKOUT_CPR_BC")

    # ===============================
    # Priority 2: Camarilla
    # ===============================
    elif last.close > r3 + 0.1 * atr and call_ok:
        signal = ("CALL", "BREAKOUT_R3")
    elif last.close > r4 + 0.1 * atr and call_ok:
        signal = ("CALL", "BREAKOUT_R4")
    elif last.close < s3 - 0.1 * atr and put_ok:
        signal = ("PUT", "BREAKOUT_S3")
    elif last.close < s4 - 0.1 * atr and put_ok:
        signal = ("PUT", "BREAKOUT_S4")
    elif last.low <= s3 and (last.close - last.low) > 0.5 * rng and call_ok:
        signal = ("CALL", "REJECTION_S3")
    elif last.low <= s4 and (last.close - last.low) > 0.5 * rng and call_ok:
        signal = ("CALL", "REJECTION_S4")
    elif last.high >= r3 and (last.high - last.close) > 0.5 * rng and put_ok:
        signal = ("PUT", "REJECTION_R3")
    elif last.high >= r4 and (last.high - last.close) > 0.5 * rng and put_ok:
        signal = ("PUT", "REJECTION_R4")

    # ===============================
    # Continuation signals (single candle)
    # ===============================
    elif last.low <= r4 and last.close > r4 + 0.05 * atr and call_ok:
        signal = ("CALL", "CONTINUATION_R4")
    elif last.high >= s4 and last.close < s4 - 0.05 * atr and put_ok:
        signal = ("PUT", "CONTINUATION_S4")

    # ===============================
    # Priority 3: Traditional
    # ===============================
    elif last.close > r2 + 0.1 * atr and call_ok:
        signal = ("CALL", "BREAKOUT_R2")
    elif last.close < s2 - 0.1 * atr and put_ok:
        signal = ("PUT", "BREAKOUT_S2")
    elif last.low <= s1 and (last.close - last.low) > 0.5 * rng and call_ok:
        signal = ("CALL", "REJECTION_S1")
    elif last.high >= r1 and (last.high - last.close) > 0.5 * rng and put_ok:
        signal = ("PUT", "REJECTION_R1")

    # ===============================
    # Priority 4: Pivot
    # ===============================
    elif prev.close < pivot and last.close > pivot + 0.1 * atr and call_ok:
        signal = ("CALL", "BREAKOUT_PIVOT")
    elif prev.close > pivot and last.close < pivot - 0.1 * atr and put_ok:
        signal = ("PUT", "BREAKOUT_PIVOT")

    # ===============================
    # Priority 5: Previous Day High/Low
    # ===============================
    elif last.close > pd_high + 0.1 * atr and call_ok:
        signal = ("CALL", "BREAKOUT_PREV_HIGH")
    elif last.close < pd_low - 0.1 * atr and put_ok:
        signal = ("PUT", "BREAKOUT_PREV_LOW")
    elif last.low <= pd_low and (last.close - last.low) > 0.5 * rng and call_ok:
        signal = ("CALL", "REJECTION_PREV_LOW")
    elif last.high >= pd_high and (last.high - last.close) > 0.5 * rng and put_ok:
        signal = ("PUT", "REJECTION_PREV_HIGH")

    # ===============================
    # Fast-path continuation: multi-candle breakdown (PUT) and breakout (CALL)
    # ===============================
    if signal is None:
        # --- Downside continuation below S3 ---
        if last.close < s3 - 0.05 * atr and put_ok:
            prev_body = abs(prev.close - prev.open)
            prev_rng  = prev.high - prev.low
            last_body = abs(last.close - last.open)
            last_rng  = last.high - last.low

            if prev.close < s3 and (prev_body / max(prev_rng, 1e-6)) > 0.6 and (last_body / max(last_rng, 1e-6)) > 0.6:
                signal = ("PUT", "CONTINUATION_S3_BREAK")

        # --- Downside continuation below S4 ---
        elif last.close < s4 - 0.05 * atr and put_ok:
            prev_body = abs(prev.close - prev.open)
            prev_rng  = prev.high - prev.low
            last_body = abs(last.close - last.open)
            last_rng  = last.high - last.low

            if prev.close < s4 and (prev_body / max(prev_rng, 1e-6)) > 0.6 and (last_body / max(last_rng, 1e-6)) > 0.6:
                signal = ("PUT", "CONTINUATION_S4_BREAK")

        # --- Upside continuation above R3 ---
        elif last.close > r3 + 0.05 * atr and call_ok:
            prev_body = abs(prev.close - prev.open)
            prev_rng  = prev.high - prev.low
            last_body = abs(last.close - last.open)
            last_rng  = last.high - last.low

            if prev.close > r3 and (prev_body / max(prev_rng, 1e-6)) > 0.6 and (last_body / max(last_rng, 1e-6)) > 0.6:
                signal = ("CALL", "CONTINUATION_R3_BREAK")

        # --- Upside continuation above R4 ---
        elif last.close > r4 + 0.05 * atr and call_ok:
            prev_body = abs(prev.close - prev.open)
            prev_rng  = prev.high - prev.low
            last_body = abs(last.close - last.open)
            last_rng  = last.high - last.low

            if prev.close > r4 and (prev_body / max(prev_rng, 1e-6)) > 0.6 and (last_body / max(last_rng, 1e-6)) > 0.6:
                signal = ("CALL", "CONTINUATION_R4_BREAK")
    
    if signal:
        # Always compute rng and body_ratio from the last candle
        last = candles_3m_.iloc[-1]
        rng = last["high"] - last["low"]
        body = abs(last["close"] - last["open"])
        body_ratio = body / (rng + 1e-6)

        ok, reason = strong_trade_fn(
            max(call_momentum, put_momentum),  # momentum
            rng,
            atr,
            body_ratio
        )
        if ok:
            logging.info(f"{CYAN}[SIGNAL ACCEPTED][FILTER] {signal}{RESET}")
            return signal, None
        else:
            logging.info(f"{CYAN}[SIGNAL REJECTED][FILTER] {signal}{RESET} | {reason}")
            return None
    return None









