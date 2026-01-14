# ===== execution.py =====
import logging
import pickle
import pandas as pd
import pendulum as dt
from fyers_apiv3 import fyersModel

from config import (
    time_zone, strategy_name, MAX_TRADES_PER_DAY, account_type, quantity,
    CALL_MONEYNESS, PUT_MONEYNESS, profit_loss_point, hard_stop_points, MIN_MOMENTUM, ATR_MAX,
    ATR_STOP_MULT, ATR_TGT_MULT, TRAIL_TRIGGER, TRAIL_STEP, ENTRY_OFFSET, ORDER_TYPE, TRAILING_SL_BUFFER, MIN_MOMENTUM, ATR_MAX, 
)
from setup import (
    fyers, fyers_asysc, ticker, option_chain, df, spot_price,
    start_time, end_time, hist_data
)
from indicators import (
    calculate_cpr, calculate_traditional_pivots, calculate_camarilla_pivots,
    resolve_atr, daily_atr, candles_3m, get_dynamic_target, get_dynamic_stop, is_strong_trade
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

#==============================================================
# Initalize filled_df
try:
    filled_df
except NameError:
    filled_df = pd.DataFrame(columns=["status", "filled_qty", "avg_price", "symbol"])


#==============================================================


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

# ====================================================== Option selection by moneyness ====================================

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

# =============================================== Dynamic levels =======================================================

def build_dynamic_levels(entry_price: float, side: str, prev_day=None, method="auto", reason=None, breakout_meta=None):
    """
    Fixed-point levels for long option buys (CALL or PUT).
    Also logs dynamic target/stop for audit comparison.

    Breakout optimization:
    - If reason == "CONSOLIDATION_BREAKOUT", widen full target, tighten initial SL,
      and start trailing earlier with smaller steps.
    - Uses breakout_meta (optional) to scale aggressiveness:
        breakout_meta = {
            "win": int,                # consolidation window size
            "range_atr_ratio": float,  # consolidation range / ATR
            "upper": float,            # upper band
            "lower": float             # lower band
        }
    """
    # Base fixed levels (defaults from config)
    sl = entry_price - hard_stop_points
    partial_tg = entry_price + profit_loss_point
    full_tg    = entry_price + 2 * profit_loss_point
    trail_start = profit_loss_point
    trail_step  = profit_loss_point

    # --- Breakout-specific adjustments ---
    if reason == "CONSOLIDATION_BREAKOUT":
        # Use band extremes as initial SL for tighter risk
        if breakout_meta and "upper" in breakout_meta and "lower" in breakout_meta:
            band_low  = breakout_meta["lower"]
            band_high = breakout_meta["upper"]
        else:
            band_low, band_high = None, None

        # Initial SL: breakout candle extreme or band extreme
        # (CALL: SL at breakout candle low or band low; PUT: SL at breakout candle high or band high)
        if side == "CALL":
            sl = max(entry_price - hard_stop_points, band_low if band_low is not None else entry_price - hard_stop_points)
        else:
            sl = min(entry_price + hard_stop_points, band_high if band_high is not None else entry_price + hard_stop_points)

        # Targets: widen full target for breakout runs, keep partial closer to secure gains
        # Scale aggressiveness by consolidation quality if provided
        win = breakout_meta.get("win", 8) if breakout_meta else 8
        rng_ratio = breakout_meta.get("range_atr_ratio", 0.4) if breakout_meta else 0.4

        # Heuristic scaling: longer window + tighter range → more aggressive TG
        tg_multiplier = 2.0
        if win >= 10 and rng_ratio <= 0.4:
            tg_multiplier = 2.5
        elif win >= 12 and rng_ratio <= 0.35:
            tg_multiplier = 3.0

        partial_tg = entry_price + profit_loss_point  # secure early
        full_tg    = entry_price + tg_multiplier * profit_loss_point

        # Trailing: start earlier and step tighter to lock in gains
        trail_start = max(8, profit_loss_point - 2)   # earlier activation
        trail_step  = max(6, int(profit_loss_point * 0.6))  # tighter steps

        logging.info(f"{CYAN}[BREAKOUT LEVELS][{side}] Entry={entry_price:.2f} "
                     f"SL={sl:.2f} PT={partial_tg:.2f} TG={full_tg:.2f} "
                     f"TrailStart={trail_start} TrailStep={trail_step} "
                     f"win={win} rngATR={rng_ratio:.2f}{RESET}")

    # If prev_day data is provided, compute pivots for dynamic levels
    if prev_day is not None:
        cpr  = calculate_cpr(prev_day["high"], prev_day["low"], prev_day["close"])
        trad = calculate_traditional_pivots(prev_day["high"], prev_day["low"], prev_day["close"])
        cam  = calculate_camarilla_pivots(prev_day["high"], prev_day["low"], prev_day["close"])

        dyn_target = get_dynamic_target(side, entry_price, trad, cpr, cam, method=method)
        dyn_stop   = get_dynamic_stop(side, entry_price, trad, cpr, cam, method=method)

        logging.info(f"{CYAN}[LEVELS][{side}] Entry={entry_price:.2f} "
                     f"FixedSL={sl:.2f} FixedTG={full_tg:.2f} "
                     f"DynSL={dyn_stop:.2f} DynTG={dyn_target:.2f} Method={method}{RESET}")

    return round(sl, 2), round(full_tg, 2), round(partial_tg, 2), trail_start, trail_step


def apply_trailing(order_info, ltp):
    """
    Move SL upward in fixed increments once trail_start is reached.
    """
    if ltp >= order_info["entry_price"] + order_info["trail_start"]:
        steps = int((ltp - (order_info["entry_price"] + order_info["trail_start"])) 
                    / order_info["trail_step"]) + 1

        candidate = order_info["entry_price"] + steps * order_info["trail_step"]
        if candidate > order_info["stoploss"]:
            order_info["stoploss"] = round(candidate, 2)
            logging.info(f"[TRAILING] Stoploss moved to {order_info['stoploss']} at LTP={ltp}")


# ================================================================================================================

# Fix trailing SL

# def update_trailing_stop(side, current_price, entry_price, current_stop, trail_start_pnl, trail_step_points):
#     """
#     Trailing for long options (CALL/PUT): SL moves upward as price rises.
#     """
#     pnl = current_price - entry_price  # same for CALL and PUT when long

#     if pnl >= trail_start_pnl:
#         # Steps beyond trail_start
#         steps = int((pnl - trail_start_pnl) / trail_step_points) + 1
#         candidate = entry_price + steps * trail_step_points

#         # Only tighten upward
#         if candidate > current_stop:
#             return round(candidate, 2)

#     return current_stop

# =============================================================================================================

# Update Tailing SL with 25% buffer

def update_trailing_stop(side, current_price, entry_price, current_stop, trail_start_pnl, trail_step_points):
    """
    Trailing stop for long options (CALL/PUT).
    SL moves upward as price rises, with a 25% buffer applied.
    """
    pnl = current_price - entry_price  # profit in points

    if pnl >= trail_start_pnl:
        # Steps beyond trail_start
        steps = int((pnl - trail_start_pnl) / trail_step_points) + 1
        candidate = entry_price + steps * trail_step_points

        # Apply 25% buffer
        buffer = TRAILING_SL_BUFFER * trail_step_points
        buffered_sl = candidate - buffer

        # Only tighten upward
        if buffered_sl > current_stop:
            return round(buffered_sl, 2)

    return current_stop
# =====================================================================================================================




# ======================================================= PAPER/LIVE STATE INIT =====================================================
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

# =================================================== Broker order functions =========================================================
def send_live_entry_order(symbol, qty, side, buffer=ENTRY_OFFSET):
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
            "side": side,           # <-- use parameter (1=BUY, -1=SELL)
            "productType": "INTRADAY",
            "limitPrice": limit_price,
            "stopPrice": 0,
            "validity": "DAY",
            "stopLoss": 0,
            "takeProfit": 0,
            "offlineOrder": False,
            "disclosedQty": 0,
            "isSliceOrder": False,
            "orderTag": str(side)   # optional tag, stringified
        }

        # Correct call with data= keyword
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
            "orderTag": str(reason) # ensure string tag
        }

        # ✅ Correct call with data= keyword
        response = fyers.place_order(data=order_data)

        if response.get("s") == "ok":
            logging.info(f"{MAGENTA}[LIVE EXIT][{reason}] {symbol} Qty={qty} OrderID={response.get('id')}{RESET}")
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
        logging.info(f"{YELLOW}[LEDGER UPDATED] {order_id} -> {status}{RESET}")  # ASCII arrow
    else:
        new_row = pd.DataFrame({
            "status": [status],
            "filled_qty": [filled_qty],
            "avg_price": [avg_price],
            "symbol": [symbol]
        }, index=[order_id])
        filled_df = pd.concat([filled_df, new_row])
        logging.info(f"{YELLOW}[LEDGER APPENDED] {order_id} -> {status}{RESET}")  # ASCII arrow


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

