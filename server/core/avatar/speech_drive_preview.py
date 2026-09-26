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


def _load_coords(asset_dir: Path) -> list:
    import pickle

    coords_file = asset_dir / "coords.pkl"
    if not coords_file.exists():
        return []
    try:
        with open(coords_file, "rb") as f:
            return list(pickle.load(f))
    except Exception:
        return []


def _load_face_imgs(asset_dir: Path) -> List[np.ndarray]:
    imgs: List[np.ndarray] = []
    for p in sorted(asset_dir.glob("face_imgs/*.jpg"), key=lambda q: int(q.stem) if q.stem.isdigit() else 0):
        img = cv2.imread(str(p))
        if img is not None:
            if img.shape[0] != 256 or img.shape[1] != 256:
                img = cv2.resize(img, (256, 256), interpolation=cv2.INTER_AREA)
            imgs.append(img)
    return imgs


def _crop_like(full: np.ndarray, coord_box) -> np.ndarray:
    """与 NeuralLipRenderer.crop_face_256 同口径的轻量裁剪（降级路径用）"""
    try:
        ymin, ymax, xmin, xmax = coord_box
        fh, fw = full.shape[:2]
        ymin = max(0, min(fh - 1, int(ymin)))
        ymax = max(0, min(fh, int(ymax)))
        xmin = max(0, min(fw - 1, int(xmin)))
        xmax = max(0, min(fw, int(xmax)))
        if ymax - ymin < 10 or xmax - xmin < 10:
            return cv2.resize(full, (256, 256))
        return cv2.resize(full[ymin:ymax, xmin:xmax], (256, 256), interpolation=cv2.INTER_AREA)
    except Exception:
        return cv2.resize(full, (256, 256))


def _describe_local_device() -> str:
    try:
        from server.core.hardware.gpu_capability import probe_local_gpu

        gpu = probe_local_gpu()
        return str(gpu.gpu_name or "")
    except Exception:
        return ""


def _local_vram_gb() -> Optional[float]:
    try:
        from server.core.hardware.gpu_capability import probe_local_gpu

        gpu = probe_local_gpu()
        vram = float(gpu.vram_total_gb or 0.0)
        return round(vram, 2) if vram > 0 else None
    except Exception:
        return None


def _render_local_sync(
    renderer,
    pcm: np.ndarray,
    n_frames: int,
    full_imgs: List[np.ndarray],
    allow_cpu: bool = False,
) -> Optional[RenderOutcome]:
    """同步执行：以给定 renderer 在 25FPS 逐帧真实推理（供 run_cpu_bound 线程内调用）"""
    outcome = RenderOutcome()
    outcome.providers = list(getattr(getattr(renderer, "session", None), "get_providers", lambda: [])())
    outcome.device = _describe_local_device()
    outcome.vram_total_gb = _local_vram_gb()

    n = len(full_imgs)
    timings: List[float] = []
    face_out: List[np.ndarray] = []
    full_out: List[np.ndarray] = []
    for i in range(n_frames):
        center = int(i * (16000.0 / FPS))
        window = pcm[max(0, center - MEL_CONTEXT_SAMPLES): center + MEL_CONTEXT_SAMPLES]
        if window.size == 0:
            window = np.zeros(MEL_CONTEXT_SAMPLES * 2, dtype=np.float32)
        amp = float(np.sqrt(np.mean(np.square(window)))) if window.size else 0.0
        mouth_open = min(1.0, amp * 12.5)  # 与既有试听驱动相同灵敏度
        t0 = time.perf_counter()
        out = renderer.render_lip_frame(
            full_imgs[i % n], i, window, mouth_open, return_face=True
        )
        dt = (time.perf_counter() - t0) * 1000.0
        if out is None:
            return None  # 推理失败，交给上层降级
        full_blended, face256 = out
        full_out.append(full_blended)
        face_out.append(face256)
        timings.append(dt)

    outcome.face_frames = face_out
    outcome.full_frames = full_out
    outcome.timings_ms = timings
    return outcome


