import logging
import pandas as pd
from orchestration import build_indicator_dataframe, fetch_fyers_history
from indicators import resolve_atr, calculate_cpr, calculate_traditional_pivots, calculate_camarilla_pivots
from signals import detect_signal
from execution_bak import build_dynamic_levels, process_order

RESET   = "\033[0m"
GREEN   = "\033[92m"

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(levelname)s - %(message)s")

def fmt(val):
    try:
        if val is None or (isinstance(val, float) and pd.isna(val)):
            return "NA"
        return f"{val:.2f}"
    except Exception:
        return str(val) if val is not None else "NA"

def replay_run_strategy(symbol, prev_date, curr_date, cooldown=20):
    # --- Fetch previous day candles ---
    df_prev_3m = fetch_fyers_history(symbol, resolution="3", days=1)
    df_prev_15m = fetch_fyers_history(symbol, resolution="15", days=1)

    prev_day = {
        "high": df_prev_3m["high"].max(),
        "low": df_prev_3m["low"].min(),
        "close": df_prev_3m["close"].iloc[-1]
    }

    cpr_levels = calculate_cpr(prev_day["high"], prev_day["low"], prev_day["close"])
    trad_levels = calculate_traditional_pivots(prev_day["high"], prev_day["low"], prev_day["close"])
    cam_levels  = calculate_camarilla_pivots(prev_day["high"], prev_day["low"], prev_day["close"])
    logging.info(f"[LEVELS] CPR={cpr_levels} Traditional={trad_levels} Camarilla={cam_levels}")

    # --- Fetch current day candles ---
    df_curr_3m = fetch_fyers_history(symbol, resolution="3", days=1)
    df_curr_15m = fetch_fyers_history(symbol, resolution="15", days=1)

    logging.info(f"[DEBUG] Current day 3m candles={len(df_curr_3m)} | 15m candles={len(df_curr_15m)}")

    # --- Replay loop ---
    state = None
    counters = {"CALL": 0, "PUT": 0}
    traded_levels = set()
    last_exit_candle = None

    for i in range(len(df_curr_3m)):
        df_slice_3m = df_curr_3m.iloc[: i + 1]

        # Merge continuity for 15m
        merged_15m = pd.concat([df_prev_15m, df_curr_15m], ignore_index=False)
        merged_15m = merged_15m.drop_duplicates(subset=["date"], keep="last").sort_values("date")
        df_slice_15m = merged_15m.loc[merged_15m["date"] <= df_slice_3m.iloc[-1]["date"]]

        if len(df_slice_3m) < 20:
            continue

        atr_val, _ = resolve_atr(df_slice_3m)
        if atr_val is None:
            continue

        if state is None:
            if last_exit_candle and i - last_exit_candle < cooldown:
                continue

            # ✅ Enrich slices
            enriched_3m = build_indicator_dataframe(symbol, df_slice_3m.copy(), interval="3m")
            enriched_15m = build_indicator_dataframe(symbol, df_slice_15m.copy(), interval="15m") if not df_slice_15m.empty else pd.DataFrame()

            signal = detect_signal(
                candles_3m=enriched_3m,
                candles_15m=enriched_15m,
                cpr_levels=cpr_levels,
                camarilla_levels=cam_levels,
                traditional_levels=trad_levels,
                atr=atr_val,
                include_partial=False
            )

            if signal:
                level_key = f"{signal['reason']}_{signal['side']}"
                if level_key in traded_levels:
                    continue
                traded_levels.add(level_key)

                side, reason, source = signal["side"], signal["reason"], signal.get("source", "UNKNOWN")
                entry_price = enriched_3m.iloc[-1]["close"]

                stop, pt, tg, trail_start, trail_step = build_dynamic_levels(entry_price, atr_val, side, enriched_3m.iloc[-1])
                if stop is None:
                    logging.warning(f"[ENTRY SKIPPED] {side} ATR regime extreme → skipping trade")
                    continue

                state = {
                    "side": side,
                    "reason": reason,
                    "source": source,
                    "buy_price": entry_price,
                    "entry_candle": i,
                    "option_name": symbol,
                    "quantity": 1,
                    "stop": stop,
                    "pt": pt,
                    "tg": tg,
                    "trail_start": trail_start,
                    "trail_step": trail_step,
                }

                counters[side] += 1
                print(f"{GREEN}[ENTRY] Candle {i}: side={side} source={source} reason={reason} ATR={fmt(atr_val)} SL={fmt(stop)}{RESET}")
            continue

    logging.info(f"[SUMMARY] CALL trades={counters['CALL']} PUT trades={counters['PUT']}")