# -*- coding: utf-8 -*-
"""
试播台词驱动编排服务 (SpeechDrivePreviewService)

把「试听台词驱动」从纯前端假驱动升级为真实神经推理闭环：
1. 复用共享 TTS 合成得到音频；
2. 解码为 16k 单声道 float32 PCM；
3. 依据 gpu_target 分流真实推理 (本地 ONNX / 云端 Sidecar)，均不可用则诚实降级；
4. 以 25 FPS 渲染帧并落盘临时会话目录，返回帧地址与【实测】遥测。

恪守 ADR-16：遥测字段全部来自真实计时与设备探测，降级时如实标注，绝不伪造。
"""
import asyncio
import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import cv2
import numpy as np

from server.config import DATA_DIR

logger = logging.getLogger("LiveAgent.SpeechDrivePreview")

FPS = 25
MAX_FRAMES = 300                 # 约 12 秒，与 sidecar 帧预算同量级
MAX_TEXT_LENGTH = 500
MEL_CONTEXT_SAMPLES = 3200       # ±200ms @16k，对齐 Wav2Lip 上下文窗口
PREVIEW_SESSION_ROOT = DATA_DIR / "preview_speech_sessions"
SESSIONS_KEPT_PER_ANCHOR = 3
JPEG_QUALITY = 90

ENGINE_LOCAL = "neural_local_onnx"
ENGINE_CLOUD = "neural_cloud_sidecar"
ENGINE_FALLBACK = "procedural_fallback"


@dataclass
class RenderOutcome:
    """单次渲染产物与实测遥测"""
    face_frames: List[np.ndarray] = field(default_factory=list)   # 256x256 BGR
    full_frames: List[np.ndarray] = field(default_factory=list)   # 原尺寸 BGR
    timings_ms: List[float] = field(default_factory=list)
    device: str = ""
    providers: List[str] = field(default_factory=list)
    vram_total_gb: Optional[float] = None


@dataclass
class SpeechDriveResult:
    engine: str
    mode: str
    device: str
    providers: List[str]
    mean_inference_ms: float
    total_inference_ms: float
    vram_total_gb: Optional[float]
    fps: int
    frame_count: int
    session_id: str
    fallback_reason: Optional[str] = None


def _resample_to_16k_mono(pcm: np.ndarray, sr: int) -> np.ndarray:
    """线性插值重采样到 16k 单声道 float32（mel 特征标准采样率）"""
    if pcm is None or pcm.size == 0:
        return np.zeros(1, dtype=np.float32)
    mono = pcm if pcm.ndim == 1 else pcm.mean(axis=1)
    mono = mono.astype(np.float32)
    if sr == 16000:
        return mono
    n = int(round(len(mono) * 16000.0 / float(sr)))
    if n <= 0:
        return np.zeros(1, dtype=np.float32)
    idx = np.linspace(0.0, len(mono) - 1, n)
    i0 = np.floor(idx).astype(np.int64)
    i1 = np.minimum(i0 + 1, len(mono) - 1)
    w = (idx - i0).astype(np.float32)
    return (mono[i0] * (1.0 - w) + mono[i1] * w).astype(np.float32)


def _compute_frame_count(duration_sec: float) -> int:
    return max(0, min(MAX_FRAMES, int(math.floor(duration_sec * FPS))))


def _decode_pcm16k_mono(audio_bytes: bytes) -> np.ndarray:
    """容器音频 -> 16k 单声道 float32；失败抛 RuntimeError"""
    from server.core.media.audio_decode import decode_audio_to_float32

    pcm, sr = decode_audio_to_float32(audio_bytes, fallback_sample_rate=16000, allow_raw_pcm=True)
    if pcm is None or pcm.size == 0:
        raise RuntimeError("音频解码为空")
    return _resample_to_16k_mono(pcm, int(sr) if sr else 16000)


def _load_full_imgs(asset_dir: Path) -> List[np.ndarray]:
    imgs: List[np.ndarray] = []
    for p in sorted(asset_dir.glob("full_imgs/*.jpg"), key=lambda q: int(q.stem) if q.stem.isdigit() else 0):
        img = cv2.imread(str(p))
        if img is not None:
            imgs.append(img)
    return imgs


