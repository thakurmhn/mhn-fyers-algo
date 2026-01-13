import pandas as pd
from collections import defaultdict, deque

def generate_eod_report_csv(input_csv: str, output_csv: str):
    """
    Parse intraday trade CSV (paper mode), match BUY/SELL legs per ticker (FIFO),
    handle split exits, compute precise PnL, and export a structured EOD report.
    """

    df = pd.read_csv(input_csv)
    df['time'] = pd.to_datetime(df['time'])
    df = df.sort_values('time')

    # Open positions per ticker (FIFO queue of entries)
    open_positions = defaultdict(deque)
    closed_trades = []
    trade_counter = 1

    for _, row in df.iterrows():
        ticker = row['ticker']
        action = row['action']
        price = float(row['price'])
        qty = int(row['quantity'])
        sl = float(row['stop_price']) if not pd.isna(row['stop_price']) else None
        tg1 = float(row['take_profit']) if not pd.isna(row['take_profit']) else None

        if action == 'BUY':
            # Start a new position (tracked independently even if same ticker)
            open_positions[ticker].append({
                'Date': row['time'].date(),
                'Trade-NO': None,  # assigned when closed
                'Ticker': ticker,
                'Entry': price,
                'SL': sl,
                'TG-1': tg1,
                'TG-2': None,  # not stored in CSV; keep None or compute if needed
                'EntryQty': qty,
                'RemainingQty': qty,
                'ExitPrices': [],
                'ExitQty': [],
                'PnL': 0.0
            })

        elif action == 'SELL':
            # Consume SELL against earliest open BUY (FIFO)
            remaining_to_sell = qty
            while remaining_to_sell > 0 and open_positions[ticker]:
                pos = open_positions[ticker][0]
                take_qty = min(remaining_to_sell, pos['RemainingQty'])

                # PnL leg
                pnl_leg = (price - pos['Entry']) * take_qty
                pos['PnL'] += pnl_leg
                pos['ExitPrices'].append(price)
                pos['ExitQty'].append(take_qty)
                pos['RemainingQty'] -= take_qty
                remaining_to_sell -= take_qty

                # If position fully closed, finalize trade
                if pos['RemainingQty'] == 0:
                    pos['Trade-NO'] = trade_counter
                    closed_trades.append(pos)
                    trade_counter += 1
                    open_positions[ticker].popleft()

            # If SELL arrives without open BUY (shouldn’t happen), ignore or log

    # Build EOD report rows
    rows = []
    for t in closed_trades:
        profit = t['PnL'] if t['PnL'] > 0 else ''
        loss = abs(t['PnL']) if t['PnL'] < 0 else ''
        rows.append({
            'Date': t['Date'],
            'Trade-NO': t['Trade-NO'],
            'Ticker': t['Ticker'],
            'Entry': round(t['Entry'], 2),
            'SL': round(t['SL'], 4) if t['SL'] is not None else '',
            'TG-1': round(t['TG-1'], 3) if t['TG-1'] is not None else '',
            'TG-2': t['TG-2'] if t['TG-2'] is not None else '',
            'ExitPrices': ';'.join(f"{p:.2f}" for p in t['ExitPrices']),
            'ExitQty': ';'.join(str(q) for q in t['ExitQty']),
            'Profit': round(profit, 2) if profit != '' else '',
            'Loss': round(loss, 2) if loss != '' else ''
        })

    report_df = pd.DataFrame(rows)
    report_df.to_csv(output_csv, index=False)
    return report_df

# Usage:
report = generate_eod_report_csv(
    input_csv="trades_option_buying_pivot_2026-01-12.csv",
    output_csv="eod_report_2026-01-12.csv"
)

print(report.tail())

