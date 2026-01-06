# ===== data_feed.py =====
import logging
import pandas as pd

from fyers_apiv3.FyersWebsocket import data_ws

from setup import client_id, access_token, fyers, ticker, symbols, df
from setup import spot_price as _spot_price  # local name; we update inplace
from indicators import build_3min_candle

# Keep a module-level spot reference
spot_price = _spot_price

def onmessage(ticks):
    global df, spot_price

    if not ticks.get('symbol'):
        return

    symbol = ticks['symbol']

    if symbol not in df.index:
        df.loc[symbol] = [None] * len(df.columns)

    for key, value in ticks.items():
        if key in df.columns:
            df.loc[symbol, key] = value

    # Build 3m candle ONLY from underlying
    if symbol == ticker and 'ltp' in ticks:
        spot_price = ticks['ltp']
        build_3min_candle(spot_price)

def onerror(message):
    logging.error(f"Socket error: {message}")

def onclose(message):
    logging.info(f"Connection closed: {message}")

def onopen():
    # Subscribe to option symbols (you can also subscribe to underlying ticker if available)
    data_type = "SymbolUpdate"
    fyers_socket.subscribe(symbols=symbols, data_type=data_type)
    fyers_socket.keep_running()
    print('starting socket')

# ===== Data socket =====
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

# ===== Order chasing =====
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