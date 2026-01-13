# ===== main.py =====
import asyncio
import time
import logging
import pandas as pd
import sys
import threading

from config import account_type, time_zone
from setup import fyers_asysc, df, end_time
from execution import paper_order, real_order
from data_feed import (
    start_data_socket,
    stop_data_socket,
    start_order_socket,
    stop_order_socket,
    chase_order,
)
from monitor import monitor_positions

import pendulum as dt
import warnings
warnings.filterwarnings("ignore", category=UserWarning, module="pkg_resources")

# ANSI COLORS
RESET   = "\033[0m"
GRAY    = "\033[90m"
CYAN    = "\033[96m"

# ===== Socket runner threads =====
_data_thread = None
_order_thread = None
_running = False

def _run_data_socket():
    """
    Run data socket in a dedicated thread (connect + keep_running).
    """
    try:
        start_data_socket()
    except Exception as e:
        logging.error(f"[DATA WS THREAD ERROR] {e}")

def _run_order_socket():
    """
    Run order socket in a dedicated thread (connect + keep_running).
    """
    try:
        start_order_socket()
    except Exception as e:
        logging.error(f"[ORDER WS THREAD ERROR] {e}")

def start_sockets():
    """
    Start both sockets in background threads.
    """
    global _data_thread, _order_thread, _running
    if _running:
        return
    _running = True

    _data_thread = threading.Thread(target=_run_data_socket, name="FyersDataSocketThread", daemon=True)
    _order_thread = threading.Thread(target=_run_order_socket, name="FyersOrderSocketThread", daemon=True)

    _data_thread.start()
    _order_thread.start()

    # Give sockets a moment to connect
    time.sleep(2)
    logging.info("[SOCKETS] Data & Order sockets started.")

def stop_sockets():
    """
    Gracefully stop both sockets using v3-safe methods.
    """
    global _running
    try:
        stop_data_socket()
        stop_order_socket()
        logging.info("[SOCKETS] Stop requested.")
    except Exception as e:
        logging.warning(f"[SOCKETS] Error during shutdown: {e}")
    finally:
        _running = False

async def main_strategy_code():
    global df
    while True:
        ct = dt.now(time_zone)

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
        try:
            if account_type == 'PAPER':
                paper_order()
            else:
                real_order()
        except Exception as e:
            logging.error(f"[STRATEGY ERROR] {e}")

        await asyncio.sleep(1)

def run():
    # Start both sockets in background threads
    start_sockets()

    try:
        asyncio.run(main_strategy_code())
    except KeyboardInterrupt:
        logging.info("Manual interrupt received, shutting down.")
    finally:
        # ✅ Graceful stop for v3 sockets
        stop_sockets()

        logging.info("Program terminated.")
        sys.exit(0)  # ✅ Force exit to kill any lingering threads

if __name__ == "__main__":
    run()