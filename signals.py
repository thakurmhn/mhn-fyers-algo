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


def detect_signal(df, pivots):
    logging.info(
        f"{YELLOW}[DETECT_SIGNAL] candles={len(df)}{RESET}"
    )

    if len(df) < 3:
        logging.info("[DETECT_SIGNAL] Not enough candles")
        return None, None

    last     = df.iloc[-1]
    prev     = df.iloc[-2]
    atr      = last.atr
    atr_prev = prev.atr

    logging.info(
        f"[MARKET] close={last.close:.2f} open={last.open:.2f} "
        f"high={last.high:.2f} low={last.low:.2f} ATR={atr:.2f}"
    )

    # ===============================
    # GLOBAL NO-TRADE FILTERS
    # ===============================
    if abs(last.close - last.open) < 0.2 * atr:
        logging.info("[FILTER] Weak candle body — skipping")
        return None, None

    if atr_prev and atr < 0.8 * atr_prev:
        logging.info(
            f"[FILTER] ATR compression detected atr={atr:.2f} prev={atr_prev:.2f}"
        )
        return None, None

    # ===============================
    # LEVELS
    # ===============================
    tc    = pivots.get("TC")
    bc    = pivots.get("BC")
    pivot = pivots.get("P")
    r3    = pivots.get("R3")
    s3    = pivots.get("S3")
    r4    = pivots.get("R4")
    s4    = pivots.get("S4")

    logging.info(
        f"[LEVELS] TC={tc} BC={bc} P={pivot} "
        f"R3={r3} S3={s3} R4={r4} S4={s4}"
    )

    # ===============================
    # CPR BREAKOUT
    # ===============================
    if tc and last.close > tc + 0.1 * atr:
        logging.info("[CHECK] CPR TC breakout attempt")
        if candle_accepts_direction(last, "CALL", atr) and level_accepted(last, tc, "CALL"):
            logging.info(f"{GREEN}[SIGNAL] CALL | CPR_TC_BREAK{RESET}")
            return "CALL", "CPR_TC_BREAK"

    if bc and last.close < bc - 0.1 * atr:
        logging.info("[CHECK] CPR BC breakout attempt")
        if candle_accepts_direction(last, "PUT", atr) and level_accepted(last, bc, "PUT"):
            logging.info(f"{RED}[SIGNAL] PUT | CPR_BC_BREAK{RESET}")
            return "PUT", "CPR_BC_BREAK"

    # ===============================
    # CPR REJECTION
    # ===============================
    if tc and last.high > tc and last.close < tc:
        logging.info("[CHECK] CPR TC rejection attempt")
        if candle_accepts_direction(last, "PUT", atr):
            logging.info(f"{RED}[SIGNAL] PUT | CPR_TC_REJECT{RESET}")
            return "PUT", "CPR_TC_REJECT"

    if bc and last.low < bc and last.close > bc:
        logging.info("[CHECK] CPR BC rejection attempt")
        if candle_accepts_direction(last, "CALL", atr):
            logging.info(f"{GREEN}[SIGNAL] CALL | CPR_BC_REJECT{RESET}")
            return "CALL", "CPR_BC_REJECT"

    # ===============================
    # CAMARILLA S3 / R3 REJECTION
    # ===============================
    if s3 and last.low <= s3:
        rejection = last.close - last.low
        logging.info(
            f"[CHECK] S3 rejection wick={rejection:.2f}"
        )
        if rejection > 0.35 * atr and candle_accepts_direction(last, "CALL", atr):
            logging.info(f"{GREEN}[SIGNAL] CALL | S3_REJECTION{RESET}")
            return "CALL", "S3_REJECTION"

    if r3 and last.high >= r3:
        rejection = last.high - last.close
        logging.info(
            f"[CHECK] R3 rejection wick={rejection:.2f}"
        )
        if rejection > 0.35 * atr and candle_accepts_direction(last, "PUT", atr):
            logging.info(f"{RED}[SIGNAL] PUT | R3_REJECTION{RESET}")
            return "PUT", "R3_REJECTION"

    # ===============================
    # CAMARILLA R4 / S4 TREND BREAK
    # ===============================
    if r4 and last.close > r4 + 0.15 * atr:
        logging.info("[CHECK] R4 trend breakout attempt")
        if candle_accepts_direction(last, "CALL", atr):
            logging.info(f"{GREEN}[SIGNAL] CALL | R4_TREND_BREAK{RESET}")
            return "CALL", "R4_TREND_BREAK"

    if s4 and last.close < s4 - 0.15 * atr:
        logging.info("[CHECK] S4 trend breakout attempt")
        if candle_accepts_direction(last, "PUT", atr):
            logging.info(f"{RED}[SIGNAL] PUT | S4_TREND_BREAK{RESET}")
            return "PUT", "S4_TREND_BREAK"

    logging.info("[DETECT_SIGNAL] No valid setup found")
    return None, None

