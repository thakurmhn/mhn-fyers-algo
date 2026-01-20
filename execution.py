# ===== execution.py =====
import logging
import pickle
import pandas as pd
import pendulum as dt
from fyers_apiv3 import fyersModel

from config import (
    time_zone, strategy_name, MAX_TRADES_PER_DAY, account_type, quantity,
    CALL_MONEYNESS, PUT_MONEYNESS, profit_loss_point,
    ATR_STOP_MULT, ATR_TGT_MULT, TRAIL_TRIGGER, TRAIL_STEP, ENTRY_OFFSET, ORDER_TYPE, ALLOW_SHORTS
)
from setup import (
    df, fyers, fyers_asysc, ticker, option_chain, df, spot_price,
    start_time, end_time, hist_data, time_zone )
from indicators import (
    calculate_cpr, calculate_traditional_pivots, calculate_camarilla_pivots,
    resolve_atr, daily_atr, candles_3m, get_dynamic_target
)
from signals import detect_signal

# ===========================================================
# ANSI COLORS for order logs
RESET   = "\033[0m"
GREEN   = "\033[92m"
YELLOW  = "\033[93m"
RED     = "\033[91m"
MAGENTA = "\033[95m"
GRAY    = "\033[90m"
CYAN    = "\033[96m"

#===========================================================
# Initalize filled_df
try:
    filled_df
except NameError:
    filled_df = pd.DataFrame(columns=["status", "filled_qty", "avg_price", "symbol"])


#===================================================================

def map_status_code(code):
    status_map = {
        1: "CANCELLED",
        2: "TRADED",
        4: "TRANSIT",
        5: "REJECTED",
        6: "PENDING",
        7: "EXPIRED"
    }
    return status_map.get(code, str(code))

def status_color(status):
    color_map = {"TRADED": GREEN, "PENDING": YELLOW, "CANCELLED": RED, "REJECTED": MAGENTA}
    return color_map.get(status, RESET)

# ===== Shared state =====
last_signal_candle_time = None

# ===== Persistence =====
def store(data, account_type_):
    try:
        pickle.dump(data, open(f'data-{dt.now(time_zone).date()}-{account_type_}.pickle', 'wb'))
    except Exception as e:
        logging.error(f"Failed to store state: {e}")

def load(account_type_):
    try:
        return pickle.load(open(f'data-{dt.now(time_zone).date()}-{account_type_}.pickle', 'rb'))
    except Exception as e:
        logging.warning(f"State load failed (fresh start): {e}")
        raise

# ===== Option selection by moneyness =====
def get_option_by_moneyness(spot_price_, side, moneyness='ITM', points=0):
    """
    Select ITM option strike with strike_diff points inside ATM.
    Jan 8th baseline: always ITM with 100-point difference.
    CALL: ATM - strike_diff
    PUT:  ATM + strike_diff
    """
    from config import strike_diff
    if spot_price_ is None or pd.isna(spot_price_):
        return None, None

    # Round to nearest strike
    atm_strike = round(spot_price_ / strike_diff) * strike_diff

    if side == 'CE':  # CALL
        strike = atm_strike - strike_diff
    else:             # PUT
        strike = atm_strike + strike_diff

    # Apply any manual offset
    strike += points

    sel = option_chain[
        (option_chain['strike_price'] == strike) &
        (option_chain['option_type'] == side)
    ]['symbol']

    if sel.empty:
        side_df = option_chain[option_chain['option_type'] == side].copy()
        if side_df.empty:
            logging.error(f"No options available for side={side}")
            return None, None
        side_df['strike_diff_abs'] = (side_df['strike_price'] - strike).abs()
        side_df = side_df.sort_values('strike_diff_abs')
        symbol = side_df.iloc[0]['symbol']
        strike = side_df.iloc[0]['strike_price']
        logging.warning(f"Fallback ITM for {side}: requested {strike}, using nearest available")
        return symbol, strike

    return sel.squeeze(), strike


# ===== Dynamic levels =====
# ===== Dynamic levels =====
def build_dynamic_levels(entry_price, side, atr, rr_ratio=2.0):
    """
    Build stop-loss, partial/full targets, and trailing parameters.
    Baseline logic (8th Jan):
    - Risk points = max(profit_loss_point, 0.25 * ATR)
    - Reward points = risk_points * rr_ratio
    - Both CALL and PUT options: targets above entry, stop below entry
    """
    risk_points = max(profit_loss_point, atr * 0.25)
    reward_points = risk_points * rr_ratio

    stop  = entry_price - risk_points
    partial_target = entry_price + reward_points / 2
    full_target    = entry_price + reward_points

    trail_start = reward_points / 2
    trail_step  = atr * 0.1

    return stop, full_target, partial_target, trail_start, trail_step


