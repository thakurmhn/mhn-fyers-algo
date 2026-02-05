# ===== execution.py =====
import logging
import pickle
import pandas as pd
import pendulum as dt
from fyers_apiv3 import fyersModel
import time
from config import (
    time_zone, strategy_name, MAX_TRADES_PER_DAY, account_type, quantity,
    CALL_MONEYNESS, PUT_MONEYNESS, profit_loss_point, ENTRY_OFFSET, ORDER_TYPE,
    MAX_DAILY_LOSS, MAX_DRAWDOWN, OSCILLATOR_EXIT_MODE, symbols
)
from setup import (
    df, fyers, ticker, option_chain, spot_price,
    start_time, end_time, hist_data
)
from indicators import (
    calculate_cpr,
    calculate_traditional_pivots,
    calculate_camarilla_pivots,
    resolve_atr,
    daily_atr,
    oscillator_exit_trigger,
    oscillator_entry_filter
)

from signals import detect_signal, evaluate_candle

from candle_builder import build_3min_candle, build_15m_candles, get_today_15m_candles
from signals import detect_signal, evaluate_candle
from tickdb import tick_db
from orchestration import update_candles_and_signals


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

# # ===== Persistence =====
# def store(data, account_type_):
#     try:
#         pickle.dump(data, open(f'data-{dt.now(time_zone).date()}-{account_type_}.pickle', 'wb'))
#     except Exception as e:
#         logging.error(f"Failed to store state: {e}")

# def load(account_type_):
#     try:
#         return pickle.load(open(f'data-{dt.now(time_zone).date()}-{account_type_}.pickle', 'rb'))
#     except Exception as e:
#         logging.warning(f"State load failed (fresh start): {e}")
#         raise

# ===== Persistence with Ledger =====

def store(data, account_type_):
    """
    Append trading state to a ledger stored in a pickle file.
    Each call adds a new snapshot to the list instead of overwriting.
    Compatible with both legacy dict and new ledger format.
    """
    filename = f"data-{dt.now(time_zone).date()}-{account_type_}.pickle"
    try:
        # Try to load existing ledger
        try:
            with open(filename, "rb") as f:
                ledger = pickle.load(f)

            # Normalize legacy formats
            if isinstance(ledger, dict):
                # Old format: single dict snapshot
                ledger = [ledger]
            elif not isinstance(ledger, list):
                # Unexpected format: reset to empty list
                ledger = []
        except Exception:
            ledger = []

        # Append current snapshot with timestamp + state
        snapshot = {
            "timestamp": dt.now(time_zone),
            "state": data
        }
        ledger.append(snapshot)

        # Save back to pickle
        with open(filename, "wb") as f:
            pickle.dump(ledger, f, protocol=pickle.HIGHEST_PROTOCOL)

    except Exception as e:
        logging.error(f"Failed to store state: {e}")


def load(account_type_):
    """
    Load the full ledger from pickle file.
    Returns a list of snapshots (each with timestamp + state).
    """
    filename = f"data-{dt.now(time_zone).date()}-{account_type_}.pickle"
    try:
        with open(filename, "rb") as f:
            ledger = pickle.load(f)
        return ledger
    except Exception as e:
        logging.warning(f"State load failed (fresh start): {e}")
        raise

def get_option_by_moneyness(spot_price_, side, moneyness='ITM', points=0):
    """
    Select ITM option strike with strike_diff points inside ATM.
    Jan 8th baseline: always ITM with 100-point difference.
    CALL: ATM - strike_diff
    PUT:  ATM + strike_diff
    """

    from config import strike_diff

    if spot_price_ is None or pd.isna(spot_price_):
        logging.error("[get_option_by_moneyness] Invalid spot price")
        return None, None

    # Normalize side to CE/PE
    side = "CE" if side in ["CALL", "CE"] else "PE"

    # Round to nearest strike
    atm_strike = round(spot_price_ / strike_diff) * strike_diff

    if side == "CE":  # CALL
        strike = atm_strike - strike_diff
    else:             # PUT
        strike = atm_strike + strike_diff

    # Apply any manual offset
    strike += points

    # Debug logging
    logging.info(
        f"[DEBUG get_option_by_moneyness] spot={spot_price_}, atm={atm_strike}, "
        f"side={side}, requested_strike={strike}"
    )

    sel = option_chain[
        (option_chain['strike_price'] == strike) &
        (option_chain['option_type'].isin([side, side.replace('E','ALL')]))  # CE/PE or CALL/PUT
    ]['symbol']

    if sel.empty:
        side_df = option_chain[option_chain['option_type'].isin([side, side.replace('E','ALL')])].copy()
        if side_df.empty:
            logging.error(f"[get_option_by_moneyness] No options available for side={side}")
            return None, None
        side_df['strike_diff_abs'] = (side_df['strike_price'] - strike).abs()
        side_df = side_df.sort_values('strike_diff_abs')
        symbol = side_df.iloc[0]['symbol']
        strike = side_df.iloc[0]['strike_price']
        logging.warning(
            f"[get_option_by_moneyness] Fallback ITM for {side}: requested {strike}, using nearest available"
        )
        return symbol, strike

    return sel.squeeze(), strike

