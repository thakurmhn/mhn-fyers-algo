# ===== oscillator_filters.py =====
import logging

def williams_r(candles, period=14):
    """
    Calculate Williams %R.
    candles: DataFrame with columns ['high','low','close']
    """
    highest_high = candles['high'].tail(period).max()
    lowest_low   = candles['low'].tail(period).min()
    last_close   = candles['close'].iloc[-1]
    if highest_high == lowest_low:
        return -50  # neutral fallback
    return ((highest_high - last_close) / (highest_high - lowest_low)) * -100


def cci_indicator(candles, period=20):
    """
    Calculate Commodity Channel Index (CCI).
    candles: DataFrame with columns ['high','low','close']
    """
    tp = (candles['high'] + candles['low'] + candles['close']) / 3
    ma = tp.tail(period).mean()
    md = (tp.tail(period) - ma).abs().mean()
    if md == 0:
        return 0
    return (tp.iloc[-1] - ma) / (0.015 * md)


# ===== Entry Filter =====
def oscillator_entry_filter(side, candles_3m):
    """
    Block entries if oscillators show exhaustion on 3m timeframe.
    Returns True if entry is allowed, False if blocked.
    """
    wr  = williams_r(candles_3m)
    cci = cci_indicator(candles_3m)

    if side == "CALL" and (wr >= -10 or cci >= 200):
        logging.info(f"[ENTRY BLOCKED][OSC] CALL skipped (W%R={wr:.2f}, CCI={cci:.2f})")
        return False

    if side == "PUT" and (wr <= -90 or cci <= -200):
        logging.info(f"[ENTRY BLOCKED][OSC] PUT skipped (W%R={wr:.2f}, CCI={cci:.2f})")
        return False

    return True


# ===== Exit Trigger =====
def oscillator_exit_trigger(side, candles_15m):
    """
    Trigger exits if oscillators hit extremes on 15m timeframe.
    Returns tuple: (triggered: bool, reason: str)
    """
    wr  = williams_r(candles_15m)
    cci = cci_indicator(candles_15m)

    if side == "CALL":
        if wr == 0:
            logging.info(f"[EXIT SIGNAL][OSC] CALL exit W%R={wr:.2f}")
            return True, "W%R=0 (overbought extreme)"
        if cci >= 200:
            logging.info(f"[EXIT SIGNAL][OSC] CALL exit CCI={cci:.2f}")
            return True, f"CCI={cci:.2f} >= 200 (bullish extreme)"
    elif side == "PUT":
        if wr == -100:
            logging.info(f"[EXIT SIGNAL][OSC] PUT exit W%R={wr:.2f}")
            return True, "W%R=-100 (oversold extreme)"
        if cci <= -200:
            logging.info(f"[EXIT SIGNAL][OSC] PUT exit CCI={cci:.2f}")
            return True, f"CCI={cci:.2f} <= -200 (bearish extreme)"

    return False, ""