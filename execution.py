import logging, pickle
import pandas as pd
import pendulum as dt
from setup import option_chain, fyers, paper_info, hist_data, daily_atr
from config import (
    strike_diff, time_zone, account_type, quantity, MAX_TRADES_PER_DAY,
    strategy_name, end_time, ticker, CALL_MONEYNESS, PUT_MONEYNESS
)
from data_feed import df, spot_price
from indicators import (
    resolve_atr, calculate_cpr, calculate_traditional_pivots,
    calculate_camarilla_pivots, detect_signal, build_dynamic_levels,
    update_trailing_stop, candles_3m
)


# ===== OTM option selection =====
def get_otm_option(spot_price_, side, points=100):
    """
    Returns (symbol, strike) for the requested side (CE/PE).
    If exact strike not found, falls back to nearest available strike in option_chain.
    """
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

# ===== Order placement =====
def take_limit_position(ticker_, action, quantity_, limit_price_):
    """
    action: 1 for BUY, -1 for SELL (Fyers side codes)
    """
    try:
        data = {
            "symbol": ticker_,
            "qty": quantity_,
            "type": 1,                # LIMIT
            "side": action,           # 1 = BUY
            "productType": "INTRADAY",
            "limitPrice": limit_price_,
            "stopPrice": 0,
            "validity": "DAY",
            "disclosedQty": 0,
            "offlineOrder": False,
            "stopLoss": 0,
            "takeProfit": 0
        }
        response3 = fyers.place_order(data=data)
        logging.info(response3)
        print(response3)
    except Exception as e:
        logging.error(f"Order place failed: {e}")
        print('unable to place order for some reason')

