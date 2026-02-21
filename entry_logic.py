# ===== entry_logic.py (v3 — IMPROVED) =====
"""
IMPROVEMENTS IN THIS VERSION vs v2:
1. VWAP score dimension added (replaces nothing; increases total weight range)
2. Score weights rebalanced — VWAP (10) takes weight from liquidity_zone (5→5, vwap=10)
3. Time-of-day filter extended: allows 15:00-15:10 window (was completely blocked)
4. ADX scoring improved: partial credit at ADX 10-15 (trending enough to score minimally)
5. EMA gap threshold reduced from 2.0 to 1.0 for full credit (NIFTY EMA9/13 typically tight)
6. Oscillator RSI bounds relaxed: 50-75 for CALL, 25-50 for PUT (was 52-75 / 25-48)
   → more entries in the 50-52 zone which is a valid BUY zone
All v2 bug-fixes retained.
"""

import logging
import pandas as pd

GREEN  = "\033[92m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
RESET  = "\033[0m"

# ── Score weights (sum = 100) ─────────────────────────────────────────────────
WEIGHTS = {
    "trend_15m":        20,   # HTF Supertrend direction
    "trend_3m":         15,   # LTF Supertrend direction
    "ema_momentum":     15,   # EMA9 vs EMA13 gap & direction (was 20 → reduced to make room)
    "adx_strength":     10,   # ADX trend strength
    "oscillators":      15,   # CCI + RSI + W%R composite
    "pivot_structure":  10,   # pivot level proximity
    "candle_quality":    5,   # body ratio + correct direction
    "liquidity_zone":    5,   # ST line proximity
    "vwap_position":    10,   # NEW: price position relative to VWAP (was 0)
}

# ── Threshold by ATR regime ───────────────────────────────────────────────────
THRESHOLDS = {
    "LOW":    999,    # low vol = never enter
    "NORMAL":  50,    # relaxed from 52 → fires more reliably
    "HIGH":    60,    # more volatile → need stronger confluence
}

# ── ATR regime boundaries ─────────────────────────────────────────────────────
ATR_LOW_MAX    = 20   # (was 25 — tightened so 20-25 ATR doesn't block entirely)
ATR_HIGH_MIN   = 120


def _safe_float(val):
    try:
        v = float(val)
        return None if pd.isna(v) else v
    except Exception:
        return None


def _atr_regime(atr):
    if atr is None:              return "UNKNOWN"
    if atr < ATR_LOW_MAX:        return "LOW"
    if atr > ATR_HIGH_MIN:       return "HIGH"
    return "NORMAL"


def _norm_bias(raw):
    """Accept both 'UP'/'DOWN' (orchestration) and 'BULLISH'/'BEARISH' (signals)."""
    if raw in ("BULLISH", "UP"):   return "BULLISH"
    if raw in ("BEARISH", "DOWN"): return "BEARISH"
    return "NEUTRAL"


# ─────────────────────────────────────────────────────────────────────────────
# LIQUIDITY ZONE (original function, bias normalised)
# ─────────────────────────────────────────────────────────────────────────────
def liquidity_zone(candle, supertrend_line, bias, atr, timeframe):
    signal = {"zone": None, "action": "HOLD", "reason": ""}

    if supertrend_line is None:
        return signal
    st = _safe_float(supertrend_line)
    if st is None:
        return signal

    atr_f = _safe_float(atr)
    if atr_f is None or atr_f <= 0:
        return signal

    close = _safe_float(candle.get("close"))
    if close is None:
        return signal

    bias_norm = _norm_bias(bias)
    tolerance = atr_f

    if bias_norm == "BEARISH" and abs(close - st) <= tolerance:
        signal["zone"]   = "RESISTANCE"
        signal["action"] = "SELL"
        signal["reason"] = f"{timeframe} rejection at ST {st:.1f}"
    elif bias_norm == "BULLISH" and abs(close - st) <= tolerance:
        signal["zone"]   = "SUPPORT"
        signal["action"] = "BUY"
        signal["reason"] = f"{timeframe} bounce at ST {st:.1f}"

    return signal


