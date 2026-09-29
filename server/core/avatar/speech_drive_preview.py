# -*- coding: utf-8 -*-
"""
试播台词驱动编排服务 (SpeechDrivePreviewService)

把「试听台词驱动」做成真实神经推理闭环（试播即真实直播效果）：
1. 复用共享 TTS 合成得到音频；
2. 解码为 16k 单声道 float32 PCM；
3. 依据 gpu_target 分流真实推理 (云端 Sidecar 优先 / 本地 ONNX GPU)；
4. 以 25 FPS 渲染帧并落盘临时会话目录，返回帧地址与【实测】遥测。

硬件硬性门禁 (用户规约)：试播只允许真实神经渲染 ——
本地 CUDA 显卡显存 >= 8GB，或已对接显存 >= 8GB 的云端 GPU 节点。
两者均不满足时【直接禁止试播】并抛出硬件不足提示；渲染失败同样如实报错，
绝不做 CPU 推理降级，绝不做程序化口型假降级（试播无真实意义）。

恪守 ADR-16：遥测字段全部来自真实计时与设备探测，绝不伪造。
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

# 试播硬件硬性门槛：真实神经渲染要求本地 CUDA 显存或云端 GPU 显存 >= 8GB
PREVIEW_REQUIRED_VRAM_GB = 8.0

ENGINE_LOCAL = "neural_local_onnx"
ENGINE_CLOUD = "neural_cloud_sidecar"


class PreviewHardwareError(RuntimeError):
    """试播硬件门槛不满足或真实渲染失败：直接禁止试播，如实告知原因，绝无降级。"""

    def __init__(self, message: str):
        super().__init__(message)
        self.detail = message


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
    """加载主播对齐人脸切片 (本地神经渲染线程入口使用)"""
    imgs: List[np.ndarray] = []
    for p in sorted(asset_dir.glob("face_imgs/*.jpg"), key=lambda q: int(q.stem) if q.stem.isdigit() else 0):
        img = cv2.imread(str(p))
        if img is not None:
            if img.shape[0] != 256 or img.shape[1] != 256:
                img = cv2.resize(img, (256, 256), interpolation=cv2.INTER_AREA)
            imgs.append(img)
    return imgs


def _crop_like(full: np.ndarray, coord_box) -> np.ndarray:
    """与 NeuralLipRenderer.crop_face_256 同口径的轻量裁剪（云端帧人脸切片提取用）"""
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
) -> Optional[RenderOutcome]:
    """同步执行：以给定 renderer 在 25FPS 逐帧真实推理（供 run_cpu_bound 线程内调用）"""
    outcome = RenderOutcome()
    outcome.providers = list(getattr(getattr(renderer, "session", None), "get_providers", lambda: [])())
    outcome.device = _describe_local_device()
    outcome.vram_total_gb = _local_vram_gb()

    n = len(full_imgs)
    if n == 0:
        return None
    timings: List[float] = []
    face_out: List[np.ndarray] = []
    full_out: List[np.ndarray] = []
    for i in range(n_frames):
        center = int(i * (16000.0 / FPS))
        window = pcm[max(0, center - MEL_CONTEXT_SAMPLES): center + MEL_CONTEXT_SAMPLES]
        if window.size == 0:
            window = np.zeros(MEL_CONTEXT_SAMPLES * 2, dtype=np.float32)
        amp = float(np.sqrt(np.mean(np.square(window)))) if window.size else 0.0
        mouth_open = min(1.0, amp * 12.5)  # 静音门限信号：仅用于跳过无声帧推理
        t0 = time.perf_counter()
        out = renderer.render_lip_frame(
            full_imgs[i % n], i, window, mouth_open, return_face=True
        )
        dt = (time.perf_counter() - t0) * 1000.0
        if out is None:
            return None  # 推理失败，交由上层如实报错
        full_blended, face256 = out
        full_out.append(full_blended)
        face_out.append(face256)
        timings.append(dt)

    outcome.face_frames = face_out
    outcome.full_frames = full_out
    outcome.timings_ms = timings
    return outcome


def _build_local_renderer(asset_dir: Path) -> Optional["object"]:
    """构造并载入资产的 NeuralLipRenderer（模块级，供线程内调用）"""
    from server.core.avatar.neural_lip_renderer import NeuralLipRenderer

    renderer = NeuralLipRenderer()
    if not renderer.is_ready:
        return None
    if not renderer.load_anchor_assets(asset_dir):
        return None
    return renderer


def _render_local_entry(asset_dir_str: str, pcm: np.ndarray, n_frames: int) -> Optional[RenderOutcome]:
    """CPU 工作线程入口：构建 renderer + 载入资产帧后执行真实逐帧推理

    修复历史缺陷：此前 _render_local 直接以旧签名调用 _render_local_sync，
    导致 full_imgs 收到布尔值 (len(bool) 崩溃)，本地神经路径从未成功过。
    """
    asset_dir = Path(asset_dir_str)
    renderer = _build_local_renderer(asset_dir)
    if renderer is None:
        return None
    full_imgs = _load_full_imgs(asset_dir)
    if not full_imgs:
        return None
    return _render_local_sync(renderer, pcm, n_frames, full_imgs)


def _sample_cloud_asset_zip(asset_dir: Path, max_frames: int = 128) -> tuple[bytes, str]:
    """从本地资产目录均匀采样最多 max_frames 帧，生成可上传的 zip 字节 (含 re-number + 坐标子集 + meta.json)"""
    import io
    import json
    import pickle
    import zipfile
    import hashlib

    face_dir = asset_dir / "face_imgs"
    full_dir = asset_dir / "full_imgs"
    face_files = sorted(face_dir.glob("*.jpg"), key=lambda p: int(p.stem) if p.stem.isdigit() else 0) if face_dir.is_dir() else []
    full_files = sorted(full_dir.glob("*.jpg"), key=lambda p: int(p.stem) if p.stem.isdigit() else 0) if full_dir.is_dir() else []
    n = min(len(face_files), len(full_files), max_frames)
    if n == 0:
        raise ValueError("资产目录中没有可用的图片")
    step = max(1, len(face_files) // n)
    sampled_faces = face_files[::step][:n]
    sampled_fulls = full_files[::step][:n]
    # 采样 coords
    coords: list = []
    coords_file = asset_dir / "coords.pkl"
    if coords_file.exists():
        with open(coords_file, "rb") as f:
            all_coords = pickle.load(f)
        step_c = max(1, len(all_coords) // n)
        coords = [all_coords[i] for i in range(0, len(all_coords), step_c)][:n]
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w", zipfile.ZIP_DEFLATED) as zf:
        for idx, (face_fp, full_fp) in enumerate(zip(sampled_faces, sampled_fulls)):
            zf.writestr(f"face_imgs/{idx}.jpg", face_fp.read_bytes())
            zf.writestr(f"full_imgs/{idx}.jpg", full_fp.read_bytes())
        if coords:
            zf.writestr("coords.pkl", pickle.dumps(coords))
        meta = {"sample_count": n, "original_face_count": len(face_files), "original_full_count": len(full_files)}
        zf.writestr("meta.json", json.dumps(meta))
    zip_bytes = bio.getvalue()
    sha = hashlib.sha256(zip_bytes).hexdigest()
    return zip_bytes, sha


def _cloud_asset_digest(zip_bytes: bytes) -> str:
    """按云端 sidecar 存储口径计算摘要：对 zip 解压后的文件集合 (按路径排序逐字节拼接) 求 sha256。

    与服务端 AssetStore.get_asset_manifest / store_from_zip 的摘要同口径。
    注意：不能对整个本地资产目录求摘要——云端只保存采样后的子集，两者恒不相等，
    会导致「已同步则跳过」永远不生效，每次试播都重传十几 MB 资产。
    """
    import hashlib
    import io
    import zipfile

    h = hashlib.sha256()
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for info in sorted(zf.infolist(), key=lambda i: i.filename):
            h.update(zf.read(info.filename))
    return h.hexdigest()


ASSET_UPLOAD_CHUNK_BYTES = 1024 * 1024  # 分块上传块大小 (原始字节)：弱网下小块更易成功


class _AssetEndpointMissing(RuntimeError):
    """云端 sidecar 未部署该资产端点 (404)：供上层回退到兼容的上传方式"""


async def _post_asset(url: str, headers: dict, payload: bytes, timeout: int, attempts: int = 3) -> bytes:
    """POST 资产请求并对瞬时连接错误 (TLS EOF / 写超时) 重试。

    - HTTP 404 → 抛 _AssetEndpointMissing (端点未部署，上层可回退)
    - 其它 HTTP 错误 / 重试耗尽 → 抛 RuntimeError
    """
    import asyncio
    import urllib.error
    import urllib.request

    last_err: urllib.error.URLError | None = None
    for attempt in range(attempts):
        req = urllib.request.Request(url, headers=headers, data=payload, method="POST")
        req.add_header("Content-Type", "application/json")
        try:
            loop = asyncio.get_event_loop()
            resp = await loop.run_in_executor(None, lambda: urllib.request.urlopen(req, timeout=timeout))
            return resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise _AssetEndpointMissing(url)
            raise RuntimeError(f"资产上传失败: POST {url} 返回 {e.code}")
        except urllib.error.URLError as e:
            last_err = e
            if attempt < attempts - 1:
                await asyncio.sleep(2.0 * (attempt + 1))
    raise RuntimeError(
        f"资产上传失败: 多次重试后仍无法连接 {url} "
        f"({last_err.reason if last_err else 'unknown'})，请检查隧道带宽与节点状态"
    )


async def _upload_asset_chunked(asset_url: str, headers: dict, zip_bytes: bytes) -> None:
    """分块上传资产：把采样包拆成 ~1MB 小块独立 POST，服务端组装后校验 sha256。

    用户规约：画质优先、时长不敏感。单次整包上传在弱速临时隧道上极易整包失败，
    分块后单块失败只需重传该块 (约 1.4MB)，且小块在抖动链路上更易穿透。
    """
    import base64
    import hashlib
    import json
    import uuid

    upload_id = uuid.uuid4().hex
    total = (len(zip_bytes) + ASSET_UPLOAD_CHUNK_BYTES - 1) // ASSET_UPLOAD_CHUNK_BYTES
    zip_sha = hashlib.sha256(zip_bytes).hexdigest()
    chunk_url = f"{asset_url}/chunks"
    commit_url = f"{asset_url}/commit"

    for idx in range(total):
        chunk = zip_bytes[idx * ASSET_UPLOAD_CHUNK_BYTES:(idx + 1) * ASSET_UPLOAD_CHUNK_BYTES]
        payload = json.dumps({
            "upload_id": upload_id,
            "index": idx,
            "total": total,
            "data": base64.b64encode(chunk).decode("ascii"),
        }).encode("utf-8")
        # 块虽小，仍按载荷放宽写超时，覆盖慢速蠕动链路
        timeout = 120 + int(len(payload) / (0.05 * 1024 * 1024))
        await _post_asset(chunk_url, headers, payload, timeout)

    commit_payload = json.dumps({"upload_id": upload_id, "total": total, "sha256": zip_sha}).encode("utf-8")
    await _post_asset(commit_url, headers, commit_payload, 120)


async def _ensure_cloud_assets(conn: "SidecarConnection", asset_dir: Path) -> None:
    """异步同步主播资产到云端 sidecar：GET 摘要比对，一致则跳过；否则分块上传 (回退单次整包)。

    失败时如实抛出 RuntimeError（由上层 _render_cloud 透传给用户）。
    """
    import asyncio
    import base64
    import json
    import urllib.error
    import urllib.request
    from urllib.parse import urlparse

    avatar_id = str(conn.canonical.get("avatar_id") or "default")
    node_url = str(conn.node_url or "").strip()
    # 资产接口挂在源站根路径 /assets/{avatar_id}，不能沿用 WebSocket 路由 /ws/render-v3
    parsed = urlparse(node_url)
    http_scheme = "https" if parsed.scheme in ("wss", "https") else "http"
    http_url = f"{http_scheme}://{parsed.netloc}"
    asset_url = f"{http_url}/assets/{avatar_id}"
    headers = {}
    token = str(conn.auth_token or "")
    if token and "127.0.0.1" not in node_url and "localhost" not in node_url:
        headers["Authorization"] = f"Bearer {token}"
    # 自定义 UA：用户自有域名的 Cloudflare Bot Fight Mode 会封禁 Python-urllib 默认签名
    # (HTTP 403 + error code 1010)，导致资产同步全链路被拦；改用诚实的应用标识 UA 即可放行。
    headers["User-Agent"] = "AI-LiveStream-Agent/1.0"

    # 先按服务端存储口径构造采样包并计算摘要 (_cloud_asset_digest 与服务端解压后同口径)。
    # 对比旧实现对整个本地目录求摘要：云端只保存采样子集，旧摘要恒不匹配，每次都重传。
    zip_bytes, _ = _sample_cloud_asset_zip(asset_dir)
    local_digest = _cloud_asset_digest(zip_bytes)

    async def _do_request() -> tuple[int, dict | None]:
        loop = asyncio.get_event_loop()
        # trycloudflare 等临时隧道偶发 TLS EOF (UNEXPECTED_EOF_WHILE_READING)；
        # GET 摘要探活是幂等读，对这类瞬时连接错误做有限重试 (HTTPError 属确定性响应，不重试)
        last_err: urllib.error.URLError | None = None
        for attempt in range(3):
            req = urllib.request.Request(asset_url, headers=headers, method="GET")
            try:
                resp = await loop.run_in_executor(None, lambda: urllib.request.urlopen(req, timeout=30))
                body = json.loads(resp.read().decode("utf-8"))
                return resp.status, body
            except urllib.error.HTTPError as e:
                return e.code, None
            except urllib.error.URLError as e:
                last_err = e
                if attempt < 2:
                    await asyncio.sleep(1.0 * (attempt + 1))
        raise RuntimeError(
            f"云端资产同步失败: 无法连接 {asset_url} ({last_err.reason if last_err else 'unknown'})"
        )

    try:
        status, body = await _do_request()
        if status == 200 and isinstance(body, dict) and body.get("sha256") == local_digest:
            logger.info(f"云端资产已是最新: {avatar_id}")
            return

        # 优先分块上传 (弱网健壮)；服务端若无分块端点 (404) 则回退单次整包上传
        try:
            await _upload_asset_chunked(asset_url, headers, zip_bytes)
            logger.info(f"云端资产已同步 (分块): {avatar_id}")
            return
        except _AssetEndpointMissing:
            logger.info(f"云端 sidecar 不支持分块上传，回退单次整包上传: {avatar_id}")

        payload = json.dumps({"zip_base64": base64.b64encode(zip_bytes).decode("ascii")}).encode("utf-8")
        # 慢速穿透隧道 (实测 0.03~0.4 MB/s) 下大包单次 socket 写可能很久：
        # 用户规约：画质优先、时长不敏感 → 按 0.05MB/s 的保守速率 + 240s 基线放宽写超时，
        # 确保大包上传只在真正长时间停滞时才失败，而非慢速蠕动被误杀。
        post_timeout = 240 + int(len(payload) / (0.05 * 1024 * 1024))
        try:
            await _post_asset(asset_url, headers, payload, post_timeout)
            logger.info(f"云端资产已同步: {avatar_id}")
        except _AssetEndpointMissing:
            raise RuntimeError(
                "云端资产端点不存在 (404): 服务器未部署资产接口，"
                "请确认使用新版 Neural Sidecar (含 /assets/ 端点)；旧版部署请重新部署"
            )
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(f"云端资产同步异常: {e}")


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


def _cloud_field(cloud_gpu, key: str, default=None):
    """兼容 dict (ComputePlan.cloud_gpu 为 to_dict() 结果) 与 dataclass 两种形态读取字段"""
    if cloud_gpu is None:
        return default
    if isinstance(cloud_gpu, dict):
        return cloud_gpu.get(key, default)
    return getattr(cloud_gpu, key, default)


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
    policy_timeouts = canonical.get("avatar_provider", {}).get("timeouts", {}) if isinstance(canonical.get("avatar_provider"), dict) else {}
    raw_connect_ms = policy_timeouts.get("connect_ms")
    connect_s = float(raw_connect_ms) / 1000.0 if raw_connect_ms else 10.0
    if "127.0.0.1" not in conn.node_url and "localhost" not in conn.node_url:
        connect_s = max(connect_s, 8.0)
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
        connect_timeout=connect_s,
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

        return await evaluate_compute(
            feature_name="试播台词驱动",
            required_vram_gb=PREVIEW_REQUIRED_VRAM_GB,
        )

    async def _dispatch_render(self, plan, asset_dir, pcm, n_frames):
        """
        严格硬件门禁分流 (用户规约)：试播只允许真实神经渲染。

        - 云端 GPU：已对接且实测显存 >= 8GB → 优先云端 Sidecar 真实推理；
        - 本地 GPU：CUDA 可用且显存 >= 8GB → 本地 ONNX GPU 真实推理；
        - 两者皆不达标 → 直接抛 PreviewHardwareError 禁止试播；
        - 渲染失败 → 如实抛出原因，绝不做 CPU 推理或程序化口型假降级。
        """
        local_gpu = plan.local_gpu or {}
        local_ok = bool(
            local_gpu.get("cuda_available")
            and float(local_gpu.get("vram_total_gb") or 0) >= PREVIEW_REQUIRED_VRAM_GB
        )
        cloud = getattr(plan, "cloud_gpu", None)
        cloud_vram = float(_cloud_field(cloud, "vram_total_gb", 0.0) or 0.0)
        cloud_ok = bool(
            plan.use_cloud
            and plan.has_cloud_gpu
            and cloud
            and cloud.get("is_reachable")
            and cloud_vram >= PREVIEW_REQUIRED_VRAM_GB
        )

        problems: List[str] = []
        if cloud_ok:
            try:
                outcome = await self._render_cloud(cloud, asset_dir, pcm, n_frames)
                if outcome is not None and outcome.full_frames:
                    return outcome, ENGINE_CLOUD, "cloud", None
                problems.append("云端 GPU 渲染未产出任何帧")
            except Exception as e:
                logger.warning(f"云端试播渲染失败: {e}")
                problems.append(f"云端 GPU 渲染失败: {e}")
        if local_ok:
            outcome = await self._render_local(asset_dir, pcm, n_frames)
            if outcome is not None:
                # 云端失败自动回退本地 GPU 真实推理（同为真实神经渲染，非降级）
                return outcome, ENGINE_LOCAL, "local", "sidecar_unreachable" if problems else None
            problems.append(self._diagnose_local_failure())

        if problems:
            raise PreviewHardwareError(
                "试播真实神经渲染失败，已禁止试听：\n- " + "\n- ".join(problems)
                + "\n请检查云端 GPU 节点状态与鉴权配置 (【GPU算力配置】)，或改用本地 >=8GB 显存显卡后重试。"
            )

        local_name = str(local_gpu.get("gpu_name") or "未检测到独立显卡")
        local_vram = float(local_gpu.get("vram_total_gb") or 0.0)
        raise PreviewHardwareError(
            f"硬件不足，已禁止试听：试播需要真实神经渲染，要求本地 CUDA 显卡显存 >= 8GB，"
            f"或已对接显存 >= 8GB 的云端 GPU 节点。\n"
            f"当前本地显卡: {local_name} (显存 {local_vram}GB"
            + (f"，云端 GPU 未达门槛或未连通" if (plan.has_cloud_gpu or cloud) else "，且未对接云端 GPU")
            + ")。\n👉 请前往【GPU算力配置】对接云端 GPU (需 >=8GB 显存) 并完成鉴权，或升级本地显卡。"
        )

    # ------------------------------------------------------------------
    # 本地 ONNX GPU 推理
    # ------------------------------------------------------------------
    async def _render_local(
        self,
        asset_dir: Path,
        pcm: np.ndarray,
        n_frames: int,
    ) -> Optional[RenderOutcome]:
        from server.core.avatar.neural_model_manager import global_neural_model_manager

        if not global_neural_model_manager.is_model_available("onnx_lipsync"):
            logger.info("神经唇形模型未安装，本地试播渲染不可用")
            return None
        try:
            from server.core.cpu_worker import run_cpu_bound

            return await run_cpu_bound(_render_local_entry, asset_dir.as_posix(), pcm, n_frames)
        except Exception as e:
            logger.warning(f"本地试播渲染异常: {e}")
            return None

    def _diagnose_local_failure(self) -> str:
        from server.core.avatar.neural_model_manager import global_neural_model_manager

        if not global_neural_model_manager.is_model_available("onnx_lipsync"):
            return "本地神经唇形模型 (onnx_lipsync.onnx) 未下载，请前往【GPU算力配置】下载后再试"
        return "本地 ONNX 推理异常 (onnxruntime 加载或推理失败)"

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

        base_url = (_cloud_field(cloud_gpu, "base_url", "") or "").strip()
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
        """云端真实推理；失败时如实抛出原因（由上层门禁透传给用户），绝不静默吞异常。"""
        driver = None
        started = time.monotonic()
        try:
            conn = await self._resolve_sidecar_connection(cloud_gpu)
            if conn is None:
                raise RuntimeError(
                    "无法解析云端 GPU 节点连接参数：请在【GPU算力配置】检查节点地址与激活状态"
                )
            if not str(conn.auth_token or "").strip() and "127.0.0.1" not in conn.node_url and "localhost" not in conn.node_url:
                raise RuntimeError(
                    f"远程 sidecar 缺少鉴权 token：请在【GPU算力配置】为节点 {conn.node_url} "
                    "填写与云端服务一致的鉴权 token (或改用本地 SSH 隧道 ws://127.0.0.1 端点)"
                )

            # 资产同步：确保云端 sidecar 拥有主播资产
            await _ensure_cloud_assets(conn, asset_dir)

            frames = _build_audio_frame_batch(pcm)
            driver = _build_sidecar_driver(conn)

            collected: list[bytes] = []

            async def _collect(jpeg: bytes, owner=None) -> None:
                # 预览专用收集器：不接管虚拟摄像头/直播画面，仅缓存 JPEG
                if jpeg:
                    collected.append(jpeg)

            # 包裹实例方法：预览会话独占，不与直播管路共享连接
            driver._publish_video_frame = _collect  # noqa: SLF001

            await driver.start()

            await driver.feed_audio_frames(frames)
            # 时间线 consumer 按实时节奏发帧，等待其排空
            timeline_task = getattr(driver, "_timeline_task", None)
            if timeline_task is not None and not timeline_task.done():
                timeout = (len(pcm) / 16000.0) + 10.0
                await asyncio.wait_for(asyncio.shield(timeline_task), timeout=timeout)

            if not collected:
                raise RuntimeError(
                    "云端 GPU 已连接但未产出任何视频帧：请检查云端渲染服务日志与主播资产挂载状态"
                )
        finally:
            if driver is not None:
                try:
                    await driver.stop()
                except Exception as e:
                    logger.debug(f"关闭预览 sidecar 连接忽略: {e}")

        coords = _load_coords(asset_dir)
        outcome = RenderOutcome()
        outcome.device = str(
            (getattr(driver, "_selected_descriptor", {}) or {}).get("device")
            or _cloud_field(cloud_gpu, "gpu_name", "")
            or ""
        )
        outcome.providers = ["remote:neural_lipsync"]
        vram = _cloud_field(cloud_gpu, "vram_total_gb", None)
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