# =================================================== Order Processing ===========================================================

# ===== Unified order processing =====
# def process_order(side, symbol, price, info, hist_data):
#     """
#     Unified MTM loop for both paper and live trades with partial profit booking
#     and trailing stop management. Exits are broker-confirmed in live mode,
#     simulated in paper mode. Stores order_id into trade['order_id'] for audit trail.

#     Breakout optimization:
#     - If trade['reason'] == "CONSOLIDATION_BREAKOUT":
#         * Use tighter trailing (already set via build_dynamic_levels).
#         * After partial booking, trail to cost immediately (already implemented).
#         * Add 'momentum hold' guard: if momentum remains strong, delay full exit
#           until next structural level (optional).
#     """

#     leg = "call_buy" if side == "CALL" else "put_buy"
#     trade = info[leg]

#     if trade["trade_flag"] != 1:
#         return

#     entry = trade["buy_price"]
#     qty   = trade["quantity"]
#     reason = trade.get("reason", "")

#     # --- Stop-loss check (side-aware) ---
#     sl_hit = (side == "CALL" and price <= trade["current_stop_price"]) or \
#              (side == "PUT"  and price >= trade["current_stop_price"])
#     if sl_hit:
#         if account_type.lower() == "paper":
#             success, order_id = send_paper_exit_order(trade["option_name"], qty, "STOPLOSS")
#         else:
#             success, order_id = send_live_exit_order(trade["option_name"], qty, "STOPLOSS")