# Dynamic ATR based SL/PT/TG 

def build_dynamic_levels(entry_price, atr, rr_ratio=2.0, profit_loss_point=5):
    """
    Build stop-loss, partial/full targets, and trailing parameters.
    Long-only logic (works for both CALL and PUT).
    Adaptive ATR thresholds:
      - Non-expiry days (Wed–Mon except Tuesday)
      - Expiry regime (Tuesday)
    """

    weekday = dt.now(time_zone).weekday()  # Monday=0 ... Sunday=6
    is_expiry_day = (weekday == 1)  # Tuesday

    # ---- Decision: Normal / Volatile / Extreme ----
    if is_expiry_day:
        if atr <= 40:
            mode = "normal"
        elif atr <= 180:
            mode = "volatile"
        else:
            mode = "extreme"
    else:
        if atr <= 25:
            mode = "normal"
        elif atr <= 120:
            mode = "volatile"
        else:
            mode = "extreme"

    if mode == "normal":
        risk_points   = max(profit_loss_point, atr * 0.25)
        reward_points = risk_points * rr_ratio

        stop  = entry_price - risk_points
        partial_target = entry_price + reward_points / 2
        full_target    = entry_price + reward_points

        trail_start = reward_points / 2
        trail_step  = max(atr * 0.1, 0.5)

        if is_expiry_day:
            logging.info(
                f"{CYAN}[LEVELS][EXPIRY-NORMAL] ATR={atr:.2f} Risk={risk_points:.2f} Reward={reward_points:.2f}{RESET}"
            )
        else:
            logging.info(
                f"{CYAN}[LEVELS][NORMAL] ATR={atr:.2f} Risk={risk_points:.2f} Reward={reward_points:.2f}{RESET}"
            )

    elif mode == "volatile":
        risk_points   = atr * 0.5
        reward_points = atr * 1.0

        stop  = entry_price - risk_points
        partial_target = entry_price + reward_points * 0.5
        full_target    = entry_price + reward_points

        trail_start = reward_points * 0.25
        trail_step  = max(atr * 0.2, 1.0)

        if is_expiry_day:
            logging.info(
                f"{CYAN}[LEVELS][EXPIRY-VOLATILE] ATR={atr:.2f} Risk={risk_points:.2f} Reward={reward_points:.2f}{RESET}"
            )
        else:
            logging.info(
                f"{CYAN}[LEVELS][VOLATILE] ATR={atr:.2f} Risk={risk_points:.2f} Reward={reward_points:.2f}{RESET}"
            )

    else:
        if is_expiry_day:
            logging.warning(
                f"{CYAN}[LEVELS][EXPIRY-EXTREME] ATR={atr:.2f} → Trade skipped due to excessive expiry-day volatility{RESET}"
            )
        else:
            logging.warning(
                f"{CYAN}[LEVELS][EXTREME] ATR={atr:.2f} → Trade skipped due to excessive volatility{RESET}"
            )
        return None, None, None, None, None

    return stop, partial_target, full_target, trail_start, trail_step



def update_trailing_stop(current_price, entry_price, current_stop, trail_start_pnl, trail_step_points):
    """
    Update trailing stop once partial target booked.
    Long-only logic (works for both CALL and PUT).
    - Ratchets stop upward as option price rises.
    """

    pnl = current_price - entry_price
    if pnl >= trail_start_pnl and trail_step_points > 0:
        candidate = current_price - trail_step_points
        new_stop = max(current_stop, candidate)
        if new_stop != current_stop:
            logging.info(
                f"{YELLOW}[TRAIL UPDATE] Stop moved from {current_stop:.2f} → {new_stop:.2f}{RESET}"
            )
        return new_stop

    return current_stop


# ===== PAPER/LIVE STATE INIT =====

risk_info = {
    "session_pnl": 0,
    "peak_equity": 0,
    "halt_trading": False
}

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
            logging.info(f"{YELLOW}[LIVE ENTRY] {symbol} Qty={qty}{RESET}")
            return True, response.get("id")

        else:
            logging.error(f"{CYAN}[LIVE ENTRY FAILED] {symbol} {response}{RESET}")
            return False, None

    except Exception as e:
        logging.error(f"{CYAN}[LIVE ENTRY ERROR] {symbol} {e}{RESET}")
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
                f"{YELLOW}[LIVE EXIT][{reason}] {symbol} Qty={qty}{RESET}"
                f"OrderID={response.get('id')}{RESET}"
            )
            return True, response.get("id")
        else:
            logging.error(f"{RED}[LIVE EXIT FAILED] {symbol} {response}{RESET}")
            return False, None

    except Exception as e:
        logging.error(f"{RED}{RED}[LIVE EXIT ERROR] {symbol} {e}{RESET}{RESET}")
        return False, None
    
