import os, sys, logging, webbrowser, certifi
import pandas as pd
import pendulum as dt
import pytz
from fyers_apiv3 import fyersModel
from config import (
    client_id, secret_key, redirect_uri, strategy_name, time_zone,
    ticker, strike_count
)

# ===== SSL fix =====
os.environ['SSL_CERT_FILE'] = certifi.where()

# ===== Logging setup =====
log_file = f"{strategy_name}_{dt.now(time_zone).date()}.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(log_file, mode="a")
    ]
)

# ===== Access token management =====
access_file = "access.txt"   # single file reused every run

if os.path.exists(access_file):
    with open(access_file, "r") as f:
        access_token = f.read().strip()
    logging.info("Access token loaded from access.txt")
else:
    response_type = "code"
    state = "sample_state"
    try:
        # Step 1: Generate auth code
        session = fyersModel.SessionModel(
            client_id=client_id,
            secret_key=secret_key,
            redirect_uri=redirect_uri,
            response_type=response_type
        )
        response = session.generate_authcode()
        webbrowser.open(response, new=1)
        newurl = input("Enter the redirected URL: ")
        auth_code = newurl[newurl.index("auth_code=")+10:newurl.index("&state")]

        # Step 2: Exchange for access token
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

        # Save token for future runs
        with open(access_file, "w") as k:
            k.write(access_token)
        logging.info("Access token generated and stored in access.txt")
    except Exception as e:
        logging.error(f"Unable to get access token: {e}")
        sys.exit()

# ===== Fyers clients =====
fyers = fyersModel.FyersModel(
    client_id=client_id, is_async=False, token=access_token, log_path=None
)
fyers_asysc = fyersModel.FyersModel(
    client_id=client_id, is_async=True, token=access_token, log_path=None
)

# ===== Option chain =====
data = {"symbol": ticker, "strikecount": strike_count, "timestamp": ""}
response = fyers.optionchain(data=data)["data"]
expiry_e = response["expiryData"][0]["expiry"]

data = {"symbol": ticker, "strikecount": strike_count, "timestamp": expiry_e}
response = fyers.optionchain(data=data)["data"]
option_chain = pd.DataFrame(response["optionsChain"])
symbols = option_chain["symbol"].to_list()

# ===== Spot price =====
spot_price = response.get("underlyingValue")
if spot_price is None:
    try:
        quote = fyers.quotes(data={"symbols": ticker})
        spot_price = quote["d"][0]["v"]["lp"]
    except Exception as e:
        logging.warning(f"Unable to fetch underlying spot via quotes: {e}")
        spot_price = option_chain["ltp"].iloc[0] if "ltp" in option_chain.columns else None
logging.info(f"Current spot price: {spot_price}")

# ===== df init (indexed by symbol) =====
df = pd.DataFrame(columns=[
    "symbol","ltp","ch","chp","avg_trade_price","open_price","high_price","low_price",
    "prev_close_price","vol_traded_today","oi","pdoi","oipercent","bid_price","ask_price",
    "last_traded_time","exch_feed_time","bid_size","ask_size","last_traded_qty",
    "tot_buy_qty","tot_sell_qty","lower_ckt","upper_ckt","type","expiry"
])
df["symbol"] = symbols
df.set_index("symbol", inplace=True)

# ===== Historical Daily data =====
f = dt.now(time_zone).date() - dt.duration(days=5)
p = dt.now(time_zone).date()
hist_req = {
    "symbol": ticker,
    "resolution": "D",
    "date_format": "1",
    "range_from": f.strftime("%Y-%m-%d"),
    "range_to": p.strftime("%Y-%m-%d"),
    "cont_flag": "1"
}
response2 = fyers.history(data=hist_req)
hist_data = pd.DataFrame(response2["candles"])
hist_data.columns = ["date","open","high","low","close","volume"]

ist = pytz.timezone("Asia/Kolkata")
hist_data["date"] = pd.to_datetime(hist_data["date"], unit="s").dt.tz_localize("UTC").dt.tz_convert(ist)
hist_data = hist_data[hist_data["date"].dt.date < dt.now(time_zone).date()]