# ===== Imports =====
import os, sys, time, pickle, asyncio, logging, certifi, webbrowser
import pandas as pd
import pendulum as dt
import pytz
from datetime import timedelta
from fyers_apiv3 import fyersModel
from fyers_apiv3.FyersWebsocket import data_ws

# ===== Credentials =====
client_id = ""
secret_key = ""
redirect_uri = ""

strategy_name = 'option_buying_pivot'

# ===== Strategy parameters =====
index_name = 'NIFTY50'
exchange = 'NSE'
ticker = f"{exchange}:{index_name}-INDEX"
strike_count = 10
strike_diff = 100
account_type = 'PAPER'   # 'PAPER' or 'LIVE'

time_zone = "Asia/Kolkata"
start_hour, start_min = 9, 30
end_hour, end_min = 15, 15
quantity = 150
buffer = 5
profit_loss_point = 20
MAX_TRADES_PER_DAY = 5

# ===== Partial profit booking levels: (ATR_multiple, sell_fraction) =====
PARTIAL_LEVELS = [(1.0, 0.5), (2.0, 0.5)]  # E.g., at 1x ATR profit, sell 50%; at 2x ATR, sell another 50%

# ===== Candle/indicator runtime constants =====
CANDLE_INTERVAL_MIN = 3
ATR_PERIOD = 14
candles_3m = pd.DataFrame(columns=['open','high','low','close','time'])
ticks_buffer = []

# === Monyness Options ===
# Option moneyness preferences
CALL_MONEYNESS = 'ITM'   # or 'OTM'
PUT_MONEYNESS  = 'ITM'   # or 'ITM'

# ===== GLOBAL STATE =====
last_signal_candle_time = None

# ===== SSL fix =====
os.environ['SSL_CERT_FILE'] = certifi.where()                                                                                                                                                                              

# ===== Logging =====
import warnings
warnings.simplefilter(action="ignore", category=FutureWarning)

# fyersModel.logging.getLogger().setLevel(logging.CRITICAL)
# logging.basicConfig(
#     level=logging.INFO,
#     filename=f'{strategy_name}_{dt.now(time_zone).date()}.log',
#     filemode='a',
#     format="%(asctime)s - %(message)s",
#     handlers=[logging.StreamHandler(sys.stdout)]
# )
log_file = f"{strategy_name}_{dt.now(time_zone).date()}.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(log_file, mode="a")
    ]
)

# ===== Access token =====
access_token = None
access_file = f'access-{dt.now(time_zone).date()}.txt'
if os.path.exists(access_file):
    with open(access_file, 'r') as f:
        access_token = f.read()
else:
    # OAuth flow
    response_type = "code"
    state = "sample_state"
    try:
        session = fyersModel.SessionModel(
            client_id=client_id,
            secret_key=secret_key,
            redirect_uri=redirect_uri,
            response_type=response_type
        )
        response = session.generate_authcode()
        webbrowser.open(response, new=1)
        newurl = input("Enter the url: ")
        auth_code = newurl[newurl.index('auth_code=')+10:newurl.index('&state')]
        grant_type = "authorization_code"
        session = fyersModel.SessionModel(
            client_id=client_id,
            secret_key=secret_key,
            redirect_uri=redirect_uri,
            response_type=response_type,
            grant_type=grant_type
        )
        session.set_token(auth_code)
        response = session.generate_token()
        access_token = response["access_token"]
        with open(access_file, 'w') as k:
            k.write(access_token)
    except Exception as e:
        print('unable to get access token', e)
        sys.exit()

# ===== Trading clock =====
start_time = dt.now(time_zone).replace(hour=start_hour, minute=start_min, second=0, microsecond=0)
end_time   = dt.now(time_zone).replace(hour=end_hour, minute=end_min,   second=0, microsecond=0)

# ===== Fyers clients =====
fyers = fyersModel.FyersModel(client_id=client_id, is_async=False, token=access_token, log_path=None)
fyers_asysc = fyersModel.FyersModel(client_id=client_id, is_async=True, token=access_token, log_path=None)

# ===== Option chain =====
data = {"symbol": ticker, "strikecount": strike_count, "timestamp": ""}
response = fyers.optionchain(data=data)['data']
expiry_e = response['expiryData'][0]['expiry']
data = {"symbol": ticker, "strikecount": strike_count, "timestamp": expiry_e}
response = fyers.optionchain(data=data)['data']
option_chain = pd.DataFrame(response['optionsChain'])
symbols = option_chain['symbol'].to_list()

# Underlying spot price (prefer chain's underlyingValue, fallback to quotes)
spot_price = response.get('underlyingValue')
if spot_price is None:
    try:
        quote = fyers.quotes(data={"symbols": ticker})
        spot_price = quote["d"][0]["v"]["lp"]
    except Exception as e:
        logging.warning(f"Unable to fetch underlying spot via quotes: {e}")
        spot_price = option_chain['ltp'].iloc[0] if 'ltp' in option_chain.columns else None
print('current spot price is', spot_price)

# ===== df init (indexed by symbol) =====
df = pd.DataFrame(
    columns=[
        'symbol','ltp','ch','chp','avg_trade_price','open_price','high_price','low_price',
        'prev_close_price','vol_traded_today','oi','pdoi','oipercent','bid_price','ask_price',
        'last_traded_time','exch_feed_time','bid_size','ask_size','last_traded_qty',
        'tot_buy_qty','tot_sell_qty','lower_ckt','upper_ckt','type','expiry'
    ]
)
df['symbol'] = symbols
df.set_index('symbol', inplace=True)

