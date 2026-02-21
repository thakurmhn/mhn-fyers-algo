# ============================================================
#  day_type.py  — v1.0
#  Camarilla Pivot-Based Day Type Classification
#  NSE NIFTY Options Buying Bot
# ============================================================
"""
Classifies the current trading day in real time using Camarilla
pivot levels, CPR width, and intraday price behaviour.

Six Day Types
─────────────
  RANGE          Price oscillates between R3 and S3
                 → Fade extremes (sell at R3, buy at S3)
                 → Avoid breakout chasing

  TRENDING       Price breaks R4 or S4 and runs toward R5/S5
                 → Ride breakout in breach direction
                 → Raise trail tightness (don't give back gains)

  REVERSAL       Price tests R4/S4 but fails; returns inside R3/S3
                 → Fade the false breakout, trade return to pivot
                 → Requires confirmed rejection candle

  DOUBLE_DIST    Two distinct value areas: first near R3/S3,
                 then a shift to a new range around R4/R5
                 → Trade the breakout between distributions
                 → High conviction breakout source

  NEUTRAL        Price tests both R4 and S4; closes near Pivot
                 → Both extremes explored; scalp only
                 → Avoid trending setups

  NON_TREND      Price hugs Pivot; cannot extend beyond R3/S3
                 → Very narrow activity; stay light
                 → No actionable setups

Signal Modifiers (applied in entry_logic)
──────────────────────────────────────────
  TRENDING       → threshold –8   (easier to enter, ride the move)
  DOUBLE_DIST    → threshold –5   (elevated confidence breakout)
  RANGE          → threshold +8   (fade only at true extremes)
  REVERSAL       → threshold –5   (but only for counter-trend side)
  NEUTRAL        → threshold +10  (scalp zone, very high bar)
  NON_TREND      → threshold +15  (effectively blocked)

Position Manager Modifiers
───────────────────────────
  TRENDING       → TRAIL_STEP tightened to 0.10  (lock gains faster)
  RANGE          → MAX_HOLD shortened to 12 bars  (don't overstay)
  NON_TREND      → MAX_HOLD shortened to 8 bars   (exit fast)

Usage
──────
    from day_type import DayTypeClassifier

    dtc = DayTypeClassifier(camarilla_levels, cpr_levels, prev_range)
    day_type = dtc.update(candles_3m)   # call every bar
    print(day_type.name, day_type.confidence, day_type.signal_modifier)

    # In entry_logic:
    effective_threshold = base_threshold + day_type.signal_modifier
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Tuple

import pandas as pd

# ── ANSI colours ──────────────────────────────────────────────────────────────
GREEN  = "\033[92m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
RED    = "\033[91m"
RESET  = "\033[0m"


# ─────────────────────────────────────────────────────────────────────────────
#  DayType Enum
# ─────────────────────────────────────────────────────────────────────────────

class DayType(str, Enum):
    RANGE       = "RANGE"
    TRENDING    = "TRENDING"
    REVERSAL    = "REVERSAL"
    DOUBLE_DIST = "DOUBLE_DIST"
    NEUTRAL     = "NEUTRAL"
    NON_TREND   = "NON_TREND"
    UNKNOWN     = "UNKNOWN"      # < 45 min of data; classification deferred


# ─────────────────────────────────────────────────────────────────────────────
#  DayTypeResult — returned by DayTypeClassifier.update()
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class DayTypeResult:
    name             : DayType = DayType.UNKNOWN
    confidence       : str     = "LOW"       # LOW | MEDIUM | HIGH
    signal_modifier  : int     = 0           # pts added to entry threshold
    pm_trail_step    : Optional[float] = None  # override PM trail step if set
    pm_max_hold      : Optional[int]   = None  # override PM max_hold if set
    evidence         : List[str] = field(default_factory=list)  # reasoning log

    # Camarilla levels snapshot at classification time
    r3: float = 0.0
    r4: float = 0.0
    r5: float = 0.0
    s3: float = 0.0
    s4: float = 0.0
    s5: float = 0.0
    cpr_width  : float = 0.0
    prev_range : float = 0.0

    @property
    def is_trending(self)    -> bool: return self.name == DayType.TRENDING
    @property
    def is_range(self)       -> bool: return self.name == DayType.RANGE
    @property
    def is_reversal(self)    -> bool: return self.name == DayType.REVERSAL
    @property
    def is_double_dist(self) -> bool: return self.name == DayType.DOUBLE_DIST
    @property
    def is_neutral(self)     -> bool: return self.name == DayType.NEUTRAL
    @property
    def is_non_trend(self)   -> bool: return self.name == DayType.NON_TREND
    @property
    def is_unknown(self)     -> bool: return self.name == DayType.UNKNOWN

    def log(self) -> None:
        color = {
            DayType.TRENDING   : GREEN,
            DayType.DOUBLE_DIST: GREEN,
            DayType.RANGE      : YELLOW,
            DayType.REVERSAL   : CYAN,
            DayType.NEUTRAL    : YELLOW,
            DayType.NON_TREND  : RED,
            DayType.UNKNOWN    : RESET,
        }.get(self.name, RESET)

        modifier_str = (f"{self.signal_modifier:+d}pts threshold"
                        if self.signal_modifier else "no threshold change")
        logging.info(
            f"{color}[DAY TYPE] {self.name.value:<12} "
            f"confidence={self.confidence:<6} "
            f"modifier={modifier_str} "
            f"| CPR_width={self.cpr_width:.1f} "
            f"prev_range={self.prev_range:.0f} "
            f"R3={self.r3:.0f} R4={self.r4:.0f} "
            f"S3={self.s3:.0f} S4={self.s4:.0f}{RESET}"
        )
        for ev in self.evidence:
            logging.debug(f"  [DAY TYPE evidence] {ev}")


# ─────────────────────────────────────────────────────────────────────────────
#  CPR / Camarilla Context
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class PivotContext:
    """
    Pre-computed pivot context for the session.
    Holds all Camarilla levels + CPR, compressed-pivot flags.
    """
    pivot     : float
    bc        : float       # CPR Bottom Central
    tc        : float       # CPR Top Central
    r3        : float
    r4        : float
    r5        : float       # computed if not supplied
    s3        : float
    s4        : float
    s5        : float       # computed if not supplied
    prev_high : float
    prev_low  : float
    prev_close: float

    @property
    def prev_range(self) -> float:
        return self.prev_high - self.prev_low

    @property
    def cpr_width(self) -> float:
        return abs(self.tc - self.bc)

    @property
    def r3_s3_band(self) -> float:
        return self.r3 - self.s3

    @property
    def is_narrow_cpr(self) -> bool:
        """CPR narrower than 0.15% of price → trending day likely."""
        return self.cpr_width < self.pivot * 0.0015

    @property
    def is_compressed_camarilla(self) -> bool:
        """
        Compressed Camarilla: R3-S3 band is less than 50% of yesterday's range.
        Historically predicts breakout/trending day.
        """
        return self.r3_s3_band < 0.5 * self.prev_range

    def inside_r3_s3(self, price: float) -> bool:
        return self.s3 <= price <= self.r3

    def above_r3(self, price: float) -> bool:
        return price > self.r3

    def above_r4(self, price: float) -> bool:
        return price > self.r4

    def below_s3(self, price: float) -> bool:
        return price < self.s3

    def below_s4(self, price: float) -> bool:
        return price < self.s4


def build_pivot_context(
    camarilla_levels : Dict,
    cpr_levels       : Dict,
    prev_high        : float,
    prev_low         : float,
    prev_close       : float,
) -> PivotContext:
    """
    Build a PivotContext from the level dicts already computed by indicators.py.
    Also computes R5/S5 which are not in the existing camarilla dict.

    Camarilla formulas:
        R3 = close + range × 1.1/4
        R4 = close + range × 1.1/2
        R5 = close + range × 1.1/1     ← added here
        S3 = close - range × 1.1/4
        S4 = close - range × 1.1/2
        S5 = close - range × 1.1/1     ← added here
    """
    rng   = prev_high - prev_low
    if rng == 0:
        rng = 0.001 * prev_close

    r5 = prev_close + rng * 1.1          # R5 = close + range × 1.1
    s5 = prev_close - rng * 1.1          # S5 = close - range × 1.1

    return PivotContext(
        pivot      = cpr_levels["pivot"],
        bc         = cpr_levels["bc"],
        tc         = cpr_levels["tc"],
        r3         = camarilla_levels["r3"],
        r4         = camarilla_levels["r4"],
        r5         = round(r5, 2),
        s3         = camarilla_levels["s3"],
        s4         = camarilla_levels["s4"],
        s5         = round(s5, 2),
        prev_high  = prev_high,
        prev_low   = prev_low,
        prev_close = prev_close,
    )


# ─────────────────────────────────────────────────────────────────────────────
#  State Tracker — tracks intraday extremes bar by bar
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class _IntraState:
    """Mutable intraday tracking state updated every bar."""
    session_high      : float = -math.inf
    session_low       : float =  math.inf
    touched_r4        : bool  = False
    touched_s4        : bool  = False
    closed_above_r4   : bool  = False
    closed_below_s4   : bool  = False
    rejected_r4       : bool  = False    # touched R4 then closed back below R3
    rejected_s4       : bool  = False    # touched S4 then closed back above S3
    closed_above_r3   : bool  = False
    closed_below_s3   : bool  = False
    bars_above_r3     : int   = 0
    bars_below_s3     : int   = 0
    bars_inside_band  : int   = 0
    first_range_high  : float = -math.inf  # for double distribution
    first_range_low   : float =  math.inf
    distribution_shift: bool  = False      # second value area detected
    value_areas       : List[Tuple[float,float]] = field(default_factory=list)
    bars_processed    : int   = 0

    def update(self, bar: pd.Series, pc: PivotContext) -> None:
        h = float(bar["high"])
        l = float(bar["low"])
        c = float(bar["close"])
        self.bars_processed += 1

        # Session extremes
        if h > self.session_high: self.session_high = h
        if l < self.session_low:  self.session_low  = l

        # R4 / S4 touch and close tracking
        if h >= pc.r4: self.touched_r4 = True
        if l <= pc.s4: self.touched_s4 = True
        if c > pc.r4:  self.closed_above_r4 = True
        if c < pc.s4:  self.closed_below_s4 = True
        if c > pc.r3:  self.closed_above_r3 = True
        if c < pc.s3:  self.closed_below_s3 = True

        # Rejection: touched R4 but now closed back inside R3-S3
        if self.touched_r4 and c < pc.r3 and not self.closed_above_r4:
            self.rejected_r4 = True
        if self.touched_s4 and c > pc.s3 and not self.closed_below_s4:
            self.rejected_s4 = True

        # Position counters
        if c > pc.r3: self.bars_above_r3 += 1
        if c < pc.s3: self.bars_below_s3 += 1
        if pc.s3 <= c <= pc.r3: self.bars_inside_band += 1

        # Double distribution: detect two distinct value clusters
        # First distribution: price accepted above R3 for ≥3 bars
        if self.bars_above_r3 == 3:
            self.first_range_high = self.session_high
            self.first_range_low  = pc.r3
        # Second distribution: after first, new cluster forms above R4
        if (self.first_range_high > -math.inf
                and self.closed_above_r4
                and self.bars_above_r3 >= 5):
            self.distribution_shift = True


# ─────────────────────────────────────────────────────────────────────────────
#  DayTypeClassifier — main class
# ─────────────────────────────────────────────────────────────────────────────

# Signal threshold modifiers by day type
_SIGNAL_MODIFIERS: Dict[DayType, int] = {
    DayType.TRENDING   : -8,    # easier entry — ride the move
    DayType.DOUBLE_DIST: -5,    # breakout between distributions
    DayType.REVERSAL   : -5,    # counter-trend fade setup (applied in entry_logic)
    DayType.RANGE      : +8,    # only at true extremes
    DayType.NEUTRAL    : +10,   # scalp zone only
    DayType.NON_TREND  : +15,   # effectively blocked
    DayType.UNKNOWN    :  0,    # neutral until classified
}

# PM overrides by day type
_PM_TRAIL_STEP: Dict[DayType, Optional[float]] = {
    DayType.TRENDING   : 0.10,  # tighter trail — lock gains faster
    DayType.RANGE      : 0.20,  # wider trail — normal
    DayType.NON_TREND  : None,
    DayType.REVERSAL   : None,
    DayType.DOUBLE_DIST: 0.12,
    DayType.NEUTRAL    : None,
    DayType.UNKNOWN    : None,
}

_PM_MAX_HOLD: Dict[DayType, Optional[int]] = {
    DayType.TRENDING   : None,   # let trail run
    DayType.RANGE      : 12,     # don't overstay in band
    DayType.NON_TREND  :  8,     # exit fast
    DayType.REVERSAL   : 15,     # moderate
    DayType.DOUBLE_DIST: None,
    DayType.NEUTRAL    :  8,
    DayType.UNKNOWN    : None,
}

# Minimum bars before classification is attempted
_MIN_BARS_FOR_CLASSIFICATION = 15    # 45 minutes of 3m data


class DayTypeClassifier:
    """
    Real-time day type classifier.  Call update() on every new 3m bar.

    The classifier is stateful — it accumulates intraday evidence and
    upgrades its confidence as more bars are processed.  Early bars
    return DayType.UNKNOWN with LOW confidence.

    Re-classification can happen until 12:00 — after midday the day type
    is locked (set lock=True or call lock_classification()).
    """

    def __init__(self, pivot_context: PivotContext):
        self.pc     = pivot_context
        self._state = _IntraState()
        self._locked_result: Optional[DayTypeResult] = None
        self._last_result  : Optional[DayTypeResult] = None
        self._classification_log: List[str] = []

    def lock_classification(self) -> None:
        """Freeze the current classification — no further changes."""
        if self._last_result:
            self._locked_result = self._last_result
            logging.info(
                f"[DAY TYPE LOCKED] {self._locked_result.name.value} "
                f"confidence={self._locked_result.confidence}"
            )

    @property
    def current(self) -> DayTypeResult:
        return self._last_result or DayTypeResult()

    def update(self, candles_3m: pd.DataFrame) -> DayTypeResult:
        """
        Process the latest bar and return the current DayTypeResult.
        Call this every bar from the main loop.
        """
        if self._locked_result is not None:
            return self._locked_result

        if candles_3m is None or len(candles_3m) < 2:
            return DayTypeResult()

        bar = candles_3m.iloc[-1]
        self._state.update(bar, self.pc)

        # Not enough data yet — defer classification
        if self._state.bars_processed < _MIN_BARS_FOR_CLASSIFICATION:
            result = DayTypeResult(
                name            = DayType.UNKNOWN,
                confidence      = "LOW",
                signal_modifier = 0,
                r3=self.pc.r3, r4=self.pc.r4, r5=self.pc.r5,
                s3=self.pc.s3, s4=self.pc.s4, s5=self.pc.s5,
                cpr_width  = self.pc.cpr_width,
                prev_range = self.pc.prev_range,
            )
            self._last_result = result
            return result

        result = self._classify()
        self._last_result = result

        # Log on first HIGH-confidence classification
        if result.confidence == "HIGH" and (
            self._locked_result is None and
            (not self._last_result or self._last_result.name != result.name)
        ):
            result.log()

        return result

    # ── Core classification logic ─────────────────────────────────────────────

    def _classify(self) -> DayTypeResult:
        pc  = self.pc
        st  = self._state
        ev  = []   # evidence list

        # ── Pre-market structural bias ──────────────────────────────────────
        narrow_cpr         = pc.is_narrow_cpr
        compressed_cam     = pc.is_compressed_camarilla
        structural_breakout = narrow_cpr or compressed_cam

        if narrow_cpr:
            ev.append(f"NarrowCPR width={pc.cpr_width:.1f} "
                      f"({pc.cpr_width/pc.pivot*100:.3f}% of pivot) — trending bias")
        if compressed_cam:
            ev.append(f"CompressedCam R3-S3={pc.r3_s3_band:.1f} "
                      f"< 50%×prev_range={pc.prev_range*0.5:.1f}")

        # ── Intraday evidence ───────────────────────────────────────────────
        breakout_up   = st.closed_above_r4
        breakout_down = st.closed_below_s4
        both_sides    = st.touched_r4 and st.touched_s4
        stayed_inside = (not st.closed_above_r3 and not st.closed_below_s3)
        oscillating   = (st.bars_above_r3 >= 2 and st.bars_below_s3 >= 2)

        # ── 1. TRENDING — closed above R4 or below S4 ──────────────────────
        if breakout_up or breakout_down:
            direction = "CALL (above R4)" if breakout_up else "PUT (below S4)"
            ev.append(f"Trending: closed {direction}")
            # Running toward R5/S5?
            if breakout_up and st.session_high >= pc.r5 * 0.998:
                ev.append(f"R5 target reached: high={st.session_high:.0f} R5={pc.r5:.0f}")
                confidence = "HIGH"
            elif breakout_down and st.session_low <= pc.s5 * 1.002:
                ev.append(f"S5 target reached: low={st.session_low:.0f} S5={pc.s5:.0f}")
                confidence = "HIGH"
            elif st.bars_above_r3 + st.bars_below_s3 >= 5:
                confidence = "HIGH"
            else:
                confidence = "MEDIUM"

            # Double distribution sub-check before confirming TRENDING
            if st.distribution_shift and breakout_up:
                ev.append("Distribution shift detected — upgrading to DOUBLE_DIST")
                return self._make_result(DayType.DOUBLE_DIST, confidence, ev)

            return self._make_result(DayType.TRENDING, confidence, ev)

        # ── 2. REVERSAL — touched R4/S4 but rejected back inside ──────────
        if (st.rejected_r4 or st.rejected_s4) and not breakout_up and not breakout_down:
            direction = "bearish (R4 reject)" if st.rejected_r4 else "bullish (S4 reject)"
            ev.append(f"Reversal: {direction}")
            confidence = "HIGH" if (st.bars_inside_band >= 5) else "MEDIUM"
            return self._make_result(DayType.REVERSAL, confidence, ev)

        # ── 3. NEUTRAL — touched both R4 and S4, still inside R3/S3 ───────
        if both_sides and not breakout_up and not breakout_down:
            ev.append(f"Neutral: touched R4={st.touched_r4} S4={st.touched_s4}, "
                      f"no decisive break")
            confidence = "MEDIUM"
            return self._make_result(DayType.NEUTRAL, confidence, ev)

        # ── 4. RANGE — oscillating between R3 and S3 ───────────────────────
        if oscillating or (st.bars_inside_band >= 8 and not structural_breakout):
            ev.append(f"Range: bars_inside={st.bars_inside_band} "
                      f"above_r3={st.bars_above_r3} below_s3={st.bars_below_s3}")
            confidence = "HIGH" if st.bars_inside_band >= 10 else "MEDIUM"
            return self._make_result(DayType.RANGE, confidence, ev)

        # ── 5. NON-TREND — never reached R3 or S3 ──────────────────────────
        if stayed_inside and st.bars_processed >= 20:
            ev.append(f"NonTrend: never closed outside R3/S3 after {st.bars_processed} bars")
            confidence = "MEDIUM"
            return self._make_result(DayType.NON_TREND, confidence, ev)

        # ── 6. Structural pre-market bias → tentative TRENDING ─────────────
        if structural_breakout and st.bars_processed >= _MIN_BARS_FOR_CLASSIFICATION:
            ev.append(f"Structural breakout bias (narrow CPR/compressed cam) "
                      f"— tentative TRENDING")
            confidence = "LOW"
            return self._make_result(DayType.TRENDING, confidence, ev)

        # ── Default — insufficient evidence ────────────────────────────────
        ev.append(f"Insufficient evidence after {st.bars_processed} bars — UNKNOWN")
        return DayTypeResult(
            name            = DayType.UNKNOWN,
            confidence      = "LOW",
            signal_modifier = 0,
            evidence        = ev,
            r3=pc.r3, r4=pc.r4, r5=pc.r5,
            s3=pc.s3, s4=pc.s4, s5=pc.s5,
            cpr_width  = pc.cpr_width,
            prev_range = pc.prev_range,
        )

    def _make_result(
        self,
        day_type  : DayType,
        confidence: str,
        evidence  : List[str],
    ) -> DayTypeResult:
        pc = self.pc
        return DayTypeResult(
            name             = day_type,
            confidence       = confidence,
            signal_modifier  = _SIGNAL_MODIFIERS[day_type],
            pm_trail_step    = _PM_TRAIL_STEP[day_type],
            pm_max_hold      = _PM_MAX_HOLD[day_type],
            evidence         = evidence,
            r3=pc.r3, r4=pc.r4, r5=pc.r5,
            s3=pc.s3, s4=pc.s4, s5=pc.s5,
            cpr_width  = pc.cpr_width,
            prev_range = pc.prev_range,
        )

    # ── Diagnostic helpers ────────────────────────────────────────────────────

    def state_summary(self) -> str:
        """One-line intraday state for logging."""
        st = self._state
        pc = self.pc
        return (
            f"bars={st.bars_processed} "
            f"hi={st.session_high:.0f}(R3={pc.r3:.0f} R4={pc.r4:.0f} R5={pc.r5:.0f}) "
            f"lo={st.session_low:.0f}(S3={pc.s3:.0f} S4={pc.s4:.0f} S5={pc.s5:.0f}) "
            f"touchR4={st.touched_r4} touchS4={st.touched_s4} "
            f"rejR4={st.rejected_r4} rejS4={st.rejected_s4} "
            f"insideBand={st.bars_inside_band} distShift={st.distribution_shift}"
        )


# ─────────────────────────────────────────────────────────────────────────────
#  Integration helpers — plug into entry_logic and position_manager
# ─────────────────────────────────────────────────────────────────────────────

def apply_day_type_to_threshold(
    base_threshold : int,
    day_type_result: DayTypeResult,
    side           : str,
) -> Tuple[int, str]:
    """
    Apply day type modifier to the entry threshold.

    For REVERSAL days the modifier is only applied to the COUNTER-TREND side:
        If day_type=REVERSAL and rejected R4 → apply -5 only to PUT entries
        If day_type=REVERSAL and rejected S4 → apply -5 only to CALL entries

    Returns (adjusted_threshold, flag_string) where flag_string is appended
    to the surcharge_note log.
    """
    if day_type_result.is_unknown:
        return base_threshold, ""

    mod  = day_type_result.signal_modifier
    name = day_type_result.name.value

    # REVERSAL: only apply the discount to the counter-trend (fading) side
    if day_type_result.is_reversal:
        st = day_type_result  # reuse as namespace
        r4_rejected = st.r4 > 0 and st.s4 > 0  # basic guard
        if side == "PUT":   # fading a failed bullish breakout → apply discount
            pass
        elif side == "CALL": # fading a failed bearish breakout → apply discount
            pass
        else:
            mod = 0    # unknown side → no change

    new_threshold = base_threshold + mod
    flag = f"DT_{name}{mod:+d}" if mod != 0 else ""
    return new_threshold, flag


def apply_day_type_to_pm(
    pm,
    day_type_result: DayTypeResult,
) -> None:
    """
    Override PositionManager thresholds based on day type.
    Call once per trade at position open time.

    pm: PositionManager instance
    """
    if day_type_result.is_unknown:
        return

    if day_type_result.pm_trail_step is not None:
        old = pm.TRAIL_STEP
        pm.TRAIL_STEP = day_type_result.pm_trail_step
        logging.info(
            f"{CYAN}[PM DAY TYPE] TRAIL_STEP {old:.2f}→{pm.TRAIL_STEP:.2f} "
            f"({day_type_result.name.value}){RESET}"
        )

    if day_type_result.pm_max_hold is not None:
        old = pm.MAX_HOLD
        pm.MAX_HOLD = day_type_result.pm_max_hold
        logging.info(
            f"{CYAN}[PM DAY TYPE] MAX_HOLD {old}→{pm.MAX_HOLD} bars "
            f"({day_type_result.name.value}){RESET}"
        )


# ─────────────────────────────────────────────────────────────────────────────
#  Convenience constructor — build from the same level dicts used everywhere
# ─────────────────────────────────────────────────────────────────────────────

def make_day_type_classifier(
    camarilla_levels : Dict,
    cpr_levels       : Dict,
    prev_high        : float,
    prev_low         : float,
    prev_close       : float,
) -> DayTypeClassifier:
    """
    One-liner factory. Build from the same dicts passed to detect_signal().

    Example (in execution_v3 replay loop, once per session):
        dtc = make_day_type_classifier(cam, cpr, prev_h, prev_l, prev_c)

    Then every bar:
        day_type = dtc.update(slice_3m)
    """
    pc = build_pivot_context(
        camarilla_levels, cpr_levels,
        prev_high, prev_low, prev_close,
    )
    return DayTypeClassifier(pc)


# ─────────────────────────────────────────────────────────────────────────────
#  Quick self-test (run: python day_type.py)
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    # Simulate a TRENDING day: prev NIFTY session
    prev_h, prev_l, prev_c = 25600.0, 25350.0, 25480.0
    rng = prev_h - prev_l  # 250

    cam = {
        "r3": prev_c + rng * 1.1 / 4,   # 25548.75
        "r4": prev_c + rng * 1.1 / 2,   # 25617.50
        "s3": prev_c - rng * 1.1 / 4,   # 25411.25
        "s4": prev_c - rng * 1.1 / 2,   # 25342.50
    }
    cpr = {
        "pivot": (prev_h + prev_l + prev_c) / 3,      # 25476.67
        "bc":    (prev_h + prev_l) / 2,               # 25475.00
        "tc":    ((prev_h + prev_l + prev_c) / 3
                  - (prev_h + prev_l) / 2)
                 + (prev_h + prev_l + prev_c) / 3,    # 25478.34
    }

    dtc = make_day_type_classifier(cam, cpr, prev_h, prev_l, prev_c)

    print("\n" + "═"*65)
    print(f"  Pivot context:")
    print(f"    R5={dtc.pc.r5:.0f}  R4={cam['r4']:.0f}  R3={cam['r3']:.0f}")
    print(f"    CPR: tc={cpr['tc']:.0f} pivot={cpr['pivot']:.0f} bc={cpr['bc']:.0f}")
    print(f"    S3={cam['s3']:.0f}  S4={cam['s4']:.0f}  S5={dtc.pc.s5:.0f}")
    print(f"    NarrowCPR={dtc.pc.is_narrow_cpr}  CompressedCam={dtc.pc.is_compressed_camarilla}")
    print("═"*65)

    # Simulate 25 bars: trending up, closes above R4 at bar 18
    import numpy as np
    closes = (
        list(np.linspace(25490, 25540, 10)) +   # bars 1-10: inside R3
        list(np.linspace(25540, 25560, 5))  +   # bars 11-15: near R3
        list(np.linspace(25560, 25630, 10))      # bars 16-25: above R4=25617
    )
    highs  = [c + 8  for c in closes]
    lows   = [c - 8  for c in closes]
    opens  = [c - 3  for c in closes]

    df = pd.DataFrame({
        "open": opens, "high": highs, "low": lows, "close": closes,
        "time": pd.date_range("2026-02-20 09:15", periods=25, freq="3min"),
    })

    print(f"\n  Simulating {len(df)} bars...\n")
    last_type = None
    for i in range(1, len(df) + 1):
        result = dtc.update(df.iloc[:i])
        if result.name != last_type:
            print(f"  Bar {i:>2} close={df.iloc[i-1]['close']:.0f} "
                  f"→ DayType={result.name.value:<12} "
                  f"confidence={result.confidence:<6} "
                  f"modifier={result.signal_modifier:+d}pts")
            last_type = result.name

    print(f"\n  Final state: {dtc.state_summary()}")
    final = dtc.current
    print(f"\n  Day type    : {final.name.value}")
    print(f"  Confidence  : {final.confidence}")
    print(f"  Threshold Δ : {final.signal_modifier:+d} pts")
    print(f"  Trail step  : {final.pm_trail_step}")
    print(f"  Max hold    : {final.pm_max_hold}")
    print("\n" + "═"*65)

    # Scenario 2: RANGE day
    print("\n  [RANGE DAY SIMULATION]")
    dtc2 = make_day_type_classifier(cam, cpr, prev_h, prev_l, prev_c)
    range_closes = [25490, 25510, 25480, 25520, 25470, 25500,
                    25530, 25490, 25510, 25480, 25520, 25500,
                    25510, 25490, 25530, 25500, 25480, 25510,
                    25490, 25520]
    df2 = pd.DataFrame({
        "open":  [c-2 for c in range_closes],
        "high":  [c+10 for c in range_closes],
        "low":   [c-10 for c in range_closes],
        "close": range_closes,
        "time":  pd.date_range("2026-02-20 09:15", periods=20, freq="3min"),
    })
    for i in range(1, len(df2)+1):
        result2 = dtc2.update(df2.iloc[:i])
    print(f"  Day type: {result2.name.value}  "
          f"confidence={result2.confidence}  "
          f"modifier={result2.signal_modifier:+d}pts")

    print("\n✅ day_type.py self-test complete\n")
