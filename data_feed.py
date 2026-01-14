# # ===== data_feed.py =====

# import logging
# import pandas as pd

# from fyers_apiv3.FyersWebsocket import data_ws, order_ws

# from setup import client_id, access_token, fyers, ticker, symbols, df
# from setup import spot_price as _spot_price  # local name; we update inplace
# from indicators import build_3min_candle
# from execution import update_order_status, map_status_code   # unified ledger + status mapping

# # Keep a module-level spot reference
# spot_price = _spot_price

# # ===== Market data callbacks =====
# def onmessage(ticks):
#     global df, spot_price

#     if not ticks.get('symbol'):
#         return

#     symbol = ticks['symbol']

#     if symbol not in df.index:
#         df.loc[symbol] = [None] * len(df.columns)

#     for key, value in ticks.items():
#         if key in df.columns:
#             df.loc[symbol, key] = value

#     # Build 3m candle ONLY from underlying
#     if symbol == ticker and 'ltp' in ticks:
#         spot_price = ticks['ltp']
#         build_3min_candle(spot_price)

# def onerror(message):
#     logging.error(f"[DATA WS ERROR] {message}")

# def onclose(message):
#     logging.info(f"[DATA WS CLOSED] {message}")

# def onopen():
#     logging.info("[DATA WS CONNECTED] Subscribing to symbols...")
#     fyers_socket.subscribe(symbols=symbols, data_type="SymbolUpdate")

# # ===== Data socket =====
# fyers_socket = data_ws.FyersDataSocket(
#     access_token=f"{client_id}:{access_token}",
#     log_path=None,
#     litemode=False,
#     write_to_file=False,
#     reconnect=True,
#     on_connect=onopen,
#     on_close=onclose,
#     on_error=onerror,
#     on_message=onmessage
# )

# # Safe start
# def start_data_socket():
#     fyers_socket.connect()
#     fyers_socket.keep_running()

# def stop_data_socket():
#     try:
#         if hasattr(fyers_socket, "stop_running"):
#             fyers_socket.stop_running()
#         elif hasattr(fyers_socket, "close"):
#             fyers_socket.close()
#     except Exception as e:
#         logging.warning(f"[DATA WS SHUTDOWN ERROR] {e}")

# # ===== Order chasing =====
# def chase_order(ord_df):
#     if not ord_df.empty:
#         ord_df = ord_df[ord_df['status'] == 6]  # pending orders
#         for _, o1 in ord_df.iterrows():
#             name = o1['symbol']
#             current_price = df.loc[name, 'ltp'] if name in df.index else None
#             if current_price is None or pd.isna(current_price):
#                 logging.warning(f"No LTP for {name}, skipping chase")
#                 continue
#             try:
#                 if o1['type'] == 1:  # Limit order
#                     id1 = o1['id']
#                     lmt_price = o1['limitPrice']
#                     qty = o1['qty']
#                     new_lmt_price = round(lmt_price + 0.1, 2) if current_price > lmt_price else round(lmt_price - 0.1, 2)
#                     logging.info(f"Chasing order {name}: old={lmt_price}, new={new_lmt_price}, qty={qty}")
#                     data = {"id": id1, "type": 1, "limitPrice": new_lmt_price, "qty": qty}
#                     response = fyers.modify_order(data=data)
#                     logging.info(response)
#             except Exception as e:
#                 logging.error(f"Error in chasing order: {e}")

# # ===== Order status callbacks =====
# def on_orders(message):
#     logging.info(f"[ORDER UPDATE RAW] {message}")
#     try:
#         orders = message.get("orders", {})
#         order_id = orders.get("id")
#         status_code = orders.get("status")
#         filled_qty = orders.get("filledQty", 0)
#         traded_price = orders.get("tradedPrice", 0)   # <-- use tradedPrice
#         symbol = orders.get("symbol")

#         status = map_status_code(status_code)
#         update_order_status(order_id, status, filled_qty, traded_price, symbol)

#     except Exception as e:
#         logging.error(f"[ORDER UPDATE ERROR] {e}")

# def on_order_error(message):
#     logging.error(f"[ORDER WS ERROR] {message}")

