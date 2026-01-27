# ===== main.py =====
# import asyncio
# import time
# import logging
# import pandas as pd
# import pendulum as dt
# import warnings

# from config import account_type, time_zone, index_name
# from setup import fyers_asysc, df, end_time, refresh_option_chain, log_bid_ask_spread   # ✅ import helper
# from execution import paper_order, real_order
# from data_feed import fyers_socket, fyers_order_socket, chase_order
# from tickdb import TickDatabase, merge_with_live_ticks, write_candle_to_db
# from signals import detect_signal
# from indicators import bias_check

# warnings.filterwarnings("ignore", category=UserWarning, module="pkg_resources")

# ANSI COLORS
RESET   = "\033[0m"
GRAY    = "\033[90m"
CYAN    = "\033[96m"

# async def main_strategy_code():
#     global df
#     while True:
#         ct = dt.now(time_zone)
#         # Logging Bid/Ask Spred 
#         # log_bid_ask_spread()

#         # Close program 2 min after end time
#         if ct > end_time + dt.duration(minutes=2):
#             logging.info('closing program')
#             return  # end coroutine

#         # Every 5 seconds: chase orders and broker PnL
#         if ct.second % 5 == 0:
#             try:
#                 order_response = await fyers_asysc.orderbook()
#                 order_df = pd.DataFrame(order_response['orderBook']) if order_response.get('orderBook') else pd.DataFrame()
#                 logging.info(f"{CYAN}[CHASE] Checking pending orders...{RESET}")
#                 chase_order(order_df)

#                 pos1 = await fyers_asysc.positions()
#                 pnl = int(pos1.get('overall', {}).get('pl_total', 0))
#                 logging.info(f"{GRAY}Live PnL from broker: {pnl}{RESET}")

#                 # ✅ Await monitor_positions since it's async
#                 await monitor_positions()

#             except Exception as e:
#                 logging.error(f"Unable to fetch pnl or chase order: {e}")

#         # Run strategy
#         if account_type == 'PAPER':
#             paper_order()
#         else:
#             real_order()

#         await asyncio.sleep(1)

# def run():
#     # ✅ Start auto-refresh loop for option chain every 10s
#     # refresh_option_chain()

#     # Connect both sockets: market data + order status
#     fyers_socket.connect()
#     fyers_order_socket.connect()
#     time.sleep(2)

#     try:
#         asyncio.run(main_strategy_code())
#     except KeyboardInterrupt:
#         logging.info("Manual interrupt received, shutting down.")
#     finally:
#         logging.info("Program terminated.")

# if __name__ == "__main__":
#     run()


# ===== main.py =====
import asyncio
import time
import logging
import pandas as pd
import pendulum as dt
import warnings
import datetime

from config import account_type, time_zone, index_name
from setup import fyers_asysc, df, end_time, hist_data
from execution import paper_order, real_order
from data_feed import fyers_socket, fyers_order_socket, chase_order
from tickdb import TickDatabase
from indicators import (
    build_15m_candles,
    check_bias,
    calculate_cpr,
    calculate_traditional_pivots,
    calculate_camarilla_pivots,
    resolve_atr,
)
from signals import detect_signal

warnings.filterwarnings("ignore", category=UserWarning, module="pkg_resources")


async def main_strategy_code():
    db = TickDatabase()
    symbol = index_name

    # ✅ Step 1: Load yesterday’s ticks from SQLite
    yesterday = datetime.date.today() - datetime.timedelta(days=1)
    ticks_df = pd.read_sql_query(
        "SELECT * FROM ticks WHERE symbol=?", db.conn, params=[symbol]
    )

    # ✅ Step 2: Build 15m candles from historical ticks
    hist_yesterday_15m = build_15m_candles(ticks_df, target_date=yesterday)

    # ✅ Step 3: Seed bias before session starts
    if not hist_yesterday_15m.empty:
        bias = check_bias(hist_yesterday_15m)
        logging.info(f"[PRE-SESSION BIAS] {bias}")

        # Compute pivots from yesterday’s last daily candle
        prev_day = hist_data.iloc[-1]
        cpr = calculate_cpr(prev_day["high"], prev_day["low"], prev_day["close"])
        trad = calculate_traditional_pivots(
            prev_day["high"], prev_day["low"], prev_day["close"]
        )
        cam = calculate_camarilla_pivots(
            prev_day["high"], prev_day["low"], prev_day["close"]
        )
        atr, _ = resolve_atr(hist_yesterday_15m, None)

        # Detect signal immediately using seeded bias + pivots
        signal = detect_signal(cpr, trad, cam, atr, df, hist_yesterday_15m=hist_yesterday_15m)
        if signal:
            side, reason = signal
            logging.info(f"[PRE-SESSION SIGNAL] {side} ({reason})")
            if account_type == "PAPER":
                paper_order()
            else:
                real_order()

    # ✅ Step 4: Enter live async loop
    # Track last bias refresh time
    last_bias_refresh = None

    while True:
        ct = dt.now(time_zone)

        # End-of-day shutdown
        if ct > end_time + dt.duration(minutes=2):
            logging.info("closing program")
            db.close()
            return

        # Intraday bias refresh every 60 minutes
        if last_bias_refresh is None or (ct - last_bias_refresh).total_seconds() >= 3600:
            hist_today_15m = build_15m_candles(df, target_date=datetime.date.today())
            full_hist = pd.concat([hist_yesterday_15m, hist_today_15m]).reset_index(drop=True)
            bias = check_bias(full_hist)
            logging.info(f"[INTRADAY BIAS REFRESH] bias={bias} candles={len(full_hist)} at {ct}")
            last_bias_refresh = ct

        # Order chasing every 5 seconds
        if ct.second % 5 == 0:
            chase_order(df)

        # Execute paper or live orders
        if account_type == "PAPER":
            paper_order(hist_yesterday_15m)
        else:
            real_order(hist_yesterday_15m)

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