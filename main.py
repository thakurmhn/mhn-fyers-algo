# ===== main.py =====
import asyncio
import time
import logging
import pandas as pd
import pendulum as dt
import warnings

from config import account_type, time_zone
from setup import fyers_asysc, df, end_time, refresh_option_chain, log_bid_ask_spread   # ✅ import helper
from execution import paper_order, real_order
from data_feed import fyers_socket, fyers_order_socket, chase_order
from monitor import monitor_positions

warnings.filterwarnings("ignore", category=UserWarning, module="pkg_resources")

# ANSI COLORS
RESET   = "\033[0m"
GRAY    = "\033[90m"
CYAN    = "\033[96m"

async def main_strategy_code():
    global df
    while True:
        ct = dt.now(time_zone)
        # Logging Bid/Ask Spred 
        # log_bid_ask_spread()

        # Close program 2 min after end time
        if ct > end_time + dt.duration(minutes=2):
            logging.info('closing program')
            return  # end coroutine

        # Every 5 seconds: chase orders and broker PnL
        if ct.second % 5 == 0:
            try:
                order_response = await fyers_asysc.orderbook()
                order_df = pd.DataFrame(order_response['orderBook']) if order_response.get('orderBook') else pd.DataFrame()
                logging.info(f"{CYAN}[CHASE] Checking pending orders...{RESET}")
                chase_order(order_df)

                pos1 = await fyers_asysc.positions()
                pnl = int(pos1.get('overall', {}).get('pl_total', 0))
                logging.info(f"{GRAY}Live PnL from broker: {pnl}{RESET}")

                # ✅ Await monitor_positions since it's async
                await monitor_positions()

            except Exception as e:
                logging.error(f"Unable to fetch pnl or chase order: {e}")

        # Run strategy
        if account_type == 'PAPER':
            paper_order()
        else:
            real_order()

        await asyncio.sleep(1)

def run():
    # ✅ Start auto-refresh loop for option chain every 10s
    # refresh_option_chain()

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
    run()