"""一次性维护脚本：清理向导状态 (让用户获得真实的首次进入自动推荐体验)
注意：运行后用户需要重新走一遍开播向导，仅在明确需要时使用！
"""
import sqlite3

conn = sqlite3.connect("data/live_agent.db")
cur = conn.execute(
    "DELETE FROM app_settings WHERE key IN ('live_mode', 'wizard_completed', 'selected_anchor_id')"
)
conn.commit()
print("cleared wizard rows:", cur.rowcount)
conn.close()


