# -*- coding: utf-8 -*-
"""
录制产物保留策略 (server.core.media.retention)

存在理由（上线诊断 BLOCKER-2）：
实机审计发现 `data/recordings` 已累积 2431 个目录 / 2.69 GB，且全仓
**没有任何自动清理机制** —— 唯一删除途径是人工调用删除接口。

对 7x24 商用直播节点，这是必然触发的磁盘耗尽事故：
    5 MB/场 x 20 场/天 = 100 MB/天 = 3.6 GB/月/节点，且无上限。

本模块提供三条互相独立的回收触发条件（满足其一即清理）：
    1. **超龄**：切片 mtime 早于 ``retention_days`` 天；
    2. **超量**：切片目录总数超过 ``max_count``；
    3. **超字节**：切片总体积超过 ``max_total_bytes``。

设计约束（安全护栏，宁可少删不可误删）：
    - 正在录制中的切片（active_ids）绝不删除；
    - ``records_index.json`` 索引文件绝不删除；
    - 永远优先删除**最旧**的切片；
    - 单个切片删除失败只记录不抛异常，绝不中断整轮清理。
"""
from __future__ import annotations

import logging
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional, Sequence

logger = logging.getLogger("LiveAgent.Retention")

#: 切片索引文件名，位于录像根目录，必须被保留策略豁免
INDEX_FILENAME = "records_index.json"

DEFAULT_RETENTION_DAYS = 7
DEFAULT_MAX_COUNT = 200
DEFAULT_MAX_TOTAL_MB = 5120


def _env_int(name: str, default: int) -> int:
    """读取整型环境变量；非法/缺失/非正值一律回退到安全默认值。

    回退而非 0，是因为 0 会被解释为「关闭清理」，等于让一次手误配置
    静默关掉磁盘保护。
    """
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        logger.warning(f"[录制保留策略] 环境变量 {name}={raw!r} 非法，回退默认值 {default}")
        return default
    if value <= 0:
        logger.warning(f"[录制保留策略] 环境变量 {name}={raw!r} 非正数，回退默认值 {default}")
        return default
    return value


@dataclass
class RetentionPolicy:
    """录制产物保留策略。``retention_days <= 0`` 表示关闭自动清理。"""

    retention_days: int = DEFAULT_RETENTION_DAYS
    max_count: int = DEFAULT_MAX_COUNT
    max_total_bytes: int = DEFAULT_MAX_TOTAL_MB * 1024 * 1024

    @classmethod
    def from_env(cls) -> "RetentionPolicy":
        raw_days = os.getenv("LIVE_AGENT_RECORDING_RETENTION_DAYS", "").strip()
        if raw_days == "0":
            # 显式 0 = 运维主动关闭自动清理，其余配额也一并放开。
            return cls(retention_days=0, max_count=0, max_total_bytes=0)
        return cls(
            retention_days=_env_int("LIVE_AGENT_RECORDING_RETENTION_DAYS", DEFAULT_RETENTION_DAYS),
            max_count=_env_int("LIVE_AGENT_RECORDING_MAX_COUNT", DEFAULT_MAX_COUNT),
            max_total_bytes=_env_int("LIVE_AGENT_RECORDING_MAX_TOTAL_MB", DEFAULT_MAX_TOTAL_MB) * 1024 * 1024,
        )

    @property
    def is_enabled(self) -> bool:
        return self.retention_days > 0 or self.max_count > 0 or self.max_total_bytes > 0


@dataclass
class PruneStats:
    """一轮清理的统计结果，用于可观测性与运维排障。"""

    total_pruned: int = 0
    pruned_by_age: int = 0
    pruned_by_count: int = 0
    pruned_by_size: int = 0
    failed: int = 0
    freed_bytes: int = 0
    remaining_count: int = 0
    remaining_bytes: int = 0
    errors: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "total_pruned": self.total_pruned,
            "pruned_by_age": self.pruned_by_age,
            "pruned_by_count": self.pruned_by_count,
            "pruned_by_size": self.pruned_by_size,
            "failed": self.failed,
            "freed_mb": round(self.freed_bytes / 1024 / 1024, 2),
            "remaining_count": self.remaining_count,
            "remaining_mb": round(self.remaining_bytes / 1024 / 1024, 2),
            "errors": list(self.errors),
        }


def _dir_size_bytes(path: Path) -> int:
    total = 0
    try:
        for child in path.rglob("*"):
            if child.is_file():
                try:
                    total += child.stat().st_size
                except OSError:
                    continue
    except OSError:
        return total
    return total


def _collect_recordings(root: Path, active_ids: Iterable[str]) -> list[tuple[Path, float, int]]:
    """扫描录像根目录，返回按「最旧优先」排序的 (目录, mtime, 字节数) 列表。

    只统计子目录；``records_index.json`` 等文件一律忽略。
    """
    protected = {a for a in (active_ids or ()) if a}
    entries: list[tuple[Path, float, int]] = []
    try:
        children = list(root.iterdir())
    except OSError as exc:
        logger.warning(f"[录制保留策略] 无法扫描录像目录 {root}: {exc}")
        return entries

    for child in children:
        if not child.is_dir() or child.name == INDEX_FILENAME:
            continue
        if child.name in protected:
            continue
        try:
            mtime = child.stat().st_mtime
        except OSError:
            continue
        entries.append((child, mtime, _dir_size_bytes(child)))

    entries.sort(key=lambda item: item[1])
    return entries