def send_paper_exit_order(symbol, qty, reason):
    """
    Simulated exit for paper mode.
    Baseline logic (8th Jan):
    - Always log the exit with reason and quantity
    - Return success flag and synthetic order_id
    """
    logging.info(f"{CYAN}[PAPER EXIT][{reason}] {symbol} Qty={qty}{RESET}")
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

# =================== Dynamic Order Processing / ATR based SL/PT/TG ==========================

def process_order(side, symbol, price, info, hist_data_3m, hist_data_15m, spot_price, account_type="paper"):
    """
    Manage exits and trailing logic for an active trade.
    Args:
        side        : "CALL" or "PUT"
        symbol      : option symbol string
        price       : current LTP
        info        : trade info dict (paper_info or live_info)
        hist_data_3m: 3m history DataFrame (for SL/targets/trailing confirmation)
        hist_data_15m: 15m history DataFrame (for oscillator exits)
        spot_price  : current spot price
        account_type: "paper" or "live"
    """

    trade = info["call_buy"] if side == "CALL" else info["put_buy"]
    entry = trade["buy_price"]
    qty   = trade["quantity"]

    # Detect expiry regime (Tuesday)
    weekday = dt.now(time_zone).weekday()
    expiry_flag = (weekday == 1)
    regime_tag = "EXPIRY EXIT" if expiry_flag else "VOLATILITY EXIT"

    # --- Stop-loss check (3m candle confirmation) ---
    sl_hit = False
    if hist_data_3m is not None and not hist_data_3m.empty:
        last_candle = hist_data_3m.iloc[-1]
        if last_candle["low"] <= trade.get("current_stop_price", 0):
            sl_hit = True
    else:
        sl_hit = price <= trade.get("current_stop_price", 0)

    if sl_hit and qty > 0:
        success, order_id = (
            send_paper_exit_order(trade["option_name"], qty, "STOPLOSS")
            if account_type.lower() == "paper"
            else send_live_exit_order(trade["option_name"], qty, "STOPLOSS")
        )
        if success:
            pnl_points = price - entry
            pnl_value  = pnl_points * qty
            trade["pnl"] += pnl_value
            info["total_pnl"] = info["call_buy"].get("pnl", 0) + info["put_buy"].get("pnl", 0)
            trade["trade_flag"] = 0
            trade["quantity"] = 0
            trade["filled_df"].loc[dt.now(time_zone)] = [
                symbol, price, "SELL", trade.get("current_stop_price", 0),
                trade.get("full_target_price", 0), spot_price, qty
            ]
            logging.info(
                f"{YELLOW}[EXIT][{account_type.upper()} STOPLOSS][{regime_tag}] LONG {side} {symbol} "
                f"Qty={qty} Entry={entry:.2f} Exit={price:.2f} "
                f"PnL={pnl_value:.2f} (points={pnl_points:.2f}){RESET}"
            )
            update_order_status(order_id, "PENDING", qty, price, symbol)
            return True
        return False

    # --- Oscillator exit check (15m for peak detection) ---
    if hist_data_15m is not None and not hist_data_15m.empty:
        triggered, reason = oscillator_exit_trigger(side, hist_data_15m)
        if triggered:
            if OSCILLATOR_EXIT_MODE == "HARD":
                success, order_id = (
                    send_paper_exit_order(trade["option_name"], qty, "OSCILLATOR")
                    if account_type.lower() == "paper"
                    else send_live_exit_order(trade["option_name"], qty, "OSCILLATOR")
                )
                if success:
                    pnl_points = price - entry
                    pnl_value  = pnl_points * qty
                    trade["pnl"] += pnl_value
                    info["total_pnl"] = info["call_buy"].get("pnl", 0) + info["put_buy"].get("pnl", 0)
                    trade["trade_flag"] = 0
                    trade["quantity"] = 0
                    trade["filled_df"].loc[dt.now(time_zone)] = [
                        symbol, price, "SELL", trade.get("current_stop_price", entry),
                        trade.get("full_target_price", 0), spot_price, qty
                    ]
                    logging.info(
                        f"{YELLOW}[EXIT][{account_type.upper()} OSCILLATOR-HARD][{regime_tag}] LONG {side} {symbol} "
                        f"Qty={qty} Entry={entry:.2f} Exit={price:.2f} "
                        f"PnL={pnl_value:.2f} (points={pnl_points:.2f}) Reason={reason}{RESET}"
                    )
                    update_order_status(order_id, "PENDING", qty, price, symbol)
                    return True
                return False

            elif OSCILLATOR_EXIT_MODE == "TRAIL":
                trade["current_stop_price"] = max(trade.get("current_stop_price", entry), entry)
                logging.info(
                    f"[OSCILLATOR TRAIL][{regime_tag}] {symbol} SL tightened to {trade['current_stop_price']:.2f} "
                    f"Reason={reason}"
                )

    # --- Partial Profit Booking (3m candle confirmation) ---
    partial_hit = False
    if hist_data_3m is not None and not hist_data_3m.empty:
        last_candle = hist_data_3m.iloc[-1]
        if last_candle["high"] >= trade.get("partial_target_price", float("inf")):
            partial_hit = True
    else:
        partial_hit = price >= trade.get("partial_target_price", float("inf"))

    if not trade.get("partial_booked", False) and partial_hit and qty > 0:
        half_qty = qty // 2
        if half_qty > 0:
            success, order_id = (
                send_paper_exit_order(trade["option_name"], half_qty, "PARTIAL")
                if account_type.lower() == "paper"
                else send_live_exit_order(trade["option_name"], half_qty, "PARTIAL")
            )
            if success:
                pnl_points = price - entry
                pnl_value  = pnl_points * half_qty
                trade["pnl"] += pnl_value
                info["total_pnl"] = info["call_buy"].get("pnl", 0) + info["put_buy"].get("pnl", 0)
                trade["quantity"] -= half_qty
                trade["partial_booked"] = True
                trade["current_stop_price"] = entry
                trade["filled_df"].loc[dt.now(time_zone)] = [
                    symbol, price, "SELL", trade.get("current_stop_price", entry),
                    trade.get("full_target_price", 0), spot_price, half_qty
                ]
                logging.info(
                    f"{YELLOW}[EXIT][{account_type.upper()} PARTIAL][{regime_tag}] LONG {side} {symbol} "
                    f"HalfQty={half_qty} Entry={entry:.2f} Exit={price:.2f} "
                    f"PnL={pnl_value:.2f} (points={pnl_points:.2f}){RESET}"
                )
                update_order_status(order_id, "PENDING", half_qty, price, symbol)

   
        # --- Full Target Check (3m candle confirmation) ---
    full_hit = False
    if hist_data_3m is not None and not hist_data_3m.empty:
        last_candle = hist_data_3m.iloc[-1]
        if last_candle["high"] >= trade.get("full_target_price", float("inf")):
            full_hit = True
    else:
        full_hit = price >= trade.get("full_target_price", float("inf"))

    if full_hit and qty > 0:
        success, order_id = (
            send_paper_exit_order(trade["option_name"], qty, "TARGET")
            if account_type.lower() == "paper"
            else send_live_exit_order(trade["option_name"], qty, "TARGET")
        )
        if success:
            pnl_points = price - entry
            pnl_value  = pnl_points * qty
            trade["pnl"] += pnl_value
            info["total_pnl"] = info["call_buy"].get("pnl", 0) + info["put_buy"].get("pnl", 0)
            trade["trade_flag"] = 0
            trade["quantity"] = 0
            trade["filled_df"].loc[dt.now(time_zone)] = [
                symbol, price, "SELL", trade.get("current_stop_price", entry),
                trade.get("full_target_price", 0), spot_price, qty
            ]
            logging.info(
                f"{YELLOW}[EXIT][{account_type.upper()} TARGET][{regime_tag}] LONG {side} {symbol} "
                f"Qty={qty} Entry={entry:.2f} Exit={price:.2f} "
                f"PnL={pnl_value:.2f} (points={pnl_points:.2f}){RESET}"
            )
            update_order_status(order_id, "PENDING", qty, price, symbol)
            return True
        return False

    # --- Trailing Stop Update (3m) ---
    if trade.get("partial_booked", False) and qty > 0:
        new_stop = update_trailing_stop(
            price, entry,
            trade.get("current_stop_price", entry),
            trade.get("trail_start_pnl", 0),
            trade.get("trail_step_points", 0)
        )
        if new_stop != trade.get("current_stop_price", entry):
            trade["current_stop_price"] = new_stop
            logging.info(f"{YELLOW}[TRAIL STOP UPDATE][{regime_tag}] {symbol} new SL={new_stop:.2f}{RESET}")

    # --- MTM Logging ---
    mtm_points = price - entry
    mtm_value  = mtm_points * trade["quantity"]
    logging.info(
        f"{CYAN}{account_type.capitalize()} MTM LONG {side} {symbol} "
        f"LTP={price:.2f} Entry={entry:.2f} Qty={trade['quantity']} "
        f"MTM={mtm_value:.2f} (points={mtm_points:.2f}){RESET}"
    )

    return False

