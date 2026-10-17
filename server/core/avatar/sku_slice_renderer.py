# -*- coding: utf-8 -*-
"""
高精切片离线渲染工厂与音频断层消除引擎 (SkuSliceRenderer)
适用阶段：7x24小时无人值守直播带货「动静分离 / 批播结合」架构。

核心职责：
1. 话术台词 -> TTS 语音合成驱动；
2. 响度归一化 (-16.0 LUFS EBU R128 标准基线) 与软限幅防爆音保护；
3. 声学底噪特征提取 (extract_noise_profile) 与舒适底噪垫声 (generate_comfort_noise)；
4. 调度 ByteDance LatentSync 官方扩散模型 (UNet3D) 云端/本地批处理进行高质量唇形重绘；
5. 切片视频/音频落盘并持久化注册到 ecommerce_sku_slices 数据库表。
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import random
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import cv2
import numpy as np
from sqlalchemy import select

from server.config import DATA_DIR
from server.core.audio.tts_preview_service import PreviewSpeechParams, synthesize_preview_audio
from server.database.db import AsyncSessionLocal
from server.database.models import Anchor, Product, SkuSlice, VoiceProfile

logger = logging.getLogger("LiveAgent.SkuSliceRenderer")

SKU_SLICES_DIR = DATA_DIR / "sku_slices"
SKU_SLICES_DIR.mkdir(parents=True, exist_ok=True)

SAMPLE_RATE_16K = 16000
TARGET_LOUDNESS_LUFS = -16.0

PLACEHOLDER_ENGINE = "placeholder_loop"
LATENTSYNC_CLOUD_ENGINE = "latentsync_cloud"


# ----------------------------------------------------------------------
# 1. 声学工程：响度对齐与底噪特征消除断层
# ----------------------------------------------------------------------
def estimate_loudness_lufs(pcm: np.ndarray) -> float:
    """
    估算单声道 float32 PCM 音频的等效综合响度 (LUFS)
    基于 EBU R128 标准 RMS 近似模型
    """
    if pcm is None or pcm.size == 0:
        return -70.0
    rms = float(np.sqrt(np.mean(np.square(pcm.astype(np.float32)))))
    if rms < 1e-6:
        return -70.0
    lufs = 20.0 * math.log10(rms) - 0.691
    return float(max(-70.0, min(0.0, lufs)))


def normalize_loudness_lufs(
    pcm: np.ndarray,
    target_lufs: float = TARGET_LOUDNESS_LUFS,
) -> Tuple[np.ndarray, float]:
    """
    将音频信号增益规整至目标 LUFS，并施加软限幅 (Soft-Clipping) 防止爆音削顶。
    :return: (normalized_pcm, measured_lufs)
    """
    if pcm is None or pcm.size == 0:
        return np.zeros(1, dtype=np.float32), -70.0

    current_lufs = estimate_loudness_lufs(pcm)
    if current_lufs <= -69.0:
        return pcm.astype(np.float32), current_lufs

    delta_db = target_lufs - current_lufs
    gain = math.pow(10.0, delta_db / 20.0)

    # 应用增益
    scaled = pcm.astype(np.float32) * gain

    # 软限幅防止数字削顶破音
    peak = float(np.max(np.abs(scaled))) if scaled.size else 0.0
    if peak > 0.95:
        # 使用 tanh 软压限
        scaled = np.tanh(scaled * (0.95 / max(0.95, peak)))

    final_lufs = estimate_loudness_lufs(scaled)
    return scaled.astype(np.float32), round(final_lufs, 2)


def extract_noise_profile(
    pcm: np.ndarray,
    sample_rate: int = SAMPLE_RATE_16K,
    frame_ms: int = 20,
) -> Dict[str, Any]:
    """
    从音频间歇中提取背景环境底噪特征 (Noise Floor Profile)
    用于弹幕插播时动态混入匹配的舒适噪声，消除静音断层违和感。
    """
    if pcm is None or pcm.size == 0:
        return {"noise_floor_db": -60.0, "spectral_centroid_hz": 400.0, "variance": 0.0}

    frame_size = int(sample_rate * frame_ms / 1000.0)
    if frame_size <= 0 or len(pcm) < frame_size:
        return {"noise_floor_db": -60.0, "spectral_centroid_hz": 400.0, "variance": 0.0}

    n_frames = len(pcm) // frame_size
    frame_energies = []
    for i in range(n_frames):
        chunk = pcm[i * frame_size : (i + 1) * frame_size]
        rms = float(np.sqrt(np.mean(np.square(chunk))))
        frame_energies.append(rms)

    # 选取能量最低的 15% 帧作为底噪样本
    sorted_energies = sorted(frame_energies)
    noise_count = max(1, int(len(sorted_energies) * 0.15))
    lowest_energies = sorted_energies[:noise_count]

    avg_noise_rms = float(np.mean(lowest_energies))
    avg_noise_rms = max(1e-6, avg_noise_rms)
    noise_floor_db = 20.0 * math.log10(avg_noise_rms)
    variance = float(np.var(lowest_energies))

    return {
        "noise_floor_db": round(float(max(-75.0, min(-20.0, noise_floor_db))), 2),
        "spectral_centroid_hz": 420.0,
        "variance": round(float(variance), 8),
    }


def generate_comfort_noise(
    length_samples: int,
    noise_profile: Optional[Dict[str, Any]] = None,
) -> np.ndarray:
    """
    根据底噪特征生成舒适背景微噪声 (Comfort Noise)
    """
    if length_samples <= 0:
        return np.zeros(0, dtype=np.float32)

    profile = noise_profile or {}
    floor_db = float(profile.get("noise_floor_db", -55.0))
    target_rms = math.pow(10.0, floor_db / 20.0)

    # 生成滤波白噪声
    noise = np.random.normal(0.0, 1.0, length_samples).astype(np.float32)
    # 极轻量一阶低通平滑
    noise = np.convolve(noise, [0.25, 0.5, 0.25], mode="same")
    curr_rms = float(np.sqrt(np.mean(np.square(noise)))) or 1.0
    scaled_noise = noise * (target_rms / curr_rms)
    return scaled_noise.astype(np.float32)


def apply_audio_varispeed(
    pcm_bytes: bytes,
    sample_rate: int = SAMPLE_RATE_16K,
    speed_ratio: float = 1.0,
) -> bytes:
    """
    音频微变速算法 (±1.5% Varispeed / Time-Stretch)
    用于阶段五听觉防查防录播：通过轻微拉伸/压缩音频时长破坏全局频谱周期指纹。
    :param pcm_bytes: 原始 16-bit 单声道 PCM 字节流
    :param sample_rate: 采样率 (默认 16000)
    :param speed_ratio: 变速比率 (建议 0.985 ~ 1.015，默认 1.0 即不变)
    :return: 变速后的 16-bit 单声道 PCM 字节流
    """
    if not pcm_bytes or len(pcm_bytes) < 4 or abs(speed_ratio - 1.0) < 1e-4:
        return pcm_bytes

    # 截断奇数字节保证 16-bit (2 bytes) 对齐
    valid_len = len(pcm_bytes) - (len(pcm_bytes) % 2)
    aligned_bytes = pcm_bytes[:valid_len]

    audio_arr = np.frombuffer(aligned_bytes, dtype=np.int16).astype(np.float32) / 32768.0
    if len(audio_arr) < 100:
        return pcm_bytes

    # 限制微变速在安全范围 [0.97, 1.03] 内，防止产生人耳可辨的突兀语速异变
    clamped_ratio = max(0.97, min(1.03, float(speed_ratio)))
    orig_len = len(audio_arr)
    target_len = int(round(orig_len / clamped_ratio))
    if target_len <= 0:
        return pcm_bytes

    # 使用线性插值时域拉伸
    orig_indices = np.linspace(0, orig_len - 1, orig_len)
    target_indices = np.linspace(0, orig_len - 1, target_len)
    stretched = np.interp(target_indices, orig_indices, audio_arr)

    # 软限幅防止溢出并转回 int16
    stretched = np.tanh(stretched)
    out_int16 = (np.clip(stretched, -1.0, 1.0) * 32767.0).astype(np.int16)
    return out_int16.tobytes()


# ----------------------------------------------------------------------
# 2. 高精切片离线渲染抽象引擎
# ----------------------------------------------------------------------
class SkuSliceRenderer:
    """电商商品切片预渲染调度器"""

    def __init__(self, storage_dir: Optional[Path] = None) -> None:
        self.storage_dir = Path(storage_dir or SKU_SLICES_DIR)
        self.storage_dir.mkdir(parents=True, exist_ok=True)

    async def render_single_slice(
        self,
        product_id: str,
        slot_code: str,
        script_text: str,
        anchor_id: str,
        voice_profile_id: Optional[str] = None,
        slice_type: str = "main_pitch",
        force_engine: Optional[str] = None,
        enable_varispeed: bool = True,
    ) -> SkuSlice:
        """
        全闭环渲染单个 SKU 切片并持久化落库
        :param product_id: 关联商品 ID
        :param slot_code: 切片插槽代号 (intro / selling_point_1 / coupon / faq_size / urgency)
        :param script_text: 对应商品脚本台词
        :param anchor_id: 主播资产 ID
        :param voice_profile_id: 音色档案 ID (可选，默认使用主播绑定音色)
        :param slice_type: 切片类型
        :param force_engine: 强制指定渲染引擎 (测试或特殊配置时使用)
        :param enable_varispeed: 是否启用 ±1.5% 随机微变速 (破坏周期声学指纹)
        """
        slice_id = f"slice_{product_id}_{slot_code}_{uuid.uuid4().hex[:8]}"
        slice_work_dir = self.storage_dir / slice_id
        slice_work_dir.mkdir(parents=True, exist_ok=True)

        logger.info(f"[SkuSliceRenderer] 开始渲染切片 {slice_id} (SKU={product_id}, slot={slot_code})")

        # 1. 解析主播与音色信息
        async with AsyncSessionLocal() as session:
            anchor_res = await session.execute(select(Anchor).where(Anchor.id == anchor_id))
            anchor_obj = anchor_res.scalar_one_or_none()
            if not anchor_obj:
                raise ValueError(f"主播不存在: {anchor_id}")

            effective_voice_id = voice_profile_id or anchor_obj.voice_id
            voice_name = "zh-CN-XiaoxiaoNeural"
            provider_name = "edge_tts"
            if effective_voice_id:
                voice_res = await session.execute(
                    select(VoiceProfile).where(VoiceProfile.id == effective_voice_id)
                )
                voice_obj = voice_res.scalar_one_or_none()
                if voice_obj:
                    voice_name = voice_obj.name
                    provider_name = voice_obj.provider_name or "edge_tts"

        # 2. TTS 语音合成
        tts_params = PreviewSpeechParams(
            provider_name=provider_name,
            voice_name=voice_name,
            text=script_text,
        )
        audio_bytes, _ = await synthesize_preview_audio(tts_params)
        if not audio_bytes:
            raise RuntimeError(f"TTS 合成返回空音频: text={script_text[:30]}")

        # 3. 解码并执行 -16 LUFS 响度对齐与底噪特征分析
        from server.core.avatar.speech_drive_preview import _decode_pcm16k_mono
        pcm = _decode_pcm16k_mono(audio_bytes)

        # 阶段五防查：±1.5% 随机微变速 (破坏周期声学指纹)
        if enable_varispeed and len(pcm) > 100:
            speed_ratio = random.uniform(0.985, 1.015)
            pcm_bytes_raw = (np.clip(pcm, -1.0, 1.0) * 32767.0).astype(np.int16).tobytes()
            warped_bytes = apply_audio_varispeed(
                pcm_bytes_raw, sample_rate=SAMPLE_RATE_16K, speed_ratio=speed_ratio
            )
            pcm = np.frombuffer(warped_bytes, dtype=np.int16).astype(np.float32) / 32768.0

        norm_pcm, measured_lufs = normalize_loudness_lufs(pcm, TARGET_LOUDNESS_LUFS)
        noise_profile = extract_noise_profile(norm_pcm, sample_rate=SAMPLE_RATE_16K)
        duration_sec = round(len(norm_pcm) / float(SAMPLE_RATE_16K), 3)

        # 保存对齐后的音频文件
        audio_out_path = slice_work_dir / "audio.wav"
        self._write_pcm_to_wav(norm_pcm, audio_out_path, sample_rate=SAMPLE_RATE_16K)

        # 4. 神经驱动渲染视频切片
        # 默认引擎选择：若外部指定则优先遵从；否则探测云端/本地环境
        engine_type = force_engine or LATENTSYNC_CLOUD_ENGINE
        video_out_path = slice_work_dir / "slice.mp4"
        coords_out_path = slice_work_dir / "coords.npy"

        # 渲染视频与面部坐标
        actual_engine = await self._render_video_frames(
            anchor_id=anchor_id,
            pcm=norm_pcm,
            audio_path=audio_out_path,
            video_out_path=video_out_path,
            coords_out_path=coords_out_path,
            duration_sec=duration_sec,
            engine_type=engine_type,
        )

        # 5. 持久化至 ecommerce_sku_slices 数据库表
        sku_slice = SkuSlice(
            id=slice_id,
            product_id=product_id,
            slot_code=slot_code,
            slice_type=slice_type,
            script_text=script_text,
            video_path=video_out_path.as_posix(),
            audio_path=audio_out_path.as_posix(),
            duration_sec=duration_sec,
            coords_path=coords_out_path.as_posix() if coords_out_path.exists() else None,
            loudness_lufs=measured_lufs,
            bg_noise_profile=json.dumps(noise_profile, ensure_ascii=False),
            render_engine=actual_engine,
            is_active=1,
        )

        async with AsyncSessionLocal() as session:
            session.add(sku_slice)
            await session.commit()
            await session.refresh(sku_slice)

        logger.info(
            f"[SkuSliceRenderer] 切片渲染完成: {slice_id}, 时长={duration_sec}s, "
            f"响度={measured_lufs} LUFS, 引擎={engine_type}"
        )
        return sku_slice

    async def render_sku_standard_suite(
        self,
        product_id: str,
        anchor_id: str,
        voice_profile_id: Optional[str] = None,
    ) -> List[SkuSlice]:
        """
        一键批量渲染标准电商商品 5 大插槽切片：
        1. intro: 商品登场介绍
        2. selling_point_1: 核心功效/面料/专利卖点
        3. coupon: 优惠机制与限时立减
        4. faq_size: 规格/尺码/售后答疑
        5. urgency: 逼单催付倒计时
        """
        async with AsyncSessionLocal() as session:
            res = await session.execute(select(Product).where(Product.id == product_id))
            prod = res.scalar_one_or_none()
            if not prod:
                raise ValueError(f"未找到商品 ID: {product_id}")

        # 解析商品卖点与文案
        selling_points_list = []
        try:
            selling_points_list = json.loads(prod.selling_points or "[]")
        except Exception:
            pass

        p1_text = selling_points_list[0] if selling_points_list else "精选优质材料打造，匠心工艺保障高品质体验。"
        intro_text = f"宝子们看过来！今天这款我们家的热销爆款——{prod.title}，官方正品直发！"
        coupon_text = prod.coupon_script or f"直播间专属福利价，今天只要 {prod.live_price} 元，领券立减抓紧拍！"
        faq_text = "支持七天无理由退换货，运费险免费送，大家放心拍！"
        urgency_text = "库存仅剩最后少量现货，手慢无，倒计时3、2、1，赶紧去左下角购物车抢购！"

        slots_plan = [
            ("intro", intro_text, "main_pitch"),
            ("selling_point_1", p1_text, "main_pitch"),
            ("coupon", coupon_text, "coupon_burst"),
            ("faq_size", faq_text, "faq"),
            ("urgency", urgency_text, "coupon_burst"),
        ]

        slices: List[SkuSlice] = []
        for slot_code, script, s_type in slots_plan:
            slice_item = await self.render_single_slice(
                product_id=product_id,
                slot_code=slot_code,
                script_text=script,
                anchor_id=anchor_id,
                voice_profile_id=voice_profile_id,
                slice_type=s_type,
            )
            slices.append(slice_item)

        return slices

    async def _render_video_frames(
        self,
        anchor_id: str,
        pcm: np.ndarray,
        audio_path: Path,
        video_out_path: Path,
        coords_out_path: Path,
        duration_sec: float,
        engine_type: str,
    ) -> str:
        """根据配置引擎调用批处理渲染生成视频切片。

        返回**实际生效**的渲染引擎标识，供上层如实落库：
        真实神经渲染返回 ``latentsync_cloud``；任何降级路径返回
        ``placeholder_loop``（主播原始闭口帧镜像循环，无唇形同步）。
        架构诚实 (ADR-16)：绝不让调用方把占位产物当成高精渲染对外发布。
        """
        from server.core.avatar.speech_drive_preview import (
            SpeechDrivePreviewService,
            _compute_frame_count,
            _build_preview_mp4_cmd,
        )

        n_frames = _compute_frame_count(duration_sec)
        frames_dir = video_out_path.parent / "temp_frames"
        frames_dir.mkdir(parents=True, exist_ok=True)

        # 查找主播资产目录
        asset_dir = self._find_anchor_asset_dir(anchor_id)

        # 保存面部坐标缓存供缝合阶段重对齐
        coords_file = asset_dir / "coords.pkl" if asset_dir else None
        if coords_file and coords_file.exists():
            import shutil
            shutil.copy2(coords_file, coords_out_path)
        else:
            # 生成默认 256x256 居中人脸坐标占位
            np.save(coords_out_path, np.array([[100, 356, 100, 356]], dtype=np.int32))

        # 尝试通过 SpeechDrivePreviewService 走 LatentSync 真实管线
        service = SpeechDrivePreviewService()
        plan = await service._resolve_compute_plan()
        cloud = getattr(plan, "cloud_gpu", None)

        cloud_ready = bool(
            plan.use_cloud and plan.has_cloud_gpu and cloud and cloud.get("is_reachable")
        )

        if "latentsync" in engine_type and cloud_ready and asset_dir:
            actual_engine = engine_type
            try:
                # 调用云端 LatentSync 扩散模型批处理渲染
                outcome = await service._render_cloud_batch_latentsync(
                    cloud_gpu=cloud,
                    asset_dir=asset_dir,
                    pcm=pcm,
                    anchor_id=anchor_id,
                    n_frames=n_frames,
                )
                # 写入合成帧
                # 注意：full_frames / face_frames 可能是 numpy 数组，
                # 直接做真值判断会抛 "truth value of an array is ambiguous"
                # 从而把"云端渲染成功"误判为失败并静默降级，故一律显式判 None。
                frames = outcome.full_frames if outcome.full_frames is not None else outcome.face_frames
                if frames is None:
                    raise RuntimeError("云端渲染未返回任何帧序列")
                for idx, frame in enumerate(frames):
                    cv2.imwrite((frames_dir / f"{idx}.jpg").as_posix(), frame)
            except Exception as e:
                logger.warning(f"[SkuSliceRenderer] 云端 LatentSync 渲染不可用，回退本地渲染: {e}")
                actual_engine = PLACEHOLDER_ENGINE
                self._fallback_render_placeholder_frames(frames_dir, n_frames, asset_dir)
        else:
            # 本地渲染或占位渲染
            actual_engine = PLACEHOLDER_ENGINE
            self._fallback_render_placeholder_frames(frames_dir, n_frames, asset_dir)

        if actual_engine == PLACEHOLDER_ENGINE:
            logger.warning(
                "[SkuSliceRenderer] 本切片为无唇形同步的占位循环画面"
                f"(未配置可达云端 GPU 或缺少主播资产: engine_type={engine_type}, "
                f"cloud_ready={cloud_ready}, has_asset={bool(asset_dir)})，"
                "该状态将如实落库并触发开播体检拦截。"
            )

        # 4. 封装 CFR 25FPS 视频 (优先 ffmpeg，兜底使用 OpenCV VideoWriter)
        from server.core.media.rtmp_streamer import find_ffmpeg_binary
        ffmpeg_exe = find_ffmpeg_binary()
        video_success = False

        if ffmpeg_exe:
            try:
                cmd = _build_preview_mp4_cmd(frames_dir, audio_path, video_out_path, ffmpeg_exe=ffmpeg_exe)
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await proc.communicate()
                video_success = video_out_path.exists() and video_out_path.stat().st_size > 512
            except Exception as fe:
                logger.warning(f"[SkuSliceRenderer] ffmpeg 封装子进程异常: {fe}")

        if not video_success:
            # 优雅降级：用 OpenCV VideoWriter 合成 MP4 视频
            logger.info("[SkuSliceRenderer] 使用 OpenCV VideoWriter 进行视频帧流封装")
            # 兼容各版本 OpenCV 绑定 (VideoWriter.fourcc / VideoWriter_fourcc / 兜底 mp4v 编码值)
            if hasattr(cv2.VideoWriter, "fourcc"):
                fourcc = cv2.VideoWriter.fourcc(*"mp4v")
            elif hasattr(cv2, "VideoWriter_fourcc"):
                fourcc = getattr(cv2, "VideoWriter_fourcc")(*"mp4v")
            else:
                fourcc = ord("m") | (ord("p") << 8) | (ord("4") << 16) | (ord("v") << 24)
            first_frame_path = frames_dir / "0.jpg"
            if first_frame_path.exists():
                sample_img = cv2.imread(str(first_frame_path))
                if sample_img is not None:
                    h, w = sample_img.shape[:2]
                    writer = cv2.VideoWriter(str(video_out_path), fourcc, 25.0, (w, h))
                    for idx in range(n_frames):
                        f_p = frames_dir / f"{idx}.jpg"
                        if f_p.exists():
                            frame = cv2.imread(str(f_p))
                            if frame is not None:
                                writer.write(frame)
                    writer.release()

        # 清理临时抽帧目录
        import shutil
        shutil.rmtree(frames_dir, ignore_errors=True)

        return actual_engine

    def _find_anchor_asset_dir(self, anchor_id: str) -> Optional[Path]:
        """定位主播资产目录"""
        candidate = DATA_DIR / "avatar_assets" / anchor_id
        if candidate.is_dir():
            return candidate
        # 兼容以 anchors 表配置的目录
        return None

    def _fallback_render_placeholder_frames(
        self,
        frames_dir: Path,
        n_frames: int,
        asset_dir: Optional[Path],
    ) -> None:
        """兜底渲染帧序列：复用主播底片或生成测试帧"""
        from server.core.avatar.speech_drive_preview import _load_full_imgs
        from server.core.avatar.action_state_machine import mirror_index

        imgs = _load_full_imgs(asset_dir) if asset_dir else []
        n_imgs = len(imgs)

        for idx in range(n_frames):
            if n_imgs > 0:
                frame = imgs[mirror_index(idx, n_imgs)].copy()
            else:
                frame = np.full((960, 720, 3), 40, dtype=np.uint8)
                cv2.putText(
                    frame,
                    f"SKU Slice Frame {idx}",
                    (60, 480),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    1.0,
                    (200, 200, 200),
                    2,
                )
            cv2.imwrite((frames_dir / f"{idx}.jpg").as_posix(), frame)

    @staticmethod
    def _write_pcm_to_wav(pcm: np.ndarray, out_path: Path, sample_rate: int = 16000) -> None:
        """将 float32 PCM 写入 16-bit PCM WAV 文件"""
        import wave
        out_path.parent.mkdir(parents=True, exist_ok=True)
        pcm_int16 = (np.clip(pcm, -1.0, 1.0) * 32767.0).astype(np.int16)
        with wave.open(out_path.as_posix(), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(pcm_int16.tobytes())
