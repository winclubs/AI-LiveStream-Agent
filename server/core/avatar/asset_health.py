# -*- coding: utf-8 -*-
"""
数字人资产健康闸门 (LIPSYNC_OPTIMIZATION_PLAN.md v3.1 §3.1)

背景
----
资产预处理阶段的守卫（`task_manager._detect_faces_and_coords_worker`）只在
**新切片**时生效。存量资产是修复前生成的，仍带着「静默回退」时期的损坏帧：
大量 `face_imgs` 里根本没有人脸（躯干/背景），送进 LatentSync 只会产出噪声唇形。

实测（audit_avatar_assets.py 全量复检 78 个资产）::

    task_1eda17e660 等 5 个   563 帧   检出率 34.3%   最长连续丢失 66 帧
    task_e0b46ab5d3          3000 帧   检出率 96.5%   最长连续丢失 25 帧

本模块提供**开播前**的资产健康检查：读 `meta.json` 的落盘指标（快），
无落盘指标时回落到抽样 Haar 复检（较慢但可靠）。

三级策略
--------
1. **快路径**：`meta.json` 含 `detection_rate` / `max_carry_run` 且已通过守卫
   -> 直接采信（资产就是新守卫生成的）。
2. **抽样复检**：存量资产无落盘指标 -> 抽样 N 帧跑 Haar，估算检出率。
3. **报告但��默认**：两种路径都只**告警**，不阻断开播。是否更换素材是产品
   与运营决策 —— 但必须让用户在开播前就看到「这个主播素材有 N 帧是无人脸的」。
"""
from __future__ import annotations

import glob
import json
import logging
import os
import pickle
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger("LiveAgent.AssetHealth")

# 与 task_manager 的资产守卫保持一致
MIN_DETECT_RATIO = 0.90
MAX_CARRY_FRAMES = 5

# 抽样复检的默认帧数（存量资产无落盘指标时使用）
DEFAULT_SAMPLE = 60

STATUS_OK = "ok"
STATUS_DEGRADED = "degraded"
STATUS_UNUSABLE = "unusable"
STATUS_UNKNOWN = "unknown"


@dataclass
class AssetHealth:
    """资产健康结论"""

    status: str = STATUS_UNKNOWN
    asset_dir: str = ""
    detect_rate: float = 0.0
    max_consecutive_miss: int = 0
    total_frames: int = 0
    source: str = ""            # meta / sampled_haar / unavailable
    reason: str = ""
    hints: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["ok"] = self.ok
        return d

    def summary(self) -> str:
        if self.status == STATUS_OK:
            return f"资产健康：检出率 {self.detect_rate:.1%}"
        if self.status == STATUS_UNKNOWN:
            return f"资产健康：无法判定（{self.reason}）"
        return (
            f"资产不健康：检出率 {self.detect_rate:.1%}"
            f"（{self.total_frames} 帧，最长连续丢失 {self.max_consecutive_miss} 帧）"
        )

    def _classify(self, from_meta: bool) -> None:
        """按检出率与连续丢失长度定级。"""
        if self.detect_rate >= MIN_DETECT_RATIO and self.max_consecutive_miss <= MAX_CARRY_FRAMES:
            self.status = STATUS_OK
            self.reason = ""
            return

        self.status = STATUS_DEGRADED if self.detect_rate >= MIN_DETECT_RATIO else STATUS_UNUSABLE
        bits = []
        if self.detect_rate < MIN_DETECT_RATIO:
            bits.append(f"人脸检出率 {self.detect_rate:.1%} < {MIN_DETECT_RATIO:.0%}")
        if self.max_consecutive_miss > MAX_CARRY_FRAMES:
            bits.append(f"最长连续丢失 {self.max_consecutive_miss} 帧 > {MAX_CARRY_FRAMES}")
        self.reason = "; ".join(bits)

        self.hints = [
            "该资产的人脸切片中存在无人脸帧（躯干/背景），送入 LatentSync 会产出噪声唇形，"
            "是唇形突变与不稳定的主要来源",
            "请在设置页重新上传原始素材触发重切；新资产守卫会在检出率不足时直接拒绝生成",
            "在重切完成前，建议不要用该主播进行正式直播",
        ]
        if not from_meta:
            self.hints.append(
                "（本结论基于抽样 Haar 复检，非精确统计；"
                "可用 python scripts/audit_avatar_assets.py 做全量复核）"
            )