def _build_local_renderer(asset_dir: Path, allow_cpu: bool = False) -> Optional["object"]:
    """构造并载入资产的 NeuralLipRenderer（模块级，供线程内调用）"""
    from server.core.avatar.neural_lip_renderer import NeuralLipRenderer

    renderer = NeuralLipRenderer()
    if not renderer.is_ready:
        return None
    if not renderer.load_anchor_assets(asset_dir):
        return None
    return renderer


def _build_audio_frame_batch(pcm16k: np.ndarray):
    """把 16k 单声道 PCM 切分为 sidecar v3 要求的连续 AudioFrame 批次（每帧 400ms）"""
    import uuid

    from server.core.media.audio_frame import AudioFrame, AudioFormat, validate_audio_frame_batch

    fmt = AudioFormat(codec="pcm_s16le", sample_rate=16000, channels=1)
    int16 = np.clip(pcm16k * 32767.0, -32768.0, 32767.0).astype(np.int16)
    frame_samples = int(16000 * 0.4)  # 400ms/帧，低于 1s 上限
    audio_id = uuid.uuid4().hex
    frames: List[AudioFrame] = []
    pos = 0
    total = int(len(int16))
    while pos < total:
        chunk = int16[pos: pos + frame_samples]
        frames.append(
            AudioFrame(
                data=chunk.tobytes(),
                format=fmt,
                audio_id=audio_id,
                sequence=len(frames),
                pts_samples=pos,
                audio_generation=0,
                session_generation=0,
                text="",
                is_first=(len(frames) == 0),
                is_final=False,
            )
        )
        pos += len(chunk)
    if frames:
        last = frames[-1]
        frames[-1] = AudioFrame(
            data=last.data,
            format=last.format,
            audio_id=last.audio_id,
            sequence=last.sequence,
            pts_samples=last.pts_samples,
            audio_generation=last.audio_generation,
            session_generation=last.session_generation,
            text=last.text,
            is_first=last.is_first,
            is_final=True,
        )
    return validate_audio_frame_batch(frames)


@dataclass(frozen=True)
class SidecarConnection:
    node_url: str
    auth_token: str
    canonical: dict
    model_name: str


