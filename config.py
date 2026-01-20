import os
import logging
import pendulum as dt
import sys

from dotenv import load_dotenv, find_dotenv

#load_dotenv("C:\\Users\\mohan\\mhn-fyers-algo\\.env")

env_path = find_dotenv(r"C:\Users\mohan\mhn-fyers-algo\.env")

load_dotenv(dotenv_path=env_path)

client_id = os.getenv("FYERS_CLIENT_ID")
secret_key = os.getenv("FYERS_SECRET_KEY")
access_token = os.getenv("FYERS_ACCESS_TOKEN")
redirect_uri = os.getenv("FYERS_REDIRECT_URI")

# ===== Credentials =====
# client_id = ""
# secret_key = ""
# redirect_uri = ""

# ===== Strategy parameters =====
strategy_name = 'option_buying_pivot'
index_name = 'NIFTY50'
exchange = 'NSE'
ticker = f"{exchange}:{index_name}-INDEX"

strike_count = 10
strike_diff = 100
account_type = 'PAPER'   # 'PAPER' or 'LIVE'
quantity = 130
buffer = 5
profit_loss_point = 10
MAX_TRADES_PER_DAY = 20

# ========== Entry Params ==================

# config.py
ORDER_TYPE = "LIMIT"   # options: "LIMIT" or "MARKET"
ENTRY_OFFSET = 5       # only used if LIMIT, e.g. ltp - 5

time_zone = "Asia/Kolkata"
start_hour, start_min = 9, 30
end_hour, end_min = 15, 15

CANDLE_INTERVAL_MIN = 3
ATR_PERIOD = 14

CALL_MONEYNESS = 'ITM'
PUT_MONEYNESS  = 'ITM'

ATR_STOP_MULT  = 1.0
ATR_TGT_MULT   = 2.0
TRAIL_TRIGGER  = 1.0
TRAIL_STEP     = 0.5
ALLOW_SHORTS = False


# # ===== Logging =====
# log_file = f"{strategy_name}_{dt.now(time_zone).date()}.log"

# logging.basicConfig(
#     level=logging.INFO,
#     format="%(asctime)s - %(levelname)s - %(message)s",
#     handlers=[
#         logging.StreamHandler(),  # defaults to sys.stderr
#         logging.FileHandler(log_file, mode="a")
#     ]
# )
# ===== Logging =====
log_file = f"{strategy_name}_{dt.now(time_zone).date()}.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(stream=sys.stdout),  # force stdout
        logging.FileHandler(log_file, mode="a", encoding="utf-8")  # ensure UTF-8 for file
    ]
)