# def on_order_close(message):
#     logging.info(f"[ORDER WS CLOSED] {message}")

# def on_order_open():
#     logging.info("[ORDER WS CONNECTED] Subscribing to OnOrders...")
#     fyers_order_socket.subscribe(data_type="OnOrders")

# # ===== Order socket =====
# fyers_order_socket = order_ws.FyersOrderSocket(
#     access_token=f"{client_id}:{access_token}",
#     write_to_file=False,
#     log_path="",
#     on_connect=on_order_open,
#     on_close=on_order_close,
#     on_error=on_order_error,
#     on_orders=on_orders,
# )

# def start_order_socket():
#     fyers_order_socket.connect()
#     fyers_order_socket.keep_running()

# def stop_order_socket():
#     try:
#         if hasattr(fyers_order_socket, "stop_running"):
#             fyers_order_socket.stop_running()
#         elif hasattr(fyers_order_socket, "close"):
#             fyers_order_socket.close()
#     except Exception as e:
#         logging.warning(f"[ORDER WS SHUTDOWN ERROR] {e}")

# =================================================================

# ===== data_feed.py =====
import logging
import pandas as pd

from fyers_apiv3.FyersWebsocket import data_ws, order_ws

from setup import client_id, access_token, fyers, ticker, symbols, df
from setup import spot_price as _spot_price  # local name; we update inplace
from indicators import build_3min_candle
from execution import update_order_status, map_status_code, apply_signal_to_trade, process_order
from signals import detect_signal
from config import quantity, time_zone


# Keep a module-level spot reference
spot_price = _spot_price

# Global trade info dict (initialize legs)
info = {
    "call_buy": {"trade_flag": 0, "pnl": 0.0, "filled_df": pd.DataFrame()},
    "put_buy": {"trade_flag": 0, "pnl": 0.0, "filled_df": pd.DataFrame()},
    "total_pnl": 0.0,
    "last_signal_bar_index": None,
}

time_zone = "Asia/Kolkata"  # adjust if needed

# ===== Market data callbacks =====
# from signal import detect_signal
# from execution import apply_signal_to_trade, process_order
# from config import lot_size, time_zone

def onmessage(ticks):
    global df, spot_price, info

    if not ticks.get("symbol"):
        return

    symbol = ticks["symbol"]

    if symbol not in df.index:
        df.loc[symbol] = [None] * len(df.columns)

    for key, value in ticks.items():
        if key in df.columns:
            df.loc[symbol, key] = value

    # Build 3m candle ONLY from underlying
    if symbol == ticker and "ltp" in ticks:
        spot_price = ticks["ltp"]

        try:
            result = build_3min_candle(spot_price)

            # 👉 Guard: only unpack if a candle actually closed
            if result is not None:
                candles_3m, cpr_levels, traditional_levels, camarilla_levels, atr = result

                # --- Trading pipeline ---
                signal = detect_signal(
                    cpr_levels, traditional_levels, camarilla_levels, atr, candles_3m,
                    in_position=(info["call_buy"]["trade_flag"]==1 or info["put_buy"]["trade_flag"]==1),
                    cooldown_bars=1,
                    last_signal_bar_index=info.get("last_signal_bar_index")
                )

                if signal:
                    side, payload = signal
                    option_name = f"{ticker}{'CE' if side=='CALL' else 'PE'}"
                    qty = quantity
                    apply_signal_to_trade(side, symbol, payload, info,
                                          option_name=option_name,
                                          quantity=qty,
                                          buy_price=spot_price,
                                          time_zone=time_zone)
                   
                    # Just mark trade ready; main.py will execute
                    info[side.lower() + "_buy"]["trade_flag"] = 1
                    info[side.lower() + "_buy"]["option_name"] = option_name
                    info[side.lower() + "_buy"]["quantity"] = qty
                    info[side.lower() + "_buy"]["buy_price"] = spot_price

                # Process existing trades
                for side in ["CALL", "PUT"]:
                    option_name = f"{ticker}{'CE' if side=='CALL' else 'PE'}"
                    option_price = df.loc[option_name, "ltp"] if option_name in df.index else None
                    if option_price:
                        process_order(side, symbol, option_price, info, candles_3m,
                                      spot_price=spot_price, time_zone=time_zone)

        except Exception as e:
            logging.error(f"[CANDLE/TRADING ERROR] {e}")


