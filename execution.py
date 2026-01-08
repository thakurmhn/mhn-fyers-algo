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

# ANSI color codes
RESET = "\033[0m"
RED   = "\033[31m"
GREEN = "\033[32m"
YELLOW= "\033[33m"
BLUE  = "\033[34m"

RESET = "\033[0m"
GRAY  = "\033[90m"


# Define ATR
atr, atr_source = resolve_atr(candles_3m, daily_atr)

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
    """
    side: 'CE' or 'PE'
    moneyness: 'OTM' or 'ITM'
    points: additional offset (+/- strike_diff multiples)
    Returns (symbol, strike)
    """
    from config import strike_diff  # local import to avoid circular at module load
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

    symbol = sel.squeeze()
    return symbol, strike

# ===== Risk management helpers =====

# def build_dynamic_levels(entry_price, side, atr_value):
#     """
#     Builds SL / TG / partial TG / trailing levels for OPTION BUY trades (CALL & PUT).
#     Returns: stop_loss, full_target, partial_target, trail_start_pnl, trail_step_points
#     """

#     # ------------------------------
#     # Fallback when ATR is unavailable
#     # ------------------------------
#     if atr_value is None or atr_value <= 0:
#         sl = entry_price - profit_loss_point
#         full_tg = entry_price + profit_loss_point
#         partial_tg = entry_price + profit_loss_point / 2
#         trail_start = profit_loss_point
#         trail_step  = profit_loss_point / 2

#         if not (sl < entry_price < full_tg):
#             logging.error(f"[SL/TG ERROR] side={side} entry={entry_price} SL={sl} TG={full_tg}")
#             return None

#         return round(sl, 2), round(full_tg, 2), round(partial_tg, 2), trail_start, trail_step

#     # ------------------------------
#     # ATR-based distances
#     # ------------------------------
#     stop_dist   = ATR_STOP_MULT * atr_value
#     target_dist = ATR_TGT_MULT  * atr_value
#     trail_start = TRAIL_TRIGGER * atr_value
#     trail_step  = TRAIL_STEP    * atr_value

#     sl = entry_price - stop_dist
#     full_tg = entry_price + target_dist
#     partial_tg = entry_price + target_dist / 2

#     if not (sl < entry_price < full_tg):
#         logging.error(f"[SL/TG ERROR] side={side} entry={entry_price} SL={sl} TG={full_tg}")
#         return None

#     return round(sl, 2), round(full_tg, 2), round(partial_tg, 2), trail_start, trail_step


def build_dynamic_levels(entry_price, side, atr_value=None):
    """
    Build SL / TG / partial TG / trailing levels for OPTION BUY trades.
    Uses fixed profit_loss_point from config.py.
    - Partial exit at +profit_loss_point
    - Full exit at +2*profit_loss_point
    - Hard stop at -15 points
    - Trail in profit_loss_point increments
    """

    # Fixed stop at -15 points
    sl = entry_price - 15

    # Partial and full targets
    partial_tg = entry_price + profit_loss_point
    full_tg    = entry_price + 2 * profit_loss_point

    # Trailing setup
    trail_start = profit_loss_point
    trail_step  = profit_loss_point

    return round(sl, 2), round(full_tg, 2), round(partial_tg, 2), trail_start, trail_step
 
def update_trailing_stop(side, current_price, entry_price, current_stop, trail_start_pnl, trail_step_points):
    """
    Returns updated stop price
    """
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