#         if success:
#             trade["order_id"] = order_id
#             pnl_points = (price - entry) if side == "CALL" else (entry - price)
#             trade["pnl"] += pnl_points * qty
#             info["total_pnl"] = info["call_buy"]["pnl"] + info["put_buy"]["pnl"]
#             trade["trade_flag"] = 0
#             trade["quantity"] = 0
#             trade["filled_df"].loc[dt.now(time_zone)] = [
#                 symbol, price, "SELL", trade["current_stop_price"],
#                 trade.get("full_target_price", 0), spot_price, qty
#             ]
#             update_order_status(order_id, "PENDING", qty, price, symbol)
#         return

#     # --- Partial Profit Booking (side-aware) ---
#     partial_hit = (side == "CALL" and price >= trade["partial_target_price"]) or \
#                   (side == "PUT"  and price <= trade["partial_target_price"])
#     if not trade.get("partial_booked", False) and partial_hit:
#         # For breakout trades, book 40% instead of 50% to keep more size for the run
#         book_ratio = 0.4 if reason == "CONSOLIDATION_BREAKOUT" else 0.5
#         book_qty = max(1, int(qty * book_ratio))

#         if account_type.lower() == "paper":
#             success, order_id = send_paper_exit_order(trade["option_name"], book_qty, "PARTIAL")
#         else:
#             success, order_id = send_live_exit_order(trade["option_name"], book_qty, "PARTIAL")

#         if success:
#             trade["order_id"] = order_id
#             pnl_points = (price - entry) if side == "CALL" else (entry - price)
#             trade["pnl"] += pnl_points * book_qty
#             info["total_pnl"] = info["call_buy"]["pnl"] + info["put_buy"]["pnl"]
#             trade["quantity"] -= book_qty
#             trade["partial_booked"] = True

#             # Move SL to cost immediately (already optimal for breakout protection)
#             trade["current_stop_price"] = entry
#             trade["filled_df"].loc[dt.now(time_zone)] = [
#                 symbol, price, "SELL", trade["current_stop_price"],
#                 trade.get("full_target_price", 0), spot_price, book_qty
#             ]
#             update_order_status(order_id, "PENDING", book_qty, price, symbol)

#     # --- Full Target Check (side-aware) ---
#     full_hit = (side == "CALL" and price >= trade["full_target_price"]) or \
#                (side == "PUT"  and price <= trade["full_target_price"])
#     if full_hit:
#         # Optional: for breakout trades, allow a 'momentum hold' if momentum is strong
#         momentum_hold = False
#         if reason == "CONSOLIDATION_BREAKOUT":
#             mom_ok, mom_val = momentum_ok(candles_3m, side)
#             # If momentum is still strong, skip immediate full exit once to trail tighter
#             momentum_hold = mom_ok and mom_val >= 30  # threshold aligned with expiry overrides

#         if not momentum_hold:
#             if account_type.lower() == "paper":
#                 success, order_id = send_paper_exit_order(trade["option_name"], trade["quantity"], "TARGET")
#             else:
#                 success, order_id = send_live_exit_order(trade["option_name"], trade["quantity"], "TARGET")

#             if success:
#                 trade["order_id"] = order_id
#                 pnl_points = (price - entry) if side == "CALL" else (entry - price)
#                 qty_exit = trade["quantity"]
#                 trade["pnl"] += pnl_points * qty_exit
#                 info["total_pnl"] = info["call_buy"]["pnl"] + info["put_buy"]["pnl"]
#                 trade["trade_flag"] = 0
#                 trade["quantity"] = 0
#                 trade["filled_df"].loc[dt.now(time_zone)] = [
#                     symbol, price, "SELL", trade["current_stop_price"],
#                     trade.get("full_target_price", 0), spot_price, qty_exit
#                 ]
#                 update_order_status(order_id, "PENDING", qty_exit, price, symbol)
#             return
#         else:
#             # Momentum hold: tighten trailing aggressively and let it run
#             # Reduce trail_step by 25% to lock gains faster
#             trade["trail_step_points"] = max(4, int(trade["trail_step_points"] * 0.75))
#             logging.info(f"{YELLOW}[MOMENTUM HOLD] {symbol} trail_step tightened to {trade['trail_step_points']}{RESET}")

#     # --- Trailing logic (inline helper) ---
#     if trade.get("partial_booked", False):
#         pnl_points = (price - entry) if side == "CALL" else (entry - price)

#         if pnl_points >= trade["trail_start_pnl"]:
#             steps = int((pnl_points - trade["trail_start_pnl"]) / trade["trail_step_points"]) + 1
#             new_stop = entry + (steps * trade["trail_step_points"]) if side == "CALL" \
#                        else entry - (steps * trade["trail_step_points"])

#             if (side == "CALL" and new_stop > trade["current_stop_price"]) or \
#                (side == "PUT"  and new_stop < trade["current_stop_price"]):
#                 trade["current_stop_price"] = round(new_stop, 2)
#                 logging.info(f"{YELLOW}[TRAIL STOP UPDATE] {symbol} new SL={trade['current_stop_price']:.2f}{RESET}")

