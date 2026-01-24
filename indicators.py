# ===== indicators.py =====
import logging
import pandas as pd
import numpy as np
import pendulum as dt
import datetime

from config import time_zone, profit_loss_point
from setup import spot_price, hist_data, fyers

# globals (must exist once in your script)
ticks_buffer = []
candles_3m = pd.DataFrame(columns=["open","high","low","close","time"])
current_3m_start = None

# ===========================================================
# ANSI COLORS for order logs
RESET   = "\033[0m"
GREEN   = "\033[92m"
YELLOW  = "\033[93m"
RED     = "\033[91m"
MAGENTA = "\033[95m"
GRAY    = "\033[90m"

#===========================================================


def calculate_cpr(high, low, close):
    pivot = (high + low + close) / 3
    bc = (high + low) / 2
    tc = (pivot - bc) + pivot
    return {
        "pivot": round(pivot, 2),
        "bc": round(bc, 2),
        "tc": round(tc, 2)
    }

def calculate_traditional_pivots(high, low, close):
    pivot = (high + low + close) / 3
    r1 = (2 * pivot) - low
    s1 = (2 * pivot) - high
    r2 = pivot + (high - low)
    s2 = pivot - (high - low)
    return {
        "pivot": round(pivot, 2),
        "r1": round(r1, 2),
        "s1": round(s1, 2),
        "r2": round(r2, 2),
        "s2": round(s2, 2)
    }

def calculate_camarilla_pivots(high, low, close):
    range_val = high - low
    r3 = close + (range_val * 1.1 / 4)
    r4 = close + (range_val * 1.1 / 2)
    s3 = close - (range_val * 1.1 / 4)
    s4 = close - (range_val * 1.1 / 2)
    return {
        "r3": round(r3, 2),
        "r4": round(r4, 2),
        "s3": round(s3, 2),
        "s4": round(s4, 2)
    }

# ===== ATR =====
def calculate_atr(df_, period=14):
    if len(df_) < period + 1:
        return None

    hl = df_["high"] - df_["low"]
    hc = (df_["high"] - df_["close"].shift()).abs()
    lc = (df_["low"] - df_["close"].shift()).abs()

    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    return float(tr.rolling(period).mean().iloc[-1])

def resolve_atr(candles_3m_, daily_atr_):
    """
    Priority:
    1. Live 3m ATR (after enough candles)
    2. Daily ATR
    3. Bootstrap range (temporary)
    """
    atr_3m = calculate_atr(candles_3m_)

    if atr_3m is not None:
        return atr_3m, "ATR_3M"

    if daily_atr_ is not None:
        return daily_atr_, "ATR_DAILY"

    # Emergency bootstrap (first few minutes only)
    if len(candles_3m_) >= 2:
        atr_boot = candles_3m_["high"].max() - candles_3m_["low"].min()
        logging.warning(f"[BOOTSTRAP ATR] using range={atr_boot:.2f}")
        return atr_boot, "ATR_BOOTSTRAP"

    return None, None

# ===== Momentum =====
def momentum_ok(candles, side):
    last = candles.iloc[-1]
    prev = candles.iloc[-2]

    momentum = last.close - prev.close

    if side == "CALL":
        ok = momentum > 0
    else:
        ok = momentum < 0

    return ok, momentum

# ===== Candle builder =====
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
                f"{YELLOW}[3M CANDLE CLOSED] {current_3m_start.strftime('%H:%M:%S')} | "
                f"O={candle['open']} H={candle['high']} "
                f"L={candle['low']} C={candle['close']} |"
                f"Spot={spot_price}{RESET}"
            )

        # --- 4️⃣ Advance to next 3-minute window ---
        current_3m_start += dt.duration(minutes=3)

        # --- 5️⃣ Reset buffer ---
        ticks_buffer.clear()

