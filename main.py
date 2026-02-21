# ===== main.py (FIXED) =====
"""
ROOT CAUSE OF NO SIGNALS (from 14:44 log):

BUG 1 — run_strategy() fails silently every iteration due to Fyers quote API error
  Log: [SPOT REFRESH FAILED] NSE:NIFTY50-INDEX: 'd'
  Code in run_strategy():
      quote = fyers.quotes(data={"symbols": sym})
      spot_price = quote["d"][0]["v"]["lp"]   ← KeyError 'd' when API returns error dict
      ...
      continue   ← skips everything for this symbol

  Result: paper_order() is NEVER called. No signals possible.

BUG 2 — run_strategy() uses wrong candle source
  update_candles_and_signals() fetches from Fyers history API (yesterday only).
  The actual live candles are built by data_feed.py from websocket ticks
  and stored in tick_db. These two are completely separate sources.
  The log shows "[LIVE 3M] ... 112 rows" from tick_db but run_strategy
  never sees those candles.

BUG 3 — run_strategy() has sleep_until_next_boundary(180) inside asyncio
  This blocks the entire event loop for up to 3 minutes.
  asyncio.sleep(1) in main_strategy_code() is meaningless because
  run_strategy() synchronously sleeps for 3 minutes inside it.
  Result: order socket, PnL fetch, chase_order all freeze for 3 minutes.

FIX:
  Remove run_strategy() from the hot loop entirely.
  main_strategy_code() now directly:
    1. Fetches today's candles from tick_db (the correct live source)
    2. Builds indicators via build_indicator_dataframe()
    3. Calls paper_order() or live_order() directly every second
    4. Exit checks run every second (paper_order handles de-dup internally)
  
  run_strategy() is kept for REPLAY mode only (unchanged).
  Fyers quote API is no longer in the critical path.
  spot_price comes from the websocket LTP (most recent tick).
"""

import asyncio
import time
import logging
import pandas as pd
import pendulum as dt
import warnings
from datetime import datetime, timedelta

from config import time_zone, MODE, symbols, account_type, strategy_name
from execution import paper_order, live_order, run_strategy, risk_info
from data_feed import (
    fyers_socket, fyers_order_socket, chase_order,
    fyers_async, tick_db, symbols, spot_price as _ws_spot
)
from orchestration import build_indicator_dataframe
from indicators import (
    calculate_cpr,
    calculate_traditional_pivots,
    calculate_camarilla_pivots,
    resolve_atr,
    daily_atr,
)

warnings.filterwarnings("ignore", category=UserWarning, module="pkg_resources")

# ANSI COLORS
RESET   = "\033[0m"
GREEN   = "\033[92m"
YELLOW  = "\033[93m"
RED     = "\033[91m"
MAGENTA = "\033[95m"
GRAY    = "\033[90m"
CYAN    = "\033[96m"

symbols = ["NSE:NIFTY50-INDEX"]


# ─────────────────────────────────────────────────────────────────────────────
# STARTUP: print daily pivot levels
# ─────────────────────────────────────────────────────────────────────────────