#     # --- MTM Logging ---
#     logging.info(f"{'Paper' if account_type.lower() == 'paper' else 'Live'} MTM {side} {symbol} "
#                  f"LTP={price:.2f} Entry={entry:.2f}")

def apply_signal_to_trade(side, symbol, signal_payload, info, *, option_name, quantity, buy_price, time_zone):
    """
    Maps detect_signal payload into info[...] fields consistently.
    Sets SL/PT/TG, bar_index, hold_grace_bars, and initializes trail config.
    """

    leg = "call_buy" if side == "CALL" else "put_buy"
    trade = info[leg]

    trade.update({
        "option_name": option_name,
        "quantity": int(quantity),
        "buy_price": float(buy_price),
        "trade_flag": 1,
        "reason": signal_payload.get("reason", ""),
        "current_stop_price": float(signal_payload["stop_loss"]),
        "partial_target_price": float(signal_payload["target1"]),
        "full_target_price": float(signal_payload["target2"]),
        "bar_index": signal_payload.get("bar_index"),
        "hold_grace_bars": int(signal_payload.get("hold_grace_bars", 0)),
        "partial_booked": False,
        "pnl": 0.0,
        # Trail config (tunable)
        "trail_start_pnl": float(signal_payload.get("trail_start_pnl", 10.0)),
        "trail_step_points": float(signal_payload.get("trail_step_points", 5.0)),
        "partial_book_ratio": float(signal_payload.get("partial_book_ratio", 0.5)),
        # Filled ledger
        "filled_df": trade.get("filled_df"),
    })

    # Audit entry
    trade["filled_df"].loc[dt.now(time_zone)] = [
        symbol, buy_price, "BUY", trade["current_stop_price"],
        trade["full_target_price"], None, trade["quantity"]
    ]

def process_order(side, symbol, price, info, hist_data, *, spot_price=None, time_zone=None):
    """
    MTM loop honoring:
      - Hold-grace: skip SL on the entry bar (from detect_signal meta)
      - Cooldown handoff: store last_signal_bar_index for next detect_signal()
      - Partial booking -> move SL to cost, then trail by configured steps
      - Clean audit logging and broker/paper exits
    """

    leg = "call_buy" if side == "CALL" else "put_buy"
    trade = info[leg]

    if trade.get("trade_flag", 0) != 1:
        return

    entry = trade["buy_price"]
    qty   = trade["quantity"]
    entry_bar_index = trade.get("bar_index")
    hold_grace_bars = int(trade.get("hold_grace_bars", 0))

    # Grace: skip SL on entry bar
    try:
        current_bar_index = hist_data.index[-1]
        if entry_bar_index is not None and current_bar_index == entry_bar_index and hold_grace_bars > 0:
            logging.info(f"[GRACE] Skipping SL on entry bar ({current_bar_index}); hold_grace_bars={hold_grace_bars}")
        else:
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
                    update_order_status(order_id, "PENDING", qty, price, symbol)
                return
    except Exception as e:
        logging.warning(f"[GRACE CHECK] Failed to evaluate grace: {e}")

    # Partial booking
    partial_hit = (side == "CALL" and price >= trade["partial_target_price"]) or \
                  (side == "PUT"  and price <= trade["partial_target_price"])
    if not trade.get("partial_booked", False) and partial_hit:
        book_ratio = float(trade.get("partial_book_ratio", 0.5))
        book_qty = max(1, int(qty * book_ratio))

        if account_type.lower() == "paper":
            success, order_id = send_paper_exit_order(trade["option_name"], book_qty, "PARTIAL")
        else:
            success, order_id = send_live_exit_order(trade["option_name"], book_qty, "PARTIAL")

        if success:
            trade["order_id"] = order_id
            pnl_points = (price - entry) if side == "CALL" else (entry - price)
            trade["pnl"] += pnl_points * book_qty
            info["total_pnl"] = info["call_buy"]["pnl"] + info["put_buy"]["pnl"]
            trade["quantity"] -= book_qty
            trade["partial_booked"] = True
            trade["current_stop_price"] = entry  # move SL to cost

            trade["filled_df"].loc[dt.now(time_zone)] = [
                symbol, price, "SELL", trade["current_stop_price"],
                trade.get("full_target_price", 0), spot_price, book_qty
            ]
            update_order_status(order_id, "PENDING", book_qty, price, symbol)

    # Full target
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
            update_order_status(order_id, "PENDING", qty_exit, price, symbol)
        return

    # Trailing after partial
    if trade.get("partial_booked", False):
        pnl_points = (price - entry) if side == "CALL" else (entry - price)
        start = float(trade.get("trail_start_pnl", 0))
        step = float(trade.get("trail_step_points", 0))
        if step > 0 and pnl_points >= start:
            steps = int((pnl_points - start) / step) + 1
            new_stop = entry + (steps * step) if side == "CALL" else entry - (steps * step)
            if (side == "CALL" and new_stop > trade["current_stop_price"]) or \
               (side == "PUT"  and new_stop < trade["current_stop_price"]):
                trade["current_stop_price"] = round(new_stop, 2)
                logging.info(f"{YELLOW}[TRAIL STOP UPDATE] {symbol} new SL={trade['current_stop_price']:.2f}{RESET}")

    # MTM log
    logging.info(f"{'Paper' if account_type.lower() == 'paper' else 'Live'} MTM {side} {symbol} "
                 f"LTP={price:.2f} Entry={entry:.2f}")

    # Cooldown handoff
    info["last_signal_bar_index"] = entry_bar_index


