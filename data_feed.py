# ===== data_feed.py =====
import logging
import pandas as pd

from fyers_apiv3.FyersWebsocket import data_ws, order_ws
from setup import client_id, access_token, fyers, fyers_asysc, ticker, symbols, df
from setup import spot_price as _spot_price
from candle_builder import build_3min_candle
from execution import update_order_status, map_status_code
from tickdb import TickDatabase   # dedicated DB helper


# ANSI COLORS
RESET   = "\033[0m"
GRAY    = "\033[90m"
CYAN    = "\033[96m"

# Keep a module-level spot reference
spot_price = _spot_price

# Create a global TickDatabase instance
tick_db = TickDatabase()

# ===== Market data callbacks =====
def onmessage(ticks):
    global df, spot_price

    if not ticks.get("symbol"):
        return

    symbol = ticks["symbol"]

    # ===== Option contracts → update df only =====
    if symbol not in ["NSE:NIFTY50-INDEX"]:            # ["NSE:NIFTY50-INDEX", "NSE:BANKNIFTY-INDEX", "NSE:FINNIFTY-INDEX"]
        # keep option LTPs in df for chasing orders
        if symbol not in df.index:
            df.loc[symbol] = [None] * len(df.columns)
        for key, value in ticks.items():
            if key in df.columns:
                df.at[symbol, key] = value
        return

    # ===== Underlying indices → persistence + candle building =====
    ltp = ticks.get("ltp") or ticks.get("last_traded_price")
    bid = ticks.get("bid") or ticks.get("bid_price")
    ask = ticks.get("ask") or ticks.get("ask_price")
    vol = ticks.get("vol") or ticks.get("last_traded_qty", 0)

    if ltp is not None:
        try:
            tick_db.insert_tick(symbol, bid, ask, ltp, vol)
            logging.info(f"{GRAY}[TICK SAVED] {symbol} LTP={ltp} VOL={vol}{RESET}")
        except Exception as e:
            logging.error(f"[DB ERROR] Failed to insert tick: {e}")

        spot_price = ltp
        try:
            df_ticks = tick_db.fetch_ticks(symbol)
            build_3min_candle(df_ticks, tick_db, symbol)
        except Exception as e:
            logging.error(f"[CANDLE ERROR] Failed to build candle for {symbol}: {e}")

def onerror(message): logging.error(f"[SOCKET ERROR] {message}")
def onclose(message): logging.info(f"[SOCKET CLOSED] {message}")

def onopen():
    fyers_socket.subscribe(symbols=symbols, data_type="SymbolUpdate")
    fyers_socket.keep_running()
    logging.info("[SOCKET OPEN] Market data subscription started")

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

# ===== Order chasing =====
def chase_order(ord_df):
    if not ord_df.empty:
        ord_df = ord_df[ord_df["status"] == 6]  # pending orders
        for _, o1 in ord_df.iterrows():
            name = o1["symbol"]
            current_price = df.loc[name, "ltp"] if name in df.index else None
            if current_price is None or pd.isna(current_price):
                logging.warning(f"[CHASE] No LTP for {name}, skipping")
                continue
            try:
                if o1["type"] == 1:  # Limit order
                    id1 = o1["id"]
                    lmt_price = o1["limitPrice"]
                    qty = o1["qty"]
                    new_lmt_price = round(lmt_price + 0.1, 2) if current_price > lmt_price else round(lmt_price - 0.1, 2)
                    logging.info(f"[CHASE] {name}: old={lmt_price}, new={new_lmt_price}, qty={qty}")
                    data = {"id": id1, "type": 1, "limitPrice": new_lmt_price, "qty": qty}
                    response = fyers.modify_order(data=data)
                    logging.info(f"[CHASE RESPONSE] {response}")
            except Exception as e:
                logging.error(f"[CHASE ERROR] {e}")

# ===== Order status callbacks =====
def on_orders(message):
    logging.info(f"[ORDER UPDATE RAW] {message}")
    try:
        orders = message.get("orders", {})
        order_id = orders.get("id")
        status_code = orders.get("status")
        filled_qty = orders.get("filledQty", 0)
        traded_price = orders.get("tradedPrice", 0)
        symbol = orders.get("symbol")

        status = map_status_code(status_code)
        update_order_status(order_id, status, filled_qty, traded_price, symbol)

    except Exception as e:
        logging.error(f"[ORDER UPDATE ERROR] {e}")

def on_order_error(message): logging.error(f"[ORDER WS ERROR] {message}")
def on_order_close(message): logging.info(f"[ORDER WS CLOSED] {message}")

def on_order_open():
    logging.info("[ORDER WS CONNECTED] Subscribing to OnOrders...")
    fyers_order_socket.subscribe(data_type="OnOrders")
    fyers_order_socket.keep_running()

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