def cleanup_trade_exit(info, leg, side, name, qty, exit_price, mode, reason):
    """
    Unified cleanup for any exit (STOPLOSS, TARGET, PARTIAL, EOD, FORCE).
    Ensures trade_flag reset to 0 so new entries are allowed.
    """
    ct = dt.now(time_zone)
    info[leg]["trade_flag"] = 0        # ✅ always reset
    info[leg]["quantity"] = 0
    info[leg]["filled_df"].loc[ct] = [
        name, exit_price, "SELL", 0, 0, spot_price, qty
    ]
    logging.info(
        f"{RED}[EXIT][{mode}] {side} {name} Qty={qty} Price={exit_price} Reason={reason}{RESET}"
    )

def force_close_old_trades(info, mode):
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
                exit_price = df.loc[name, "ltp"] if name in df.index else spot_price
                cleanup_trade_exit(info, leg, side, name, qty, exit_price, mode, "FORCE_CLEANUP")


def update_risk(trade_info, risk_info):
    """
    Update risk metrics after each exit.
    trade_info: paper_info or live_info dict
    risk_info: session-level dict
    """
    # Calculate cumulative PnL
    total_pnl = sum([
        trade_info["call_buy"].get("pnl", 0),
        trade_info["put_buy"].get("pnl", 0)
    ])
    risk_info["session_pnl"] = total_pnl

    # Update peak equity
    risk_info["peak_equity"] = max(risk_info["peak_equity"], total_pnl)

    # Check daily max loss
    if total_pnl <= MAX_DAILY_LOSS:
        risk_info["halt_trading"] = True
        logging.warning(f"[RISK HALT] Daily loss limit breached: {total_pnl:.2f}")

    # Check drawdown
    if (total_pnl - risk_info["peak_equity"]) <= MAX_DRAWDOWN:
        risk_info["halt_trading"] = True
        logging.warning(
            f"[RISK HALT] Max drawdown breached: {total_pnl:.2f} vs peak {risk_info['peak_equity']:.2f}"
        )