def get_option_by_moneyness(spot_price_, side, moneyness='OTM', points=0):
    """
    side: 'CE' or 'PE'
    moneyness: 'OTM' or 'ITM'
    points: additional offset (+/- strike_diff multiples)
    Returns (symbol, strike)
    """
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

    # ====================================================
    # 4. ENTRY LOGIC
    # ====================================================
    if signal:
        side, reason = signal
        logging.info(f"[SIGNAL] {side} ({reason}) at spot={spot_price}")

        if paper_info.get("trade_count", 0) >= MAX_TRADES_PER_DAY:
            logging.info("[PAPER] Max trades reached")
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

                    stop, target, trail_start, trail_step = build_dynamic_levels(
                        ltp, side, atr
                    )

                    paper_info[leg].update({
                        "option_name": opt_name,
                        "quantity": quantity,
                        "buy_price": ltp,
                        "current_stop_price": stop,
                        "current_profit_price": target,
                        "trail_start_pnl": trail_start,
                        "trail_step_points": trail_step,
                        "trade_flag": 1
                    })

                    entry_price = max(ltp - 5, 0.05)

                    paper_info[leg]["filled_df"].loc[ct] = [
                        opt_name, entry_price, "BUY", stop, target, spot_price, quantity
                    ]

                    paper_info["trade_count"] = paper_info.get("trade_count", 0) + 1

                    logging.info(
                        f"[{side} ENTRY] {opt_name} @ {entry_price} "
                        f"SL={stop:.2f} TG={target:.2f}"
                    )

    # ====================================================
    # 5. TRAILING STOP + EXIT MANAGEMENT
    # ====================================================
    for leg, side in [("call_buy", "CALL"), ("put_buy", "PUT")]:
        if paper_info[leg]["trade_flag"] != 1:
            continue

        name = paper_info[leg]["option_name"]
        price = df.loc[name, "ltp"] if name in df.index else None
        if price is None or pd.isna(price):
            continue

        logging.info(
            f"[PAPER MTM] {side} {name} LTP={price:.2f} "
            f"Entry={paper_info[leg]['buy_price']:.2f}"
        )

        new_stop = update_trailing_stop(
            side,
            price,
            paper_info[leg]["buy_price"],
            paper_info[leg]["current_stop_price"],
            paper_info[leg]["trail_start_pnl"],
            paper_info[leg]["trail_step_points"]
        )

        paper_info[leg]["current_stop_price"] = new_stop

        hit_target = (
            side == "CALL" and price >= paper_info[leg]["current_profit_price"]
        ) or (
            side == "PUT" and price <= paper_info[leg]["current_profit_price"]
        )

        hit_stop = (
            side == "CALL" and price <= new_stop
        ) or (
            side == "PUT" and price >= new_stop
        )

        if hit_target or hit_stop:
            entry = paper_info[leg]["buy_price"]
            pnl = (price - entry) if side == "CALL" else (entry - price)
            pnl *= paper_info[leg]["quantity"]

            paper_info[leg]["pnl"] += pnl
            paper_info["total_pnl"] = paper_info.get("total_pnl", 0) + pnl

            paper_info[leg]["filled_df"].loc[ct] = [
                name, price, "SELL", 0, 0, spot_price, 0
            ]

            paper_info[leg]["trade_flag"] = 2
            paper_info[leg]["quantity"] = 0

            reason = "TARGET" if hit_target else "STOPLOSS"

            logging.info(
                f"[{side} EXIT][{reason}] {name} @ {price:.2f} "
                f"PnL={pnl:.2f} Total={paper_info['total_pnl']:.2f}"
            )

    # ====================================================
    # 6. SAVE TRADES
    # ====================================================
    combined = pd.concat([
        paper_info["call_buy"]["filled_df"],
        paper_info["put_buy"]["filled_df"]
    ])

    if not combined.empty:
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
    if signal:
        side, reason = signal
        logging.info(f"[SIGNAL][LIVE] {side} ({reason}) at spot={spot_price}")

        if live_info.get("trade_count", 0) >= MAX_TRADES_PER_DAY:
            logging.info("[LIVE] Max trades reached")
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

                    stop, target, trail_start, trail_step = build_dynamic_levels(
                        ltp, side, atr
                    )

                    # ---- Broker BUY ----
                    try:
                        fyers.place_order({
                            "symbol": opt_name,
                            "qty": quantity,
                            "type": 2,  # MARKET
                            "side": 1,  # BUY
                            "productType": "INTRADAY",
                            "limitPrice": 0,
                            "stopPrice": 0,
                            "validity": "DAY",
                            "disclosedQty": 0,
                            "offlineOrder": "False"
                        })
                    except Exception as e:
                        logging.error(f"[LIVE ENTRY FAILED] {opt_name}: {e}")
                        return

                    live_info[leg].update({
                        "option_name": opt_name,
                        "quantity": quantity,
                        "buy_price": ltp,
                        "current_stop_price": stop,
                        "current_profit_price": target,
                        "trail_start_pnl": trail_start,
                        "trail_step_points": trail_step,
                        "trade_flag": 1
                    })

                    live_info[leg]["filled_df"].loc[ct] = [
                        opt_name, ltp, "BUY", stop, target, spot_price, quantity
                    ]

                    live_info["trade_count"] = live_info.get("trade_count", 0) + 1

                    logging.info(
                        f"[{side} ENTRY][LIVE] {opt_name} @ {ltp:.2f} "
                        f"SL={stop:.2f} TG={target:.2f}"
                    )

    # ====================================================
    # 5. TRAILING STOP + EXIT MANAGEMENT
    # ====================================================
    for leg, side in [("call_buy", "CALL"), ("put_buy", "PUT")]:
        if live_info[leg]["trade_flag"] != 1:
            continue

        name = live_info[leg]["option_name"]
        price = df.loc[name, "ltp"] if name in df.index else None
        if price is None or pd.isna(price):
            continue

        logging.info(
            f"[LIVE MTM] {side} {name} LTP={price:.2f} "
            f"Entry={live_info[leg]['buy_price']:.2f}"
        )

        new_stop = update_trailing_stop(
            side,
            price,
            live_info[leg]["buy_price"],
            live_info[leg]["current_stop_price"],
            live_info[leg]["trail_start_pnl"],
            live_info[leg]["trail_step_points"]
        )

        live_info[leg]["current_stop_price"] = new_stop

        hit_target = (
            side == "CALL" and price >= live_info[leg]["current_profit_price"]
        ) or (
            side == "PUT" and price <= live_info[leg]["current_profit_price"]
        )

        hit_stop = (
            side == "CALL" and price <= new_stop
        ) or (
            side == "PUT" and price >= new_stop
        )

        if hit_target or hit_stop:
            entry = live_info[leg]["buy_price"]
            pnl = (price - entry) if side == "CALL" else (entry - price)
            pnl *= live_info[leg]["quantity"]

            live_info[leg]["pnl"] += pnl
            live_info["total_pnl"] = live_info.get("total_pnl", 0) + pnl

            try:
                fyers.exit_positions(data={"id": name + "-INTRADAY"})
            except Exception as e:
                logging.error(f"[LIVE EXIT FAILED] {name}: {e}")

            live_info[leg]["filled_df"].loc[ct] = [
                name, price, "SELL", 0, 0, spot_price, 0
            ]

            live_info[leg]["trade_flag"] = 2
            live_info[leg]["quantity"] = 0

            reason = "TARGET" if hit_target else "STOPLOSS"

            logging.info(
                f"[{side} EXIT][LIVE][{reason}] {name} @ {price:.2f} "
                f"PnL={pnl:.2f} Total={live_info['total_pnl']:.2f}"
            )

    # ====================================================
    # 6. SAVE TRADES
    # ====================================================
    combined = pd.concat([
        live_info["call_buy"]["filled_df"],
        live_info["put_buy"]["filled_df"]
    ])

    if not combined.empty:
        combined.to_csv(
            f"trades_{strategy_name}_{dt.now(time_zone).date()}.csv"
        )

    store(live_info, account_type)
