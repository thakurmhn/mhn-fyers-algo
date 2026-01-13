import re
import pandas as pd

# Path to your log file
log_file = "option_buying_pivot_2026-01-13.log"   # replace with your actual log filename
output_csv = "candles_3m.csv"

rows = []
pattern = re.compile(
    r"\[3M CANDLE CLOSED\]\s+(\d{2}:\d{2}:\d{2})\s+\| O=(\d+\.\d+)\s+H=(\d+\.\d+)\s+L=(\d+\.\d+)\s+C=(\d+\.\d+)"
)

with open(log_file, "r") as f:
    for line in f:
        m = pattern.search(line)
        if m:
            ts, o, h, l, c = m.groups()
            rows.append({
                "timestamp": ts,
                "open": float(o),
                "high": float(h),
                "low": float(l),
                "close": float(c)
            })

# Convert to DataFrame
df = pd.DataFrame(rows)

# Save to CSV
df.to_csv(output_csv, index=False)
print(f"Extracted {len(df)} candles to {output_csv}")