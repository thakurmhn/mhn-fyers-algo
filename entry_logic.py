# ===== entry_logic.py (v4 — FULL SIGNAL CONFLUENCE CHECK) =====
"""
CHANGES IN v4 vs v3:

1. confluence_gate() — new function
   Seven binary VETO checks that ALL must pass before an entry is allowed.
   A single veto failure blocks the trade regardless of composite score.

   Check       CALL must                    PUT must
   ─────────── ─────────────────────────── ───────────────────────────
   ST_15M      15m SuperTrend = BULLISH    = BEARISH  (NEUTRAL → skip)
   ST_3M       3m  SuperTrend ≠ BEARISH    ≠ BULLISH  (NEUTRAL → allow)
   EMA_3M      EMA9 ≥ EMA13               EMA9 ≤ EMA13
   EMA_15M     15m EMA9 ≥ EMA13           15m EMA9 ≤ EMA13  (no 15m → skip)
   RSI_ZONE    RSI < 80  (not exhausted)  RSI > 20   (not exhausted)
   ADX_TREND   ADX ≥ 18  (confirmed trend, not chop)
   VWAP_SIDE   close ≥ vwap−0.15×ATR      close ≤ vwap+0.15×ATR  (no VWAP → skip)

2. Time-of-day floors tightened
   LATE  floor: 65 → 75  (14:00–15:05)
   AFTN  floor: threshold+15 → threshold+20  (13:00–14:00)

3. Base threshold raised
   NORMAL regime: 50 → 75
   HIGH   regime: 60 → 80

4. Logging
   Every blocked confluence check is logged at INFO level in YELLOW.
   Result dict gains "confluence_fails" list key for diagnostics.

All v3 scorers, weights, and surcharge logic unchanged.
"""

import logging
import pandas as pd
from day_type import apply_day_type_to_threshold, DayTypeResult

GREEN  = "\033[92m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
RED    = "\033[91m"
RESET  = "\033[0m"

# ── Score weights (sum = 100) — unchanged from v3 ────────────────────────────
WEIGHTS = {
    "trend_15m":        20,
    "trend_3m":         15,
    "ema_momentum":     15,
    "adx_strength":     10,
    "oscillators":      15,
    "pivot_structure":  10,
    "candle_quality":    5,
    "liquidity_zone":    5,
    "vwap_position":    10,
}

# ── Base thresholds raised from v3 (50/60 → 75/80) ───────────────────────────
THRESHOLDS = {
    "LOW":    999,   # low vol = never enter
    "NORMAL":  75,   # raised from 50 — only high-conviction entries
    "HIGH":    80,   # raised from 60
}

# ── Time-of-day floors (raised from v3) ──────────────────────────────────────
AFTN_FLOOR_ADD  = 20   # 13:00–14:00: base + 20 (was +15)
LATE_FLOOR      = 75   # 14:00–15:05: hard floor 75 (was 65)

# ── ATR regime boundaries ─────────────────────────────────────────────────────
ATR_LOW_MAX  = 20
ATR_HIGH_MIN = 120

# ── Confluence veto thresholds ────────────────────────────────────────────────
CONFLUENCE_ADX_MIN  = 18     # ADX below this = chop, block all entries
CONFLUENCE_RSI_OB   = 80     # CALL veto: RSI above this = exhausted
CONFLUENCE_RSI_OS   = 20     # PUT  veto: RSI below this = exhausted
CONFLUENCE_VWAP_TOL = 0.15   # fraction of ATR tolerance around VWAP


def _safe_float(val):
    try:
        v = float(val)
        return None if pd.isna(v) else v
    except Exception:
        return None


def _atr_regime(atr):
    if atr is None:          return "UNKNOWN"
    if atr < ATR_LOW_MAX:    return "LOW"
    if atr > ATR_HIGH_MIN:   return "HIGH"
    return "NORMAL"


def _norm_bias(raw):
    """Accept 'UP'/'DOWN' (orchestration) and 'BULLISH'/'BEARISH' (signals)."""
    if raw in ("BULLISH", "UP"):   return "BULLISH"
    if raw in ("BEARISH", "DOWN"): return "BEARISH"
    return "NEUTRAL"


