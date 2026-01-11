import logging
from setup import fyers_asysc   # async client instance
from execution import RESET, GREEN, RED, YELLOW 

def monitor_positions():
    try:
        response = fyers_asysc.positions()
        if response.get("s") == "ok":
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
                f"[PORTFOLIO] Total={overall.get('count_total',0)} "
                f"Open={overall.get('count_open',0)} "
                f"PnL={overall.get('pl_total',0)}"
            )
        else:
            logging.warning(f"[POSITION] Failed: {response}")
    except Exception as e:
        logging.error(f"[POSITION ERROR] {e}")