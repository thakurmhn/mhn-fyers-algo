import pandas as pd
import logging
from datetime import datetime as dt
from zoneinfo import ZoneInfo

# --- Import your existing modules ---
from execution import paper_order, paper_info
from indicators import (
    calculate_cpr,
    calculate_traditional_pivots,
    calculate_camarilla_pivots,
    resolve_atr,
)
from signals import detect_signal
from config import time_zone

# Path to your extracted CSV file
csv_file = "candles-extracted.csv"

# --- Setup logging ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

def replay_session(csv_file: str):
    # Load candles from CSV
    df_candles = pd.read_csv(csv_file)
    logging.info(f"[REPLAY] Loaded {len(df_candles)} candles from {csv_file}")

    if df_candles.empty:
        logging.warning("[REPLAY] No candle data found in CSV file")
        return

    # Prepare pivots from first candle (proxy for prev day)
    prev_day = df_candles.iloc[0]
    cpr  = calculate_cpr(prev_day["high"], prev_day["low"], prev_day["close"])
    trad = calculate_traditional_pivots(prev_day["high"], prev_day["low"], prev_day["close"])
    cam  = calculate_camarilla_pivots(prev_day["high"], prev_day["low"], prev_day["close"])

    # Iterate through candles sequentially
    for i in range(len(df_candles)):
        sub_df = df_candles.iloc[:i+1]

        # Resolve ATR dynamically (no daily_atr in replay)
        atr, atr_source = resolve_atr(sub_df, None)

        # Detect signal
        signal = detect_signal(cpr, trad, cam, atr, sub_df)
        if signal:
            side, reason = signal
            candle_time = sub_df.iloc[-1]["time"]
            logging.info(f"[REPLAY SIGNAL] {side} ({reason}) at candle={candle_time} [ATR={atr_source}]")

            # --- Prepare globals for paper_order ---
            globals()["candles_3m"] = sub_df
            opt_symbol = f"NIFTY26JAN25500{'CE' if side=='CALL' else 'PE'}"
            globals()["df"] = pd.DataFrame({"ltp": [sub_df.iloc[-1]["close"]]}, index=[opt_symbol])
            globals()["spot_price"] = sub_df.iloc[-1]["close"]
            globals()["daily_atr"] = None  # not available in replay

            # Call entry logic
            try:
                paper_order()  # uses globals
                leg = "call_buy" if side == "CALL" else "put_buy"
                logging.info(f"[ENTRY STATE][{side}] {paper_info[leg]}")
            except Exception as e:
                logging.error(f"[ENTRY ERROR] {e}", exc_info=True)

    logging.info("[REPLAY] Completed session replay")

if __name__ == "__main__":
    replay_session(csv_file=csv_file)