# ===== execution.py =====
import logging
import pickle
import pandas as pd
import pendulum as dt

from config import (
    time_zone, strategy_name, MAX_TRADES_PER_DAY, account_type, quantity,
    CALL_MONEYNESS, PUT_MONEYNESS, profit_loss_point,
    ATR_STOP_MULT, ATR_TGT_MULT, TRAIL_TRIGGER, TRAIL_STEP
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
#     Builds SL / TG / trailing levels for OPTION BUY trades (CALL & PUT).

#     Returns:
#         stop_loss, target, trail_start_pnl, trail_step_points
#     """

#     # ------------------------------
#     # Fallback when ATR is unavailable
#     # ------------------------------
#     if atr_value is None or atr_value <= 0:
#         sl = entry_price - profit_loss_point
#         tg = entry_price + profit_loss_point
#         trail_start = profit_loss_point
#         trail_step  = profit_loss_point / 2

#         return round(sl, 2), round(tg, 2), trail_start, trail_step

#     # ------------------------------
#     # ATR-based distances
#     # ------------------------------
#     stop_dist   = ATR_STOP_MULT * atr_value
#     target_dist = ATR_TGT_MULT  * atr_value
#     trail_start = TRAIL_TRIGGER * atr_value
#     trail_step  = TRAIL_STEP    * atr_value

#     # ------------------------------
#     # OPTION BUY LOGIC (CALL & PUT)
#     # ------------------------------
#     sl = entry_price - stop_dist
#     tg = entry_price + target_dist

#     # ------------------------------
#     # Defensive sanity check
#     # ------------------------------
#     if sl >= entry_price or tg <= entry_price:
#         logging.error(
#             f"[SL/TG ERROR] side={side} entry={entry_price} SL={sl} TG={tg}"
#         )
#         return None

#     return round(sl, 2), round(tg, 2), trail_start, trail_step

def build_dynamic_levels(entry_price, side, atr_value):
    """
    Builds SL / TG / partial TG / trailing levels for OPTION BUY trades (CALL & PUT).

    Returns:
        stop_loss, full_target, partial_target, trail_start_pnl, trail_step_points
    """

    # ------------------------------
    # Fallback when ATR is unavailable
    # ------------------------------
    if atr_value is None or atr_value <= 0:
        sl = entry_price - profit_loss_point if side == "CALL" else entry_price + profit_loss_point
        full_tg = entry_price + profit_loss_point if side == "CALL" else entry_price - profit_loss_point
        partial_tg = entry_price + profit_loss_point/2 if side == "CALL" else entry_price - profit_loss_point/2
        trail_start = profit_loss_point
        trail_step  = profit_loss_point / 2

        return round(sl, 2), round(full_tg, 2), round(partial_tg, 2), trail_start, trail_step

    # ------------------------------
    # ATR-based distances
    # ------------------------------
    stop_dist   = ATR_STOP_MULT * atr_value
    target_dist = ATR_TGT_MULT  * atr_value
    trail_start = TRAIL_TRIGGER * atr_value
    trail_step  = TRAIL_STEP    * atr_value

    # ------------------------------
    # OPTION BUY LOGIC (CALL & PUT)
    # ------------------------------
    if side == "CALL":
        sl = entry_price - stop_dist
        full_tg = entry_price + target_dist
        partial_tg = entry_price + target_dist / 2
    else:  # PUT
        sl = entry_price + stop_dist
        full_tg = entry_price - target_dist
        partial_tg = entry_price - target_dist / 2

    # ------------------------------
    # Defensive sanity check
    # ------------------------------
    if sl >= entry_price or full_tg <= entry_price:
        logging.error(
            f"[SL/TG ERROR] side={side} entry={entry_price} SL={sl} TG={full_tg}"
        )
        return None

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
def process_order(side, symbol, price, info, hist_data):
    """
    Unified MTM loop for both paper and live trades.
    side: "CALL" or "PUT"
    symbol: option symbol
    price: current LTP
    info: paper_info or live_info dict
    hist_data: DataFrame with historical OHLC data
    """

    # --- Calculate levels once per session ---
    prev_day = hist_data.iloc[-1]
    pivots = calculate_traditional_pivots(prev_day["high"], prev_day["low"], prev_day["close"])
    cpr = calculate_cpr(prev_day["high"], prev_day["low"], prev_day["close"])
    camarilla = calculate_camarilla_pivots(prev_day["high"], prev_day["low"], prev_day["close"])

    # --- Determine leg key ---
    leg = "call_buy" if side == "CALL" else "put_buy"

    # --- Update trailing stop ---
    new_stop = update_trailing_stop(
        side,
        price,
        info[leg]["buy_price"],
        info[leg]["current_stop_price"],
        info[leg]["trail_start_pnl"],
        info[leg]["trail_step_points"]
    )

    # --- Set target dynamically ---
    info[leg]["current_profit_price"] = get_dynamic_target(
        side,
        info[leg]["buy_price"],
        pivots,
        cpr,
        camarilla,
        method=info[leg]["target_method"]  # "auto" by default
    )

    # --- Check if target has been reached ---
    if (
        (side == "CALL" and price >= info[leg]["current_profit_price"]) or
        (side == "PUT" and price <= info[leg]["current_profit_price"])
    ):
        info[leg]["target_reached"] = True

    # --- Exit logic ---
    if info[leg].get("target_reached", False):
        hit_stop = (
            (side == "CALL" and price <= new_stop) or
            (side == "PUT" and price >= new_stop)
        )
        if hit_stop:
            logging.info(f"[EXIT TRAIL] {side} {symbol} LTP={price} Stop={new_stop}")
            info[leg]["trade_flag"] = 0
    else:
        hit_target = (
            (side == "CALL" and price >= info[leg]["current_profit_price"]) or
            (side == "PUT" and price <= info[leg]["current_profit_price"])
        )
        hit_stop = (
            (side == "CALL" and price <= new_stop) or
            (side == "PUT" and price >= new_stop)
        )
        if hit_target or hit_stop:
            logging.info(
                f"[EXIT] {side} {symbol} LTP={price} Stop={new_stop} Target={info[leg]['current_profit_price']}"
            )
            info[leg]["trade_flag"] = 0

    # --- PnL logging ---
    pnl = (price - info[leg]["buy_price"]) * info[leg]["quantity"]
    info[leg]["pnl"] = pnl
    info["total_pnl"] = info["call_buy"]["pnl"] + info["put_buy"]["pnl"]

    logging.info(f"{'Paper' if 'paper' in info else 'Live'} PnL ({side}): {pnl:.2f}")
    logging.info(f"{'Paper' if 'paper' in info else 'Live'} Total PnL: {info['total_pnl']:.2f}")

   

# ===== paper_order =====
def paper_order():
    global quantity, paper_info, df, spot_price, last_signal_candle_time

    ct = dt.now(time_zone)

    # ====================================================
    # 1. Refresh spot price
    # ====================================================
    try:
        if spot_price is None or pd.isna(spot_price):
            quote = fyers.quotes(data={"symbols": ticker})
            spot_price = quote["d"][0]["v"]["lp"]
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
                ltp = df.loc[name, "ltp"] if name in df.index else None

                paper_info[leg]["filled_df"].loc[ct] = [
                    name, ltp, "SELL", 0, 0, spot_price, 0
                ]
                paper_info[leg]["trade_flag"] = 2
                paper_info[leg]["quantity"] = 0

                logging.info(f"[{side} EXIT][EOD] {name} @ {ltp}")

        return

    # ====================================================
    # 3. SIGNAL EVALUATION (NEW 3M CANDLE ONLY)
    # ====================================================
    signal = None

    if not candles_3m.empty:
        last_candle_time = candles_3m.iloc[-1]["time"]

        if last_signal_candle_time != last_candle_time:
            last_signal_candle_time = last_candle_time

            atr, atr_source = resolve_atr(candles_3m, daily_atr)

            logging.info(
                f"[SIGNAL EVAL] candle={last_candle_time} "
                f"candles={len(candles_3m)} atr={atr} source={atr_source}"
            )

            prev_day = hist_data.iloc[-1]
            cpr  = calculate_cpr(prev_day["high"], prev_day["low"], prev_day["close"])
            trad = calculate_traditional_pivots(prev_day["high"], prev_day["low"], prev_day["close"])
            cam  = calculate_camarilla_pivots(prev_day["high"], prev_day["low"], prev_day["close"])

            signal = detect_signal(cpr, trad, cam, atr, candles_3m)

    # # ====================================================
    # # 4. ENTRY LOGIC
    # # ====================================================
    # if signal:
    #     side, reason = signal
    #     logging.info(f"[SIGNAL] {side} ({reason}) at spot={spot_price}")

    #     if paper_info.get("trade_count", 0) >= MAX_TRADES_PER_DAY:
    #         logging.info("[PAPER] Max trades reached")
    #     else:
    #         leg = "call_buy" if side == "CALL" else "put_buy"

    #         if paper_info[leg]["trade_flag"] == 0:
    #             opt_type = "CE" if side == "CALL" else "PE"
    #             opt_name, _ = get_option_by_moneyness(
    #                 spot_price, opt_type,
    #                 moneyness=CALL_MONEYNESS if side == "CALL" else PUT_MONEYNESS
    #             )

    #             if opt_name and opt_name in df.index:
    #                 ltp = df.loc[opt_name, "ltp"]

    #                 stop, target, trail_start, trail_step = build_dynamic_levels(
    #                     ltp, side, atr
    #                 )

    #                 paper_info[leg].update({
    #                     "option_name": opt_name,
    #                     "quantity": quantity,
    #                     "buy_price": ltp,
    #                     "current_stop_price": stop,
    #                     "current_profit_price": target,
    #                     "trail_start_pnl": trail_start,
    #                     "trail_step_points": trail_step,
    #                     "trade_flag": 1
    #                 })

    #                 entry_price = max(ltp - 5, 0.05)

    #                 paper_info[leg]["filled_df"].loc[ct] = [
    #                     opt_name, entry_price, "BUY", stop, target, spot_price, quantity
    #                 ]

    #                 paper_info["trade_count"] = paper_info.get("trade_count", 0) + 1

    #                 logging.info(
    #                     f"[{side} ENTRY] {opt_name} @ {entry_price} "
    #                     f"SL={stop:.2f} TG={target:.2f}"
    #                 )

    # ====================================================
    # 4. ENTRY LOGIC
    # ====================================================
    if signal:
        side, reason = signal
        logging.info(f"[SIGNAL] {side} ({reason}) at spot={spot_price}")

        if paper_info.get("trade_count", 0) >= MAX_TRADES_PER_DAY:
            logging.info("[ENTRY] Max trades reached")
        else:
            leg = "call_buy" if side == "CALL" else "put_buy"

            if paper_info[leg]["trade_flag"] == 0:
                opt_type = "CE" if side == "CALL" else "PE"
                opt_name, _ = get_option_by_moneyness(
                    spot_price, opt_type,
                    moneyness=CALL_MONEYNESS if side == "CALL" else PUT_MONEYNESS
                )

                if opt_name and opt_name in df.index:
                    ltp = df.loc[opt_name, "ltp"]

                    # --- Build levels (now includes partial target) ---
                    stop, full_target, partial_target, trail_start, trail_step = build_dynamic_levels(
                        ltp, side, atr
                    )

                    # --- Update trade info ---
                    paper_info[leg].update({
                        "option_name": opt_name,
                        "quantity": quantity,
                        "buy_price": ltp,
                        "current_stop_price": stop,
                        "full_target_price": full_target,
                        "partial_target_price": partial_target,
                        "trail_start_pnl": trail_start,
                        "trail_step_points": trail_step,
                        "trade_flag": 1,
                        "partial_booked": False,
                        "pnl": 0
                    })

                    entry_price = max(ltp - 5, 0.05)

                    paper_info[leg]["filled_df"].loc[ct] = [
                        opt_name, entry_price, "BUY", stop, full_target, spot_price, quantity
                    ]

                    paper_info["trade_count"] = paper_info.get("trade_count", 0) + 1

                    logging.info(
                        f"[{side} ENTRY] {opt_name} @ {entry_price:.2f} "
                        f"SL={stop:.2f} PT={partial_target:.2f} TG={full_target:.2f}"
                    )


    # ====================================================
    # 5. TRAILING STOP + EXIT MANAGEMENT (Unified)
    # ====================================================
    for leg, side in [("call_buy", "CALL"), ("put_buy", "PUT")]:
        if paper_info[leg]["trade_flag"] != 1:
            continue

        name = paper_info[leg]["option_name"]
        price = df.loc[name, "ltp"] if name in df.index else None
        if price is None or pd.isna(price):
            continue

        # Delegate to unified process_order()
        process_order(side, name, price, paper_info, hist_data)

    # ====================================================
    # 6. SAVE TRADES
    # ====================================================
    frames = [
        paper_info["call_buy"]["filled_df"],
        paper_info["put_buy"]["filled_df"]
    ]
    frames = [f for f in frames if not f.empty]  # exclude empties

    if frames:  # only concat if at least one non-empty
        combined = pd.concat(frames)
        combined.to_csv(
            f"trades_{strategy_name}_{dt.now(time_zone).date()}.csv"
        )

    store(paper_info, account_type)


# ===== real_order =====
def real_order():
    global quantity, live_info, df, spot_price, last_signal_candle_time

    ct = dt.now(time_zone)

    # ====================================================
    # 1. Refresh spot price
    # ====================================================
    try:
        if spot_price is None or pd.isna(spot_price):
            quote = fyers.quotes(data={"symbols": ticker})
            spot_price = quote["d"][0]["v"]["lp"]
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
                ltp = df.loc[name, "ltp"] if name in df.index else None

                live_info[leg]["filled_df"].loc[ct] = [
                    name, ltp, "SELL", 0, 0, spot_price, 0
                ]
                live_info[leg]["trade_flag"] = 2
                live_info[leg]["quantity"] = 0

                try:
                    fyers.exit_positions(data={"id": name + "-INTRADAY"})
                except Exception as e:
                    logging.error(f"[LIVE EXIT][EOD] {name} failed: {e}")

                logging.info(f"[{side} EXIT][EOD] {name} @ {ltp}")

        return

    # ====================================================
    # 3. SIGNAL EVALUATION (NEW 3M CANDLE ONLY)
    # ====================================================
    signal = None

    if not candles_3m.empty:
        last_candle_time = candles_3m.iloc[-1]["time"]

        if last_signal_candle_time != last_candle_time:
            last_signal_candle_time = last_candle_time

            atr, atr_source = resolve_atr(candles_3m, daily_atr)

            logging.info(
                f"[SIGNAL EVAL] candle={last_candle_time} "
                f"candles={len(candles_3m)} atr={atr} source={atr_source}"
            )

            prev_day = hist_data.iloc[-1]
            cpr  = calculate_cpr(prev_day["high"], prev_day["low"], prev_day["close"])
            trad = calculate_traditional_pivots(prev_day["high"], prev_day["low"], prev_day["close"])
            cam  = calculate_camarilla_pivots(prev_day["high"], prev_day["low"], prev_day["close"])

            signal = detect_signal(cpr, trad, cam, atr, candles_3m)

    # ====================================================
    # 4. ENTRY LOGIC
    # ====================================================
    # if signal:
    #     side, reason = signal
    #     logging.info(f"[SIGNAL][LIVE] {side} ({reason}) at spot={spot_price}")

    #     if live_info.get("trade_count", 0) >= MAX_TRADES_PER_DAY:
    #         logging.info("[LIVE] Max trades reached")
    #     else:
    #         leg = "call_buy" if side == "CALL" else "put_buy"

    #         if live_info[leg]["trade_flag"] == 0:
    #             opt_type = "CE" if side == "CALL" else "PE"
    #             opt_name, _ = get_option_by_moneyness(
    #                 spot_price, opt_type,
    #                 moneyness=CALL_MONEYNESS if side == "CALL" else PUT_MONEYNESS
    #             )

    #             if opt_name and opt_name in df.index:
    #                 ltp = df.loc[opt_name, "ltp"]

    #                 stop, target, trail_start, trail_step = build_dynamic_levels(
    #                     ltp, side, atr
    #                 )

    #                 # ---- Broker BUY ----
    #                 try:
    #                     fyers.place_order({
    #                         "symbol": opt_name,
    #                         "qty": quantity,
    #                         "type": 2,  # MARKET
    #                         "side": 1,  # BUY
    #                         "productType": "INTRADAY",
    #                         "limitPrice": 0,
    #                         "stopPrice": 0,
    #                         "validity": "DAY",
    #                         "disclosedQty": 0,
    #                         "offlineOrder": "False"
    #                     })
    #                 except Exception as e:
    #                     logging.error(f"[LIVE ENTRY FAILED] {opt_name}: {e}")
    #                     return

    #                 live_info[leg].update({
    #                     "option_name": opt_name,
    #                     "quantity": quantity,
    #                     "buy_price": ltp,
    #                     "current_stop_price": stop,
    #                     "current_profit_price": target,
    #                     "trail_start_pnl": trail_start,
    #                     "trail_step_points": trail_step,
    #                     "trade_flag": 1
    #                 })

    #                 live_info[leg]["filled_df"].loc[ct] = [
    #                     opt_name, ltp, "BUY", stop, target, spot_price, quantity
    #                 ]

    #                 live_info["trade_count"] = live_info.get("trade_count", 0) + 1

    #                 logging.info(
    #                     f"[{side} ENTRY][LIVE] {opt_name} @ {ltp:.2f} "
    #                     f"SL={stop:.2f} TG={target:.2f}"
    #                 )

    # ====================================================
    # 4. ENTRY LOGIC
    # ====================================================
    if signal:
        side, reason = signal
        logging.info(f"[SIGNAL] {side} ({reason}) at spot={spot_price}")

        if live_info.get("trade_count", 0) >= MAX_TRADES_PER_DAY:
            logging.info("[ENTRY] Max trades reached")
        else:
            leg = "call_buy" if side == "CALL" else "put_buy"

            if live_info[leg]["trade_flag"] == 0:
                opt_type = "CE" if side == "CALL" else "PE"
                opt_name, _ = get_option_by_moneyness(
                    spot_price, opt_type,
                    moneyness=CALL_MONEYNESS if side == "CALL" else PUT_MONEYNESS
                )

                if opt_name and opt_name in df.index:
                    ltp = df.loc[opt_name, "ltp"]

                    # --- Build levels (now includes partial target) ---
                    stop, full_target, partial_target, trail_start, trail_step = build_dynamic_levels(
                        ltp, side, atr
                    )

                    # --- Update trade info ---
                    live_info[leg].update({
                        "option_name": opt_name,
                        "quantity": quantity,
                        "buy_price": ltp,
                        "current_stop_price": stop,
                        "full_target_price": full_target,
                        "partial_target_price": partial_target,
                        "trail_start_pnl": trail_start,
                        "trail_step_points": trail_step,
                        "trade_flag": 1,
                        "partial_booked": False,
                        "pnl": 0
                    })

                    entry_price = max(ltp - 5, 0.05)

                    live_info[leg]["filled_df"].loc[ct] = [
                        opt_name, entry_price, "BUY", stop, full_target, spot_price, quantity
                    ]

                    live_info["trade_count"] = live_info.get("trade_count", 0) + 1

                    logging.info(
                        f"[{side} ENTRY] {opt_name} @ {entry_price:.2f} "
                        f"SL={stop:.2f} PT={partial_target:.2f} TG={full_target:.2f}"
                    )


    # ====================================================
    # 5. TRAILING STOP + EXIT MANAGEMENT (Unified)
    # ====================================================
    for leg, side in [("call_buy", "CALL"), ("put_buy", "PUT")]:
        if live_info[leg]["trade_flag"] != 1:
            continue

        name = live_info[leg]["option_name"]
        price = df.loc[name, "ltp"] if name in df.index else None
        if price is None or pd.isna(price):
            continue

        # Delegate to unified process_order()
        process_order(side, name, price, live_info, hist_data)

    # ====================================================
    # 6. SAVE TRADES
    # ====================================================
    frames = [
        live_info["call_buy"]["filled_df"],
        live_info["put_buy"]["filled_df"]
    ]
    frames = [f for f in frames if not f.empty]  # exclude empties

    if frames:  # only concat if at least one non-empty
        combined = pd.concat(frames)
        combined.to_csv(
            f"trades_{strategy_name}_{dt.now(time_zone).date()}.csv"
        )

    store(live_info, account_type)