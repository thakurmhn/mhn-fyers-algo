import pandas as pd
import sqlite3
import logging
import os

from indicators import (
    resolve_atr,
    calculate_cpr,
    calculate_traditional_pivots,
    calculate_camarilla_pivots
)
from signals import detect_signal
from execution_bak import build_dynamic_levels, process_order
from orchestration import build_indicator_dataframe

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
    db_path_prev = os.path.join(r"C:\\SQLite\\ticks", f"ticks_{prev_date}.db")
    db_path_curr = os.path.join(r"C:\\SQLite\\ticks", f"ticks_{curr_date}.db")

    conn_prev = sqlite3.connect(db_path_prev)
    conn_curr = sqlite3.connect(db_path_curr)

    # --- Previous day OHLC (3m) ---
    query_prev = f"""
    SELECT trade_date, ist_slot, open, high, low, close
    FROM candles_3m_ist
    WHERE symbol = '{symbol}' AND trade_date = '{prev_date}'
    """
    df_prev = pd.read_sql(query_prev, conn_prev)
    df_prev["datetime"] = pd.to_datetime(df_prev["trade_date"] + " " + df_prev["ist_slot"])
    df_prev.set_index("datetime", inplace=True)

    prev_day = {
        "high": df_prev["high"].max(),
        "low": df_prev["low"].min(),
        "close": df_prev["close"].iloc[-1]
    }

    cpr_levels = calculate_cpr(prev_day["high"], prev_day["low"], prev_day["close"])
    trad_levels = calculate_traditional_pivots(prev_day["high"], prev_day["low"], prev_day["close"])
    cam_levels  = calculate_camarilla_pivots(prev_day["high"], prev_day["low"], prev_day["close"])
    logging.info(f"[LEVELS] CPR={cpr_levels} Traditional={trad_levels} Camarilla={cam_levels}")

    # --- Current day 3m candles ---
    query_curr_3m = f"""
    SELECT trade_date, ist_slot, open, high, low, close
    FROM candles_3m_ist
    WHERE symbol = '{symbol}' AND trade_date = '{curr_date}'
    """
    df_curr_3m = pd.read_sql(query_curr_3m, conn_curr)
    df_curr_3m["datetime"] = pd.to_datetime(df_curr_3m["trade_date"] + " " + df_curr_3m["ist_slot"])
    df_curr_3m.set_index("datetime", inplace=True)

    # --- Current day 15m candles ---
    query_curr_15m = f"""
    SELECT trade_date, ist_slot, open, high, low, close
    FROM candles_15m_ist
    WHERE symbol = '{symbol}' AND trade_date = '{curr_date}'
    """
    df_curr_15m = pd.read_sql(query_curr_15m, conn_curr)
    df_curr_15m["datetime"] = pd.to_datetime(df_curr_15m["trade_date"] + " " + df_curr_15m["ist_slot"])
    df_curr_15m.set_index("datetime", inplace=True)

    # --- Previous day 15m candles for continuity ---
    query_prev_15m = f"""
    SELECT trade_date, ist_slot, open, high, low, close
    FROM candles_15m_ist
    WHERE symbol = '{symbol}' AND trade_date = '{prev_date}'
    """
    df_prev_15m = pd.read_sql(query_prev_15m, conn_prev)
    df_prev_15m["datetime"] = pd.to_datetime(df_prev_15m["trade_date"] + " " + df_prev_15m["ist_slot"])
    df_prev_15m.set_index("datetime", inplace=True)

    logging.info(f"[DEBUG] Current day 3m candles={len(df_curr_3m)} | 15m candles={len(df_curr_15m)} | Prev 15m={len(df_prev_15m)}")

    # --- Replay loop ---
    state = None
    counters = {"CALL": 0, "PUT": 0}
    exit_counters = {"ATR_EXIT": 0, "MOMENTUM_EXIT": 0, "TIME_EXIT": 0,
                     "LOGIC_EXIT": 0, "SL_HIT": 0, "TARGET_HIT": 0}
    hold_durations = {k: [] for k in exit_counters.keys()}
    trail_updates_summary = []

    last_exit_candle = None
    traded_levels = set()

    for i in range(len(df_curr_3m)):
        df_slice_3m = df_curr_3m.iloc[: i + 1]

        # ✅ Merge continuity for 15m before enrichment
        merged_15m = pd.concat([df_prev_15m, df_curr_15m], ignore_index=False)
        merged_15m = merged_15m.drop_duplicates(subset=["trade_date","ist_slot"], keep="last").sort_index()
        df_slice_15m = merged_15m.loc[merged_15m.index <= df_slice_3m.index[-1]]

        if len(df_slice_3m) < 20:
            continue

        atr_val, _ = resolve_atr(df_slice_3m)
        if atr_val is None:
            continue

        if state is None:
            if last_exit_candle and i - last_exit_candle < cooldown:
                continue

            # signal = detect_signal(
            #     cpr_levels=cpr_levels,
            #     traditional_levels=trad_levels,
            #     camarilla_levels=cam_levels,
            #     candles_3m=df_slice_3m,
            #     atr=atr_val,
            #     bias="PREV_DAY",
            #     higher_tf=df_slice_15m if not df_slice_15m.empty else None,
            #     include_partial=False
            # )
            signal = detect_signal(
                candles_3m=df_slice_3m,
                candles_15m=df_slice_15m,
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

                side, reason = signal["side"], signal["reason"]
                entry_price = df_slice_3m.iloc[-1]["close"]
                entry_candle = df_slice_3m.iloc[-1]

                stop, pt, tg, trail_start, trail_step = build_dynamic_levels(
                    entry_price, atr_val, side, entry_candle
                )
                if stop is None:
                    logging.warning(f"[ENTRY SKIPPED] {side} ATR regime extreme → skipping trade")
                    continue

                state = {
                    "side": side,
                    "reason": reason,
                    "buy_price": entry_price,
                    "entry_candle": i,
                    "option_name": symbol,
                    "quantity": 1,
                    "stop": stop,
                    "pt": pt,
                    "tg": tg,
                    "trail_start": trail_start,
                    "trail_step": trail_step,
                    "prev_gap": 0,
                    "peak_momentum": 0,
                    "peak_candle": i,
                    "plateau_count": 0,
                    "trail_updates": 0,
                }

                counters[side] += 1
                print(f"{GREEN}[ENTRY] Candle {i}: side={side} reason={reason} ATR={fmt(atr_val)} SL={fmt(stop)}{RESET}")
            continue

        if not df_slice_15m.empty:
            enriched_15m = build_indicator_dataframe(symbol, df_slice_15m.copy(), interval="15m")
            logging.info(f"[15m CONTEXT] Tail of 15m candles at entry:\n{enriched_15m.tail(3)}")

        if state:
            dummy_info = {
                "call_buy": {"pnl": 0, "qty": 0, "trade_flag": 0, "quantity": 0,
                             "filled_df": pd.DataFrame(columns=[
                                 "symbol", "entry", "exit_price", "side",
                                 "entry_reason", "exit_reason",
                                 "entry_candle", "exit_candle",
                                 "pnl_points", "pnl_value",
                                 "spot_price", "qty"
                             ])},
                "put_buy": {"pnl": 0, "qty": 0, "trade_flag": 0, "quantity": 0,
                            "filled_df": pd.DataFrame(columns=[
                                "symbol", "entry", "exit_price", "side",
                                "entry_reason", "exit_reason",
                                "entry_candle", "exit_candle",
                                "pnl_points", "pnl_value",
                                "spot_price", "qty"
                            ])},
                "total_pnl": 0
            }

            triggered, reason = process_order(
                state,
                df_slice_3m,
                dummy_info,
                spot_price=df_slice_3m.iloc[-1]["close"],
                account_type="paper"
            )

            if triggered and reason:
                exit_counters[reason] += 1
                hold_durations[reason].append(i - state["entry_candle"])
                trail_updates_summary.append(state["trail_updates"])
                last_exit_candle = i
                print(f"{GREEN}[EXIT] Candle {i}: side={state['side']} {reason} "
                      f"SL={fmt(state['stop'])} TrailUpdates={state['trail_updates']}{RESET}")
                state = None

    logging.info(
        f"[SUMMARY] CALL trades={counters['CALL']} PUT trades={counters['PUT']} "
        + " ".join([f"{k} exits={v}" for k,v in exit_counters.items()])
    )

    for reason, durations in hold_durations.items():
        if durations:
            avg = sum(durations) / len(durations)
            logging.info(f"[HOLD] {reason} average candles held={avg:.2f}")

    if trail_updates_summary:
       avg_updates = sum(trail_updates_summary) / len(trail_updates_summary)
       logging.info(f"[TRAIL SUMMARY] Average trailing stop updates per trade={avg_updates:.2f}")



if __name__ == "__main__":
    symbol = "NSE:NIFTY50-INDEX"
    replay_run_strategy(symbol, prev_date="2026-02-05", curr_date="2026-02-06", cooldown=20)