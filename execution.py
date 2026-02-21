# ===== execution.py =====
import logging
import pickle
import pathlib
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

from signals import detect_signal, get_opening_range
# from tickdb import tick_db
from orchestration import update_candles_and_signals  # uses fixed ADX/CCI
from orchestration import build_indicator_dataframe   # uses fixed ADX/CCI
from position_manager import PositionManager, TradeLogger, make_replay_pm
from day_type import (make_day_type_classifier, apply_day_type_to_pm,
                      DayType, DayTypeResult)

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
    Exit logic for options buying (CALL and PUT are both LONG positions).
    All price comparisons use option LTP (injected as df_slice["close"] by process_order).

    FIXES vs original:
    - Reversal candle direction is side-aware
    - partial_booked initialised and used correctly
    - buffer_points = 5 (was 12, too large for option premiums)
    - trail_updates uses .get() to avoid KeyError
    """

    i            = len(df_slice) - 1
    side         = state["side"]
    entry_price  = state.get("buy_price", 0)
    entry_candle = state.get("entry_candle", i)
    current_ltp  = df_slice["close"].iloc[-1]

    # Minimum hold: 2 candles
    if i - entry_candle < 2:
        return False, None

    stop       = state.get("stop")
    pt         = state.get("pt")
    tg         = state.get("tg")
    trail_step = state.get("trail_step", 5)

    # 1. Hard stop loss
    if stop is not None and current_ltp <= stop:
        logging.info(f"{RED}[EXIT][SL_HIT] {side} ltp={current_ltp:.2f} stop={stop:.2f}{RESET}")
        return True, "SL_HIT"

    # 2. Full target
    if tg is not None and current_ltp >= tg:
        logging.info(f"{GREEN}[EXIT][TARGET_HIT] {side} ltp={current_ltp:.2f} tg={tg:.2f}{RESET}")
        return True, "TARGET_HIT"

    # 3. Partial target + lock break-even
    if pt is not None and not state.get("partial_booked", False):
        if current_ltp >= pt:
            state["partial_booked"] = True
            if (state.get("stop") or 0) < entry_price:
                state["stop"] = entry_price
            logging.info(
                f"{GREEN}[PARTIAL] {side} ltp={current_ltp:.2f} >= pt={pt:.2f} "
                f"→ stop locked to entry {entry_price:.2f}{RESET}"
            )

    # 4. Trailing stop (buffer = 5 option pts)
    pnl = current_ltp - entry_price
    if pnl >= 5 and trail_step > 0:
        new_stop = current_ltp - trail_step
        if new_stop > state.get("stop", 0):
            state["stop"] = new_stop
            state["trail_updates"] = state.get("trail_updates", 0) + 1
            logging.info(f"{CYAN}[TRAIL] {side} stop → {new_stop:.2f} ltp={current_ltp:.2f}{RESET}")

    # 5. Oscillator exhaustion (2-of-3)
    osc_hits = []
    try:
        cci_s = calculate_cci(df_slice) if "cci20" not in df_slice.columns else df_slice["cci20"]
        cci   = float(cci_s.iloc[-1]) if not cci_s.empty else None
        if cci and not pd.isna(cci):
            if side == "CALL" and cci >  130: osc_hits.append(f"CCI={cci:.0f}")
            if side == "PUT"  and cci < -130: osc_hits.append(f"CCI={cci:.0f}")
    except Exception: pass

    try:
        rsi_col = df_slice["rsi14"] if "rsi14" in df_slice.columns else pd.Series(dtype=float)
        rsi     = float(rsi_col.iloc[-1]) if not rsi_col.empty else None
        if rsi and not pd.isna(rsi):
            if side == "CALL" and rsi >  75: osc_hits.append(f"RSI={rsi:.0f}")
            if side == "PUT"  and rsi <  25: osc_hits.append(f"RSI={rsi:.0f}")
    except Exception: pass

    try:
        wr = williams_r(df_slice)
        if wr and not pd.isna(wr):
            if side == "CALL" and wr > -10: osc_hits.append(f"WR={wr:.0f}")
            if side == "PUT"  and wr < -88: osc_hits.append(f"WR={wr:.0f}")
    except Exception: pass

    if len(osc_hits) >= 2:
        if OSCILLATOR_EXIT_MODE == "HARD":
            logging.info(f"{YELLOW}[EXIT][OSC] {side} {'+'.join(osc_hits)}{RESET}")
            return True, "OSC_EXHAUSTION"
        else:
            if state.get("stop", 0) < entry_price:
                state["stop"] = entry_price

    # 6. Supertrend flip: 2 consecutive opposing candles
    if "supertrend_bias" in df_slice.columns and len(df_slice) >= 2:
        def norm(b):
            return "UP" if b in ("UP","BULLISH") else ("DOWN" if b in ("DOWN","BEARISH") else "N")
        b1 = norm(df_slice["supertrend_bias"].iloc[-1])
        b2 = norm(df_slice["supertrend_bias"].iloc[-2])
        if side == "CALL" and b1 == "DOWN" and b2 == "DOWN":
            logging.info(f"{YELLOW}[EXIT][ST_FLIP] CALL bearish x2{RESET}")
            return True, "ST_FLIP"
        if side == "PUT"  and b1 == "UP"   and b2 == "UP":
            logging.info(f"{YELLOW}[EXIT][ST_FLIP] PUT bullish x2{RESET}")
            return True, "ST_FLIP"

    # 7. Consecutive reversal candles (direction-aware — FIX)
    last_c = df_slice.iloc[-1]
    is_reversal = (
        (side == "CALL" and last_c["close"] < last_c["open"]) or
        (side == "PUT"  and last_c["close"] > last_c["open"])
    )
    state["consec_count"] = (state.get("consec_count", 0) + 1) if is_reversal else 0
    if state["consec_count"] >= 3:
        logging.info(f"{YELLOW}[EXIT][REVERSAL] {side} {state['consec_count']} reversal candles{RESET}")
        return True, "REVERSAL_EXIT"

    # 8. EMA plateau + momentum drop
    ema9  = df_slice["close"].ewm(span=9,  adjust=False).mean().iloc[-1]
    ema13 = df_slice["close"].ewm(span=13, adjust=False).mean().iloc[-1]
    ema_gap = abs(ema9 - ema13)

    _, momentum = momentum_ok(df_slice, side)
    momentum = momentum or 0

    prev_gap      = state.get("prev_gap", ema_gap)
    peak_momentum = state.get("peak_momentum", abs(momentum))

    if ema_gap > prev_gap:
        state["prev_gap"]      = ema_gap
        state["peak_momentum"] = max(peak_momentum, abs(momentum))
        state["plateau_count"] = 0
        return False, None

    state["plateau_count"] = state.get("plateau_count", 0) + 1
    state["prev_gap"]      = ema_gap

    if state["plateau_count"] >= 2 and abs(momentum) < peak_momentum * 0.4 and len(osc_hits) >= 1:
        logging.info(f"{YELLOW}[EXIT][MOMENTUM] {side} plateau+drop+osc{RESET}")
        return True, "MOMENTUM_EXIT"

    # 9. Time guard: 8 candles with no trail
    if i - entry_candle >= 8 and state.get("trail_updates", 0) == 0:
        logging.info(f"{YELLOW}[EXIT][TIME] {side} {i-entry_candle} candles no trail{RESET}")
        return True, "TIME_EXIT"

    return False, None


def build_dynamic_levels(entry_price, atr, side, entry_candle,
                         rr_ratio=2.0, profit_loss_point=5, candles_df=None):
    """
    Build SL/PT/TG/trail for OPTIONS BUYING (long call or long put).

    FIX: Uses % of option premium (entry_price), not underlying index ATR.
    ATR on Nifty index = 30-100 pts. Option premium = 50-300 pts.
    Setting SL = entry - 1.5*ATR = entry - 75 pts for a 100-pt option premium
    means SL is below zero — meaningless.

    % approach: SL at 18% below premium, PT at 25%, TG at 45%.
    Example: entry=150 → SL=123, PT=187.5, TG=217.5
    """
    if entry_price is None or entry_price <= 0:
        logging.warning(f"[LEVELS] Invalid entry_price={entry_price}")
        return None, None, None, None, None

    if atr is None or pd.isna(atr):
        logging.warning("[LEVELS] ATR unavailable")
        return None, None, None, None, None

    # Regime from underlying ATR
    if atr <= 80:
        mode = "normal"
    elif atr <= 200:
        mode = "volatile"
    else:
        logging.warning(f"[LEVELS][EXTREME] ATR={atr:.0f} — skipping trade")
        return None, None, None, None, None

    # % of option premium — entirely independent of underlying ATR
    if mode == "normal":
        sl_pct   = 0.18   # 18% below entry
        pt_pct   = 0.25   # partial target
        tg_pct   = 0.45   # full target
        step_pct = 0.06   # trail step
    else:  # volatile
        sl_pct   = 0.22
        pt_pct   = 0.30
        tg_pct   = 0.55
        step_pct = 0.09

    stop           = round(entry_price * (1 - sl_pct),  2)
    partial_target = round(entry_price * (1 + pt_pct),  2)
    full_target    = round(entry_price * (1 + tg_pct),  2)
    trail_start    = round(entry_price * pt_pct * 0.5,  2)
    trail_step     = round(max(entry_price * step_pct, 2.0), 2)

    logging.info(
        f"{CYAN}[LEVELS][{mode.upper()}] {side} entry={entry_price:.2f} "
        f"SL={stop:.2f}(-{sl_pct*100:.0f}%) "
        f"PT={partial_target:.2f}(+{pt_pct*100:.0f}%) "
        f"TG={full_target:.2f}(+{tg_pct*100:.0f}%) "
        f"Step={trail_step:.2f} indexATR={atr:.1f}{RESET}"
    )
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
def process_order(state, df_slice, info, spot_price,
                  account_type="paper", mode="LIVE"):
    """
    Manage exits for an active trade using SL/Target + hybrid exit logic.
    - mode="LIVE": full DB persistence + live orders
    - mode="REPLAY": skip DB writes, only simulate exits
    """

    side   = state["side"]
    symbol = state.get("option_name", "N/A")
    entry  = state.get("buy_price", 0)
    qty    = state.get("quantity", 0)

    current_candle = df_slice.iloc[-1]

    buffer = 2.0
    exit_reason = None

    # --- Explicit SL/Target checks ---
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

    # --- Hybrid exit logic ---
    if not exit_reason:
        triggered, reason = check_exit_condition(df_slice, state)
        if triggered and reason:
            exit_reason = reason

    if not exit_reason:
        return False, None

    # --- Route exit order ---
    if account_type.lower() == "paper":
        success, order_id = send_paper_exit_order(symbol, qty, exit_reason)
    else:
        if mode == "LIVE":
            success, order_id = send_live_exit_order(symbol, qty, exit_reason, order_type="MARKET")
        else:
            # REPLAY mode → simulate success, no DB
            success, order_id = True, "REPLAY_ORDER"

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
            symbol, entry, exit_price, side,
            state.get("reason", "UNKNOWN"),
            exit_reason,
            state.get("entry_candle", -1),
            len(df_slice) - 1,
            pnl_points, pnl_value,
            spot_price, qty
        ]

        logging.info(
            f"{YELLOW}[EXIT][{account_type.upper()} {exit_reason}] {side} {symbol} "
            f"EntryCandle={state['entry_candle']} ExitCandle={len(df_slice)-1} "
            f"Entry={entry:.2f} Exit={exit_price:.2f} Qty={qty} "
            f"PnL={pnl_value:.2f} (points={pnl_points:.2f}) "
            f"Reason={state.get('reason','UNKNOWN')} "
            f"TrailUpdates={state.get('trail_updates',0)}{RESET}"
        )

        if mode == "LIVE":
            update_order_status(order_id, "PENDING", qty, exit_price, symbol)
        else:
            logging.info("[REPLAY] Skipping DB update")

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

def paper_order(candles_3m, hist_yesterday_15m=None, exit=False, mode="REPLAY"):
    global quantity, paper_info, df, spot_price, last_signal_candle_time, risk_info

    COOLDOWN_SECONDS = 120
    ct = dt.now(time_zone)

    # 1. Safety reset
    for leg in ["call_buy", "put_buy"]:
        if paper_info[leg].get("trade_flag", 0) == 2:
            logging.warning(f"[RESET] lingering trade_flag=2 for {leg}")
            paper_info[leg]["trade_flag"] = 0

    # 2. Spot price
    if not candles_3m.empty:
        spot_price = candles_3m.iloc[-1]["close"]
        logging.info(f"[PAPER] Spot={spot_price}")

    # 3. End-of-day force exit
    if ct > end_time:
        logging.info("[PAPER] EOD — closing open positions")
        for leg, side in [("call_buy", "CALL"), ("put_buy", "PUT")]:
            if paper_info[leg]["trade_flag"] == 1:
                name = paper_info[leg]["option_name"]
                qty  = paper_info[leg]["quantity"]
                ep   = df.loc[name, "ltp"] if name in df.index else spot_price
                send_paper_exit_order(name, qty, "EOD")
                cleanup_trade_exit(paper_info, leg, side, name, qty, ep, "PAPER", "EOD")
        store(paper_info, account_type)
        return

    # 4. EXIT MANAGEMENT — runs every call when position is open
    # FIX: was gated by  if exit:  which was never True. Now unconditional.
    # FIX: runs BEFORE de-dup so exit is checked every second, not once per candle.
    for leg, side in [("call_buy", "CALL"), ("put_buy", "PUT")]:
        if paper_info[leg].get("trade_flag", 0) == 1:
            state = paper_info[leg]
            triggered, reason = process_order(
                state, candles_3m, paper_info, spot_price,
                account_type="paper", mode=mode
            )
            if triggered:
                paper_info["last_exit_time"] = ct
                logging.info(f"[EXIT DONE][PAPER] {side} reason={reason}")

    if candles_3m is None or candles_3m.empty:
        return

    # 5. De-duplication — only gates ENTRY signals (not exit, which ran above)
    last_candle_time = str(candles_3m.iloc[-1].get("time", len(candles_3m)))
    if last_signal_candle_time == last_candle_time:
        return   # already processed entry signal for this candle
    last_signal_candle_time = last_candle_time

    # Risk gate
    if risk_info.get("halt_trading", False):
        logging.info("[ENTRY BLOCKED][RISK] Halt active")
        return

    # Cooldown
    if paper_info.get("last_exit_time"):
        elapsed = (ct - paper_info["last_exit_time"]).total_seconds()
        if elapsed < COOLDOWN_SECONDS:
            logging.info(f"[ENTRY BLOCKED][COOLDOWN] {elapsed:.0f}s < {COOLDOWN_SECONDS}s")
            return

    # 6. Signal evaluation
    atr, _ = resolve_atr(candles_3m)

    # TPMA (stored as "vwap" column by build_indicator_dataframe)
    tpma = float(candles_3m["vwap"].iloc[-1]) if "vwap" in candles_3m.columns and not pd.isna(candles_3m["vwap"].iloc[-1]) else None

    # Opening range (first 5 bars of session)
    orb_h, orb_l = get_opening_range(candles_3m)

    # FIX: pivots from previous completed candle (iloc[-2]), not current (iloc[-1])
    pivot_src = candles_3m.iloc[-2] if len(candles_3m) >= 2 else candles_3m.iloc[-1]
    cpr  = calculate_cpr(pivot_src["high"], pivot_src["low"], pivot_src["close"])
    trad = calculate_traditional_pivots(pivot_src["high"], pivot_src["low"], pivot_src["close"])
    cam  = calculate_camarilla_pivots(pivot_src["high"], pivot_src["low"], pivot_src["close"])

    signal = detect_signal(
        candles_3m=candles_3m,
        candles_15m=hist_yesterday_15m if hist_yesterday_15m is not None else pd.DataFrame(),
        cpr_levels=cpr,
        camarilla_levels=cam,
        traditional_levels=trad,
        atr=atr,
        include_partial=False,
        current_time=ct,
        vwap=tpma,
        orb_high=orb_h,
        orb_low=orb_l,
    )

    # 7. Entry
    if not signal:
        _save_trades_paper()
        store(paper_info, account_type)
        return

    side   = signal["side"]
    reason = signal["reason"]
    source = signal.get("source", "UNKNOWN")
    logging.info(
        f"[SIGNAL][PAPER] {side} score={signal.get('score','?')} "
        f"source={source} tpma={tpma:.1f if tpma else 'N/A'} | {reason}"
    )

    # Log 15m bias (FIX: no longer hard-blocks on NEUTRAL — scoring handles it)
    if hist_yesterday_15m is not None and not hist_yesterday_15m.empty:
        bias15 = hist_yesterday_15m.iloc[-1].get("supertrend_bias", "NEUTRAL")
        logging.info(f"[BIAS][15m] {bias15}")

    if risk_info.get("halt_trading", False):
        return

    leg = "call_buy" if side == "CALL" else "put_buy"
    try:
        if paper_info[leg]["trade_flag"] == 0:
            if paper_info.get("trade_count", 0) >= paper_info.get("max_trades", MAX_TRADES_PER_DAY):
                logging.info("[ENTRY SKIP] Max trades reached")
            else:
                opt_type = "CE" if side == "CALL" else "PE"
                opt_name, strike = get_option_by_moneyness(
                    spot_price, opt_type,
                    moneyness=CALL_MONEYNESS if side == "CALL" else PUT_MONEYNESS
                )

                if opt_name and opt_name in df.index:
                    ltp_val = df.loc[opt_name, "ltp"]
                    entry_price = float(ltp_val) if (ltp_val and not pd.isna(ltp_val)) else spot_price
                    if not entry_price or entry_price <= 0:
                        logging.warning(f"[ENTRY SKIP] invalid entry_price={entry_price}")
                        return

                    # FIX: pass candles_df so build_dynamic_levels can resolve entry candle
                    stop, pt, tg, trail_start, trail_step = build_dynamic_levels(
                        entry_price, atr, side,
                        entry_candle=len(candles_3m) - 1,
                        candles_df=candles_3m
                    )
                    if stop is None:
                        logging.warning(f"[ENTRY SKIP] {side} levels failed (ATR extreme?)")
                        return

                    # FIX: all required exit-state keys initialised
                    paper_info[leg].update({
                        "option_name":    opt_name,
                        "quantity":       quantity,
                        "buy_price":      entry_price,
                        "order_type":     ORDER_TYPE,
                        "trade_flag":     1,
                        "pnl":            0,
                        "reason":         reason,
                        "source":         source,
                        "order_id":       f"paper_{opt_name}_{ct}",
                        "entry_time":     ct,
                        "entry_candle":   len(candles_3m) - 1,
                        "side":           side,
                        "stop":           stop,
                        "pt":             pt,
                        "tg":             tg,
                        "trail_start":    trail_start,
                        "trail_step":     trail_step,
                        "trail_updates":  0,
                        "consec_count":   0,
                        "prev_gap":       0,
                        "peak_momentum":  0,
                        "peak_candle":    len(candles_3m) - 1,
                        "plateau_count":  0,
                        "partial_booked": False,
                    })

                    paper_info[leg]["filled_df"].loc[ct] = [
                        opt_name, entry_price, float("nan"), side,
                        reason, None, len(candles_3m) - 1,
                        None, None, None, spot_price, quantity, source
                    ]
                    paper_info["trade_count"] = paper_info.get("trade_count", 0) + 1

                    logging.info(
                        f"{GREEN}[ENTRY][PAPER] {side} {opt_name} @ {entry_price:.2f} "
                        f"SL={stop:.2f} PT={pt:.2f} TG={tg:.2f} "
                        f"ATR={atr:.1f} step={trail_step:.2f} "
                        f"score={signal.get('score','?')} source={source}{RESET}"
                    )
                else:
                    logging.warning(f"[ENTRY SKIP] no option for {side} strike={strike}")
    except Exception as e:
        logging.error(f"[ENTRY ERROR][PAPER] {e}", exc_info=True)

    _save_trades_paper()
    store(paper_info, account_type)


def _save_trades_paper():
    frames = [paper_info["call_buy"]["filled_df"], paper_info["put_buy"]["filled_df"]]
    frames = [f for f in frames if not f.empty]
    if frames:
        pd.concat(frames).to_csv(
            f"trades_{strategy_name}_{dt.now(time_zone).date()}_PAPER.csv", index=True
        )

# =============================== Live Trading =======================================

# ===== real_order =====
def live_order(candles_3m, hist_yesterday_15m=None, exit=False):
    global quantity, live_info, df, spot_price, last_signal_candle_time, risk_info

    COOLDOWN_SECONDS = 120
    ct = dt.now(time_zone)

    # 1. Safety reset
    for leg in ["call_buy", "put_buy"]:
        if live_info[leg].get("trade_flag", 0) == 2:
            logging.warning(f"[RESET] lingering trade_flag=2 for {leg}")
            live_info[leg]["trade_flag"] = 0

    # 2. Refresh spot price
    try:
        quote      = fyers.quotes(data={"symbols": ticker})
        spot_price = quote["d"][0]["v"]["lp"]
        logging.info(f"[LIVE] Spot={spot_price}")
    except Exception as e:
        logging.warning(f"[LIVE] Spot fetch failed: {e}")

    # 3. End-of-day force exit
    if ct > end_time:
        logging.info("[LIVE] EOD — closing positions")
        for leg, side in [("call_buy", "CALL"), ("put_buy", "PUT")]:
            if live_info[leg]["trade_flag"] == 1:
                name = live_info[leg]["option_name"]
                qty  = live_info[leg]["quantity"]
                success, order_id = send_live_exit_order(name, qty, "EOD")
                if success:
                    ep = df.loc[name, "ltp"] if name in df.index else spot_price
                    cleanup_trade_exit(live_info, leg, side, name, qty, ep, "LIVE", "EOD")
                    update_order_status(order_id, "PENDING", qty, ep, name)
        return

    # 4. EXIT MANAGEMENT — unconditional, before de-dup
    # FIX: was gated by  if exit:  which was never True.
    for leg, side in [("call_buy", "CALL"), ("put_buy", "PUT")]:
        if live_info[leg].get("trade_flag", 0) == 1:
            state = live_info[leg]
            triggered, reason = process_order(
                state, candles_3m, live_info, spot_price,
                account_type="live", mode="LIVE"
            )
            if triggered:
                live_info["last_exit_time"] = ct
                logging.info(f"[EXIT DONE][LIVE] {side} reason={reason}")

    if candles_3m is None or candles_3m.empty:
        return

    # 5. De-duplication — only gates entry signals
    last_candle_time = str(candles_3m.iloc[-1].get("time", len(candles_3m)))
    if last_signal_candle_time == last_candle_time:
        return
    last_signal_candle_time = last_candle_time

    if risk_info.get("halt_trading", False):
        return

    if live_info.get("last_exit_time"):
        elapsed = (ct - live_info["last_exit_time"]).total_seconds()
        if elapsed < COOLDOWN_SECONDS:
            logging.info(f"[ENTRY BLOCKED][COOLDOWN] {elapsed:.0f}s")
            return

    # 6. Signal evaluation
    atr, _ = resolve_atr(candles_3m)

    # TPMA (stored as "vwap" column by build_indicator_dataframe)
    tpma = float(candles_3m["vwap"].iloc[-1]) if "vwap" in candles_3m.columns and not pd.isna(candles_3m["vwap"].iloc[-1]) else None

    # Opening range (first 5 bars of session)
    orb_h, orb_l = get_opening_range(candles_3m)

    # FIX: pivots from previous completed candle
    pivot_src = candles_3m.iloc[-2] if len(candles_3m) >= 2 else candles_3m.iloc[-1]
    cpr  = calculate_cpr(pivot_src["high"], pivot_src["low"], pivot_src["close"])
    trad = calculate_traditional_pivots(pivot_src["high"], pivot_src["low"], pivot_src["close"])
    cam  = calculate_camarilla_pivots(pivot_src["high"], pivot_src["low"], pivot_src["close"])

    signal = detect_signal(
        candles_3m=candles_3m,
        candles_15m=hist_yesterday_15m if hist_yesterday_15m is not None else pd.DataFrame(),
        cpr_levels=cpr,
        camarilla_levels=cam,
        traditional_levels=trad,
        atr=atr,
        include_partial=False,
        current_time=ct,
        vwap=tpma,
        orb_high=orb_h,
        orb_low=orb_l,
    )

    # 7. Entry
    if not signal:
        _save_trades_live()
        store(live_info, account_type)
        return

    side   = signal["side"]
    reason = signal["reason"]
    source = signal.get("source", "UNKNOWN")
    logging.info(
        f"[SIGNAL][LIVE] {side} score={signal.get('score','?')} "
        f"source={source} tpma={tpma:.1f if tpma else 'N/A'} | {reason}"
    )

    if hist_yesterday_15m is not None and not hist_yesterday_15m.empty:
        bias15 = hist_yesterday_15m.iloc[-1].get("supertrend_bias", "NEUTRAL")
        logging.info(f"[BIAS][15m] {bias15}")

    if risk_info.get("halt_trading", False):
        return

    leg = "call_buy" if side == "CALL" else "put_buy"
    try:
        if live_info[leg]["trade_flag"] == 0:
            if live_info.get("trade_count", 0) >= live_info.get("max_trades", MAX_TRADES_PER_DAY):
                logging.info("[ENTRY SKIP] Max trades reached")
                return

            opt_type = "CE" if side == "CALL" else "PE"
            opt_name, strike = get_option_by_moneyness(
                spot_price, opt_type,
                moneyness=CALL_MONEYNESS if side == "CALL" else PUT_MONEYNESS
            )

            if opt_name and opt_name in df.index:
                ltp_val = df.loc[opt_name, "ltp"]
                entry_price = float(ltp_val) if (ltp_val and not pd.isna(ltp_val)) else spot_price
                if not entry_price or entry_price <= 0:
                    return

                # FIX: pass candles_df
                stop, pt, tg, trail_start, trail_step = build_dynamic_levels(
                    entry_price, atr, side,
                    entry_candle=len(candles_3m) - 1,
                    candles_df=candles_3m
                )
                if stop is None:
                    logging.warning(f"[ENTRY SKIP] {side} levels failed")
                    return

                success, order_id = send_live_entry_order(opt_name, quantity, 1)
                if not success:
                    logging.warning(f"[ENTRY FAILED][LIVE] {side} {opt_name}")
                    return

                # FIX: all required exit-state keys
                live_info[leg].update({
                    "option_name":    opt_name,
                    "quantity":       quantity,
                    "buy_price":      entry_price,
                    "order_type":     ORDER_TYPE,
                    "trade_flag":     1,
                    "pnl":            0,
                    "reason":         reason,
                    "source":         source,
                    "order_id":       order_id,
                    "entry_time":     ct,
                    "entry_candle":   len(candles_3m) - 1,
                    "side":           side,
                    "stop":           stop,
                    "pt":             pt,
                    "tg":             tg,
                    "trail_start":    trail_start,
                    "trail_step":     trail_step,
                    "trail_updates":  0,
                    "consec_count":   0,
                    "prev_gap":       0,
                    "peak_momentum":  0,
                    "peak_candle":    len(candles_3m) - 1,
                    "plateau_count":  0,
                    "partial_booked": False,
                })

                live_info[leg]["filled_df"].loc[ct] = [
                    opt_name, entry_price, float("nan"), side,
                    reason, None, len(candles_3m) - 1,
                    None, None, None, spot_price, quantity, source
                ]
                live_info["trade_count"] = live_info.get("trade_count", 0) + 1

                logging.info(
                    f"{GREEN}[ENTRY][LIVE] {side} {opt_name} @ {entry_price:.2f} "
                    f"SL={stop:.2f} PT={pt:.2f} TG={tg:.2f} "
                    f"ATR={atr:.1f} score={signal.get('score','?')} source={source}{RESET}"
                )
            else:
                logging.warning(f"[ENTRY SKIP] {side} no option. opt_name={opt_name}")
    except Exception as e:
        logging.error(f"[ENTRY ERROR][LIVE] {e}", exc_info=True)

    _save_trades_live()
    store(live_info, account_type)


def _save_trades_live():
    frames = [live_info["call_buy"]["filled_df"], live_info["put_buy"]["filled_df"]]
    frames = [f for f in frames if not f.empty]
    if frames:
        pd.concat(frames).to_csv(
            f"trades_{strategy_name}_{dt.now(time_zone).date()}_LIVE.csv", index=True
        )
# ============================================== RUN Strategy ==============================================


# --- Helper: sleep until next boundary ---
def sleep_until_next_boundary(interval=180, tz="Asia/Kolkata"):
    """Sleep until next candle boundary. Only used in LIVE fallback mode."""
    now           = dt.now(tz)
    seconds       = now.minute * 60 + now.second
    next_boundary = ((seconds // interval) + 1) * interval
    sleep_secs    = next_boundary - seconds
    logging.debug(f"[SLEEP] {sleep_secs}s until next {interval}s boundary")
    time.sleep(sleep_secs)


# ─────────────────────────────────────────────────────────────────────────────
# OFFLINE REPLAY ENGINE
# ─────────────────────────────────────────────────────────────────────────────

def _print_replay_summary():
    """Print trade summary after a replay run."""
    frames = [paper_info["call_buy"]["filled_df"], paper_info["put_buy"]["filled_df"]]
    frames = [f for f in frames if not f.empty]
    if not frames:
        logging.info("[REPLAY SUMMARY] No trades taken.")
        return

    combined = pd.concat(frames)
    total    = len(combined)
    winners  = (combined["pnl_points"] > 0).sum() if "pnl_points" in combined.columns else 0
    losers   = (combined["pnl_points"] < 0).sum() if "pnl_points" in combined.columns else 0
    total_pnl = combined["pnl_value"].sum() if "pnl_value" in combined.columns else 0
    win_rate  = winners / total * 100 if total > 0 else 0

    logging.info(
        f"\n{'='*60}\n"
        f"  REPLAY SUMMARY\n"
        f"{'='*60}\n"
        f"  Total trades : {total}\n"
        f"  Winners      : {winners}  ({win_rate:.1f}%)\n"
        f"  Losers       : {losers}\n"
        f"  Total PnL    : {total_pnl:.2f}\n"
        f"{'='*60}"
    )

    if "exit_reason" in combined.columns:
        logging.info("[REPLAY] Exit reasons:")
        for reason, count in combined["exit_reason"].value_counts().items():
            logging.info(f"  {reason:20s}: {count}")

    if "source" in combined.columns:
        logging.info("[REPLAY] Signal sources:")
        for src, count in combined["source"].value_counts().items():
            logging.info(f"  {src:20s}: {count}")

    if "side" in combined.columns:
        logging.info("[REPLAY] By side:")
        for side, count in combined["side"].value_counts().items():
            logging.info(f"  {side:20s}: {count}")


def run_offline_replay(tick_db, symbols_list=None, date_str=None,
                       min_warmup_candles=35, signal_only=False,
                       output_dir=".", db_path=None):
    """
    Candle-by-candle offline replay using tick_db data. No live connection needed.

    Simulates exactly how the live bot sees data: at each 3m bar boundary
    only candles[0..i] are visible — no lookahead. Indicators are rebuilt
    incrementally on each slice.

    Args:
        tick_db             TickDatabase instance
        symbols_list        list of symbols; defaults to config symbols
        date_str            'YYYY-MM-DD' to filter replay to a specific date.
                            Warmup bars from the previous day are always prepended
                            for indicator accuracy regardless of this filter.
                            None = use all data in DB.
        min_warmup_candles  3m bars needed before evaluation starts.
                            Supertrend/ADX need 14+; 30 is a safe default.
        signal_only         True  → log signals, skip trade simulation.
                            False → simulate entries, SL/PT/TG exits, log PnL.
        output_dir          Directory for CSV trade log. Default = current dir.

    Two CSVs are saved when signal_only=False:
        signals_<sym>_<date>.csv   — every signal that fired (bar, time, side, score, reason)
        trades_<sym>_<date>.csv    — every completed trade (entry, exit, PnL)

    Typical usage:
        # Terminal:
        python execution.py                  # trade simulation
        python execution.py --signal-only    # signals only

        # From Python / notebook:
        from tickdb import tick_db
        from execution import run_strategy
        run_strategy(["NSE:NIFTY50-INDEX"], mode="OFFLINE", tick_db=tick_db,
                     date_str="2026-02-20", signal_only=True)
    """
    import os
    from collections import Counter
    # build_indicator_dataframe imported at top of module (from orchestration_v3)
    from signals import detect_signal

    if symbols_list is None:
        symbols_list = symbols if isinstance(symbols, list) else [symbols]

    os.makedirs(output_dir, exist_ok=True)

    logging.info(
        f"\n{'='*64}\n"
        f"  OFFLINE REPLAY\n"
        f"  date        = {date_str or 'all available'}\n"
        f"  signal_only = {signal_only}\n"
        f"  warmup      = {min_warmup_candles} bars\n"
        f"{'='*64}\n"
    )

    # ── Helper: find the sort/align column (prefer tz-aware "date") ───────────
    def _time_col(df):
        for c in ("date", "time", "timestamp", "candle_time"):
            if c in df.columns:
                return c
        return df.columns[0]

    # ── Helper: find string column for date-prefix filtering (YYYY-MM-DD...) ──
    # Direct SQL output has both "date" (datetime) and "time" (string).
    # Use "time" for startswith-style date filtering.
    def _str_col(df):
        return "time" if "time" in df.columns else _time_col(df)

    # ── Helper: merge and dedup two DataFrames on their time column ───────────
    def _merge_candles(hist, today):
        dfs = [d for d in [hist, today] if d is not None and not d.empty]
        if not dfs:
            return pd.DataFrame()
        out  = pd.concat(dfs, ignore_index=True)
        tcol = _time_col(out)
        return (out.drop_duplicates(subset=[tcol])
                   .sort_values(tcol)
                   .reset_index(drop=True))

    # ── Helper: option premium proxy from underlying move ─────────────────────
    # tick_db has no option prices. We simulate a 1.5× leveraged option premium.
    # Entry premium = ATM option rough value (underlying * 0.006).
    # Subsequent moves: delta ≈ 0.5, gamma amplification ≈ 1.5×.
    # This is intentionally approximate — the signal quality is what matters.
    def _option_premium(underlying_close, entry_underlying, entry_premium, side):
        move = underlying_close - entry_underlying
        if side == "PUT":
            move = -move                         # PUT profits when market falls
        delta_pnl = move * 0.5 * 1.5            # delta 0.5, 1.5× leverage
        return max(entry_premium + delta_pnl, 1.0)

    # ── Resolve DB path: explicit arg → tick_db attributes → give up ──────────
    _db_path = db_path
    if _db_path is None:
        for _attr in ("db_path", "path", "database", "db_file", "_db_path"):
            _val = getattr(tick_db, _attr, None)
            if isinstance(_val, str) and _val:
                _db_path = _val
                break
    if _db_path:
        logging.info(f"[REPLAY] DB path: {_db_path}")
    else:
        logging.warning(
            "[REPLAY] Could not determine DB path from tick_db. "
            "Pass it explicitly: run_strategy(..., db_path=r'C:\\SQLite\\ticks\\ticks_2026-02-20.db')"
        )

    # ── Helper: query SQLite directly using confirmed schema ─────────────────
    # Schema (from DB inspection):
    #   candles_3m_ist / candles_15m_ist
    #   Columns: trade_date TEXT, ist_slot TEXT, symbol TEXT,
    #            open REAL, high REAL, low REAL, close REAL, volume REAL
    #
    # Output column "date": tz-aware IST datetime — required by
    # build_indicator_dataframe and detect_signal.
    # "time": "YYYY-MM-DD HH:MM:SS" string — used for de-dup and date filtering.
    def _load_direct(interval, sym):
        if not _db_path:
            return pd.DataFrame()
        table = "candles_3m_ist" if "3m" in interval else "candles_15m_ist"
        try:
            import sqlite3
            conn  = sqlite3.connect(_db_path)
            query = f"""
                SELECT
                    trade_date || 'T' || ist_slot  AS _dt,
                    trade_date || ' ' || ist_slot  AS time,
                    trade_date, ist_slot,
                    open, high, low, close,
                    COALESCE(volume, 0) AS volume
                FROM {table}
                WHERE symbol = ?
                ORDER BY trade_date, ist_slot
            """
            df   = pd.read_sql_query(query, conn, params=(sym,))
            conn.close()

            if df.empty:
                logging.warning(f"  [{table}] 0 rows for symbol={sym}")
                return pd.DataFrame()

            # Build tz-aware "date" column (IST) — this is what downstream expects
            ist       = "Asia/Kolkata"
            df["date"] = (pd.to_datetime(df["_dt"])
                            .dt.tz_localize(ist))
            df = df.drop(columns=["_dt"])

            for col in ("open", "high", "low", "close", "volume"):
                df[col] = pd.to_numeric(df[col], errors="coerce")

            logging.info(
                f"  [{table}] {len(df)} rows  "
                f"({df['time'].iloc[0]} → {df['time'].iloc[-1]})"
            )
            return df.reset_index(drop=True)

        except Exception as e:
            logging.error(f"  Direct SQL failed ({table}): {e}")
            return pd.DataFrame()

    def _load_best(interval, sym):
        # Direct SQL first — always correct, bypasses use_yesterday date filtering
        df = _load_direct(interval, sym)
        if not df.empty:
            return df
        # Fallback to fetch_candles (works during live session)
        for flag in (False, True):
            try:
                df = tick_db.fetch_candles(interval, use_yesterday=flag, symbol=sym)
                if df is not None and not df.empty:
                    logging.debug(f"  fetch_candles(use_yesterday={flag}) → {len(df)} rows")
                    return df
            except Exception as e:
                logging.debug(f"  fetch_candles(use_yesterday={flag}): {e}")
        return pd.DataFrame()

    for sym in symbols_list:
        logging.info(f"[REPLAY] Loading candles for {sym} ...")

        # Load using best-of-both strategy (handles post-market and live sessions)
        try:
            df_3m_all  = _load_best("3m",  sym)
            df_15m_all = _load_best("15m", sym)
        except Exception as e:
            logging.error(f"[REPLAY] DB load failed for {sym}: {e}")
            continue

        # ── Load previous trading day(s) of 15m data for indicator warmup ───────
        # ADX14 needs 28 bars, CCI20 needs 20 bars → need >28 15m rows before today.
        # Scan up to 5 prev trading days from the replay DB date and prepend them.
        if _db_path and date_str:
            import sqlite3 as _sql2, os as _os2
            _db_dir   = str(pathlib.Path(_db_path).parent)
            _ref_date = pd.Timestamp(date_str)
            _prev_frames_15m = []
            _prev_frames_3m  = []
            _days_back        = 0
            _days_found       = 0
            while _days_back < 14 and _days_found < 5:
                _days_back += 1
                _cand = (_ref_date - pd.Timedelta(days=_days_back))
                if _cand.weekday() >= 5:          # skip Sat/Sun
                    continue
                _cand_str  = _cand.strftime("%Y-%m-%d")
                _cand_path = _os2.path.join(_db_dir, f"ticks_{_cand_str}.db")
                if not _os2.path.exists(_cand_path):
                    continue
                try:
                    for _tbl, _lst in [("candles_15m_ist", _prev_frames_15m),
                                        ("candles_3m_ist",  _prev_frames_3m)]:
                        _q = f"""
                            SELECT
                                trade_date || 'T' || ist_slot  AS _dt,
                                trade_date || ' ' || ist_slot  AS time,
                                trade_date, ist_slot,
                                open, high, low, close,
                                COALESCE(volume, 0) AS volume
                            FROM {_tbl}
                            WHERE symbol = ?
                            ORDER BY trade_date, ist_slot
                        """
                        with _sql2.connect(_cand_path) as _c2:
                            _tmp = pd.read_sql_query(_q, _c2, params=(sym,))
                        if not _tmp.empty:
                            _ist = "Asia/Kolkata"
                            _tmp["date"] = pd.to_datetime(_tmp["_dt"]).dt.tz_localize(_ist)
                            _tmp = _tmp.drop(columns=["_dt"])
                            for _col in ("open","high","low","close","volume"):
                                _tmp[_col] = pd.to_numeric(_tmp[_col], errors="coerce")
                            _lst.append(_tmp)
                except Exception as _ex:
                    logging.debug(f"[REPLAY WARMUP] {_cand_str}: {_ex}")
                    continue
                _days_found += 1

            if _prev_frames_15m:
                _prev_15m = pd.concat(_prev_frames_15m, ignore_index=True)
                df_15m_all = pd.concat([_prev_15m, df_15m_all], ignore_index=True)
                df_15m_all = (df_15m_all.drop_duplicates(subset=["time"])
                                         .sort_values("time")
                                         .reset_index(drop=True))
                logging.info(f"[REPLAY] 15m warmup: prepended {len(_prev_15m)} rows "
                             f"from {_days_found} prev day(s) → total {len(df_15m_all)} 15m rows")

            if _prev_frames_3m:
                _prev_3m = pd.concat(_prev_frames_3m, ignore_index=True)
                df_3m_all = pd.concat([_prev_3m, df_3m_all], ignore_index=True)
                df_3m_all = (df_3m_all.drop_duplicates(subset=["time"])
                                       .sort_values("time")
                                       .reset_index(drop=True))

        if df_3m_all.empty:
            logging.warning(
                f"[REPLAY] No 3m candles found for {sym}. "
                f"Check that the DB file has session data."
            )
            continue

        tc3  = _time_col(df_3m_all)          # datetime col for sorting/alignment
        sc3  = _str_col(df_3m_all)           # string col for date-prefix filtering
        tc15 = _time_col(df_15m_all) if not df_15m_all.empty else tc3
        sc15 = _str_col(df_15m_all)  if not df_15m_all.empty else sc3
        total_bars = len(df_3m_all)

        # Show available range
        logging.info(
            f"[REPLAY] {sym}: {total_bars} total 3m bars  "
            f"({df_3m_all.iloc[0][sc3]} → {df_3m_all.iloc[-1][sc3]})"
        )

        # Auto-reduce warmup if DB has fewer bars than requested
        effective_warmup = min_warmup_candles
        if total_bars <= min_warmup_candles:
            effective_warmup = max(14, total_bars // 3)
            logging.warning(
                f"[REPLAY] Only {total_bars} bars but warmup={min_warmup_candles}. "
                f"Auto-reduced to {effective_warmup}."
            )
            if total_bars <= effective_warmup + 1:
                logging.warning(
                    f"[REPLAY] Still not enough bars ({total_bars}). "
                    f"The DB may only contain post-market data."
                )
                continue

        # ── Optional date filter ───────────────────────────────────────────────
        replay_start_idx = effective_warmup
        if date_str:
            try:
                mask    = df_3m_all[sc3].astype(str).str.startswith(date_str)
                matches = mask[mask].index
                if len(matches) == 0:
                    available = sorted(df_3m_all[sc3].astype(str).str[:10].unique())
                    logging.warning(
                        f"[REPLAY] No bars for date={date_str}. "
                        f"Dates in DB: {available}"
                    )
                    continue
                first_match      = int(matches[0])
                replay_start_idx = max(first_match, effective_warmup)
                logging.info(
                    f"[REPLAY] Date {date_str}: bars {first_match}–{total_bars-1} "
                    f"(replay from bar {replay_start_idx})"
                )
            except Exception as e:
                logging.warning(f"[REPLAY] Date filter error: {e}")

        replay_bars = total_bars - replay_start_idx
        logging.info(
            f"[REPLAY] Warmup={replay_start_idx} | Evaluating={replay_bars} bars\n"
        )

        # ── Per-run state ─────────────────────────────────────────────────────
        signals_fired  = []
        trade_log      = []
        blocker_counts = Counter()

        global last_signal_candle_time
        last_signal_candle_time = None  # reset dedup so replay is clean

        # ── PositionManager — single source of truth for open position ─────────
        # Prevents repeated orders: once a trade is open, detect_signal is
        # bypassed entirely. Each bar instead runs pm.update() to monitor the
        # position and decide HOLD vs EXIT using:
        #   HARD_STOP   — premium drops to 50% of entry
        #   TRAIL_STOP  — ratcheting stop after 40% gain
        #   PARTIAL     — book half at 60% gain, move stop to breakeven
        #   MOMENTUM_PEAK — RSI extreme + (CCI extreme OR slope reversal)
        #   ST_FLIP     — SuperTrend flips against position
        #   MAX_HOLD    — safety valve at MAX_HOLD_BARS bars
        #   EOD_EXIT    — force close at 15:15 IST

        # ── Position Manager (imported from position_manager.py) ──────────────
        # Single source of truth for all exit logic across replay, paper, live.
        # Replacing the former inline _PM class — thresholds live in position_manager.py
        pm = make_replay_pm(lot_size=quantity)
        cooldown_until = 0   # bar index after which new entries are allowed again

        # ── Day Type Classifier — initialized BEFORE replay loop ────────────
        # BUG FIX: Previous code used slice_3m.iloc[-2] (a single 3m warmup candle)
        # as the "previous day OHLC". A 3m candle has range ~5-15 pts vs the real
        # daily range of 150-350+ pts, producing nonsense Camarilla levels and
        # always classifying as DOUBLE_DIST regardless of market conditions.
        #
        # FIX: Aggregate ALL warmup bars before replay_start_idx into daily OHLC.
        # The warmup data (df_3m_all rows 0..replay_start_idx-1) is yesterday's
        # 3m session. max(high) / min(low) / last(close) = daily OHLC.
        #
        # If date_str is set: warmup bars are rows where date != date_str.
        # If no date_str: warmup bars are rows 0..replay_start_idx-1.
        _dtc      = None
        _day_type = DayTypeResult()

        try:
            # Identify warmup rows: before replay_start_idx AND (if date given) not replay date
            _wm_end = replay_start_idx
            if date_str:
                # Use only bars strictly before the replay date as prev-day OHLC
                _prev_mask = ~df_3m_all[sc3].astype(str).str.startswith(date_str)
                _prev_bars = df_3m_all[_prev_mask]
            else:
                _prev_bars = df_3m_all.iloc[:_wm_end]

            if not _prev_bars.empty:
                _prev_h = float(_prev_bars["high"].max())
                _prev_l = float(_prev_bars["low"].min())
                _prev_c = float(_prev_bars["close"].iloc[-1])
                _prev_range = _prev_h - _prev_l
                _cpr0 = calculate_cpr(_prev_h, _prev_l, _prev_c)
                _cam0 = calculate_camarilla_pivots(_prev_h, _prev_l, _prev_c)
                _dtc  = make_day_type_classifier(
                    _cam0, _cpr0, _prev_h, _prev_l, _prev_c
                )
                logging.info(
                    f"[DAY TYPE INIT] prev OHLC: H={_prev_h:.0f} L={_prev_l:.0f} "
                    f"C={_prev_c:.0f} range={_prev_range:.0f}pts | "
                    f"R3={_cam0['r3']:.0f} R4={_cam0['r4']:.0f} "
                    f"S3={_cam0['s3']:.0f} S4={_cam0['s4']:.0f}"
                )
            else:
                logging.warning("[DAY TYPE INIT] No warmup bars found — DTC disabled")
        except Exception as _dte:
            logging.warning(f"[DAY TYPE INIT] Failed: {_dte}")

        # Pre-build a simple _FakeTime factory (avoids class-inside-loop issues)
        class _FakeTime:
            __slots__ = ("hour", "minute")
            def __init__(self, h, m):
                self.hour, self.minute = h, m

        # ── Main loop ─────────────────────────────────────────────────────────
        for i in range(replay_start_idx, total_bars):
            slice_3m = df_3m_all.iloc[:i + 1].copy()
            cur_time = slice_3m.iloc[-1][tc3]    # tz-aware datetime for 15m alignment

            # Align 15m: bars whose datetime <= current 3m datetime
            if not df_15m_all.empty:
                slice_15m = df_15m_all[df_15m_all[tc15] <= cur_time].copy()
            else:
                slice_15m = pd.DataFrame()

            # Build indicators
            try:
                slice_3m = build_indicator_dataframe(sym, slice_3m, interval="3m")
                if not slice_15m.empty:
                    slice_15m = build_indicator_dataframe(sym, slice_15m, interval="15m")
            except Exception as e:
                logging.debug(f"[REPLAY bar={i}] indicator error: {e}")
                continue

            last_row  = slice_3m.iloc[-1]
            bar_time  = last_row.get(sc3, str(cur_time))
            bar_close = float(last_row["close"])

            # ── Bar time gate ─────────────────────────────────────────────────
            ts    = pd.Timestamp(bar_time)
            bar_t = ts.hour * 60 + ts.minute

            # ── Day Type Classifier — update every bar ─────────────────────────
            # DTC is pre-initialized above (before loop) with real daily OHLC.
            # In-loop fallback removed — was the bug source (used 3m bar as daily OHLC).
            if _dtc is not None:
                _day_type = _dtc.update(slice_3m)
                # Lock classification at 12:00 (midday — stable for rest of session)
                if bar_t == 12 * 60 and _day_type.confidence in ("MEDIUM", "HIGH"):
                    _dtc.lock_classification()
                    _day_type.log()

            # ── POSITION MONITOR — runs every bar when a trade is open ─────────
            # Works in both signal_only=True AND False modes.
            # While pm.is_open(): detect_signal is bypassed — no repeated orders.
            if pm.is_open():
                decision = pm.update(i, bar_time, bar_close, last_row)
                if decision.should_exit:
                    record = pm.close(i, bar_time, bar_close,
                                      decision.exit_px, decision.reason, quantity)
                    trade_log.append(record)
                    cooldown_until = i + 2   # 2-bar cooldown before next entry
                    # After exit: don't evaluate entry on the same bar
                continue

            # ── COOLDOWN — bars immediately after an exit ─────────────────────
            if i < cooldown_until:
                blocker_counts["COOLDOWN"] = blocker_counts.get("COOLDOWN", 0) + 1
                continue

            # ── POST-MARKET GATE — no entries after close ─────────────────────
            if bar_t >= 15 * 60 + 30:
                blocker_counts["POST_MARKET"] = blocker_counts.get("POST_MARKET", 0) + 1
                continue

            # ── ENTRY EVALUATION — only runs when no position is open ──────────
            atr, _ = resolve_atr(slice_3m)

            # TPMA (stored as "vwap" by build_indicator_dataframe)
            tpma = (float(slice_3m["vwap"].iloc[-1])
                    if "vwap" in slice_3m.columns
                    and not pd.isna(slice_3m["vwap"].iloc[-1]) else None)

            orb_h, orb_l = get_opening_range(slice_3m)

            pivot_src = slice_3m.iloc[-2] if len(slice_3m) >= 2 else slice_3m.iloc[-1]
            cpr  = calculate_cpr(pivot_src["high"], pivot_src["low"], pivot_src["close"])
            trad = calculate_traditional_pivots(pivot_src["high"], pivot_src["low"], pivot_src["close"])
            cam  = calculate_camarilla_pivots(pivot_src["high"], pivot_src["low"], pivot_src["close"])

            fake_time = _FakeTime(ts.hour, ts.minute)

            try:
                signal = detect_signal(
                    candles_3m=slice_3m,
                    candles_15m=slice_15m,
                    cpr_levels=cpr,
                    camarilla_levels=cam,
                    traditional_levels=trad,
                    atr=atr,
                    include_partial=False,
                    current_time=fake_time,
                    vwap=tpma,
                    orb_high=orb_h,
                    orb_low=orb_l,
                    day_type_result=_day_type,
                )
            except Exception as e:
                logging.debug(f"[REPLAY bar={i}] detect_signal error: {e}")
                continue

            if not signal:
                blocked_by = signal.get("reason", "SCORE_LOW") if signal else "NO_SIGNAL"
                blocker_counts[blocked_by] += 1
                continue

            side   = signal["side"]
            score  = signal.get("score", "?")
            reason = signal["reason"]
            source = signal.get("source", "?")

            # Enrich signal with current ST and day type for PM entry tracking
            signal["st_bias"]  = str(last_row.get("supertrend_bias", "?"))
            signal["pivot"]    = signal.get("pivot", "")
            signal["day_type"] = _day_type.name.value

            # Apply day type overrides to PM (trail step, max hold)
            apply_day_type_to_pm(pm, _day_type)

            # Option premium approximation: ATM ≈ 0.6% of underlying
            entry_premium = round(bar_close * 0.006, 1)

            # ── Log the entry signal ───────────────────────────────────────────
            logging.info(
                f"  {GREEN}[SIGNAL→ENTRY] bar={i} {bar_time} | "
                f"{side} score={score} src={source} pivot={signal.get('pivot','')}{RESET}"
            )
            logging.info(
                f"    underlying={bar_close:.2f}  "
                f"premium≈{entry_premium:.1f}  "
                f"ST={last_row.get('supertrend_bias','?')}  "
                f"EMA9={last_row.get('ema9', float('nan')):.1f}  "
                f"CCI={last_row.get('cci20', float('nan')):.1f}  "
                f"RSI={last_row.get('rsi14', float('nan')):.1f}"
            )
            logging.info(f"    reason: {reason}")

            # Track in signals_fired (first entry only per trade)
            signals_fired.append({
                "bar":         i,
                "time":        bar_time,
                "side":        side,
                "score":       score,
                "reason":      reason,
                "source":      source,
                "pivot":       signal.get("pivot", ""),
                "underlying":  bar_close,
                "est_premium": entry_premium,
            })

            # ── Open position via PositionManager ─────────────────────────────
            # This locks out all subsequent bars from calling detect_signal
            # until this trade is exited.
            pm.open(i, bar_time, bar_close, entry_premium, signal)

        # ── Force close if still open at end of data ──────────────────────────
        if pm.is_open():
            last_ul = float(df_3m_all.iloc[-1]["close"])
            record  = pm.force_close_eod(
                total_bars - 1, df_3m_all.iloc[-1][sc3], last_ul
            )
            if record:
                trade_log.append(record)

        # ── Summary ───────────────────────────────────────────────────────────
        safe_sym  = sym.replace(":", "_")
        date_tag  = date_str or "all"
        sep       = "─" * 64

        logging.info(f"\n{sep}")
        logging.info(f"  RESULTS: {sym}  ({date_tag})")
        logging.info(sep)
        logging.info(f"  Trades taken  : {len(signals_fired)}  "
                     f"(one entry per trade — PM locks out re-entry while open)")

        if signals_fired:
            call_ct = sum(1 for s in signals_fired if s["side"] == "CALL")
            put_ct  = sum(1 for s in signals_fired if s["side"] == "PUT")
            scores  = [s["score"] for s in signals_fired if isinstance(s["score"], (int, float))]
            avg_sc  = sum(scores) / len(scores) if scores else 0
            logging.info(f"    CALL={call_ct}  PUT={put_ct}  avg_score={avg_sc:.1f}")
            sig_csv = os.path.join(output_dir, f"signals_{safe_sym}_{date_tag}.csv")
            pd.DataFrame(signals_fired).to_csv(sig_csv, index=False)
            logging.info(f"    → {sig_csv}")

        # Trade log shown in all modes (PM tracks exits even in signal_only)
        if trade_log:
            wins     = sum(1 for t in trade_log if t["pnl_points"] > 0)
            losses   = len(trade_log) - wins
            tot_pnl  = sum(t["pnl_value"] for t in trade_log)
            win_rate = wins / len(trade_log) * 100
            logging.info(f"\n  Trades closed : {len(trade_log)}")
            logging.info(f"  Win / Loss    : {wins} / {losses}  ({win_rate:.1f}%)")
            logging.info(f"  Total PnL (₹) : {tot_pnl:+.2f}")
            logging.info(f"\n  Exit breakdown:")
            for reason, cnt in Counter(t["exit_reason"] for t in trade_log).most_common():
                logging.info(f"    {reason:22s}: {cnt}")
            logging.info(f"\n  Trade details:")
            for t in trade_log:
                color = GREEN if t["pnl_points"] >= 0 else RED
                logging.info(
                    f"    {color}{t['side']:4s} entry={t['entry_time']} "
                    f"exit={t['exit_time']} held={t['bars_held']}bars "
                    f"pnl={t['pnl_points']:+.1f}pts ({t['pnl_value']:+.0f}₹) "
                    f"[{t['exit_reason']}]{RESET}"
                )
            trd_csv = os.path.join(output_dir, f"trades_{safe_sym}_{date_tag}.csv")
            pd.DataFrame(trade_log).to_csv(trd_csv, index=False)
            logging.info(f"\n  → {trd_csv}")
        else:
            logging.info("  Trades closed : 0")

        if blocker_counts:
            logging.info(f"\n  Signal blockers:")
            for reason, cnt in blocker_counts.most_common(10):
                logging.info(f"    {reason:30s}: {cnt} bars")

        logging.info(f"{sep}\n")


# ─────────────────────────────────────────────────────────────────────────────
# RUN STRATEGY — LIVE fallback + entry point for run_offline_replay
# ─────────────────────────────────────────────────────────────────────────────

def run_strategy(symbols, tz=time_zone, end_time=None,
                 replay_data=None, mode="LIVE",
                 tick_db=None, date_str=None,
                 signal_only=False, min_warmup_candles=35,
                 output_dir=".", db_path=None):
    """
    Unified strategy entry point.

    mode="LIVE"
        Single-pass fallback. Primary live execution runs via main.py.

    mode="REPLAY"
        Snapshot replay — pass replay_data={sym: (df_3m, df_15m)}.
        Calls paper_order() once with the full snapshot.

    mode="OFFLINE"
        Candle-by-candle walk-through using tick_db. No live connection needed.

        Required:
            tick_db             TickDatabase instance

        Optional:
            date_str            'YYYY-MM-DD' to replay a specific date
            signal_only         True = signals only, no trade simulation
            min_warmup_candles  bars before evaluation starts (default 30)
            output_dir          directory for CSV output (default '.')

        Usage:
            from tickdb import tick_db
            from execution import run_strategy

            # Signal evaluation only — fastest way to check what fires
            run_strategy(["NSE:NIFTY50-INDEX"], mode="OFFLINE",
                         tick_db=tick_db, date_str="2026-02-20",
                         signal_only=True)

            # Full trade simulation with PnL
            run_strategy(["NSE:NIFTY50-INDEX"], mode="OFFLINE",
                         tick_db=tick_db, date_str="2026-02-20",
                         signal_only=False, output_dir="./replay_results")
    """

    # ── OFFLINE ──────────────────────────────────────────────────────────────
    if mode == "OFFLINE":
        if tick_db is None:
            logging.error("[run_strategy] mode=OFFLINE requires tick_db argument")
            return
        run_offline_replay(
            tick_db=tick_db,
            symbols_list=symbols if isinstance(symbols, list) else [symbols],
            date_str=date_str,
            min_warmup_candles=min_warmup_candles,
            signal_only=signal_only,
            output_dir=output_dir,
            db_path=db_path,
        )
        return

    # ── LIVE (single-pass fallback) ───────────────────────────────────────────
    if mode == "LIVE":
        for sym in symbols:
            logging.info(f"{GRAY}[STRATEGY][LIVE] {sym}{RESET}")
            spot_local = None
            try:
                q = fyers.quotes(data={"symbols": sym})
                spot_local = q["d"][0]["v"]["lp"]
            except Exception as e:
                logging.debug(f"[STRATEGY] Quote API: {e}")

            candles_3m, candles_15m = update_candles_and_signals(
                symbol=sym, spot_price=spot_local, tick_db=tick_db
            )
            if candles_3m is None or candles_3m.empty:
                logging.warning(f"[STRATEGY] No candles for {sym}")
                continue

            if account_type.upper() == "PAPER":
                paper_order(candles_3m, hist_yesterday_15m=candles_15m, mode=mode)
            else:
                live_order(candles_3m, hist_yesterday_15m=candles_15m)
        return

    # ── REPLAY (snapshot) ─────────────────────────────────────────────────────
    for sym in symbols:
        if not replay_data or sym not in replay_data:
            continue
        candles_3m, candles_15m = replay_data[sym]
        if candles_3m is None or candles_3m.empty:
            continue
        logging.info(
            f"[REPLAY] {sym}: "
            f"3m={len(candles_3m)} "
            f"15m={len(candles_15m) if not candles_15m.empty else 0}"
        )
        if account_type.upper() == "PAPER":
            paper_order(candles_3m, hist_yesterday_15m=candles_15m, mode="REPLAY")
        else:
            live_order(candles_3m, hist_yesterday_15m=candles_15m)

    _print_replay_summary()


# ─────────────────────────────────────────────────────────────────────────────
# STANDALONE ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    """
    Run offline replay from the terminal — no live connection needed.

    Usage:
        python execution.py                           # trade simulation, today's DB data
        python execution.py --signal-only             # signals only
        python execution.py --date 2026-02-20         # specific date
        python execution.py --date 2026-02-20 --signal-only
        python execution.py --out ./results           # custom output directory
        python execution.py --warmup 50               # more warmup bars
    """
    import sys, os, argparse

    parser = argparse.ArgumentParser(description="Offline replay using tick_db data")
    parser.add_argument("--date",        default=None,  help="Date to replay YYYY-MM-DD")
    parser.add_argument("--signal-only", action="store_true", help="Log signals only, no trade sim")
    parser.add_argument("--out",         default=".",   help="Output directory for CSVs")
    parser.add_argument("--warmup",      default=30,    type=int, help="Warmup bars (default 30)")
    parser.add_argument("--sym",         default="NSE:NIFTY50-INDEX", help="Symbol to replay")
    parser.add_argument("--db",          default=None,
                        help=r"Direct SQLite DB path, e.g. C:\SQLite\ticks\ticks_2026-02-20.db "
                             "(use when fetch_candles returns too few rows post-market)")
    args = parser.parse_args()

    try:
        from tickdb import tick_db as _tick_db
    except ImportError:
        logging.error("tickdb module not found — cannot run offline replay")
        sys.exit(1)

    run_strategy(
        symbols=[args.sym],
        mode="OFFLINE",
        tick_db=_tick_db,
        date_str=args.date,
        signal_only=args.signal_only,
        min_warmup_candles=args.warmup,
        output_dir=args.out,
        db_path=args.db,
    )