# ===== Build levels once (optional print) + Daily ATR =====
prev_day = hist_data.iloc[-1]
prev_high, prev_low, prev_close = float(prev_day['high']), float(prev_day['low']), float(prev_day['close'])

cpr_levels_base = calculate_cpr(prev_high, prev_low, prev_close)
traditional_levels_base = calculate_traditional_pivots(prev_high, prev_low, prev_close)
camarilla_levels_base = calculate_camarilla_pivots(prev_high, prev_low, prev_close)

print(
    f"CPR: Pivot={cpr_levels_base['pivot']}, TC={cpr_levels_base['tc']}, BC={cpr_levels_base['bc']}\n"
    f"Traditional: Pivot={traditional_levels_base['pivot']}, R1={traditional_levels_base['r1']}, S1={traditional_levels_base['s1']}, "
    f"R2={traditional_levels_base['r2']}, S2={traditional_levels_base['s2']}\n"
    f"Camarilla: R3={camarilla_levels_base['r3']}, R4={camarilla_levels_base['r4']}, S3={camarilla_levels_base['s3']}, S4={camarilla_levels_base['s4']}"
)

daily_atr = calculate_atr(hist_data, period=14)

logging.info(
    f"[INIT] Daily ATR loaded = {daily_atr:.2f}"
    if daily_atr is not None else
    "[INIT] Daily ATR unavailable"
)

# ===== Trend & Bias Filters =====

# Get Historical Previous day data

def get_intraday_data(symbol, resolution="3", target_date=None):
    """
    Fetch previous intraday historical data from Fyers for a given date.
    Returns DataFrame with [timestamp, open, high, low, close, volume].
    """
    if target_date is None:
        target_date = datetime.date.today() - datetime.timedelta(days=1)

    start = datetime.datetime.combine(target_date, datetime.time(9,15))
    end   = datetime.datetime.combine(target_date, datetime.time(15,30))

    data = {
        "symbol": symbol,
        "resolution": resolution,
        "date_format": "0",
        "range_from": int(start.timestamp()),
        "range_to": int(end.timestamp()),
        "cont_flag": "0"
    }
    response = fyers.history(data=data)
    candles = response["candles"]
    df = pd.DataFrame(candles, columns=["timestamp","open","high","low","close","volume"])
    return df

def build_15m_candles(df_intraday, target_date=None):
    """
    Resample previous intraday data into 15m OHLCV candles for a given date.
    """
    df = df_intraday.copy()
    df["datetime"] = pd.to_datetime(df["timestamp"], unit="s")
    df = df.set_index("datetime")

    if target_date is not None:
        df = df[df.index.date == target_date]

    df_15m = df.resample("15min").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum"
    }).dropna()

    return df_15m

def get_today_15m_candles(candles_3m):
    """
    Resample live 3m candles into 15m candles.
    """
    if not isinstance(candles_3m.index, pd.DatetimeIndex):
        candles_3m = candles_3m.copy()
        candles_3m["datetime"] = pd.to_datetime(candles_3m["time"])
        candles_3m = candles_3m.set_index("datetime")

    df_15m_today = candles_3m.resample("15min").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last"
    }).dropna()

    return df_15m_today


# ===== EMA =====
def calculate_ema(series, period=20):
    """Calculate Exponential Moving Average (EMA)."""
    return series.ewm(span=period, adjust=False).mean()

# ============ CCI ================================
def calculate_cci(df, period=20):
    """Calculate Commodity Channel Index (CCI)."""
    tp = (df["high"] + df["low"] + df["close"]) / 3
    ma = tp.rolling(period).mean()
    # Mean Absolute Deviation (MAD)
    md = tp.rolling(period).apply(lambda x: np.mean(np.abs(x - x.mean())), raw=True)
    cci = (tp - ma) / (0.015 * md)
    return cci

