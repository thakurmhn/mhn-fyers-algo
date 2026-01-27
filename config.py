import os
import logging
import pendulum as dt
import sys

from dotenv import load_dotenv, find_dotenv

# ===== Credentials ================================================

env_path = find_dotenv(r"C:\Users\mohan\mhn-fyers-algo\.env")

load_dotenv(dotenv_path=env_path)

client_id = os.getenv("FYERS_CLIENT_ID")
secret_key = os.getenv("FYERS_SECRET_KEY")
access_token = os.getenv("FYERS_ACCESS_TOKEN")
redirect_uri = os.getenv("FYERS_REDIRECT_URI")

# ===================================================================


# ===== Strategy parameters ===========================================

strategy_name = 'option_buying_pivot'
index_name = 'NIFTY50'
exchange = 'NSE'
ticker = f"{exchange}:{index_name}-INDEX"

strike_count = 10               # Options Stikes from Option chain
strike_diff = 100               # Differrence from ATM Option
account_type = 'PAPER'          # 'PAPER' or 'LIVE'
quantity = 130                  # lot size - Nifty - 65
buffer = 5
profit_loss_point = 10          # Used for target profit/stoploss 
MAX_TRADES_PER_DAY = 30         # Maximum trades per day

CALL_MONEYNESS = 'ITM'          # strike/contract type - ITM/OTM  
PUT_MONEYNESS  = 'ITM'

# =======================================================================

# ================ Time Params ==========================================

time_zone = "Asia/Kolkata"
start_hour, start_min = 9, 30
end_hour, end_min = 15, 15

# =======================================================================

# ========== Live Entry Params ==================

ORDER_TYPE = "LIMIT"   # options: "LIMIT" or "MARKET"
ENTRY_OFFSET = 5       # only used if LIMIT, e.g. ltp - 5

# ========= Indicator Parms =====================

CANDLE_INTERVAL_MIN = 3
ATR_PERIOD = 14

# ===============================================

# ============ Signal Parameters =============================

CANDLE_BODY_RANGE = 0.54            # Default is 0.6
ATR_VALUE = 15                      # Default is 20

# ============================================================


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