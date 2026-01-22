"""
Standalone module for multi-strike option monitoring and spread strategies.
Does not interfere with existing trading bot setup.

                ┌───────────────────────────┐
                │   Data Feed (Fyers API)   │
                │ - Option Chain (CE/PE)    │
                │ - Spot Price              │
                │ - Historical Candles      │
                └─────────────┬─────────────┘
                              │
                              ▼
                ┌───────────────────────────┐
                │   Data Preparation         │
                │ - Load option_chain DF     │
                │ - Select multiple strikes  │
                │   (ATM, ITM ±100, OTM)     │
                │ - Refresh LTPs             │
                └─────────────┬─────────────┘
                              │
                              ▼
                ┌───────────────────────────┐
                │   Price Monitoring         │
                │ - Track LTP changes        │
                │ - Compute spreads:         │
                │   CE(ATM) vs CE(ITM)       │
                │   PE(ATM) vs PE(ITM)       │
                │ - Detect volatility skews  │
                └─────────────┬─────────────┘
                              │
                              ▼
                ┌───────────────────────────┐
                │   Signal Generation        │
                │ - Threshold triggers:      │
                │   • Spread widening        │
                │   • IV skew                │
                │   • Breakout levels        │
                │ - Strategy decision:       │
                │   • Vertical spread        │
                │   • Straddle/Strangle      │
                │   • Arbitrage              │
                └─────────────┬─────────────┘
                              │
                              ▼
                ┌───────────────────────────┐
                │   Execution Engine         │
                │ - Place multi-leg orders   │
                │ - Paper vs Live mode       │
                │ - Update ledger (PnL, qty) │
                └─────────────┬─────────────┘
                              │
                              ▼
                ┌───────────────────────────┐
                │   Audit & Persistence      │
                │ - Log trades (CSV, pickle) │
                │ - Track MTM & PnL          │
                │ - Save option chain states │
                └───────────────────────────┘

"""

import pandas as pd
import logging
from fyers_apiv3 import fyersModel
from config import client_id, secret_key, redirect_uri, ticker, strike_count, strike_diff

# ===== Setup logging =====
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# ===== Fyers client (reuse token logic if needed) =====
# For simplicity, assume access_token already available
fyers = fyersModel.FyersModel(client_id=client_id, is_async=False, token="YOUR_ACCESS_TOKEN")

# ===== Workflow Steps =====

def fetch_option_chain():
    """Fetch option chain from Fyers API."""
    data = {"symbol": ticker, "strikecount": strike_count, "timestamp": ""}
    response = fyers.optionchain(data=data)['data']
    expiry_e = response['expiryData'][0]['expiry']
    data = {"symbol": ticker, "strikecount": strike_count, "timestamp": expiry_e}
    response = fyers.optionchain(data=data)['data']
    option_chain = pd.DataFrame(response['optionsChain'])
    return option_chain, response.get('underlyingValue')

def prepare_data(option_chain, spot_price):
    """Select ATM, ITM ±100, OTM strikes."""
    atm_strike = round(spot_price / strike_diff) * strike_diff
    strikes_to_watch = [atm_strike, atm_strike - strike_diff, atm_strike + strike_diff]
    subset = option_chain[option_chain['strike_price'].isin(strikes_to_watch)]
    return subset

def monitor_spreads(subset):
    """Compute spreads between strikes."""
    ce = subset[subset['option_type'] == 'CE'].set_index('strike_price')['ltp']
    pe = subset[subset['option_type'] == 'PE'].set_index('strike_price')['ltp']

    spreads = {}
    if len(ce) >= 2:
        spreads['CE_spread'] = ce.max() - ce.min()
    if len(pe) >= 2:
        spreads['PE_spread'] = pe.max() - pe.min()

    logging.info(f"Spread snapshot: {spreads}")
    return spreads

def generate_signals(spreads, threshold=5):
    """Generate signals based on spread widening or skew."""
    signals = []
    if 'CE_spread' in spreads and spreads['CE_spread'] > threshold:
        signals.append(("VERTICAL_CALL", spreads['CE_spread']))
    if 'PE_spread' in spreads and spreads['PE_spread'] > threshold:
        signals.append(("VERTICAL_PUT", spreads['PE_spread']))
    return signals

def execute_strategy(signals):
    """Placeholder for execution engine (multi-leg orders)."""
    for side, value in signals:
        logging.info(f"[SIGNAL] {side} spread={value} → Execute strategy here")

def audit_trades(trades):
    """Save trades to CSV for audit."""
    df = pd.DataFrame(trades, columns=["time", "strategy", "details"])
    df.to_csv("spread_trades.csv", index=False)
    logging.info("Trades saved to spread_trades.csv")

# ===== Main loop (example) =====
if __name__ == "__main__":
    option_chain, spot_price = fetch_option_chain()
    subset = prepare_data(option_chain, spot_price)
    spreads = monitor_spreads(subset)
    signals = generate_signals(spreads)
    execute_strategy(signals)
    # audit_trades([...])  # Example audit call