[Start: main.py]
       │
       ▼
┌───────────────────────────┐
│ setup.py                  │
│ - Load access token        │
│ - Init fyers clients       │
│ - Fetch option chain       │
│ - Init df & hist_data      │
└───────────────────────────┘
       │
       ▼
┌───────────────────────────┐
│ data_feed.py              │
│ - Connect WebSocket        │
│ - onmessage updates df     │
│ - Build 3m candles         │
│ - Maintain spot_price      │
└───────────────────────────┘
       │
       ▼
┌───────────────────────────┐
│ indicators.py             │
│ - Detect signals (CALL/PUT)│
│ - ATR & pivots             │
│ - Trailing stop updates    │
└───────────────────────────┘
       │
       ▼
┌───────────────────────────┐
│ execution.py              │
│ - paper_order() or        │
│   real_order()            │
│ - Place/modify orders      │
│ - Track PnL, exits         │
│ - Persist trades to CSV    │
└───────────────────────────┘
       │
       ▼
┌───────────────────────────┐
│ main.py                   │
│ - Async loop               │
│ - Every 5s: orderbook, PnL │
│ - Call paper/real_order    │
│ - End after trading hours  │
└───────────────────────────┘