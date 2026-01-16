# ===== signals.py =====
import logging
from setup import spot_price
from indicators import momentum_ok


# ===========================================================
# ANSI COLORS for order logs
RESET   = "\033[0m"
GREEN   = "\033[92m"
YELLOW  = "\033[93m"
RED     = "\033[91m"
MAGENTA = "\033[95m"
GRAY    = "\033[90m"

#===========================================================


def candle_accepts_direction(candle, side, atr):
    body = abs(candle.close - candle.open)
    rng  = candle.high - candle.low

    if rng == 0:
        return False

    # Avoid weak / noise candles
    if body < 0.25 * atr:
        return False

    if side == "CALL":
        return candle.close > candle.open and body > 0.4 * rng
    else:
        return candle.close < candle.open and body > 0.4 * rng


def atr_is_sane(atr, atr_prev):
    if atr_prev == 0:
        return False
    return atr > 0.8 * atr_prev


def level_accepted(candle, level, side):
    if side == "CALL":
        return candle.close > level and candle.low > level
    else:
        return candle.close < level and candle.high < level


# ===========================================================
# CONTEXT-BASED SIGNAL ENGINE (REFactored)
# ===========================================================
def detect_signal(ctx: dict):
    """
    Structure-aware signal detection.
    Supports breakout / breakdown with acceptance & rejection
    across CPR, Camarilla, and Traditional pivots.
    """

    import logging

    df = ctx.get("candles_3m")
    pivots = ctx.get("pivots", {})
    atr = ctx.get("atr")

    if df is None or len(df) < 3 or atr is None:
        return None, None

    last = df.iloc[-1]
    prev = df.iloc[-2]

    logging.info(f"[DETECT_SIGNAL] candles={len(df)}")
    logging.info(
        f"[MARKET] close={last.close:.2f} open={last.open:.2f} "
        f"high={last.high:.2f} low={last.low:.2f} ATR={atr:.2f}"
    )

    # --------------------------------------------------
    # Helper functions (price-action based)
    # --------------------------------------------------

    def bullish_acceptance(candle, level):
        body = abs(candle.close - candle.open)
        return (
            candle.close > level and
            body > 0.35 * atr and
            candle.low > level
        )

    def bearish_acceptance(candle, level):
        body = abs(candle.close - candle.open)
        return (
            candle.close < level and
            body > 0.35 * atr and
            candle.high < level
        )

    def bullish_rejection(candle, level):
        return (
            candle.high > level and
            candle.close < level and
            (candle.high - candle.close) > 0.35 * atr
        )

    def bearish_rejection(candle, level):
        return (
            candle.low < level and
            candle.close > level and
            (candle.close - candle.low) > 0.35 * atr
        )

    # --------------------------------------------------
    # Unified pivot map (ordered by importance)
    # --------------------------------------------------
    pivot_levels = [
        ("CPR_TC", pivots.get("TC")),
        ("CPR_BC", pivots.get("BC")),

        ("R4", pivots.get("R4")),
        ("S4", pivots.get("S4")),

        ("R3", pivots.get("R3")),
        ("S3", pivots.get("S3")),

        ("R2", pivots.get("R2")),
        ("S2", pivots.get("S2")),

        ("R1", pivots.get("R1")),
        ("S1", pivots.get("S1")),
    ]

    logging.info(
        "[LEVELS] " +
        " ".join(f"{name}={lvl}" for name, lvl in pivot_levels if lvl)
    )

    # --------------------------------------------------
    # Signal evaluation (top-down, structure first)
    # --------------------------------------------------
    for name, level in pivot_levels:
        if level is None:
            continue

        # -------------------------
        # BREAKOUT (CALL)
        # -------------------------
        if bullish_acceptance(last, level):

            # R4 RULE (LOCKED-IN)
            if name == "R4":
                logging.info("[CHECK] R4 acceptance confirmed")
                return "CALL", "R4_TREND_BREAK"

            if name.startswith("R"):
                return "CALL", f"{name}_BREAKOUT"

            if name == "CPR_TC":
                return "CALL", "CPR_TC_BREAK"

        # -------------------------
        # BREAKDOWN (PUT)
        # -------------------------
        if bearish_acceptance(last, level):

            if name == "S4":
                return "PUT", "S4_TREND_BREAK"

            if name.startswith("S"):
                return "PUT", f"{name}_BREAKDOWN"

            if name == "CPR_BC":
                return "PUT", "CPR_BC_BREAK"

        # -------------------------
        # REJECTIONS (MEAN REVERSION / STRUCTURAL FAILURE)
        # -------------------------
        if bullish_rejection(last, level):
            logging.info(f"[REJECT] Failed breakout at {name}")
            if name.startswith("R"):
                return "PUT", f"{name}_REJECT"

        if bearish_rejection(last, level):
            logging.info(f"[REJECT] Failed breakdown at {name}")
            if name.startswith("S"):
                return "CALL", f"{name}_REJECT"

    logging.info("[DETECT_SIGNAL] No valid setup found")
    return None, None