# ===== Historical Daily data =====
f = dt.now(time_zone).date() - dt.duration(days=5)
p = dt.now(time_zone).date()
hist_req = {
    "symbol": ticker,
    "resolution": "D",
    "date_format": "1",
    "range_from": f.strftime('%Y-%m-%d'),
    "range_to": p.strftime('%Y-%m-%d'),
    "cont_flag": "1"
}
response2 = fyers.history(data=hist_req)
hist_data = pd.DataFrame(response2['candles'])
hist_data.columns = ['date','open','high','low','close','volume']
ist = pytz.timezone('Asia/Kolkata')
hist_data['date'] = pd.to_datetime(hist_data['date'], unit='s').dt.tz_localize('UTC').dt.tz_convert(ist)
hist_data = hist_data[hist_data['date'].dt.date < dt.now(time_zone).date()]

# ===== DAILY ATR (INIT ONCE) =====

# ===== Level calculators and signals =====
def calculate_cpr(high, low, close):
    pivot = (high + low + close) / 3
    bc = (high + low) / 2
    tc = (pivot - bc) + pivot
    return {"Pivot": round(pivot, 2), "BC": round(bc, 2), "TC": round(tc, 2)}

def calculate_camarilla_pivots(high, low, close):
    range_val = high - low
    pivots = {
        "R3": close + (range_val * 1.1 / 4),
        "R4": close + (range_val * 1.1 / 2),
        "S3": close - (range_val * 1.1 / 4),
        "S4": close - (range_val * 1.1 / 2),
    }
    return {k: round(v, 2) for k, v in pivots.items()}

def calculate_traditional_pivots(high, low, close):
    pivot = (high + low + close) / 3
    r1 = (2 * pivot) - low
    s1 = (2 * pivot) - high
    r2 = pivot + (high - low)
    s2 = pivot - (high - low)
    return {"Pivot": round(pivot, 2),"R1": round(r1, 2),"S1": round(s1, 2),"R2": round(r2, 2),"S2": round(s2, 2)}

# globals (must exist once in your script)
ticks_buffer = []
candles_3m = pd.DataFrame(columns=["open","high","low","close","time"])
current_3m_start = None

