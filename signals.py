# ===== signals.py =====
import logging
from setup import spot_price
from indicators import detect_candle_pattern_at_pivot, detect_confluence
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

# def detect_consolidation_breakout(
#     candles,
#     atr,
#     spot,
#     side_hint=None,
#     win=8,
#     k_atr=0.5,
#     alpha=0.1,
#     max_spot_div=10
# ):
#     """
#     Detect breakout/breakdown after price consolidation.
#     Returns (signal_tuple, breakout_meta) or None.

#     signal_tuple = ('CALL'|'PUT', 'CONSOLIDATION_BREAKOUT')
#     breakout_meta = {
#         "win": int,                # consolidation window size
#         "range_atr_ratio": float,  # consolidation range / ATR
#         "upper": float,            # upper band
#         "lower": float             # lower band
#     }
#     """
#     if atr is None or len(candles) < win + 2:
#         return None

#     # Exclude last candle for consolidation window
#     window = candles.iloc[-win-1:-1]
#     highs = window['high'].values
#     lows  = window['low'].values
#     closes = window['close'].values
#     opens  = window['open'].values

#     upper = highs.max()
#     lower = lows.min()
#     rng = upper - lower
#     body_ratio = np.median(np.abs(closes - opens) / (highs - lows + 1e-6))
#     touches = ((highs >= upper * 0.999).sum() + (lows <= lower * 1.001).sum())

#     # Consolidation gate
#     if rng > k_atr * atr or body_ratio > 0.35:
#         logging.info(f"{CYAN}[CONSOLIDATION REJECTED] rng={rng:.2f} ({rng/atr:.2f}*ATR) body_ratio={body_ratio:.2f}{RESET}")
#         return None

#     rng_atr_ratio = rng / atr
#     logging.info(f"{CYAN}[CONSOLIDATION] win={win} upper={upper:.2f} lower={lower:.2f}"
#                  f"range={rng:.2f} ({rng_atr_ratio:.2f}*ATR) touches={touches}{RESET}")

#     last = candles.iloc[-1]
#     call_ok, call_mom = momentum_ok(candles, "CALL")
#     put_ok,  put_mom  = momentum_ok(candles, "PUT")

#     # Breakout checks
#     call_break = last.close > (upper + alpha * atr)
#     put_break  = last.close < (lower - alpha * atr)

#     # Spot alignment (relax threshold: allow up to 0.75*ATR divergence)
#     spot_div = abs(spot - last.close)
#     dyn_spot_gate = max(max_spot_div, 0.75 * atr)
#     if spot_div > dyn_spot_gate:
#         logging.info(f"{CYAN}[BREAKOUT REJECTED] spot_misaligned div={spot_div:.2f} > {dyn_spot_gate:.2f} (dyn gate){RESET}")
#         return None

#     breakout_meta = {
#         "win": win,
#         "range_atr_ratio": rng_atr_ratio,
#         "upper": upper,
#         "lower": lower
#     }

#     # Decide side with explicit logs
#     if call_break and call_ok:
#         logging.info(f"{CYAN}[BREAKOUT_UP ACCEPTED] CALL close={last.close:.2f} > upper+{alpha}*ATR "
#                      f"({upper:.2f}+{alpha*atr:.2f}) mom={call_mom:.2f} spot_div={spot_div:.2f}{RESET}")
#         return (("CALL", "CONSOLIDATION_BREAKOUT"), breakout_meta)

#     if put_break and put_ok:
#         logging.info(f"{CYAN}[BREAKDOWN ACCEPTED] PUT close={last.close:.2f} < lower-{alpha}*ATR "
#                      f"({lower:.2f}-{alpha*atr:.2f}) mom={put_mom:.2f} spot_div={spot_div:.2f}{RESET}")
#         return (("PUT", "CONSOLIDATION_BREAKOUT"), breakout_meta)

#     logging.info(f"{CYAN}[BREAKOUT REJECTED] no clean close beyond band or momentum weak{RESET}")
#     return None

# =================================================================================
# def is_strong_trade(signal, atr, momentum, spot, candle_close):
#     """
#     Strong trade filter:
#     - ATR within acceptable band
#     - Adaptive momentum threshold for decisive breakdowns
#     - Dynamic spot alignment gate vs ATR
#     Uses expiry-day overrides from config.py automatically.
#     """
#     if not signal or atr is None:
#         return False

