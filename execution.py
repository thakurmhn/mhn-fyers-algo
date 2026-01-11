# ===== execution.py =====
import logging
import pickle
import pandas as pd
import pendulum as dt
from fyers_apiv3 import fyersModel

from config import (
    time_zone, strategy_name, MAX_TRADES_PER_DAY, account_type, quantity,
    CALL_MONEYNESS, PUT_MONEYNESS, profit_loss_point,
    ATR_STOP_MULT, ATR_TGT_MULT, TRAIL_TRIGGER, TRAIL_STEP, ENTRY_OFFSET, ORDER_TYPE
)
from setup import (
    fyers, fyers_asysc, ticker, option_chain, df, spot_price,
    start_time, end_time, hist_data
)
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

#===========================================================


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
def get_option_by_moneyness(spot_price_, side, moneyness='OTM', points=0):
    from config import strike_diff
    if spot_price_ is None or pd.isna(spot_price_):
        return None, None

    base_strike = round(spot_price_ / strike_diff) * strike_diff
    if side == 'CE':
        strike = base_strike + strike_diff if moneyness == 'OTM' else base_strike - strike_diff
    else:  # 'PE'
        strike = base_strike - strike_diff if moneyness == 'OTM' else base_strike + strike_diff
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
        logging.warning(f"Fallback {moneyness} for {side}: requested {strike}, using nearest available")
        return symbol, strike
    return sel.squeeze(), strike

# ===== Dynamic levels =====
def build_dynamic_levels(entry_price, side, atr, rr_ratio=2.0):
    risk_points = max(profit_loss_point, atr * 0.25)
    reward_points = risk_points * rr_ratio

    # Long options: both CALL and PUT targets above entry, stop below entry
    stop  = entry_price - risk_points
    partial_target = entry_price + reward_points / 2
    full_target    = entry_price + reward_points

    trail_start = reward_points / 2
    trail_step  = atr * 0.1
    return stop, full_target, partial_target, trail_start, trail_step

def update_trailing_stop(side, current_price, entry_price, current_stop, trail_start_pnl, trail_step_points):
    if side == "CALL":
        pnl = current_price - entry_price
        if pnl >= trail_start_pnl:
            candidate = current_price - trail_step_points
            return max(current_stop, candidate)
        return current_stop
    else:
        pnl = entry_price - current_price
        if pnl >= trail_start_pnl:
            candidate = current_price + trail_step_points
            return min(current_stop, candidate)
        return current_stop

# ===== PAPER/LIVE STATE INIT =====
if account_type == 'PAPER':
    try:
        paper_info = load(account_type)
    except:
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
        put_option,  put_buy_strike  = _init_otm(spot_price, 'PE', 0)
        logging.info('started')

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
    except:
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
        put_option,  put_buy_strike  = _init_otm(spot_price, 'PE', 0)
        logging.info('started')

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
    try:
        quote = fyers.quotes({"symbols": symbol})
        ltp = quote["d"][0]["v"]["lp"]
        limit_price = max(ltp - buffer, 0.05)

        order_data = {
            "symbol": symbol,
            "qty": qty,
            "type": 1,              # LIMIT
            "side": 1,              # BUY
            "productType": "INTRADAY",
            "limitPrice": limit_price,
            "stopPrice": 0,
            "validity": "DAY",
            "stopLoss": 0,
            "takeProfit": 0,
            "offlineOrder": False,
            "disclosedQty": 0,
            "isSliceOrder": False,
            "orderTag": side
        }

        response = fyers.place_order(order_data)
        if response.get("s") == "ok":
            return True, response.get("id")
        else:
            logging.error(f"[LIVE ENTRY FAILED] {symbol} {response}")
            return False, None
    except Exception as e:
        logging.error(f"[LIVE ENTRY ERROR] {symbol} {e}")
        return False, None

def send_live_exit_order(symbol, qty, reason):
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
            "orderTag": reason
        }

        response = fyers.place_order(order_data)
        if response.get("s") == "ok":
            logging.info(f"[LIVE EXIT][{reason}] {symbol} Qty={qty} OrderID={response.get('id')}")
            return True, response.get("id")
        else:
            logging.error(f"[LIVE EXIT FAILED] {symbol} {response}")
            return False, None
    except Exception as e:
        logging.error(f"[LIVE EXIT ERROR] {symbol} {e}")
        return False, None