# ─────────────────────────────────────────────────────────────────────────────
# INDIVIDUAL SCORERS
# ─────────────────────────────────────────────────────────────────────────────

def _score_trend_15m(bias_15m, side):
    w    = WEIGHTS["trend_15m"]
    bias = _norm_bias(bias_15m)
    if bias == "BULLISH" and side == "CALL": return w
    if bias == "BEARISH" and side == "PUT":  return w
    if bias == "NEUTRAL":                    return w // 2
    return 0


def _score_trend_3m(indicators, side):
    w        = WEIGHTS["trend_3m"]
    raw_bias = indicators.get("st_bias_3m", "NEUTRAL")
    bias     = _norm_bias(raw_bias)
    if bias == "BULLISH" and side == "CALL": return w
    if bias == "BEARISH" and side == "PUT":  return w
    if bias == "NEUTRAL":                    return w // 3
    return 0


def _score_ema(indicators, side):
    w    = WEIGHTS["ema_momentum"]
    fast = _safe_float(indicators.get("ema_fast"))
    slow = _safe_float(indicators.get("ema_slow"))
    if fast is None or slow is None:
        return 0
    gap = fast - slow
    if side == "CALL":
        if gap > 1.0:   return w           # was 2.0 — NIFTY EMA9/13 gap is usually small
        if gap > 0:     return w // 2
        return 0
    else:
        if gap < -1.0:  return w
        if gap < 0:     return w // 2
        return 0


def _score_adx(indicators):
    w   = WEIGHTS["adx_strength"]
    adx = _safe_float(indicators.get("adx"))
    if adx is None: return 0
    if adx >= 30:   return w
    if adx >= 20:   return int(w * 0.7)
    if adx >= 15:   return int(w * 0.4)    # was 0.3 — slightly more generous
    if adx >= 10:   return int(w * 0.15)   # NEW: partial credit, was 0
    return 0


def _score_oscillators(candle, indicators, side):
    w     = WEIGHTS["oscillators"]
    score = 0.0
    max_s = 3.5

    # CCI 3m
    cci3 = _safe_float(candle.get("cci20") or candle.get("cci"))
    if cci3 is not None:
        if side == "CALL":
            score += 1.0 if cci3 > 60 else (0.5 if cci3 > 40 else 0)
        else:
            score += 1.0 if cci3 < -60 else (0.5 if cci3 < -40 else 0)

    # CCI 15m
    c15 = indicators.get("candle_15m")
    if c15 is not None:
        try:
            cci15 = _safe_float(c15.get("cci20") or c15.get("cci"))
            if cci15 is not None:
                if side == "CALL" and cci15 > 40:  score += 0.5
                if side == "PUT"  and cci15 < -40: score += 0.5
        except Exception:
            pass

    # RSI 3m — relaxed from 52-75 to 50-75 for CALL
    rsi = _safe_float(candle.get("rsi14") or candle.get("rsi"))
    if rsi is not None:
        if side == "CALL":
            score += 1.0 if 50 <= rsi <= 75 else (0.4 if rsi > 75 else 0)
        else:
            score += 1.0 if 25 <= rsi <= 50 else (0.4 if rsi < 25 else 0)

    # Williams %R
    wr = _safe_float(candle.get("wr14") or candle.get("wr"))
    if wr is not None:
        if side == "CALL" and wr > -40:  score += 1.0
        if side == "PUT"  and wr < -60:  score += 1.0

    return int(w * min(score / max_s, 1.0))


def _score_pivot(pivot_signal, side):
    w = WEIGHTS["pivot_structure"]
    if not pivot_signal:
        return 0
    ps, reason = pivot_signal
    if ps != side:
        return 0
    if "BREAKOUT"     in reason: return w
    if "REJECTION"    in reason: return int(w * 0.85)
    if "ACCEPTANCE"   in reason: return int(w * 0.70)
    if "CONTINUATION" in reason: return int(w * 0.50)
    return int(w * 0.30)


