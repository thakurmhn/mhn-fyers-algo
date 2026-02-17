import os
import sqlite3

def add_is_partial_column(db_path):
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Check schema for 3m candles
    cursor.execute("PRAGMA table_info(candles_3m_ist)")
    cols_3m = [row[1] for row in cursor.fetchall()]
    if "is_partial" not in cols_3m:
        print(f"[PATCH] Adding is_partial to candles_3m_ist in {db_path}")
        cursor.execute("ALTER TABLE candles_3m_ist ADD COLUMN is_partial INTEGER DEFAULT 0")

    # Check schema for 15m candles
    cursor.execute("PRAGMA table_info(candles_15m_ist)")
    cols_15m = [row[1] for row in cursor.fetchall()]
    if "is_partial" not in cols_15m:
        print(f"[PATCH] Adding is_partial to candles_15m_ist in {db_path}")
        cursor.execute("ALTER TABLE candles_15m_ist ADD COLUMN is_partial INTEGER DEFAULT 0")

    conn.commit()
    conn.close()

# Path to your ticks folder
ticks_dir = r"C:\SQLite\ticks"

for file in os.listdir(ticks_dir):
    if file.endswith(".db"):
        db_path = os.path.join(ticks_dir, file)
        try:
            add_is_partial_column(db_path)
        except Exception as e:
            print(f"[ERROR] Could not patch {db_path}: {e}")