def onerror(message):
    logging.error(f"[DATA WS ERROR] {message}")

def onclose(message):
    logging.info(f"[DATA WS CLOSED] {message}")

def onopen():
    logging.info("[DATA WS CONNECTED] Subscribing to symbols...")
    try:
        fyers_socket.subscribe(symbols=symbols, data_type="SymbolUpdate")
    except Exception as e:
        logging.error(f"[DATA WS SUBSCRIBE ERROR] {e}")

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
    on_message=onmessage,
)

def start_data_socket():
    try:
        fyers_socket.connect()
        fyers_socket.keep_running()
    except Exception as e:
        logging.error(f"[DATA SOCKET START ERROR] {e}")

def stop_data_socket():
    try:
        if hasattr(fyers_socket, "stop_running"):
            fyers_socket.stop_running()
        elif hasattr(fyers_socket, "close"):
            fyers_socket.close()
    except Exception as e:
        logging.warning(f"[DATA WS SHUTDOWN ERROR] {e}")

# ===== Order chasing =====
def chase_order(ord_df):
    if ord_df.empty:
        return

    pending = ord_df[ord_df["status"] == 6]  # pending orders
    for _, o1 in pending.iterrows():
        name = o1["symbol"]
        current_price = df.loc[name, "ltp"] if name in df.index else None
        if current_price is None or pd.isna(current_price):
            logging.warning(f"No LTP for {name}, skipping chase")
            continue
        try:
            if o1["type"] == 1:  # Limit order
                id1 = o1["id"]
                lmt_price = o1["limitPrice"]
                qty = o1["qty"]
                new_lmt_price = round(lmt_price + 0.1, 2) if current_price > lmt_price else round(lmt_price - 0.1, 2)
                logging.info(f"Chasing order {name}: old={lmt_price}, new={new_lmt_price}, qty={qty}")
                data = {"id": id1, "type": 1, "limitPrice": new_lmt_price, "qty": qty}
                response = fyers.modify_order(data=data)
                logging.info(response)
        except Exception as e:
            logging.error(f"Error in chasing order: {e}")

# ===== Order status callbacks =====
def on_orders(message):
    logging.info(f"[ORDER UPDATE RAW] {message}")
    try:
        orders = message.get("orders", {})
        if isinstance(orders, dict):
            orders = [orders]
        for o in orders:
            order_id = o.get("id")
            status_code = o.get("status")
            filled_qty = o.get("filledQty", 0)
            traded_price = o.get("tradedPrice", 0)
            symbol = o.get("symbol")

            status = map_status_code(status_code)
            update_order_status(order_id, status, filled_qty, traded_price, symbol)
    except Exception as e:
        logging.error(f"[ORDER UPDATE ERROR] {e}")

def on_order_error(message):
    logging.error(f"[ORDER WS ERROR] {message}")

def on_order_close(message):
    logging.info(f"[ORDER WS CLOSED] {message}")

def on_order_open():
    logging.info("[ORDER WS CONNECTED] Subscribing to OnOrders...")
    try:
        fyers_order_socket.subscribe(data_type="OnOrders")
    except Exception as e:
        logging.error(f"[ORDER WS SUBSCRIBE ERROR] {e}")

# ===== Order socket =====
fyers_order_socket = order_ws.FyersOrderSocket(
    access_token=f"{client_id}:{access_token}",
    write_to_file=False,
    log_path="",
    on_connect=on_order_open,
    on_close=on_order_close,
    on_error=on_order_error,
    on_orders=on_orders,
)

def start_order_socket():
    try:
        fyers_order_socket.connect()
        fyers_order_socket.keep_running()
    except Exception as e:
        logging.error(f"[ORDER SOCKET START ERROR] {e}")

def stop_order_socket():
    try:
        if hasattr(fyers_order_socket, "stop_running"):
            fyers_order_socket.stop_running()
        elif hasattr(fyers_order_socket, "close"):
            fyers_order_socket.close()
    except Exception as e:
        logging.warning(f"[ORDER WS SHUTDOWN ERROR] {e}")