def update_trailing_stop(side, current_price, entry_price, current_stop, trail_start_pnl, trail_step_points):
    """
    Update trailing stop once partial target booked.
    Baseline logic (8th Jan):
    - CALL: move stop up as price rises
    - PUT: move stop down as price falls
    """
    if side == "CALL":
        pnl = current_price - entry_price
        if pnl >= trail_start_pnl:
            candidate = current_price - trail_step_points
            return max(current_stop, candidate)
        return current_stop

    elif side == "PUT":
        pnl = entry_price - current_price
        if pnl >= trail_start_pnl:
            candidate = current_price + trail_step_points
            return min(current_stop, candidate)
        return current_stop

    return current_stop

# ===== PAPER/LIVE STATE INIT =====
if account_type == 'PAPER':
    try:
        paper_info = load(account_type)
    except Exception:
        column_names = ['time', 'ticker', 'price', 'action', 'stop_price', 'take_profit', 'spot_price', 'quantity']
        filled_df = pd.DataFrame(columns=column_names)
        filled_df.set_index('time', inplace=True)

        from setup import option_chain
        from config import strike_diff

        def _init_otm(spot_price_, side, points=0):
            if spot_price_ is None:
                return None, None
            base_strike = round(spot_price_ / strike_diff) * strike_diff
            otm_strike = base_strike + points if side == 'CE' else base_strike - points
            sel = option_chain[
                (option_chain['strike_price'] == otm_strike) &
                (option_chain['option_type'] == side)
            ]['symbol']
            if sel.empty:
                side_df = option_chain[option_chain['option_type'] == side].copy()
                if side_df.empty:
                    logging.error(f"No options available for side={side} in option_chain")
                    return None, None
                side_df['strike_diff_abs'] = (side_df['strike_price'] - otm_strike).abs()
                side_df = side_df.sort_values('strike_diff_abs')
                symbol = side_df.iloc[0]['symbol']
                strike = side_df.iloc[0]['strike_price']
                logging.warning(f"Fallback OTM for {side}: requested {otm_strike}, using {strike}")
                return symbol, strike
            symbol = sel.squeeze()
            return symbol, otm_strike

        call_option, call_buy_strike = _init_otm(spot_price, 'CE', 0)
        put_option, put_buy_strike   = _init_otm(spot_price, 'PE', 0)
        logging.info('[PAPER INIT] started')

        paper_info = {
            'call_buy': {
                'option_name': call_option,
                'trade_flag': 0,
                'buy_price': 0,
                'current_stop_price': 0,
                'current_profit_price': 0,
                'target_method': "auto",
                'target_reached': False,
                'filled_df': filled_df.copy(),
                'underlying_price_level': 0,
                'quantity': quantity,
                'pnl': 0,
                'trade_count': 0,
                'trail_start_pnl': 0,
                'trail_step_points': 0
            },
            'put_buy': {
                'option_name': put_option,
                'trade_flag': 0,
                'buy_price': 0,
                'current_stop_price': 0,
                'current_profit_price': 0,
                'target_method': "auto",
                'target_reached': False,
                'filled_df': filled_df.copy(),
                'underlying_price_level': 0,
                'quantity': quantity,
                'pnl': 0,
                'trade_count': 0,
                'trail_start_pnl': 0,
                'trail_step_points': 0
            },
            'condition': False,
            'total_pnl': 0,
            'trade_count': 0,
            'max_trades': MAX_TRADES_PER_DAY
        }

