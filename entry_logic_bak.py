import pandas as pd
import logging


def liquidity_zone(candle: pd.Series, supertrend_line: float, bias: str, atr: float, timeframe: str) -> dict:
    """
    Detect liquidity zone interaction with Supertrend line.
    
    Parameters:
    - candle: pd.Series with OHLCV
    - supertrend_line: float, Supertrend line value
    - bias: str, 'UP' or 'DOWN' (from higher timeframe or same timeframe)
    - atr: float, ATR tolerance
    - timeframe: str, '3m' or '15m' (for logging)
    
    Returns:
    - dict with zone info { 'zone': 'SUPPORT'/'RESISTANCE'/None,
                            'action': 'BUY'/'SELL'/'HOLD',
                            'reason': str }
    """
    signal = {"zone": None, "action": "HOLD", "reason": ""}

    if bias == "DOWN":
        if abs(candle["close"] - supertrend_line) <= atr:
            signal["zone"] = "RESISTANCE"
            signal["action"] = "SELL"
            signal["reason"] = f"{timeframe} liquidity rejection at Supertrend line {supertrend_line}"

    elif bias == "UP":
        if abs(candle["close"] - supertrend_line) <= atr:
            signal["zone"] = "SUPPORT"
            signal["action"] = "BUY"
            signal["reason"] = f"{timeframe} liquidity bounce at Supertrend line {supertrend_line}"

    return signal

def check_entry_condition(candle: pd.Series, indicators: dict, bias_15m: str) -> dict:
    """
    Evaluate entry signals using both 3m and 15m liquidity zones.
    Adds:
      - strength: HIGH for 15m zone, MEDIUM for 3m zone
      - zone_type: SUPPORT or RESISTANCE
    """

    signal = {"action": "HOLD", "reason": "", "strength": "NONE", "zone_type": None}

    # --- 3m Liquidity Zone ---
    lz_3m = liquidity_zone(
        candle,
        indicators["supertrend_line_3m"],
        bias_15m,
        indicators["atr"],
        timeframe="3m"
    )

    # --- 15m Liquidity Zone ---
    lz_15m = liquidity_zone(
        candle,
        indicators["supertrend_line_15m"],
        bias_15m,
        indicators["atr"],
        timeframe="15m"
    )

    # --- Entry Decision ---
    candidate = None
    if lz_15m["action"] in ["BUY", "SELL"]:
        candidate = lz_15m
        candidate["strength"] = "HIGH"
        candidate["zone_type"] = lz_15m["zone"]
    elif lz_3m["action"] in ["BUY", "SELL"]:
        candidate = lz_3m
        candidate["strength"] = "MEDIUM"
        candidate["zone_type"] = lz_3m["zone"]

    # Apply confirmation filters
    if candidate:
        if candidate["action"] == "BUY":
            if indicators["ema_fast"] > indicators["ema_slow"] and indicators["adx"] > 25:
                signal = candidate
                signal["reason"] += " + EMA/ADX confirmation"
        elif candidate["action"] == "SELL":
            if indicators["ema_fast"] < indicators["ema_slow"] and indicators["adx"] > 25:
                signal = candidate
                signal["reason"] += " + EMA/ADX confirmation"

    return signal