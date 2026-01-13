import pandas as pd
import numpy as np
from signals import detect_signal, is_strong_trade_replay
from indicators import (
    calculate_atr,
    calculate_cpr,
    calculate_camarilla_pivots,
    calculate_traditional_pivots
)



# Load your 3m candles CSV
df = pd.read_csv(
    "candles_3m.csv",
    parse_dates=["timestamp"],
    date_format="%H:%M:%S"   # parses HH:MM:SS consistently
)

candles_3m_ = pd.DataFrame()

for i, row in df.iterrows():
    # Append new candle
    candles_3m_ = pd.concat([candles_3m_, pd.DataFrame([row])], ignore_index=True)

    # Skip until we have enough candles for ATR
    if len(candles_3m_) < 14:   # ATR usually needs 14 periods
        continue

    # Compute ATR on rolling candles
    atr = calculate_atr(candles_3m_)
    if atr is None:
        continue

    # Use the most recent candle’s OHLC for pivots (simplified for replay)
    last_high = candles_3m_["high"].iloc[-1]
    last_low  = candles_3m_["low"].iloc[-1]
    last_close = candles_3m_["close"].iloc[-1]

    cpr_levels = calculate_cpr(last_high, last_low, last_close)
    traditional_levels = calculate_traditional_pivots(last_high, last_low, last_close)
    camarilla_levels = calculate_camarilla_pivots(last_high, last_low, last_close)

    # Previous day high/low (simplified for replay)
    prev_day_levels = {
        "high": candles_3m_["high"].max(),
        "low": candles_3m_["low"].min()
    }

    # Run signal detection
    #result = detect_signal(cpr_levels, traditional_levels, camarilla_levels, atr, candles_3m_, prev_day_levels)
    result = detect_signal(cpr_levels, traditional_levels, camarilla_levels,
                       atr, candles_3m_, prev_day_levels,
                       strong_trade_fn=is_strong_trade_replay)

    if result:
        signal, meta = result
        print(f"{row['timestamp'].time()} | {signal} | {meta}")

if __name__ == "__main__":
    import pandas as pd
    import numpy as np

    # Example: load candles and run detect_signal once
    df = pd.read_csv("candles_3m.csv")
    candles_3m_ = df.head(20)  # take first 20 candles for demo

    atr = calculate_atr(candles_3m_)
    last_high = candles_3m_["high"].iloc[-1]
    last_low  = candles_3m_["low"].iloc[-1]
    last_close = candles_3m_["close"].iloc[-1]

    cpr_levels = calculate_cpr(last_high, last_low, last_close)
    traditional_levels = calculate_traditional_pivots(last_high, last_low, last_close)
    camarilla_levels = calculate_camarilla_pivots(last_high, last_low, last_close)

    prev_day_levels = {
        "high": candles_3m_["high"].max(),
        "low": candles_3m_["low"].min()
    }

    result = detect_signal(cpr_levels, traditional_levels, camarilla_levels, atr, candles_3m_, prev_day_levels)
    print("Standalone run result:", result)