# ===== PAPER STATE INIT =====
if account_type == 'PAPER':
    try:
        paper_info = load(account_type)
    except:
        column_names = ['time', 'ticker', 'price', 'action', 'stop_price', 'take_profit', 'spot_price', 'quantity']
        filled_df = pd.DataFrame(columns=column_names)
        filled_df.set_index('time', inplace=True)
        # Initial OTM selection equivalent to Base-Part-4
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
        print('call option:', call_option)
        print('put option:', put_option)

        # paper_info = {
        #     'call_buy': {'option_name': call_option,'trade_flag': 0,'buy_price': 0,
        #                  'current_stop_price': 0,'current_profit_price': 0,'filled_df': filled_df.copy(),
        #                  'underlying_price_level': 0,'quantity': quantity,'pnl': 0,'trade_count': 0},
        #     'put_buy':  {'option_name': put_option,'trade_flag': 0,'buy_price': 0,
        #                  'current_stop_price': 0,'current_profit_price': 0,'filled_df': filled_df.copy(),
        #                  'underlying_price_level': 0,'quantity': quantity,'pnl': 0,'trade_count': 0},
        #     'condition': False,
        #     'total_pnl': 0,
        #     'trade_count': 0,
        #     'max_trades': MAX_TRADES_PER_DAY
        # }
        paper_info = {
    'call_buy': {
        'option_name': call_option,
        'trade_flag': 0,
        'buy_price': 0,
        'current_stop_price': 0,
        'current_profit_price': 0,   # will be set dynamically via get_dynamic_target()
        'target_method': "auto",     # "classic", "cpr", "camarilla", or "auto"
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
        'current_profit_price': 0,   # will be set dynamically via get_dynamic_target()
        'target_method': "auto",     # "classic", "cpr", "camarilla", or "auto"
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
    'total_pnl': 0,       # cumulative Paper PnL across all legs
    'trade_count': 0,     # total trades taken
    'max_trades': MAX_TRADES_PER_DAY
}
else:
    try:
        live_info = load(account_type)
    except:
        column_names = ['time', 'ticker', 'price', 'action', 'stop_price', 'take_profit', 'spot_price', 'quantity']
        filled_df = pd.DataFrame(columns=column_names)
        filled_df.set_index('time', inplace=True)
        # Initial OTM selection equivalent
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
        print('call option:', call_option)
        print('put option:', put_option)

        # live_info = {
        #     'call_buy': {'option_name': call_option,'trade_flag': 0,'buy_price': 0,
        #                  'current_stop_price': 0,'current_profit_price': 0,'filled_df': filled_df.copy(),
        #                  'underlying_price_level': 0,'quantity': quantity,'pnl': 0,'trade_count': 0},
        #     'put_buy':  {'option_name': put_option,'trade_flag': 0,'buy_price': 0,
        #                  'current_stop_price': 0,'current_profit_price': 0,'filled_df': filled_df.copy(),
        #                  'underlying_price_level': 0,'quantity': quantity,'pnl': 0,'trade_count': 0},
        #     'condition': False,
        #     'total_pnl': 0,
        #     'trade_count': 0,
        #     'max_trades': MAX_TRADES_PER_DAY
        # }
        live_info = {
    'call_buy': {
        'option_name': call_option,
        'trade_flag': 0,
        'buy_price': 0,
        'current_stop_price': 0,
        'current_profit_price': 0,   # will be set dynamically via get_dynamic_target()
        'target_method': "auto",     # "classic", "cpr", "camarilla", or "auto"
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
        'current_profit_price': 0,   # will be set dynamically via get_dynamic_target()
        'target_method': "auto",     # "classic", "cpr", "camarilla", or "auto"
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
    'total_pnl': 0,       # cumulative Live PnL across all legs
    'trade_count': 0,     # total trades taken
    'max_trades': MAX_TRADES_PER_DAY
}


# ==== Order Processing Logic used for trade Exit =====

# def process_order(side, symbol, price, info, hist_data):
#     """
#     Unified MTM loop for both paper and live trades with partial profit booking.
#     side: "CALL" or "PUT"
#     symbol: option symbol
#     price: current LTP
#     info: paper_info or live_info dict
#     hist_data: DataFrame with historical OHLC data
#     """

#     leg = "call_buy" if side == "CALL" else "put_buy"
#     trade = info[leg]

#     if trade["trade_flag"] != 1:
#         return

#     entry = trade["buy_price"]
#     qty   = trade["quantity"]

#     # --- Stop-loss check ---
#     if price <= trade["current_stop_price"]:
#         pnl = (price - entry) * qty
#         trade["pnl"] += pnl
#         info["total_pnl"] = info["call_buy"]["pnl"] + info["put_buy"]["pnl"]

#         trade["trade_flag"] = 0
#         trade["quantity"] = 0

#         logging.info(f"[{side} EXIT][STOPLOSS] {symbol} @ {price:.2f} PnL={pnl:.2f} Total={info['total_pnl']:.2f}")
#         return

#     # --- Partial Profit Booking ---
#     if not trade.get("partial_booked", False) and price >= trade["partial_target_price"]:
#         half_qty = qty // 2
#         pnl = (price - entry) * half_qty
#         trade["pnl"] += pnl
#         info["total_pnl"] = info["call_buy"]["pnl"] + info["put_buy"]["pnl"]

#         trade["quantity"] -= half_qty
#         trade["partial_booked"] = True

#         # Move SL to cost after partial exit
#         trade["current_stop_price"] = entry

#         logging.info(f"[{side} PARTIAL EXIT] {symbol} @ {price:.2f} Qty={half_qty} PnL={pnl:.2f}")

#     # --- Full Target Check ---
#     if price >= trade["full_target_price"]:
#         pnl = (price - entry) * trade["quantity"]
#         trade["pnl"] += pnl
#         info["total_pnl"] = info["call_buy"]["pnl"] + info["put_buy"]["pnl"]

#         trade["trade_flag"] = 0
#         trade["quantity"] = 0

#         logging.info(f"[{side} EXIT][TARGET] {symbol} @ {price:.2f} PnL={pnl:.2f} Total={info['total_pnl']:.2f}")
#         return

#     # --- Trailing logic (after partial booked) ---
#     if trade["partial_booked"]:
#         pnl_points = price - entry
#         trail_start = trade["trail_start_pnl"]   # profit_loss_point
#         trail_step  = trade["trail_step_points"] # profit_loss_point

#         if pnl_points >= trail_start:
#             new_stop = entry + (pnl_points // trail_step) * trail_step
#             if new_stop > trade["current_stop_price"]:
#                 trade["current_stop_price"] = new_stop
#                 logging.info(f"[TRAIL STOP UPDATE] {symbol} new SL={new_stop:.2f}")

#     # --- MTM Logging ---
#     logging.info(f"{'Paper' if 'paper' in info else 'Live'} MTM {side} {symbol} LTP={price:.2f} Entry={entry:.2f}")

def send_paper_exit_order(symbol, qty, reason):
    """
    Paper trading exit simulation.
    """
    logging.info(f"[PAPER EXIT][{reason}] {symbol} Qty={qty}")
    return True, f"paper_{symbol}_{reason}"

def send_live_exit_order(symbol, qty, reason):
    """
    Send an exit order to Fyers and log response.
    symbol: option symbol
    qty: quantity to exit
    reason: string for logging (STOPLOSS, PARTIAL, TARGET, EOD)
    """
    exit_data = {
        "symbol": symbol,
        "qty": qty,
        "type": 2,       # MARKET
        "side": -1,      # SELL
        "productType": "INTRADAY",
        "validity": "DAY"
    }
    try:
        response = fyers.place_order(exit_data)
        if response.get("s") == "ok":
            order_id = response.get("id")
            logging.info(f"[EXIT][{reason}][BROKER SUCCESS] {symbol} Qty={qty} OrderID={order_id}")
            return True, order_id
        else:
            logging.error(f"[EXIT][{reason}][BROKER FAILED] {symbol} error={response.get('message')}")
            return False, None
    except Exception as e:
        logging.error(f"[EXIT][{reason}][BROKER EXCEPTION] {symbol} error={e}")
        return False, None

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

    # --- Stop-loss check ---
    if price <= trade["current_stop_price"]:
        if account_type.lower() == "paper":
            success, order_id = send_paper_exit_order(trade["option_name"], qty, "STOPLOSS")
        else:
            success, order_id = send_live_exit_order(trade["option_name"], qty, "STOPLOSS")

        if success:
            trade["order_id"] = order_id
            trade["pnl"] += (price - entry) * qty
            info["total_pnl"] = info["call_buy"]["pnl"] + info["put_buy"]["pnl"]
            trade["trade_flag"] = 0
            trade["quantity"] = 0
        return

    # --- Partial Profit Booking ---
    if not trade.get("partial_booked", False) and price >= trade["partial_target_price"]:
        half_qty = qty // 2
        if account_type.lower() == "paper":
            success, order_id = send_paper_exit_order(trade["option_name"], half_qty, "PARTIAL")
        else:
            success, order_id = send_live_exit_order(trade["option_name"], half_qty, "PARTIAL")

        if success:
            trade["order_id"] = order_id
            trade["pnl"] += (price - entry) * half_qty
            info["total_pnl"] = info["call_buy"]["pnl"] + info["put_buy"]["pnl"]
            trade["quantity"] -= half_qty
            trade["partial_booked"] = True
            trade["current_stop_price"] = entry  # move SL to cost

    # --- Full Target Check ---
    if price >= trade["full_target_price"]:
        if account_type.lower() == "paper":
            success, order_id = send_paper_exit_order(trade["option_name"], trade["quantity"], "TARGET")
        else:
            success, order_id = send_live_exit_order(trade["option_name"], trade["quantity"], "TARGET")

        if success:
            trade["order_id"] = order_id
            trade["pnl"] += (price - entry) * trade["quantity"]
            info["total_pnl"] = info["call_buy"]["pnl"] + info["put_buy"]["pnl"]
            trade["trade_flag"] = 0
            trade["quantity"] = 0
        return

    # --- Trailing logic ---
    if trade["partial_booked"]:
        pnl_points = price - entry
        trail_start = trade["trail_start_pnl"]
        trail_step  = trade["trail_step_points"]

        if pnl_points >= trail_start:
            new_stop = entry + (pnl_points // trail_step) * trail_step
            if new_stop > trade["current_stop_price"]:
                trade["current_stop_price"] = new_stop
                logging.info(f"[TRAIL STOP UPDATE] {symbol} new SL={new_stop:.2f}")

    # --- MTM Logging ---
    logging.info(f"{'Paper' if account_type.lower() == 'paper' else 'Live'} MTM {side} {symbol} LTP={price:.2f} Entry={entry:.2f}")

# ===== paper_order =====

def paper_order():
    global quantity, paper_info, df, spot_price, last_signal_candle_time

    ct = dt.now(time_zone)

    # ====================================================
    # 1. Refresh spot price (simulated)
    # ====================================================
    try:
        quote = fyers.quotes(data={"symbols": ticker})
        spot_price = quote["d"][0]["v"]["lp"]
        # logging.info(f"[SPOT REFRESH][PAPER] {ticker} Spot={spot_price}")
        logging.info(f"{GRAY}Spot={spot_price}")
    except Exception as e:
        logging.warning(f"[PAPER] Spot fetch failed: {e}")

    # ====================================================
    # 2. EOD FORCE EXIT
    # ====================================================
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
                    paper_info[leg]["filled_df"].loc[ct] = [name, None, "SELL", 0, 0, spot_price, 0]

        return

    # ====================================================
    # 3. SIGNAL EVALUATION (NEW 3M CANDLE ONLY)
    # ====================================================
    signal = None
    if not candles_3m.empty:
        last_candle_time = candles_3m.iloc[-1]["time"]

        # logging.info(f"[DEBUG] last_candle_time={last_candle_time}, last_signal_candle_time={last_signal_candle_time}, spot={spot_price}")

        if last_signal_candle_time != last_candle_time:
            last_signal_candle_time = last_candle_time

            atr, atr_source = resolve_atr(candles_3m, daily_atr)
            logging.info(f"[SIGNAL EVAL][PAPER] candle={last_candle_time} candles={len(candles_3m)} atr={atr} source={atr_source}")

            prev_day = hist_data.iloc[-1]
            cpr  = calculate_cpr(prev_day["high"], prev_day["low"], prev_day["close"])
            trad = calculate_traditional_pivots(prev_day["high"], prev_day["low"], prev_day["close"])
            cam  = calculate_camarilla_pivots(prev_day["high"], prev_day["low"], prev_day["close"])

            # logging.info(f"[DEBUG] Inputs to detect_signal: close={candles_3m.iloc[-1]['close']}, spot={spot_price}, atr={atr}")
            signal = detect_signal(cpr, trad, cam, atr, candles_3m)

    # ====================================================
    # 4. PAPER ENTRY LOGIC
    # ====================================================
    if signal:
        side, reason = signal
        logging.info(f"[SIGNAL][PAPER] {side} ({reason}) at spot={spot_price}")

        if paper_info["call_buy"]["trade_flag"] == 1 or paper_info["put_buy"]["trade_flag"] == 1:
            logging.info("[ENTRY BLOCKED][PAPER] Existing trade active, skipping new signal")
            return

        if paper_info.get("trade_count", 0) >= MAX_TRADES_PER_DAY:
            logging.info("[ENTRY][PAPER] Max trades reached")
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

                stop, full_target, partial_target, trail_start, trail_step = build_dynamic_levels(ltp, side, atr)

                # --- Entry price logic (MARKET vs LIMIT) ---
                if ORDER_TYPE == "MARKET":
                    entry_price = ltp
                else:
                    entry_price = max(ltp - ENTRY_OFFSET, 0.05)

                # --- Update ledger ---
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

                paper_info[leg]["filled_df"].loc[ct] = [opt_name, entry_price, "BUY", stop, full_target, spot_price, quantity]
                paper_info["trade_count"] = paper_info.get("trade_count", 0) + 1

                logging.info(f"[{side} ENTRY][PAPER] {opt_name} @ {entry_price:.2f} SL={stop:.2f} PT={partial_target:.2f} TG={full_target:.2f}")

    # ====================================================
    # 5. TRAILING STOP + EXIT MANAGEMENT (continuous)
    # ====================================================
    for leg, side in [("call_buy", "CALL"), ("put_buy", "PUT")]:
        if paper_info[leg]["trade_flag"] != 1:
            continue

        name = paper_info[leg]["option_name"]
        price = df.loc[name, "ltp"] if name in df.index else None
        if price is None or pd.isna(price):
            continue

        # Delegate to unified process_order() (mode-aware)
        process_order(side, name, price, paper_info, hist_data)

    # ====================================================
    # 6. SAVE TRADES
    # ====================================================
    frames = [paper_info["call_buy"]["filled_df"], paper_info["put_buy"]["filled_df"]]
    frames = [f for f in frames if not f.empty]

    if frames:
        combined = pd.concat(frames)
        combined.to_csv(f"trades_{strategy_name}_{dt.now(time_zone).date()}.csv")

    store(paper_info, account_type)

# ===== real_order =====
def real_order():
    global quantity, live_info, df, spot_price, last_signal_candle_time

    ct = dt.now(time_zone)

    # ====================================================
    # 1. Refresh spot price (live)
    # ====================================================
    try:
        quote = fyers.quotes(data={"symbols": ticker})
        spot_price = quote["d"][0]["v"]["lp"]
        # logging.info(f"[SPOT REFRESH][LIVE] {ticker} Spot={spot_price}")
        logging.info(f"Spot={spot_price}")
    except Exception as e:
        logging.warning(f"[LIVE] Spot fetch failed: {e}")

    # ====================================================
    # 2. EOD FORCE EXIT
    # ====================================================
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
                    live_info[leg]["filled_df"].loc[ct] = [name, None, "SELL", 0, 0, spot_price, 0]

        return

    # ====================================================
    # 3. SIGNAL EVALUATION (NEW 3M CANDLE ONLY)
    # ====================================================
    signal = None
    if not candles_3m.empty:
        last_candle_time = candles_3m.iloc[-1]["time"]

        # logging.info(f"[DEBUG] last_candle_time={last_candle_time}, last_signal_candle_time={last_signal_candle_time}, spot={spot_price}")

        if last_signal_candle_time != last_candle_time:
            last_signal_candle_time = last_candle_time

            atr, atr_source = resolve_atr(candles_3m, daily_atr)
            logging.info(f"[SIGNAL EVAL][LIVE] candle={last_candle_time} candles={len(candles_3m)} atr={atr} source={atr_source}")

            prev_day = hist_data.iloc[-1]
            cpr  = calculate_cpr(prev_day["high"], prev_day["low"], prev_day["close"])
            trad = calculate_traditional_pivots(prev_day["high"], prev_day["low"], prev_day["close"])
            cam  = calculate_camarilla_pivots(prev_day["high"], prev_day["low"], prev_day["close"])

            # logging.info(f"[DEBUG] Inputs to detect_signal: close={candles_3m.iloc[-1]['close']}, spot={spot_price}, atr={atr}")
            signal = detect_signal(cpr, trad, cam, atr, candles_3m)

    # ====================================================
    # 4. LIVE ENTRY LOGIC
    # ====================================================
    if signal:
        side, reason = signal
        logging.info(f"[SIGNAL][LIVE] {side} ({reason}) at spot={spot_price}")

        if live_info["call_buy"]["trade_flag"] == 1 or live_info["put_buy"]["trade_flag"] == 1:
            logging.info("[ENTRY BLOCKED][LIVE] Existing trade active, skipping new signal")
            return

        if live_info.get("trade_count", 0) >= MAX_TRADES_PER_DAY:
            logging.info("[ENTRY][LIVE] Max trades reached")
            return

        leg = "call_buy" if side == "CALL" else "put_buy"

        if live_info[leg]["trade_flag"] == 0:
            opt_type = "CE" if side == "CALL" else "PE"
            opt_name, _ = get_option_by_moneyness(
                spot_price, opt_type,
                moneyness=CALL_MONEYNESS if side == "CALL" else PUT_MONEYNESS
            )

            # Get current LTP (fast path via df; if missing, you can fallback to broker quotes)
            ltp = df.loc[opt_name, "ltp"] if (opt_name and opt_name in df.index) else None
            if ltp is None or pd.isna(ltp):
                logging.warning(f"[LIVE ENTRY] No LTP for {opt_name}, skipping entry")
            else:
                stop, full_target, partial_target, trail_start, trail_step = build_dynamic_levels(ltp, side, atr)

                # Live entries are market by design (your preference: separate SL via bot)
                entry_price = ltp

                # --- Place live order
                success, order_id = send_live_entry_order(opt_name, quantity, side)
                if not success:
                    logging.warning(f"[LIVE ENTRY] Failed to place {side} for {opt_name}")
                else:
                    # --- Update ledger
                    live_info[leg].update({
                        "option_name": opt_name,
                        "quantity": quantity,
                        "buy_price": entry_price,
                        "order_type": "MARKET",
                        "current_stop_price": stop,
                        "full_target_price": full_target,
                        "partial_target_price": partial_target,
                        "trail_start_pnl": trail_start,
                        "trail_step_points": trail_step,
                        "trade_flag": 1,
                        "partial_booked": False,
                        "pnl": 0,
                        "reason": reason,
                        "order_id": order_id,
                        "entry_time": ct,
                    })

                    live_info[leg]["filled_df"].loc[ct] = [opt_name, entry_price, "BUY", stop, full_target, spot_price, quantity]
                    live_info["trade_count"] = live_info.get("trade_count", 0) + 1

                    logging.info(f"[{side} ENTRY][LIVE] {opt_name} @ {entry_price:.2f} SL={stop:.2f} PT={partial_target:.2f} TG={full_target:.2f}")

    # ====================================================
    # 5. TRAILING STOP + EXIT MANAGEMENT (continuous)
    # ====================================================
    for leg, side in [("call_buy", "CALL"), ("put_buy", "PUT")]:
        if live_info[leg]["trade_flag"] != 1:
            continue

        name = live_info[leg]["option_name"]
        price = df.loc[name, "ltp"] if name in df.index else None
        if price is None or pd.isna(price):
            continue

        # Delegate to unified process_order() (mode-aware)
        process_order(side, name, price, live_info, hist_data)

    # ====================================================
    # 6. SAVE TRADES
    # ====================================================
    frames = [live_info["call_buy"]["filled_df"], live_info["put_buy"]["filled_df"]]
    frames = [f for f in frames if not f.empty]

    if frames:
        combined = pd.concat(frames)
        combined.to_csv(f"trades_{strategy_name}_{dt.now(time_zone).date()}.csv")

    store(live_info, account_type)

