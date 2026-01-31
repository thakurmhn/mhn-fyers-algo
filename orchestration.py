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
    )
from signals import detect_signal, evaluate_candle


def update_candles_and_signals(symbol, hist_yesterday_15m=None, spot_price=None):
    try:
        # --- Fetch latest ticks from DB ---
        df_ticks = tick_db.fetch_ticks(symbol)
        if df_ticks.empty:
            logging.warning(f"[UPDATE] No ticks found for {symbol}")
            return None, None

        # --- Build 3m candles incrementally ---
        df_3m = build_3min_candle(df_ticks, tick_db, symbol)
        if df_3m.empty:
            logging.warning(f"[UPDATE] No 3m candles built for {symbol}")
            return None, None

        # --- Build 15m candles incrementally ---
        df_15m = build_15m_candles(df_ticks, tick_db, symbol)
        if df_15m.empty:
            logging.warning(f"[UPDATE] No 15m candles built for {symbol}")
            return None, df_3m

        # --- Merge yesterday’s historical 15m candles if provided ---
        if hist_yesterday_15m is not None and not hist_yesterday_15m.empty:
            df_15m = pd.concat([hist_yesterday_15m, df_15m]).drop_duplicates(
                subset=["trade_date", "ist_slot", "symbol"], keep="last"
            )
            logging.info(f"[BOOTSTRAP] Seeded bias with {len(hist_yesterday_15m)} candles from yesterday")

        # --- Resolve spot price ---
        if spot_price is None:
            latest_tick = tick_db.get_latest_tick(symbol)
            if latest_tick is not None:
                spot_price = latest_tick.get("ltp")
        if spot_price is None:
            logging.warning(f"[UPDATE] No spot price available for {symbol}")
            return None, df_3m

        # --- ATR calculation ---
        daily_val = daily_atr(df_15m)
        atr_value, atr_source = resolve_atr(df_15m, daily_val)
        atr_str = f"{atr_value:.2f}" if atr_value is not None else "NA"
        logging.info(f"[ATR] {symbol} source={atr_source} value={atr_str}")

        # --- Evaluate last 3m candle ---
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
            side, reason = signal
            logging.info(f"[SIGNAL FIRED] {symbol} side={side} reason={reason}")
            return signal, df_3m
        else:
            logging.debug(f"[SIGNAL CHECK] No signal for {symbol}")
            return None, df_3m

    except Exception as e:
        logging.error(f"[UPDATE ERROR] {symbol}: {e}")
        return None, None