else:
    try:
        live_info = load(account_type)
    except Exception:
        column_names = ['time', 'ticker', 'price', 'action', 'stop_price', 'take_profit', 'spot_price', 'quantity']
        filled_df = pd.DataFrame(columns=column_names)
        filled_df.set_index('time', inplace=True)

        from setup import option_chain
        from config import strike_diff

        def _init_otm(spot_price_, side, points=0):
            if spot_price_ is None:
                return None, None
            base_strike = round(spot_price_ / strike_diff) * strike_diff
            otm_strike = base_strike + points if side == 'CE' else base_strike - points
            sel = option_chain[
                (option_chain['strike_price'] == otm_strike) &
                (option_chain['option_type'] == side)
            ]['symbol']
            if sel.empty:
                side_df = option_chain[option_chain['option_type'] == side].copy()
                if side_df.empty:
                    logging.error(f"No options available for side={side} in option_chain")
                    return None, None
                side_df['strike_diff_abs'] = (side_df['strike_price'] - otm_strike).abs()
                side_df = side_df.sort_values('strike_diff_abs')
                symbol = side_df.iloc[0]['symbol']
                strike = side_df.iloc[0]['strike_price']
                logging.warning(f"Fallback OTM for {side}: requested {otm_strike}, using {strike}")
                return symbol, strike
            symbol = sel.squeeze()
            return symbol, otm_strike

        call_option, call_buy_strike = _init_otm(spot_price, 'CE', 0)
        put_option, put_buy_strike   = _init_otm(spot_price, 'PE', 0)
        logging.info('[LIVE INIT] started')

        live_info = {
            'call_buy': {
                'option_name': call_option,
                'trade_flag': 0,
                'buy_price': 0,
                'current_stop_price': 0,
                'current_profit_price': 0,
                'target_method': "auto",
                'target_reached': False,
                'filled_df': filled_df.copy(),
                'underlying_price_level': 0,
                'quantity': quantity,
                'pnl': 0,
                'trade_count': 0,
                'trail_start_pnl': 0,
                'trail_step_points': 0
            },
            'put_buy': {
                'option_name': put_option,
                'trade_flag': 0,
                'buy_price': 0,
                'current_stop_price': 0,
                'current_profit_price': 0,
                'target_method': "auto",
                'target_reached': False,
                'filled_df': filled_df.copy(),
                'underlying_price_level': 0,
                'quantity': quantity,
                'pnl': 0,
                'trade_count': 0,
                'trail_start_pnl': 0,
                'trail_step_points': 0
            },
            'condition': False,
            'total_pnl': 0,
            'trade_count': 0,
            'max_trades': MAX_TRADES_PER_DAY
        }

# ===== Broker order functions =====
def send_live_entry_order(symbol, qty, side, buffer=ENTRY_OFFSET):
    """
    Place a live LIMIT entry order via Fyers API.
    Baseline logic: entry price = LTP - buffer (min 0.05).
    """
    try:
        # Get LTP
        quote = fyers.quotes({"symbols": symbol})
        ltp = quote["d"][0]["v"]["lp"]

        # Calculate limit price with buffer
        limit_price = max(ltp - buffer, 0.05)

        order_data = {
            "symbol": symbol,
            "qty": qty,
            "type": 1,              # LIMIT
            "side": side,           # 1=BUY, -1=SELL
            "productType": "INTRADAY",
            "limitPrice": limit_price,
            "stopPrice": 0,
            "validity": "DAY",
            "stopLoss": 0,
            "takeProfit": 0,
            "offlineOrder": False,
            "disclosedQty": 0,
            "isSliceOrder": False,
            "orderTag": str(side)
        }

        response = fyers.place_order(data=order_data)

        if response.get("s") == "ok":
            return True, response.get("id")
        else:
            logging.error(f"[LIVE ENTRY FAILED] {symbol} {response}")
            return False, None

    except Exception as e:
        logging.error(f"[LIVE ENTRY ERROR] {symbol} {e}")
        return False, None


def send_live_exit_order(symbol, qty, reason):
    """
    Place a live MARKET exit order via Fyers API.
    Baseline logic (8th Jan):
    - Always SELL (-1 side)
    - MARKET type (type=2)
    - Tag order with exit reason for audit trail
    """
    try:
        order_data = {
            "symbol": symbol,
            "qty": qty,
            "type": 2,              # MARKET
            "side": -1,             # SELL
            "productType": "INTRADAY",
            "limitPrice": 0,
            "stopPrice": 0,
            "validity": "DAY",
            "stopLoss": 0,
            "takeProfit": 0,
            "offlineOrder": False,
            "disclosedQty": 0,
            "isSliceOrder": False,
            "orderTag": str(reason)  # ensure string tag
        }

        response = fyers.place_order(data=order_data)

        if response.get("s") == "ok":
            logging.info(
                f"{MAGENTA}[LIVE EXIT][{reason}] {symbol} Qty={qty} "
                f"OrderID={response.get('id')}{RESET}"
            )
            return True, response.get("id")
        else:
            logging.error(f"[LIVE EXIT FAILED] {symbol} {response}")
            return False, None

    except Exception as e:
        logging.error(f"{RED}[LIVE EXIT ERROR] {symbol} {e}{RESET}")
        return False, None
    
def send_paper_exit_order(symbol, qty, reason):
    """
    Simulated exit for paper mode.
    Baseline logic (8th Jan):
    - Always log the exit with reason and quantity
    - Return success flag and synthetic order_id
    """
    logging.info(f"{MAGENTA}[PAPER EXIT][{reason}] {symbol} Qty={qty}{RESET}")
    return True, f"paper_exit_{symbol}_{reason}"


