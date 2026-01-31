import pickle

def analyze_pickle_file(file_path):
    # Load pickle file
    with open(file_path, "rb") as f:
        data = pickle.load(f)

    print(f"\nLoaded pickle file: {file_path}")

    # Case 1: Legacy format (single dict snapshot)
    if isinstance(data, dict):
        print("\nLegacy format detected (single dict snapshot).")
        print("Keys available:", list(data.keys()))

        total_pnl = data.get("total_pnl", 0)
        trade_count = data.get("trade_count", 0)
        print(f"Total PnL: {total_pnl}")
        print(f"Trade Count: {trade_count}")

        for leg in ["call_buy", "put_buy"]:
            if leg in data:
                trade = data[leg]
                print(f"\n--- {leg.upper()} ---")
                print(f"Option: {trade.get('option_name')}")
                print(f"Entry Price: {trade.get('buy_price')}")
                print(f"Quantity: {trade.get('quantity')}")
                print(f"PnL: {trade.get('pnl')}")
                print(f"Stop Price: {trade.get('current_stop_price')}")
                print(f"Full Target: {trade.get('full_target_price')}")
                print(f"Partial Target: {trade.get('partial_target_price')}")
                print(f"Trade Flag: {trade.get('trade_flag')}")

    # Case 2: Ledger format (list of snapshots)
    elif isinstance(data, list):
        print(f"\nLedger format detected ({len(data)} snapshots).")

        equity_curve = []
        timestamps = []
        wins, losses = 0, 0
        pnl_list = []

        for i, snap in enumerate(data):
            # If snapshot is dict with timestamp/state
            if isinstance(snap, dict) and "timestamp" in snap and "state" in snap:
                ts = snap["timestamp"]
                state = snap["state"]
            else:
                # Fallback: snapshot is just a dict of state
                ts = f"Snapshot {i}"
                state = snap

            total_pnl = state.get("total_pnl", 0)
            trade_count = state.get("trade_count", 0)

            equity_curve.append(total_pnl)
            timestamps.append(ts)

            for leg in ["call_buy", "put_buy"]:
                if leg in state:
                    trade = state[leg]
                    pnl = trade.get("pnl", 0)
                    if pnl > 0:
                        wins += 1
                    elif pnl < 0:
                        losses += 1
                    pnl_list.append(pnl)

            print(f"{ts}: Total PnL={total_pnl}, Trades={trade_count}")

        # Compute metrics
        total_trades = wins + losses
        win_rate = (wins / total_trades * 100) if total_trades > 0 else 0
        avg_pnl = (sum(pnl_list) / total_trades) if total_trades > 0 else 0

        print("\n=== Performance Summary ===")
        print(f"Closed Trades: {total_trades}")
        print(f"Wins: {wins}, Losses: {losses}")
        print(f"Win Rate: {win_rate:.2f}%")
        print(f"Average PnL per trade: {avg_pnl:.2f}")
        print(f"Final Total PnL: {equity_curve[-1] if equity_curve else 0}")

        print("\n=== Equity Progression ===")
        for ts, pnl in zip(timestamps, equity_curve):
            print(f"{ts}: Total PnL={pnl}")

    else:
        print("\nUnrecognized pickle format:", type(data))


# Example usage
if __name__ == "__main__":
    analyze_pickle_file("data-2026-01-29-PAPER.pickle")