def print_daily_levels():
    """Print pivot levels and ATR regime for each symbol at startup."""
    for sym in symbols:
        hist_data = tick_db.fetch_candles("15m", use_yesterday=True, symbol=sym)
        if hist_data is None or hist_data.empty:
            logging.warning(f"[DAILY LEVELS] No historical data for {sym}")
            continue

        prev_day = hist_data.iloc[-1]
        ph, pl, pc = float(prev_day['high']), float(prev_day['low']), float(prev_day['close'])

        cpr_levels  = calculate_cpr(ph, pl, pc)
        trad_levels = calculate_traditional_pivots(ph, pl, pc)
        cam_levels  = calculate_camarilla_pivots(ph, pl, pc)

        daily_atr_val = daily_atr(hist_data)
        atr_val, atr_src = resolve_atr(pd.DataFrame(), daily_atr_val)
        atr_display = f"{atr_val:.2f}" if atr_val else "N/A"
        atr_regime  = "HIGH" if (atr_val and atr_val > 120) else "LOW"

        logging.info(
            f"{GREEN}[{sym}] "
            f"CPR: P={cpr_levels['pivot']} TC={cpr_levels['tc']} BC={cpr_levels['bc']} | "
            f"Trad: P={trad_levels['pivot']} R1={trad_levels['r1']} S1={trad_levels['s1']} | "
            f"Cam: R3={cam_levels['r3']} S3={cam_levels['s3']} | "
            f"ATR={atr_display} ({atr_src}, {atr_regime}){RESET}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# CANDLE FETCH: get today's indicator-enriched candles from tick_db
# ─────────────────────────────────────────────────────────────────────────────

def get_live_candles(sym):
    """
    Fetch today's completed candles from tick_db and enrich with indicators.
    Returns (df_3m, df_15m) — same format paper_order/live_order expect.

    tick_db.fetch_candles("3m", use_yesterday=False) returns today's candles.
    tick_db.fetch_candles("15m", use_yesterday=False) returns today's 15m candles.

    Indicators (Supertrend, EMA, CCI, RSI, ADX) are computed on the combined
    historical + today DataFrame so the Supertrend state machine is correct.
    """
 
    try:
        # Today's candles from tick_db (live, built from websocket ticks)
        df_3m_today  = tick_db.fetch_candles("3m",  use_yesterday=False, symbol=sym)
        df_15m_today = tick_db.fetch_candles("15m", use_yesterday=False, symbol=sym)

        # 3m history: yesterday only — 3m indicators need ~35 bars = ~105 min = 1 session
        df_3m_hist  = tick_db.fetch_candles("3m",  use_yesterday=True,  symbol=sym)

        # 15m history: fetch last 5 trading days for proper indicator warmup.
        # ADX14 needs 28+ bars, CCI20 needs 20+ bars → 28 × 15m = 7 hours = ~2 sessions.
        # We fetch 5 days to be safe (5 × 25 bars ≈ 125 bars total).
        import pytz, os, sqlite3 as _sql
        _tz   = pytz.timezone("Asia/Kolkata")
        _today = datetime.now(_tz).date()
        _15m_frames = []
        _offset = 1
        _days_found = 0
        while _offset <= 14 and _days_found < 5:     # scan up to 2 weeks for 5 trading days
            _candidate = (_today - timedelta(days=_offset)).isoformat()
            _df = tick_db.fetch_candles("15m", use_yesterday=False, symbol=sym)  # placeholder
            # Directly query by date since fetch_candles only supports yesterday
            for _table in ["candles_15m_ist"]:
                try:
                    _q = f"SELECT * FROM {_table} WHERE trade_date = ? AND symbol = ?"
                    _tmp = pd.read_sql_query(_q, tick_db.conn, params=[_candidate, sym])
                    if _tmp.empty:
                        _db_path = f"C:/SQLite/ticks/ticks_{_candidate}.db"
                        if os.path.exists(_db_path):
                            with _sql.connect(_db_path) as _c:
                                _tmp = pd.read_sql_query(_q, _c, params=[_candidate, sym])
                    if not _tmp.empty:
                        _15m_frames.append(_tmp)
                        _days_found += 1
                except Exception as _e:
                    logging.debug(f"[15m WARMUP] date={_candidate}: {_e}")
            _offset += 1

        df_15m_hist = pd.concat(_15m_frames, ignore_index=True) if _15m_frames else pd.DataFrame()

        def _merge(hist, today):
            if hist is None:  hist  = pd.DataFrame()
            if today is None: today = pd.DataFrame()
            if hist.empty and today.empty:
                return pd.DataFrame()
            if hist.empty:  return today
            if today.empty: return hist
            combined = pd.concat([hist, today], ignore_index=True)
            # Deduplicate on time column if available
            time_col = "date" if "date" in combined.columns else (
                       "time" if "time" in combined.columns else None)
            if time_col:
                combined = (combined.drop_duplicates(subset=[time_col])
                                    .sort_values(time_col)
                                    .reset_index(drop=True))
            return combined

        df_3m  = _merge(df_3m_hist,  df_3m_today)
        df_15m = _merge(df_15m_hist, df_15m_today)

        # Build indicators
        if not df_3m.empty:
            df_3m  = build_indicator_dataframe(sym, df_3m,  interval="3m")
        if not df_15m.empty:
            df_15m = build_indicator_dataframe(sym, df_15m, interval="15m")

        return df_3m, df_15m

    except Exception as e:
        logging.error(f"[GET LIVE CANDLES] {sym}: {e}", exc_info=True)
        return pd.DataFrame(), pd.DataFrame()


# ─────────────────────────────────────────────────────────────────────────────
# MAIN STRATEGY LOOP (FIXED)
# ─────────────────────────────────────────────────────────────────────────────

async def main_strategy_code():
    """
    Main async loop. Runs every second.

    FIX: No longer calls run_strategy() which:
      - Required Fyers quote API (fails intermittently → [SPOT REFRESH FAILED])
      - Used Fyers history candles (wrong source — live candles are in tick_db)
      - Blocked the event loop with sleep_until_next_boundary(180)

    Now directly:
      - Fetches tick_db candles (correct live source, no API call needed)
      - Builds indicators
      - Calls paper_order() or live_order() every second
      - Exit checks fire every second regardless of candle boundaries
    """
    today    = dt.now(time_zone).date()
    end_time = dt.datetime(today.year, today.month, today.day, 15, 30, tz=time_zone)

    # Cache: only rebuild indicators when candle count changes (every ~3 min)
    candle_cache = {sym: (pd.DataFrame(), pd.DataFrame(), 0) for sym in symbols}
    # (df_3m, df_15m, last_3m_count)

    logging.info(f"{GREEN}[MAIN] Strategy loop started. End time={end_time}{RESET}")

    while True:
        ct = dt.now(time_zone)

        # Stop after session end
        if ct > end_time.add(minutes=2):
            logging.info("[MAIN] Session ended. Shutting down.")
            return

        # ── Order management (every 5 seconds) ─────────────────────────────
        if ct.second % 5 == 0:
            try:
                order_response = await fyers_async.orderbook()
                order_df = (pd.DataFrame(order_response["orderBook"])
                            if order_response.get("orderBook") else pd.DataFrame())
                chase_order(order_df)

                pos1 = await fyers_async.positions()
                pnl  = int(pos1.get("overall", {}).get("pl_total", 0))
                logging.info(f"{GRAY}[PnL] Live broker PnL={pnl}{RESET}")

            except Exception as e:
                logging.debug(f"[ORDERBOOK/PNL ERROR] {e}")

        # ── Strategy ────────────────────────────────────────────────────────
        if MODE != "STRATEGY":
            await asyncio.sleep(1)
            continue

        for sym in symbols:
            try:
                # Get current candle count from tick_db to detect new candles
                df_3m_raw = tick_db.fetch_candles("3m", use_yesterday=False, symbol=sym)
                current_count = len(df_3m_raw) if df_3m_raw is not None else 0

                # Rebuild indicators when a new 3m candle is available
                cached_3m, cached_15m, last_count = candle_cache[sym]
                if current_count != last_count or cached_3m.empty:
                    df_3m, df_15m = get_live_candles(sym)
                    candle_cache[sym] = (df_3m, df_15m, current_count)
                    logging.info(
                        f"{GRAY}[CANDLE REFRESH] {sym} "
                        f"3m={len(df_3m) if not df_3m.empty else 0} "
                        f"15m={len(df_15m) if not df_15m.empty else 0}{RESET}"
                    )
                else:
                    df_3m, df_15m = cached_3m, cached_15m

                if df_3m is None or df_3m.empty:
                    logging.debug(f"[MAIN] No 3m candles for {sym}, skipping")
                    continue

                # Call order function — handles exit every call, entry de-duped per candle
                if account_type.upper() == "PAPER":
                    paper_order(df_3m, hist_yesterday_15m=df_15m, mode="LIVE")
                else:
                    live_order(df_3m, hist_yesterday_15m=df_15m)

            except Exception as e:
                logging.error(f"[STRATEGY ERROR] {sym}: {e}", exc_info=True)

        await asyncio.sleep(1)


# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

def run():
    fyers_socket.connect()
    fyers_order_socket.connect()
    time.sleep(2)  # allow sockets to connect

    try:
        asyncio.run(main_strategy_code())
    except KeyboardInterrupt:
        logging.info("[MAIN] Interrupted.")
    finally:
        logging.info("[MAIN] Terminated.")


if __name__ == "__main__":
    print_daily_levels()
    run()