# ================================================= Paper Trading =====================================================================

# def paper_order():
#     global quantity, paper_info, df, spot_price, last_signal_candle_time

#     ct = dt.now(time_zone)

#     # 1. Refresh spot price
#     try:
#         quote = fyers.quotes(data={"symbols": ticker})
#         spot_price = quote["d"][0]["v"]["lp"]
#         logging.info(f"Spot={spot_price}")
#     except Exception as e:
#         logging.warning(f"[PAPER] Spot fetch failed: {e}")

#     # 2. EOD FORCE EXIT
#     if ct > end_time:
#         logging.info("[PAPER] End time reached, closing open positions")
#         for leg, side in [("call_buy", "CALL"), ("put_buy", "PUT")]:
#             if paper_info[leg]["trade_flag"] == 1:
#                 name = paper_info[leg]["option_name"]
#                 qty  = paper_info[leg]["quantity"]
#                 exit_price = df.loc[name, "ltp"] if name in df.index else spot_price
#                 paper_info[leg]["trade_flag"] = 2
#                 paper_info[leg]["quantity"] = 0
#                 paper_info[leg]["filled_df"].loc[ct] = [name, exit_price, "SELL", 0, 0, spot_price, 0]
#                 logging.info(f"{RED}[EXIT][PAPER] {side} {name} Qty={qty} Price={exit_price}{RESET}")
#         return

#     # 3. SIGNAL EVALUATION (new 3M candle only)
#     result = None
#     if not candles_3m.empty:
#         last_candle_time = candles_3m.iloc[-1]["time"]
#         if last_signal_candle_time != last_candle_time:
#             last_signal_candle_time = last_candle_time
#             atr, atr_source = resolve_atr(candles_3m, daily_atr)
#             logging.info(f"{YELLOW}[SIGNAL EVAL][PAPER] candle={last_candle_time} candles={len(candles_3m)} atr={atr} source={atr_source}{RESET}")

#             prev_day = hist_data.iloc[-1]
#             cpr  = calculate_cpr(prev_day["high"], prev_day["low"], prev_day["close"])
#             trad = calculate_traditional_pivots(prev_day["high"], prev_day["low"], prev_day["close"])
#             cam  = calculate_camarilla_pivots(prev_day["high"], prev_day["low"], prev_day["close"])
#             prev_day_levels = {"high": prev_day["high"], "low": prev_day["low"]}

#             # detect_signal returns (signal_tuple, breakout_meta) or None
#             result = detect_signal(cpr, trad, cam, atr, candles_3m, prev_day_levels)

#     # 4. PAPER ENTRY LOGIC
#     if result:
#         signal, breakout_meta = result
#         side, reason = signal
#         logging.info(f"{GREEN}[SIGNAL][PAPER] {side} ({reason}) at spot={spot_price}{RESET}")

#         # Block if existing trade active
#         if paper_info["call_buy"]["trade_flag"] == 1 or paper_info["put_buy"]["trade_flag"] == 1:
#             logging.info(f"{MAGENTA}[ENTRY BLOCKED][PAPER] Existing trade active, skipping new signal{RESET}")
#             return

#         # Block if max trades reached
#         if paper_info.get("trade_count", 0) >= MAX_TRADES_PER_DAY:
#             logging.info(f"{MAGENTA}[ENTRY][PAPER] Max trades reached{RESET}")
#             return

#         leg = "call_buy" if side == "CALL" else "put_buy"
#         opt_type = "CE" if side == "CALL" else "PE"
#         opt_name, _ = get_option_by_moneyness(
#             spot_price, opt_type,
#             moneyness=CALL_MONEYNESS if side == "CALL" else PUT_MONEYNESS
#         )

#         ltp = df.loc[opt_name, "ltp"] if (opt_name and opt_name in df.index) else None
#         if ltp is None or pd.isna(ltp):
#             logging.warning(f"{MAGENTA}[PAPER ENTRY] No LTP for {opt_name}, skipping entry{RESET}")
#         else:
#             entry_price = ltp

#             # Fixed + Dynamic levels with breakout-aware tuning
#             prev_day = hist_data.iloc[-1]
#             stop, full_target, partial_target, trail_start, trail_step = build_dynamic_levels(
#                 entry_price, side, prev_day=prev_day, method="auto",
#                 reason=reason, breakout_meta=breakout_meta
#             )

#             if breakout_meta:
#                 logging.info(f"{CYAN}[BREAKOUT META] win={breakout_meta.get('win')} "
#                              f"rngATR={breakout_meta.get('range_atr_ratio'):.2f} "
#                              f"upper={breakout_meta.get('upper'):.2f} lower={breakout_meta.get('lower'):.2f}{RESET}")