def _build_sidecar_driver(conn: "SidecarConnection"):
    """按连接参数构造 NeuralSidecarMediaDriver（预览会话独占，不与直播管路共享）"""
    from server.adapters.media.neural_sidecar_driver import NeuralSidecarMediaDriver

    canonical = conn.canonical or {}
    return NeuralSidecarMediaDriver(
        node_url=conn.node_url,
        auth_token=conn.auth_token,
        backend_id=str(canonical.get("backend_id") or conn.model_name or "auto"),
        avatar_id=str(canonical.get("avatar_id") or "default"),
        avatar_revision=str(canonical.get("avatar_revision") or ""),
        avatar_digest=str(canonical.get("avatar_digest") or ""),
        license_manifest_digest=str(canonical.get("license_manifest_digest") or ""),
        weights_sha256=str(canonical.get("weights_sha256") or ""),
        model_version=str(canonical.get("model_version") or ""),
        require_neural_lipsync=canonical.get("require_neural_lipsync") is True,
    )


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
    # 算力分流
    # ------------------------------------------------------------------
    async def _resolve_compute_plan(self):
        from server.core.hardware.gpu_capability import evaluate_compute

        return await evaluate_compute(feature_name="试播台词驱动")

    async def _dispatch_render(self, plan, asset_dir, pcm, n_frames):
        """返回 (outcome, engine, mode, fallback_reason)。云端失败自动回退本地。"""
        local_gpu = plan.local_gpu or {}
        local_ok = bool(
            local_gpu.get("cuda_available")
            and float(local_gpu.get("vram_total_gb") or 0) >= 2.0
        )
        cloud = getattr(plan, "cloud_gpu", None)
        cloud_ok = bool(plan.use_cloud and plan.has_cloud_gpu and cloud and cloud.get("is_reachable"))

        if cloud_ok:
            try:
                outcome = await self._render_cloud(cloud, asset_dir, pcm, n_frames)
                if outcome is not None and outcome.full_frames:
                    return outcome, ENGINE_CLOUD, "cloud", None
                logger.warning("云端试播渲染未产出帧，自动回退本地引擎")
            except Exception as e:
                logger.warning(f"云端试播渲染失败，自动回退本地引擎: {e}")
            if local_ok:
                outcome = await self._render_local(asset_dir, pcm, n_frames)
                if outcome is not None:
                    return outcome, ENGINE_LOCAL, "local", "sidecar_unreachable"

        if local_ok:
            outcome = await self._render_local(asset_dir, pcm, n_frames)
            if outcome is not None:
                return outcome, ENGINE_LOCAL, "local", None
            return self._render_fallback(asset_dir, n_frames), ENGINE_FALLBACK, "fallback", "render_error"

        # 本地无 CUDA：仍尝试 CPU 推理，模型缺失/异常时诚实降级
        outcome = await self._render_local(asset_dir, pcm, n_frames, allow_cpu=True)
        if outcome is not None:
            return outcome, ENGINE_LOCAL, "local", "cuda_unavailable_cpu_fallback"
        reason = self._diagnose_local_failure()
        return self._render_fallback(asset_dir, n_frames), ENGINE_FALLBACK, "fallback", reason

    # ------------------------------------------------------------------
    # 本地 ONNX 推理
    # ------------------------------------------------------------------
    async def _render_local(
        self,
        asset_dir: Path,
        pcm: np.ndarray,
        n_frames: int,
        allow_cpu: bool = False,
    ) -> Optional[RenderOutcome]:
        from server.core.avatar.neural_model_manager import global_neural_model_manager

        if not global_neural_model_manager.is_model_available("onnx_lipsync"):
            logger.info("神经唇形模型未安装，试播跳过本地推理")
            return None
        try:
            from server.core.cpu_worker import run_cpu_bound

            outcome = await run_cpu_bound(_render_local_sync, asset_dir.as_posix(), pcm, n_frames, allow_cpu)
            return outcome
        except Exception as e:
            logger.warning(f"本地试播渲染异常: {e}")
            return None

    def _diagnose_local_failure(self) -> str:
        from server.core.avatar.neural_model_manager import global_neural_model_manager

        if not global_neural_model_manager.is_model_available("onnx_lipsync"):
            return "model_not_installed"
        return "onnx_runtime_unavailable"

    # ------------------------------------------------------------------
    # 降级：仅回放原素材（微动态），绝不声称神经渲染
    # ------------------------------------------------------------------
    def _render_fallback(self, asset_dir: Path, n_frames: int) -> RenderOutcome:
        full_imgs = _load_full_imgs(asset_dir)
        coords = _load_coords(asset_dir)
        if not full_imgs:
            raise RuntimeError("主播资产缺少 full_imgs 帧序列")
        face_imgs = _load_face_imgs(asset_dir)
        n = max(1, len(full_imgs))
        outcome = RenderOutcome()
        outcome.device = ""
        for i in range(n_frames):
            full = full_imgs[i % n]
            outcome.full_frames.append(full)
            if face_imgs:
                outcome.face_frames.append(face_imgs[i % len(face_imgs)])
            elif coords:
                outcome.face_frames.append(_crop_like(full, coords[i % len(coords)]))
            else:
                outcome.face_frames.append(cv2.resize(full, (256, 256)))
            outcome.timings_ms.append(0.0)
        return outcome

    # ------------------------------------------------------------------
    # 云端 Sidecar 批量推理
    # ------------------------------------------------------------------
    async def _resolve_sidecar_connection(self, cloud_gpu) -> Optional["SidecarConnection"]:
        """复用 evaluate_compute 的探活结果，从同一 DB 记录取连接参数"""
        import json

        from sqlalchemy import select

        from server.adapters.media.avatar_provider_registry import (
            normalize_avatar_provider_config,
        )
        from server.config import decrypt_secret
        from server.database.db import AsyncSessionLocal
        from server.database.models import ApiProviderConfig

        base_url = (getattr(cloud_gpu, "base_url", "") or "").strip()
        if not base_url:
            return None
        async with AsyncSessionLocal() as db:
            stmt = (
                select(ApiProviderConfig)
                .where(
                    ApiProviderConfig.config_group.in_(["neural_renderer", "remote_gpu"]),
                    ApiProviderConfig.is_active == 1,
                )
                .order_by(ApiProviderConfig.updated_at.desc())
            )
            res = await db.execute(stmt)
            record = next(
                (c for c in res.scalars().all() if (c.base_url or "").strip() == base_url),
                None,
            )
        if record is None:
            return None
        credential = decrypt_secret(record.encrypted_api_key) if record.encrypted_api_key else ""
        extra: dict = {}
        raw_extra = getattr(record, "extra_params_json", "") or "{}"
        try:
            extra = json.loads(raw_extra) if isinstance(raw_extra, str) else dict(raw_extra)
        except Exception:
            extra = {}
        try:
            canonical = normalize_avatar_provider_config(
                extra,
                base_url=base_url,
                credential_present=bool(str(credential).strip()),
            )
        except Exception as e:
            logger.warning(f"云端 sidecar 配置规范化失败，跳过云端试播: {e}")
            return None
        return SidecarConnection(
            node_url=base_url,
            auth_token=str(credential),
            canonical=canonical,
            model_name=str(getattr(record, "model_name", "") or "auto"),
        )

    async def _render_cloud(
        self,
        cloud_gpu,
        asset_dir: Path,
        pcm: np.ndarray,
        n_frames_hint: int,
    ) -> Optional[RenderOutcome]:
        driver = None
        try:
            conn = await self._resolve_sidecar_connection(cloud_gpu)
            if conn is None:
                logger.warning("无法解析云端 sidecar 连接参数，跳过云端试播")
                return None

            frames = _build_audio_frame_batch(pcm)
            driver = _build_sidecar_driver(conn)

            collected: list[bytes] = []

            async def _collect(jpeg: bytes, owner=None) -> None:
                # 预览专用收集器：不接管虚拟摄像头/直播画面，仅缓存 JPEG
                if jpeg:
                    collected.append(jpeg)

            # 包裹实例方法：预览会话独占，不与直播管路共享连接
            driver._publish_video_frame = _collect  # noqa: SLF001

            started = time.monotonic()
            await driver.start()

            await driver.feed_audio_frames(frames)
            # 时间线 consumer 按实时节奏发帧，等待其排空
            timeline_task = getattr(driver, "_timeline_task", None)
            if timeline_task is not None and not timeline_task.done():
                timeout = (len(pcm) / 16000.0) + 10.0
                await asyncio.wait_for(asyncio.shield(timeline_task), timeout=timeout)
        except Exception as e:
            logger.warning(f"云端 sidecar 试播失败: {e}")
            return None
        finally:
            if driver is not None:
                try:
                    await driver.stop()
                except Exception as e:
                    logger.debug(f"关闭预览 sidecar 连接忽略: {e}")

        if not collected:
            return None

        coords = _load_coords(asset_dir)
        outcome = RenderOutcome()
        outcome.device = str(
            (getattr(driver, "_selected_descriptor", {}) or {}).get("device")
            or getattr(cloud_gpu, "gpu_name", "")
            or ""
        )
        outcome.providers = ["remote:neural_lipsync"]
        vram = getattr(cloud_gpu, "vram_total_gb", None)
        outcome.vram_total_gb = round(float(vram), 2) if vram else None

        per_frame = (time.monotonic() - started) * 1000.0 / max(1, len(collected))
        for i, jpeg in enumerate(collected):
            arr = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
            if arr is None:
                continue
            outcome.full_frames.append(arr)
            if coords:
                outcome.face_frames.append(_crop_like(arr, coords[i % len(coords)]))
            else:
                outcome.face_frames.append(cv2.resize(arr, (256, 256), interpolation=cv2.INTER_AREA))
            outcome.timings_ms.append(round(per_frame, 2))

        if not outcome.full_frames:
            return None
        return outcome

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