def send_paper_exit_order(symbol, qty, reason):
    """
    Simulated exit for paper mode.
    """
    logging.info(f"{MAGENTA}[PAPER EXIT][{reason}] {symbol} Qty={qty}{RESET}")
    return True, f"paper_exit_{symbol}_{reason}"

def update_order_status(order_id, status, filled_qty, avg_price, symbol):
    global filled_df
    color = status_color(status)
    if order_id in filled_df.index:
        filled_df.loc[order_id, "status"] = status
        filled_df.loc[order_id, "filled_qty"] = filled_qty
        filled_df.loc[order_id, "avg_price"] = avg_price
        logging.info(f"{color}[LEDGER UPDATED] {order_id} → {status}{RESET}")
    else:
        new_row = pd.DataFrame({
            "status": [status],
            "filled_qty": [filled_qty],
            "avg_price": [avg_price],
            "symbol": [symbol]
        }, index=[order_id])
        filled_df = pd.concat([filled_df, new_row])
        logging.info(f"{color}[LEDGER APPENDED] {order_id} → {status}{RESET}")

# ===== Order status polling =====
def check_order_status(order_id, fyers):
    try:
        response = fyers.orderbook(data={"id": order_id})   
        if response.get("s") == "ok":
            order = response.get("orderBook", [{}])[0]
            status_code = order.get("status")
            filled_qty = order.get("filledQty", 0)
            traded_price = order.get("tradedPrice", 0)  
            symbol = order.get("symbol")

            status = map_status_code(status_code)
            update_order_status(order_id, status, filled_qty, traded_price, symbol)
            return status, traded_price
        else:
            logging.warning(f"{RED}[ORDER STATUS] Failed for {order_id}: {response}{RESET}")
            return None, None
    except Exception as e:
        logging.error(f"[ORDER STATUS ERROR] {e}")
        return None, None


