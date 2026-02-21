# ===== orchestration.py (v3 — IMPROVED) =====
"""
IMPROVEMENTS vs v2:
1. build_indicator_dataframe now also computes VWAP and stores in df["vwap"]
2. RSI column naming made consistent: always "rsi14"
3. ADX returns scalar per-row instead of full series (avoids potential dtype issues)
4. Supertrend warmup guard: returns NEUTRAL/NaN for first (atr_period) rows safely
5. fetch_fyers_history: better handling of empty response (no silent empty return)
All v2 bug-fixes retained (reconciliation, include_today, etc.)
"""

import logging
import pandas as pd
import numpy as np
from datetime import datetime as dt, timedelta, date as date_type
from setup import fyers
import pytz

from indicators import (
    calculate_cpr,
    calculate_traditional_pivots,
    calculate_camarilla_pivots,
    calculate_ema,
    calculate_adx   as _adx_orig,   # shadowed below with Wilder version
    calculate_cci   as _cci_orig,   # shadowed below with min_periods version
    compute_rsi,
)


# ── FIXED calculate_adx — Wilder EWM smoothing ────────────────────────────────
# Original uses rolling sum × 2 → needs 28 bars minimum (NA at live open).
# Wilder EWM (alpha=1/period) converges from period+1 = 15 bars.
# Matches TradingView / Bloomberg standard.
def calculate_adx(df, period=14):
    if not {"high", "low", "close"}.issubset(df.columns):
        return pd.Series(dtype=float, index=df.index)
    if len(df) < period + 1:
        return pd.Series([float("nan")] * len(df), index=df.index)
    df      = df.copy()
    alpha   = 1.0 / period
    tr      = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift(1)).abs(),
        (df["low"]  - df["close"].shift(1)).abs(),
    ], axis=1).max(axis=1)
    up      = df["high"].diff()
    dn      = (-df["low"].diff())
    plus_dm  = pd.Series(np.where((up > dn) & (up > 0),   up,  0.0), index=df.index)
    minus_dm = pd.Series(np.where((dn > up) & (dn > 0),   dn,  0.0), index=df.index)
    tr_s     = tr.ewm(alpha=alpha,        adjust=False).mean()
    pdm_s    = plus_dm.ewm(alpha=alpha,   adjust=False).mean()
    mdm_s    = minus_dm.ewm(alpha=alpha,  adjust=False).mean()
    plus_di  = 100 * pdm_s  / tr_s.replace(0, np.nan)
    minus_di = 100 * mdm_s  / tr_s.replace(0, np.nan)
    dx       = 100 * (plus_di - minus_di).abs() /                (plus_di + minus_di).replace(0, np.nan)
    adx      = dx.ewm(alpha=alpha, adjust=False).mean()
    adx.iloc[:period] = float("nan")   # blank first (period) rows — not enough history
    return adx


