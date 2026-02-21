# ============================================================
#  position_manager.py  — v1.0
#  Unified Position Manager for NSE NIFTY Options Buying Bot
# ============================================================
"""
Single source of truth for ALL trade lifecycle management.
Works identically in three execution contexts:

  Context         How PM is used
  ─────────────── ─────────────────────────────────────────────────────────
  REPLAY          PositionManager(mode="REPLAY")  — no broker, simulates LTP
  PAPER           PositionManager(mode="PAPER")   — logs orders, no real money
  LIVE            PositionManager(mode="LIVE")    — calls Fyers API for exits

Design principles
─────────────────
• One trade at a time — is_open() guard prevents any duplicate entry.
• Immutable entry snapshot — entry price / side / score captured at open(),
  never mutated. All exit logic reads from indicators, not from entry params.
• Layered exits (evaluated in strict priority order every bar):
    1. HARD_STOP        — premium drops to HARD_STOP_FRAC of entry
    2. TRAIL_STOP       — ratcheting trail after TRAIL_ACTIVATE gain
    3. PARTIAL          — move hard stop to breakeven at PARTIAL_FRAC gain
    4. MOMENTUM_PEAK    — RSI extreme + (CCI extreme OR slope reversal)
    5. EMA_CROSS        — EMA9 crosses EMA13 against side (post-partial only)
    6. ST_FLIP_2        — SuperTrend flips AGAINST side for 2 consecutive bars
    7. REVERSAL_3       — 3 consecutive candles closing against side
    8. OSC_EXHAUSTION   — 2-of-3 oscillators (RSI/CCI/WR) in extreme zone
    9. MAX_HOLD         — hard cap (extendable if trail active + profitable)
   10. EOD_EXIT         — force-close at EOD_MIN IST

All thresholds are class-level constants — one place to tune everything.

Usage (replay)
──────────────
    from position_manager import PositionManager

    pm = PositionManager(mode="REPLAY", lot_size=50)

    # Main bar loop
    if pm.is_open():
        decision = pm.update(bar_idx, bar_time, underlying_close, indicator_row)
        if decision.should_exit:
            record = pm.close(bar_idx, bar_time, underlying_close,
                              decision.exit_px, decision.reason)
            trade_log.append(record)
    elif signal:
        pm.open(bar_idx, bar_time, underlying_close, entry_premium, signal)

Usage (live / paper)
─────────────────────
    pm = PositionManager(mode="LIVE", lot_size=50,
                         broker_exit_fn=send_live_exit_order)

    # Called on every 3m candle tick
    if pm.is_open():
        decision = pm.update(bar_idx, bar_time, ltp, indicator_row)
        if decision.should_exit:
            pm.close(bar_idx, bar_time, ltp, ltp, decision.reason)
    elif signal:
        pm.open(bar_idx, bar_time, spot, ltp, signal)
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, Optional

# ── ANSI colour helpers (safe on Windows too via PYTHONIOENCODING) ─────────────
GREEN  = "\033[92m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
RED    = "\033[91m"
RESET  = "\033[0m"


# ─────────────────────────────────────────────────────────────────────────────
#  ExitDecision — returned by update() every bar
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ExitDecision:
    """Immutable result of one bar's exit evaluation."""
    should_exit : bool   = False
    reason      : str    = ""
    exit_px     : float  = 0.0          # simulated / actual option LTP
    cur_gain    : float  = 0.0          # current gain vs entry premium
    peak_gain   : float  = 0.0          # max gain seen so far
    bars_held   : int    = 0

    @property
    def is_win(self) -> bool:
        return self.exit_px > 0 and self.should_exit and self.cur_gain > 0


# ─────────────────────────────────────────────────────────────────────────────
#  PositionManager
# ─────────────────────────────────────────────────────────────────────────────

