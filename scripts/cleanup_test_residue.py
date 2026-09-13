"""一次性维护脚本：清理早期测试隔离机制上线前，测试残留在生产库的自定义角色"""
import sqlite3

conn = sqlite3.connect("data/live_agent.db")
cur = conn.execute("DELETE FROM anchor_roles WHERE id = 'role_custom_finance_01'")
conn.commit()
print("removed test residue roles:", cur.rowcount)
for row in conn.execute("SELECT role_type, role_name FROM anchor_roles").fetchall():
    print("remaining role:", row)
conn.close()
