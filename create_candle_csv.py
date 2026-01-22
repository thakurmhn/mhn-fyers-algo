import re
import pandas as pd

log_file = "option_buying_pivot_2026-01-19.log"
csv_file = "candles-extracted.csv"

pattern = re.compile(
    r"\[3M CANDLE CLOSED\]\s+(\d{2}:\d{2}:\d{2})\s+\| O=(\d+\.\d+) H=(\d+\.\d+) L=(\d+\.\d+) C=(\d+\.\d+)\s+\|Spot=(\d+\.\d+)"
)

rows = []
with open(log_file, "r") as f:
    for line in f:
        match = pattern.search(line)
        if match:
            candle_time, o, h, l, c, spot = match.groups()
            rows.append([candle_time, float(o), float(h), float(l), float(c), float(spot)])

# Save to CSV
df = pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "spot"])
df.to_csv(csv_file, index=False)

print(f"Extracted {len(df)} candles to {csv_file}")
print(df.head())