#             paper_info[leg].update({
#                 "option_name": opt_name,
#                 "quantity": quantity,
#                 "buy_price": entry_price,
#                 "current_stop_price": stop,
#                 "full_target_price": full_target,
#                 "partial_target_price": partial_target,
#                 "trail_start_pnl": trail_start,
#                 "trail_step_points": trail_step,
#                 "trade_flag": 1,
#                 "partial_booked": False,
#                 "pnl": 0,
#                 "reason": reason,
#                 "breakout_meta": breakout_meta,
#                 "entry_time": ct,
#             })
#             paper_info[leg]["filled_df"].loc[ct] = [
#                 opt_name, entry_price, "BUY", stop, full_target, spot_price, quantity
#             ]
#             paper_info["trade_count"] = paper_info.get("trade_count", 0) + 1
#             logging.info(f"{GREEN}[{side} ENTRY CONFIRMED][PAPER] {opt_name} @ {entry_price:.2f} "
#                          f"SL={stop:.2f} PT={partial_target:.2f} TG={full_target:.2f}{RESET}")

#     # 5. TRAILING STOP + EXIT MANAGEMENT
#     for leg, side in [("call_buy", "CALL"), ("put_buy", "PUT")]:
#         if paper_info[leg]["trade_flag"] != 1:
#             continue
#         name = paper_info[leg]["option_name"]
#         price = df.loc[name, "ltp"] if name in df.index else None
#         if price is None or pd.isna(price):
#             continue
#         process_order(side, name, price, paper_info, hist_data)

#     # 6. SAVE TRADES
#     frames = [paper_info["call_buy"]["filled_df"], paper_info["put_buy"]["filled_df"]]
#     frames = [f for f in frames if not f.empty]
#     if frames:
#         combined = pd.concat(frames)
#         combined.to_csv(f"trades_{strategy_name}_{dt.now(time_zone).date()}_PAPER.csv")
#     store(paper_info, account_type)

def paper_order():
    global quantity, paper_info, df, spot_price, last_signal_candle_time

    ct = dt.now(time_zone)

    # 1. Refresh spot price
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
                exit_price = df.loc[name, "ltp"] if name in df.index else spot_price
                paper_info[leg]["trade_flag"] = 2
                paper_info[leg]["quantity"] = 0
                paper_info[leg]["filled_df"].loc[ct] = [name, exit_price, "SELL", 0, 0, spot_price, 0]
                logging.info(f"{RED}[EXIT][PAPER] {side} {name} Qty={qty} Price={exit_price}{RESET}")
        return

    # 3. SIGNAL EVALUATION (new 3M candle only)
    result = None
    if not candles_3m.empty:
        last_candle_time = candles_3m.iloc[-1]["time"]
        if last_signal_candle_time != last_candle_time:
            last_signal_candle_time = last_candle_time
            atr, atr_source = resolve_atr(candles_3m, daily_atr)
            logging.info(f"{YELLOW}[SIGNAL EVAL][PAPER] candle={last_candle_time} candles={len(candles_3m)} atr={atr} source={atr_source}{RESET}")

            prev_day = hist_data.iloc[-1]
            cpr  = calculate_cpr(prev_day["high"], prev_day["low"], prev_day["close"])
            trad = calculate_traditional_pivots(prev_day["high"], prev_day["low"], prev_day["close"])
            cam  = calculate_camarilla_pivots(prev_day["high"], prev_day["low"], prev_day["close"])

            # detect_signal returns (side, meta) or None
            result = detect_signal(cpr, trad, cam, atr, candles_3m)

    # 4. PAPER ENTRY LOGIC
    if result:
        side, meta = result
        reason = meta["reason"]
        logging.info(f"{GREEN}[SIGNAL][PAPER] {side} ({reason}) at spot={spot_price}{RESET}")

        # Block if existing trade active
        if paper_info["call_buy"]["trade_flag"] == 1 or paper_info["put_buy"]["trade_flag"] == 1:
            logging.info(f"{MAGENTA}[ENTRY BLOCKED][PAPER] Existing trade active, skipping new signal{RESET}")
            return

        # Block if max trades reached
        if paper_info.get("trade_count", 0) >= MAX_TRADES_PER_DAY:
            logging.info(f"{MAGENTA}[ENTRY][PAPER] Max trades reached{RESET}")
            return

        leg = "call_buy" if side == "CALL" else "put_buy"
        opt_type = "CE" if side == "CALL" else "PE"
        opt_name, _ = get_option_by_moneyness(
            spot_price, opt_type,
            moneyness=CALL_MONEYNESS if side == "CALL" else PUT_MONEYNESS
        )

        ltp = df.loc[opt_name, "ltp"] if (opt_name and opt_name in df.index) else None
        if ltp is None or pd.isna(ltp):
            logging.warning(f"{MAGENTA}[PAPER ENTRY] No LTP for {opt_name}, skipping entry{RESET}")
        else:
            entry_price = ltp

            # Use pivot-anchored SL/PT/TG from meta
            stop        = entry_price - (0.0) if side == "CALL" else entry_price + (0.0)  # placeholder for symmetry
            stop        = meta["stop_loss"]    # store as option-level SL; if you prefer spot-level, map here
            full_target = meta["target2"]
            partial_target = meta["target1"]

            # Trail config (simple defaults; can be tuned per pattern score)
            trail_start = max(4, int(0.5 * abs(full_target - entry_price)))
            trail_step  = max(2, int(0.25 * trail_start))

            paper_info[leg].update({
                "option_name": opt_name,
                "quantity": quantity,
                "buy_price": entry_price,
                "current_stop_price": stop,
                "full_target_price": full_target,
                "partial_target_price": partial_target,
                "trail_start_pnl": trail_start,
                "trail_step_points": trail_step,
                "trade_flag": 1,
                "partial_booked": False,
                "pnl": 0,
                "reason": reason,
                "score": meta.get("score"),
                "entry_time": ct,
            })
            paper_info[leg]["filled_df"].loc[ct] = [
                opt_name, entry_price, "BUY", stop, full_target, spot_price, quantity
            ]
            paper_info["trade_count"] = paper_info.get("trade_count", 0) + 1
            logging.info(f"{GREEN}[{side} ENTRY CONFIRMED][PAPER] {opt_name} @ {entry_price:.2f} "
                         f"SL={stop:.2f} PT={partial_target:.2f} TG={full_target:.2f}{RESET}")

    # 5. TRAILING STOP + EXIT MANAGEMENT
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
        combined.to_csv(f"trades_{strategy_name}_{dt.now(time_zone).date()}_PAPER.csv")
    store(paper_info, account_type)


