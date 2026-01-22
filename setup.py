import os, sys, webbrowser, certifi, pandas as pd, pytz
import pendulum as dt
from fyers_apiv3 import fyersModel
from config import *
#from config import client_id, secret_key, redirect_uri

os.environ['SSL_CERT_FILE'] = certifi.where()

# ===== Access token =====
access_token = None
access_file = f'access-{dt.now(time_zone).date()}.txt'
if os.path.exists(access_file):
    with open(access_file, 'r') as f:
        access_token = f.read()
else:
    response_type = "code"
    state = "sample_state"
    session = fyersModel.SessionModel(client_id=client_id, secret_key=secret_key, redirect_uri=redirect_uri, response_type=response_type)
    response = session.generate_authcode()
    webbrowser.open(response, new=1)
    newurl = input("Enter the url: ")
    auth_code = newurl[newurl.index('auth_code=')+10:newurl.index('&state')]
    grant_type = "authorization_code"
    session = fyersModel.SessionModel(client_id=client_id, secret_key=secret_key, redirect_uri=redirect_uri, response_type=response_type, grant_type=grant_type)
    session.set_token(auth_code)
    response = session.generate_token()
    access_token = response["access_token"]
    with open(access_file, 'w') as k:
        k.write(access_token)

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

spot_price = response.get('underlyingValue')
if spot_price is None:
    try:
        quote = fyers.quotes(data={"symbols": ticker})
        spot_price = quote["d"][0]["v"]["lp"]
    except Exception:
        spot_price = option_chain['ltp'].iloc[0] if 'ltp' in option_chain.columns else None

# ===== df init =====
df = pd.DataFrame(columns=[
    'symbol','ltp','ch','chp','avg_trade_price','open_price','high_price','low_price',
    'prev_close_price','vol_traded_today','oi','pdoi','oipercent','bid_price','ask_price',
    'last_traded_time','exch_feed_time','bid_size','ask_size','last_traded_qty',
    'tot_buy_qty','tot_sell_qty','lower_ckt','upper_ckt','type','expiry'
])
df['symbol'] = symbols
df.set_index('symbol', inplace=True)

# ===== Historical Daily data =====
f = dt.now(time_zone).date() - dt.duration(days=5)
p = dt.now(time_zone).date()
hist_req = {
    "symbol": ticker, "resolution": "D", "date_format": "1",
    "range_from": f.strftime('%Y-%m-%d'), "range_to": p.strftime('%Y-%m-%d'),
    "cont_flag": "1"
}
response2 = fyers.history(data=hist_req)
hist_data = pd.DataFrame(response2['candles'])
hist_data.columns = ['date','open','high','low','close','volume']
ist = pytz.timezone('Asia/Kolkata')
hist_data['date'] = pd.to_datetime(hist_data['date'], unit='s').dt.tz_localize('UTC').dt.tz_convert(ist)
hist_data = hist_data[hist_data['date'].dt.date < dt.now(time_zone).date()]

import threading

def refresh_option_chain():
    """
    Refresh option chain and update df with latest bid/ask/ltp values.
    Returns updated df and spot_price.
    """
    global df, spot_price
    try:
        # Get expiry
        data = {"symbol": ticker, "strikecount": strike_count, "timestamp": ""}
        response = fyers.optionchain(data=data)['data']
        expiry_e = response['expiryData'][0]['expiry']

        # Get option chain for expiry
        data = {"symbol": ticker, "strikecount": strike_count, "timestamp": expiry_e}
        response = fyers.optionchain(data=data)['data']
        option_chain = pd.DataFrame(response['optionsChain'])

        # Update df with latest values
        for col in df.columns:
            if col in option_chain.columns:
                df[col] = option_chain[col].values

        # Update spot price
        spot_price = response.get('underlyingValue')
        if spot_price is None:
            try:
                quote = fyers.quotes(data={"symbols": ticker})
                spot_price = quote["d"][0]["v"]["lp"]
            except Exception:
                spot_price = option_chain['ltp'].iloc[0] if 'ltp' in option_chain.columns else None

    except Exception as e:
        logging.warning(f"[SETUP] Option chain refresh failed: {e}")

    # Schedule next refresh in 10 seconds
    threading.Timer(10, refresh_option_chain).start()
    

def log_bid_ask_spread():
    """
    Logs bid/ask spread for each option in df.
    Helps evaluate if spreads are wide enough to capture profit.
    """
    global df
    try:
        if "bid_price" in df.columns and "ask_price" in df.columns:
            df["spread"] = df["ask_price"] - df["bid_price"]

            # Filter only valid spreads
            valid_spreads = df[df["spread"] > 0][["bid_price", "ask_price", "spread"]]

            # Log top 5 widest spreads
            top_spreads = valid_spreads.sort_values("spread", ascending=False).head(5)
            logging.info("[SPREAD] Top 5 widest bid/ask spreads:")
            for symbol, row in top_spreads.iterrows():
                logging.info(
                    f"{symbol}: Bid={row['bid_price']:.2f}, Ask={row['ask_price']:.2f}, Spread={row['spread']:.2f}"
                )
    except Exception as e:
        logging.warning(f"[SPREAD] Logging failed: {e}")

        