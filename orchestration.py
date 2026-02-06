# ===== orchestration.py =====

import logging
import pandas as pd
from candle_builder import build_3min_candle, build_15m_candles
from tickdb import tick_db
from indicators import (
    calculate_cpr,
    calculate_traditional_pivots,
    calculate_camarilla_pivots,
    resolve_atr,
    daily_atr,
    calculate_ema, 
    calculate_atr, 
    calculate_adx, 
    calculate_cci, 
    supertrend
)

from signals import detect_signal, evaluate_candle
from signals import bias_from_indicators 
from config import symbols
from setup import fyers_async

# ANSI COLORS
RESET   = "\033[0m"
GREEN   = "\033[92m"
YELLOW  = "\033[93m"
RED     = "\033[91m"
MAGENTA = "\033[95m"
GRAY    = "\033[90m"
CYAN    = "\033[96m"


def build_indicator_dataframe(symbol, interval="3m", df_15m=None):
    # --- Fetch candles from DB ---
    df = tick_db.fetch_candles(resolution=interval, symbol=symbol)
    if df is None or df.empty:
        logging.warning(f"[INDICATORS] No {interval} candles for {symbol}")
        return pd.DataFrame()

    # --- Append latest in-progress candle from ticks ---
    latest_tick = tick_db.get_latest_tick(symbol)
    if latest_tick is not None:
        in_progress = {
            "open": latest_tick["last_price"],
            "high": latest_tick["last_price"],
            "low": latest_tick["last_price"],
            "close": latest_tick["last_price"],
            "volume": latest_tick.get("volume", 0),
            "trade_date": latest_tick["trade_date"],
            "ist_slot": pd.to_datetime(latest_tick["timestamp"])
                          .tz_localize("UTC")
                          .tz_convert("Asia/Kolkata")
                          .strftime("%H:%M:%S"),
            "symbol": symbol,
            "in_progress": True,
        }
        df = pd.concat([df, pd.DataFrame([in_progress])], ignore_index=True)

    # --- Indicators ---
    df["ema20"] = calculate_ema(df, column="close", period=20)
    df["ema50"] = calculate_ema(df, column="close", period=50)
    df["adx14"] = calculate_adx(df)
    df["cci20"] = calculate_cci(df)

    # --- ATR resolution for Supertrend ---
    daily_val = daily_atr(df_15m) if df_15m is not None and not df_15m.empty else None
    atr, atr_source = resolve_atr(df, daily_val)

    # --- Supertrend bias/slope assignment (last row only) ---
    bias, slope = supertrend(df, atr_val=atr)
    df.loc[df.index[-1], "supertrend_bias"] = bias
    df.loc[df.index[-1], "supertrend_slope"] = slope

    # --- Enrich with bias/signal ---
    df["signal"], df["confidence"] = zip(*df.apply(
        lambda row: bias_from_indicators(row, df_15m), axis=1
    ))

    # --- Debug log ---
    last_row = df.iloc[-1]
    progress_tag = "LIVE" if last_row.get("in_progress", False) else "FINAL"
    logging.info(
        f"{CYAN}[INDICATOR DF] {symbol} {interval} ({progress_tag}) "
        f"ema20={last_row['ema20']:.2f} ema50={last_row['ema50']:.2f} "
        f"adx14={last_row['adx14']:.2f} cci20={last_row['cci20']:.2f} "
        f"supertrend_bias={last_row['supertrend_bias']} "
        f"slope={last_row['supertrend_slope']} "
        f"signal={last_row['signal']} confidence={last_row['confidence']}{RESET}"
    )

    return df


def build_multi_symbol_indicators(symbols=None, interval="3m"):
    if symbols is None:
        symbols = symbols

    result = {}
    for sym in symbols:
        df_15m = build_indicator_dataframe(sym, interval="15m")
        df = build_indicator_dataframe(sym, interval=interval, df_15m=df_15m)
        result[sym] = df
        logging.info(f"[INDICATORS] Built {interval} DataFrame for {sym} with {len(df)} rows")

    return result

# def update_candles_and_signals(symbol, hist_yesterday_15m=None, spot_price=None):
#     try:
#         # --- Fetch latest ticks from DB ---
#         df_ticks = tick_db.fetch_ticks(symbol)
#         if df_ticks.empty:
#             logging.warning(f"[UPDATE] No ticks found for {symbol}")
#             return None, None

