import pandas as pd
import logging
import pendulum as dt
from config import time_zone, profit_loss_point

# globals (must exist once in your script)
ticks_buffer = []
candles_3m = pd.DataFrame(columns=["open","high","low","close","time"])
current_3m_start = None

ATR_STOP_MULT  = 1.0
ATR_TGT_MULT   = 2.0
TRAIL_TRIGGER  = 1.0  # start trailing after 1×ATR profit
TRAIL_STEP     = 0.5  # trail by 0.5×ATR

def calculate_cpr(high, low, close):
    pivot = (high + low + close) / 3
    bc = (high + low) / 2
    tc = (pivot - bc) + pivot
    return {"Pivot": round(pivot, 2), "BC": round(bc, 2), "TC": round(tc, 2)}

def calculate_camarilla_pivots(high, low, close):
    range_val = high - low
    pivots = {
        "R3": close + (range_val * 1.1 / 4),
        "R4": close + (range_val * 1.1 / 2),
        "S3": close - (range_val * 1.1 / 4),
        "S4": close - (range_val * 1.1 / 2),
    }
    return {k: round(v, 2) for k, v in pivots.items()}

def calculate_traditional_pivots(high, low, close):
    pivot = (high + low + close) / 3
    r1 = (2 * pivot) - low
    s1 = (2 * pivot) - high
    r2 = pivot + (high - low)
    s2 = pivot - (high - low)
    return {"Pivot": round(pivot, 2),"R1": round(r1, 2),"S1": round(s1, 2),"R2": round(r2, 2),"S2": round(s2, 2)}

def build_3min_candle(price):
    global ticks_buffer, candles_3m, current_3m_start

    if price is None or pd.isna(price):
        return

    ct = dt.now(time_zone)

    # --- 1️⃣ Initialize first candle aligned to 3-minute boundary ---
    if current_3m_start is None:
        minute_bucket = (ct.minute // 3) * 3
        current_3m_start = ct.replace(
            minute=minute_bucket,
            second=0,
            microsecond=0
        )
        ticks_buffer.clear()
        return

    # --- 2️⃣ Accumulate ticks ---
    ticks_buffer.append(float(price))

    # --- 3️⃣ Close candle ONLY after full 3 minutes elapsed ---
    if ct >= current_3m_start + dt.duration(minutes=3):

        if len(ticks_buffer) > 0:
            candle = {
                "open": ticks_buffer[0],
                "high": max(ticks_buffer),
                "low":  min(ticks_buffer),
                "close": ticks_buffer[-1],
                "time": current_3m_start
            }

            candles_3m.loc[len(candles_3m)] = candle

            logging.info(
                f"[3M CANDLE CLOSED] {current_3m_start.strftime('%H:%M:%S')} | "
                f"O={candle['open']} H={candle['high']} "
                f"L={candle['low']} C={candle['close']} |"
                f"Spot={spot_price}"
            )

        # --- 4️⃣ Advance to next 3-minute window ---
        current_3m_start += dt.duration(minutes=3)

        # --- 5️⃣ Reset buffer ---
        ticks_buffer.clear()

import pandas as pd
import logging
from data_feed import spot_price

def calculate_atr(df_, period=14):
    if len(df_) < period + 1:
        return None

    hl = df_["high"] - df_["low"]
    hc = (df_["high"] - df_["close"].shift()).abs()
    lc = (df_["low"] - df_["close"].shift()).abs()

    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    return float(tr.rolling(period).mean().iloc[-1])

def momentum_ok(candles, side):
    last = candles.iloc[-1]
    prev = candles.iloc[-2]

    momentum = last.close - prev.close

    if side == "CALL":
        ok = momentum > 0
    else:
        ok = momentum < 0

    return ok, momentum

def resolve_atr(candles_3m, daily_atr):
    """
    Priority:
    1. Live 3m ATR (after enough candles)
    2. Daily ATR
    3. Bootstrap range (temporary)
    """
    atr_3m = calculate_atr(candles_3m)

    if atr_3m is not None:
        return atr_3m, "ATR_3M"

    if daily_atr is not None:
        return daily_atr, "ATR_DAILY"

    # Emergency bootstrap (first few minutes only)
    if len(candles_3m) >= 2:
        atr_boot = candles_3m["high"].max() - candles_3m["low"].min()
        logging.warning(f"[BOOTSTRAP ATR] using range={atr_boot:.2f}")
        return atr_boot, "ATR_BOOTSTRAP"

    return None, None

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

def build_dynamic_levels(entry_price, side, atr_value):
    """
    Builds SL / TG / trailing levels for OPTION BUY trades (CALL & PUT).
    Returns: stop_loss, target, trail_start_pnl, trail_step_points
    """
    if atr_value is None or atr_value <= 0:
        sl = entry_price - profit_loss_point
        tg = entry_price + profit_loss_point
        trail_start = profit_loss_point
        trail_step  = profit_loss_point / 2
        return round(sl, 2), round(tg, 2), trail_start, trail_step

    stop_dist   = ATR_STOP_MULT * atr_value
    target_dist = ATR_TGT_MULT  * atr_value
    trail_start = TRAIL_TRIGGER * atr_value
    trail_step  = TRAIL_STEP    * atr_value

    sl = entry_price - stop_dist
    tg = entry_price + target_dist

    if sl >= entry_price or tg <= entry_price:
        logging.error(f"[SL/TG ERROR] side={side} entry={entry_price} SL={sl} TG={tg}")
        return None

    return round(sl, 2), round(tg, 2), trail_start, trail_step

def update_trailing_stop(side, current_price, entry_price, current_stop, trail_start_pnl, trail_step_points):
    """
    Returns updated stop price
    """
    if side == "CALL":
        pnl = current_price - entry_price
        if pnl >= trail_start_pnl:
            candidate = current_price - trail_step_points
            return max(current_stop, candidate)
        return current_stop
    else:
        pnl = entry_price - current_price
        if pnl >= trail_start_pnl:
            candidate = current_price + trail_step_points
            return min(current_stop, candidate)
        return current_stop

