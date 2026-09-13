import json
import logging
import os
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server.app import app
from server.config import DATA_DIR
from server.observability.logging_config import flush_logging
from server.operations.backup_restore import create_backup, restore_backup, validate_backup


def test_health_probes_and_persistent_json_log():
    with TestClient(app) as client:
        assert client.get("/livez").status_code == 200
        startup = client.get("/startupz")
        assert startup.status_code == 200
        assert startup.json()["phase"] == "ready"
        ready = client.get("/readyz")
        assert ready.status_code == 200
        assert ready.json()["details"]["database"] == "ready"
        logging.getLogger("LiveAgent.Test").info("operation-log-probe")
        flush_logging()

    log_path = DATA_DIR / "logs" / "server.log"
    assert log_path.is_file()
    records = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert any(record["message"] == "operation-log-probe" for record in records)
    assert all({"timestamp", "level", "logger", "message", "pid", "version"} <= record.keys() for record in records)


def _make_wal_data_dir(path: Path):
    path.mkdir()
    (path / ".master.key").write_bytes(b"k" * 32)
    (path / "voices").mkdir()
    (path / "voices" / "sample.wav").write_bytes(b"sample")
    connection = sqlite3.connect(path / "live_agent.db")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA wal_autocheckpoint=0")
    connection.execute("CREATE TABLE proof (value TEXT NOT NULL)")
    connection.execute("INSERT INTO proof VALUES ('committed-in-wal')")
    connection.commit()
    return connection


def test_backup_api_captures_wal_and_restore_is_atomic(tmp_path):
    data_dir = tmp_path / "data"
    connection = _make_wal_data_dir(data_dir)
    backup_dir = tmp_path / "backup-one"
    try:
        manifest = create_backup(data_dir, backup_dir, app_version="test")
    finally:
        connection.close()

    assert manifest["format_version"] == 1
    assert not (backup_dir / "live_agent.db-wal").exists()
    with closing(sqlite3.connect(backup_dir / "live_agent.db")) as snapshot:
        assert snapshot.execute("SELECT value FROM proof").fetchone()[0] == "committed-in-wal"
    assert (backup_dir / "voices" / "sample.wav").read_bytes() == b"sample"
    validate_backup(backup_dir)

    with closing(sqlite3.connect(data_dir / "live_agent.db")) as active:
        active.execute("UPDATE proof SET value='changed'")
        active.commit()
    rollback = restore_backup(backup_dir, data_dir)
    assert rollback and rollback.is_dir()
    with closing(sqlite3.connect(data_dir / "live_agent.db")) as restored:
        assert restored.execute("SELECT value FROM proof").fetchone()[0] == "committed-in-wal"


def test_backup_rejects_running_service_and_tampering(tmp_path):
    data_dir = tmp_path / "data"
    connection = _make_wal_data_dir(data_dir)
    connection.close()
    (data_dir / ".service.lock").write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
    with pytest.raises(RuntimeError, match="仍在运行"):
        create_backup(data_dir, tmp_path / "blocked")

    (data_dir / ".service.lock").unlink()
    backup_dir = tmp_path / "backup"
    create_backup(data_dir, backup_dir)
    (backup_dir / "voices" / "sample.wav").write_bytes(b"tampered")
    with pytest.raises(RuntimeError, match="校验失败"):
        restore_backup(backup_dir, data_dir)


def test_repeated_local_source_start_stop_cycles():
    with TestClient(app) as client:
        for index in range(5):
            started = client.post("/api/v1/live/start", json={"room_id": f"smoke-cycle-{index}"})
            assert started.status_code == 200
            assert started.json()["is_live"] is True
            status = client.get("/api/v1/live/status")
            assert status.status_code == 200
            assert status.json()["local_source"]["status"] == "running"
            stopped = client.post("/api/v1/live/stop")
            assert stopped.status_code == 200
            assert stopped.json()["is_live"] is False
        assert client.get("/readyz").status_code == 200


def test_lifespan_initialization_failure_cleans_owned_resources_independently(tmp_path, monkeypatch):
    import server.app as app_module
    from server.health import health_state
    from server.routes.live import global_live_controller
    from server.core.media.virtual_audio import global_virtual_audio

    calls = []

    async def fail_init():
        raise RuntimeError("init failed")

    async def fail_stop():
        calls.append("stop")
        raise RuntimeError("stop failed")

    def fail_audio_shutdown():
        calls.append("audio")
        raise RuntimeError("audio failed")

    class FailingEngine:
        async def dispose(self):
            calls.append("dispose")
            raise RuntimeError("dispose failed")

    monkeypatch.setattr(app_module, "DATA_DIR", tmp_path)
    monkeypatch.setattr(app_module, "init_db", fail_init)
    monkeypatch.setattr(app_module, "engine", FailingEngine())
    monkeypatch.setattr(app_module, "flush_logging", lambda: calls.append("flush"))
    monkeypatch.setattr(global_live_controller, "stop", fail_stop)
    monkeypatch.setattr(global_virtual_audio, "shutdown", fail_audio_shutdown)

    async def enter_lifespan():
        async with app_module.lifespan(app_module.app):
            pytest.fail("initialization failure must not yield")

    with pytest.raises(RuntimeError, match="init failed"):
        import asyncio
        asyncio.run(enter_lifespan())

    assert health_state.phase == "failed"
    assert "RuntimeError: init failed" in health_state.last_error
    assert not (tmp_path / ".service.lock").exists()
    assert calls == ["stop", "audio", "dispose", "flush"]


def test_lifespan_service_lock_write_failure_marks_health_failed(tmp_path, monkeypatch):
    import asyncio
    import server.app as app_module
    from server.health import health_state
    from server.routes.live import global_live_controller
    from server.core.media.virtual_audio import global_virtual_audio

    calls = []
    original_write_text = Path.write_text

    def fail_service_lock_write(path, *args, **kwargs):
        if path.name == ".service.lock":
            raise OSError("lock write failed")
        return original_write_text(path, *args, **kwargs)

    class Engine:
        async def dispose(self):
            calls.append("dispose")

    async def stop():
        calls.append("stop")

    monkeypatch.setattr(app_module, "DATA_DIR", tmp_path)
    monkeypatch.setattr(app_module, "engine", Engine())
    monkeypatch.setattr(app_module, "flush_logging", lambda: calls.append("flush"))
    monkeypatch.setattr(Path, "write_text", fail_service_lock_write)
    monkeypatch.setattr(global_live_controller, "stop", stop)
    monkeypatch.setattr(global_virtual_audio, "shutdown", lambda: calls.append("audio"))

    async def enter_lifespan():
        async with app_module.lifespan(app_module.app):
            pytest.fail("service lock failure must not yield")

    with pytest.raises(OSError, match="lock write failed"):
        asyncio.run(enter_lifespan())

    assert health_state.phase == "failed"
    assert "OSError: lock write failed" in health_state.last_error
    assert calls == ["stop", "audio", "dispose", "flush"]