# ===== paper_order =====
def paper_order(candles_3m, hist_yesterday_15m=None):
    global quantity, paper_info, df, spot_price, last_signal_candle_time, risk_info

    COOLDOWN_SECONDS = 180  # 3 minutes
    CONFIDENCE_THRESHOLD = 80  # configurable, move to config.py if preferred

    # --- Safety reset ---
    for leg in ["call_buy", "put_buy"]:
        if paper_info[leg].get("trade_flag", 0) == 2:
            logging.warning(f"{CYAN}[RESET] Found lingering trade_flag=2 for {leg}, resetting to 0{RESET}")
            paper_info[leg]["trade_flag"] = 0
    
    ct = dt.now(time_zone)

    # 1. Refresh spot price (simulated)
    try:
        quote = fyers.quotes(data={"symbols": ticker})
        spot_price = quote["d"][0]["v"]["lp"]
        logging.info(f"[PAPER] Spot={spot_price}")
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
                    exit_price = df.loc[name, "ltp"] if name in df.index else spot_price
                    cleanup_trade_exit(paper_info, leg, side, name, qty, exit_price, "PAPER", "EOD")
                    update_order_status(order_id, "PENDING", qty, exit_price, name)
        return

    # 3. SIGNAL EVALUATION
    signal = None
    if not candles_3m.empty and hist_yesterday_15m is not None and not hist_yesterday_15m.empty:
        last_candle_time = candles_3m.iloc[-1]["time"]
        if last_signal_candle_time != last_candle_time:
            last_signal_candle_time = last_candle_time
            daily_val = daily_atr(hist_yesterday_15m)
            atr, atr_source = resolve_atr(candles_3m, daily_val)
            logging.info(f"{YELLOW}[SIGNAL EVAL][PAPER] candle={last_candle_time} candles={len(candles_3m)} atr={atr} source={atr_source}{RESET}")
            prev_day = hist_yesterday_15m.iloc[-1]
            cpr  = calculate_cpr(prev_day["high"], prev_day["low"], prev_day["close"])
            trad = calculate_traditional_pivots(prev_day["high"], prev_day["low"], prev_day["close"])
            cam  = calculate_camarilla_pivots(prev_day["high"], prev_day["low"], prev_day["close"])
            signal = detect_signal(cpr, trad, cam, candles_3m, hist_yesterday_15m, spot_price=spot_price, daily_atr=daily_val)

    # 4. PAPER ENTRY LOGIC (with confidence filter)
    if signal:
        side, reason, confidence = signal
        logging.info(f"{YELLOW}[SIGNAL][PAPER] {side} ({reason}) confidence={confidence} spot={spot_price}{RESET}")

        if confidence < CONFIDENCE_THRESHOLD:
            logging.info(f"{MAGENTA}[ENTRY BLOCKED][CONFIDENCE] {side} skipped (score={confidence} < {CONFIDENCE_THRESHOLD}){RESET}")
            return

        if risk_info.get("halt_trading", False):
            logging.info(f"{MAGENTA}[ENTRY BLOCKED][RISK] Trading halted due to risk limits{RESET}")
            return

        if paper_info.get("last_exit_time"):
            elapsed = (dt.now(time_zone) - paper_info["last_exit_time"]).total_seconds()
            if elapsed < COOLDOWN_SECONDS:
                logging.info(f"{MAGENTA}[ENTRY BLOCKED][PAPER] Cool-down active ({elapsed:.0f}s < {COOLDOWN_SECONDS}s){RESET}")
                return

        if not oscillator_entry_filter(side, candles_3m):
            logging.info(f"{MAGENTA}[ENTRY BLOCKED][OSC] {side} skipped due to oscillator filter{RESET}")
            return

        leg = "call_buy" if side == "CALL" else "put_buy"
        try:
            if paper_info[leg]["trade_flag"] == 0:
                opt_type = "CE" if side == "CALL" else "PE"
                opt_name, strike = get_option_by_moneyness(
                    spot_price, opt_type,
                    moneyness=CALL_MONEYNESS if side == "CALL" else PUT_MONEYNESS
                )

                if opt_name and opt_name in df.index:
                    entry_price = df.loc[opt_name, "ltp"] or spot_price
                    levels = build_dynamic_levels(entry_price, atr)
                    if any(v is None for v in levels):
                        logging.info(f"{CYAN}[ENTRY SKIPPED][PAPER] {side} ATR regime extreme, no levels built{RESET}")
                        return
                    stop, partial_target, full_target, trail_start, trail_step = levels

                    # Detect expiry regime (Tuesday)
                    weekday = dt.now(time_zone).weekday()
                    expiry_flag = (weekday == 1)  # Tuesday
                    regime_tag = "EXPIRY ENTRY" if expiry_flag else "VOLATILITY ENTRY"

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
                        "confidence": confidence,   # <-- NEW
                        "order_id": f"paper_{opt_name}_{ct}",
                        "entry_time": ct,
                    })

                    paper_info[leg]["filled_df"].loc[ct] = [
                        opt_name, entry_price, "BUY", stop, full_target, spot_price, quantity
                    ]
                    paper_info["trade_count"] = paper_info.get("trade_count", 0) + 1

                    logging.info(
                        f"{GREEN}[ENTRY][PAPER][{regime_tag}] LONG {side} {opt_name} BUY @ {entry_price:.2f} "
                        f"SL={stop:.2f} PT={partial_target:.2f} TG={full_target:.2f} CONF={confidence}{RESET}"
                    )
                else:
                    logging.warning(f"{CYAN}[ENTRY SKIPPED] {side} no valid option found in df for strike={strike}{CYAN}")
        except Exception as e:
            logging.error(f"[ENTRY ERROR][PAPER] {e}", exc_info=True)

    # 5. EXIT MANAGEMENT (unchanged)
    for leg, side in [("call_buy", "CALL"), ("put_buy", "PUT")]:
        if paper_info[leg]["trade_flag"] != 1:
            continue
        name = paper_info[leg]["option_name"]
        price = df.loc[name, "ltp"] if name in df.index else None
        if price is None or pd.isna(price):
            continue
        exit_triggered = process_order(side, name, price, paper_info, hist_yesterday_15m, spot_price, account_type="paper")
        if exit_triggered:
            paper_info["last_exit_time"] = dt.now(time_zone)
            logging.info(f"{YELLOW}[EXIT RECORDED][PAPER] {side} {name} at {paper_info['last_exit_time']}{RESET}")
            update_risk(paper_info, risk_info)

    # 6. SAVE TRADES (unchanged)
    frames = [paper_info["call_buy"]["filled_df"], paper_info["put_buy"]["filled_df"]]
    frames = [f for f in frames if not f.empty]
    if frames:
        combined = pd.concat(frames)
        combined.to_csv(f"trades_{strategy_name}_{dt.now(time_zone).date()}.csv")
    store(paper_info, account_type)