# ─────────────────────────────────────────────────────────────────────────────
# LIQUIDITY ZONE — unchanged from v3
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
# INDIVIDUAL SCORERS — all unchanged from v3
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
        if gap > 1.0:  return w
        if gap > 0:    return w // 2
        return 0
    else:
        if gap < -1.0: return w
        if gap < 0:    return w // 2
        return 0


def _score_adx(indicators):
    w   = WEIGHTS["adx_strength"]
    adx = _safe_float(indicators.get("adx"))
    if adx is None: return 0
    if adx >= 30:   return w
    if adx >= 20:   return int(w * 0.7)
    if adx >= 15:   return int(w * 0.4)
    if adx >= 10:   return int(w * 0.15)
    return 0


def _score_oscillators(candle, indicators, side):
    w     = WEIGHTS["oscillators"]
    score = 0.0
    max_s = 3.5

    cci3 = _safe_float(candle.get("cci20") or candle.get("cci"))
    if cci3 is not None:
        if side == "CALL":
            score += 1.0 if cci3 > 60 else (0.5 if cci3 > 40 else 0)
        else:
            score += 1.0 if cci3 < -60 else (0.5 if cci3 < -40 else 0)

    c15 = indicators.get("candle_15m")
    if c15 is not None:
        try:
            cci15 = _safe_float(c15.get("cci20") or c15.get("cci"))
            if cci15 is not None:
                if side == "CALL" and cci15 > 40:  score += 0.5
                if side == "PUT"  and cci15 < -40: score += 0.5
        except Exception:
            pass

    rsi = _safe_float(candle.get("rsi14") or candle.get("rsi"))
    if rsi is not None:
        if side == "CALL":
            score += 1.0 if 50 <= rsi <= 75 else (0.4 if rsi > 75 else 0)
        else:
            score += 1.0 if 25 <= rsi <= 50 else (0.4 if rsi < 25 else 0)

    wr = _safe_float(candle.get("wr14") or candle.get("wr"))
    if wr is not None:
        if side == "CALL" and wr > -40: score += 1.0
        if side == "PUT"  and wr < -60: score += 1.0

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
    w    = WEIGHTS["vwap_position"]
    vwap = _safe_float(indicators.get("vwap"))
    if vwap is None:
        return w // 4
    close = _safe_float(candle.get("close"))
    atr   = _safe_float(indicators.get("atr"))
    if close is None:
        return 0
    tol  = (atr * 0.1) if atr else 5.0
    dist = close - vwap
    if side == "CALL":
        if dist > tol:      return w
        if dist > 0:        return w // 2
        if abs(dist) < tol: return w // 3
        return 0
    else:
        if dist < -tol:     return w
        if dist < 0:        return w // 2
        if abs(dist) < tol: return w // 3
        return 0


