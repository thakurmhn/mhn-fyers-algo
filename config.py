import os, certifi
import pendulum as dt
from dotenv import load_dotenv


# Load environment variables
load_dotenv()


# Set follwing Env

# FYERS_CLIENT_ID=your_client_id_here
# FYERS_SECRET_KEY=your_secret_here
# FYERS_ACCESS_TOKEN=your_access_token_here
# FYERS_REDIRECT_URI=https://your.redirect.uri

# Credentials
client_id = os.getenv("FYERS_CLIENT_ID")
secret_key = os.getenv("FYERS_SECRET_KEY")
access_token = os.getenv("FYERS_ACCESS_TOKEN")
redirect_uri = os.getenv("FYERS_REDIRECT_URI")

# Trading parameters (safe to keep here)
ticker = "NSE:NIFTY50-INDEX"
strike_diff = 50
quantity = 15
account_type = "PAPER"
strategy_name = "CPR_Camarilla_Breakout"
time_zone = "Asia/Kolkata"
start_time = dt.datetime(2026, 1, 5, 9, 15, tz=time_zone)
end_time   = dt.datetime(2026, 1, 5, 15, 30, tz=time_zone)
profit_loss_point = 20

# # ===== Credentials =====
# client_id = ""
# secret_key = ""
# redirect_uri = ""

# # ===== Strategy parameters =====
# strategy_name = "option_buying_pivot"
# index_name = "NIFTY50"
# exchange = "NSE"
# ticker = f"{exchange}:{index_name}-INDEX"

strike_count = 10
strike_diff = 100
account_type = "PAPER"   # or "LIVE"

time_zone = "Asia/Kolkata"
start_hour, start_min = 9, 30
end_hour, end_min = 15, 15
quantity = 75
buffer = 5
profit_loss_point = 30
MAX_TRADES_PER_DAY = 5

# ===== Candle/indicator runtime constants =====
CANDLE_INTERVAL_MIN = 3
ATR_PERIOD = 14

# === Monyness Options ===
CALL_MONEYNESS = "ITM"   # or "OTM"
PUT_MONEYNESS  = "ITM"

# ===== SSL fix =====
os.environ["SSL_CERT_FILE"] = certifi.where()

# ===== Trading clock =====
start_time = dt.now(time_zone).replace(hour=start_hour, minute=start_min, second=0, microsecond=0)
end_time   = dt.now(time_zone).replace(hour=end_hour, minute=end_min, second=0, microsecond=0)