#     side, reason = signal

#     # thresholds from config.py (with sensible defaults)
#     base_min_momentum = getattr(config, "MIN_MOMENTUM", 25)
#     atr_min = 20
#     atr_max = getattr(config, "ATR_MAX", 80)

#     # ATR gate
#     if atr < atr_min or atr > atr_max:
#         logging.info(f"[STRONG REJECT] ATR gate atr={atr:.2f} not in [{atr_min},{atr_max}]")
#         return False

#     # Adaptive momentum gate:
#     # Relax threshold for decisive expansion candles in breakout/breakdown contexts
#     adaptive_min = base_min_momentum
#     if reason in ("CONSOLIDATION_BREAKOUT", "BREAKOUT_S3", "BREAKOUT_S4", "BREAKOUT_CPR_BC"):
#         adaptive_min = max(10, base_min_momentum - 8)

#     if momentum < adaptive_min:
#         logging.info(f"{CYAN}[STRONG REJECT] momentum={momentum:.2f} < min={adaptive_min}{RESET}")
#         return False

#     # Spot vs candle alignment (dynamic gate vs ATR)
#     dyn_spot_gate = max(10, 0.75 * atr)
#     if abs(spot - candle_close) > dyn_spot_gate:
#         logging.info(f"{CYAN}[STRONG REJECT] spot_div={abs(spot - candle_close):.2f} > {dyn_spot_gate:.2f}{RESET}")
#         return False

#     return True

# def is_strong_trade_replay(momentum, rng, atr, body_ratio,
#                            min_momentum=15,   # relaxed from 30
#                            max_range_factor=3.0,  # relaxed from 1.5–2.0
#                            min_body_ratio=0.4):   # relaxed from 0.6–0.8
#     """
#     Replay-mode filter: looser thresholds to validate detection logic.
#     """
#     if momentum < min_momentum:
#         return False, f"[REPLAY] momentum={momentum} < min={min_momentum}"
#     if body_ratio < min_body_ratio:
#         return False, f"[REPLAY] body_ratio={body_ratio} < {min_body_ratio}"
#     if rng > max_range_factor * atr:
#         return False, f"[REPLAY] rng={rng:.2f} > {max_range_factor}*ATR"
#     return True, "[REPLAY] accepted"


# =====================================================================================================
# MODE = "replay"

# def detect_signal(cpr_levels, traditional_levels, camarilla_levels, atr, candles_3m_, prev_day_levels, strong_trade_fn=None):
#     if strong_trade_fn is None:
#         strong_trade_fn = is_strong_trade_replay if MODE == "replay" else is_strong_trade

#     """
#     above is adjustment for backtesting
#     """

#     logging.info(f"{YELLOW}[DETECT_SIGNAL CALLED] candles={len(candles_3m_)} atr={atr}{RESET}")

#     # ---- Guards ----
#     if atr is None or len(candles_3m_) < 2:
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

#     pd_high = prev_day_levels["high"]
#     pd_low  = prev_day_levels["low"]

#     # ---- Strength + Momentum ----
#     def strong(side):
#         mom_ok, momentum = momentum_ok(candles_3m_, side)
#         strength_ok = (body / rng) > 0.6
#         return strength_ok and mom_ok, momentum

#     call_ok, call_momentum = strong("CALL")
#     put_ok,  put_momentum  = strong("PUT")

#     logging.info(
#         f"{YELLOW}[SIGNAL CHECK] "
#         f"close={last.close:.2f} spot={spot_price:.2f} "
#         f"ATR={atr:.2f} body/range={body/rng:.2f} "
#         f"CALL_mom={call_momentum:.2f} PUT_mom={put_momentum:.2f}{RESET}"
#     )

#     signal = None
#     breakout_meta = None

#     # ===============================
#     # Priority 0: Consolidation Breakout/Breakdown
#     # ===============================
#     cons_result = detect_consolidation_breakout(candles_3m_, atr, spot_price)
#     if cons_result:
#         cons_signal, breakout_meta = cons_result
#         if is_strong_trade(cons_signal, atr, max(call_momentum, put_momentum), spot_price, last.close):
#             side_tag = "BREAKOUT_UP" if cons_signal[0] == "CALL" else "BREAKDOWN"
#             logging.info(f"{CYAN}[SIGNAL ACCEPTED][{side_tag}][FILTER] {cons_signal}{RESET}")
#             return cons_signal, breakout_meta
#         else:
#             side_tag = "BREAKOUT_UP" if cons_signal[0] == "CALL" else "BREAKDOWN"
#             logging.info(f"{CYAN}[SIGNAL REJECTED][{side_tag}][FILTER] {cons_signal}{RESET}")
#             return None

