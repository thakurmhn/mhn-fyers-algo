import logging, sys
import warnings
from config import strategy_name, time_zone
import pendulum as dt

warnings.simplefilter(action="ignore", category=FutureWarning)

log_file = f"{strategy_name}_{dt.now(time_zone).date()}.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(log_file, mode="a")
    ]
)