def update_order_status(order_id, status, filled_qty, avg_price, symbol):
    """
    Update the global filled_df ledger with order status.
    Baseline logic (8th Jan):
    - If order_id exists, update row
    - Else, append new row
    - Log every update for audit trail
    """
    global filled_df
    color = status_color(status)

    if order_id in filled_df.index:
        filled_df.loc[order_id, "status"] = status
        filled_df.loc[order_id, "filled_qty"] = filled_qty
        filled_df.loc[order_id, "avg_price"] = avg_price
        logging.info(f"{YELLOW}[LEDGER UPDATED] {order_id} -> {status}{RESET}")
    else:
        new_row = pd.DataFrame({
            "status": [status],
            "filled_qty": [filled_qty],
            "avg_price": [avg_price],
            "symbol": [symbol]
        }, index=[order_id])
        filled_df = pd.concat([filled_df, new_row])
        logging.info(f"{YELLOW}[LEDGER APPENDED] {order_id} -> {status}{RESET}")


# ===== Order status polling =====
def check_order_status(order_id, fyers):
    """
    Poll broker for order status and update ledger.
    Baseline logic (8th Jan):
    - Query orderbook by order_id
    - Map status code to human-readable string
    - Update global filled_df via update_order_status
    - Return (status, traded_price)
    """
    try:
        response = fyers.orderbook(data={"id": order_id})

        if response.get("s") == "ok":
            order = response.get("orderBook", [{}])[0]

            status_code   = order.get("status")
            filled_qty    = order.get("filledQty", 0)
            traded_price  = order.get("tradedPrice", 0)
            symbol        = order.get("symbol")

            status = map_status_code(status_code)
            update_order_status(order_id, status, filled_qty, traded_price, symbol)

            return status, traded_price

        else:
            logging.warning(
                f"{RED}[ORDER STATUS] Failed for {order_id}: {response}{RESET}"
            )
            return None, None

    except Exception as e:
        logging.error(f"{RED}[ORDER STATUS ERROR] {e}{RESET}")
        return None, None