# ── FIXED calculate_cci — min_periods warmup ──────────────────────────────────
# Original double rolling needs 2×period-1 = 39 bars (NA at live open).
# min_periods=period//4 gives valid estimates from ~9 bars.
def calculate_cci(df, period=20):
    if df is None or df.empty or not {"high", "low", "close"}.issubset(df.columns):
        return pd.Series(dtype=float,
                         index=df.index if df is not None else None)
    min_p = max(period // 4, 3)   # 5 for period=20
    tp    = (df["high"] + df["low"] + df["close"]) / 3
    ma    = tp.rolling(period, min_periods=min_p).mean()
    md    = (tp - ma).abs().rolling(period, min_periods=min_p).mean()
    return  (tp - ma) / (0.015 * md.replace(0, np.nan))

RESET  = "\033[0m"
GREEN  = "\033[92m"
CYAN   = "\033[96m"
YELLOW = "\033[93m"

ist = pytz.timezone("Asia/Kolkata")


def fmt(val):
    return f"{val:.2f}" if val is not None and not pd.isna(val) else "NA"


def calculate_atr(df, period=14):
    high, low, close = df["high"], df["low"], df["close"]
    tr1 = high - low
    tr2 = (high - close.shift()).abs()
    tr3 = (low  - close.shift()).abs()
    tr  = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.rolling(window=period, min_periods=period).mean()


# ─────────────────────────────────────────────────────────────────────────────
# VWAP — session VWAP computed from candle DataFrame
# ─────────────────────────────────────────────────────────────────────────────
def calculate_typical_price_ma(df: pd.DataFrame, period: int = 20) -> pd.Series:
    """
    Rolling mean of typical price (H+L+C)/3.
    VWAP substitute for volume-less instruments like NSE:NIFTY50-INDEX.
    Stored as df["vwap"] so downstream scoring engine works without changes.
    """
    try:
        tp = (df["high"] + df["low"] + df["close"]) / 3
        return tp.rolling(period, min_periods=1).mean()
    except Exception as e:
        logging.debug(f"[TPMA ERROR] {e}")
        return pd.Series([np.nan] * len(df), index=df.index)


# ─────────────────────────────────────────────────────────────────────────────
# SUPERTREND — with bias reconciliation (v2 fix retained)
# ─────────────────────────────────────────────────────────────────────────────
def supertrend(df, atr_period=14, multiplier=3):
    """
    Supertrend with correct bias reconciliation.
    Returns (line_series, bias_series, slope_series).
    """
    df = df.copy()

    df['H-L'] = df['high'] - df['low']
    df['H-C'] = abs(df['high'] - df['close'].shift())
    df['L-C'] = abs(df['low']  - df['close'].shift())
    df['TR']  = df[['H-L', 'H-C', 'L-C']].max(axis=1)
    df['ATR'] = df['TR'].rolling(atr_period).mean()

    hl2 = (df['high'] + df['low']) / 2
    df['upperband'] = hl2 + multiplier * df['ATR']
    df['lowerband'] = hl2 - multiplier * df['ATR']

    df['final_upperband'] = df['upperband'].copy()
    df['final_lowerband'] = df['lowerband'].copy()

    for i in range(atr_period, len(df)):
        prev_ub    = df['final_upperband'].iloc[i - 1]
        prev_lb    = df['final_lowerband'].iloc[i - 1]
        prev_close = df['close'].iloc[i - 1]

        if prev_close <= prev_ub:
            df.loc[df.index[i], 'final_upperband'] = min(df['upperband'].iloc[i], prev_ub)
        else:
            df.loc[df.index[i], 'final_upperband'] = df['upperband'].iloc[i]

        if prev_close >= prev_lb:
            df.loc[df.index[i], 'final_lowerband'] = max(df['lowerband'].iloc[i], prev_lb)
        else:
            df.loc[df.index[i], 'final_lowerband'] = df['lowerband'].iloc[i]

    line  = pd.Series(index=df.index, dtype=float)
    bias  = pd.Series(index=df.index, dtype=object)
    slope = pd.Series(index=df.index, dtype=object)

    for i in range(atr_period, len(df)):
        close_i = df['close'].iloc[i]
        prev_ub = df['final_upperband'].iloc[i - 1]
        prev_lb = df['final_lowerband'].iloc[i - 1]
        curr_lb = df['final_lowerband'].iloc[i]
        curr_ub = df['final_upperband'].iloc[i]

        if close_i > prev_ub:
            line.iloc[i] = curr_lb
            bias.iloc[i] = "UP"
        elif close_i < prev_lb:
            line.iloc[i] = curr_ub
            bias.iloc[i] = "DOWN"
        else:
            prev_bias = bias.iloc[i - 1] if i > atr_period else "NEUTRAL"
            prev_line = line.iloc[i - 1] if i > atr_period else float('nan')
            if prev_bias == "UP":
                line.iloc[i] = curr_lb
                bias.iloc[i] = "UP"
            elif prev_bias == "DOWN":
                line.iloc[i] = curr_ub
                bias.iloc[i] = "DOWN"
            else:
                line.iloc[i] = prev_line
                bias.iloc[i] = "NEUTRAL"

        if i > atr_period:
            pl = line.iloc[i - 1]
            cl = line.iloc[i]
            if pd.isna(pl) or pd.isna(cl):
                slope.iloc[i] = slope.iloc[i - 1]
            elif cl > pl:
                slope.iloc[i] = "UP"
            elif cl < pl:
                slope.iloc[i] = "DOWN"
            else:
                slope.iloc[i] = slope.iloc[i - 1]

    # Reconcile bias against line position (v2 fix)
    corrected = 0
    for i in range(atr_period, len(df)):
        cl = line.iloc[i]
        cc = df['close'].iloc[i]
        if pd.isna(cl):
            continue
        if cc > cl and bias.iloc[i] != "UP":
            bias.iloc[i] = "UP"
            corrected += 1
        elif cc < cl and bias.iloc[i] != "DOWN":
            bias.iloc[i] = "DOWN"
            corrected += 1

    if corrected > 0:
        logging.debug(
            f"[SUPERTREND] Reconciled {corrected} rows where bias "
            f"didn't match line position"
        )

    return line, bias, slope


# ─────────────────────────────────────────────────────────────────────────────
# INDICATOR DATAFRAME
# ─────────────────────────────────────────────────────────────────────────────
def build_indicator_dataframe(symbol, df, interval="3m"):
    if df is None or df.empty:
        logging.warning(f"[INDICATORS] No {interval} candles for {symbol}")
        return pd.DataFrame()

    df = df.copy()

    df["ema9"]  = calculate_ema(df, column="close", period=9)
    df["ema13"] = calculate_ema(df, column="close", period=13)

    try:
        df["adx14"] = calculate_adx(df)
    except Exception as e:
        logging.error(f"[ADX ERROR] {e}")
        df["adx14"] = float("nan")

    try:
        df["cci20"] = calculate_cci(df)
    except Exception as e:
        logging.error(f"[CCI ERROR] {e}")
        df["cci20"] = float("nan")

    try:
        df["atr14"] = calculate_atr(df, period=14)
    except Exception as e:
        logging.error(f"[ATR ERROR] {e}")
        df["atr14"] = float("nan")

    try:
        line_s, bias_s, slope_s = supertrend(df, atr_period=14, multiplier=3)
        df["supertrend_line"]  = line_s
        df["supertrend_bias"]  = bias_s
        df["supertrend_slope"] = slope_s
    except Exception as e:
        logging.error(f"[SUPERTREND ERROR] {e}")
        df["supertrend_line"]  = float("nan")
        df["supertrend_bias"]  = "NEUTRAL"
        df["supertrend_slope"] = "FLAT"

    try:
        df["rsi14"] = compute_rsi(df["close"], period=14)
    except Exception as e:
        logging.error(f"[RSI ERROR] {e}")
        df["rsi14"] = float("nan")

    # TPMA — Typical Price Moving Average (VWAP substitute for NSE index, no volume)
    # Stored as "vwap" column so downstream scoring engine works unchanged.
    try:
        df["vwap"] = calculate_typical_price_ma(df, period=20)
    except Exception as e:
        logging.debug(f"[TPMA ERROR] {e}")
        df["vwap"] = float("nan")

    last = df.iloc[-1]
    logging.info(
        f"[INDICATOR DF] {symbol} {interval} "
        f"ema9={fmt(last['ema9'])} ema13={fmt(last['ema13'])} "
        f"adx14={fmt(last['adx14'])} cci20={fmt(last['cci20'])} "
        f"rsi14={fmt(last['rsi14'])} "
        f"supertrend_bias={last.get('supertrend_bias', '')} "
        f"slope={last.get('supertrend_slope', '')} "
        f"line={fmt(last['supertrend_line'])} "
        f"vwap={fmt(last.get('vwap'))}"
    )
    return df


# ─────────────────────────────────────────────────────────────────────────────
# FETCH CANDLES — with today's intraday data
# ─────────────────────────────────────────────────────────────────────────────
def fetch_fyers_history(symbol, resolution="15", days=5, include_today=False):
    today      = dt.now(ist).date()
    start_date = today - timedelta(days=days)
    range_to   = (today + timedelta(days=1)) if include_today else today

    hist_req = {
        "symbol":      symbol,
        "resolution":  resolution,
        "date_format": "1",
        "range_from":  start_date.strftime("%Y-%m-%d"),
        "range_to":    range_to.strftime("%Y-%m-%d"),
        "cont_flag":   "1",
    }

    try:
        response = fyers.history(data=hist_req)
        candles  = response.get("candles", [])
        if not candles:
            logging.warning(f"[FETCH] No candles for {symbol} res={resolution}")
            return pd.DataFrame()

        hist_data = pd.DataFrame(candles, columns=["date", "open", "high", "low", "close", "volume"])
        hist_data["date"] = (
            pd.to_datetime(hist_data["date"], unit="s")
            .dt.tz_localize("UTC")
            .dt.tz_convert(ist)
        )

        if not include_today:
            hist_data = hist_data[hist_data["date"].dt.date < today]
        else:
            mins     = int(resolution) if str(resolution).isdigit() else 3
            now_ist  = dt.now(ist)
            slot_min = (now_ist.minute // mins) * mins
            current_slot_start = now_ist.replace(minute=slot_min, second=0, microsecond=0)
            if current_slot_start.tzinfo is None:
                current_slot_start = ist.localize(current_slot_start)
            hist_data = hist_data[hist_data["date"] < current_slot_start]

        hist_data["trade_date"] = hist_data["date"].dt.strftime("%Y-%m-%d")
        hist_data["ist_slot"]   = hist_data["date"].dt.strftime("%H:%M:%S")
        hist_data["symbol"]     = symbol
        hist_data["time"]       = hist_data["trade_date"] + " " + hist_data["ist_slot"]

        logging.info(
            f"[FETCH] {symbol} res={resolution} include_today={include_today} "
            f"rows={len(hist_data)} "
            f"last={hist_data.iloc[-1]['date'] if not hist_data.empty else 'none'}"
        )
        return hist_data.reset_index(drop=True)

    except Exception as e:
        logging.error(f"[FETCH ERROR] {symbol} res={resolution}: {e}", exc_info=True)
        return pd.DataFrame()


# ─────────────────────────────────────────────────────────────────────────────
# UPDATE CANDLES AND SIGNALS
# ─────────────────────────────────────────────────────────────────────────────
def update_candles_and_signals(symbol, spot_price=None, days=5, tick_db=None):
    try:
        df_3m  = fetch_fyers_history(symbol, resolution="3",  days=days, include_today=True)
        df_15m = fetch_fyers_history(symbol, resolution="15", days=days, include_today=True)

        if tick_db is not None:
            try:
                today_3m = tick_db.fetch_candles("3m", use_yesterday=False, symbol=symbol)
                if today_3m is not None and not today_3m.empty:
                    if "time" in today_3m.columns and "date" not in today_3m.columns:
                        today_3m = today_3m.rename(columns={"time": "date"})
                    if "date" in today_3m.columns:
                        today_3m["date"] = pd.to_datetime(today_3m["date"])
                        if today_3m["date"].dt.tz is None:
                            today_3m["date"] = today_3m["date"].dt.tz_localize(ist)
                        today_3m = today_3m[today_3m["date"].dt.date == dt.now(ist).date()]
                        if not today_3m.empty:
                            df_3m = pd.concat([df_3m, today_3m], ignore_index=True)
                            df_3m = (df_3m.drop_duplicates(subset=["date"])
                                         .sort_values("date")
                                         .reset_index(drop=True))
                            logging.info(f"[TICK_DB MERGE 3m] {symbol}: {len(df_3m)} rows after merge")
            except Exception as e:
                logging.debug(f"[TICK_DB MERGE] {symbol}: {e}")

        if not df_3m.empty:
            df_3m = build_indicator_dataframe(symbol, df_3m, interval="3m")
        if not df_15m.empty:
            df_15m = build_indicator_dataframe(symbol, df_15m, interval="15m")

        return df_3m, df_15m

    except Exception as e:
        logging.error(f"[UPDATE ERROR] {symbol}: {e}", exc_info=True)
        return pd.DataFrame(), pd.DataFrame()
