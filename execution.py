# ===== execution.py =====
import logging
import pickle
import pandas as pd
import pendulum as dt
from fyers_apiv3 import fyersModel
import time
from datetime import datetime, timedelta

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
    williams_r,
    calculate_cci,
    momentum_ok
    
)

from signals import detect_signal
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

today_str = datetime.now().strftime("%Y-%m-%d")

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
    Load the latest trading state from pickle file.
    Returns the most recent snapshot's state dict.
    """
    filename = f"data-{dt.now(time_zone).date()}-{account_type_}.pickle"
    try:
        with open(filename, "rb") as f:
            ledger = pickle.load(f)

        # Ledger is a list of snapshots; return the latest state
        if isinstance(ledger, list) and ledger:
            return ledger[-1]["state"]
        elif isinstance(ledger, dict):
            # Legacy single snapshot
            return ledger
        else:
            raise ValueError("Ledger format invalid or empty")
    except Exception as e:
        logging.warning(f"State load failed (fresh start): {e}")
        raise


def load_ledger(account_type_):
    """
    Load the full ledger (all snapshots) from pickle file.
    Useful for audit, replay, or debugging.
    """
    filename = f"data-{dt.now(time_zone).date()}-{account_type_}.pickle"
    try:
        with open(filename, "rb") as f:
            ledger = pickle.load(f)

        # Normalize legacy formats
        if isinstance(ledger, dict):
            return [ledger]
        elif isinstance(ledger, list):
            return ledger
        else:
            return []
    except Exception as e:
        logging.warning(f"Ledger load failed: {e}")
        return []

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



def check_exit_condition(df_slice, state):
    """
    Hybrid exit logic for live trading:
    - ATR-based dynamic levels (stop-loss, partial/full targets, trailing stop)
    - EMA plateau + momentum drop + oscillator confirmation
    - Consecutive candle reversal guard (anticipates 3–4 candle reversals)
    - Time-based guard (max 5 candles unless trailing engaged)
    Returns: (bool, reason_str)
    """

    i = len(df_slice) - 1
    side = state["side"]
    entry_price = state.get("buy_price")
    current_price = df_slice["close"].iloc[-1]

    # --- Minimum hold period ---
    if i - state["entry_candle"] < 2:  # allow at least 2 candles before exit checks
        return False, None

    # --- EMA gap & momentum ---
    ema9 = df_slice["close"].ewm(span=9, adjust=False).mean().iloc[-1]
    ema13 = df_slice["close"].ewm(span=13, adjust=False).mean().iloc[-1]
    ema_gap = abs(ema9 - ema13)

    ok, momentum = momentum_ok(df_slice, side)
    if not ok or momentum is None:
        momentum = 0

    # --- ATR-based dynamic levels ---
    stop = state.get("stop")
    tg   = state.get("tg")
    trail_step = state.get("trail_step")

    # --- ATR exits (same for CALL and PUT since both are long) ---
    if stop and current_price <= stop:
        logging.info(f"{YELLOW}[EXIT] ATR Stop Loss hit side={side} stop={stop} current={current_price}{RESET}")
        return True, "SL_HIT"

    if tg and current_price >= tg:
        logging.info(f"{YELLOW}[EXIT] ATR Full Target hit side={side} target={tg} current={current_price}{RESET}")
        return True, "TARGET_HIT"

    # --- Buffered trailing stop update ---
    buffer_points = 12  # minimum favorable move before trailing engages
    if current_price >= entry_price + buffer_points:
        new_stop = current_price - trail_step
        if new_stop > state["stop"]:
            state["stop"] = new_stop
            state["trail_updates"] += 1
            logging.info(f"[TRAIL] {side} stop updated → {state['stop']:.2f} at Candle {i}")

    # --- Consecutive candle reversal guard ---
    if "consec_count" not in state:
        state["consec_count"] = 0

    last_candle = df_slice.iloc[-1]
    if last_candle["close"] > last_candle["open"]:
        state["consec_count"] += 1
    else:
        state["consec_count"] = 0

    if state["consec_count"] >= 4:
        logging.info(f"{YELLOW}[EXIT] Consecutive candles → reversal risk side={side}{RESET}")
        return True, "REVERSAL_EXIT"

    # --- Momentum exits ---
    if ema_gap > state["prev_gap"]:
        state["prev_gap"] = ema_gap
        if abs(momentum) > state["peak_momentum"]:
            state["peak_momentum"] = abs(momentum)
            state["peak_candle"] = i
        state["plateau_count"] = 0
        return False, None

    if ema_gap <= state["prev_gap"]:
        state["plateau_count"] += 1
    else:
        state["plateau_count"] = 0

    if state["plateau_count"] >= 2 and abs(momentum) < state["peak_momentum"] * 0.4:
        cci_series = calculate_cci(df_slice)
        cci_val = cci_series.iloc[-1] if not cci_series.empty else None
        wr_val = williams_r(df_slice)

        if (cci_val is not None and cci_val > 120) or (wr_val is not None and wr_val < -85):
            logging.info(f"{YELLOW}[EXIT] EMA plateau + momentum drop + oscillator confirm side={side}{RESET}")
            return True, "MOMENTUM_EXIT"

    # --- Time guard ---
    if i - state["entry_candle"] >= 5 and state["trail_updates"] == 0:
        logging.info(f"{YELLOW}[EXIT] Max hold time (5 candles) exceeded side={side}{RESET}")
        return True, "TIME_EXIT"

    return False, None


def build_dynamic_levels(entry_price, atr, side, entry_candle,
                         rr_ratio=2.0, profit_loss_point=5, candles_df=None):
    """
    Build stop-loss, partial/full targets, and trailing parameters.
    Adjustments for live market:
    - SL anchored to entry candle extremes
    - Wider reward points (2 × ATR in normal regime)
    - Buffered trailing stop
    - Unified PT/TG logic for long CALL and long PUT

    Parameters:
    - entry_price: float, trade entry price
    - atr: float, average true range
    - side: str, "CALL" or "PUT"
    - entry_candle: either a DataFrame row (Series) or an integer index
    - rr_ratio: reward-to-risk multiplier
    - profit_loss_point: minimum risk points
    - candles_df: optional DataFrame of candles (required if entry_candle is int)
    """

    # ---- Resolve entry_candle row ----
    if isinstance(entry_candle, int):
        if candles_df is None:
            logging.error("[LEVELS] entry_candle is int but candles_df not provided")
            return None, None, None, None, None
        candle_row = candles_df.iloc[entry_candle]
    elif isinstance(entry_candle, pd.Series):
        candle_row = entry_candle
    else:
        logging.error("[LEVELS] Invalid entry_candle type")
        return None, None, None, None, None

    # ---- Decision: Normal / Volatile / Extreme ----
    if atr <= 80:
        mode = "normal"
    elif atr <= 200:
        mode = "volatile"
    else:
        mode = "extreme"

    if mode == "normal":
        risk_points   = max(profit_loss_point, atr * 0.25)
        reward_points = atr * rr_ratio

        if side == "CALL":
            stop = candle_row["low"]
        elif side == "PUT":
            stop = candle_row["high"]
        else:
            stop = entry_price - risk_points

        partial_target = entry_price + reward_points / 2
        full_target    = entry_price + reward_points
        trail_start    = reward_points / 2
        trail_step     = max(atr * 0.2, 1.0)

        logging.info(
            f"{CYAN}[LEVELS][NORMAL] ATR={atr:.2f} Risk={risk_points:.2f} "
            f"Reward={reward_points:.2f} SL={stop:.2f}{RESET}"
        )

    elif mode == "volatile":
        risk_points   = atr * 0.5
        reward_points = atr * 2.0

        if side == "CALL":
            stop = candle_row["low"]
        elif side == "PUT":
            stop = candle_row["high"]
        else:
            stop = entry_price - risk_points

        partial_target = entry_price + reward_points * 0.5
        full_target    = entry_price + reward_points
        trail_start    = reward_points * 0.25
        trail_step     = max(atr * 0.3, 1.5)

        logging.info(
            f"{CYAN}[LEVELS][VOLATILE] ATR={atr:.2f} Risk={risk_points:.2f} "
            f"Reward={reward_points:.2f} SL={stop:.2f}{RESET}"
        )

    else:
        logging.warning(
            f"{CYAN}[LEVELS][EXTREME] ATR={atr:.2f} → Trade skipped due to excessive volatility{RESET}"
        )
        return None, None, None, None, None

    return stop, partial_target, full_target, trail_start, trail_step

def update_trailing_stop(current_price, entry_price, current_stop,
                         trail_start_pnl, trail_step_points, buffer_points=12):
    """
    Update trailing stop once partial target booked.
    Adjustments for live market:
    - Buffered trailing (≥ buffer_points move in favor)
    - Ratchets stop upward/downward depending on side
    """

    pnl = current_price - entry_price

    # Only trail if price has moved enough in favor
    if abs(pnl) >= buffer_points and trail_step_points > 0:
        candidate = current_price - trail_step_points if pnl > 0 else current_price + trail_step_points
        new_stop = max(current_stop, candidate) if pnl > 0 else min(current_stop, candidate)

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
                'trail_start_pnl': 0,
                'trail_step_points': 0,
                'reason': None,
                'confidence': 0,
                'order_id': None,
                'entry_time': None,
                'partial_booked': False,
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
                'trail_start_pnl': 0,
                'trail_step_points': 0,
                'reason': None,
                'confidence': 0,
                'order_id': None,
                'entry_time': None,
                'partial_booked': False,
            },
            'condition': False,
            'total_pnl': 0,
            'trade_count': 0,
            'max_trades': MAX_TRADES_PER_DAY,
            'last_exit_time': None,
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
                'trail_start_pnl': 0,
                'trail_step_points': 0,
                'reason': None,
                'confidence': 0,
                'order_id': None,
                'entry_time': None,
                'partial_booked': False,
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
                'trail_start_pnl': 0,
                'trail_step_points': 0,
                'reason': None,
                'confidence': 0,
                'order_id': None,
                'entry_time': None,
                'partial_booked': False,
            },
            'condition': False,
            'total_pnl': 0,
            'trade_count': 0,
            'max_trades': MAX_TRADES_PER_DAY,
            'last_exit_time': None,
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

# ===== process_order =====
def process_order(state, df_slice, info, spot_price, account_type="paper"):
    """
    Manage exits for an active trade using SL/Target + hybrid exit logic.
    Adjustments for live market:
    - Explicit SL/Target checks with latency buffer
    - Hybrid exit logic (ATR, momentum, reversal, time)
    - Market exits for guaranteed fills
    - Audit logging with trail updates
    """

    side   = state["side"]
    symbol = state.get("option_name", "N/A")
    entry  = state.get("buy_price", 0)
    qty    = state.get("quantity", 0)

    current_candle = df_slice.iloc[-1]

    # --- Latency buffer (2–3 points) ---
    buffer = 2.0

    # --- Explicit SL/Target checks ---
    exit_reason = None
    if side == "CALL":
        if current_candle["low"] <= state["stop"] + buffer:
            exit_reason = "SL_HIT"
        elif spot_price >= state["pt"] - buffer:
            exit_reason = "TARGET_HIT"
    elif side == "PUT":
        if current_candle["high"] >= state["stop"] - buffer:
            exit_reason = "SL_HIT"
        elif spot_price <= state["pt"] + buffer:
            exit_reason = "TARGET_HIT"

    # --- Hybrid exit logic (ATR, momentum, reversal, time) ---
    if not exit_reason:
        triggered, reason = check_exit_condition(df_slice, state)
        if triggered and reason:
            exit_reason = reason

    # --- If no exit condition met, keep trade alive ---
    if not exit_reason:
        return False, None

    # --- Route exit order ---
    if account_type.lower() == "paper":
        success, order_id = send_paper_exit_order(symbol, qty, exit_reason)
    else:
        # Live trading: always use market order for exit
        success, order_id = send_live_exit_order(symbol, qty, exit_reason, order_type="MARKET")

    if success:
        exit_price = current_candle["close"]
        pnl_points = exit_price - entry if side == "CALL" else entry - exit_price
        pnl_value  = pnl_points * qty

        trade = info["call_buy"] if side == "CALL" else info["put_buy"]
        trade["pnl"] += pnl_value
        info["total_pnl"] = info["call_buy"].get("pnl", 0) + info["put_buy"].get("pnl", 0)
        trade["trade_flag"] = 0
        trade["quantity"] = 0

        trade["filled_df"].loc[dt.now(time_zone)] = [
            symbol,
            entry,
            exit_price,
            side,
            state.get("reason", "UNKNOWN"),   # entry reason
            exit_reason,                      # exit reason
            state.get("entry_candle", -1),    # entry candle number
            len(df_slice) - 1,                # exit candle number
            pnl_points,
            pnl_value,
            spot_price,
            qty
        ]

        logging.info(
            f"{YELLOW}[EXIT][{account_type.upper()} {exit_reason}] {side} {symbol} "
            f"EntryCandle={state['entry_candle']} ExitCandle={len(df_slice)-1} "
            f"Entry={entry:.2f} Exit={exit_price:.2f} Qty={qty} "
            f"PnL={pnl_value:.2f} (points={pnl_points:.2f}) "
            f"Reason={state.get('reason','UNKNOWN')} "
            f"TrailUpdates={state.get('trail_updates',0)}{RESET}"
        )

        update_order_status(order_id, "PENDING", qty, exit_price, symbol)
        return True, exit_reason

    return False, None


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
def paper_order(candles_3m, hist_yesterday_15m=None, exit=False):
    global quantity, paper_info, df, spot_price, last_signal_candle_time, risk_info

    COOLDOWN_SECONDS = 180  # 3 minutes
    ct = dt.now(time_zone)

    # 1. Safety reset
    for leg in ["call_buy", "put_buy"]:
        if paper_info[leg].get("trade_flag", 0) == 2:
            logging.warning(f"{CYAN}[RESET] Found lingering trade_flag=2 for {leg}, resetting to 0{RESET}")
            paper_info[leg]["trade_flag"] = 0

    # 2. Refresh spot price (simulated)
    try:
        quote = fyers.quotes(data={"symbols": ticker})
        spot_price = quote["d"][0]["v"]["lp"]
        logging.info(f"[PAPER] Spot={spot_price}")
    except Exception as e:
        logging.warning(f"[PAPER] Spot fetch failed: {e}")

    # 3. End-of-day force exit
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

    # 4. Exit management (explicit exit flag)
    if exit:
        for leg, side in [("call_buy", "CALL"), ("put_buy", "PUT")]:
            if paper_info[leg]["trade_flag"] == 1:
                state = paper_info[leg]
                triggered = process_order(state, candles_3m, paper_info, spot_price, account_type="paper")
                if triggered:
                    paper_info["last_exit_time"] = ct
                    logging.info(f"{YELLOW}[EXIT][PAPER] {side} {state['option_name']} at {paper_info['last_exit_time']}{RESET}")
        return
    
    # 5. Signal evaluation (entry detection on 3m candles only)
    signal = None
    if not candles_3m.empty:
        last_candle_time = candles_3m.iloc[-1]["time"]
        if last_signal_candle_time != last_candle_time:
            last_signal_candle_time = last_candle_time

            # ATR from 3m candles
            atr, atr_source = resolve_atr(candles_3m)
            logging.info(
                f"{YELLOW}[SIGNAL EVAL][PAPER] candle={last_candle_time} "
                f"candles={len(candles_3m)} atr={atr:.2f} source={atr_source}{RESET}"
            )

            # Pivot calculations from the most recent 3m candle
            prev_candle = candles_3m.iloc[-1]
            cpr  = calculate_cpr(prev_candle["high"], prev_candle["low"], prev_candle["close"])
            trad = calculate_traditional_pivots(prev_candle["high"], prev_candle["low"], prev_candle["close"])
            cam  = calculate_camarilla_pivots(prev_candle["high"], prev_candle["low"], prev_candle["close"])

            # Detect signal using 3m pivots + ATR
            signal = detect_signal(cpr, trad, cam, candles_3m, atr=atr)

    # 6. Paper entry logic
    if signal:
        side, reason = signal["side"], signal["reason"]
        logging.info(f"{YELLOW}[SIGNAL][PAPER] {side} ({reason}) spot={spot_price}{RESET}")

        # --- Optional 15m bias filter ---
        if hist_yesterday_15m is not None and not hist_yesterday_15m.empty:
            last_15m = hist_yesterday_15m.iloc[-1]
            bias15 = last_15m.get("supertrend_bias", "NEUTRAL")
            slope15 = last_15m.get("supertrend_slope", "FLAT")
            logging.info(f"[BIAS][15m] bias={bias15} slope={slope15}")
            # Example filter: block entries if 15m bias is NEUTRAL
            if bias15 == "NEUTRAL":
                logging.info(f"{MAGENTA}[ENTRY BLOCKED][15m Bias] Skipping trade due to neutral 15m bias{RESET}")
                return

        # --- Filters ---
        if risk_info.get("halt_trading", False):
            logging.info(f"{MAGENTA}[ENTRY BLOCKED][RISK] Trading halted due to risk limits{RESET}")
            return
        if paper_info.get("last_exit_time"):
            elapsed = (ct - paper_info["last_exit_time"]).total_seconds()
            if elapsed < COOLDOWN_SECONDS:
                logging.info(f"{MAGENTA}[ENTRY BLOCKED][COOLDOWN] {side} skipped ({elapsed:.0f}s < {COOLDOWN_SECONDS}s){RESET}")
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

                    # --- ATR dynamic levels ---
                    stop, pt, tg, trail_start, trail_step = build_dynamic_levels(
                        entry_price,
                        atr,
                        side,
                        len(candles_3m) - 1
                    )
                    if stop is None:
                        logging.warning(f"{CYAN}[ENTRY SKIPPED] {side} ATR regime extreme → skipping trade{RESET}")
                        return

                    # --- Update trade state ---
                    paper_info[leg].update({
                        "option_name": opt_name,
                        "quantity": quantity,
                        "buy_price": entry_price,
                        "order_type": ORDER_TYPE,
                        "trade_flag": 1,
                        "pnl": 0,
                        "reason": reason,
                        "order_id": f"paper_{opt_name}_{ct}",
                        "entry_time": ct,
                        "entry_candle": len(candles_3m) - 1,
                        "side": side,
                        # ATR dynamic levels
                        "stop": stop,
                        "pt": pt,
                        "tg": tg,
                        "trail_start": trail_start,
                        "trail_step": trail_step,
                        # Momentum state
                        "prev_gap": 0,
                        "peak_momentum": 0,
                        "peak_candle": len(candles_3m) - 1,
                        "plateau_count": 0,
                    })

                    # --- Record entry in filled_df ---
                    paper_info[leg]["filled_df"].loc[ct] = [
                        opt_name,
                        entry_price,
                        float("nan"),          # exit_price placeholder
                        side,
                        reason,                # entry_reason
                        None,                  # exit_reason placeholder
                        len(candles_3m) - 1,   # entry_candle
                        None,                  # exit_candle placeholder
                        None,                  # pnl_points placeholder
                        None,                  # pnl_value placeholder
                        spot_price,
                        quantity,
                    ]

                    paper_info["trade_count"] = paper_info.get("trade_count", 0) + 1

                    logging.info(
                        f"{GREEN}[ENTRY][PAPER] LONG {side} {opt_name} BUY @ {entry_price:.2f} "
                        f"Reason={reason} ATR={atr:.2f} SL={stop:.2f} PT={pt:.2f} TG={tg:.2f} "
                        f"TrailStart={trail_start:.2f} TrailStep={trail_step:.2f}{RESET}"
                    )
                else:
                    logging.warning(f"{CYAN}[ENTRY SKIPPED] {side} no valid option found in df for strike={strike}{CYAN}")
        except Exception as e:
            logging.error(f"[ENTRY ERROR][PAPER] {e}", exc_info=True)

    # 7. Save trades
    frames = [paper_info["call_buy"]["filled_df"], paper_info["put_buy"]["filled_df"]]
    frames = [f for f in frames if not f.empty]
    if frames:
        combined = pd.concat(frames)
        combined.to_csv(f"trades_{strategy_name}_{dt.now(time_zone).date()}.csv")
    store(paper_info, account_type)
# =============================== Live Trading =======================================

# ===== real_order =====
def live_order(candles_3m, hist_yesterday_15m=None, exit=False):
    global quantity, live_info, df, spot_price, last_signal_candle_time, risk_info

    COOLDOWN_SECONDS = 180  # 3 minutes
    ct = dt.now(time_zone)

    # 1. Safety reset
    for leg in ["call_buy", "put_buy"]:
        if live_info[leg].get("trade_flag", 0) == 2:
            logging.warning(f"{CYAN}[RESET] Found lingering trade_flag=2 for {leg}, resetting to 0{RESET}")
            live_info[leg]["trade_flag"] = 0

    # 2. Refresh spot price (live)
    try:
        quote = fyers.quotes(data={"symbols": ticker})
        spot_price = quote["d"][0]["v"]["lp"]
        logging.info(f"[LIVE] Spot={spot_price}")
    except Exception as e:
        logging.warning(f"[LIVE] Spot fetch failed: {e}")

    # 3. End-of-day force exit
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

    # 4. Exit management (explicit exit flag)
    if exit:
        for leg, side in [("call_buy", "CALL"), ("put_buy", "PUT")]:
            if live_info[leg]["trade_flag"] == 1:
                state = live_info[leg]
                triggered = process_order(state, candles_3m, live_info, spot_price, account_type="live")
                if triggered:
                    live_info["last_exit_time"] = ct
                    logging.info(f"{YELLOW}[EXIT][LIVE] {side} {state['option_name']} at {live_info['last_exit_time']}{RESET}")
        return

    # 5. Signal evaluation (entry detection on 3m candles only)
    signal = None
    if not candles_3m.empty:
        last_candle_time = candles_3m.iloc[-1]["time"]
        if last_signal_candle_time != last_candle_time:
            last_signal_candle_time = last_candle_time

            # ATR from 3m candles
            atr, atr_source = resolve_atr(candles_3m)
            logging.info(
                f"{YELLOW}[SIGNAL EVAL][LIVE] candle={last_candle_time} "
                f"candles={len(candles_3m)} atr={atr:.2f} source={atr_source}{RESET}"
            )

            # Pivot calculations from the most recent 3m candle
            prev_candle = candles_3m.iloc[-1]
            cpr  = calculate_cpr(prev_candle["high"], prev_candle["low"], prev_candle["close"])
            trad = calculate_traditional_pivots(prev_candle["high"], prev_candle["low"], prev_candle["close"])
            cam  = calculate_camarilla_pivots(prev_candle["high"], prev_candle["low"], prev_candle["close"])

            # Detect signal using 3m pivots + ATR
            signal = detect_signal(cpr, trad, cam, candles_3m, atr=atr)

    # 6. Live entry logic
    if signal:
        side, reason = signal["side"], signal["reason"]
        logging.info(f"[SIGNAL][LIVE] {side} ({reason}) spot={spot_price}")

        # --- Optional 15m bias filter ---
        if hist_yesterday_15m is not None and not hist_yesterday_15m.empty:
            last_15m = hist_yesterday_15m.iloc[-1]
            bias15 = last_15m.get("supertrend_bias", "NEUTRAL")
            slope15 = last_15m.get("supertrend_slope", "FLAT")
            logging.info(f"[BIAS][15m] bias={bias15} slope={slope15}")
            if bias15 == "NEUTRAL":
                logging.info(f"{MAGENTA}[ENTRY BLOCKED][15m Bias] Skipping trade due to neutral 15m bias{RESET}")
                return

        # --- Filters ---
        if risk_info.get("halt_trading", False):
            logging.info(f"{MAGENTA}[ENTRY BLOCKED][RISK] Trading halted due to risk limits{RESET}")
            return
        if live_info.get("last_exit_time"):
            elapsed = (ct - live_info["last_exit_time"]).total_seconds()
            if elapsed < COOLDOWN_SECONDS:
                logging.info(f"{MAGENTA}[ENTRY BLOCKED][LIVE] Cool-down active ({elapsed:.0f}s < {COOLDOWN_SECONDS}s){RESET}")
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

                    # --- ATR dynamic levels ---
                    stop, pt, tg, trail_start, trail_step = build_dynamic_levels(
                        entry_price,
                        atr,
                        side,
                        len(candles_3m) - 1
                    )
                    if stop is None:
                        logging.warning(f"{CYAN}[ENTRY SKIPPED] {side} ATR regime extreme → skipping trade{RESET}")
                        return

                    # Place live order
                    success, order_id = send_live_entry_order(opt_name, quantity, 1)  # BUY side=1
                    if not success:
                        logging.warning(f"{MAGENTA}[ENTRY FAILED][LIVE] {side} {opt_name}{RESET}")
                        return

                    # --- Update trade state ---
                    live_info[leg].update({
                        "option_name": opt_name,
                        "quantity": quantity,
                        "buy_price": entry_price,
                        "order_type": ORDER_TYPE,
                        "trade_flag": 1,
                        "pnl": 0,
                        "reason": reason,
                        "order_id": order_id,
                        "entry_time": ct,
                        "entry_candle": len(candles_3m) - 1,
                        "side": side,
                        # ATR dynamic levels
                        "stop": stop,
                        "pt": pt,
                        "tg": tg,
                        "trail_start": trail_start,
                        "trail_step": trail_step,
                        # Momentum state
                        "prev_gap": 0,
                        "peak_momentum": 0,
                        "peak_candle": len(candles_3m) - 1,
                        "plateau_count": 0,
                    })

                    # --- Record entry in filled_df ---
                    live_info[leg]["filled_df"].loc[ct] = [
                        opt_name,
                        entry_price,
                        float("nan"),          # exit_price placeholder
                        side,
                        reason,                # entry_reason
                        None,                  # exit_reason placeholder
                        len(candles_3m) - 1,   # entry_candle
                        None,                  # exit_candle placeholder
                        None,                  # pnl_points placeholder
                        None,                  # pnl_value placeholder
                        spot_price,
                        quantity,
                    ]

                    live_info["trade_count"] = live_info.get("trade_count", 0) + 1

                    logging.info(
                        f"{GREEN}[ENTRY][LIVE] LONG {side} {opt_name} BUY @ {entry_price:.2f} "
                        f"Reason={reason} ATR={atr:.2f} SL={stop:.2f} PT={pt:.2f} TG={tg:.2f} "
                        f"TrailStart={trail_start:.2f} TrailStep={trail_step:.2f}{RESET}"
                    )
                else:
                    logging.warning(f"{CYAN}[ENTRY SKIPPED] {side} no valid option found in df for strike={strike}{RESET}")
        except Exception as e:
            logging.error(f"{RED}[ENTRY ERROR][LIVE] {e}{RESET}", exc_info=True)

    # 7. Save trades
    # 7. Save trades
    frames = [live_info["call_buy"]["filled_df"], live_info["put_buy"]["filled_df"]]
    frames = [f for f in frames if not f.empty]
    if frames:
        combined = pd.concat(frames)
        combined.to_csv(
            f"trades_{strategy_name}_{dt.now(time_zone).date()}_LIVE.csv",
            index=True
        )
    store(live_info, account_type)
# ============================================== RUN Strategy ==============================================


# --- Helper: sleep until next boundary ---
def sleep_until_next_boundary(interval=180, tz="Asia/Kolkata"):
    now = dt.now(tz)
    seconds = now.minute * 60 + now.second
    next_boundary = ((seconds // interval) + 1) * interval
    sleep_time = next_boundary - seconds
    time.sleep(sleep_time)



def run_strategy(symbols, tz=time_zone, end_time=None):
    """
    Orchestration loop:
    - Refresh spot price
    - Delegate candle building + signal detection (with previous day continuity, 3m + 15m)
    - Route to paper/live order (entry + exit handled inside those functions)
    """
    while dt.now(tz) < end_time:
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

            # --- Delegate candles + signal detection (3m + 15m continuity) ---
            signal, candles_3m, candles_15m = update_candles_and_signals(
                symbol=sym,
                spot_price=spot_price,
                # lookback_days=2   # ✅ ensure previous day continuity
            )

            # --- Guard against empty DataFrames ---
            if (candles_3m is None or candles_3m.empty) and (candles_15m is None or candles_15m.empty):
                logging.warning(f"[STRATEGY] No candles for {sym}, skipping order evaluation")
                continue

            # --- Summary log ---
            logging.info(
                f"[SUMMARY] {sym}: "
                f"3m candles={len(candles_3m) if candles_3m is not None and not candles_3m.empty else 0} | "
                f"15m candles={len(candles_15m) if candles_15m is not None and not candles_15m.empty else 0}"
            )

            # --- Route to order functions ---
            if account_type.upper() == "PAPER":
                paper_order(candles_3m, hist_yesterday_15m=candles_15m)
            else:
                live_order(candles_3m, hist_yesterday_15m=candles_15m)

        # --- Sleep until next 3m boundary to avoid hammering ---
        sleep_until_next_boundary(interval=180, tz=tz)


if __name__ == "__main__":
    # --- Restrict to indices explicitly ---
    symbols = ["NSE:NIFTY50-INDEX"]  # adjust as needed

    today = dt.now(time_zone).date()
    end_time = dt.datetime(today.year, today.month, today.day, 15, 30, tzinfo=time_zone)

    logging.info(f"[SESSION] Trading until {end_time}")

    # --- Bootstrap yesterday’s 15m candles ---
    hist_yesterday_15m = {
        sym: tick_db.fetch_candles("15m", use_yesterday=True, symbol=sym)
        for sym in symbols
    }

    # ✅ Visibility: log how many 15m candles were bootstrapped per symbol
    for sym, df in hist_yesterday_15m.items():
        if df is not None and not df.empty:
            logging.info(f"{CYAN}[BOOTSTRAP] {sym} yesterday 15m candles={len(df)}{RESET}")
        else:
            logging.warning(f"{CYAN}[BOOTSTRAP] {sym} no 15m candles found for yesterday{RESET}")

    # --- Run strategy orchestration ---
    run_strategy(symbols, tz=time_zone, end_time=end_time)