# ===== Supertrend =====
def calculate_supertrend(df, period=8, multiplier=3):
    hl2 = (df['high'] + df['low']) / 2
    atr = calculate_atr(df, period)

    upperband = hl2 + (multiplier * atr)
    lowerband = hl2 - (multiplier * atr)

    supertrend = pd.Series(index=df.index, dtype="object")
    trend = None

    for i in range(period, len(df)):
        if df['close'].iloc[i] > upperband.iloc[i-1]:
            trend = "BULLISH"
        elif df['close'].iloc[i] < lowerband.iloc[i-1]:
            trend = "BEARISH"
        supertrend.iloc[i] = trend

    return supertrend

# ===== ADX =====
def calculate_adx(df, period=14):
    df = df.copy()
    df['up_move'] = df['high'] - df['high'].shift()
    df['down_move'] = df['low'].shift() - df['low']

    df['plus_dm'] = np.where((df['up_move'] > df['down_move']) & (df['up_move'] > 0), df['up_move'], 0.0)
    df['minus_dm'] = np.where((df['down_move'] > df['up_move']) & (df['down_move'] > 0), df['down_move'], 0.0)

    tr = calculate_atr(df, period)
    plus_di = 100 * (df['plus_dm'].rolling(period).sum() / tr)
    minus_di = 100 * (df['minus_dm'].rolling(period).sum() / tr)

    dx = (np.abs(plus_di - minus_di) / (plus_di + minus_di)) * 100
    adx = dx.rolling(period).mean()
    return adx

def check_bias(hist_data_15m, daily_atr=None, atr_threshold=15, adx_threshold=20, min_candles=20):
    """
    Determine bias using Supertrend, EMA, CCI, ADX, and ATR (via resolve_atr) on 15m candles.
    Returns: "BULLISH", "BEARISH", or "NEUTRAL"
    """

    # --- Safety: need enough candles ---
    if len(hist_data_15m) < min_candles:
        logging.info(f"[BIAS CHECK] insufficient 15m candles (<{min_candles}), defaulting to NEUTRAL")
        return "NEUTRAL"

    # --- ATR resolution ---
    atr, atr_source = resolve_atr(hist_data_15m, daily_atr)
    if atr is None:
        logging.info("[BIAS CHECK] ATR unavailable, defaulting to NEUTRAL")
        return "NEUTRAL"

    # --- ADX ---
    adx = calculate_adx(hist_data_15m, period=14).iloc[-1]

    # --- Sideways filter ---
    if atr < atr_threshold or adx < adx_threshold:
        logging.info(f"[SIDEWAYS DETECTED] ATR={atr:.2f} ({atr_source}) ADX={adx:.2f}, skipping signals")
        return "NEUTRAL"

    # --- Supertrend ---
    supertrend = calculate_supertrend(hist_data_15m, period=8, multiplier=3)
    st_bias = supertrend.iloc[-1]

    # --- EMA ---
    ema20 = calculate_ema(hist_data_15m["close"], period=20).iloc[-1]
    last_close = hist_data_15m["close"].iloc[-1]
    ema_bias = "BULLISH" if last_close > ema20 else "BEARISH"

    # --- CCI ---
    cci = calculate_cci(hist_data_15m, period=20).iloc[-1]
    cci_bias = "BULLISH" if cci > 50 else "BEARISH" if cci < -50 else "NEUTRAL"

    # --- Combine biases Voting system - at least two indicaors should be Bullish or Bearish---
    biases = [st_bias, ema_bias, cci_bias]
    bullish_votes = biases.count("BULLISH")
    bearish_votes = biases.count("BEARISH")

    if bullish_votes >= 2:
        final_bias = "BULLISH"
    elif bearish_votes >= 2:
        final_bias = "BEARISH"
    else:
        final_bias = "NEUTRAL"

    # --- Debug log ---
    logging.info(f"[BIAS DEBUG] ATR={atr:.2f} ({atr_source}) ADX={adx:.2f} "
                 f"Supertrend={st_bias} EMA={ema_bias} CCI={cci_bias} -> Bias={final_bias}")

    return final_bias