# =============================== Live Trading =======================================

# ===== real_order =====
def live_order(candles_3m, hist_yesterday_15m=None):
    global quantity, live_info, df, spot_price, last_signal_candle_time, risk_info

    COOLDOWN_SECONDS = 180  # 3 minutes
    CONFIDENCE_THRESHOLD = 80  # configurable, move to config.py if preferred

    # --- Safety reset ---
    for leg in ["call_buy", "put_buy"]:
        if live_info[leg].get("trade_flag", 0) == 2:
            logging.warning(f"{CYAN}[RESET] Found lingering trade_flag=2 for {leg}, resetting to 0{RESET}")
            live_info[leg]["trade_flag"] = 0

    ct = dt.now(time_zone)

    # 1. Refresh spot price (live)
    try:
        quote = fyers.quotes(data={"symbols": ticker})
        spot_price = quote["d"][0]["v"]["lp"]
        logging.info(f"[LIVE] Spot={spot_price}")
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
                    exit_price = df.loc[name, "ltp"] if name in df.index else spot_price
                    cleanup_trade_exit(live_info, leg, side, name, qty, exit_price, "LIVE", "EOD")
                    update_order_status(order_id, "PENDING", qty, exit_price, name)
        return

    # 3. SIGNAL EVALUATION
    signal = None
    if not candles_3m.empty and hist_yesterday_15m is not None and not hist_yesterday_15m.empty:
        last_candle_time = candles_3m.iloc[-1]["time"]
        if last_signal_candle_time != last_candle_time:
            last_signal_candle_time = last_candle_time
            daily_val = daily_atr(hist_yesterday_15m)
            atr, atr_source = resolve_atr(candles_3m, daily_val)
            logging.info(f"{YELLOW}[SIGNAL EVAL][LIVE] candle={last_candle_time} candles={len(candles_3m)} atr={atr} source={atr_source}{RESET}")
            prev_day = hist_yesterday_15m.iloc[-1]
            cpr  = calculate_cpr(prev_day["high"], prev_day["low"], prev_day["close"])
            trad = calculate_traditional_pivots(prev_day["high"], prev_day["low"], prev_day["close"])
            cam  = calculate_camarilla_pivots(prev_day["high"], prev_day["low"], prev_day["close"])
            signal = detect_signal(cpr, trad, cam, candles_3m, hist_yesterday_15m, spot_price=spot_price, daily_atr=daily_val)

    # 4. LIVE ENTRY LOGIC (with confidence filter)
    if signal:
        side, reason, confidence = signal
        logging.info(f"[SIGNAL][LIVE] {side} ({reason}) confidence={confidence} spot={spot_price}")

        if confidence < CONFIDENCE_THRESHOLD:
            logging.info(f"{MAGENTA}[ENTRY BLOCKED][CONFIDENCE] {side} skipped (score={confidence} < {CONFIDENCE_THRESHOLD}){RESET}")
            return

        if risk_info.get("halt_trading", False):
            logging.info(f"{MAGENTA}[ENTRY BLOCKED][RISK] Trading halted due to risk limits{RESET}")
            return

        if live_info.get("last_exit_time"):
            elapsed = (dt.now(time_zone) - live_info["last_exit_time"]).total_seconds()
            if elapsed < COOLDOWN_SECONDS:
                logging.info(f"{MAGENTA}[ENTRY BLOCKED][LIVE] Cool-down active ({elapsed:.0f}s < {COOLDOWN_SECONDS}s){RESET}")
                return

        if not oscillator_entry_filter(side, candles_3m):
            logging.info(f"{MAGENTA}[ENTRY BLOCKED][OSC] {side} skipped due to oscillator filter{RESET}")
            return

        leg = "call_buy" if side == "CALL" else "put_buy"
        try:
            if live_info[leg]["trade_flag"] == 0:
                opt_type = "CE" if side == "CALL" else "PE"
                opt_name, strike = get_option_by_moneyness(
                    spot_price, opt_type,
                    moneyness=CALL_MONEYNESS if side == "CALL" else PUT_MONEYNESS
                )

                if opt_name and opt_name in df.index:
                    entry_price = df.loc[opt_name, "ltp"] or spot_price
                    levels = build_dynamic_levels(entry_price, atr)
                    if any(v is None for v in levels):
                        logging.info(f"{MAGENTA}[ENTRY SKIPPED][LIVE] {side} ATR regime extreme, no levels built{RESET}")
                        return
                    stop, partial_target, full_target, trail_start, trail_step = levels

                    # Detect expiry regime (Tuesday)
                    weekday = dt.now(time_zone).weekday()
                    expiry_flag = (weekday == 1)  # Tuesday
                    regime_tag = "EXPIRY ENTRY" if expiry_flag else "VOLATILITY ENTRY"

                    success, order_id = send_live_entry_order(opt_name, quantity, 1)  # BUY side=1
                    if not success:
                        logging.warning(f"{MAGENTA}[ENTRY FAILED][LIVE] {side} {opt_name}{RESET}")
                        return

                    live_info[leg].update({
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
                        "confidence": confidence,   # <-- NEW
                        "order_id": order_id,
                        "entry_time": ct,
                    })

                    live_info[leg]["filled_df"].loc[ct] = [
                        opt_name, entry_price, "BUY", stop, full_target, spot_price, quantity
                    ]
                    live_info["trade_count"] = live_info.get("trade_count", 0) + 1

                    logging.info(
                        f"{GREEN}[ENTRY][LIVE][{regime_tag}] LONG {side} {opt_name} BUY @ {entry_price:.2f} "
                        f"SL={stop:.2f} PT={partial_target:.2f} TG={full_target:.2f} CONF={confidence}{RESET}"
                    )
                else:
                    logging.warning(f"{CYAN}[ENTRY SKIPPED] {side} no valid option found in df for strike={strike}{RESET}")
        except Exception as e:
            logging.error(f"{RED}[ENTRY ERROR][LIVE] {e}{RESET}", exc_info=True)

    # 5. EXIT MANAGEMENT (unchanged)
    for leg, side in [("call_buy", "CALL"), ("put_buy", "PUT")]:
        if live_info[leg]["trade_flag"] != 1:
            continue
        name = live_info[leg]["option_name"]
        price = df.loc[name, "ltp"] if name in df.index else None
        if price is None or pd.isna(price):
            continue
        exit_triggered = process_order(side, name, price, live_info, hist_yesterday_15m, spot_price, account_type="live")
        if exit_triggered:
            live_info["last_exit_time"] = dt.now(time_zone)
            logging.info(f"{YELLOW}[EXIT RECORDED][LIVE] {side} {name} at {live_info['last_exit_time']}{RESET}")
            update_risk(live_info, risk_info)

    # 6. SAVE TRADES (unchanged)
    frames = [live_info["call_buy"]["filled_df"], live_info["put_buy"]["filled_df"]]
    frames = [f for f in frames if not f.empty]
    if frames:
        combined = pd.concat(frames)
        combined.to_csv(f"trades_{strategy_name}_{dt.now(time_zone).date()}_LIVE.csv")
    store(live_info, account_type)


