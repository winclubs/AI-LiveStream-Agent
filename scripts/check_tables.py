import sqlite3

for db_path in ['server/data.db', 'data.db']:
    try:
        conn = sqlite3.connect(db_path)
        tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        print(f"Tables in {db_path}:", tables)
    except Exception as e:
        print(f"Error {db_path}:", e)
