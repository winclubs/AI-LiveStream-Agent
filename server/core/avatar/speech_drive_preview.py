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
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import cv2
import numpy as np

from server.config import DATA_DIR
from server.core.avatar.action_state_machine import mirror_index

logger = logging.getLogger("LiveAgent.SpeechDrivePreview")

FPS = 25
MAX_FRAMES = 300                 # 约 12 秒，与 sidecar 帧预算同量级
MAX_TEXT_LENGTH = 500
MEL_CONTEXT_SAMPLES = 3200       # ±200ms @16k，对齐 LatentSync 上下文窗口
SAMPLES_PER_FRAME_16K = int(16000 // FPS)  # 640 采样/帧 @16k 25fps，分段渲染切片粒度
# 分段渲染轮次上限：云端每轮 ~45s 安全窗口，12 轮远超 MAX_FRAMES=300 帧的试播预算，
# 仅作为异常兜底（节点每轮零进展时的终止条件），正常 300 帧 4~5 轮内完成
LATENTSYNC_MAX_BATCH_ROUNDS = 12

# 节点降级坏帧判据 (防御纵深)：云端在缺失人脸切片时输出灰底占位图 (210,220,240)，
# 其灰度均值为 224.8、纹理标准差 < 1.5；推理异常时也可能产出类似的「高亮无纹理」帧。
# 客户端必须识别并替换为主播底片，绝不让白屏出现在试播画面里。
DEGRADED_FRAME_MIN_MEAN = 190.0   # 灰度均值下限 (真实人脸底片约 125)
DEGRADED_FRAME_MAX_STD = 30.0     # 灰度标准差上限 (真实人脸底片约 54)


def _is_degraded_frame(img: np.ndarray) -> bool:
    """判断云端返回帧是否为降级坏帧（灰底/过曝白屏等无纹理画面）。"""
    try:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(np.float32)
    except Exception:
        return False
    return bool(
        float(gray.mean()) > DEGRADED_FRAME_MIN_MEAN
        and float(gray.std()) < DEGRADED_FRAME_MAX_STD
    )
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
    # 如实声明产物完整性的缺口（如「云端仅完成前 N/M 帧神经渲染，其余为底片」）。
    # 历史缺陷：分段截断后静默用底片补帧，用户误以为全片已完成神经口型渲染。
    fallback_reason: Optional[str] = None


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
    # True 表示本次直接复用了「同主播+同台词+同音色」的历史试播会话，未重新渲染
    reused: bool = False


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
    """按四舍五入换算帧数，保证帧序列完整覆盖音频时长。

    历史缺陷：用 floor 导致最多漏掉 39ms 尾音（25fps 下 2.03s 只渲染 50 帧=2.00s），
    封装时又靠 ffmpeg `-shortest` 砍流兜底，造成音画尾部错位。此处改用 round：
    帧覆盖时长与音频时长偏差 < 半个帧周期（20ms）。
    """
    return max(0, min(MAX_FRAMES, int(round(duration_sec * FPS))))


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
    visual_lead = int(os.environ.get("LIPSYNC_VISUAL_LEAD_SAMPLES", "640"))
    for i in range(n_frames):
        center = int(i * (16000.0 / FPS) + 320.0 + visual_lead)
        window = pcm[max(0, center - MEL_CONTEXT_SAMPLES): center + MEL_CONTEXT_SAMPLES]
        if window.size == 0:
            window = np.zeros(MEL_CONTEXT_SAMPLES * 2, dtype=np.float32)
        amp = float(np.sqrt(np.mean(np.square(window)))) if window.size else 0.0
        mouth_open = min(1.0, amp * 12.5)  # 静音门限信号：仅用于跳过无声帧推理
        t0 = time.perf_counter()
        out = renderer.render_lip_frame(
            full_imgs[mirror_index(i, n)], i, window, mouth_open, return_face=True
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
        # source.mp4 动辄数十至数百 MB，云端批处理渲染仅使用 face_imgs 切片与 coords 贴回；
        # 严禁将庞大的原视频强行打包上传，避免在弱网/跨境隧道上造成数十兆不必要的网络拥塞与超时。
        # 仅在视频极小 (<= 2MB) 或测试占位样本时作为元数据附带。
        source_mp4 = asset_dir / "source.mp4"
        if source_mp4.exists() and source_mp4.stat().st_size <= 2 * 1024 * 1024:
            zf.writestr("source.mp4", source_mp4.read_bytes())
        meta = {"sample_count": n, "original_face_count": len(face_files), "original_full_count": len(full_files), "has_source_video": source_mp4.exists()}
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

CHUNK_UPLOAD_ATTEMPTS = 3
CHUNK_UPLOAD_BACKOFF_BASE_SEC = 4.0
CHUNK_UPLOAD_BACKOFF_CAP_SEC = 30.0
# 退避抖动系数区间 [0.5, 1.5]：避免多客户端/多块同时重试形成尖峰
CHUNK_UPLOAD_BACKOFF_JITTER = (0.5, 1.5)
# 保守假定最低有效速率 0.05MB/s，用于按载荷放宽写超时（实测隧道可低至 0.03MB/s）
CHUNK_UPLOAD_ASSUMED_BPS = 0.05 * 1024 * 1024
CHUNK_UPLOAD_TIMEOUT_BASE_SEC = 120


class _AssetEndpointMissing(RuntimeError):
    """云端 sidecar 未部署该资产端点 (404)：供上层回退到兼容的上传方式"""


class CloudAssetSyncError(RuntimeError):
    """云端资产同步阶段失败 (带宽/链路/节点)，与「渲染失败」「鉴权失败」区分开。

    分阶段归类是刻意的：实测 9.8MB 资产在 ~6KB/s 的隧道上可能传 20 分钟仍失败，
    若混作「云端 GPU 渲染失败」并统一附上「请检查鉴权配置」，会把用户引向
    完全错误的方向（真正需要处理的是带宽，而不是 token）。
    """


def _cloud_upload_id(avatar_id: str, zip_sha256: str) -> str:
    """由 (avatar_id, zip 摘要) 确定化 upload_id。

    确定性是断点续传的前提：历史实现每次调用都用 uuid4 生成全新 upload_id，
    服务端 UPLOAD_CHUNKS 只在 commit 成功时清理，于是任一块失败都会让已传的
    分块全部作废，弱网隧道上 9.8MB/10 块几乎永远无法从头传完。改为确定化后，
    下次试听会命中服务端同一份 upload_id，可据服务端回传的 received 只补缺块。
    """
    import hashlib

    return hashlib.sha256(f"{avatar_id}:{zip_sha256}".encode("utf-8")).hexdigest()[:32]


def _chunk_post_timeout(payload_len: int) -> int:
    """按载荷放宽写超时：慢速蠕动链路只在真正长时间停滞时才失败，而非被误杀"""
    return CHUNK_UPLOAD_TIMEOUT_BASE_SEC + int(payload_len / CHUNK_UPLOAD_ASSUMED_BPS)


def _chunk_payload(upload_id: str, idx: int, total: int, chunk: bytes) -> bytes:
    import base64
    import json

    return json.dumps({
        "upload_id": upload_id,
        "index": idx,
        "total": total,
        "data": base64.b64encode(chunk).decode("ascii"),
    }).encode("utf-8")


def _parse_received(resp_body: bytes) -> Optional[int]:
    """解析分块端点回传的已收块数；无法解析时返回 None（退化为顺序全量上传）"""
    import json

    try:
        value = json.loads(resp_body.decode("utf-8")).get("received")
        return int(value) if value is not None else None
    except Exception:
        return None


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
            try:
                return resp.read()
            finally:
                # 必须显式关闭：否则 socket / TLS 会话滞留到 GC，服务端侧可能收到 broken pipe
                resp.close()
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


async def _upload_asset_chunked(
    asset_url: str,
    headers: dict,
    zip_bytes: bytes,
    *,
    avatar_id: str = "",
) -> None:
    """分块上传资产：把采样包拆成 ~1MB 小块独立 POST，服务端组装后校验 sha256。

    用户规约：画质优先、时长不敏感。单次整包上传在弱速临时隧道上极易整包失败，
    分块后单块失败只需重传该块 (约 1.4MB)，且小块在抖动链路上更易穿透。
    对单块写超时叠加指数退避重试：隧道被 Cloudflare 断链后往往需要数十秒恢复，
    固定 1.5s/3s 的短退避会在链路尚未恢复时耗尽全部重试次数。

    断点续传：upload_id 由 (avatar_id, zip sha256) 确定化，首块兼作断点探测
    (服务端按 index 幂等覆盖并回传 received)，据此只补传服务端尚缺的分块。
    """
    import hashlib
    import json
    import random
    import urllib.error
    import urllib.request

    zip_sha = hashlib.sha256(zip_bytes).hexdigest()
    upload_id = _cloud_upload_id(avatar_id, zip_sha)
    total = (len(zip_bytes) + ASSET_UPLOAD_CHUNK_BYTES - 1) // ASSET_UPLOAD_CHUNK_BYTES
    chunk_url = f"{asset_url}/chunks"
    commit_url = f"{asset_url}/commit"

    async def _post_chunk(idx: int, chunk: bytes) -> Optional[int]:
        """上传单块并返回服务端已收块数 (无法解析时 None)"""
        import asyncio

        payload = _chunk_payload(upload_id, idx, total, chunk)
        timeout = _chunk_post_timeout(len(payload))
        last_err: Exception | None = None
        for attempt in range(CHUNK_UPLOAD_ATTEMPTS):
            req = urllib.request.Request(chunk_url, headers=headers, data=payload, method="POST")
            req.add_header("Content-Type", "application/json")
            try:
                loop = asyncio.get_event_loop()
                resp = await loop.run_in_executor(None, lambda: urllib.request.urlopen(req, timeout=timeout))
                try:
                    body = resp.read()
                finally:
                    # 必须消费并关闭响应：丢弃返回值会持续泄漏 socket/TLS 会话
                    resp.close()
                return _parse_received(body)
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    raise _AssetEndpointMissing(chunk_url)
                raise CloudAssetSyncError(f"资产上传失败: POST {chunk_url} 返回 {e.code}")
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last_err = e
                if attempt < CHUNK_UPLOAD_ATTEMPTS - 1:
                    lo, hi = CHUNK_UPLOAD_BACKOFF_JITTER
                    delay = min(
                        CHUNK_UPLOAD_BACKOFF_CAP_SEC,
                        CHUNK_UPLOAD_BACKOFF_BASE_SEC * (2 ** attempt),
                    ) * (lo + (hi - lo) * random.random())
                    logger.warning(
                        f"云端资产 chunk {idx + 1}/{total} 第 {attempt + 1} 次失败 ({e})，"
                        f"{delay:.1f}s 后重试"
                    )
                    await asyncio.sleep(delay)
                    continue
                raise CloudAssetSyncError(
                    f"资产上传失败: chunk {idx + 1}/{total} 多次重试后仍无法连接 ({last_err})，"
                    f"请检查隧道带宽与节点状态"
                )
        return None

    started = time.monotonic()
    sent_bytes = 0
    # 首块兼作断点探测：服务端按 index 幂等覆盖，回传 received 告知已收块数
    first = zip_bytes[0:ASSET_UPLOAD_CHUNK_BYTES]
    next_idx = 1
    received = await _post_chunk(0, first)
    sent_bytes += len(first)
    if received is not None and 1 <= received <= total and received > 1:
        next_idx = received
        sent_bytes = min(len(zip_bytes), received * ASSET_UPLOAD_CHUNK_BYTES)
        logger.info(
            f"云端资产断点续传: {avatar_id} 服务端已有 {received}/{total} 块，"
            f"跳过前 {received} 块，仅补传 {total - received} 块"
        )

    for idx in range(next_idx, total):
        chunk = zip_bytes[idx * ASSET_UPLOAD_CHUNK_BYTES:(idx + 1) * ASSET_UPLOAD_CHUNK_BYTES]
        await _post_chunk(idx, chunk)
        sent_bytes += len(chunk)
        elapsed = max(1e-6, time.monotonic() - started)
        logger.info(
            f"云端资产上传进度: {avatar_id} {idx + 1}/{total} 块 "
            f"({sent_bytes / 1048576:.2f}/{len(zip_bytes) / 1048576:.2f} MB, "
            f"{sent_bytes / elapsed / 1024:.1f} KB/s)"
        )

    commit_payload = json.dumps({"upload_id": upload_id, "total": total, "sha256": zip_sha}).encode("utf-8")
    await _post_asset(commit_url, headers, commit_payload, CHUNK_UPLOAD_TIMEOUT_BASE_SEC)


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
                try:
                    body = json.loads(resp.read().decode("utf-8"))
                finally:
                    resp.close()
                return resp.status, body
            except urllib.error.HTTPError as e:
                return e.code, None
            except urllib.error.URLError as e:
                last_err = e
                if attempt < 2:
                    await asyncio.sleep(1.0 * (attempt + 1))
        raise CloudAssetSyncError(
            f"云端资产同步失败: 无法连接 {asset_url} ({last_err.reason if last_err else 'unknown'})"
        )

    try:
        status, body = await _do_request()
        if status == 200 and isinstance(body, dict) and body.get("sha256") == local_digest:
            logger.info(f"云端资产已是最新: {avatar_id}")
            return

        # 优先分块上传 (弱网健壮，支持断点续传)；服务端若无分块端点 (404) 则回退单次整包上传
        try:
            await _upload_asset_chunked(asset_url, headers, zip_bytes, avatar_id=avatar_id)
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
            raise CloudAssetSyncError(
                "云端资产端点不存在 (404): 服务器未部署资产接口，"
                "请确认使用新版 Neural Sidecar (含 /assets/ 端点)；旧版部署请重新部署"
            )
    except RuntimeError:
        raise
    except Exception as e:
        raise CloudAssetSyncError(f"云端资产同步异常: {e}")



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


def _conn_for_anchor(conn: "SidecarConnection", anchor_id: str) -> "SidecarConnection":
    """云端资产按主播隔离：canonical.avatar_id 缺省时历史上恒为 "default"，
    所有主播共用一个资产键，后上传者覆盖先上传者的切片，且每次切换主播都触发
    整包重传。强制以 anchor_id 作为云端资产键，杜绝互相覆盖。"""
    canonical = dict(conn.canonical or {})
    if canonical.get("avatar_id") == anchor_id:
        return conn
    canonical["avatar_id"] = anchor_id
    return SidecarConnection(
        node_url=conn.node_url,
        auth_token=conn.auth_token,
        canonical=canonical,
        model_name=conn.model_name,
    )


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
        # 试播只要 256x256 人脸帧：隧道下行约 123 KB/s，720x960 全图单帧 50KB，
        # 10 秒音频就是 12.7MB ≈ 101 秒，连接会在传输中途被掐断。
        preview_face_only=True,
    )


def _build_preview_mp4_cmd(frames_dir: Path, audio_path: Path, out_path: Path, ffmpeg_exe: str = "ffmpeg") -> list:
    """构造 preview.mp4 封装命令（帧序列 + 音频）。

    音画同步纪律（历史缺陷已修）：
    1. **禁用 `-shortest`**：该选项在最短流结束时直接截断另一路。帧序列与音频
       时长天然不等（25fps 下按 round 对齐仍有最多 ±20ms 偏差），用 `-shortest`
       会把长的那一路尾部砍掉，造成末尾音画错位。改为在渲染端用
       `_compute_frame_count` 的 round 对齐帧数，靠帧覆盖时长保证一致。
    2. **显式 `-fps_mode cfr`**：避免 muxer 按自身策略二次改写时间戳
       （image2 解复用器默认 framerate=25，但显式写出更安全）。
    """
    return [
        ffmpeg_exe, "-y",
        "-framerate", str(FPS),
        "-i", str(frames_dir / "%d.jpg"),
        "-i", str(audio_path),
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-tune", "zerolatency",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-fps_mode", "cfr",
        str(out_path),
    ]


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
        render_mode: str = "latentsync",
        cache_key: Optional[str] = None,
    ) -> SpeechDriveResult:
        anchor_id = str(anchor_id).strip()
        async with self._lock_for(anchor_id):
            pcm = _decode_pcm16k_mono(audio_bytes)
            duration = len(pcm) / 16000.0
            n_frames = _compute_frame_count(duration)
            if n_frames == 0:
                raise RuntimeError("音频时长过短，无可渲染帧")

            plan = await self._resolve_compute_plan()

            cloud = getattr(plan, "cloud_gpu", None)
            cloud_vram = float(_cloud_field(cloud, "vram_total_gb", 0.0) or 0.0)
            cloud_ok = bool(
                plan.use_cloud
                and plan.has_cloud_gpu
                and cloud
                and cloud.get("is_reachable")
                and cloud_vram >= PREVIEW_REQUIRED_VRAM_GB
            )

            # 全平台收敛：优先采用 ByteDance LatentSync 官方扩散模型批处理高精渲染
            should_use_latentsync = (render_mode == "latentsync") or (cloud_ok and render_mode != "local_only")

            if should_use_latentsync:
                if not cloud_ok:
                    raise PreviewHardwareError(
                        "硬件不足：【💎 官方 LatentSync 顶尖拟人口型】需要连接搭载 GPU (显存 >= 8GB) 的云端节点进行批处理扩散去噪渲染。\n"
                        "👉 请先在【GPU算力配置】中连接云端 GPU 节点（如 Google Colab / A100）并确认探活正常。"
                    )
                outcome = await self._render_cloud_batch_latentsync(
                    cloud, Path(asset_dir), pcm, anchor_id, n_frames
                )
                engine = "neural_cloud_latentsync"
                mode = "latentsync_batch"
                # 分段渲染仍存在缺口时如实透传（如「仅完成前 N/M 帧，其余为底片」），
                # 供前端状态条展示，绝不让用户误以为全片已完成神经口型渲染
                fallback_reason = outcome.fallback_reason
            else:
                outcome, engine, mode, fallback_reason = await self._dispatch_render(
                    plan, Path(asset_dir), pcm, n_frames
                )

            total_ms = float(np.sum(outcome.timings_ms)) if outcome.timings_ms else 0.0
            mean_ms = float(np.mean(outcome.timings_ms)) if outcome.timings_ms else 0.0
            frame_count = len(outcome.full_frames)
            # 试播结果缓存元数据：同「主播+台词+音色+资产版本」重复试听时直接复用整个会话，
            # 跳过 TTS 合成与数分钟的云端逐帧神经渲染
            meta = {
                "cache_key": cache_key,
                # 只有全片完成神经渲染的会话才允许被复用；含底片补帧的残缺会话必须重渲
                "complete": fallback_reason is None,
                "engine": engine,
                "mode": mode,
                "device": outcome.device,
                "providers": list(outcome.providers),
                "mean_inference_ms": round(mean_ms, 2),
                "total_inference_ms": round(total_ms, 2),
                "vram_total_gb": outcome.vram_total_gb,
                "fps": FPS,
                "frame_count": frame_count,
            }
            session_id = await self._persist(anchor_id, outcome, audio_bytes, meta=meta)
            self._cleanup_old_sessions(anchor_id, session_id)

            return SpeechDriveResult(
                engine=engine,
                mode=mode,
                device=outcome.device,
                providers=list(outcome.providers),
                mean_inference_ms=round(mean_ms, 2),
                total_inference_ms=round(total_ms, 2),
                vram_total_gb=outcome.vram_total_gb,
                fps=FPS,
                frame_count=frame_count,
                session_id=session_id,
                fallback_reason=fallback_reason,
            )

    def get_cached_result(self, anchor_id: str, cache_key: Optional[str]) -> Optional[SpeechDriveResult]:
        """查找可复用的历史试播会话：同「主播+台词+音色+资产版本」且全片完成神经渲染。

        命中时直接返回历史会话 (秒开)，跳过 TTS 与云端逐帧渲染；
        残缺会话 (含底片补帧) 与不完整落盘一律不命中，必须如实重渲。
        """
        import json

        if not cache_key:
            return None
        anchor_root = self._sessions_root / str(anchor_id).strip()
        if not anchor_root.is_dir():
            return None
        for sess in sorted((p for p in anchor_root.iterdir() if p.is_dir()), key=lambda p: p.name, reverse=True):
            meta_file = sess / "meta.json"
            if not meta_file.is_file():
                continue
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
            except Exception:
                continue
            if meta.get("cache_key") != cache_key or meta.get("complete") is not True:
                continue
            frame_count = int(meta.get("frame_count") or 0)
            if frame_count <= 0:
                continue
            # 轻量完整性校验：帧与音频必须真实落盘，避免缓存指向半途而废的目录
            if not (sess / "audio.mp3").is_file() or not (sess / "face" / "0.jpg").is_file():
                continue
            return SpeechDriveResult(
                engine=str(meta.get("engine") or ENGINE_CLOUD),
                mode=str(meta.get("mode") or "cloud"),
                device=str(meta.get("device") or ""),
                providers=list(meta.get("providers") or []),
                mean_inference_ms=float(meta.get("mean_inference_ms") or 0.0),
                total_inference_ms=float(meta.get("total_inference_ms") or 0.0),
                vram_total_gb=(float(meta["vram_total_gb"]) if meta.get("vram_total_gb") is not None else None),
                fps=int(meta.get("fps") or FPS),
                frame_count=frame_count,
                session_id=sess.name,
                fallback_reason=None,
                reused=True,
            )
        return None

    async def _post_latentsync_batch(
        self,
        batch_url: str,
        headers: dict,
        seg_pcm: np.ndarray,
        face_imgs_b64: List[str],
        anchor_id: str,
    ) -> List[str]:
        """单次调用云端 /render/batch：发送一段 16k PCM，返回该段逐帧重绘的 base64 帧列表。

        云端节点位于 Cloudflare 等代理之后（100s 源站硬超时），其内部设有 ~45s
        渲染安全窗口：段内渲染不完时提前封包，HTTP 200 但只携带已完成的帧。
        调用方必须按实际返回帧数做分段补全，绝不能假设「一次请求 = 全部帧」。
        """
        import base64
        import io
        import json
        import wave

        import aiohttp

        # 将该段精准 PCM 封装为标准 16-bit 单声道 WAV 容器
        # 彻底杜绝 TTS 返回 MP3 时云端误将其作为裸 PCM 强行解码、丢帧停顿与噪声乱码口型缺陷
        wav_buf = io.BytesIO()
        with wave.open(wav_buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            int16_pcm = np.clip(seg_pcm * 32767.0, -32768.0, 32767.0).astype(np.int16)
            wf.writeframes(int16_pcm.tobytes())

        payload = {
            "request_id": f"preview_{int(time.time() * 1000)}",
            "audio_id": f"audio_{int(time.time() * 1000)}",
            "avatar_id": anchor_id,
            "audio_b64": base64.b64encode(wav_buf.getvalue()).decode("ascii"),
            "face_imgs_b64": face_imgs_b64,
            "guidance_scale": 1.0,
            "num_inference_steps": 20,
            "seed": 1247,
        }

        timeout = aiohttp.ClientTimeout(total=180.0, connect=10.0)
        async with aiohttp.ClientSession(timeout=timeout, auto_decompress=False) as session:
            try:
                async with session.post(batch_url, json=payload, headers=headers) as resp:
                    if resp.status == 404:
                        raise PreviewHardwareError(
                            "云端节点尚未部署或更新 LatentSync 批处理端点 (/render/batch 返回 404)。\n"
                            "👉 请使用最新的 google_gpu.md / intern_gpu.md 重新启动云端渲染节点，或切换为【⚡ 极速模式】体验。"
                        )
                    if resp.status in (401, 403):
                        raise PreviewHardwareError(f"云端节点鉴权失败 (HTTP {resp.status})，请在【GPU算力配置】核对 Token。")
                    raw_body = await resp.read()
                    encoding = (resp.headers.get("Content-Encoding") or "").lower().strip()
                    try:
                        if encoding == "br":
                            import brotli
                            body_bytes = brotli.decompress(raw_body)
                        elif encoding in ("gzip", "deflate"):
                            import gzip
                            try:
                                body_bytes = gzip.decompress(raw_body)
                            except Exception:
                                import zlib
                                body_bytes = zlib.decompress(raw_body)
                        else:
                            body_bytes = raw_body
                    except Exception as e:
                        logger.warning(f"解压响应载荷失败 ({encoding}): {e}")
                        body_bytes = raw_body

                    if resp.status != 200:
                        err_text = body_bytes.decode("utf-8", errors="ignore")
                        raise PreviewHardwareError(f"云端批处理渲染失败 HTTP {resp.status}: {err_text[:200]}")
                    try:
                        data = json.loads(body_bytes.decode("utf-8"))
                    except Exception as e:
                        raise PreviewHardwareError(f"解析云端批处理渲染响应失败: {e}")
            except aiohttp.ClientError as e:
                raise PreviewHardwareError(f"连接云端批处理渲染节点失败 ({batch_url}): {e}")
        return list(data.get("frames", []))

    async def _render_cloud_batch_latentsync(
        self,
        cloud_gpu,
        asset_dir: Path,
        pcm: np.ndarray,
        anchor_id: str,
        n_frames: int,
    ) -> RenderOutcome:
        """调用云端 /render/batch 批处理端点，获取 LatentSync 逐帧高精重绘切片，并与本地底片无缝融合。

        分段补全（真实故障修复）：云端节点受代理 100s 超时约束，内部设 ~45s 渲染
        安全窗口，单次请求只返回窗口内完成的帧（实测 ~80 帧 ≈ 3.2 秒）。历史实现
        把「一次请求」当「全部帧」用，剩余帧被静默补上无口型的主播底片——用户看到的
        正是「第一句话有口型，第二句开始闭口」。现在按每轮实际返回帧数切分音频，
        从未完成帧号对应的音频位置继续请求，直至全部帧完成真实神经渲染。
        """
        import base64
        from urllib.parse import urlparse

        from server.core.avatar.neural_lip_renderer import NeuralLipRenderer

        conn = await self._resolve_sidecar_connection(cloud_gpu)
        if conn is None:
            raise PreviewHardwareError("无法解析云端 GPU 节点连接参数：请在【GPU算力配置】检查节点地址与激活状态")
        # 云端资产按主播隔离：历史实现所有主播共用 "default" 资产键，互相覆盖
        conn = _conn_for_anchor(conn, anchor_id)

        # 确保云端已持有该主播资产
        await _ensure_cloud_assets(conn, asset_dir)

        node_url = str(conn.node_url or "").strip()
        parsed = urlparse(node_url)
        http_scheme = "https" if parsed.scheme in ("wss", "https") else "http"
        http_url = f"{http_scheme}://{parsed.netloc}"
        batch_url = f"{http_url}/render/batch"

        headers = {
            "Content-Type": "application/json",
            "User-Agent": "AI-LiveStream-Agent/1.0",
            "Accept-Encoding": "gzip, deflate",
        }
        token = str(conn.auth_token or "").strip()
        if token and "127.0.0.1" not in node_url and "localhost" not in node_url:
            headers["Authorization"] = f"Bearer {token}"

        # 提取本地真实主播切片（保持真实 25fps 自然连续时序，严禁大跨度跳帧采样导致眨眼/头动被快进数十倍）
        local_faces = _load_face_imgs(asset_dir)
        face_imgs_b64: list[str] = []
        if local_faces:
            # 截取前 50 帧连续自然帧（约 2 秒稳定真实动作，保持 1:1 自然物理时间轴，杜绝抽动跳跃）
            max_backup = min(50, len(local_faces))
            for f in local_faces[:max_backup]:
                _, buf = cv2.imencode(".jpg", f, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
                face_imgs_b64.append(base64.b64encode(buf.tobytes()).decode("ascii"))

        full_imgs = _load_full_imgs(asset_dir)
        coords = _load_coords(asset_dir)
        n_full = len(full_imgs)
        n_coords = len(coords)
        n_faces = len(local_faces)

        outcome = RenderOutcome()
        outcome.device = f"{_cloud_field(cloud_gpu, 'gpu_name', '') or 'Cloud GPU'} (LatentSync/Diffusion)"
        outcome.providers = ["cloud:latentsync_unet3d"]
        vram = _cloud_field(cloud_gpu, "vram_total_gb", None)
        outcome.vram_total_gb = round(float(vram), 2) if vram else None

        started_at = time.monotonic()
        total_samples = min(len(pcm), n_frames * SAMPLES_PER_FRAME_16K)
        seg_start = 0
        rounds = 0
        degraded_count = 0
        while seg_start < n_frames and rounds < LATENTSYNC_MAX_BATCH_ROUNDS:
            rounds += 1
            # 从未完成帧号对应的音频切片继续请求：云端对该段从头渲染，帧序与全局帧号天然衔接
            seg_pcm = pcm[seg_start * SAMPLES_PER_FRAME_16K : total_samples]
            if seg_pcm.size == 0:
                break
            # 每轮都必须随行携带本地切片兜底 (face_imgs_b64)：
            # 云端节点的历史缺陷 load_face_imgs() 漏 return，资产目录里的切片被加载后丢弃，
            # 导致 `face_imgs` 恒为 None → 无切片可用时节点直接落到灰底占位图 (210,220,240)
            # → 整段渲染成「白屏」(实测灰度均值 224.8)。在节点修复并重新部署前，
            # 任何分段都不得省略该兜底。
            frames_b64 = await self._post_latentsync_batch(
                batch_url, headers, seg_pcm, face_imgs_b64, anchor_id
            )
            if not frames_b64:
                if seg_start == 0:
                    raise PreviewHardwareError("云端 LatentSync 批处理已完成但未返回有效画面帧")
                break  # 后续轮次空返回：保留已完成帧，如实标注缺口后退出
            take = frames_b64[: n_frames - seg_start]
            for item in take:
                global_idx = seg_start
                seg_start += 1
                try:
                    jpeg_bytes = base64.b64decode(item)
                    arr = cv2.imdecode(np.frombuffer(jpeg_bytes, np.uint8), cv2.IMREAD_COLOR)
                    if arr is None:
                        raise ValueError("JPEG 解码为空")
                    face256 = arr if arr.shape[:2] == (256, 256) else cv2.resize(arr, (256, 256))

                    # 智能底模融合保护：若远端因未载入切片返回灰色/单色纯色画面，自动无缝融合本地真人切片底模
                    if n_faces > 0:
                        corner_std = float(np.std(np.asarray(face256[:50, :50], dtype=np.float32)))
                        if corner_std < 5.0:  # 额头/面部边缘几乎无真实皮肤纹理（灰底或纯色）
                            ref_face = local_faces[mirror_index(global_idx, n_faces)].copy()
                            h, w = ref_face.shape[:2]
                            # 仅将远端渲染的下半部高精唇部区域平滑羽化贴至真人脸切片
                            mask = np.zeros((h, w), dtype=np.float32)
                            cv2.ellipse(mask, (int(w * 0.5), int(h * 0.72)), (int(w * 0.28), int(h * 0.20)), 0, 0, 360, 1.0, -1)
                            mask = cv2.GaussianBlur(mask, (21, 21), 7.0)[:, :, np.newaxis]
                            face256 = (face256.astype(np.float32) * mask + ref_face.astype(np.float32) * (1.0 - mask)).astype(np.uint8)

                    # 白屏/降级坏帧保护：节点缺失切片时会输出灰底占位图 (210,220,240)
                    # (灰度均值 224.8、纹理 std < 1.5)，旧版灰底融合保护只查额头 50x50 角标，
                    # 对「整幅灰底」无效——实测导致末尾整段白屏。此处整幅判别并回退主播底片。
                    if _is_degraded_frame(face256):
                        degraded_count += 1
                        if n_faces > 0:
                            logger.warning(
                                f"云端返回降级坏帧 (灰底/白屏) @帧 {global_idx}，已回退主播底片"
                            )
                            face256 = local_faces[mirror_index(global_idx, n_faces)].copy()

                    outcome.face_frames.append(face256)

                    if n_full > 0 and n_coords > 0:
                        base_full = full_imgs[mirror_index(global_idx, n_full)].copy()
                        box = coords[mirror_index(global_idx, n_coords)]
                        blended = NeuralLipRenderer._blend_back(base_full, face256, box)
                        outcome.full_frames.append(blended)
                    else:
                        outcome.full_frames.append(face256)
                except Exception as e:
                    # 单帧解码失败也必须占用全局帧号，否则后续帧整体前移造成音画错位；
                    # 该帧回退主播底片，保持时长与音频严格对齐
                    logger.warning(f"LatentSync 帧 {global_idx} 解码失败，回退主播底片: {e}")
                    if n_faces > 0:
                        ref_face = local_faces[mirror_index(global_idx, n_faces)].copy()
                        outcome.face_frames.append(ref_face)
                        if n_full > 0 and n_coords > 0:
                            base_full = full_imgs[mirror_index(global_idx, n_full)].copy()
                            box = coords[mirror_index(global_idx, n_coords)]
                            outcome.full_frames.append(NeuralLipRenderer._blend_back(base_full, ref_face, box))
                        else:
                            outcome.full_frames.append(ref_face)
                    # 无底片可用时只能丢帧（资产目录已在上游端点校验存在，此分支实际不可达）
            logger.info(
                f"LatentSync 分段渲染: 第 {rounds} 轮返回 {len(take)} 帧，"
                f"神经渲染累计 {seg_start}/{n_frames} 帧"
            )

        total_time_ms = (time.monotonic() - started_at) * 1000.0

        # 播放时长连续性严格保障：若多轮分段后仍未完成全部帧（节点停滞/轮次耗尽），
        # 绝不复制静态末帧导致画面死板定格，而是衔接真人底模的自然时序物理运动
        # （呼吸/微动/眨眼）——但必须如实告知用户，绝不允许静默补帧。
        real_count = len(outcome.full_frames)
        # 白屏/降级坏帧也属产物缺口：已回退底片保证画面可用，但必须如实告知，
        # 并提示根因 (云端节点未载入主播切片，旧版 load_face_imgs 漏 return)
        if degraded_count > 0 and outcome.fallback_reason is None:
            outcome.fallback_reason = (
                f"云端有 {degraded_count}/{n_frames} 帧渲染异常（灰底/白屏），"
                "已回退主播底片显示；根因多为云端节点未载入主播切片，"
                "请用最新版 google_gpu.md / intern_gpu.md 重新部署节点"
            )
            logger.warning(f"试播存在降级坏帧: {outcome.fallback_reason}")

        if real_count < n_frames:
            for pad_idx in range(real_count, n_frames):
                if n_full > 0:
                    outcome.full_frames.append(full_imgs[mirror_index(pad_idx, n_full)].copy())
                elif outcome.full_frames:
                    outcome.full_frames.append(outcome.full_frames[-1].copy())

                if n_faces > 0:
                    outcome.face_frames.append(local_faces[mirror_index(pad_idx, n_faces)].copy())
                elif outcome.face_frames:
                    outcome.face_frames.append(outcome.face_frames[-1].copy())

            outcome.fallback_reason = (
                f"云端节点在代理安全窗口内仅完成前 {real_count}/{n_frames} 帧神经渲染，"
                "其余帧为主播底片（无口型）；可缩短台词分句试听，或提升云端节点算力/带宽后重试"
            )
            logger.warning(f"试播分段渲染未全部完成: {outcome.fallback_reason}")

        per_frame_ms = total_time_ms / max(1, len(outcome.full_frames))
        outcome.timings_ms = [round(per_frame_ms, 2)] * len(outcome.full_frames)

        if not outcome.full_frames:
            raise PreviewHardwareError("LatentSync 输出帧解码全部失败")

        return outcome

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
        # 显存必须来自实测探活 (gpu_capability 由 sidecar 握手 device 字段解析)。
        # 历史缺陷：曾按「只要可连通就当作 8GB 显存」放行，使纯 CPU 节点冒充 >=8GB
        # 真实 GPU 节点进入渲染分支——既违背用户「试播只允许 >=8GB 显存真实神经渲染」
        # 的硬性规约，也让用户拿到一段耗时极长的 CPU 推理而非快速诚实的拒绝。
        cloud_vram = float(_cloud_field(cloud, "vram_total_gb", 0.0) or 0.0)
        cloud_ok = bool(
            plan.use_cloud
            and plan.has_cloud_gpu
            and cloud
            and cloud.get("is_reachable")
            and cloud_vram >= PREVIEW_REQUIRED_VRAM_GB
        )

        problems: List[str] = []
        # 分阶段归类：资产同步(带宽/链路) 与 渲染/鉴权 是两类完全不同的根因，
        # 提示语必须分别给出，否则会把用户从带宽问题引向鉴权排查。
        hints: List[str] = []
        if cloud_ok:
            from server.adapters.media.neural_sidecar_driver import (
                SidecarBackendNotReadyError as _NodeNotReady,
            )

            try:
                outcome = await self._render_cloud(cloud, asset_dir, pcm, n_frames)
                if outcome is not None and outcome.full_frames:
                    return outcome, ENGINE_CLOUD, "cloud", None
                problems.append("云端 GPU 渲染未产出任何帧")
            except CloudAssetSyncError as e:
                logger.warning(f"云端资产同步失败: {e}")
                problems.append(f"云端资产同步失败: {e}")
                hints.append(
                    "👉 这是「主播资产上传到云端」的带宽/链路问题，不是鉴权问题："
                    "资产包约 10MB，弱速隧道上传需要十几分钟甚至更久。"
                    "已启用断点续传，再次点击会只补传云端缺失的分块，"
                    "请保持云端节点在线并重试；长期方案是改善隧道带宽或改用本地 >=8GB 显存显卡。"
                )
            except _NodeNotReady as e:
                logger.warning(f"云端节点后端未就绪: {e}")
                problems.append(f"云端节点后端未就绪: {e}")
                hints.append(
                    "👉 鉴权已通过，节点握手也声明可用，是**节点自身**的真实推理后端未就绪。"
                    "最常见原因是节点装了 CPU 版 onnxruntime：nvidia-smi 能看到 Tesla T4，"
                    "但 InferenceSession 落到 CPUExecutionProvider，节点遂判定 available=false。"
                    "请在云端节点上确认 `session.get_providers()` 含 CUDAExecutionProvider"
                    "（安装 onnxruntime-gpu 并卸载 CPU 版），并查看节点自身日志。"
                )
            except Exception as e:
                logger.warning(f"云端试播渲染失败: {e}")
                problems.append(f"云端 GPU 渲染失败: {e}")
                hints.append("请检查云端 GPU 节点状态与鉴权配置 (【GPU算力配置】)，或改用本地 >=8GB 显存显卡后重试。")
        if local_ok:
            outcome = await self._render_local(asset_dir, pcm, n_frames)
            if outcome is not None:
                # 云端失败自动回退本地 GPU 真实推理（同为真实神经渲染，非降级）
                return outcome, ENGINE_LOCAL, "local", "sidecar_unreachable" if problems else None
            problems.append(self._diagnose_local_failure())

        if problems:
            raise PreviewHardwareError(
                "试播真实神经渲染失败，已禁止试听：\n- " + "\n- ".join(problems)
                + "\n" + "\n".join(hints)
            )

        local_name = str(local_gpu.get("gpu_name") or "未检测到独立显卡")
        local_vram = float(local_gpu.get("vram_total_gb") or 0.0)
        raise PreviewHardwareError(
            f"硬件不足，已禁止试听：试播需要真实神经渲染，要求本地 CUDA 显卡显存 >= 8GB，"
            f"或已对接显存 >= 8GB 的云端 GPU 节点。\n"
            f"当前本地显卡: {local_name} (显存 {local_vram}GB"
            + ("，云端 GPU 未达门槛或未连通" if (plan.has_cloud_gpu or cloud) else "，且未对接云端 GPU")
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
            # 试播只是收集帧序列供事后统一播放，不需要按墙钟实时推进音画同步。
            # 沿用直播的实时节奏会把收集拖成实时播放：节点渲染 + 隧道传输一旦
            # 慢于音频时长，时间线就撞上 deadline 超时退出，进而误报
            # 「视频时间线 consumer 已提前终止」。
            driver.realtime_pacing = False

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
        # 试播请求了 preview_face_only：节点输出的就是 256x256 人脸帧，
        # 不能再按原图 coords 二次裁剪（否则得到错误的画面）。
        face_only = bool(getattr(driver, "preview_face_only", False))
        for i, jpeg in enumerate(collected):
            arr = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
            if arr is None:
                continue
            if face_only:
                face = arr if arr.shape[:2] == (256, 256) else cv2.resize(
                    arr, (256, 256), interpolation=cv2.INTER_AREA
                )
                outcome.face_frames.append(face)
                outcome.full_frames.append(face)
                outcome.timings_ms.append(round(per_frame, 2))
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
    async def _persist(
        self,
        anchor_id: str,
        outcome: RenderOutcome,
        audio_bytes: bytes,
        meta: Optional[dict] = None,
    ) -> str:
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

            if meta:
                import json

                try:
                    (base / "meta.json").write_text(
                        json.dumps(meta, ensure_ascii=False), encoding="utf-8"
                    )
                except Exception as e:
                    logger.debug(f"写入试播缓存元数据忽略: {e}")

            # 🚀 工业级标准：自动合成 preview.mp4 供前端原生硬件加速超丝滑播放
            import subprocess
            import shutil
            ffmpeg_exe = shutil.which("ffmpeg")
            if ffmpeg_exe and len(outcome.full_frames) > 0:
                try:
                    subprocess.run(
                        _build_preview_mp4_cmd(
                            base / "full", base / "audio.mp3", base / "preview.mp4", ffmpeg_exe
                        ),
                        capture_output=True,
                        timeout=15,
                    )
                except Exception as e:
                    logger.warning(f"合成 preview.mp4 异常: {e}")

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
