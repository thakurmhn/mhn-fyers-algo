                ┌─────────────────────┐
                │      main.py        │
                │  - async loop       │
                │  - sockets, PnL     │
                │  - calls run_strategy() 
                └─────────┬───────────┘
                          │
                          ▼
                ┌─────────────────────┐
                │   execution.py      │
                │  run_strategy()     │
                │   → builds candles  │
                │   → routes to       │
                │      paper_order()  │
                │      live_order()   │
                │                     │
                │  process_order()    │
                │   → SL / TG / Trail │
                │   → updates PnL     │
                └─────────┬───────────┘
                          │
          ┌───────────────┼────────────────┐
          ▼                               ▼
 ┌─────────────────────┐         ┌─────────────────────┐
 │   candle_builder.py │         │    signals.py       │
 │  build_3min_candle()│         │  detect_signal()    │
 │  build_15m_candles()│         │  oscillator_exit()  │
 │  get_today_15m_candles()      │  bias validation    │
 └─────────┬───────────┘         └─────────┬───────────┘
           │                                │
           ▼                                ▼
 ┌─────────────────────┐         ┌─────────────────────┐
 │   indicators.py     │         │   risk mgmt helpers │
 │  calculate_cpr()    │         │  update_risk()      │
 │  calculate_trad()   │         │  cleanup_trade_exit()│
 │  calculate_cam()    │         │  force_close_old_trades()│
 │  resolve_atr()      │         └─────────────────────┘
 └─────────────────────┘