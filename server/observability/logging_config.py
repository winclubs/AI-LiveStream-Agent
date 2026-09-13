"""应用统一控制台与持久化轮转日志。"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
from logging.handlers import RotatingFileHandler

from server.config import APP_VERSION, DATA_DIR

_CONFIGURED = False


class JsonLineFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "pid": os.getpid(),
            "version": APP_VERSION,
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    log_dir = DATA_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    level_name = os.getenv("LIVE_AGENT_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    max_bytes = max(64 * 1024, int(os.getenv("LIVE_AGENT_LOG_MAX_BYTES", str(5 * 1024 * 1024))))
    backup_count = max(1, min(100, int(os.getenv("LIVE_AGENT_LOG_BACKUP_COUNT", "5"))))

    root = logging.getLogger()
    root.setLevel(level)
    formatter = JsonLineFormatter()
    file_handler = RotatingFileHandler(
        log_dir / "server.log",
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(level)
    file_handler._live_agent_handler = True
    root.addHandler(file_handler)
    _CONFIGURED = True


def flush_logging() -> None:
    for handler in logging.getLogger().handlers:
        if getattr(handler, "_live_agent_handler", False):
            handler.flush()
