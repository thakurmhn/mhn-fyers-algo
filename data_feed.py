import pandas as pd
import logging
from config import ticker, client_id
from fyers_apiv3.FyersWebsocket import data_ws
from setup import access_token, symbols, spot_price
from indicators import build_3min_candle, current_3m_start
from datetime import datetime as dt
import pytz

time_zone = pytz.timezone("Asia/Kolkata")

# ===== Tick snapshot DataFrame (indexed by symbol) =====
df = pd.DataFrame(
    columns=[
        "symbol","ltp","ch","chp","avg_trade_price","open_price","high_price","low_price",
        "prev_close_price","vol_traded_today","oi","pdoi","oipercent","bid_price","ask_price",
        "last_traded_time","exch_feed_time","bid_size","ask_size","last_traded_qty",
        "tot_buy_qty","tot_sell_qty","lower_ckt","upper_ckt","type","expiry"
    ]
)

# ===== Lightweight tick DataFrame for candle building =====
df_ticks = pd.DataFrame(columns=["timestamp", "price"])

def onmessage(message):
    global df, df_ticks, spot_price, current_3m_start

    if not message.get("symbol"):
        return

    symbol = message["symbol"]

    # --- Update full tick snapshot ---
    if symbol not in df.index:
        df.loc[symbol] = [None] * len(df.columns)

    for key, value in message.items():
        if key in df.columns:
            df.loc[symbol, key] = value

    # --- Update spot price + candle builder ---
    if symbol == ticker and "ltp" in message:
        spot_price = message["ltp"]

        # Append tick to df_ticks (modern pandas)
        df_ticks.loc[len(df_ticks)] = [dt.now(time_zone), spot_price]

        # Build candle with just the price
        build_3min_candle(spot_price)


def onerror(message):
    logging.error(f"Socket error: {message}")


def onclose(message):
    logging.info(f"Connection closed: {message}")


def onopen():
    data_type = "SymbolUpdate"
    fyers_socket.subscribe(symbols=symbols, data_type=data_type)
    fyers_socket.keep_running()
    print('starting socket')


fyers_socket = data_ws.FyersDataSocket(
    access_token=f"{client_id}:{access_token}",
    log_path=None,
    litemode=False,
    write_to_file=False,
    reconnect=True,
    on_connect=onopen,
    on_close=onclose,
    on_error=onerror,
    on_message=onmessage
)


def chase_order(ord_df):
    if not ord_df.empty:
        ord_df = ord_df[ord_df['status'] == 6]
        for _, o1 in ord_df.iterrows():
            name = o1['symbol']
            current_price = df.loc[name, 'ltp'] if name in df.index else None
            if current_price is None or pd.isna(current_price):
                logging.warning(f"No LTP for {name}, skipping chase")
                continue
            try:
                if o1['type'] == 1:  # Limit order
                    id1 = o1['id']
                    lmt_price = o1['limitPrice']
                    qty = o1['qty']
                    new_lmt_price = round(lmt_price + 0.1, 2) if current_price > lmt_price else round(lmt_price - 0.1, 2)
                    logging.info(f"Chasing order {name}: old={lmt_price}, new={new_lmt_price}, qty={qty}")
                    data = {"id": id1, "type": 1, "limitPrice": new_lmt_price, "qty": qty}
                    response = fyers.modify_order(data=data)
                    logging.info(response)
            except Exception as e:
                logging.error(f"Error in chasing order: {e}")