#         # --- Build enriched 15m candles ---
#         df_15m = build_indicator_dataframe(symbol, interval="15m")
#         if df_15m.empty:
#             logging.warning(f"[UPDATE] No 15m candles built for {symbol}")
#             return None, None

#         # --- Merge yesterday’s historical 15m candles if provided ---
#         if hist_yesterday_15m is not None and not hist_yesterday_15m.empty:
#             df_15m = pd.concat([hist_yesterday_15m, df_15m]).drop_duplicates(
#                 subset=["trade_date", "ist_slot", "symbol"], keep="last"
#             )
#             logging.info(f"{CYAN}[BOOTSTRAP] Seeded bias with {len(hist_yesterday_15m)} candles from yesterday{RESET}")

#         # --- Build enriched 3m candles (with reference to 15m DF) ---
#         df_3m = build_indicator_dataframe(symbol, interval="3m", df_15m=df_15m)
#         if df_3m.empty:
#             logging.warning(f"[UPDATE] No 3m candles built for {symbol}")
#             return None, None

#         # --- Resolve spot price ---
#         if spot_price is None:
#             try:
#                 quote = fyers_async.quotes({"symbols": symbol})
#                 spot_price = quote["d"][0]["v"].get("lp")
#                 logging.info(f"[SPOT] {symbol} resolved via quotes API: {spot_price}")
#             except Exception as e:
#                 logging.warning(f"[SPOT FALLBACK] {symbol} quotes API failed: {e}")
#                 latest_tick = tick_db.get_latest_tick(symbol)
#                 if latest_tick is not None:
#                     spot_price = latest_tick.get("ltp")
#                     logging.info(f"[SPOT] {symbol} fallback to tick LTP: {spot_price}")

#         if spot_price is None:
#             logging.warning(f"[UPDATE] No spot price available for {symbol}")
#             return None, df_3m

#         # --- ATR calculation ---
#         daily_val = daily_atr(df_15m)
#         atr_value, atr_source = resolve_atr(df_15m, daily_val)
#         atr_str = f"{atr_value:.2f}" if atr_value is not None else "NA"
#         logging.info(f"[ATR] {symbol} source={atr_source} value={atr_str}")

#         # --- Evaluate last 3m candle (diagnostic) ---
#         last_candle = df_3m.iloc[-1]
#         evaluate_candle(
#             ts=last_candle.name,
#             row=last_candle,
#             candles=df_3m,
#             resolution="3m",
#             spot=spot_price,
#             side="CALL",
#             candles_15m=df_15m
#         )

#         # --- Compute levels from last 15m candle ---
#         last_candle_15m = df_15m.iloc[-1]
#         cpr_levels = calculate_cpr(last_candle_15m.high, last_candle_15m.low, last_candle_15m.close)
#         traditional_levels = calculate_traditional_pivots(last_candle_15m.high, last_candle_15m.low, last_candle_15m.close)
#         camarilla_levels = calculate_camarilla_pivots(last_candle_15m.high, last_candle_15m.low, last_candle_15m.close)

#         # --- Detect signal ---
#         signal = detect_signal(
#             cpr_levels,
#             traditional_levels,
#             camarilla_levels,
#             df_3m,
#             df_15m,
#             spot_price=spot_price,
#             daily_atr=daily_val
#         )
#         if signal:
#             side, reason = signal
#             logging.info(f"{GREEN}[SIGNAL FIRED] {symbol} side={side} reason={reason}{RESET}")
#             return signal, df_3m
#         else:
#             logging.debug(f"[SIGNAL CHECK] No signal for {symbol}")
#             return None, df_3m

#     except Exception as e:
#         logging.error(f"[UPDATE ERROR] {symbol}: {e}")
#         return None, None