# =============================== Live Trading =======================================

# def real_order():
#     global quantity, live_info, df, spot_price, last_signal_candle_time

#     ct = dt.now(time_zone)

#     # 1. Refresh spot price
#     try:
#         quote = fyers.quotes(data={"symbols": ticker})
#         spot_price = quote["d"][0]["v"]["lp"]
#         logging.info(f"Spot={spot_price}")
#     except Exception as e:
#         logging.warning(f"[LIVE] Spot fetch failed: {e}")

#     # 2. EOD FORCE EXIT
#     if ct > end_time:
#         logging.info("[LIVE] End time reached, closing open positions")
#         for leg, side in [("call_buy", "CALL"), ("put_buy", "PUT")]:
#             if live_info[leg]["trade_flag"] == 1:
#                 name = live_info[leg]["option_name"]
#                 qty  = live_info[leg]["quantity"]
#                 success, order_id = send_live_exit_order(name, qty, "EOD")
#                 if success:
#                     live_info[leg]["trade_flag"] = 2
#                     live_info[leg]["quantity"] = 0
#                     exit_price = df.loc[name, "ltp"] if name in df.index else spot_price
#                     live_info[leg]["filled_df"].loc[ct] = [name, exit_price, "SELL", 0, 0, spot_price, 0]
#                     logging.info(f"{RED}[EXIT][LIVE] {side} {name} Qty={qty} Price={exit_price}{RESET}")
#         return

#     # 3. SIGNAL EVALUATION (new 3M candle only)
#     result = None
#     if not candles_3m.empty:
#         last_candle_time = candles_3m.iloc[-1]["time"]
#         if last_signal_candle_time != last_candle_time:
#             last_signal_candle_time = last_candle_time
#             atr, atr_source = resolve_atr(candles_3m, daily_atr)
#             logging.info(f"{YELLOW}[SIGNAL EVAL][LIVE] candle={last_candle_time} candles={len(candles_3m)} atr={atr} source={atr_source}{RESET}")

#             prev_day = hist_data.iloc[-1]
#             cpr  = calculate_cpr(prev_day["high"], prev_day["low"], prev_day["close"])
#             trad = calculate_traditional_pivots(prev_day["high"], prev_day["low"], prev_day["close"])
#             cam  = calculate_camarilla_pivots(prev_day["high"], prev_day["low"], prev_day["close"])
#             prev_day_levels = {"high": prev_day["high"], "low": prev_day["low"]}

#             # detect_signal returns (signal_tuple, breakout_meta) or None
#             result = detect_signal(cpr, trad, cam, atr, candles_3m, prev_day_levels)

#     # 4. LIVE ENTRY LOGIC
#     if result:
#         signal, breakout_meta = result
#         side, reason = signal
#         logging.info(f"{GREEN}[SIGNAL][LIVE] {side} ({reason}) at spot={spot_price}{RESET}")

#         if live_info["call_buy"]["trade_flag"] == 1 or live_info["put_buy"]["trade_flag"] == 1:
#             logging.info(f"{MAGENTA}[ENTRY BLOCKED][LIVE] Existing trade active, skipping new signal{RESET}")
#             return
#         if live_info.get("trade_count", 0) >= MAX_TRADES_PER_DAY:
#             logging.info(f"{MAGENTA}[ENTRY][LIVE] Max trades reached{RESET}")
#             return

#         leg = "call_buy" if side == "CALL" else "put_buy"
#         opt_type = "CE" if side == "CALL" else "PE"
#         opt_name, _ = get_option_by_moneyness(
#             spot_price, opt_type,
#             moneyness=CALL_MONEYNESS if side == "CALL" else PUT_MONEYNESS
#         )

#         ltp = df.loc[opt_name, "ltp"] if (opt_name and opt_name in df.index) else None
#         if ltp is None or pd.isna(ltp):
#             logging.warning(f"{MAGENTA}[LIVE ENTRY] No LTP for {opt_name}, skipping entry{RESET}")
#         else:
#             # Place live LIMIT order
#             success, order_id = send_live_entry_order(opt_name, quantity, side)
#             if not success:
#                 logging.warning(f"{RED}[LIVE ENTRY] Failed to place {side} for {opt_name}{RESET}")
#             else:
#                 live_info[leg].update({
#                     "option_name": opt_name,
#                     "quantity": quantity,
#                     "order_type": "LIMIT",
#                     "trade_flag": 0,   # pending until filled
#                     "order_id": order_id,
#                     "reason": reason,
#                     "breakout_meta": breakout_meta,
#                     "entry_time": ct,
#                 })
#                 logging.info(f"{YELLOW}[LIVE ENTRY PENDING] {side} {opt_name} OrderID={order_id}{RESET}")