#     # ===============================
#     # Priority 1: CPR
#     # ===============================
#     if last.close > tc + 0.1 * atr and call_ok:
#         signal = ("CALL", "BREAKOUT_CPR_TC")
#     elif last.close < bc - 0.1 * atr and put_ok:
#         signal = ("PUT", "BREAKOUT_CPR_BC")

#     # ===============================
#     # Priority 2: Camarilla
#     # ===============================
#     elif last.close > r3 + 0.1 * atr and call_ok:
#         signal = ("CALL", "BREAKOUT_R3")
#     elif last.close > r4 + 0.1 * atr and call_ok:
#         signal = ("CALL", "BREAKOUT_R4")
#     elif last.close < s3 - 0.1 * atr and put_ok:
#         signal = ("PUT", "BREAKOUT_S3")
#     elif last.close < s4 - 0.1 * atr and put_ok:
#         signal = ("PUT", "BREAKOUT_S4")
#     elif last.low <= s3 and (last.close - last.low) > 0.5 * rng and call_ok:
#         signal = ("CALL", "REJECTION_S3")
#     elif last.low <= s4 and (last.close - last.low) > 0.5 * rng and call_ok:
#         signal = ("CALL", "REJECTION_S4")
#     elif last.high >= r3 and (last.high - last.close) > 0.5 * rng and put_ok:
#         signal = ("PUT", "REJECTION_R3")
#     elif last.high >= r4 and (last.high - last.close) > 0.5 * rng and put_ok:
#         signal = ("PUT", "REJECTION_R4")

#     # ===============================
#     # Continuation signals (single candle)
#     # ===============================
#     elif last.low <= r4 and last.close > r4 + 0.05 * atr and call_ok:
#         signal = ("CALL", "CONTINUATION_R4")
#     elif last.high >= s4 and last.close < s4 - 0.05 * atr and put_ok:
#         signal = ("PUT", "CONTINUATION_S4")

#     # ===============================
#     # Priority 3: Traditional
#     # ===============================
#     elif last.close > r2 + 0.1 * atr and call_ok:
#         signal = ("CALL", "BREAKOUT_R2")
#     elif last.close < s2 - 0.1 * atr and put_ok:
#         signal = ("PUT", "BREAKOUT_S2")
#     elif last.low <= s1 and (last.close - last.low) > 0.5 * rng and call_ok:
#         signal = ("CALL", "REJECTION_S1")
#     elif last.high >= r1 and (last.high - last.close) > 0.5 * rng and put_ok:
#         signal = ("PUT", "REJECTION_R1")

#     # ===============================
#     # Priority 4: Pivot
#     # ===============================
#     elif prev.close < pivot and last.close > pivot + 0.1 * atr and call_ok:
#         signal = ("CALL", "BREAKOUT_PIVOT")
#     elif prev.close > pivot and last.close < pivot - 0.1 * atr and put_ok:
#         signal = ("PUT", "BREAKOUT_PIVOT")

#     # ===============================
#     # Priority 5: Previous Day High/Low
#     # ===============================
#     elif last.close > pd_high + 0.1 * atr and call_ok:
#         signal = ("CALL", "BREAKOUT_PREV_HIGH")
#     elif last.close < pd_low - 0.1 * atr and put_ok:
#         signal = ("PUT", "BREAKOUT_PREV_LOW")
#     elif last.low <= pd_low and (last.close - last.low) > 0.5 * rng and call_ok:
#         signal = ("CALL", "REJECTION_PREV_LOW")
#     elif last.high >= pd_high and (last.high - last.close) > 0.5 * rng and put_ok:
#         signal = ("PUT", "REJECTION_PREV_HIGH")

#     # ===============================
#     # Fast-path continuation: multi-candle breakdown (PUT) and breakout (CALL)
#     # ===============================
#     if signal is None:
#         # --- Downside continuation below S3 ---
#         if last.close < s3 - 0.05 * atr and put_ok:
#             prev_body = abs(prev.close - prev.open)
#             prev_rng  = prev.high - prev.low
#             last_body = abs(last.close - last.open)
#             last_rng  = last.high - last.low

