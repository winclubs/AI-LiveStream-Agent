# -*- coding: utf-8 -*-
"""
录制产物保留策略回归测试。

背景（上线诊断 BLOCKER-2）：
`data/recordings` 目录在实机上已累积 2431 个目录 / 2.69 GB，且全仓 grep
`retention_days|max_recordings|auto_clean|prune` 零命中 —— **没有任何自动
清理机制**，唯一删除途径是人工调用 `DELETE /api/v1/live/recordings/{id}`。

对 7x24 商用直播节点，这是必然触发的磁盘耗尽事故：
5 MB/场 x 20 场/天 = 100 MB/天 = 3.6 GB/月/节点，且无上限。

本文件锁定三条不变量：
    1. 超龄切片必须被自动回收；
    2. 超量/超字节切片必须被自动回收；
    3. 保留策略**绝不**触碰正在录制中的切片与保底窗口内的新切片。
"""
import os
import time
from pathlib import Path

import pytest



def _make_recording(root: Path, record_id: str, age_days: float = 0.0, size_kb: int = 8) -> Path:
    """构造一个具备真实落盘结构的切片目录，并回填 mtime 以模拟老化。"""
    d = root / record_id
    d.mkdir(parents=True, exist_ok=True)
    payload = b"0" * (size_kb * 1024)
    (d / f"clip_{record_id}.mp4").write_bytes(payload)
    (d / "preview.jpg").write_bytes(payload[:512])
    if age_days > 0:
        stamp = time.time() - age_days * 86400
        os.utime(d, (stamp, stamp))
        for child in d.iterdir():
            os.utime(child, (stamp, stamp))
    return d


@pytest.fixture
def isolated_recordings(tmp_path, monkeypatch):
    """把切片落盘根目录重定向到 tmp_path，避免污染真实数据目录。"""
    root = tmp_path / "recordings"
    root.mkdir(parents=True, exist_ok=True)
    return root


# ---------------------------------------------------------------------------
# RED-1: 超龄切片必须被回收
# ---------------------------------------------------------------------------

def test_prunes_recordings_older_than_retention_window(isolated_recordings):
    """超过保留窗口的切片必须被删除。"""
    from server.core.media.retention import RetentionPolicy, prune_recordings

    old = _make_recording(isolated_recordings, "sess_old", age_days=30)
    _make_recording(isolated_recordings, "sess_fresh", age_days=0.5)

    stats = prune_recordings(isolated_recordings, RetentionPolicy(retention_days=7))

    assert not old.exists(), "30 天前的切片应在保留窗口(7天)外被回收"
    assert stats.pruned_by_age == 1


def test_keeps_recordings_inside_retention_window(isolated_recordings):
    """保留窗口内的切片必须完整保留。"""
    from server.core.media.retention import RetentionPolicy, prune_recordings

    fresh = _make_recording(isolated_recordings, "sess_fresh", age_days=1)

    prune_recordings(isolated_recordings, RetentionPolicy(retention_days=7))

    assert fresh.exists(), "1 天前的切片仍在保留窗口内，不应被删除"


# ---------------------------------------------------------------------------
# RED-2: 数量与字节配额必须生效（按最旧的先删）
# ---------------------------------------------------------------------------

def test_prunes_excess_count_keeping_newest(isolated_recordings):
    """切片数量超限时，必须保留最新的若干个并删除最旧的。"""
    from server.core.media.retention import RetentionPolicy, prune_recordings

    for i in range(5):
        _make_recording(isolated_recordings, f"sess_{i}", age_days=(5 - i))  # sess_0 最旧

    stats = prune_recordings(isolated_recordings, RetentionPolicy(max_count=2))

    remaining = sorted(p.name for p in isolated_recordings.iterdir() if p.is_dir())
    assert remaining == ["sess_3", "sess_4"], f"应保留最新 2 个，实际 {remaining}"
    assert stats.pruned_by_count == 3


def test_prunes_excess_total_bytes_keeping_newest(isolated_recordings):
    """总字节超限时，必须优先删除最旧的切片。"""
    from server.core.media.retention import RetentionPolicy, prune_recordings

    for i in range(4):
        _make_recording(isolated_recordings, f"sess_{i}", age_days=(4 - i), size_kb=64)  # 每条约 64.5KB

    # 总计约 258KB；上限 140KB -> 只能保留 2 个（约 129KB）
    stats = prune_recordings(isolated_recordings, RetentionPolicy(max_total_bytes=140 * 1024))

    remaining = sorted(p.name for p in isolated_recordings.iterdir() if p.is_dir())
    assert len(remaining) <= 2, f"字节超限后应仅保留约 2 个，实际 {remaining}"
    assert remaining == ["sess_2", "sess_3"], f"应保留最新的 2 个，实际 {remaining}"
    assert stats.pruned_by_size >= 1