# ============================================== RUN Strategy ==============================================

# --- Helper: sleep until next boundary ---
def sleep_until_next_boundary(interval=180, tz="Asia/Kolkata"):
    now = dt.now(tz)
    seconds = now.minute * 60 + now.second
    next_boundary = ((seconds // interval) + 1) * interval
    sleep_time = next_boundary - seconds
    time.sleep(sleep_time)

# --- Main Orchestration Loop ---
# ===== execution.py =====

def run_strategy(symbols, hist_yesterday_15m, tz="Asia/Kolkata", end_time=None):
    """
    Orchestration loop:
    - Refresh spot price
    - Delegate candle building + signal detection
    - Route to paper/live order
    - Run ATR/CPR bias logic on 15m closes
    """
    while dt.now(tz) < end_time:
        now = dt.now(tz)

        for sym in symbols:
            logging.info(f"{GRAY}[STRATEGY] Running for {sym}{RESET}")

            # --- Refresh spot price ---
            try:
                quote = fyers.quotes(data={"symbols": sym})
                spot_price = quote["d"][0]["v"]["lp"]
                logging.info(f"{GRAY}[SPOT REFRESH] {sym} Spot={spot_price}{RESET}")
            except Exception as e:
                logging.warning(f"{GRAY}[SPOT REFRESH FAILED] {sym}: {e}{RESET}")
                continue

            # --- Delegate candles + signal detection ---
            signal, candles_3m = update_candles_and_signals(
                symbol=sym,
                hist_yesterday_15m=hist_yesterday_15m.get(sym),
                spot_price=spot_price   # ✅ pass spot price down
            )

            # --- Route orders if signal fired ---
            if signal:
                side, reason = signal
                logging.info(f"{YELLOW}[SIGNAL FIRED] {sym} side={side} reason={reason}{RESET}")
                if account_type.upper() == "PAPER":
                    paper_order(candles_3m, hist_yesterday_15m[sym])
                else:
                    live_order(candles_3m, hist_yesterday_15m[sym])

            # --- Extra bias logic on 15m close ---
            if now.minute % 15 == 0 and now.second == 0:
                logging.info(f"{CYAN}[SYNC] 15m candle closed for {sym}, running ATR/CPR bias logic{RESET}")

                hist_data = tick_db.fetch_candles("15m", symbol=sym)

                # --- Debug guard: log type and length ---
                logging.debug(
                    f"{CYAN}[BIAS GUARD @RUN] hist_data type={type(hist_data)} len={len(hist_data)}{RESET}"
                )
                if not hist_data.empty:
                    logging.debug(f"{CYAN}[BIAS PREVIEW @RUN]\n{hist_data.tail(3)}{RESET}")

                    daily_val = daily_atr(hist_data)  # float
                    atr_value, atr_source = resolve_atr(hist_data, daily_val)
                    atr_str = f"{atr_value:.2f}" if atr_value is not None else "NA"
                    logging.info(f"[ATR] {sym} source={atr_source} value={atr_str}")


                    
if __name__ == "__main__":
    symbols = symbols  # restrict to indices only

    today = dt.now("Asia/Kolkata").date()
    end_time = dt.datetime(today.year, today.month, today.day, 15, 30, tz="Asia/Kolkata")

    logging.info(f"[SESSION] Trading until {end_time}")

    hist_yesterday_15m = {
        sym: tick_db.fetch_candles("15m", use_yesterday=True, symbol=sym)
        for sym in symbols
    }

    # ✅ Visibility: log how many 15m candles were bootstrapped per symbol
    for sym, df in hist_yesterday_15m.items():
        logging.info(f"{CYAN}[BOOTSTRAP] {sym} yesterday 15m candles={len(df)}{RESET}")

    run_strategy(symbols, hist_yesterday_15m, tz="Asia/Kolkata", end_time=end_time)