# # ===== main.py =====
# import asyncio
# import time
# import logging
# import pandas as pd
# import pendulum as dt
# import warnings

# from config import account_type, time_zone, start_time, end_time
# from execution import paper_order, real_order
# from data_feed import fyers_socket, fyers_order_socket, chase_order, fyers_asysc
# # from monitor import monitor_positions   # optional, uncomment if you want to use it

# warnings.filterwarnings("ignore", category=UserWarning, module="pkg_resources")


# # ANSI COLORS
# RESET   = "\033[0m"
# GRAY    = "\033[90m"
# CYAN    = "\033[96m"

# async def main_strategy_code():
#     global df
#     while True:
#         ct = dt.now(time_zone)

#         # Close program 2 min after end time
#         if ct > end_time.add(minutes=2):
#             logging.info("Closing program after session end.")
#             return  # end coroutine

#         # Every 5 seconds: chase orders and broker PnL
#         if ct.second % 5 == 0:
#             try:
#                 order_response = await fyers_asysc.orderbook()
#                 order_df = pd.DataFrame(order_response["orderBook"]) if order_response.get("orderBook") else pd.DataFrame()
#                 logging.info(f"{CYAN}[CHASE] Checking pending orders...{RESET}")
#                 chase_order(order_df)

#                 pos1 = await fyers_asysc.positions()
#                 pnl = int(pos1.get("overall", {}).get("pl_total", 0))
#                 logging.info(f"{GRAY}Live PnL from broker: {pnl}{RESET}")

#             except Exception as e:
#                 logging.error(f"Unable to fetch PnL or chase order: {e}")

#         # Run strategy loop
#         if account_type.upper() == "PAPER":
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

from config import time_zone, end_time
from execution import run_strategy   # ✅ unified strategy runner
from data_feed import fyers_socket, fyers_order_socket, chase_order, fyers_asysc
from setup import df, hist_data      # ✅ your data sources
from indicators import (
    calculate_cpr,
    calculate_traditional_pivots,
    calculate_camarilla_pivots,
    resolve_atr,
    daily_atr
)

warnings.filterwarnings("ignore", category=UserWarning, module="pkg_resources")

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

# ANSI COLORS
RESET   = "\033[0m"
GRAY    = "\033[90m"
CYAN    = "\033[96m"

def print_daily_levels():
    prev_day = hist_data.iloc[-1]
    prev_high, prev_low, prev_close = float(prev_day['high']), float(prev_day['low']), float(prev_day['close'])

    # Pivot levels
    cpr_levels_base = calculate_cpr(prev_high, prev_low, prev_close)
    traditional_levels_base = calculate_traditional_pivots(prev_high, prev_low, prev_close)
    camarilla_levels_base = calculate_camarilla_pivots(prev_high, prev_low, prev_close)

    # Daily ATR regime
    daily_atr_value = daily_atr(hist_data)   # compute daily ATR
    atr_value, atr_source = resolve_atr(pd.DataFrame(), daily_atr_value)  # pass into resolver

    atr_display = f"{atr_value:.2f}" if atr_value is not None else "0.00"
    atr_regime = "HIGH" if atr_value and atr_value > 120 else "LOW"

    logging.info(
    f"CPR: Pivot={cpr_levels_base['pivot']}, TC={cpr_levels_base['tc']}, BC={cpr_levels_base['bc']} | "
    f"Traditional: Pivot={traditional_levels_base['pivot']}, R1={traditional_levels_base['r1']}, S1={traditional_levels_base['s1']} | "
    f"Camarilla: R3={camarilla_levels_base['r3']}, R4={camarilla_levels_base['r4']}, "
    f"S3={camarilla_levels_base['s3']}, S4={camarilla_levels_base['s4']} | "
    f"ATR={atr_display} ({atr_source}, {atr_regime})"
)


async def main_strategy_code():
    while True:
        ct = dt.now(time_zone)

        # Close program 2 min after end time
        if ct > end_time.add(minutes=2):
            logging.info("Closing program after session end.")
            return  # end coroutine

        # Every 5 seconds: chase orders and broker PnL
        if ct.second % 5 == 0:
            try:
                order_response = await fyers_asysc.orderbook()
                order_df = pd.DataFrame(order_response["orderBook"]) if order_response.get("orderBook") else pd.DataFrame()
                logging.info(f"{CYAN}[CHASE] Checking pending orders...{RESET}")
                chase_order(order_df)

                pos1 = await fyers_asysc.positions()
                pnl = int(pos1.get("overall", {}).get("pl_total", 0))
                logging.info(f"{GRAY}Live PnL from broker: {pnl}{RESET}")

            except Exception as e:
                logging.error(f"Unable to fetch PnL or chase order: {e}")

        # ✅ Run unified strategy loop (handles paper/live internally)
        try:
            run_strategy(df, hist_data)
        except Exception as e:
            logging.error(f"[STRATEGY ERROR] {e}", exc_info=True)

        await asyncio.sleep(1)

def run():
    # Connect both sockets: market data + order status
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
    print_daily_levels()   # ✅ print pivots + ATR regime at startup
    run()