# ---------------------------------------------------------------------------
# RED-3: 安全护栏 —— 绝不误删正在录制 / 索引文件
# ---------------------------------------------------------------------------

def test_never_prunes_active_recording(isolated_recordings):
    """正在录制中的切片（即使超龄超量）也绝不能被回收。"""
    from server.core.media.retention import RetentionPolicy, prune_recordings

    active = _make_recording(isolated_recordings, "sess_active", age_days=99, size_kb=512)
    _make_recording(isolated_recordings, "sess_other", age_days=99)

    prune_recordings(isolated_recordings, RetentionPolicy(retention_days=1, max_count=1), active_ids={"sess_active"})

    assert active.exists(), "正在录制中的切片被误删，会直接毁掉正在进行的直播切片"


def test_never_deletes_index_file(isolated_recordings):
    """清理过程绝不能删除切片索引文件 records_index.json。"""
    from server.core.media.retention import RetentionPolicy, prune_recordings

    index = isolated_recordings / "records_index.json"
    index.write_text("[]", encoding="utf-8")
    _make_recording(isolated_recordings, "sess_old", age_days=99)

    prune_recordings(isolated_recordings, RetentionPolicy(retention_days=1))

    assert index.exists(), "records_index.json 是切片元数据唯一索引，绝不能被清理误删"


def test_prune_is_idempotent_and_safe_on_empty_dir(isolated_recordings):
    """空目录上重复执行清理不得抛异常（幂等）。"""
    from server.core.media.retention import RetentionPolicy, prune_recordings

    policy = RetentionPolicy(retention_days=1)
    for _ in range(3):
        stats = prune_recordings(isolated_recordings, policy)
        assert stats.total_pruned == 0


def test_prune_removes_stale_index_entries(isolated_recordings):
    """被清理的切片必须同步从索引缓存中移除，避免列表页出现死链。"""
    from server.core.media.retention import RetentionPolicy, prune_recordings

    victim = _make_recording(isolated_recordings, "sess_victim", age_days=99)
    stale_entry = {"record_id": "sess_victim", "video_path": str(victim / "clip_sess_victim.mp4")}

    prune_recordings(isolated_recordings, RetentionPolicy(retention_days=1), index_entries=[stale_entry])

    assert not victim.exists()


# ---------------------------------------------------------------------------
# RED-4: 策略默认值与关停开关
# ---------------------------------------------------------------------------

def test_default_policy_comes_from_env(monkeypatch):
    """保留策略默认值必须来自环境变量，且必须有安全的兜底默认值。"""
    from server.core.media.retention import RetentionPolicy

    monkeypatch.setenv("LIVE_AGENT_RECORDING_RETENTION_DAYS", "3")
    monkeypatch.setenv("LIVE_AGENT_RECORDING_MAX_COUNT", "50")
    monkeypatch.setenv("LIVE_AGENT_RECORDING_MAX_TOTAL_MB", "2048")

    policy = RetentionPolicy.from_env()

    assert policy.retention_days == 3
    assert policy.max_count == 50
    assert policy.max_total_bytes == 2048 * 1024 * 1024


def test_invalid_env_falls_back_to_safe_defaults(monkeypatch):
    """非法环境变量值必须回退到安全默认值，而不是崩溃或产生 0 天保留。"""
    from server.core.media.retention import RetentionPolicy

    monkeypatch.setenv("LIVE_AGENT_RECORDING_RETENTION_DAYS", "abc")
    monkeypatch.setenv("LIVE_AGENT_RECORDING_MAX_COUNT", "-5")
    monkeypatch.setenv("LIVE_AGENT_RECORDING_MAX_TOTAL_MB", "not-a-number")

    policy = RetentionPolicy.from_env()

    assert policy.retention_days > 0, "非法值必须回退到 >0 的安全保留天数"
    assert policy.max_count > 0
    assert policy.max_total_bytes > 0


def test_prune_can_be_disabled(monkeypatch):
    """运维必须能通过环境变量完全关闭自动清理（0 天 = 关闭）。"""
    from server.core.media.retention import RetentionPolicy

    monkeypatch.setenv("LIVE_AGENT_RECORDING_RETENTION_DAYS", "0")
    monkeypatch.delenv("LIVE_AGENT_RECORDING_MAX_COUNT", raising=False)
    monkeypatch.delenv("LIVE_AGENT_RECORDING_MAX_TOTAL_MB", raising=False)

    assert RetentionPolicy.from_env().is_enabled is False


def test_expired_policy_disabled_skips_pruning(isolated_recordings):
    """策略关闭时 prune 不得删除任何文件。"""
    from server.core.media.retention import RetentionPolicy, prune_recordings

    keep = _make_recording(isolated_recordings, "sess_keep", age_days=999)

    prune_recordings(isolated_recordings, RetentionPolicy(retention_days=0))

    assert keep.exists(), "策略关闭时不得清理任何切片"