# ===== Unified order processing =====
def process_order(side, symbol, price, info, hist_data):
    """
    Unified MTM loop for both paper and live trades with partial profit booking.
    Baseline logic (8th Jan) + Phase-1 updates:
    - Stop-loss check (side-aware, long-only)
    - Partial profit booking (half qty, move SL to cost)
    - Full target exit
    - Trailing stop update after partial booked
    - MTM logging for audit trail
    Returns:
        True if a full exit occurred (STOPLOSS or TARGET), False otherwise.
    """

    leg = "call_buy" if side == "CALL" else "put_buy"
    trade = info[leg]

    if trade["trade_flag"] != 1:
        return False

    entry = trade["buy_price"]
    qty   = trade["quantity"]

    # --- Stop-loss check (long-only, side-aware) ---
    sl_hit = (side == "CALL" and price <= trade["current_stop_price"]) or \
             (side == "PUT"  and price >= trade["current_stop_price"])
    if sl_hit:
        if account_type.lower() == "paper":
            success, order_id = send_paper_exit_order(trade["option_name"], qty, "STOPLOSS")
        else:
            success, order_id = send_live_exit_order(trade["option_name"], qty, "STOPLOSS")

        if success:
            trade["order_id"] = order_id
            pnl_points = (price - entry) if side == "CALL" else (entry - price)
            trade["pnl"] += pnl_points * qty
            info["total_pnl"] = info["call_buy"]["pnl"] + info["put_buy"]["pnl"]
            trade["trade_flag"] = 0
            trade["quantity"] = 0
            trade["filled_df"].loc[dt.now(time_zone)] = [
                symbol, price, "SELL", trade["current_stop_price"],
                trade.get("full_target_price", 0), spot_price, qty
            ]
            logging.info(f"{RED}[EXIT][{account_type.upper()} STOPLOSS] {side} {symbol} Qty={qty} Price={price:.2f}{RESET}")
            update_order_status(order_id, "PENDING", qty, price, symbol)
            return True   # EXIT occurred
        return False

    # --- Partial Profit Booking ---
    partial_hit = (side == "CALL" and price >= trade["partial_target_price"]) or \
                  (side == "PUT"  and price <= trade["partial_target_price"])
    if not trade.get("partial_booked", False) and partial_hit:
        half_qty = qty // 2
        if account_type.lower() == "paper":
            success, order_id = send_paper_exit_order(trade["option_name"], half_qty, "PARTIAL")
        else:
            success, order_id = send_live_exit_order(trade["option_name"], half_qty, "PARTIAL")

        if success:
            trade["order_id"] = order_id
            pnl_points = (price - entry) if side == "CALL" else (entry - price)
            trade["pnl"] += pnl_points * half_qty
            info["total_pnl"] = info["call_buy"]["pnl"] + info["put_buy"]["pnl"]
            trade["quantity"] -= half_qty
            trade["partial_booked"] = True
            trade["current_stop_price"] = entry  # move SL to cost
            trade["filled_df"].loc[dt.now(time_zone)] = [
                symbol, price, "SELL", trade["current_stop_price"],
                trade.get("full_target_price", 0), spot_price, half_qty
            ]
            logging.info(f"{CYAN}[EXIT][{account_type.upper()} PARTIAL] {side} {symbol} HalfQty={half_qty} Price={price:.2f}{RESET}")
            update_order_status(order_id, "PENDING", half_qty, price, symbol)

    # --- Full Target Check ---
    full_hit = (side == "CALL" and price >= trade["full_target_price"]) or \
               (side == "PUT"  and price <= trade["full_target_price"])
    if full_hit:
        if account_type.lower() == "paper":
            success, order_id = send_paper_exit_order(trade["option_name"], trade["quantity"], "TARGET")
        else:
            success, order_id = send_live_exit_order(trade["option_name"], trade["quantity"], "TARGET")

        if success:
            trade["order_id"] = order_id
            pnl_points = (price - entry) if side == "CALL" else (entry - price)
            qty_exit = trade["quantity"]
            trade["pnl"] += pnl_points * qty_exit
            info["total_pnl"] = info["call_buy"]["pnl"] + info["put_buy"]["pnl"]
            trade["trade_flag"] = 0
            trade["quantity"] = 0
            trade["filled_df"].loc[dt.now(time_zone)] = [
                symbol, price, "SELL", trade["current_stop_price"],
                trade.get("full_target_price", 0), spot_price, qty_exit
            ]
            logging.info(f"{GREEN}[EXIT][{account_type.upper()} TARGET] {side} {symbol} Qty={qty_exit} Price={price:.2f}{RESET}")
            update_order_status(order_id, "PENDING", qty_exit, price, symbol)
            return True   # EXIT occurred
        return False

    # --- Trailing Stop Update ---
    if trade.get("partial_booked", False):
        new_stop = update_trailing_stop(
            side, price, entry,
            trade["current_stop_price"],
            trade["trail_start_pnl"],
            trade["trail_step_points"]
        )
        if new_stop != trade["current_stop_price"]:
            trade["current_stop_price"] = new_stop
            logging.info(f"{MAGENTA}[TRAIL STOP UPDATE] {symbol} new SL={new_stop:.2f}{RESET}")

    # --- MTM Logging ---
    logging.info(
        f"{'Paper' if account_type.lower() == 'paper' else 'Live'} MTM "
        f"{side} {symbol} LTP={price:.2f} Entry={entry:.2f}"
    )

    return False   # No full exit occurred

def force_close_old_trades(info, mode="PAPER"):
    ct = dt.now(time_zone)
    for leg, side in [("call_buy", "CALL"), ("put_buy", "PUT")]:
        if info[leg]["trade_flag"] == 1:  # still active
            name = info[leg]["option_name"]
            qty  = info[leg]["quantity"]

            if mode.upper() == "PAPER":
                success, order_id = send_paper_exit_order(name, qty, "FORCE_CLEANUP")
            else:
                success, order_id = send_live_exit_order(name, qty, "FORCE_CLEANUP")

            if success:
                info[leg]["trade_flag"] = 2
                info[leg]["quantity"] = 0
                exit_price = df.loc[name, "ltp"] if name in df.index else spot_price
                info[leg]["filled_df"].loc[ct] = [
                    name, exit_price, "SELL", 0, 0, spot_price, qty
                ]
                logging.info(
                    f"{RED}[FORCE EXIT][{mode}] {side} {name} Qty={qty} Price={exit_price}{RESET}"
                )