# ===== Unified order processing =====
def process_order(side, symbol, price, info, hist_data):
    """
    Unified MTM loop for both paper and live trades with partial profit booking.
    Exits are broker-confirmed in live mode, simulated in paper mode.
    Stores order_id into trade['order_id'] for audit trail.
    """

    leg = "call_buy" if side == "CALL" else "put_buy"
    trade = info[leg]

    if trade["trade_flag"] != 1:
        return

    entry = trade["buy_price"]
    qty   = trade["quantity"]

    # --- Stop-loss check (side-aware) ---
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
            # Initialize global ledger entry
            update_order_status(order_id, "PENDING", qty, price, symbol)
        return

    # --- Partial Profit Booking (side-aware) ---
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
            # Initialize global ledger entry
            update_order_status(order_id, "PENDING", half_qty, price, symbol)

    # --- Full Target Check (side-aware) ---
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
            # Initialize global ledger entry
            update_order_status(order_id, "PENDING", qty_exit, price, symbol)
        return

    # --- Trailing logic (use helper for side-awareness) ---
    if trade.get("partial_booked", False):
        new_stop = update_trailing_stop(
            side, price, entry,
            trade["current_stop_price"],
            trade["trail_start_pnl"],
            trade["trail_step_points"]
        )
        if new_stop != trade["current_stop_price"]:
            trade["current_stop_price"] = new_stop
            logging.info(f"[TRAIL STOP UPDATE] {symbol} new SL={new_stop:.2f}")

    # --- MTM Logging ---
    logging.info(f"{'Paper' if account_type.lower() == 'paper' else 'Live'} MTM {side} {symbol} LTP={price:.2f} Entry={entry:.2f}")

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
                    paper_info[leg]["filled_df"].loc[ct] = [name, exit_price, "SELL", 0, 0, spot_price, 0]
                    logging.info(f"{RED}[EXIT][PAPER] {side} {name} Qty={qty} Price={exit_price}{RESET}")
        return

    # 3. SIGNAL EVALUATION (NEW 3M CANDLE ONLY)
    signal = None
    if not candles_3m.empty:
        last_candle_time = candles_3m.iloc[-1]["time"]
        if last_signal_candle_time != last_candle_time:
            last_signal_candle_time = last_candle_time

            atr, atr_source = resolve_atr(candles_3m, daily_atr)
            logging.info(f"[SIGNAL EVAL][PAPER] candle={last_candle_time} candles={len(candles_3m)} atr={atr} source={atr_source}")

            prev_day = hist_data.iloc[-1]
            cpr  = calculate_cpr(prev_day["high"], prev_day["low"], prev_day["close"])
            trad = calculate_traditional_pivots(prev_day["high"], prev_day["low"], prev_day["close"])
            cam  = calculate_camarilla_pivots(prev_day["high"], prev_day["low"], prev_day["close"])

            signal = detect_signal(cpr, trad, cam, atr, candles_3m)

    # 4. PAPER ENTRY LOGIC
    if signal:
        side, reason = signal
        logging.info(f"[SIGNAL][PAPER] {side} ({reason}) at spot={spot_price}")

        if paper_info["call_buy"]["trade_flag"] == 1 or paper_info["put_buy"]["trade_flag"] == 1:
            logging.info(f"{MAGENTA}[ENTRY BLOCKED][PAPER] Existing trade active, skipping new signal{RESET}")
            return

        if paper_info.get("trade_count", 0) >= MAX_TRADES_PER_DAY:
            logging.info(f"{MAGENTA}[ENTRY][PAPER] Max trades reached{RESET}")
            return

        leg = "call_buy" if side == "CALL" else "put_buy"

        if paper_info[leg]["trade_flag"] == 0:
            opt_type = "CE" if side == "CALL" else "PE"
            opt_name, _ = get_option_by_moneyness(
                spot_price, opt_type,
                moneyness=CALL_MONEYNESS if side == "CALL" else PUT_MONEYNESS
            )

            if opt_name and opt_name in df.index:
                ltp = df.loc[opt_name, "ltp"]

                # ATR-scaled but ratio-balanced levels
                risk_points = max(profit_loss_point, atr * 0.25)
                reward_points = risk_points * 2.0
                # Long options: both CALL and PUT targets are above entry (option price rises when trade works)
                stop  = ltp - risk_points
                partial_target = ltp + reward_points / 2
                full_target    = ltp + reward_points

                trail_start = reward_points / 2
                trail_step  = atr * 0.1

                # Entry price logic (MARKET vs LIMIT)
                entry_price = ltp if ORDER_TYPE == "MARKET" else max(ltp - ENTRY_OFFSET, 0.05)

                # Update ledger
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

                # logging.info(
                #     f"[{side} ENTRY][PAPER] {opt_name} @ {entry_price:.2f} "
                #     f"SL={stop:.2f} PT={partial_target:.2f} TG={full_target:.2f}"
                # )
                logging.info(f"{GREEN}[ENTRY][PAPER] {side} {opt_name} @ {entry_price:.2f}"
                 f"SL={stop:.2f} PT={partial_target:.2f} TG={full_target:.2f}{RESET}")

    # 5. TRAILING STOP + EXIT MANAGEMENT (continuous)
    for leg, side in [("call_buy", "CALL"), ("put_buy", "PUT")]:
        if paper_info[leg]["trade_flag"] != 1:
            continue

        name = paper_info[leg]["option_name"]
        price = df.loc[name, "ltp"] if name in df.index else None
        if price is None or pd.isna(price):
            continue

        process_order(side, name, price, paper_info, hist_data)

    # 6. SAVE TRADES
    frames = [paper_info["call_buy"]["filled_df"], paper_info["put_buy"]["filled_df"]]
    frames = [f for f in frames if not f.empty]

    if frames:
        combined = pd.concat(frames)
        combined.to_csv(f"trades_{strategy_name}_{dt.now(time_zone).date()}.csv")

    store(paper_info, account_type)

