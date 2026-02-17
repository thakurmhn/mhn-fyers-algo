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
symbols = ["NSE:NIFTY50-INDEX"]
# # ["NSE:NIFTY50-INDEX", "NSE:BANKNIFTY-INDEX", "NSE:FINNIFTY-INDEX"]
exchange = 'NSE'
ticker = f"{exchange}:{index_name}-INDEX"

strike_count = 10               # Options strikes from Option chain
strike_diff = 100               # Difference from ATM Option
account_type = 'PAPER'          # 'PAPER' or 'LIVE'
quantity = 130                  # lot size - Nifty - 65
buffer = 5
profit_loss_point = 25          # Used for target profit/stoploss 
MAX_TRADES_PER_DAY = 20         # Maximum trades per day

CALL_MONEYNESS = 'ITM'          # strike/contract type - ITM/OTM  
PUT_MONEYNESS  = 'ITM'

# =======================================================================

# ================ Time Params ==========================================

time_zone = "Asia/Kolkata"
start_hour, start_min = 9, 30
end_hour, end_min = 15, 15

# Proper timezone-aware datetime objects
today = dt.today(time_zone)
start_time = today.replace(hour=start_hour, minute=start_min, second=0, microsecond=0)
end_time   = today.replace(hour=end_hour, minute=end_min, second=0, microsecond=0)

# =======================================================================

# ========== Live Entry Params ==================

ORDER_TYPE = "LIMIT"   # options: "LIMIT" or "MARKET"
ENTRY_OFFSET = 5       # only used if LIMIT, e.g. ltp - 5

# ========= Indicator Params =====================

CANDLE_INTERVAL_MIN = 3
ATR_PERIOD = 14

# ===============================================

# ============ Signal Parameters =============================

CANDLE_BODY_RANGE = 0.54            # Default is 0.6
ATR_VALUE = 15                      # Default is 20

# ============================================================

# ============ Risk Management ===========================

MAX_DAILY_LOSS = -5000      # stop trading if net PnL < -5000
MAX_DRAWDOWN   = -3000      # stop trading if drawdown exceeds 3000

# =============================================================

# ===================== Oscilater Exit condition ====================
# 	HARD → when oscillator exit triggers, you close the entire position immediately.
# 	TRAIL → instead of closing, you tighten stop-loss to entry and let trailing logic handle the exit.

OSCILLATOR_EXIT_MODE = "HARD"   # or "TRAIL"

# ===================================================================

# ========================= MODE ====================

MODE =   "STRATEGY"                         # "COLLECT" or "STRATEGY"      # Collet = and build db with tick data #Strategy = run the whole bot strategy 




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