def _load_meta(asset_dir: str) -> Dict[str, Any]:
    p = os.path.join(asset_dir, "meta.json")
    if not os.path.exists(p):
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _sample_haar(asset_dir: str, sample: int = DEFAULT_SAMPLE) -> Optional[Dict[str, Any]]:
    """抽样 Haar 复检 face_imgs。返回 None 表示无法判定。"""
    try:
        import cv2
        import numpy as np
    except ImportError:
        return None

    faces = sorted(
        glob.glob(os.path.join(asset_dir, "face_imgs", "*.jpg")),
        key=lambda s: int(os.path.splitext(os.path.basename(s))[0])
        if os.path.splitext(os.path.basename(s))[0].isdigit() else 0,
    )
    if not faces:
        return {"total": 0, "detected": 0, "rate": 0.0, "max_run": 0, "judged": False}

    cascade_path = os.path.join(cv2.data.haarcascades,
                                "haarcascade_frontalface_default.xml")
    cascade = cv2.CascadeClassifier(cascade_path)
    if cascade.empty():
        return None

    step = max(1, len(faces) // max(1, sample))
    picked = faces[::step][:sample]
    detected = 0
    run = max_run = 0
    for f in picked:
        im = cv2.imread(f)
        ok = False
        if im is not None:
            g = cv2.equalizeHist(cv2.cvtColor(im, cv2.COLOR_BGR2GRAY))
            ok = len(cascade.detectMultiScale(
                g, scaleFactor=1.1, minNeighbors=4, minSize=(60, 60))) > 0
        if ok:
            detected += 1
            run = 0
        else:
            run += 1
            max_run = max(max_run, run)

    # 连续丢失按抽样步长折算回原始帧数
    scale = step
    return {
        "total": len(faces),
        "sampled": len(picked),
        "detected": detected,
        "rate": detected / max(1, len(picked)),
        "max_run": max_run * scale,
        "judged": True,
    }


def check_asset_health(asset_dir: str, sample: int = DEFAULT_SAMPLE,
                       allow_sampled: bool = True) -> AssetHealth:
    """检查单个资产目录的健康度。"""
    result = AssetHealth(asset_dir=asset_dir)

    if not asset_dir or not os.path.isdir(asset_dir):
        result.status = STATUS_UNKNOWN
        result.reason = "资产目录不存在"
        return result

    meta = _load_meta(asset_dir)
    coords_path = os.path.join(asset_dir, "coords.pkl")
    total = 0
    if os.path.exists(coords_path):
        try:
            with open(coords_path, "rb") as f:
                total = len(pickle.load(f))
        except Exception:
            total = 0
    result.total_frames = total or int(meta.get("frame_count") or 0)

    # 快路径：新守卫生成的资产，meta 里有可信指标
    if meta.get("detection_rate") is not None and meta.get("max_carry_run") is not None:
        result.source = "meta"
        result.detect_rate = float(meta.get("detection_rate") or 0.0)
        result.max_consecutive_miss = int(meta.get("max_carry_run") or 0)
        result._classify(from_meta=True)
        return result

    # 存量资产：抽样复检
    if not allow_sampled:
        result.status = STATUS_UNKNOWN
        result.source = "unavailable"
        result.reason = "资产无落盘检测指标，且未启用抽样复检"
        return result

    sampled = _sample_haar(asset_dir, sample)
    if sampled is None or not sampled.get("judged"):
        result.status = STATUS_UNKNOWN
        result.source = "unavailable"
        result.reason = "无法执行 Haar 复检（缺少 opencv 或分类器不可用）"
        return result

    result.source = "sampled_haar"
    result.detect_rate = float(sampled["rate"])
    result.max_consecutive_miss = int(sampled["max_run"])
    if not result.total_frames:
        result.total_frames = int(sampled["total"])
    result._classify(from_meta=False)
    return result


# 向后兼容别名
_classify_impl = AssetHealth._classify


def check_and_warn(asset_dir: str, logger_: Optional[logging.Logger] = None,
                    **kwargs) -> AssetHealth:
    """检查并在不达标时记录告警。"""
    log = logger_ or logger
    health = check_asset_health(asset_dir, **kwargs)
    if health.status in (STATUS_DEGRADED, STATUS_UNUSABLE):
        log.warning("数字人资产健康告警 [%s]: %s", asset_dir, health.summary())
        for h in health.hints:
            log.warning("  · %s", h)
    elif health.status == STATUS_UNKNOWN and log is not None:
        log.info("数字人资产健康未知 [%s]: %s", asset_dir, health.reason)
    return health


class AssetUnusableError(RuntimeError):
    """资产严重损坏（检出率严重不足或连续丢失帧过多），送入神经模型将产生局部画面严重畸变与噪声。"""
    pass


def check_and_enforce(
    asset_dir: str,
    allow_unusable: Optional[bool] = None,
    logger_: Optional[logging.Logger] = None,
    **kwargs,
) -> AssetHealth:
    """检查并在资产严重损坏 (STATUS_UNUSABLE) 时抛出 AssetUnusableError 阻断开播。"""
    health = check_and_warn(asset_dir, logger_=logger_, **kwargs)
    if allow_unusable is None:
        allow_unusable = os.getenv("LIVE_AGENT_ALLOW_UNUSABLE_ASSETS", "0").lower() in ("1", "true", "yes")
    if health.status == STATUS_UNUSABLE and not allow_unusable:
        raise AssetUnusableError(
            f"数字人资产严重损坏，已阻断开播：{health.reason}。"
            f"人脸切片中存在过多非人脸/损坏帧，送入神经重绘模型会导致剧烈抽搐与闪烁。"
            f"请重新上传视频生成切片，或配置环境变量 LIVE_AGENT_ALLOW_UNUSABLE_ASSETS=1 强制跳过。"
        )
    return health