def update_candles_and_signals(symbol, hist_yesterday_15m=None, spot_price=None):
    try:
        # --- Fetch latest ticks from DB ---
        df_ticks = tick_db.fetch_ticks(symbol)
        if df_ticks.empty:
            logging.warning(f"[UPDATE] No ticks found for {symbol}")
            return None, None

        # --- Build enriched 15m candles ---
        df_15m = build_indicator_dataframe(symbol, interval="15m")
        if df_15m.empty:
            logging.warning(f"[UPDATE] No 15m candles built for {symbol}")
            return None, None

        # --- Merge yesterday’s historical 15m candles if provided ---
        if hist_yesterday_15m is not None and not hist_yesterday_15m.empty:
            df_15m = pd.concat([hist_yesterday_15m, df_15m]).drop_duplicates(
                subset=["trade_date", "ist_slot", "symbol"], keep="last"
            )
            logging.info(f"{CYAN}[BOOTSTRAP] Seeded bias with {len(hist_yesterday_15m)} candles from yesterday{RESET}")

        # --- Build enriched 3m candles (with reference to 15m DF) ---
        df_3m = build_indicator_dataframe(symbol, interval="3m", df_15m=df_15m)
        if df_3m.empty:
            logging.warning(f"[UPDATE] No 3m candles built for {symbol}")
            return None, None

        # --- Resolve spot price ---
        if spot_price is None:
            try:
                quote = fyers_async.quotes({"symbols": symbol})
                spot_price = quote["d"][0]["v"].get("lp")
                logging.info(f"[SPOT] {symbol} resolved via quotes API: {spot_price}")
            except Exception as e:
                logging.warning(f"[SPOT FALLBACK] {symbol} quotes API failed: {e}")
                latest_tick = tick_db.get_latest_tick(symbol)
                if latest_tick is not None:
                    spot_price = latest_tick.get("ltp")
                    logging.info(f"[SPOT] {symbol} fallback to tick LTP: {spot_price}")

        if spot_price is None:
            logging.warning(f"[UPDATE] No spot price available for {symbol}")
            return None, df_3m

        # --- ATR calculation ---
        daily_val = daily_atr(df_15m)
        atr_value, atr_source = resolve_atr(df_15m, daily_val)
        atr_str = f"{atr_value:.2f}" if atr_value is not None else "NA"
        logging.info(f"[ATR] {symbol} source={atr_source} value={atr_str}")

        # --- Evaluate last 3m candle (diagnostic) ---
        last_candle = df_3m.iloc[-1]
        evaluate_candle(
            ts=last_candle.name,
            row=last_candle,
            candles=df_3m,
            resolution="3m",
            spot=spot_price,
            side="CALL",
            candles_15m=df_15m
        )

        # --- Compute levels from last 15m candle ---
        last_candle_15m = df_15m.iloc[-1]
        cpr_levels = calculate_cpr(last_candle_15m.high, last_candle_15m.low, last_candle_15m.close)
        traditional_levels = calculate_traditional_pivots(last_candle_15m.high, last_candle_15m.low, last_candle_15m.close)
        camarilla_levels = calculate_camarilla_pivots(last_candle_15m.high, last_candle_15m.low, last_candle_15m.close)

        # --- Detect signal ---
        signal = detect_signal(
            cpr_levels,
            traditional_levels,
            camarilla_levels,
            df_3m,
            df_15m,
            spot_price=spot_price,
            daily_atr=daily_val
        )

        if signal:
            if isinstance(signal, (list, tuple)):
                if len(signal) == 4:
                    side, reason, targets, confidence = signal
                    logging.info(
                        f"{GREEN}[SIGNAL FIRED] {symbol} side={side} reason={reason} "
                        f"SL={targets['SL']:.2f} PT={targets['PT']:.2f} TG={targets['TG']:.2f} "
                        f"Confidence={confidence}{RESET}"
                    )
                elif len(signal) == 2:
                    side, reason = signal
                    logging.info(f"{GREEN}[SIGNAL FIRED] {symbol} side={side} reason={reason}{RESET}")
                else:
                    logging.error(f"[SIGNAL ERROR] Unexpected signal format: {signal}")
            else:
                logging.error(f"[SIGNAL ERROR] Signal not tuple/list: {signal}")

            return signal, df_3m
        else:
            logging.debug(f"[SIGNAL CHECK] No signal for {symbol}")
            return None, df_3m

    except Exception as e:
        logging.error(f"[UPDATE ERROR] {symbol}: {e}")
        return None, None