# ===== paper_order =====
def paper_order():
    global quantity, paper_info, df, spot_price, last_signal_candle_time

    ct = dt.now(time_zone)

    # 1. Refresh spot price (simulated)
    try:
        quote = fyers.quotes(data={"symbols": ticker})
        spot_price = quote["d"][0]["v"]["lp"]
        logging.info(f"Spot={spot_price}")
    except Exception as e:
        logging.warning(f"[PAPER] Spot fetch failed: {e}")

    # 2. EOD FORCE EXIT
    if ct > end_time:
        logging.info("[PAPER] End time reached, closing open positions")
        for leg, side in [("call_buy", "CALL"), ("put_buy", "PUT")]:
            if paper_info[leg]["trade_flag"] == 1:
                name = paper_info[leg]["option_name"]
                qty  = paper_info[leg]["quantity"]
                success, order_id = send_paper_exit_order(name, qty, "EOD")
                if success:
                    paper_info[leg]["trade_flag"] = 2
                    paper_info[leg]["quantity"] = 0
                    exit_price = df.loc[name, "ltp"] if name in df.index else spot_price
                    paper_info[leg]["filled_df"].loc[ct] = [
                        name, exit_price, "SELL", 0, 0, spot_price, 0
                    ]
                    logging.info(
                        f"{RED}[EXIT][PAPER] {side} {name} Qty={qty} Price={exit_price}{RESET}"
                    )
        return

    # 3. SIGNAL EVALUATION (NEW 3M CANDLE ONLY)
    signal = None
    if not candles_3m.empty:
        last_candle_time = candles_3m.iloc[-1]["time"]
        if last_signal_candle_time != last_candle_time:
            last_signal_candle_time = last_candle_time

            atr, atr_source = resolve_atr(candles_3m, daily_atr)
            logging.info(
                f"{YELLOW}[SIGNAL EVAL][PAPER] candle={last_candle_time} "
                f"candles={len(candles_3m)} atr={atr} source={atr_source}{RESET}"
            )

            prev_day = hist_data.iloc[-1]
            cpr  = calculate_cpr(prev_day["high"], prev_day["low"], prev_day["close"])
            trad = calculate_traditional_pivots(prev_day["high"], prev_day["low"], prev_day["close"])
            cam  = calculate_camarilla_pivots(prev_day["high"], prev_day["low"], prev_day["close"])

            signal = detect_signal(cpr, trad, cam, atr, candles_3m)

   # 4. PAPER ENTRY LOGIC
    if signal:
        side, reason = signal
        logging.info(f"{YELLOW}[SIGNAL][PAPER] {side} ({reason}) at spot={spot_price}{RESET}")

        try:
            # Block conditions
            if paper_info["call_buy"]["trade_flag"] == 1 or paper_info["put_buy"]["trade_flag"] == 1:
                logging.info(f"{MAGENTA}[ENTRY BLOCKED][PAPER] Existing trade active{RESET}")
                return
            if paper_info.get("trade_count", 0) >= MAX_TRADES_PER_DAY:
                logging.info(f"{MAGENTA}[ENTRY BLOCKED][PAPER] Max trades reached{RESET}")
                return
            if paper_info.get("last_exit_time"):
                elapsed = (dt.now(time_zone) - paper_info["last_exit_time"]).total_seconds()
                if elapsed < 180:
                    logging.info(f"{MAGENTA}[ENTRY BLOCKED][PAPER] Cool-down active{RESET}")
                    return

            leg = "call_buy" if side == "CALL" else "put_buy"
            if paper_info[leg]["trade_flag"] == 0:
                opt_type = "CE" if side == "CALL" else "PE"
                opt_name, _ = get_option_by_moneyness(
                    spot_price, opt_type,
                    moneyness=CALL_MONEYNESS if side == "CALL" else PUT_MONEYNESS
                )

                if opt_name and opt_name in df.index:
                    entry_price = df.loc[opt_name, "ltp"] or spot_price  # ✅ original logic

                    # Risk/Reward
                    risk_points   = max(profit_loss_point * 2, atr * 0.5)
                    reward_points = risk_points * 2.0
                    if side == "CALL":
                        stop  = entry_price - risk_points
                        partial_target = entry_price + reward_points / 2
                        full_target    = entry_price + reward_points
                    else:
                        stop  = entry_price - risk_points
                        partial_target = entry_price - reward_points / 2
                        full_target    = entry_price - reward_points

                    trail_start = reward_points / 2
                    trail_step  = atr * 0.1

                    # Ledger update
                    paper_info[leg].update({
                        "option_name": opt_name,
                        "quantity": quantity,
                        "buy_price": entry_price,
                        "order_type": ORDER_TYPE,
                        "current_stop_price": stop,
                        "full_target_price": full_target,
                        "partial_target_price": partial_target,
                        "trail_start_pnl": trail_start,
                        "trail_step_points": trail_step,
                        "trade_flag": 1,
                        "partial_booked": False,
                        "pnl": 0,
                        "reason": reason,
                        "order_id": f"paper_{opt_name}_{ct}",
                        "entry_time": ct,
                    })
                    paper_info[leg]["filled_df"].loc[ct] = [
                        opt_name, entry_price, "BUY", stop, full_target, spot_price, quantity
                    ]
                    paper_info["trade_count"] = paper_info.get("trade_count", 0) + 1

                    logging.info(
                        f"{GREEN}[ENTRY][PAPER] {side} {opt_name} BUY @ {entry_price:.2f} "
                        f"SL={stop:.2f} PT={partial_target:.2f} TG={full_target:.2f}{RESET}"
                    )
        except Exception as e:
            logging.error(f"[ENTRY ERROR][PAPER] {e}", exc_info=True)


    # 5. TRAILING STOP + EXIT MANAGEMENT
    for leg, side in [("call_buy", "CALL"), ("put_buy", "PUT")]:
        if paper_info[leg]["trade_flag"] != 1:
            continue
        name = paper_info[leg]["option_name"]
        price = df.loc[name, "ltp"] if name in df.index else None
        if price is None or pd.isna(price):
            continue
        exit_triggered = process_order(side, name, price, paper_info, hist_data)
        if exit_triggered:
            paper_info["last_exit_time"] = dt.now(time_zone)
            logging.info(f"{MAGENTA}[EXIT RECORDED] {side} {name} at {paper_info['last_exit_time']}{RESET}")

    # 6. SAVE TRADES
    frames = [paper_info["call_buy"]["filled_df"], paper_info["put_buy"]["filled_df"]]
    frames = [f for f in frames if not f.empty]
    if frames:
        combined = pd.concat(frames)
        combined.to_csv(f"trades_{strategy_name}_{dt.now(time_zone).date()}.csv")
    store(paper_info, account_type)


