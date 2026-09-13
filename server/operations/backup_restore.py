"""停服状态下创建和恢复包含 SQLite、主密钥及持久资产的一致数据快照。"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import uuid
from pathlib import Path

EXCLUDED_TOP_LEVEL = {"logs", "tmp", "backups"}
LOCK_NAME = ".service.lock"
DB_NAME = "live_agent.db"
KEY_NAME = ".master.key"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as input_file:
        for chunk in iter(lambda: input_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            import ctypes
            process_query_limited_information = 0x1000
            still_active = 259
            handle = ctypes.windll.kernel32.OpenProcess(process_query_limited_information, False, pid)
            if not handle:
                return False
            try:
                exit_code = ctypes.c_ulong()
                return bool(ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))) and exit_code.value == still_active
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def assert_service_stopped(data_dir: Path) -> None:
    lock = data_dir / LOCK_NAME
    if not lock.exists():
        return
    try:
        pid = int(json.loads(lock.read_text(encoding="utf-8")).get("pid", 0))
    except (OSError, ValueError, json.JSONDecodeError):
        pid = 0
    if _pid_alive(pid):
        raise RuntimeError(f"服务仍在运行（PID: {pid}），必须停服后操作完整数据快照")
    lock.unlink(missing_ok=True)


def _check_database(path: Path) -> None:
    connection = sqlite3.connect(str(path))
    try:
        result = connection.execute("PRAGMA integrity_check").fetchone()
        if not result or result[0] != "ok":
            raise RuntimeError(f"SQLite 完整性检查失败: {result}")
    finally:
        connection.close()


def _copy_database(source: Path, target: Path) -> None:
    source_connection = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    target_connection = sqlite3.connect(str(target))
    try:
        source_connection.backup(target_connection)
    finally:
        target_connection.close()
        source_connection.close()
    _check_database(target)


def create_backup(data_dir: Path, destination: Path, app_version: str = "unknown") -> dict:
    data_dir = data_dir.resolve()
    destination = destination.resolve()
    assert_service_stopped(data_dir)
    source_db = data_dir / DB_NAME
    source_key = data_dir / KEY_NAME
    if not source_db.is_file() or not source_key.is_file():
        raise RuntimeError("数据目录必须同时包含 live_agent.db 与 .master.key")
    if destination.exists():
        raise FileExistsError(f"备份目标已存在: {destination}")

    staging = destination.with_name(f".{destination.name}.tmp-{uuid.uuid4().hex}")
    files: list[dict] = []
    try:
        staging.mkdir(parents=True)
        _copy_database(source_db, staging / DB_NAME)
        for source in data_dir.rglob("*"):
            if not source.is_file():
                continue
            relative = source.relative_to(data_dir)
            if relative.parts[0] in EXCLUDED_TOP_LEVEL:
                continue
            if source.name in {LOCK_NAME, DB_NAME, f"{DB_NAME}-wal", f"{DB_NAME}-shm"}:
                continue
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)

        for file_path in sorted(path for path in staging.rglob("*") if path.is_file()):
            relative = file_path.relative_to(staging).as_posix()
            files.append({"path": relative, "size": file_path.stat().st_size, "sha256": _sha256(file_path)})
        key_bytes = (staging / KEY_NAME).read_bytes()
        manifest = {
            "format_version": 1,
            "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "app_version": app_version,
            "database": DB_NAME,
            "key_protection": "windows_dpapi" if key_bytes.startswith(b"DPAPI1:") else "raw_local",
            "restore_scope": "same_machine_same_user" if key_bytes.startswith(b"DPAPI1:") else "local_environment",
            "files": files,
        }
        (staging / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(staging, destination)
        return manifest
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def validate_backup(backup_dir: Path) -> dict:
    backup_dir = backup_dir.resolve()
    manifest_path = backup_dir / "manifest.json"
    if not manifest_path.is_file():
        raise RuntimeError("备份缺少 manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("format_version") != 1:
        raise RuntimeError("不支持的备份格式版本")
    listed = manifest.get("files")
    if not isinstance(listed, list):
        raise RuntimeError("备份文件清单无效")
    for item in listed:
        relative = Path(item.get("path", ""))
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise RuntimeError("备份文件路径无效")
        file_path = backup_dir / relative
        if not file_path.is_file():
            raise RuntimeError(f"备份缺少文件: {relative.as_posix()}")
        if file_path.stat().st_size != item.get("size") or _sha256(file_path) != item.get("sha256"):
            raise RuntimeError(f"备份校验失败: {relative.as_posix()}")
    if not (backup_dir / KEY_NAME).is_file():
        raise RuntimeError("备份缺少主密钥")
    _check_database(backup_dir / manifest.get("database", DB_NAME))
    return manifest


def restore_backup(backup_dir: Path, data_dir: Path) -> Path | None:
    backup_dir = backup_dir.resolve()
    data_dir = data_dir.resolve()
    assert_service_stopped(data_dir)
    manifest = validate_backup(backup_dir)
    staging = data_dir.with_name(f".{data_dir.name}.restore-{uuid.uuid4().hex}")
    rollback = data_dir.with_name(f"{data_dir.name}.rollback-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}")
    moved_old = False
    try:
        staging.mkdir(parents=True)
        for item in manifest["files"]:
            relative = Path(item["path"])
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup_dir / relative, target)
        _check_database(staging / DB_NAME)
        if data_dir.exists():
            os.replace(data_dir, rollback)
            moved_old = True
        os.replace(staging, data_dir)
        return rollback if moved_old else None
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        if moved_old and rollback.exists() and not data_dir.exists():
            os.replace(rollback, data_dir)
        raise