#             if prev.close < s3 and (prev_body / max(prev_rng, 1e-6)) > 0.6 and (last_body / max(last_rng, 1e-6)) > 0.6:
#                 signal = ("PUT", "CONTINUATION_S3_BREAK")

#         # --- Downside continuation below S4 ---
#         elif last.close < s4 - 0.05 * atr and put_ok:
#             prev_body = abs(prev.close - prev.open)
#             prev_rng  = prev.high - prev.low
#             last_body = abs(last.close - last.open)
#             last_rng  = last.high - last.low

#             if prev.close < s4 and (prev_body / max(prev_rng, 1e-6)) > 0.6 and (last_body / max(last_rng, 1e-6)) > 0.6:
#                 signal = ("PUT", "CONTINUATION_S4_BREAK")

#         # --- Upside continuation above R3 ---
#         elif last.close > r3 + 0.05 * atr and call_ok:
#             prev_body = abs(prev.close - prev.open)
#             prev_rng  = prev.high - prev.low
#             last_body = abs(last.close - last.open)
#             last_rng  = last.high - last.low

#             if prev.close > r3 and (prev_body / max(prev_rng, 1e-6)) > 0.6 and (last_body / max(last_rng, 1e-6)) > 0.6:
#                 signal = ("CALL", "CONTINUATION_R3_BREAK")

#         # --- Upside continuation above R4 ---
#         elif last.close > r4 + 0.05 * atr and call_ok:
#             prev_body = abs(prev.close - prev.open)
#             prev_rng  = prev.high - prev.low
#             last_body = abs(last.close - last.open)
#             last_rng  = last.high - last.low

#             if prev.close > r4 and (prev_body / max(prev_rng, 1e-6)) > 0.6 and (last_body / max(last_rng, 1e-6)) > 0.6:
#                 signal = ("CALL", "CONTINUATION_R4_BREAK")
    
#     if signal:
#         # --- ATR filter ---
#         if atr <= 20:
#             logging.info(f"{CYAN}[SIGNAL REJECTED][FILTER] {signal}{RESET} | ATR={atr:.2f} below threshold")
#             return None

#         # --- CCI strict crossover filter ---
#         df = calculate_cci(candles_3m_)

#         if signal[0] == "CALL":
#             if not cci_cross_up_strict(df, margin=5):  # margin optional
#                 logging.info(f"{CYAN}[SIGNAL REJECTED][FILTER] {signal}{RESET} | CPR CALL but no strict bullish CCI cross")
#                 return None

#         if signal[0] == "PUT":
#             if not cci_cross_down_strict(df, margin=2):  # margin optional
#                 logging.info(f"{CYAN}[SIGNAL REJECTED][FILTER] {signal}{RESET} | CPR PUT but no strict bearish CCI cross")
#                 return None

#         # --- Existing strong_trade_fn filter ---
#         last = candles_3m_.iloc[-1]
#         rng = last["high"] - last["low"]
#         body = abs(last["close"] - last["open"])
#         body_ratio = body / (rng + 1e-6)

#         # ok, reason = strong_trade_fn(
#         #     max(call_momentum, put_momentum),  # momentum
#         #     rng,
#         #     atr,
#         #     body_ratio
#         # )
#         ok, reason = strong_trade_with_cci(
#         df,
#         signal[0],  # "CALL" or "PUT"
#         max(call_momentum, put_momentum),
#         rng,
#         atr,
#         body_ratio,
#         margin=2  # tune margin here
# )
#         if ok:
#             logging.info(f"{CYAN}[SIGNAL ACCEPTED][FILTER] {signal}{RESET} | Strict CCI confirmed")
#             return signal, None
#         else:
#             logging.info(f"{CYAN}[SIGNAL REJECTED][FILTER] {signal}{RESET} | {reason}")
#             return None

#     return None



# ================================== detect_signal with Candle Patterns =========================================