#                 # Poll broker until filled
#                 status, filled_price = check_order_status(order_id, fyers)
#                 if status == "TRADED":
#                     # Build breakout-aware levels
#                     sl, full_target, partial_target, trail_start, trail_step = build_dynamic_levels(
#                         filled_price, side, prev_day=prev_day, method="auto",
#                         reason=reason, breakout_meta=breakout_meta
#                     )

#                     if breakout_meta:
#                         logging.info(f"{CYAN}[BREAKOUT META] win={breakout_meta.get('win')} "
#                                      f"rngATR={breakout_meta.get('range_atr_ratio'):.2f} "
#                                      f"upper={breakout_meta.get('upper'):.2f} lower={breakout_meta.get('lower'):.2f}{RESET}")

#                     live_info[leg].update({
#                         "buy_price": filled_price,
#                         "current_stop_price": sl,
#                         "full_target_price": full_target,
#                         "partial_target_price": partial_target,
#                         "trail_start_pnl": trail_start,
#                         "trail_step_points": trail_step,
#                         "trade_flag": 1,
#                         "partial_booked": False,
#                         "pnl": 0,
#                     })
#                     live_info[leg]["filled_df"].loc[ct] = [
#                         opt_name, filled_price, "BUY", sl, full_target, spot_price, quantity
#                     ]
#                     live_info["trade_count"] = live_info.get("trade_count", 0) + 1
#                     logging.info(f"{GREEN}[{side} ENTRY CONFIRMED][LIVE] {opt_name} @ {filled_price:.2f} SL={sl:.2f} PT={partial_target:.2f} TG={full_target:.2f}{RESET}")
#                 elif status == "PENDING":
#                     logging.info(f"{YELLOW}[LIVE ENTRY STILL PENDING] {side} {opt_name} OrderID={order_id}{RESET}")
#                 elif status == "CANCELLED":
#                     logging.warning(f"{MAGENTA}[LIVE ENTRY CANCELLED] {side} {opt_name} OrderID={order_id}{RESET}")
#                     live_info[leg]["trade_flag"] = 0
#                     live_info[leg]["order_id"] = None

#     # 5. TRAILING STOP + EXIT MANAGEMENT
#     for leg, side in [("call_buy", "CALL"), ("put_buy", "PUT")]:
#         if live_info[leg]["trade_flag"] != 1:
#             continue
#         name = live_info[leg]["option_name"]
#         price = df.loc[name, "ltp"] if name in df.index else None
#         if price is None or pd.isna(price):
#             continue
#         process_order(side, name, price, live_info, hist_data)

#     # 6. SAVE TRADES
#     frames = [live_info["call_buy"]["filled_df"], live_info["put_buy"]["filled_df"]]
#     frames = [f for f in frames if not f.empty]
#     if frames:
#         combined = pd.concat(frames)
#         combined.to_csv(f"trades_{strategy_name}_{dt.now(time_zone).date()}.csv")
#     store(live_info, account_type)
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
    result = None
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

            # detect_signal returns (side, meta) or None
            result = detect_signal(cpr, trad, cam, atr, candles_3m)

    # 4. LIVE ENTRY LOGIC
    if result:
        side, meta = result
        reason = meta["reason"]
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
                    "score": meta.get("score"),
                    "entry_time": ct,
                })
                logging.info(f"{YELLOW}[LIVE ENTRY PENDING] {side} {opt_name} OrderID={order_id}{RESET}")

                # Poll broker until filled
                status, filled_price = check_order_status(order_id, fyers)
                if status == "TRADED":
                    # Use pivot-anchored SL/PT/TG from meta
                    sl            = meta["stop_loss"]
                    full_target   = meta["target2"]
                    partial_target= meta["target1"]

                    # Trail config (simple defaults; can be tuned per pattern score)
                    trail_start = max(4, int(0.5 * abs(full_target - filled_price)))
                    trail_step  = max(2, int(0.25 * trail_start))

                    live_info[leg].update({
                        "buy_price": filled_price,
                        "current_stop_price": sl,
                        "full_target_price": full_target,
                        "partial_target_price": partial_target,
                        "trail_start_pnl": trail_start,
                        "trail_step_points": trail_step,
                        "trade_flag": 1,
                        "partial_booked": False,
                        "pnl": 0,
                    })
                    live_info[leg]["filled_df"].loc[ct] = [
                        opt_name, filled_price, "BUY", sl, full_target, spot_price, quantity
                    ]
                    live_info["trade_count"] = live_info.get("trade_count", 0) + 1
                    logging.info(f"{GREEN}[{side} ENTRY CONFIRMED][LIVE] {opt_name} @ {filled_price:.2f} SL={sl:.2f} PT={partial_target:.2f} TG={full_target:.2f}{RESET}")
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