class SpeechDrivePreviewService:
    def __init__(self, sessions_root: Path = PREVIEW_SESSION_ROOT) -> None:
        self._sessions_root = Path(sessions_root)
        self._anchor_locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, anchor_id: str) -> asyncio.Lock:
        return self._anchor_locks.setdefault(anchor_id, asyncio.Lock())

    # ------------------------------------------------------------------
    # 对外入口
    # ------------------------------------------------------------------
    async def run(
        self,
        anchor_id: str,
        asset_dir: str | Path,
        audio_bytes: bytes,
    ) -> SpeechDriveResult:
        anchor_id = str(anchor_id).strip()
        async with self._lock_for(anchor_id):
            pcm = _decode_pcm16k_mono(audio_bytes)
            duration = len(pcm) / 16000.0
            n_frames = _compute_frame_count(duration)
            if n_frames == 0:
                raise RuntimeError("音频时长过短，无可渲染帧")

            plan = await self._resolve_compute_plan()
            outcome, engine, mode, fallback_reason = await self._dispatch_render(
                plan, Path(asset_dir), pcm, n_frames
            )

            session_id = await self._persist(anchor_id, outcome, audio_bytes)
            self._cleanup_old_sessions(anchor_id, session_id)

            total_ms = float(np.sum(outcome.timings_ms)) if outcome.timings_ms else 0.0
            mean_ms = float(np.mean(outcome.timings_ms)) if outcome.timings_ms else 0.0
            return SpeechDriveResult(
                engine=engine,
                mode=mode,
                device=outcome.device,
                providers=list(outcome.providers),
                mean_inference_ms=round(mean_ms, 2),
                total_inference_ms=round(total_ms, 2),
                vram_total_gb=outcome.vram_total_gb,
                fps=FPS,
                frame_count=len(outcome.full_frames),
                session_id=session_id,
                fallback_reason=fallback_reason,
            )

    # ------------------------------------------------------------------
    # 算力分流 (Task 4 填充 _resolve_compute_plan；Task 6 填充云端分支)
    # ------------------------------------------------------------------
    async def _resolve_compute_plan(self):
        raise NotImplementedError

    async def _dispatch_render(self, plan, asset_dir, pcm, n_frames):
        raise NotImplementedError

    # ------------------------------------------------------------------
    # 落盘与清理
    # ------------------------------------------------------------------
    async def _persist(self, anchor_id: str, outcome: RenderOutcome, audio_bytes: bytes) -> str:
        session_id = datetime.now().strftime("%Y%m%d%H%M%S%f")
        base = self._sessions_root / anchor_id / session_id
        (base / "face").mkdir(parents=True, exist_ok=True)
        (base / "full").mkdir(parents=True, exist_ok=True)

        def _write() -> None:
            (base / "audio.mp3").write_bytes(audio_bytes)
            for i, (face, full) in enumerate(zip(outcome.face_frames, outcome.full_frames)):
                ok_f, f_buf = cv2.imencode(".jpg", face, [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY])
                ok_u, u_buf = cv2.imencode(".jpg", full, [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY])
                if ok_f:
                    (base / "face" / f"{i}.jpg").write_bytes(f_buf.tobytes())
                if ok_u:
                    (base / "full" / f"{i}.jpg").write_bytes(u_buf.tobytes())

        await asyncio.to_thread(_write)
        return session_id

    def _cleanup_old_sessions(self, anchor_id: str, keep_session_id: str) -> None:
        anchor_root = self._sessions_root / anchor_id
        if not anchor_root.exists():
            return
        sessions = sorted(
            (p for p in anchor_root.iterdir() if p.is_dir()),
            key=lambda p: p.name,
            reverse=True,
        )
        for old in sessions[SESSIONS_KEPT_PER_ANCHOR:]:
            if old.name == keep_session_id:
                continue
            try:
                for f in old.rglob("*"):
                    if f.is_file():
                        f.unlink(missing_ok=True)
                old.rmdir()
            except Exception as e:
                logger.debug(f"清理旧试播会话 {old} 忽略: {e}")


_service_singleton: Optional[SpeechDrivePreviewService] = None


def get_speech_drive_preview_service() -> SpeechDrivePreviewService:
    global _service_singleton
    if _service_singleton is None:
        _service_singleton = SpeechDrivePreviewService()
    return _service_singleton
