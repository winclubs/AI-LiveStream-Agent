"""创建停服一致数据备份。"""
import argparse
import datetime as dt
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from server.operations.backup_restore import create_backup


def main() -> int:
    parser = argparse.ArgumentParser(description="备份 SQLite、主密钥与持久资产（要求服务已停止）")
    parser.add_argument("--data-dir", type=Path, default=Path(os.getenv("LIVE_AGENT_DATA_DIR") or ROOT / "data"))
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()
    destination = args.output or args.data_dir.resolve().parent / "backups" / f"backup-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}"
    version = "unknown"
    try:
        import json
        version = json.loads((ROOT / "version.json").read_text(encoding="utf-8")).get("version", "unknown")
    except Exception:
        pass
    manifest = create_backup(args.data_dir, destination, app_version=version)
    print(f"备份完成: {destination.resolve()} ({len(manifest['files'])} 个文件)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
