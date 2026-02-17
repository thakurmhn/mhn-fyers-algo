import sqlite3
import os

db_file = r"C:\SQLite\ticks\ticks_2026-02-08.db"

if os.path.exists(db_file):
    conn = sqlite3.connect(db_file)
    cursor = conn.cursor()
    for table in ["ticks", "candles_3m_ist", "candles_15m_ist"]:
        try:
            cursor.execute(f"DROP TABLE IF EXISTS {table}")
            print(f"[CLEANUP] Dropped {table} from {db_file}")
        except Exception as e:
            print(f"[ERROR] Could not drop {table}: {e}")
    conn.commit()
    conn.close()
else:
    print("[SKIP] No DB file found for Feb 8th")