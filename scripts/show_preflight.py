"""临时验证脚本：打印 preflight 检查报告 (UTF-8 输出)"""
import json
import urllib.request

with urllib.request.urlopen("http://127.0.0.1:18080/api/v1/live/preflight", timeout=30) as resp:
    data = json.loads(resp.read().decode("utf-8"))["data"]

print("ready:", data["ready"])
print("summary:", data["summary"])
for c in data["checks"]:
    print(f"  [{c['status']:4}] {c['key']:10} {c['title']}: {c['message']}")
    if c["fix_hint"]:
        print(f"           fix: {c['fix_hint']} (tab={c['action_tab']})")