def detect_signal(
    cpr_levels,
    traditional_levels,
    camarilla_levels,
    atr,
    candles_3m_,
    *,
    in_position=False,
    cooldown_bars=1,
    last_signal_bar_index=None
):
    """
    Robust signal detection with:
      - Key normalization (lowercase)
      - Monotonic, candle-anchored targets (PT nearer, TG further)
      - Fresh breakout confirmation (prev candle on opposite side)
      - Body ratio and retest filters to avoid wick-only triggers
      - SL anchored to both level and wick for realistic retest tolerance
      - Optional position/cooldown guards to prevent re-entry on each close
      - Hold-grace hint for order management (skip SL on entry bar)

    Returns:
      ("CALL"/"PUT", {reason, stop_loss, target1, target2, bar_index, hold_grace_bars}) or None
    """

    logging.info(f"{YELLOW}[DETECT_SIGNAL CALLED] candles={len(candles_3m_)} atr={atr}{RESET}")

    # ---- Guards ----
    if atr is None or atr <= 0:
        logging.warning("[ATR] Invalid ATR; aborting signal detection")
        return None
    if len(candles_3m_) < 2:
        logging.info("[CANDLES] Not enough candles; need at least 2")
        return None
    if in_position:
        logging.info("[POSITION GUARD] Already in position; skipping new signal")
        return None

    # Cooldown guard
    if last_signal_bar_index is not None and cooldown_bars > 0:
        try:
            last_idx_pos = candles_3m_.index.get_loc(last_signal_bar_index)
            curr_idx_pos = len(candles_3m_) - 1
            if (curr_idx_pos - last_idx_pos) < cooldown_bars:
                logging.info(f"[COOLDOWN] Skipping signal; cooldown_bars={cooldown_bars}")
                return None
        except Exception:
            pass

    last = candles_3m_.iloc[-1]
    prev = candles_3m_.iloc[-2]
    rng = float(last.high - last.low)
    if rng <= 0:
        logging.info("[RANGE] Zero/negative range; skipping")
        return None

    # ---- Normalize keys ----
    cpr_levels = {str(k).lower(): v for k, v in cpr_levels.items()}
    traditional_levels = {str(k).lower(): v for k, v in traditional_levels.items()}
    camarilla_levels = {str(k).lower(): v for k, v in camarilla_levels.items()}

    # ---- Levels ----
    pivot = traditional_levels.get("pivot")
    r1, s1 = traditional_levels.get("r1"), traditional_levels.get("s1")
    r2, s2 = traditional_levels.get("r2"), traditional_levels.get("s2")
    r3, r4 = camarilla_levels.get("r3"), camarilla_levels.get("r4")
    s3, s4 = camarilla_levels.get("s3"), camarilla_levels.get("s4")
    tc, bc = cpr_levels.get("tc"), cpr_levels.get("bc")

    required = [pivot, r1, s1, r2, s2, r3, r4, s3, s4, tc, bc]
    if any(v is None for v in required):
        logging.warning("[LEVELS] Missing one or more required levels; aborting signal detection")
        return None

    # ---- Helpers ----
    def body_ratio(candle):
        _rng = candle.high - candle.low
        return 0.0 if _rng <= 0 else abs(candle.close - candle.open) / _rng

    def retest_level_low(candle, level, atr, tolerance=0.05):
        return candle.low <= level + tolerance * atr

    def retest_level_high(candle, level, atr, tolerance=0.05):
        return candle.high >= level - tolerance * atr

    def fresh_breakout_above(prev_close, level, last_close, atr, margin=0.15):
        return prev_close <= level and last_close > (level + margin * atr)

    def fresh_breakout_below(prev_close, level, last_close, atr, margin=0.15):
        return prev_close >= level and last_close < (level - margin * atr)

    def compute_targets(signal, close, atr, near_pivot=None, far_pivot=None, ext_near=0.75, ext_far=1.5):
        if signal == "CALL":
            near_candidates = []
            if near_pivot is not None and near_pivot > close:
                near_candidates.append(near_pivot)
            near_candidates.append(close + ext_near * atr)
            t1 = min([x for x in near_candidates if x > close]) if near_candidates else close + ext_near * atr

            far_candidates = []
            if far_pivot is not None and far_pivot > t1:
                far_candidates.append(far_pivot)
            far_candidates.append(close + ext_far * atr)
            t2 = max([x for x in far_candidates if x > t1]) if far_candidates else close + ext_far * atr

            if t2 <= t1:
                t2 = t1 + 0.5 * atr
            return t1, t2

        else:  # PUT
            near_candidates = []
            if near_pivot is not None and near_pivot < close:
                near_candidates.append(near_pivot)
            near_candidates.append(close - ext_near * atr)
            t1 = max([x for x in near_candidates if x < close]) if near_candidates else close - ext_near * atr

            far_candidates = []
            if far_pivot is not None and far_pivot < t1:
                far_candidates.append(far_pivot)
            far_candidates.append(close - ext_far * atr)
            t2 = min([x for x in far_candidates if x < t1]) if far_candidates else close - ext_far * atr

            if t2 >= t1:
                t2 = t1 - 0.5 * atr
            return t1, t2

    # ===============================
    # CPR Breakouts (robust confirmation)
    # ===============================
    if fresh_breakout_above(prev.close, tc, last.close, atr, margin=0.15) \
       and body_ratio(last) >= 0.4 \
       and retest_level_low(last, tc, atr, tolerance=0.05):

        sl_tc = tc - 0.2 * atr
        sl_wick = last.low - 0.1 * atr
        stop_loss = min(sl_tc, sl_wick)

        t1, t2 = compute_targets("CALL", last.close, atr, near_pivot=r1, far_pivot=r2, ext_near=0.75, ext_far=1.5)

        return "CALL", {
            "reason": "BREAKOUT_CPR_TC",
            "stop_loss": stop_loss,
            "target1": t1,
            "target2": t2,
            "bar_index": candles_3m_.index[-1],
            "hold_grace_bars": 1
        }

    if fresh_breakout_below(prev.close, bc, last.close, atr, margin=0.15) \
       and body_ratio(last) >= 0.4 \
       and retest_level_high(last, bc, atr, tolerance=0.05):

        sl_bc = bc + 0.2 * atr
        sl_wick = last.high + 0.1 * atr
        stop_loss = max(sl_bc, sl_wick)

        t1, t2 = compute_targets("PUT", last.close, atr, near_pivot=s1, far_pivot=s2, ext_near=0.75, ext_far=1.5)

        return "PUT", {
            "reason": "BREAKOUT_CPR_BC",
            "stop_loss": stop_loss,
            "target1": t1,
            "target2": t2,
            "bar_index": candles_3m_.index[-1],
            "hold_grace_bars": 1
        }

    # ===============================
    # Camarilla Breakouts/Continuations (fresh + retest)
    # ===============================
    if fresh_breakout_above(prev.close, r3, last.close, atr, margin=0.15) and retest_level_low(last, r3, atr):
        stop_loss = min(r3 - 0.2 * atr, last.low - 0.1 * atr)
        t1, t2 = compute_targets("CALL", last.close, atr, near_pivot=r4, far_pivot=r2)
        return "CALL", {"reason": "BREAKOUT_R3", "stop_loss": stop_loss, "target1": t1, "target2": t2, "bar_index": candles_3m_.index[-1], "hold_grace_bars": 1}

    if fresh_breakout_above(prev.close, r4, last.close, atr, margin=0.15) and retest_level_low(last, r4, atr):
        stop_loss = min(r4 - 0.2 * atr, last.low - 0.1 * atr)
        t1, t2 = compute_targets("CALL", last.close, atr, near_pivot=r2, far_pivot=r1)
        return "CALL", {"reason": "BREAKOUT_R4", "stop_loss": stop_loss, "target1": t1, "target2": t2, "bar_index": candles_3m_.index[-1], "hold_grace_bars": 1}

    if fresh_breakout_below(prev.close, s3, last.close, atr, margin=0.15) and retest_level_high(last, s3, atr):
        stop_loss = max(s3 + 0.2 * atr, last.high + 0.1 * atr)
        t1, t2 = compute_targets("PUT", last.close, atr, near_pivot=s4, far_pivot=s2)
        return "PUT", {"reason": "BREAKOUT_S3", "stop_loss": stop_loss, "target1": t1, "target2": t2, "bar_index": candles_3m_.index[-1], "hold_grace_bars": 1}

    if fresh_breakout_below(prev.close, s4, last.close, atr, margin=0.15) and retest_level_high(last, s4, atr):
        stop_loss = max(s4 + 0.2 * atr, last.high + 0.1 * atr)
        t1, t2 = compute_targets("PUT", last.close, atr, near_pivot=s2, far_pivot=s1)
        return "PUT", {"reason": "BREAKOUT_S4", "stop_loss": stop_loss, "target1": t1, "target2": t2, "bar_index": candles_3m_.index[-1], "hold_grace_bars": 1}

    # Continuations (wick touch + close beyond + prev context)
    def continuation_long(level): return last.low <= level and last.close > level + 0.05 * atr and prev.close <= level
    def continuation_short(level): return last.high >= level and last.close < level - 0.05 * atr and prev.close >= level

    if continuation_long(r4):
        stop_loss = min(r4 - 0.2 * atr, last.low - 0.1 * atr)
        t1, t2 = compute_targets("CALL", last.close, atr, near_pivot=r2, far_pivot=r1)
        return "CALL", {"reason": "CONTINUATION_R4", "stop_loss": stop_loss, "target1": t1, "target2": t2, "bar_index": candles_3m_.index[-1], "hold_grace_bars": 1}

    if continuation_short(s4):
        stop_loss = max(s4 + 0.2 * atr, last.high + 0.1 * atr)
        t1, t2 = compute_targets("PUT", last.close, atr, near_pivot=s2, far_pivot=s1)
        return "PUT", {"reason": "CONTINUATION_S4", "stop_loss": stop_loss, "target1": t1, "target2": t2, "bar_index": candles_3m_.index[-1], "hold_grace_bars": 1}

    # ===============================
    # Traditional Levels (fresh + rejections)
    # ===============================
    if fresh_breakout_above(prev.close, r2, last.close, atr, margin=0.15) and retest_level_low(last, r2, atr):
        stop_loss = min(r2 - 0.2 * atr, last.low - 0.1 * atr)
        t1, t2 = compute_targets("CALL", last.close, atr, near_pivot=r3, far_pivot=r4)
        return "CALL", {"reason": "BREAKOUT_R2", "stop_loss": stop_loss, "target1": t1, "target2": t2, "bar_index": candles_3m_.index[-1], "hold_grace_bars": 1}

    if fresh_breakout_below(prev.close, s2, last.close, atr, margin=0.15) and retest_level_high(last, s2, atr):
        stop_loss = max(s2 + 0.2 * atr, last.high + 0.1 * atr)
        t1, t2 = compute_targets("PUT", last.close, atr, near_pivot=s3, far_pivot=s4)
        return "PUT", {"reason": "BREAKOUT_S2", "stop_loss": stop_loss, "target1": t1, "target2": t2, "bar_index": candles_3m_.index[-1], "hold_grace_bars": 1}

    if last.low <= s1 and (last.close - last.low) > 0.5 * rng and prev.close <= s1:
        stop_loss = last.low - 0.1 * atr
        t1, t2 = compute_targets("CALL", last.close, atr, near_pivot=pivot, far_pivot=r1)
        return "CALL", {"reason": "REJECTION_S1", "stop_loss": stop_loss, "target1": t1, "target2": t2, "bar_index": candles_3m_.index[-1], "hold_grace_bars": 1}

    if last.high >= r1 and (last.high - last.close) > 0.5 * rng and prev.close >= r1:
        stop_loss = last.high + 0.1 * atr
        t1, t2 = compute_targets("PUT", last.close, atr, near_pivot=pivot, far_pivot=s1)
        return "PUT", {"reason": "REJECTION_R1", "stop_loss": stop_loss, "target1": t1, "target2": t2, "bar_index": candles_3m_.index[-1], "hold_grace_bars": 1}

    # ===============================
    # Pivot (fresh only)
    # ===============================
    if fresh_breakout_above(prev.close, pivot, last.close, atr, margin=0.15) and retest_level_low(last, pivot, atr):
        stop_loss = min(pivot - 0.2 * atr, last.low - 0.1 * atr)
        t1, t2 = compute_targets("CALL", last.close, atr, near_pivot=r1, far_pivot=r2)
        return "CALL", {"reason": "BREAKOUT_PIVOT", "stop_loss": stop_loss, "target1": t1, "target2": t2, "bar_index": candles_3m_.index[-1], "hold_grace_bars": 1}

    if fresh_breakout_below(prev.close, pivot, last.close, atr, margin=0.15) and retest_level_high(last, pivot, atr):
        stop_loss = max(pivot + 0.2 * atr, last.high + 0.1 * atr)
        t1, t2 = compute_targets("PUT", last.close, atr, near_pivot=s1, far_pivot=s2)
        return "PUT", {"reason": "BREAKOUT_PIVOT", "stop_loss": stop_loss, "target1": t1, "target2": t2, "bar_index": candles_3m_.index[-1], "hold_grace_bars": 1}

    return None


