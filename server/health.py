"""应用启动与就绪状态。"""
from __future__ import annotations

import datetime as dt
import shutil
from dataclasses import dataclass, field

from sqlalchemy import text

from server.config import DATA_DIR
from server.database.db import engine


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


@dataclass
class HealthState:
    phase: str = "starting"
    started_at: str = field(default_factory=_now)
    ready_at: str | None = None
    last_error: str = ""

    def starting(self) -> None:
        self.phase = "starting"
        self.started_at = _now()
        self.ready_at = None
        self.last_error = ""

    def ready(self) -> None:
        self.phase = "ready"
        self.ready_at = _now()
        self.last_error = ""

    def failed(self, exc: BaseException) -> None:
        self.phase = "failed"
        self.last_error = f"{type(exc).__name__}: {exc}"[:2000]

    def stopping(self) -> None:
        self.phase = "stopping"


health_state = HealthState()


async def readiness_details() -> tuple[bool, dict]:
    details = {"phase": health_state.phase, "database": "unknown", "data_dir": "unknown", "disk_free_bytes": 0}
    if health_state.phase != "ready":
        return False, details
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        details["database"] = "ready"
    except Exception as exc:
        details["database"] = f"error: {type(exc).__name__}"
        return False, details
    try:
        usage = shutil.disk_usage(DATA_DIR)
        details["disk_free_bytes"] = usage.free
        details["data_dir"] = "ready" if DATA_DIR.is_dir() else "missing"
    except OSError as exc:
        details["data_dir"] = f"error: {type(exc).__name__}"
        return False, details
    return details["data_dir"] == "ready", details
