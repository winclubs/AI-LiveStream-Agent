"""校验并离线原子恢复完整数据备份。"""
import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from server.operations.backup_restore import restore_backup


def main() -> int:
    parser = argparse.ArgumentParser(description="恢复完整数据备份（要求服务已停止）")
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, default=Path(os.getenv("LIVE_AGENT_DATA_DIR") or ROOT / "data"))
    parser.add_argument("--confirm", action="store_true", help="确认执行数据目录原子替换")
    args = parser.parse_args()
    if not args.confirm:
        parser.error("恢复会替换当前数据目录；确认无误后必须传入 --confirm")
    rollback = restore_backup(args.backup, args.data_dir)
    print(f"恢复完成: {args.data_dir.resolve()}")
    if rollback:
        print(f"原数据保留在: {rollback}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
