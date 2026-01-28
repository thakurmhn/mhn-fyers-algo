# ===== main.py =====
import asyncio
import time
import logging
import pandas as pd
import pendulum as dt
import warnings
import datetime

from config import account_type, time_zone, index_name
from setup import fyers_asysc, df, end_time, hist_data, spot_price
from execution import paper_order, real_order
from data_feed import fyers_socket, fyers_order_socket, chase_order, orders_df
from tickdb import TickDatabase
from indicators import (
    build_15m_candles,
    build_3min_candle,
    check_bias,
    calculate_cpr,
    calculate_traditional_pivots,
    calculate_camarilla_pivots,
    resolve_atr,
)
from signals import detect_signal
from monitor import monitor_positions   # ✅ bring back broker position monitoring
from indicators import daily_atr, resolve_atr

warnings.filterwarnings("ignore", category=UserWarning, module="pkg_resources")

# ANSI COLORS
RESET   = "\033[0m"
GREEN   = "\033[92m"
YELLOW  = "\033[93m"
RED     = "\033[91m"
MAGENTA = "\033[95m"
GRAY    = "\033[90m"
CYAN    = "\033[96m"

async def main_strategy_code():
    db = TickDatabase()
    symbol = index_name

    # ✅ Seed pivots immediately from previous daily candle in hist_data
    cpr = trad = cam = None
    if not hist_data.empty:
        prev_day = hist_data.iloc[-1]
        cpr = calculate_cpr(prev_day["high"], prev_day["low"], prev_day["close"])
        trad = calculate_traditional_pivots(prev_day["high"], prev_day["low"], prev_day["close"])
        cam = calculate_camarilla_pivots(prev_day["high"], prev_day["low"], prev_day["close"])
        logging.info(f"{YELLOW}[PIVOTS SEEDED] CPR={cpr}{RESET}")
        logging.info(f"{YELLOW}[PIVOTS SEEDED] Traditional={trad}{RESET}")
        logging.info(f"{YELLOW}[PIVOTS SEEDED] Camarilla={cam}{RESET}")
    else:
        logging.warning("[PIVOTS] hist_data empty, pivots not seeded")

    # ===== Debug hist_data before ATR =====
    logging.info(f"[DEBUG] hist_data rows={len(hist_data)} columns={hist_data.columns.tolist()}")
    if not hist_data.empty:
        logging.info(f"[DEBUG] Last 3 rows of hist_data:\n{hist_data.tail(3)}")

    # ===== Seed Daily ATR =====
    daily_atr_val = daily_atr(hist_data, period=14)
    if daily_atr_val is not None:
        logging.info(f"{YELLOW}[DAILY ATR] {daily_atr_val:.2f}{RESET}")
    else:
        logging.warning("[DAILY ATR] Not enough daily candles to compute ATR")

    # Step 1: Load yesterday’s ticks from SQL DB
    yesterday = datetime.date.today() - datetime.timedelta(days=1)
    ticks_df = pd.read_sql_query(
        "SELECT * FROM ticks WHERE symbol=?", db.conn, params=[symbol]
    )

    # Step 2: Build 15m candles from historical ticks
    hist_yesterday_15m = build_15m_candles(ticks_df, target_date=yesterday)

    # Step 3: Seed bias before session starts
    if not hist_yesterday_15m.empty:
        bias = check_bias(hist_yesterday_15m)
        logging.info(f"{YELLOW}[PRE-SESSION BIAS] {bias}{RESET}")
    else:
        bias = None
        logging.warning("[PRE-SESSION BIAS] No yesterday 15m candles available")

    # Detect signal immediately using seeded bias + pivots + daily ATR
    if cpr is not None and trad is not None and cam is not None and daily_atr_val is not None:
        signal = detect_signal(
            cpr, trad, cam,
            daily_atr_val, hist_yesterday_15m,
            hist_yesterday_15m=hist_yesterday_15m,
            atr_src="ATR_DAILY"   # ✅ explicitly mark daily ATR
        )
        if signal:
            side, reason = signal
            logging.info(f"{YELLOW}[PRE-SESSION SIGNAL] {side} ({reason}){RESET}")
            if account_type == "PAPER":
                paper_order(hist_yesterday_15m)
            else:
                real_order(hist_yesterday_15m)

    # Step 4: Live loop
    last_bias_refresh = None
    last_candle_ts = None

    while True:
        ct = dt.now(time_zone)

        # End-of-day shutdown
        if ct > end_time + dt.duration(minutes=2):
            logging.info("closing program")
            db.close()
            return

        # Bias refresh every 60 minutes
        if last_bias_refresh is None or (ct - last_bias_refresh).total_seconds() >= 3600:
            hist_today_15m = build_15m_candles(df, target_date=datetime.date.today())
            full_hist = pd.concat([hist_yesterday_15m, hist_today_15m]).reset_index(drop=True)
            bias = check_bias(full_hist)
            logging.info(f"{YELLOW}[INTRADAY BIAS REFRESH] bias={bias} candles={len(full_hist)} at {ct}{RESET}")
            last_bias_refresh = ct

        # Refresh 3m candles
        hist_today_3m = build_3min_candle(df, target_date=datetime.date.today())
        if not hist_today_3m.empty:
            last_candle = hist_today_3m.iloc[-1]
            ts = last_candle.name

            # Log only when a new candle closes
            if last_candle_ts != ts:
                last_candle_ts = ts
                logging.info(
                    f"{YELLOW}[3M CANDLE CLOSED] {ts} | O={last_candle.open:.2f} "
                    f"H={last_candle.high:.2f} L={last_candle.low:.2f} "
                    f"C={last_candle.close:.2f} | Spot={spot_price:.2f}{RESET}"
                )

                # Signal evaluation context — prefer 3m ATR, fallback to daily ATR
                atr_val, atr_src = resolve_atr(hist_today_3m, daily_atr_val)
                logging.info(
                    f"{YELLOW}[SIGNAL EVAL][{account_type}] candle={ts} candles={len(hist_today_3m)} atr={atr_val:.2f} source={atr_src}{RESET}"
                )

                # Detect signal on candle close
                if cpr is not None and trad is not None and cam is not None:
                    signal = detect_signal(
                        cpr, trad, cam,
                        atr_val, hist_today_3m,
                        hist_yesterday_15m=hist_yesterday_15m,
                        atr_src=atr_src   # ✅ pass source here
                    )
                    if signal:
                        side, reason = signal
                        logging.info(f"{YELLOW}[DETECT_SIGNAL CALLED] side={side} reason={reason}{RESET}")
                        if account_type == "PAPER":
                            paper_order(hist_today_3m)
                        else:
                            real_order(hist_today_3m)
                else:
                    logging.warning("[SIGNAL SKIP] Pivots not available yet")

        # Order chasing + broker PnL every 5 seconds
        if ct.second % 5 == 0:
            logging.info(f"{CYAN}[CHASE] Checking pending orders...{RESET}")
            chase_order(orders_df)

            try:
                pos1 = await fyers_asysc.positions()
                pnl = int(pos1.get('overall', {}).get('pl_total', 0))
                logging.info(f"{GRAY}Live PnL from broker: {pnl}{RESET}")
                await monitor_positions()
            except Exception as e:
                logging.error(f"Unable to fetch pnl or positions: {e}")

        await asyncio.sleep(1)


def run():
    fyers_socket.connect()
    fyers_order_socket.connect()
    time.sleep(2)
    try:
        asyncio.run(main_strategy_code())
    except KeyboardInterrupt:
        logging.info("Manual interrupt received, shutting down.")
    finally:
        logging.info("Program terminated.")


if __name__ == "__main__":
    run()