# =============================== Live Trading =======================================
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
                    live_info[leg]["filled_df"].loc[ct] = [name, exit_price, "SELL", 0, 0, spot_price, 0]
                    logging.info(f"{RED}[EXIT][LIVE] {side} {name} Qty={qty} Price={exit_price}{RESET}")

        return

    # 3. SIGNAL EVALUATION (new 3M candle only)
    signal = None
    if not candles_3m.empty:
        last_candle_time = candles_3m.iloc[-1]["time"]
        if last_signal_candle_time != last_candle_time:
            last_signal_candle_time = last_candle_time
            atr, atr_source = resolve_atr(candles_3m, daily_atr)
            logging.info(f"{YELLOW}[SIGNAL EVAL][LIVE] candle={last_candle_time} candles={len(candles_3m)} atr={atr} source={atr_source}{RESET}")
            prev_day = hist_data.iloc[-1]
            cpr  = calculate_cpr(prev_day["high"], prev_day["low"], prev_day["close"])
            trad = calculate_traditional_pivots(prev_day["high"], prev_day["low"], prev_day["close"])
            cam  = calculate_camarilla_pivots(prev_day["high"], prev_day["low"], prev_day["close"])
            signal = detect_signal(cpr, trad, cam, atr, candles_3m)

    # 4. LIVE ENTRY LOGIC
    if signal:
        side, reason = signal
        logging.info(f"{GREEN}[SIGNAL][LIVE] {side} ({reason}) at spot={spot_price}{RESET}")

        if live_info["call_buy"]["trade_flag"] == 1 or live_info["put_buy"]["trade_flag"] == 1:
            logging.info(f"{MAGENTA}[ENTRY BLOCKED][LIVE] Existing trade active, skipping new signal{RESET}")
            return
        if live_info.get("trade_count", 0) >= MAX_TRADES_PER_DAY:
            logging.info(f"{MAGENTA}[ENTRY][LIVE] Max trades reached{RESET}")
            return

        leg = "call_buy" if side == "CALL" else "put_buy"
        opt_type = "CE" if side == "CALL" else "PE"
        opt_name, _ = get_option_by_moneyness(
            spot_price, opt_type,
            moneyness=CALL_MONEYNESS if side == "CALL" else PUT_MONEYNESS
        )

        ltp = df.loc[opt_name, "ltp"] if (opt_name and opt_name in df.index) else None
        if ltp is None or pd.isna(ltp):
            logging.warning(f"{MAGENTA}[LIVE ENTRY] No LTP for {opt_name}, skipping entry{RESET}")
        else:
            # Place live LIMIT order
            success, order_id = send_live_entry_order(opt_name, quantity, side)
            if not success:
                logging.warning(f"{RED}[LIVE ENTRY] Failed to place {side} for {opt_name}{RESET}")
            else:
                live_info[leg].update({
                    "option_name": opt_name,
                    "quantity": quantity,
                    "order_type": "LIMIT",
                    "trade_flag": 0,   # pending until filled
                    "order_id": order_id,
                    "reason": reason,
                    "entry_time": ct,
                })
                logging.info(f"{YELLOW}[LIVE ENTRY PENDING] {side} {opt_name} OrderID={order_id}{RESET}")

                # Poll broker until filled
                status, filled_price = check_order_status(order_id, fyers)
                if status == "TRADED":
                    stop, full_target, partial_target, trail_start, trail_step = build_dynamic_levels(filled_price, side, atr)
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
                    logging.info(f"{GREEN}[{side} ENTRY CONFIRMED][LIVE] {opt_name} @ {filled_price:.2f} SL={stop:.2f} PT={partial_target:.2f} TG={full_target:.2f}{RESET}")
                elif status == "PENDING":
                    logging.info(f"{YELLOW}[LIVE ENTRY STILL PENDING] {side} {opt_name} OrderID={order_id}{RESET}")
                elif status == "CANCELLED":
                    logging.warning(f"{MAGENTA}[LIVE ENTRY CANCELLED] {side} {opt_name} OrderID={order_id}{RESET}")
                    live_info[leg]["trade_flag"] = 0
                    live_info[leg]["order_id"] = None

    # 5. TRAILING STOP + EXIT MANAGEMENT
    for leg, side in [("call_buy", "CALL"), ("put_buy", "PUT")]:
        if live_info[leg]["trade_flag"] != 1:
            continue
        name = live_info[leg]["option_name"]
        price = df.loc[name, "ltp"] if name in df.index else None
        if price is None or pd.isna(price):
            continue
        process_order(side, name, price, live_info, hist_data)

    # 6. SAVE TRADES
    frames = [live_info["call_buy"]["filled_df"], live_info["put_buy"]["filled_df"]]
    frames = [f for f in frames if not f.empty]
    if frames:
        combined = pd.concat(frames)
        combined.to_csv(f"trades_{strategy_name}_{dt.now(time_zone).date()}.csv")
    store(live_info, account_type)