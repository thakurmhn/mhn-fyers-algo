import asyncio, logging, time, sys
import pandas as pd
import pendulum as dt
from datetime import timedelta
from config import account_type, end_time, time_zone
from execution import paper_order, real_order
from data_feed import fyers_socket, chase_order, df
from setup import fyers_asysc

async def main_strategy_code():
    global df
    while True:
        ct = dt.now(time_zone)

        if ct > end_time + timedelta(minutes=2):
            logging.info('closing program')
            return

        if ct.second % 5 == 0:
            try:
                order_response = await fyers_asysc.orderbook()
                order_df = pd.DataFrame(order_response['orderBook']) if order_response.get('orderBook') else pd.DataFrame()
                chase_order(order_df)

                pos1 = await fyers_asysc.positions()
                pnl = int(pos1.get('overall', {}).get('pl_total', 0))
                logging.info(f"Live PnL from broker: {pnl}")
            except Exception as e:
                logging.error(f"Unable to fetch pnl or chase order: {e}")

        if account_type == 'PAPER':
            paper_order()
        else:
            real_order()

        await asyncio.sleep(1)

def run():
    fyers_socket.connect()
    time.sleep(2)
    try:
        asyncio.run(main_strategy_code())
    except KeyboardInterrupt:
        logging.info("Manual interrupt received, shutting down.")
    finally:
        logging.info("Program terminated.")
        sys.exit(0)

if __name__ == "__main__":
    run()