def build_3min_candle(price):
    global ticks_buffer, candles_3m, current_3m_start

    if price is None or pd.isna(price):
        return

    ct = dt.now(time_zone)

    # --- 1️⃣ Initialize first candle aligned to 3-minute boundary ---
    if current_3m_start is None:
        minute_bucket = (ct.minute // 3) * 3
        current_3m_start = ct.replace(
            minute=minute_bucket,
            second=0,
            microsecond=0
        )
        ticks_buffer.clear()
        return

    # --- 2️⃣ Accumulate ticks ---
    ticks_buffer.append(float(price))

    # --- 3️⃣ Close candle ONLY after full 3 minutes elapsed ---
    if ct >= current_3m_start + dt.duration(minutes=3):

        if len(ticks_buffer) > 0:
            candle = {
                "open": ticks_buffer[0],
                "high": max(ticks_buffer),
                "low":  min(ticks_buffer),
                "close": ticks_buffer[-1],
                "time": current_3m_start
            }

            candles_3m.loc[len(candles_3m)] = candle

            logging.info(
                f"[3M CANDLE CLOSED] {current_3m_start.strftime('%H:%M:%S')} | "
                f"O={candle['open']} H={candle['high']} "
                f"L={candle['low']} C={candle['close']} |"
                f"Spot={spot_price}"
            )
            

        # --- 4️⃣ Advance to next 3-minute window ---
        current_3m_start += dt.duration(minutes=3)

        # --- 5️⃣ Reset buffer ---
        ticks_buffer.clear()

def calculate_atr(df_, period=14):
    if len(df_) < period + 1:
        return None

    hl = df_["high"] - df_["low"]
    hc = (df_["high"] - df_["close"].shift()).abs()
    lc = (df_["low"] - df_["close"].shift()).abs()

    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    return float(tr.rolling(period).mean().iloc[-1])

def momentum_ok(candles, side):
    last = candles.iloc[-1]
    prev = candles.iloc[-2]

    momentum = last.close - prev.close

    if side == "CALL":
        ok = momentum > 0
    else:
        ok = momentum < 0

    return ok, momentum

def resolve_atr(candles_3m, daily_atr):
    """
    Priority:
    1. Live 3m ATR (after enough candles)
    2. Daily ATR
    3. Bootstrap range (temporary)
    """
    atr_3m = calculate_atr(candles_3m)

    if atr_3m is not None:
        return atr_3m, "ATR_3M"

    if daily_atr is not None:
        return daily_atr, "ATR_DAILY"

    # Emergency bootstrap (first few minutes only)
    if len(candles_3m) >= 2:
        atr_boot = candles_3m["high"].max() - candles_3m["low"].min()
        logging.warning(f"[BOOTSTRAP ATR] using range={atr_boot:.2f}")
        return atr_boot, "ATR_BOOTSTRAP"

    return None, None

def classify_day_type(cpr_levels, atr):
    """
    Pivot Boss style day classification using CPR width vs ATR
    | Day Type               | CPR Width vs ATR  | Meaning                   |
    | ---------------------- | ----------------- | ------------------------- |
    | **TREND DAY**          | CPR ≤ 25% of ATR  | Expansion likely          |
    | **NORMAL VARIETY DAY** | CPR 25–50% of ATR | Directional but pullbacks |
    | **NORMAL DAY**         | CPR 50–75% of ATR | Rotational                |
    | **SIDEWAYS / CHOPPY**  | CPR > 75% of ATR  | Mean reversion            |

    """
    if atr is None or atr <= 0:
        return "UNKNOWN"

    cpr_width = abs(cpr_levels["TC"] - cpr_levels["BC"])
    ratio = cpr_width / atr

    if ratio <= 0.25:
        return "TREND DAY"
    elif ratio <= 0.50:
        return "NORMAL VARIETY DAY"
    elif ratio <= 0.75:
        return "NORMAL DAY"
    else:
        return "SIDEWAYS / CHOPPY"



# def detect_signal(cpr_levels, traditional_levels, camarilla_levels, atr, candles_3m_):
#     logging.info(
#         f"[DETECT_SIGNAL CALLED] candles={len(candles_3m_)} atr={atr}"
#     )

#     # ---- Guards ----
#     if len(candles_3m_) < 2 or atr is None:
#         return None

#     last = candles_3m_.iloc[-1]
#     prev = candles_3m_.iloc[-2]

#     body = abs(last.close - last.open)
#     rng  = last.high - last.low
#     if rng == 0:
#         return None

#     # ---- Levels ----
#     pivot = traditional_levels["Pivot"]
#     r1, s1, r2, s2 = (
#         traditional_levels["R1"],
#         traditional_levels["S1"],
#         traditional_levels["R2"],
#         traditional_levels["S2"],
#     )
#     r3, r4, s3, s4 = (
#         camarilla_levels["R3"],
#         camarilla_levels["R4"],
#         camarilla_levels["S3"],
#         camarilla_levels["S4"],
#     )
#     tc, bc = cpr_levels["TC"], cpr_levels["BC"]

#     # ---- Strength + Momentum ----
#     def strong(side):
#         mom_ok, momentum = momentum_ok(candles_3m_, side)
#         strength_ok = (body / rng) > 0.6
#         return strength_ok and mom_ok, momentum

#     call_ok, call_momentum = strong("CALL")
#     put_ok,  put_momentum  = strong("PUT")

#     # ---- DEBUG LOG ----
#     logging.info(
#         f"[SIGNAL CHECK] "
#         f"close={last.close:.2f} spot={spot_price:.2f} "
#         f"ATR={atr:.2f} body/range={body/rng:.2f} "
#         f"CALL_mom={call_momentum:.2f} PUT_mom={put_momentum:.2f}"
#     )

#     # ===============================
#     # Priority 1: CPR
#     # ===============================
#     if last.close > tc + 0.1 * atr and call_ok:
#         return "CALL", "BREAKOUT_CPR_TC"

#     if last.close < bc - 0.1 * atr and put_ok:
#         return "PUT", "BREAKOUT_CPR_BC"

#     # ===============================
#     # Priority 2: Camarilla
#     # ===============================
#     if last.close > r3 + 0.1 * atr and call_ok:
#         return "CALL", "BREAKOUT_R3"

#     if last.close > r4 + 0.1 * atr and call_ok:
#         return "CALL", "BREAKOUT_R4"

#     if last.close < s3 - 0.1 * atr and put_ok:
#         return "PUT", "BREAKOUT_S3"

#     if last.close < s4 - 0.1 * atr and put_ok:
#         return "PUT", "BREAKOUT_S4"

#     if last.low <= s3 and (last.close - last.low) > 0.5 * rng and call_ok:
#         return "CALL", "REJECTION_S3"

#     if last.low <= s4 and (last.close - last.low) > 0.5 * rng and call_ok:
#         return "CALL", "REJECTION_S4"

#     if last.high >= r3 and (last.high - last.close) > 0.5 * rng and put_ok:
#         return "PUT", "REJECTION_R3"

#     if last.high >= r4 and (last.high - last.close) > 0.5 * rng and put_ok:
#         return "PUT", "REJECTION_R4"
    
#     # ===============================
#     # Continuation helpers
#     # ===============================
#     def continuation_long(level):
#         return last.low <= level and last.close > level + 0.05 * atr

#     def continuation_short(level):
#         return last.high >= level and last.close < level - 0.05 * atr

    # # ===============================
    # # Continuation signals
    # # ===============================
    # if continuation_long(r4) and call_ok:
    #     return "CALL", "CONTINUATION_R4"

    # if continuation_short(s4) and put_ok:
    #     return "PUT", "CONTINUATION_S4"

    # # ===============================
    # # Priority 3: Traditional
    # # ===============================
    # if last.close > r2 + 0.1 * atr and call_ok:
    #     return "CALL", "BREAKOUT_R2"

    # if last.close < s2 - 0.1 * atr and put_ok:
    #     return "PUT", "BREAKOUT_S2"

    # if last.low <= s1 and (last.close - last.low) > 0.5 * rng and call_ok:
    #     return "CALL", "REJECTION_S1"

    # if last.high >= r1 and (last.high - last.close) > 0.5 * rng and put_ok:
    #     return "PUT", "REJECTION_R1"

    # # ===============================
    # # Priority 4: Pivot
    # # ===============================
    # if prev.close < pivot and last.close > pivot + 0.1 * atr and call_ok:
    #     return "CALL", "BREAKOUT_PIVOT"

    # if prev.close > pivot and last.close < pivot - 0.1 * atr and put_ok:
    #     return "PUT", "BREAKOUT_PIVOT"

    # return None

def detect_signal(cpr_levels, traditional_levels, camarilla_levels, atr, candles_3m_):
    """
    Returns:
        ("CALL" | "PUT", reason)  OR  None
   
    """

    # --------------------------------------------------
    # 0. Basic guards
    # --------------------------------------------------
    logging.info(
        f"[DETECT_SIGNAL CALLED] candles={len(candles_3m_)} atr={atr}"
    )

    if atr is None or atr <= 0:
        return None

    if len(candles_3m_) < 2:
        return None

    last = candles_3m_.iloc[-1]
    prev = candles_3m_.iloc[-2]

    body = abs(last.close - last.open)
    rng  = last.high - last.low
    if rng <= 0:
        return None

    # --------------------------------------------------
    # 1. Strength + Momentum filters
    # --------------------------------------------------
    strength_ok = (body / rng) >= 0.6

    call_mom_ok, call_momentum = momentum_ok(candles_3m_, "CALL")
    put_mom_ok,  put_momentum  = momentum_ok(candles_3m_, "PUT")

    call_ok = strength_ok and call_mom_ok
    put_ok  = strength_ok and put_mom_ok

    # --------------------------------------------------
    # 2. Levels
    # --------------------------------------------------
    tc, bc = cpr_levels["TC"], cpr_levels["BC"]

    pivot = traditional_levels["Pivot"]
    r2, s2 = traditional_levels["R2"], traditional_levels["S2"]

    r3, r4 = camarilla_levels["R3"], camarilla_levels["R4"]
    s3, s4 = camarilla_levels["S3"], camarilla_levels["S4"]

    # --------------------------------------------------
    # 3. Debug log (single, consistent)
    # --------------------------------------------------
    logging.info(
        f"[SIGNAL CHECK] "
        f"close={last.close:.2f} spot={spot_price:.2f} "
        f"ATR={atr:.2f} body/range={body/rng:.2f} "
        f"CALL_mom={call_momentum:.2f} PUT_mom={put_momentum:.2f}"
    )

    # --------------------------------------------------
    # Helper functions
    # --------------------------------------------------
    def continuation_long(level):
        return last.low <= level and last.close > level + 0.05 * atr

    def continuation_short(level):
        return last.high >= level and last.close < level - 0.05 * atr

    # ==================================================
    # SIGNAL PRIORITY (Pivot Boss hierarchy)
    # ==================================================

    # --------------------------------------------------
    # 1. CPR Acceptance (Trend Day trigger)
    # --------------------------------------------------
    if last.close > tc + 0.1 * atr and call_ok:
        return "CALL", "CPR_ACCEPTANCE_UP"

    if last.close < bc - 0.1 * atr and put_ok:
        return "PUT", "CPR_ACCEPTANCE_DOWN"

    # --------------------------------------------------
    # 2. Camarilla Breakouts (Volatility Expansion)
    # --------------------------------------------------
    if last.close > r4 + 0.1 * atr and call_ok:
        return "CALL", "CAM_BREAK_R4"

    if last.close > r3 + 0.1 * atr and call_ok:
        return "CALL", "CAM_BREAK_R3"

    if last.close < s4 - 0.1 * atr and put_ok:
        return "PUT", "CAM_BREAK_S4"

    if last.close < s3 - 0.1 * atr and put_ok:
        return "PUT", "CAM_BREAK_S3"

    # --------------------------------------------------
    # 3. Rejection Trades (Failed Auction)
    # --------------------------------------------------
    if last.low <= s4 and last.close > s4 + 0.3 * rng and call_ok:
        return "CALL", "REJECTION_S4"

    if last.low <= s3 and last.close > s3 + 0.3 * rng and call_ok:
        return "CALL", "REJECTION_S3"

    if last.high >= r4 and last.close < r4 - 0.3 * rng and put_ok:
        return "PUT", "REJECTION_R4"

    if last.high >= r3 and last.close < r3 - 0.3 * rng and put_ok:
        return "PUT", "REJECTION_R3"

    # --------------------------------------------------
    # 4. Pivot Flip (Bias Change)
    # --------------------------------------------------
    if prev.close < pivot and last.close > pivot + 0.1 * atr and call_ok:
        return "CALL", "PIVOT_FLIP_UP"

    if prev.close > pivot and last.close < pivot - 0.1 * atr and put_ok:
        return "PUT", "PIVOT_FLIP_DOWN"

    # --------------------------------------------------
    # 5. Continuation Trades
    # --------------------------------------------------
    if continuation_long(r4) and call_ok:
        return "CALL", "CONTINUATION_R4"

    if continuation_short(s4) and put_ok:
        return "PUT", "CONTINUATION_S4"

    # --------------------------------------------------
    # No valid signal
    # --------------------------------------------------
    return None


# ===== Build levels once (optional print) =====
prev_day = hist_data.iloc[-1]
prev_high, prev_low, prev_close = float(prev_day['high']), float(prev_day['low']), float(prev_day['close'])
cpr_levels_base = calculate_cpr(prev_high, prev_low, prev_close)
traditional_levels_base = calculate_traditional_pivots(prev_high, prev_low, prev_close)
camarilla_levels_base = calculate_camarilla_pivots(prev_high, prev_low, prev_close)
print(
    f"CPR: Pivot={cpr_levels_base['Pivot']}, TC={cpr_levels_base['TC']}, BC={cpr_levels_base['BC']}\n"
    f"Traditional: Pivot={traditional_levels_base['Pivot']}, R1={traditional_levels_base['R1']}, S1={traditional_levels_base['S1']}, "
    f"R2={traditional_levels_base['R2']}, S2={traditional_levels_base['S2']}\n"
    f"Camarilla: R3={camarilla_levels_base['R3']}, R4={camarilla_levels_base['R4']}, S3={camarilla_levels_base['S3']}, S4={camarilla_levels_base['S4']}"
)

daily_atr = calculate_atr(hist_data, period=14)

logging.info(
    f"[INIT] Daily ATR loaded = {daily_atr:.2f}"
    if daily_atr is not None else
    "[INIT] Daily ATR unavailable"
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

call_option, call_buy_strike = get_otm_option(spot_price, 'CE', 0)
put_option,  put_buy_strike  = get_otm_option(spot_price, 'PE', 0)
logging.info('started')
print('call option:', call_option)
print('put option:', put_option)

# ===== DAILY CONTEXT LOG =====
prev_day = hist_data.iloc[-1]

cpr  = calculate_cpr(prev_day["high"], prev_day["low"], prev_day["close"])
trad = calculate_traditional_pivots(prev_day["high"], prev_day["low"], prev_day["close"])
cam  = calculate_camarilla_pivots(prev_day["high"], prev_day["low"], prev_day["close"])

day_type = classify_day_type(cpr, daily_atr)

print(
    f"[DAY CONTEXT] "
    f"Spot={spot_price:.2f} | "
    f"DayType={day_type} | "
    f"CPR(TC={cpr['TC']:.2f}, BC={cpr['BC']:.2f}) | "
    f"ATR={daily_atr:.2f}"
)

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

# ===== Updated Trailing Constants =====
ATR_STOP_MULT  = 1.0
ATR_TGT_MULT   = 2.0
TRAIL_TRIGGER  = 0.0   # Start trailing immediately
TRAIL_STEP     = 0.2   # Smaller step for tighter trailing

def build_dynamic_levels(entry_price, side, atr_value):
    """
    Builds SL / TG / trailing levels for OPTION BUY trades (CALL & PUT).
    - Disables fixed target to let profits run.
    - Wider initial SL to avoid premature exits.
    """
    if atr_value is None or atr_value <= 0:
        sl = entry_price - profit_loss_point * 2  # Wider fallback SL
        tg = float('inf') if side == "CALL" else float('-inf')  # Disable fixed target
        trail_start = profit_loss_point
        trail_step = profit_loss_point / 4  # Smaller step
        return round(sl, 2), tg, trail_start, trail_step

    stop_dist = ATR_STOP_MULT * atr_value * 1.5  # Wider initial SL (1.5x ATR)
    trail_start = TRAIL_TRIGGER * atr_value
    trail_step = TRAIL_STEP * atr_value

    sl = entry_price - stop_dist if side == "CALL" else entry_price + stop_dist
    tg = float('inf') if side == "CALL" else float('-inf')  # No fixed target

    if sl >= entry_price if side == "CALL" else sl <= entry_price:
        logging.error(f"[SL ERROR] Invalid SL for {side}: entry={entry_price}, SL={sl}")
        return None

    return round(sl, 2), tg, trail_start, trail_step

def update_trailing_stop(side, current_price, entry_price, current_stop, trail_start_pnl, trail_step_points):
    """
    Aggressive trailing: Starts immediately, tightens dynamically.
    """
    pnl = (current_price - entry_price) if side == "CALL" else (entry_price - current_price)

    if pnl <= 0:
        return current_stop  # No trailing if in loss

    # Start trailing immediately (since trail_start_pnl=0)
    if side == "CALL":
        trail_amount = trail_step_points
        candidate = current_price - trail_amount
        return max(current_stop, candidate)
    else:  # PUT
        trail_amount = trail_step_points
        candidate = current_price + trail_amount
        return min(current_stop, candidate)

def check_partial_profit(side, current_price, entry_price, atr_value,
                         current_quantity, leg_info):
    """
    Books HALF quantity on first qualifying partial-profit level.
    Returns (qty_to_sell, remaining_qty, updated_buy_price)
    """

    if atr_value is None or atr_value <= 0 or current_quantity <= 1:
        return 0, current_quantity, entry_price

    pnl_per_unit = (
        current_price - entry_price
        if side == "CALL"
        else entry_price - current_price
    )

    if pnl_per_unit <= 0:
        return 0, current_quantity, entry_price

    # Track which partial levels already executed
    if "partials_done" not in leg_info:
        leg_info["partials_done"] = set()

    for multiple, _ in PARTIAL_LEVELS:
        if multiple in leg_info["partials_done"]:
            continue

        profit_threshold = multiple * atr_value

        if pnl_per_unit >= profit_threshold:
            qty_to_sell = current_quantity // 2  # ✅ ALWAYS HALF

            if qty_to_sell <= 0:
                return 0, current_quantity, entry_price

            remaining_qty = current_quantity - qty_to_sell

            # Recalculate effective buy price
            effective_buy_price = (
                ((entry_price * current_quantity) -
                 (current_price * qty_to_sell))
                / remaining_qty
            )

            leg_info["partials_done"].add(multiple)

            return qty_to_sell, remaining_qty, effective_buy_price

    return 0, current_quantity, entry_price


# ===== State init =====
if account_type == 'PAPER':
    try:
        paper_info = load(account_type)
    except:
        column_names = ['time', 'ticker', 'price', 'action', 'stop_price', 'take_profit', 'spot_price', 'quantity']
        filled_df = pd.DataFrame(columns=column_names)
        filled_df.set_index('time', inplace=True)
        paper_info = {
            'call_buy': {'option_name': call_option,'trade_flag': 0,'buy_price': 0,
                         'current_stop_price': 0,'current_profit_price': 0,'filled_df': filled_df.copy(),
                         'underlying_price_level': 0,'quantity': quantity,'pnl': 0,'trade_count': 0,
                         'remaining_quantity': quantity, 'partial_exits': [], 'effective_buy_price': 0},
            'put_buy':  {'option_name': put_option,'trade_flag': 0,'buy_price': 0,
                         'current_stop_price': 0,'current_profit_price': 0,'filled_df': filled_df.copy(),
                         'underlying_price_level': 0,'quantity': quantity,'pnl': 0,'trade_count': 0,
                         'remaining_quantity': quantity, 'partial_exits': [], 'effective_buy_price': 0},
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
        live_info = {
            'call_buy': {'option_name': call_option,'trade_flag': 0,'buy_price': 0,
                         'current_stop_price': 0,'current_profit_price': 0,'filled_df': filled_df.copy(),
                         'underlying_price_level': 0,'quantity': quantity,'pnl': 0,'trade_count': 0,
                         'remaining_quantity': quantity, 'partial_exits': [], 'effective_buy_price': 0},
            'put_buy':  {'option_name': put_option,'trade_flag': 0,'buy_price': 0,
                         'current_stop_price': 0,'current_profit_price': 0,'filled_df': filled_df.copy(),
                         'underlying_price_level': 0,'quantity': quantity,'pnl': 0,'trade_count': 0,
                         'remaining_quantity': quantity, 'partial_exits': [], 'effective_buy_price': 0},
            'condition': False,
            'total_pnl': 0,
            'trade_count': 0,
            'max_trades': MAX_TRADES_PER_DAY
        }

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

def manage_open_position(mode, side, leg_key, current_price, atr_value, ct):
    """
    mode: 'PAPER' or 'REAL'
    side: 'CALL' or 'PUT'
    leg_key: 'call_buy' or 'put_buy'
    """

    info = paper_info if mode == "PAPER" else live_info
    leg = info[leg_key]

    if leg['trade_flag'] != 1 or current_price is None or pd.isna(current_price):
        return

    entry_price = leg['buy_price']
    qty = leg['quantity']

    # ---- MTM PnL ----
    pnl_per_unit = (current_price - entry_price) if side == "CALL" else (entry_price - current_price)
    mtm_pnl = pnl_per_unit * qty
    leg['mtm_pnl'] = mtm_pnl

    logging.info(
        f"[{mode} MTM] {side} {leg['option_name']} "
        f"LTP={current_price:.2f} Entry={entry_price:.2f} MTM={mtm_pnl:.2f}"
    )

    # ---- Track MaxPnL ----
    leg['max_pnl'] = max(leg.get('max_pnl', 0), mtm_pnl)

    # ---- PARTIAL PROFIT (50%) ----
    if not leg.get('partial_done', False):
        sell_qty, rem_qty, new_entry = check_partial_profit(
            side, current_price, entry_price, atr_value, qty, leg
        )

        if sell_qty > 0:
            realized = pnl_per_unit * sell_qty
            leg['quantity'] = rem_qty
            leg['buy_price'] = new_entry
            leg['realized_pnl'] = leg.get('realized_pnl', 0) + realized
            leg['partial_done'] = True

            logging.info(
                f"[{mode} PARTIAL] {side} {leg['option_name']} "
                f"Sold={sell_qty} @ {current_price:.2f} "
                f"RealizedPnL={realized:.2f}"
            )

            leg['filled_df'].loc[ct] = [
                leg['option_name'], current_price, 'SELL-PARTIAL',
                0, 0, spot_price, sell_qty
            ]

    # ---- MAX PnL DRAWDOWN EXIT (5%) ----
    max_pnl = leg.get('max_pnl', 0)
    if max_pnl > 0:
        drawdown = (max_pnl - mtm_pnl) / max_pnl
        if drawdown >= 0.05:
            total_realized = leg.get('realized_pnl', 0) + mtm_pnl

            logging.info(
                f"[{mode} EXIT] {side} {leg['option_name']} "
                f"Exit={current_price:.2f} "
                f"TotalPnL={total_realized:.2f} "
                f"Reason=MAXPNL_DRAWDOWN"
            )

            leg['filled_df'].loc[ct] = [
                leg['option_name'], current_price, 'SELL',
                0, 0, spot_price, leg['quantity']
            ]

            leg['trade_flag'] = 2
            leg['quantity'] = 0
            leg['pnl'] = total_realized
            info['total_pnl'] = info.get('total_pnl', 0) + total_realized

            # Broker exit only for REAL
            if mode == "REAL":
                try:
                    fyers.exit_positions(data={"id": leg['option_name'] + "-INTRADAY"})
                except Exception as e:
                    logging.error(f"Broker exit failed: {e}")


# ===== paper_order =====
def paper_order():
    global quantity, paper_info, df, spot_price, last_signal_candle_time

    ct = dt.now(time_zone)

    if "last_signal_candle_time" not in globals():
        last_signal_candle_time = None

    # ----------------------------------------------------
    # 1. Spot refresh
    # ----------------------------------------------------
    try:
        if spot_price is None or pd.isna(spot_price):
            quote = fyers.quotes(data={"symbols": ticker})
            spot_price = quote["d"][0]["v"]["lp"]
    except Exception as e:
        logging.warning(f"[PAPER] Spot fetch failed: {e}")

    # ----------------------------------------------------
    # 2. EOD exit
    # ----------------------------------------------------
    if ct > end_time:
        logging.info("[PAPER] End time reached, closing open positions")

        for leg in ["call_buy", "put_buy"]:
            if paper_info[leg]["trade_flag"] == 1:
                name = paper_info[leg]["option_name"]
                price = df.loc[name, "ltp"] if name in df.index else None

                paper_info[leg]["filled_df"].loc[ct] = [
                    name, price, "SELL", 0, 0, spot_price, 0
                ]
                paper_info[leg]["quantity"] = 0
                paper_info[leg]["trade_flag"] = 2

        store(paper_info, account_type)
        return

    # ----------------------------------------------------
    # 3. Signal evaluation (new 3m candle only)
    # ----------------------------------------------------
    signal = None
    atr = None

    if not candles_3m.empty:
        last_candle_time = candles_3m.iloc[-1]["time"]

        if last_signal_candle_time != last_candle_time:
            last_signal_candle_time = last_candle_time

            atr, atr_source = resolve_atr(candles_3m, daily_atr)
            logging.info(
                f"[SIGNAL EVAL] candle={last_candle_time} atr={atr} source={atr_source}"
            )

            prev = hist_data.iloc[-1]
            cpr = calculate_cpr(prev["high"], prev["low"], prev["close"])
            trad = calculate_traditional_pivots(prev["high"], prev["low"], prev["close"])
            cam = calculate_camarilla_pivots(prev["high"], prev["low"], prev["close"])

            signal = detect_signal(cpr, trad, cam, atr, candles_3m)

    # ----------------------------------------------------
    # 4. Entry
    # ----------------------------------------------------
    if paper_info['call_buy']['trade_flag'] == 0 and paper_info['put_buy']['trade_flag'] == 0:
        signal = detect_signal(cpr, trad, cam, atr, candles_3m)
        if signal:
            side, reason = signal
            logging.info(f"[SIGNAL] {side} ({reason}) Spot={spot_price}")

            name, _ = get_option_by_moneyness(
                spot_price, 'CE' if side == "CALL" else 'PE'
            )

            if name in df.index:
                ltp = df.loc[name, 'ltp']
                leg = paper_info['call_buy'] if side == "CALL" else paper_info['put_buy']

                leg.update({
                    'option_name': name,
                    'quantity': quantity,
                    'buy_price': ltp,
                    'trade_flag': 1,
                    'max_pnl': 0,
                    'realized_pnl': 0,
                    'partial_done': False
                })

                leg['filled_df'].loc[ct] = [
                    name, ltp, 'BUY', 0, 0, spot_price, quantity
                ]

                logging.info(f"[PAPER ENTRY] {side} {name} @ {ltp}")

    # ---- EXIT MANAGEMENT ----
    if paper_info['call_buy']['trade_flag'] == 1:
        name = paper_info['call_buy']['option_name']
        price = df.loc[name, 'ltp']
        manage_open_position("PAPER", "CALL", "call_buy", price, atr, ct)

    if paper_info['put_buy']['trade_flag'] == 1:
        name = paper_info['put_buy']['option_name']
        price = df.loc[name, 'ltp']
        manage_open_position("PAPER", "PUT", "put_buy", price, atr, ct)

    # ----------------------------------------------------
    # 5. Partial / Trail / Exit
    # ----------------------------------------------------
    for leg, side in [("call_buy", "CALL"), ("put_buy", "PUT")]:
        info = paper_info[leg]
        if info["trade_flag"] != 1:
            continue

        name = info["option_name"]
        price = df.loc[name, "ltp"] if name in df.index else None
        if price is None or pd.isna(price):
            continue

        # ---- Partial profit ----
        qty_sell, rem_qty, new_entry = check_partial_profit(
            side,
            price,
            info["buy_price"],
            info["entry_atr"],
            info["quantity"],
            info
        )

        if qty_sell > 0:
            realized = (
                (price - info["buy_price"])
                if side == "CALL"
                else (info["buy_price"] - price)
            ) * qty_sell

            info["quantity"] = rem_qty
            info["buy_price"] = new_entry
            info["pnl"] += realized
            paper_info["total_pnl"] += realized

            logging.info(
                f"[{side} PARTIAL] {name} qty={qty_sell} price={price} "
                f"realized={realized:.2f}"
            )

        # ---- Trailing stop ----
        new_stop = update_trailing_stop(
            side,
            price,
            info["buy_price"],
            info["current_stop_price"],
            info["trail_start_pnl"],
            info["trail_step_points"]
        )
        info["current_stop_price"] = new_stop

        # ---- Full exit ----
        exit_hit = (
            (side == "CALL" and (price >= info["current_profit_price"] or price <= new_stop)) or
            (side == "PUT"  and (price <= info["current_profit_price"] or price >= new_stop))
        )

        if exit_hit:
            pnl = (
                (price - info["buy_price"])
                if side == "CALL"
                else (info["buy_price"] - price)
            ) * info["quantity"]

            info["pnl"] += pnl
            paper_info["total_pnl"] += pnl

            info["filled_df"].loc[ct] = [name, price, "SELL", 0, 0, spot_price, 0]
            info["quantity"] = 0
            info["trade_flag"] = 2

            logging.info(
                f"[{side} EXIT] {name} @ {price} "
                f"PnL={pnl:.2f} Total={paper_info['total_pnl']:.2f}"
            )

    store(paper_info, account_type)

def real_order():
    global quantity, live_info, df, spot_price, last_signal_candle_time

    ct = dt.now(time_zone)
    if "last_signal_candle_time" not in globals():
        last_signal_candle_time = None

    # ----------------------------------------------------
    # Spot refresh
    # ----------------------------------------------------
    try:
        if spot_price is None or pd.isna(spot_price):
            quote = fyers.quotes(data={"symbols": ticker})
            spot_price = quote["d"][0]["v"]["lp"]
    except Exception as e:
        logging.warning(f"[LIVE] Spot fetch failed: {e}")

    # ----------------------------------------------------
    # EOD exit
    # ----------------------------------------------------
    if ct > end_time:
        logging.info("[LIVE] EOD exit")

        for leg in ["call_buy", "put_buy"]:
            if live_info[leg]["trade_flag"] == 1:
                name = live_info[leg]["option_name"]
                try:
                    fyers.exit_positions({"id": name + "-INTRADAY"})
                except Exception as e:
                    logging.error(f"[LIVE] Broker exit failed {name}: {e}")

                live_info[leg]["trade_flag"] = 2
                live_info[leg]["quantity"] = 0

        store(live_info, account_type)
        return

    # ----------------------------------------------------
    # Signal eval
    # ----------------------------------------------------
    signal = None
    atr = None

    if not candles_3m.empty:
        last_candle_time = candles_3m.iloc[-1]["time"]
        if last_signal_candle_time != last_candle_time:
            last_signal_candle_time = last_candle_time

            atr, _ = resolve_atr(candles_3m, daily_atr)

            prev = hist_data.iloc[-1]
            signal = detect_signal(
                calculate_cpr(prev["high"], prev["low"], prev["close"]),
                calculate_traditional_pivots(prev["high"], prev["low"], prev["close"]),
                calculate_camarilla_pivots(prev["high"], prev["low"], prev["close"]),
                atr,
                candles_3m
            )

    # ----------------------------------------------------
    # Entry
    # ----------------------------------------------------
    if live_info['call_buy']['trade_flag'] == 0 and live_info['put_buy']['trade_flag'] == 0:
        signal = detect_signal(cpr, trad, cam, atr, candles_3m)
        if signal:
            side, reason = signal
            name, _ = get_option_by_moneyness(
                spot_price, 'CE' if side == "CALL" else 'PE'
            )

            if name in df.index:
                ltp = df.loc[name, 'ltp']
                leg = live_info['call_buy'] if side == "CALL" else live_info['put_buy']

                leg.update({
                    'option_name': name,
                    'quantity': quantity,
                    'buy_price': ltp,
                    'trade_flag': 1,
                    'max_pnl': 0,
                    'realized_pnl': 0,
                    'partial_done': False
                })

                fyers.place_order({
                    "symbol": name,
                    "qty": quantity,
                    "type": 2,
                    "side": 1,
                    "productType": "INTRADAY",
                    "limitPrice": 0,
                    "stopPrice": 0,
                    "validity": "DAY"
                })

                logging.info(f"[LIVE ENTRY] {side} {name} @ {ltp}")

    # ---- EXIT MANAGEMENT ----
    if live_info['call_buy']['trade_flag'] == 1:
        name = live_info['call_buy']['option_name']
        price = df.loc[name, 'ltp']
        manage_open_position("REAL", "CALL", "call_buy", price, atr, ct)

    if live_info['put_buy']['trade_flag'] == 1:
        name = live_info['put_buy']['option_name']
        price = df.loc[name, 'ltp']
        manage_open_position("REAL", "PUT", "put_buy", price, atr, ct)

    # ----------------------------------------------------
    # Partial / Exit
    # ----------------------------------------------------
    for leg, side in [("call_buy", "CALL"), ("put_buy", "PUT")]:
        info = live_info[leg]
        if info["trade_flag"] != 1:
            continue

        name = info["option_name"]
        price = df.loc[name, "ltp"] if name in df.index else None
        if price is None:
            continue

        qty_sell, rem_qty, new_entry = check_partial_profit(
            side, price, info["buy_price"], info["entry_atr"], info["quantity"], info
        )

        if qty_sell > 0:
            try:
                fyers.place_order({
                    "symbol": name,
                    "qty": qty_sell,
                    "type": 2,
                    "side": -1,
                    "productType": "INTRADAY"
                })
            except Exception as e:
                logging.error(f"[LIVE] Partial exit failed {name}: {e}")
                continue

            info["quantity"] = rem_qty
            info["buy_price"] = new_entry

            logging.info(f"[LIVE {side} PARTIAL] {name} qty={qty_sell}")

        # Full exit
        exit_hit = (
            (side == "CALL" and (price <= info["current_stop_price"] or price >= info["current_profit_price"])) or
            (side == "PUT"  and (price >= info["current_stop_price"] or price <= info["current_profit_price"]))
        )

        if exit_hit:
            try:
                fyers.exit_positions({"id": name + "-INTRADAY"})
            except Exception as e:
                logging.error(f"[LIVE] Exit failed {name}: {e}")

            info["trade_flag"] = 2
            info["quantity"] = 0
            logging.info(f"[LIVE {side} EXIT] {name} @ {price}")

    store(live_info, account_type)

def onmessage(ticks):
    global df, spot_price, current_3m_start

    if not ticks.get('symbol'):
        return

    symbol = ticks['symbol']

    if symbol not in df.index:
        df.loc[symbol] = [None] * len(df.columns)

    for key, value in ticks.items():
        if key in df.columns:
            df.loc[symbol, key] = value

    # Build 3m candle ONLY from underlying
    if symbol == ticker and 'ltp' in ticks:
        spot_price = ticks['ltp']
        build_3min_candle(spot_price)


def onerror(message):
    logging.error(f"Socket error: {message}")

def onclose(message):
    logging.info(f"Connection closed: {message}")

def onopen():
    # Subscribe to option symbols (you can also subscribe to underlying ticker if available)
    data_type = "SymbolUpdate"
    fyers_socket.subscribe(symbols=symbols, data_type=data_type)
    fyers_socket.keep_running()
    print('starting socket')

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
    on_message=onmessage
)

# ===== Order chasing =====
def chase_order(ord_df):
    if not ord_df.empty:
        ord_df = ord_df[ord_df['status'] == 6]
        for _, o1 in ord_df.iterrows():
            name = o1['symbol']
            current_price = df.loc[name, 'ltp'] if name in df.index else None
            if current_price is None or pd.isna(current_price):
                logging.warning(f"No LTP for {name}, skipping chase")
                continue
            try:
                if o1['type'] == 1:  # Limit order
                    id1 = o1['id']
                    lmt_price = o1['limitPrice']
                    qty = o1['qty']
                    new_lmt_price = round(lmt_price + 0.1, 2) if current_price > lmt_price else round(lmt_price - 0.1, 2)
                    logging.info(f"Chasing order {name}: old={lmt_price}, new={new_lmt_price}, qty={qty}")
                    data = {"id": id1, "type": 1, "limitPrice": new_lmt_price, "qty": qty}
                    response = fyers.modify_order(data=data)
                    logging.info(response)
            except Exception as e:
                logging.error(f"Error in chasing order: {e}")

# ===== Main async loop =====
async def main_strategy_code():
    global df
    while True:
        ct = dt.now(time_zone)

        # Close program 2 min after end time
        if ct > end_time + timedelta(minutes=2):
            logging.info('closing program')
            # break
            return # end coroutine


        # Every 5 seconds: chase orders and broker PnL
        if ct.second % 5 == 0:
            try:
                order_response = await fyers_asysc.orderbook()
                order_df = pd.DataFrame(order_response['orderBook']) if order_response.get('orderBook') else pd.DataFrame()
                chase_order(order_df)

                pos1 = await fyers_asysc.positions()
                pnl = int(pos1.get('overall', {}).get('pl_total', 0))
                logging.info(f"Live PnL from broker: {pnl}")
            except Exception as e:
                logging.error(f"Unable to fetch pnl or chase order: {e}")

        # Run strategy if df has data
        # if not df.empty:
        #     logging.info(f"Running strategy at {ct}")
        if account_type == 'PAPER':
            paper_order()
        else:
            real_order()

        await asyncio.sleep(1)

def run():
    fyers_socket.connect()
    time.sleep(2)
    try:
        asyncio.run(main_strategy_code())
    except KeyboardInterrupt:
        logging.info("Manual interrupt received, shutting down.")
    finally:
        logging.info("Program terminated.")
        sys.exit(0)


# ===== Run sockets and strategy =====
# def run():
#     # Connect socket
#     fyers_socket.connect()
#     time.sleep(2)
#     # Run strategy loop
#     asyncio.run(main_strategy_code())

if __name__ == "__main__":
    run()