class PositionManager:
    """
    Manages the lifecycle of a single options-buying position
    (CALL or PUT long) across replay, paper, and live modes.
    """

    # ── Tunable exit thresholds ───────────────────────────────────────────────
    # Premium-based exits (fractions of entry premium)
    HARD_STOP_FRAC  : float = 0.45   # exit if LTP drops to 45 % of entry
    PARTIAL_FRAC    : float = 0.50   # lock breakeven after 50 % gain
    TRAIL_ACTIVATE  : float = 0.25   # start trailing after 25 % gain
    TRAIL_STEP      : float = 0.15   # trail gives back ≤15 % of peak gain

    # Oscillator exit thresholds
    RSI_OB          : float = 70.0   # RSI overbought  (CALL exit)
    RSI_OS          : float = 30.0   # RSI oversold    (PUT  exit)
    CCI_EXTREME     : float = 120.0  # |CCI| threshold for exhaustion
    WR_OB           : float = -10.0  # Williams %R overbought  (CALL exit)
    WR_OS           : float = -88.0  # Williams %R oversold    (PUT  exit)

    # Time / bar limits
    MIN_HOLD        : int   = 3      # bars before momentum / ST exits activate
    MAX_HOLD        : int   = 20     # hard cap in bars (3m × 20 = 60 min)
    MAX_HOLD_EXT    : int   = 10     # extra bars allowed when trail active + profitable
    EOD_MIN         : int   = 15 * 60 + 10   # 15:10 IST in minutes-since-midnight

    # Delta approximation for replay (ATM ≈ 0.50 delta)
    DELTA_APPROX    : float = 0.50

    def __init__(
        self,
        mode        : str = "REPLAY",
        lot_size    : int = 50,
        broker_exit_fn : Optional[Callable] = None,
    ):
        """
        Parameters
        ──────────
        mode            "REPLAY" | "PAPER" | "LIVE"
        lot_size        NSE lot size (default 50 for NIFTY)
        broker_exit_fn  Callable(symbol, qty, reason) → (bool, order_id)
                        Required for LIVE / PAPER modes. Ignored in REPLAY.
        """
        self.mode           = mode.upper()
        self.lot_size       = lot_size
        self.broker_exit_fn = broker_exit_fn
        self._t : Optional[Dict[str, Any]] = None   # active trade state
        self._prev_st       : Optional[str] = None   # previous bar's ST bias

    # ── Public API ────────────────────────────────────────────────────────────

    def is_open(self) -> bool:
        """True when a position is currently active."""
        return self._t is not None

    def open(
        self,
        bar_idx       : int,
        bar_time      : Any,
        underlying    : float,
        entry_premium : float,
        signal        : Dict[str, Any],
    ) -> None:
        """
        Record a new entry. Raises RuntimeError if a position is already open.

        Parameters
        ──────────
        bar_idx         Sequential bar index (used for hold-time accounting)
        bar_time        Bar timestamp (str or datetime-like)
        underlying      NIFTY spot price at entry
        entry_premium   Option LTP at entry (actual fill or ATM approximation)
        signal          Signal dict — must contain 'side' ('CALL' or 'PUT')
                        May also contain 'score', 'source', 'pivot', 'st_bias'
        """
        if self._t is not None:
            raise RuntimeError(
                f"[PM] open() called while position already open "
                f"(side={self._t['side']} since {self._t['entry_time']})"
            )

        side = signal["side"].upper()
        assert side in ("CALL", "PUT"), f"Invalid side: {side}"

        self._t = {
            # ── entry snapshot (immutable) ──
            "entry_bar"   : bar_idx,
            "entry_time"  : bar_time,
            "entry_ul"    : underlying,
            "entry_px"    : entry_premium,
            "side"        : side,
            "score"       : signal.get("score",   0),
            "source"      : signal.get("source",  "?"),
            "pivot"       : signal.get("pivot",   ""),
            "entry_st"    : str(signal.get("st_bias", "?")),
            "option_name" : signal.get("option_name", ""),

            # ── mutable exit-tracking state ──
            "bars_held"   : 0,
            "peak_px"     : entry_premium,
            "partial_done": False,
            "hard_stop"   : entry_premium * self.HARD_STOP_FRAC,
            "trail_active": False,
            "trail_stop"  : None,
            "consec_rev"  : 0,          # consecutive reversal candle counter
            "st_flip_bars": 0,          # consecutive ST-against-side counter
            "prev_ema_gap": None,       # for momentum plateau detection
            "peak_momentum": 0.0,
            "plateau_count": 0,
            "trail_updates": 0,
        }
        self._prev_st = None

        qty = self.lot_size
        logging.info(
            f"{GREEN}[TRADE OPEN][{self.mode}] {side} "
            f"bar={bar_idx} {bar_time} "
            f"underlying={underlying:.2f} "
            f"premium={entry_premium:.2f} "
            f"score={signal.get('score','?')} "
            f"src={signal.get('source','?')} "
            f"pivot={signal.get('pivot','')} "
            f"lot={qty}{RESET}"
        )

    def update(
        self,
        bar_idx    : int,
        bar_time   : Any,
        underlying : float,
        row        : Any,
    ) -> ExitDecision:
        """
        Evaluate exit conditions for the current bar.

        Call this every bar while is_open() is True.
        Returns an ExitDecision; if decision.should_exit is True,
        immediately call close() with the same bar parameters.

        Parameters
        ──────────
        bar_idx     Current bar index
        bar_time    Current bar timestamp
        underlying  Current NIFTY spot close
        row         Pandas Series (or dict) with columns:
                    rsi14, cci20, supertrend_bias, slope, ema9, ema13
        """
        if not self._t:
            return ExitDecision()

        t   = self._t
        ep  = t["entry_px"]
        side = t["side"]

        # ── Simulate current option LTP ───────────────────────────────────────
        # In REPLAY: approximate with delta × Δspot
        # In LIVE/PAPER: caller should pass actual LTP as `underlying` param
        # and set DELTA_APPROX=1.0 so cur = underlying directly.
        if self.mode == "REPLAY":
            move = underlying - t["entry_ul"]
            if side == "PUT":
                move = -move
            cur = max(0.1, ep + self.DELTA_APPROX * move)
        else:
            # LIVE/PAPER: underlying IS the actual option LTP
            cur = max(0.1, underlying)

        # ── Update bar state ──────────────────────────────────────────────────
        t["bars_held"] += 1
        if cur > t["peak_px"]:
            t["peak_px"] = cur

        gain     = t["peak_px"] - ep     # maximum seen
        cur_gain = cur - ep              # current unrealised

        # ── Safe indicator reads ──────────────────────────────────────────────
        def _f(k, default=float("nan")):
            try:
                v = float(row[k]) if hasattr(row, "__getitem__") else float(getattr(row, k))
                return v if math.isfinite(v) else default
            except Exception:
                return default

        rsi  = _f("rsi14",  50.0)
        cci  = _f("cci20",   0.0)
        ema9 = _f("ema9",  float("nan"))
        ema13= _f("ema13", float("nan"))

        try:
            st  = str(row["supertrend_bias"] if hasattr(row, "__getitem__") else row.supertrend_bias).upper()
        except Exception:
            st  = "?"
        try:
            slp = str(row["slope"] if hasattr(row, "__getitem__") else row.slope).upper()
        except Exception:
            slp = "?"

        # ── Bar time → minutes since midnight ────────────────────────────────
        bar_min = self._bar_minutes(bar_time)

        # ══════════════════════════════════════════════════════════════════════
        #  EXIT LADDER  (evaluated in strict priority order)
        # ══════════════════════════════════════════════════════════════════════

        # ── 1. HARD STOP ──────────────────────────────────────────────────────
        if cur <= t["hard_stop"]:
            return self._exit(cur, "HARD_STOP", cur_gain, gain, t["bars_held"])

        # ── 2. TRAIL STOP ─────────────────────────────────────────────────────
        if not t["trail_active"] and gain >= ep * self.TRAIL_ACTIVATE:
            t["trail_active"] = True
            t["trail_stop"]   = ep + gain * (1.0 - self.TRAIL_STEP)
            logging.info(
                f"  {CYAN}[TRAIL ON] bar={bar_idx} {bar_time} "
                f"peak_gain=+{gain:.1f} "
                f"trail_stop={t['trail_stop']:.2f}{RESET}"
            )

        if t["trail_active"]:
            new_trail = ep + gain * (1.0 - self.TRAIL_STEP)
            if new_trail > t["trail_stop"]:
                old = t["trail_stop"]
                t["trail_stop"] = new_trail
                t["trail_updates"] = t.get("trail_updates", 0) + 1
                logging.debug(
                    f"  [TRAIL UPDATE] {old:.2f} → {new_trail:.2f} "
                    f"peak_gain=+{gain:.1f}"
                )
            if cur <= t["trail_stop"]:
                return self._exit(cur, "TRAIL_STOP", cur_gain, gain, t["bars_held"])

        # ── 3. PARTIAL PROFIT → move hard stop to breakeven ───────────────────
        if not t["partial_done"] and cur_gain >= ep * self.PARTIAL_FRAC:
            t["partial_done"] = True
            t["hard_stop"]    = ep          # can't lose from here
            logging.info(
                f"  {CYAN}[PARTIAL] bar={bar_idx} {bar_time} "
                f"{side} cur={cur:.2f} gain=+{cur_gain:.1f} "
                f"→ hard stop moved to breakeven {ep:.2f}{RESET}"
            )

        # ── Exits below only activate after MIN_HOLD bars ─────────────────────
        if t["bars_held"] < self.MIN_HOLD:
            return ExitDecision(
                should_exit=False, cur_gain=cur_gain,
                peak_gain=gain, bars_held=t["bars_held"]
            )

        # ── 4. MOMENTUM PEAK ─────────────────────────────────────────────────
        #       RSI extreme AND (CCI extreme OR slope reversal)
        rsi_extreme = (side == "CALL" and rsi >= self.RSI_OB) or \
                      (side == "PUT"  and rsi <= self.RSI_OS)
        cci_extreme = (side == "CALL" and cci >= self.CCI_EXTREME) or \
                      (side == "PUT"  and cci <= -self.CCI_EXTREME)
        slope_rev   = (side == "CALL" and slp == "DOWN") or \
                      (side == "PUT"  and slp == "UP")

        if rsi_extreme and (cci_extreme or slope_rev):
            return self._exit(cur, "MOMENTUM_PEAK", cur_gain, gain, t["bars_held"])

        # ── 5. EMA CROSS (post-partial only — avoids premature exits) ─────────
        if t["partial_done"] and math.isfinite(ema9) and math.isfinite(ema13):
            if (side == "CALL" and ema9 < ema13) or \
               (side == "PUT"  and ema9 > ema13):
                return self._exit(cur, "EMA_CROSS", cur_gain, gain, t["bars_held"])

        # ── 6. SUPERTREND FLIP × 2 consecutive bars ───────────────────────────
        st_against = (side == "CALL" and st == "DOWN") or \
                     (side == "PUT"  and st == "UP")
        if st_against:
            t["st_flip_bars"] += 1
        else:
            t["st_flip_bars"] = 0

        if t["st_flip_bars"] >= 2:
            return self._exit(cur, "ST_FLIP_2", cur_gain, gain, t["bars_held"])

        # ── 7. REVERSAL CANDLES × 3 consecutive ──────────────────────────────
        # Suppressed during strong trends (ADX≥25): in a trending market, 3
        # consecutive pullback candles are normal consolidation — NOT reversal.
        # Only meaningful in choppy / range-bound conditions (ADX<25).
        adx_val   = _f("adx14", float("nan"))   # column name from build_indicator_dataframe
        _trending = math.isfinite(adx_val) and adx_val >= 25.0

        try:
            o = float(row["open"]  if hasattr(row, "__getitem__") else row.open)
            c = float(row["close"] if hasattr(row, "__getitem__") else row.close)
            is_rev = (side == "CALL" and c < o) or (side == "PUT" and c > o)
            t["consec_rev"] = (t["consec_rev"] + 1) if is_rev else 0
        except Exception:
            pass

        if t["consec_rev"] >= 3 and not _trending:
            return self._exit(cur, "REVERSAL_3", cur_gain, gain, t["bars_held"])
        elif t["consec_rev"] >= 3 and _trending:
            logging.debug(
                f"  [REVERSAL_3 SUPPRESSED] ADX={adx_val:.1f}>=25 — trending, "
                f"holding {side} through {t['consec_rev']}-bar pullback"
            )

        # ── 8. OSCILLATOR EXHAUSTION — 2-of-3 ────────────────────────────────
        osc_hits = []
        if (side == "CALL" and rsi >= self.RSI_OB) or \
           (side == "PUT"  and rsi <= self.RSI_OS):
            osc_hits.append(f"RSI={rsi:.0f}")
        if (side == "CALL" and cci >= self.CCI_EXTREME) or \
           (side == "PUT"  and cci <= -self.CCI_EXTREME):
            osc_hits.append(f"CCI={cci:.0f}")
        try:
            wr_col = row.get("williams_r", None) if hasattr(row, "get") else None
            if wr_col is not None and math.isfinite(float(wr_col)):
                wr = float(wr_col)
                if (side == "CALL" and wr >= self.WR_OB) or \
                   (side == "PUT"  and wr <= self.WR_OS):
                    osc_hits.append(f"WR={wr:.0f}")
        except Exception:
            pass

        if len(osc_hits) >= 2:
            logging.info(
                f"  {YELLOW}[OSC EXHAUSTION] {side} {'+'.join(osc_hits)}{RESET}"
            )
            # Hard exit only when profitable (else just lock breakeven)
            if cur_gain > 0:
                return self._exit(cur, "OSC_EXHAUSTION", cur_gain, gain, t["bars_held"])
            else:
                t["hard_stop"] = max(t["hard_stop"], ep)   # at least breakeven

        # ── 9. MAX HOLD ───────────────────────────────────────────────────────
        max_cap = self.MAX_HOLD + (self.MAX_HOLD_EXT if t["trail_active"] and cur_gain >= ep * 0.40 else 0)
        if t["bars_held"] >= max_cap:
            return self._exit(cur, "MAX_HOLD", cur_gain, gain, t["bars_held"])

        # ── 10. EOD FORCE EXIT ────────────────────────────────────────────────
        if bar_min >= self.EOD_MIN:
            return self._exit(cur, "EOD_EXIT", cur_gain, gain, t["bars_held"])

        # ── HOLD ──────────────────────────────────────────────────────────────
        return ExitDecision(
            should_exit=False,
            cur_gain=cur_gain,
            peak_gain=gain,
            bars_held=t["bars_held"],
        )

    def close(
        self,
        bar_idx   : int,
        bar_time  : Any,
        underlying: float,
        exit_px   : float,
        reason    : str,
        qty       : Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Close the active position.  Calls broker_exit_fn in LIVE/PAPER modes.

        Returns a trade record dict suitable for appending to a trade log.
        Resets internal state so is_open() returns False immediately.
        """
        if not self._t:
            raise RuntimeError("[PM] close() called but no position is open")

        t   = self._t
        ep  = t["entry_px"]
        side = t["side"]
        qty  = qty or self.lot_size

        pnl_pts = exit_px - ep
        pnl_val = pnl_pts * qty
        color   = GREEN if pnl_pts >= 0 else RED
        outcome = "WIN " if pnl_pts >= 0 else "LOSS"

        # ── Broker exit in LIVE / PAPER modes ────────────────────────────────
        if self.mode in ("LIVE", "PAPER") and self.broker_exit_fn is not None:
            opt_name = t.get("option_name", "?")
            ok, order_id = self.broker_exit_fn(opt_name, qty, reason)
            if not ok:
                logging.error(
                    f"{RED}[PM] broker_exit_fn failed for {opt_name} "
                    f"reason={reason}{RESET}"
                )
            else:
                logging.info(
                    f"{YELLOW}[PM] Broker exit sent: {opt_name} "
                    f"order_id={order_id}{RESET}"
                )

        logging.info(
            f"{color}[TRADE EXIT][{reason}] {outcome} "
            f"{side} bar={bar_idx} {bar_time} "
            f"premium {ep:.2f}→{exit_px:.2f} "
            f"P&L={pnl_pts:+.2f}pts ({pnl_val:+.0f}₹) "
            f"peak={t['peak_px']:.2f} "
            f"held={t['bars_held']}bars "
            f"trail_updates={t.get('trail_updates',0)}{RESET}"
        )

        record = {
            # ── identity ──
            "mode"          : self.mode,
            "side"          : side,
            "source"        : t.get("source", "?"),
            "score"         : t.get("score",  "?"),
            "pivot"         : t.get("pivot",  ""),
            "option_name"   : t.get("option_name", ""),
            # ── timing ──
            "entry_bar"     : t["entry_bar"],
            "entry_time"    : t["entry_time"],
            "exit_bar"      : bar_idx,
            "exit_time"     : bar_time,
            "bars_held"     : t["bars_held"],
            # ── prices ──
            "entry_ul"      : t["entry_ul"],
            "exit_ul"       : underlying,
            "entry_premium" : ep,
            "exit_premium"  : exit_px,
            "peak_premium"  : t["peak_px"],
            # ── exit metadata ──
            "exit_reason"   : reason,
            "partial_done"  : t["partial_done"],
            "trail_active"  : t["trail_active"],
            "trail_updates" : t.get("trail_updates", 0),
            # ── PnL ──
            "pnl_points"    : round(pnl_pts, 2),
            "pnl_value"     : round(pnl_val, 2),
            "pnl_pct"       : round(pnl_pts / ep * 100, 1) if ep else 0,
            "lot_size"      : qty,
        }

        self._t       = None
        self._prev_st = None
        return record

    def force_close_eod(
        self,
        bar_idx   : int,
        bar_time  : Any,
        underlying: float,
    ) -> Optional[Dict[str, Any]]:
        """
        Force-close any open position at end of day.
        Used by the main orchestration loop at session end.
        Returns trade record or None if no position was open.
        """
        if not self._t:
            return None

        t  = self._t
        ep = t["entry_px"]

        if self.mode == "REPLAY":
            move     = underlying - t["entry_ul"]
            if t["side"] == "PUT":
                move = -move
            exit_px  = max(0.1, ep + self.DELTA_APPROX * move)
        else:
            exit_px  = underlying   # caller passes actual LTP

        logging.warning(
            f"{YELLOW}[PM] force_close_eod: {t['side']} "
            f"held={t['bars_held']}bars — forcing EOD_CLOSE{RESET}"
        )
        return self.close(bar_idx, bar_time, underlying, exit_px, "EOD_CLOSE")

    def position_summary(self) -> str:
        """One-line summary of open position for logging / dashboard."""
        if not self._t:
            return "No open position"
        t = self._t
        return (
            f"{t['side']} | entry={t['entry_px']:.2f} "
            f"| bars={t['bars_held']} "
            f"| peak={t['peak_px']:.2f} "
            f"| trail={'ON' if t['trail_active'] else 'OFF'} "
            f"| partial={'YES' if t['partial_done'] else 'NO'}"
        )

    def state_dict(self) -> Dict[str, Any]:
        """
        Return a deep copy of the internal trade state.
        Useful for persistence (JSON / SQLite) across restarts.
        """
        if not self._t:
            return {}
        return dict(self._t)

    def restore_state(self, state: Dict[str, Any]) -> None:
        """
        Restore position from a previously saved state_dict.
        Used to recover an open position after a process restart.
        """
        if not state:
            return
        required = {"entry_bar", "entry_time", "entry_px", "side"}
        missing  = required - set(state.keys())
        if missing:
            raise ValueError(f"[PM] restore_state missing keys: {missing}")
        self._t = dict(state)
        logging.warning(
            f"{YELLOW}[PM] Position RESTORED from state: "
            f"{self._t['side']} entry={self._t['entry_px']:.2f} "
            f"bars_held={self._t.get('bars_held',0)}{RESET}"
        )

    # ── Private helpers ───────────────────────────────────────────────────────

    def _exit(
        self,
        exit_px   : float,
        reason    : str,
        cur_gain  : float,
        peak_gain : float,
        bars_held : int,
    ) -> ExitDecision:
        """Build and return an ExitDecision. Does NOT call close()."""
        logging.info(
            f"  {YELLOW}[EXIT SIGNAL] reason={reason} "
            f"exit_px={exit_px:.2f} "
            f"gain={cur_gain:+.2f} "
            f"peak={peak_gain:+.2f} "
            f"held={bars_held}bars{RESET}"
        )
        return ExitDecision(
            should_exit = True,
            reason      = reason,
            exit_px     = exit_px,
            cur_gain    = cur_gain,
            peak_gain   = peak_gain,
            bars_held   = bars_held,
        )

    @staticmethod
    def _bar_minutes(bar_time: Any) -> int:
        """Convert bar_time to minutes since midnight (for EOD gate)."""
        try:
            s = str(bar_time).split(" ")[-1]   # "HH:MM:SS" portion
            h, m = int(s[:2]), int(s[3:5])
            return h * 60 + m
        except Exception:
            return 0


# ─────────────────────────────────────────────────────────────────────────────
#  TradeLogger — structured CSV / console output helper
# ─────────────────────────────────────────────────────────────────────────────

class TradeLogger:
    """
    Accumulates trade records from PositionManager.close() calls
    and emits a formatted summary + CSV at end of session.
    """

    def __init__(self, symbol: str = "NSE:NIFTY50-INDEX"):
        self.symbol  = symbol
        self.records = []

    def append(self, record: Dict[str, Any]) -> None:
        self.records.append(record)

    def summary(self, date_str: str = "") -> str:
        """Return a formatted multi-line summary string."""
        sep  = "─" * 64
        n    = len(self.records)
        if n == 0:
            return f"{sep}\n  No trades recorded ({date_str})\n{sep}"

        wins   = sum(1 for r in self.records if r["pnl_points"] > 0)
        losses = n - wins
        total  = sum(r["pnl_value"] for r in self.records)
        avg_held = sum(r["bars_held"] for r in self.records) / n

        exit_counts: Dict[str, int] = {}
        for r in self.records:
            exit_counts[r["exit_reason"]] = exit_counts.get(r["exit_reason"], 0) + 1

        lines = [
            f"\n{sep}",
            f"  RESULTS: {self.symbol}  ({date_str})",
            sep,
            f"  Trades closed : {n}",
            f"  Win / Loss    : {wins} / {losses}  ({100*wins/n:.1f}%)",
            f"  Total PnL (₹) : {total:+.2f}",
            f"  Avg hold      : {avg_held:.1f} bars  ({avg_held*3:.0f} min)",
            "",
            "  Exit breakdown:",
        ]
        for reason, cnt in sorted(exit_counts.items(), key=lambda x: -x[1]):
            lines.append(f"    {reason:<22}: {cnt}")

        lines.append("")
        lines.append("  Trade details:")
        for r in self.records:
            color = GREEN if r["pnl_points"] > 0 else RED
            lines.append(
                f"{color}    {r['side']:<4} "
                f"entry={r['entry_time']} "
                f"exit={r['exit_time']} "
                f"held={r['bars_held']}bars "
                f"pnl={r['pnl_points']:+.1f}pts "
                f"({r['pnl_value']:+.0f}₹) "
                f"[{r['exit_reason']}]{RESET}"
            )

        lines.append(sep)
        return "\n".join(lines)

    def to_csv(self, path: str) -> None:
        """Write all records to a CSV file."""
        try:
            import csv
            if not self.records:
                return
            fieldnames = list(self.records[0].keys())
            with open(path, "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=fieldnames)
                w.writeheader()
                w.writerows(self.records)
            logging.info(f"[TradeLogger] Saved {len(self.records)} trades → {path}")
        except Exception as e:
            logging.error(f"[TradeLogger] CSV write failed: {e}")


# ─────────────────────────────────────────────────────────────────────────────
#  Module-level convenience: build a pre-configured PM for each run mode
# ─────────────────────────────────────────────────────────────────────────────

def make_replay_pm(lot_size: int = 50) -> PositionManager:
    """Return a PositionManager configured for backtesting / replay."""
    return PositionManager(mode="REPLAY", lot_size=lot_size)


def make_paper_pm(
    broker_exit_fn: Callable,
    lot_size: int = 50,
) -> PositionManager:
    """Return a PositionManager configured for paper trading."""
    return PositionManager(mode="PAPER", lot_size=lot_size,
                           broker_exit_fn=broker_exit_fn)


def make_live_pm(
    broker_exit_fn: Callable,
    lot_size: int = 50,
) -> PositionManager:
    """Return a PositionManager configured for live trading."""
    return PositionManager(mode="LIVE", lot_size=lot_size,
                           broker_exit_fn=broker_exit_fn)
