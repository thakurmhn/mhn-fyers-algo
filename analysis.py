# from indicators import ema, supertrend, atr, adx
from signals import detect_signal
import logging
from tickdb import tick_db
import pandas as pd

from indicators import calculate_ema as ema
from indicators import calculate_atr as atr
from indicators import calculate_cci as cci
from indicators import calculate_adx as adx
from indicators import supertrend 
import logging
from tickdb import tick_db

def bias_from_indicators(row, df_15m=None):
    reason = []
    score = 0

    # EMA crossover bias
    if row['ema20'] > row['ema50']:
        reason.append("EMA20>EMA50")
        score += 20
    elif row['ema20'] < row['ema50']:
        reason.append("EMA20<EMA50")
        score += 20

    # Supertrend bias (3m)
    if row['supertrend'] == 'up':
        reason.append("3m Supertrend=UP")
        score += 20
    elif row['supertrend'] == 'down':
        reason.append("3m Supertrend=DOWN")
        score += 20

    # ADX strength
    if row['adx14'] > 20:
        reason.append("ADX strong")
        score += 20

    # CCI filter
    if row['cci20'] > 50:
        reason.append("CCI>50")
        score += 20
    elif row['cci20'] < -50:
        reason.append("CCI<-50")
        score += 20

    # --- Multi-timeframe Supertrend check ---
    st_15m = None
    if df_15m is not None and not df_15m.empty:
        st_15m_val = df_15m.iloc[-1]['supertrend']
        # Normalize numeric or string values
        if isinstance(st_15m_val, str):
            st_15m = st_15m_val.lower()
        elif isinstance(st_15m_val, (int, float)):
            st_15m = "up" if st_15m_val > 0 else "down"
        else:
            st_15m = None

        if st_15m is not None:
            reason.append(f"15m Supertrend={st_15m.upper()}")
            score += 20

    # --- Decision logic ---
    if (row['supertrend'] == 'up' and st_15m == 'up'
        and row['close'] > row['ema20']
        and row['cci20'] > 50
        and row['adx14'] > 20):
        return "CALL BUY | " + ", ".join(reason), score

    elif (row['supertrend'] == 'down' and st_15m == 'down'
          and row['close'] < row['ema20']
          and row['cci20'] < -50
          and row['adx14'] > 20):
        return "PUT SELL | " + ", ".join(reason), score

    else:
        return "HOLD | " + ", ".join(reason), score
    

def build_indicator_dataframe(symbol, interval="3m", df_15m=None):
    df = tick_db.fetch_candles(interval, symbol=symbol)
    if df.empty:
        logging.warning(f"[INDICATORS] No candles found for {symbol}")
        return df

    # Apply indicators consistently
    df['ema20'] = ema(df, period=20)
    df['ema50'] = ema(df, period=50)
    df['atr14'] = atr(df, period=14)
    df['supertrend'] = supertrend(df)
    df['adx14'] = adx(df, period=14)
    df['cci20'] = cci(df, period=20)

    # Add signal + confidence columns
    df[['signal', 'confidence']] = df.apply(
        lambda row: pd.Series(bias_from_indicators(row, df_15m)), axis=1
    )

    return df

def build_multi_symbol_indicators(symbols=None, interval="3m"):
    """
    Build indicator DataFrames for multiple symbols.
    Includes both the requested interval (default 3m) and 15m candles
    for multi-timeframe confirmation.
    """
    if symbols is None:
        symbols = ["NSE:NIFTY-INDEX", "NSE:FINNIFTY-INDEX"]

    result = {}
    for sym in symbols:
        # Build 15m indicators for multi-timeframe confirmation
        df_15m = build_indicator_dataframe(sym, interval="15m")

        # Build main interval indicators, passing 15m DataFrame for bias checks
        df = build_indicator_dataframe(sym, interval=interval, df_15m=df_15m)

        result[sym] = df
        logging.info(f"[INDICATORS] Built {interval} DataFrame for {sym} with {len(df)} rows")

    return result


if __name__ == "__main__":
    from config import symbols   # ✅ use the same list defined in config.py

    logging.info("[ANALYSIS] Starting end-of-day analysis run")

    # Build indicators for all symbols
    results = build_multi_symbol_indicators(symbols=symbols, interval="3m")

    # Print/log the latest signal for each symbol
    for sym, df in results.items():
        if not df.empty:
            latest = df.iloc[-1]
            logging.info(
                f"[ANALYSIS][{sym}] "
                f"Signal={latest['signal']} "
                f"Confidence={latest.get('confidence', 'NA')} "
                f"Close={latest['close']:.2f}"
            )
        else:
            logging.warning(f"[ANALYSIS][{sym}] No data available")

    logging.info("[ANALYSIS] Completed end-of-day run")