# ─────────────────────────────────────────────────────────────────────────────
# CONFLUENCE GATE  (NEW in v4)
# ─────────────────────────────────────────────────────────────────────────────
def confluence_gate(candle, indicators, bias_15m, side, atr):
    """
    Seven binary VETO checks. Every check that fails is recorded.
    Returns a list of failed check names — empty list means all passed.

    Design principles:
    • NEUTRAL SuperTrend = skip that check (don't penalise ambiguity).
    • Missing data (no 15m, no VWAP) = skip that check (don't penalise gaps).
    • Only DIRECT opposition is vetoed — partial signals are not blocked here,
      they are already penalised by the scoring engine.
    • Thresholds are intentionally relaxed — this is a safety net for extreme
      misalignments, not a hair-trigger filter.

    Check definitions
    ─────────────────
    ST_15M    15m SuperTrend must agree with side (NEUTRAL → skip)
    ST_3M     3m  SuperTrend must not directly oppose side (NEUTRAL → pass)
    EMA_3M    3m  EMA9 must be on correct side of EMA13
    EMA_15M   15m EMA9 must be on correct side of EMA13 (missing → skip)
    RSI_ZONE  RSI must not be in exhaustion zone (missing → skip)
    ADX_TREND ADX must be ≥ CONFLUENCE_ADX_MIN = 18 (missing → skip)
    VWAP_SIDE Price must be on correct side of VWAP ± tolerance (missing → skip)
    """
    fails = []

    # ── ST_15M ───────────────────────────────────────────────────────────────
    bias15 = _norm_bias(bias_15m)
    if bias15 != "NEUTRAL":
        if side == "CALL" and bias15 == "BEARISH":
            fails.append("ST_15M")
        elif side == "PUT" and bias15 == "BULLISH":
            fails.append("ST_15M")

    # ── ST_3M ────────────────────────────────────────────────────────────────
    bias3 = _norm_bias(indicators.get("st_bias_3m", "NEUTRAL"))
    if bias3 != "NEUTRAL":
        if side == "CALL" and bias3 == "BEARISH":
            fails.append("ST_3M")
        elif side == "PUT" and bias3 == "BULLISH":
            fails.append("ST_3M")

    # ── EMA_3M ───────────────────────────────────────────────────────────────
    ema_fast = _safe_float(indicators.get("ema_fast"))
    ema_slow = _safe_float(indicators.get("ema_slow"))
    if ema_fast is not None and ema_slow is not None:
        if side == "CALL" and ema_fast < ema_slow:
            fails.append("EMA_3M")
        elif side == "PUT" and ema_fast > ema_slow:
            fails.append("EMA_3M")

    # ── EMA_15M ──────────────────────────────────────────────────────────────
    c15 = indicators.get("candle_15m")
    if c15 is not None:
        try:
            ema9_15  = _safe_float(c15.get("ema9"))
            ema13_15 = _safe_float(c15.get("ema13"))
            if ema9_15 is not None and ema13_15 is not None:
                if side == "CALL" and ema9_15 < ema13_15:
                    fails.append("EMA_15M")
                elif side == "PUT" and ema9_15 > ema13_15:
                    fails.append("EMA_15M")
        except Exception:
            pass   # missing 15m data → skip check

    # ── RSI_ZONE ─────────────────────────────────────────────────────────────
    rsi = _safe_float(candle.get("rsi14") or candle.get("rsi"))
    if rsi is not None:
        if side == "CALL" and rsi >= CONFLUENCE_RSI_OB:
            fails.append("RSI_ZONE")
        elif side == "PUT" and rsi <= CONFLUENCE_RSI_OS:
            fails.append("RSI_ZONE")

    # ── ADX_TREND ────────────────────────────────────────────────────────────
    adx = _safe_float(indicators.get("adx"))
    if adx is not None and adx < CONFLUENCE_ADX_MIN:
        fails.append("ADX_TREND")

    # ── VWAP_SIDE ────────────────────────────────────────────────────────────
    vwap  = _safe_float(indicators.get("vwap"))
    close = _safe_float(candle.get("close"))
    if vwap is not None and close is not None and atr is not None and atr > 0:
        tol = atr * CONFLUENCE_VWAP_TOL
        if side == "CALL" and close < vwap - tol:
            fails.append("VWAP_SIDE")
        elif side == "PUT" and close > vwap + tol:
            fails.append("VWAP_SIDE")

    return fails


# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC API — check_entry_condition
# ─────────────────────────────────────────────────────────────────────────────
def check_entry_condition(candle, indicators, bias_15m,
                          pivot_signal=None, current_time=None,
                          day_type_result=None):
    """
    Scoring engine + confluence gate.

    Flow (v4):
    1. ATR regime check     — block LOW/UNKNOWN
    2. Time-of-day filter   — PRE_OPEN / OPENING_NOISE / LUNCH_CHOP / EOD_BLOCK
    3. Score both sides     — same as v3
    4. Confluence gate      — 7 binary vetoes on winning side
    5. If gate passes       — emit ENTRY OK
    6. If gate fails        — block + log each failed check in YELLOW

    Result dict additions vs v3:
      "confluence_fails"  list[str]  checks that vetoed the trade ([] = all passed)
    """

    result = {
        "action":           "HOLD",
        "reason":           "",
        "strength":         "NONE",
        "zone_type":        None,
        "side":             None,
        "score":            0,
        "threshold":        52,
        "breakdown":        {},
        "confluence_fails": [],   # NEW — diagnostic list
    }

    atr = _safe_float(indicators.get("atr"))
    if atr is None:
        result["reason"] = "ATR unavailable"
        return result

    regime    = _atr_regime(atr)
    threshold = THRESHOLDS.get(regime, 75)
    result["threshold"] = threshold

    if regime in ("LOW", "UNKNOWN"):
        result["reason"] = f"Regime blocked: {regime} ATR={atr:.1f}"
        return result

    # ── Time-of-day filter ────────────────────────────────────────────────────
    _late_session   = False
    _afternoon_chop = False
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
        if t >= 15 * 60 + 5:
            result["reason"] = "EOD_BLOCK"
            return result
        if 13 * 60 <= t < 14 * 60:
            _afternoon_chop = True
        if t >= 14 * 60:
            _late_session = True

    # ── Surcharge and ST bias resolution ─────────────────────────────────────
    st_bias_3m  = _norm_bias(indicators.get("st_bias_3m",  "NEUTRAL"))
    st_bias_15m = _norm_bias(indicators.get("st_bias_15m", "NEUTRAL"))

    best_score, best_side, best_bd, best_threshold = -1, "CALL", {}, threshold
    _dt_flag_best = ""

    for side in ("CALL", "PUT"):
        side_threshold = threshold

        # Surcharge 1: 3m ST opposes side → +8 pts
        if (side == "CALL" and st_bias_3m == "BEARISH") or \
           (side == "PUT"  and st_bias_3m == "BULLISH"):
            side_threshold += 8

        # Surcharge 2: 15m ST also opposes side → +7 pts more (total +15)
        if (side == "CALL" and st_bias_15m == "BEARISH") or \
           (side == "PUT"  and st_bias_15m == "BULLISH"):
            side_threshold += 7

        # Day type modifier
        if day_type_result is not None:
            side_threshold, _dt_flag = apply_day_type_to_threshold(
                side_threshold, day_type_result, side
            )
        else:
            _dt_flag = ""

        # ── Time-of-day hard floors (tightened in v4) ─────────────────────
        # AFTN 13:00–14:00: floor = base + AFTN_FLOOR_ADD (20, was 15)
        if _afternoon_chop:
            side_threshold = max(side_threshold, threshold + AFTN_FLOOR_ADD)

        # LATE 14:00–15:05: hard floor LATE_FLOOR (75, was 65)
        if _late_session:
            side_threshold = max(side_threshold, LATE_FLOOR)

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
            best_threshold  = side_threshold
            _dt_flag_best   = _dt_flag

    result["score"]     = best_score
    result["breakdown"] = best_bd
    result["side"]      = best_side
    result["threshold"] = best_threshold

    # ── Score gate ────────────────────────────────────────────────────────────
    if best_score < best_threshold:
        result["reason"] = (
            f"Score too low: {best_score}<{best_threshold} ({regime}) "
            f"best_side={best_side}"
        )
        logging.debug(f"[ENTRY BLOCKED] {result['reason']}")
        return result

    # ── Confluence gate (NEW v4) ──────────────────────────────────────────────
    fails = confluence_gate(candle, indicators, bias_15m, best_side, atr)
    result["confluence_fails"] = fails

    if fails:
        fail_str = ",".join(fails)
        result["reason"] = f"CONFLUENCE_FAIL [{best_side}]: {fail_str}"
        logging.info(
            f"{YELLOW}[CONFLUENCE BLOCK] {best_side} score={best_score}/{best_threshold}"
            f" | Failed: {fail_str}{RESET}"
        )
        return result

    # ── All gates passed — emit entry ─────────────────────────────────────────
    action    = "BUY"     if best_side == "CALL" else "SELL"
    zone_type = "SUPPORT" if best_side == "CALL" else "RESISTANCE"
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
    if _afternoon_chop:
        surcharge_flags.append(f"AFTN+{AFTN_FLOOR_ADD}")
    if _late_session:
        surcharge_flags.append(f"LATE≥{LATE_FLOOR}")
    if _dt_flag_best:
        surcharge_flags.append(_dt_flag_best)

    surcharge_note = f" [{','.join(surcharge_flags)}]" if surcharge_flags else ""
    logging.info(
        f"{GREEN}[ENTRY OK] {best_side} score={best_score}/{best_threshold}"
        f"{surcharge_note} {regime} {strength}{RESET}"
    )
    return result