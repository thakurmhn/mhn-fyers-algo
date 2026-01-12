
from config import account_type
from setup import fyers_asysc
import logging

# ANSI COLORS
RESET   = "\033[0m"
YELLOW  = "\033[93m"
GRAY    = "\033[90m"

async def monitor_positions():
    """
    Monitor broker positions every 5 seconds.
    - Skips broker call in PAPER mode.
    - Awaits async Fyers API in LIVE mode.
    """
    if account_type == "PAPER":
        logging.info(f"{GRAY}[POSITION] Skipped broker positions (Paper mode){RESET}")
        return

    try:
        response = await fyers_asysc.positions()   # ✅ await async call
        if response and response.get("s") == "ok":
            net_positions = response.get("netPositions", [])
            overall = response.get("overall", {})

            for pos in net_positions:
                symbol = pos.get("symbol")
                qty = pos.get("netQty", 0)
                avg_price = pos.get("avgPrice", 0)
                ltp = pos.get("ltp", 0)
                pnl = pos.get("pl", 0)
                realized = pos.get("realized_profit", 0)
                unrealized = pos.get("unrealized_profit", 0)

                logging.info(
                    f"{YELLOW}[POSITION] {symbol} Qty={qty} Avg={avg_price:.2f} LTP={ltp:.2f} "
                    f"PnL={pnl:.2f} (Realized={realized:.2f}, Unrealized={unrealized:.2f}){RESET}"
                )

            logging.info(
                f"{GRAY}[PORTFOLIO] Total={overall.get('count_total',0)} "
                f"Open={overall.get('count_open',0)} "
                f"PnL={overall.get('pl_total',0)}{RESET}"
            )
        else:
            logging.warning(f"[POSITION] Failed: {response}")
    except Exception as e:
        logging.error(f"[POSITION ERROR] {e}")