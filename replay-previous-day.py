import pandas as pd
import sqlite3
import logging

from indicators import momentum_ok, resolve_atr, supertrend, calculate_adx, adx_bias, calculate_cci, williams_r
from signals import to_scalar
from previous_day_pivot_testing import compute_levels

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(levelname)s - %(message)s")

# --- Connect to DBs ---
conn_thu = sqlite3.connect(r"C:\\SQLite\\ticks\\ticks_2026-02-04.db")
conn_fri = sqlite3.connect(r"C:\\SQLite\\ticks\\ticks_2026-02-05.db")

# --- Query 15m candles for daily OHLC ---
query_15m = """
SELECT trade_date, ist_slot, open, high, low, close
FROM candles_15m_ist
WHERE symbol = 'NSE:NIFTY50-INDEX'
"""
df_thu_15m = pd.read_sql(query_15m, conn_thu)
df_fri_15m = pd.read_sql(query_15m, conn_fri)
df_15m = pd.concat([df_thu_15m, df_fri_15m], ignore_index=True)

df_15m["datetime"] = pd.to_datetime(df_15m["trade_date"] + " " + df_15m["ist_slot"])
df_15m.set_index("datetime", inplace=True)

# --- Build daily OHLC ---
df_daily = df_15m.resample("1D").agg({
    "open": "first",
    "high": "max",
    "low": "min",
    "close": "last"
}).dropna()

# --- Get Thursday’s OHLC for Friday replay ---
prev_day = df_daily.iloc[-2]
prev_high, prev_low, prev_close = prev_day.high, prev_day.low, prev_day.close

# --- Compute levels ---
traditional_levels, cpr_levels, camarilla_levels = compute_levels(prev_high, prev_low, prev_close)
logging.info(f"[LEVELS] Traditional={traditional_levels} CPR={cpr_levels} Camarilla={camarilla_levels}")

# --- Query 3m candles (Friday only) ---
query_3m = """
SELECT trade_date, ist_slot, open, high, low, close
FROM candles_3m_ist
WHERE symbol = 'NSE:NIFTY50-INDEX'
  AND trade_date = '2026-02-05'
"""
df_3m = pd.read_sql(query_3m, conn_fri)
df_3m["datetime"] = pd.to_datetime(df_3m["trade_date"] + " " + df_3m["ist_slot"])
df_3m.set_index("datetime", inplace=True)

# --- Trade state tracking ---
current_position = None
entry_candle = None
peak_momentum = None
peak_candle = None
prev_gap = None

# --- Replay loop ---
for i in range(len(df_3m)):
    df_slice = df_3m.iloc[: i + 1]
    last_close = df_slice.iloc[-1].close

    # ATR
    atr_val, _ = resolve_atr(df_slice, None)
    atr_val = to_scalar(atr_val)
    if atr_val is None:
        continue

    # Pivot side
    if last_close > traditional_levels["pivot"]:
        side = "CALL"
    elif last_close < traditional_levels["pivot"]:
        side = "PUT"
    else:
        continue

    # Momentum
    ok, momentum = momentum_ok(df_slice, side)
    if not ok:
        continue

    # EMA9 vs EMA13
    ema9 = df_slice["close"].ewm(span=9, adjust=False).mean().iloc[-1]
    ema13 = df_slice["close"].ewm(span=13, adjust=False).mean().iloc[-1]
    ema_gap = abs(ema9 - ema13)

    # --- Standard EMA conditions ---
    ema_ok = False
    if side == "CALL" and (ema9 > ema13 and last_close > ema9):
        ema_ok = True
    if side == "PUT" and (ema9 < ema13 and last_close < ema9):
        ema_ok = True

    # Supertrend slope
    st_bias, st_slope = supertrend(df_slice)

    # ADX
    adx_val = calculate_adx(df_slice)
    adx_val = to_scalar(adx_val)
    adx_dir = adx_bias(df_slice)
    if adx_val is None:
        adx_val = 0
        adx_dir = "NEUTRAL"

    # --- Momentum override path (both sides) ---
    momentum_override = False
    if side == "CALL" and momentum > 12:   # lowered threshold for bullish bursts
        momentum_override = True
    if side == "PUT" and momentum < -15:  # bearish bursts
        momentum_override = True

    # --- Entry conditions ---
    if current_position is None:
        if ema_ok and ((side == "CALL" and st_slope in ["UP", "NEUTRAL"]) or (side == "PUT" and st_slope == "DOWN")) \
           and adx_val >= 20 \
           and ((side == "CALL" and adx_dir in ["BULLISH", "NEUTRAL"]) or (side == "PUT" and adx_dir == "BEARISH")):
            # Strict entry path
            current_position = side
            logging.info(f"[ENTRY] Candle {i}: side={side} momentum={momentum:.2f} "
                         f"Close={last_close:.2f} EMA9={ema9:.2f} EMA13={ema13:.2f} Gap={ema_gap:.2f} "
                         f"SupertrendSlope={st_slope} ADX={adx_val:.2f}/{adx_dir} ATR={atr_val:.2f}")
        elif momentum_override:
            # Override entry path for strong bursts
            current_position = side
            logging.info(f"[OVERRIDE ENTRY] Candle {i}: side={side} momentum={momentum:.2f} "
                         f"Close={last_close:.2f} EMA9={ema9:.2f} EMA13={ema13:.2f} Gap={ema_gap:.2f} "
                         f"SupertrendSlope={st_slope} ADX={adx_val:.2f}/{adx_dir} ATR={atr_val:.2f}")
        else:
            continue

        entry_candle = i
        peak_momentum = abs(momentum)
        peak_candle = i
        prev_gap = ema_gap
        continue

    # --- Hold while EMA gap is widening ---
    if current_position == side and ema_gap > prev_gap:
        prev_gap = ema_gap
        if abs(momentum) > peak_momentum:
            peak_momentum = abs(momentum)
            peak_candle = i
        continue

    # --- Exit when EMA gap stops widening and momentum drops ---
    if current_position == side and ema_gap <= prev_gap and abs(momentum) < peak_momentum * 0.6:
        logging.info(f"[EXIT] Candle {i}: side={side} exit on EMA plateau + momentum drop "
                     f"Peak={peak_momentum:.2f} at Candle {peak_candle}, Current={momentum:.2f}, Gap={ema_gap:.2f}")
        current_position = None
        peak_momentum = None
        peak_candle = None
        prev_gap = None
        continue

    # --- Slow exit path (futures/hedged trades) ---
    cci_series = calculate_cci(df_slice)
    cci_val = cci_series.iloc[-1] if not cci_series.empty else None
    wr_val = williams_r(df_slice)

    if current_position == side:
        cci_str = f"{cci_val:.2f}" if cci_val is not None else "NA"
        wr_str = f"{wr_val:.2f}" if wr_val is not None else "NA"

        if side == "CALL" and ((cci_val is not None and cci_val > 100) or (wr_val is not None and wr_val < -80)):
            logging.info(f"[EXIT] Candle {i}: side={side} exit triggered CCI={cci_str} W%R={wr_str}")
            current_position = None
            continue
        if side == "PUT" and ((cci_val is not None and cci_val < -100) or (wr_val is not None and wr_val > -20)):
            logging.info(f"[EXIT] Candle {i}: side={side} exit triggered CCI={cci_str} W%R={wr_str}")
            current_position = None
            continue