def _remove(path: Path, stats: PruneStats, reason: str, protected: set[str]) -> bool:
    """删除单个切片目录；失败只记录不抛出。"""
    if path.name in protected:
        return False
    size = _dir_size_bytes(path)
    try:
        shutil.rmtree(path, ignore_errors=False)
    except Exception as exc:  # 单个失败不得中断整轮清理
        stats.failed += 1
        stats.errors.append(f"{path.name}: {exc}")
        logger.warning(f"[录制保留策略] 删除切片失败 {path.name} ({reason}): {exc}")
        return False
    stats.total_pruned += 1
    stats.freed_bytes += size
    if reason == "age":
        stats.pruned_by_age += 1
    elif reason == "count":
        stats.pruned_by_count += 1
    elif reason == "size":
        stats.pruned_by_size += 1
    logger.info(f"[录制保留策略] 已回收切片 {path.name}（原因: {reason}，释放 {size / 1024 / 1024:.1f}MB）")
    return True


def prune_recordings(
    root: Path | str,
    policy: Optional[RetentionPolicy] = None,
    active_ids: Iterable[str] = (),
    index_entries: Optional[Sequence[dict]] = None,
) -> PruneStats:
    """按保留策略回收超限切片目录。

    ``index_entries`` 为可选的索引缓存列表（``record_id`` -> 目录名 映射），
    被回收的条目会从中同步移除，避免列表页出现死链。
    """
    policy = policy or RetentionPolicy.from_env()
    stats = PruneStats()

    root_path = Path(root)
    if not policy.is_enabled:
        return stats
    if not root_path.exists():
        return stats

    protected = {a for a in (active_ids or ()) if a}
    entries = _collect_recordings(root_path, protected)
    if not entries:
        stats.remaining_count = 0
        return stats

    remaining = list(entries)

    # --- 触发条件 1：超龄 ---
    if policy.retention_days > 0:
        cutoff = time.time() - policy.retention_days * 86400
        survivors = []
        for path, mtime, _size in remaining:
            if mtime < cutoff:
                _remove(path, stats, "age", protected)
            else:
                survivors.append((path, mtime, _size))
        remaining = survivors

    # --- 触发条件 2：超量（保留最新的 max_count 个）---
    if policy.max_count > 0 and len(remaining) > policy.max_count:
        # remaining 已按 mtime 升序 -> 头部即最旧，从头部删到剩 max_count
        excess = len(remaining) - policy.max_count
        for path, _mtime, _size in remaining[:excess]:
            _remove(path, stats, "count", protected)
        remaining = remaining[excess:]

    # --- 触发条件 3：超字节（同样优先删最旧）---
    if policy.max_total_bytes > 0:
        total = sum(size for _p, _m, size in remaining)
        i = 0
        while total > policy.max_total_bytes and i < len(remaining):
            path, _mtime, size = remaining[i]
            if _remove(path, stats, "size", protected):
                total -= size
            i += 1
        remaining = remaining[i:]

    stats.remaining_count = len(remaining)
    stats.remaining_bytes = sum(size for _p, _m, size in remaining)

    # 同步清理索引中的死链
    if index_entries is not None and stats.total_pruned:
        pruned_names = set()
        for path, _m, _s in entries:
            if not path.exists():
                pruned_names.add(path.name)
        if pruned_names:
            alive = [e for e in index_entries if e.get("record_id") not in pruned_names]
            if len(alive) != len(index_entries):
                try:
                    index_entries[:] = alive
                except TypeError:
                    pass

    if stats.total_pruned:
        logger.info(
            "[录制保留策略] 清理完成: 回收 %d 个切片，释放 %.1fMB，剩余 %d 个 / %.1fMB",
            stats.total_pruned,
            stats.freed_bytes / 1024 / 1024,
            stats.remaining_count,
            stats.remaining_bytes / 1024 / 1024,
        )
    return stats


def run_startup_prune(active_ids: Iterable[str] = ()) -> PruneStats:
    """服务启动时执行一轮清理。

    刻意做成**模块函数**而非在 ``recorder.py`` 导入期执行：
    导入期副作用会污染单元测试与测试数据隔离。
    """
    from server.core.media.recorder import RECORDINGS_DIR, get_record_manager

    manager = get_record_manager()
    entries = list(getattr(manager, "_records_cache", []) or [])
    stats = prune_recordings(
        RECORDINGS_DIR,
        RetentionPolicy.from_env(),
        active_ids=active_ids,
        index_entries=entries,
    )
    if stats.total_pruned:
        try:
            manager._records_cache[:] = entries
            manager._save_records_index()
        except Exception:
            logger.exception("[录制保留策略] 同步切片索引失败")
    return stats