# =============================== Live Trading =======================================
# ===== real_order =====

def real_order():
    global quantity, live_info, df, spot_price, last_signal_candle_time

    ct = dt.now(time_zone)

    # 1. Refresh spot price
    try:
        quote = fyers.quotes(data={"symbols": ticker})
        spot_price = quote["d"][0]["v"]["lp"]
        logging.info(f"Spot={spot_price}")
    except Exception as e:
        logging.warning(f"[LIVE] Spot fetch failed: {e}")

    # 2. EOD FORCE EXIT
    if ct > end_time:
        logging.info("[LIVE] End time reached, closing open positions")
        for leg, side in [("call_buy", "CALL"), ("put_buy", "PUT")]:
            if live_info[leg]["trade_flag"] == 1:
                name = live_info[leg]["option_name"]
                qty  = live_info[leg]["quantity"]
                success, order_id = send_live_exit_order(name, qty, "EOD")
                if success:
                    live_info[leg]["trade_flag"] = 2
                    live_info[leg]["quantity"] = 0
                    exit_price = df.loc[name, "ltp"] if name in df.index else spot_price
                    live_info[leg]["filled_df"].loc[ct] = [
                        name, exit_price, "SELL", 0, 0, spot_price, 0
                    ]
                    logging.info(
                        f"{RED}[EXIT][LIVE] {side} {name} Qty={qty} Price={exit_price}{RESET}"
                    )
        return

    # 3. SIGNAL EVALUATION (new 3M candle only)
    signal = None
    if not candles_3m.empty:
        last_candle_time = candles_3m.iloc[-1]["time"]
        if last_signal_candle_time != last_candle_time:
            last_signal_candle_time = last_candle_time
            atr, atr_source = resolve_atr(candles_3m, daily_atr)
            logging.info(
                f"{YELLOW}[SIGNAL EVAL][LIVE] candle={last_candle_time} "
                f"candles={len(candles_3m)} atr={atr} source={atr_source}{RESET}"
            )
            prev_day = hist_data.iloc[-1]
            cpr  = calculate_cpr(prev_day["high"], prev_day["low"], prev_day["close"])
            trad = calculate_traditional_pivots(prev_day["high"], prev_day["low"], prev_day["close"])
            cam  = calculate_camarilla_pivots(prev_day["high"], prev_day["low"], prev_day["close"])
            signal = detect_signal(cpr, trad, cam, atr, candles_3m)

   # 4. LIVE ENTRY LOGIC
    if signal:
        side, reason = signal
        logging.info(f"{GREEN}[SIGNAL][LIVE] {side} ({reason}) at spot={spot_price}{RESET}")

        try:
            # Block conditions
            if live_info["call_buy"]["trade_flag"] == 1 or live_info["put_buy"]["trade_flag"] == 1:
                logging.info(f"{MAGENTA}[ENTRY BLOCKED][LIVE] Existing trade active{RESET}")
                return
            if live_info.get("trade_count", 0) >= MAX_TRADES_PER_DAY:
                logging.info(f"{MAGENTA}[ENTRY BLOCKED][LIVE] Max trades reached{RESET}")
                return
            if live_info.get("last_exit_time"):
                elapsed = (dt.now(time_zone) - live_info["last_exit_time"]).total_seconds()
                if elapsed < 180:
                    logging.info(f"{MAGENTA}[ENTRY BLOCKED][LIVE] Cool-down active{RESET}")
                    return

            leg = "call_buy" if side == "CALL" else "put_buy"
            opt_type = "CE" if side == "CALL" else "PE"
            opt_name, _ = get_option_by_moneyness(
                spot_price, opt_type,
                moneyness=CALL_MONEYNESS if side == "CALL" else PUT_MONEYNESS
            )

            if opt_name and opt_name in df.index:
                entry_price = df.loc[opt_name, "ltp"] or spot_price  # ✅ original logic

                # Place live order
                success, order_id = send_live_entry_order(opt_name, quantity, "BUY")
                if not success:
                    logging.warning(f"{RED}[LIVE ENTRY] Failed to place {side} for {opt_name}{RESET}")
                    return

                live_info[leg].update({
                    "option_name": opt_name,
                    "quantity": quantity,
                    "order_type": ORDER_TYPE,
                    "trade_flag": 0,   # pending until filled
                    "order_id": order_id,
                    "reason": reason,
                    "entry_time": ct,
                })
                logging.info(f"{YELLOW}[LIVE ENTRY PENDING] {side} {opt_name} OrderID={order_id}{RESET}")

                # Poll broker until filled
                status, filled_price = check_order_status(order_id, fyers)
                if status == "TRADED":
                    risk_points   = max(profit_loss_point * 2, atr * 0.5)
                    reward_points = risk_points * 2.0
                    if side == "CALL":
                        stop  = filled_price - risk_points
                        partial_target = filled_price + reward_points / 2
                        full_target    = filled_price + reward_points
                    else:
                        stop  = filled_price - risk_points
                        partial_target = filled_price - reward_points / 2
                        full_target    = filled_price - reward_points

                    trail_start = reward_points / 2
                    trail_step  = atr * 0.1

                    live_info[leg].update({
                        "buy_price": filled_price,
                        "current_stop_price": stop,
                        "full_target_price": full_target,
                        "partial_target_price": partial_target,
                        "trail_start_pnl": trail_start,
                        "trail_step_points": trail_step,
                        "trade_flag": 1,
                        "partial_booked": False,
                        "pnl": 0,
                    })
                    live_info[leg]["filled_df"].loc[ct] = [
                        opt_name, filled_price, "BUY", stop, full_target, spot_price, quantity
                    ]
                    live_info["trade_count"] = live_info.get("trade_count", 0) + 1
                    logging.info(
                        f"{GREEN}[{side} ENTRY CONFIRMED][LIVE] {opt_name} BUY @ {filled_price:.2f} "
                        f"SL={stop:.2f} PT={partial_target:.2f} TG={full_target:.2f}{RESET}"
                    )
                elif status == "PENDING":
                    logging.info(f"{YELLOW}[LIVE ENTRY STILL PENDING] {side} {opt_name} OrderID={order_id}{RESET}")
                elif status == "CANCELLED":
                    logging.warning(f"{MAGENTA}[LIVE ENTRY CANCELLED] {side} {opt_name} OrderID={order_id}{RESET}")
                    live_info[leg]["trade_flag"] = 0
                    live_info[leg]["order_id"] = None
        except Exception as e:
            logging.error(f"[ENTRY ERROR][LIVE] {e}", exc_info=True)
            
    # 5. TRAILING STOP + EXIT MANAGEMENT
    for leg, side in [("call_buy", "CALL"), ("put_buy", "PUT")]:
        if live_info[leg]["trade_flag"] != 1:
            continue
        name = live_info[leg]["option_name"]
        price = df.loc[name, "ltp"] if name in df.index else None
        if price is None or pd.isna(price):
            continue
        exit_triggered = process_order(side, name, price, live_info, hist_data)
        if exit_triggered:
            live_info["last_exit_time"] = dt.now(time_zone)
            logging.info(f"{MAGENTA}[EXIT RECORDED][LIVE] {side} {name} at {live_info['last_exit_time']}{RESET}")

    # 6. SAVE TRADES
    frames = [live_info["call_buy"]["filled_df"], live_info["put_buy"]["filled_df"]]
    frames = [f for f in frames if not f.empty]
    if frames:
        combined = pd.concat(frames)
        combined.to_csv(f"trades_{strategy_name}_{dt.now(time_zone).date()}.csv")
    store(live_info, account_type)