def _score_candle(candle, side):
    w = WEIGHTS["candle_quality"]
    o = _safe_float(candle.get("open"))
    h = _safe_float(candle.get("high"))
    l = _safe_float(candle.get("low"))
    c = _safe_float(candle.get("close"))
    if any(v is None for v in [o, h, l, c]):
        return 0
    rng = h - l
    if rng == 0:
        return 0
    body_ratio   = abs(c - o) / rng
    direction_ok = (side == "CALL" and c > o) or (side == "PUT" and c < o)
    if body_ratio >= 0.50 and direction_ok: return w
    if body_ratio >= 0.35 and direction_ok: return w // 2
    return 0


def _score_lz(candle, indicators, bias_15m, side):
    w = WEIGHTS["liquidity_zone"]
    lz_15m = liquidity_zone(candle, indicators.get("supertrend_line_15m"),
                            bias_15m, indicators.get("atr"), "15m")
    lz_3m  = liquidity_zone(candle, indicators.get("supertrend_line_3m"),
                            bias_15m, indicators.get("atr"), "3m")
    if lz_15m["action"] == ("BUY" if side == "CALL" else "SELL"):
        return w
    if lz_3m["action"]  == ("BUY" if side == "CALL" else "SELL"):
        return w // 2
    return 0


def _score_vwap(candle, indicators, side):
    """
    NEW: Score based on price position relative to VWAP.
    - Price above VWAP → bullish context → CALL gets full credit
    - Price below VWAP → bearish context → PUT gets full credit
    - Within 0.1 ATR of VWAP → neutral → half credit for trend-side entry
    """
    w    = WEIGHTS["vwap_position"]
    vwap = _safe_float(indicators.get("vwap"))
    if vwap is None:
        return w // 4  # VWAP unavailable → small partial credit, don't penalise

    close = _safe_float(candle.get("close"))
    atr   = _safe_float(indicators.get("atr"))
    if close is None:
        return 0

    tol = (atr * 0.1) if atr else 5.0
    dist = close - vwap

    if side == "CALL":
        if dist > tol:      return w        # above VWAP by meaningful margin
        if dist > 0:        return w // 2   # just above
        if abs(dist) < tol: return w // 3   # at VWAP — could bounce
        return 0                            # below VWAP → no credit for CALL
    else:  # PUT
        if dist < -tol:     return w
        if dist < 0:        return w // 2
        if abs(dist) < tol: return w // 3
        return 0


# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC API — check_entry_condition
# ─────────────────────────────────────────────────────────────────────────────
def check_entry_condition(candle, indicators, bias_15m,
                          pivot_signal=None, current_time=None):
    """
    Scoring engine. All existing call sites work unchanged.
    New: VWAP dimension added to scoring.
    """

    result = {
        "action":    "HOLD",
        "reason":    "",
        "strength":  "NONE",
        "zone_type": None,
        "side":      None,
        "score":     0,
        "threshold": 52,
        "breakdown": {},
    }

    atr = _safe_float(indicators.get("atr"))
    if atr is None:
        result["reason"] = "ATR unavailable"
        return result

    regime    = _atr_regime(atr)
    threshold = THRESHOLDS.get(regime, 55)
    result["threshold"] = threshold

    if regime in ("LOW", "UNKNOWN"):
        result["reason"] = f"Regime blocked: {regime} ATR={atr:.1f}"
        return result

    # Time-of-day filter
    _late_session = False
    if current_time is not None:
        h, m = current_time.hour, current_time.minute
        t    = h * 60 + m
        if t < 9 * 60 + 30:
            result["reason"] = "PRE_OPEN"
            return result
        if t < 9 * 60 + 45:
            result["reason"] = "OPENING_NOISE"
            return result
        if 12 * 60 <= t < 12 * 60 + 20:
            result["reason"] = "LUNCH_CHOP"
            return result
        if t >= 15 * 60 + 15:
            result["reason"] = "EOD_BLOCK"
            return result
        # Late session (14:00+): require stronger confluence
        if t >= 14 * 60:
            _late_session = True

    best_score, best_side, best_bd, best_threshold = -1, "CALL", {}, threshold

    # Surcharge 1: 3m ST opposes side → +8 pts required
    # Surcharge 2: Both 3m AND 15m oppose side → +15 pts total (full conflict)
    st_bias_3m  = _norm_bias(indicators.get("st_bias_3m",  "NEUTRAL"))
    st_bias_15m = _norm_bias(indicators.get("st_bias_15m", "NEUTRAL"))

    for side in ("CALL", "PUT"):
        side_threshold = threshold

        # Surcharge 1: 3m ST opposes side → +8 pts
        if (side == "CALL" and st_bias_3m == "BEARISH") or \
           (side == "PUT"  and st_bias_3m == "BULLISH"):
            side_threshold += 8

        # Surcharge 2: 15m ST also opposes side (full conflict) → +7 pts more (total +15)
        if (side == "CALL" and st_bias_15m == "BEARISH") or \
           (side == "PUT"  and st_bias_15m == "BULLISH"):
            side_threshold += 7

        # Late session floor: after 14:00, need score ≥65 for new entries
        if _late_session:
            side_threshold = max(side_threshold, 65)
        bd = {
            "trend_15m":       _score_trend_15m(bias_15m, side),
            "trend_3m":        _score_trend_3m(indicators, side),
            "ema_momentum":    _score_ema(indicators, side),
            "adx_strength":    _score_adx(indicators),
            "oscillators":     _score_oscillators(candle, indicators, side),
            "pivot_structure": _score_pivot(pivot_signal, side),
            "candle_quality":  _score_candle(candle, side),
            "liquidity_zone":  _score_lz(candle, indicators, bias_15m, side),
            "vwap_position":   _score_vwap(candle, indicators, side),
        }
        total = sum(bd.values())
        logging.debug(f"[SCORE][{side}] {total}/{side_threshold} | {bd}")

        if total > best_score:
            best_score, best_side, best_bd = total, side, bd
            best_threshold = side_threshold   # track the threshold that applies to the winner

    result["score"]     = best_score
    result["breakdown"] = best_bd
    result["side"]      = best_side
    result["threshold"] = best_threshold

    if best_score >= best_threshold:
        action    = "BUY"      if best_side == "CALL" else "SELL"
        zone_type = "SUPPORT"  if best_side == "CALL" else "RESISTANCE"
        strength  = (
            "HIGH"   if best_score >= best_threshold + 15 else
            "MEDIUM" if best_score >= best_threshold + 5  else
            "WEAK"
        )
        result.update(
            action=action, zone_type=zone_type, strength=strength,
            reason=f"Score={best_score}/{best_threshold} ({regime}) side={best_side}"
        )
        surcharge_flags = []
        if (best_side == "CALL" and st_bias_3m == "BEARISH") or \
           (best_side == "PUT"  and st_bias_3m == "BULLISH"):
            surcharge_flags.append("CT3m+8")
        if (best_side == "CALL" and st_bias_15m == "BEARISH") or \
           (best_side == "PUT"  and st_bias_15m == "BULLISH"):
            surcharge_flags.append("CT15m+7")
        if _late_session and best_threshold >= 65:
            surcharge_flags.append("LATE+65")
        surcharge_note = f" [{','.join(surcharge_flags)}]" if surcharge_flags else ""
        logging.info(
            f"{GREEN}[ENTRY OK] {best_side} score={best_score}/{best_threshold}"
            f"{surcharge_note} {regime} {strength}{RESET}"
        )
    else:
        result["reason"] = (
            f"Score too low: {best_score}<{best_threshold} ({regime}) "
            f"best_side={best_side}"
        )
        logging.debug(f"[ENTRY BLOCKED] {result['reason']}")

    return result
