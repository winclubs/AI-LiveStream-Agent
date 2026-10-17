# -*- coding: utf-8 -*-
"""
高精切片渲染工厂与音频断层消除单元测试
覆盖：
1. 响度估算与 -16.0 LUFS 归一化 (EBU R128 近似)
2. 软限幅防爆音保护 (Soft-Clipping)
3. 背景环境底噪特征提取 (Noise Floor Profile)
4. 动态舒适底噪生成 (Comfort Noise Generation)
5. SkuSliceRenderer 全闭环切片调度与持久化入库
"""
import math
from pathlib import Path
from unittest.mock import AsyncMock, patch

import numpy as np
import pytest
from sqlalchemy import select

from server.core.avatar.sku_slice_renderer import (
    SkuSliceRenderer,
    estimate_loudness_lufs,
    extract_noise_profile,
    generate_comfort_noise,
    normalize_loudness_lufs,
)
from server.database.db import AsyncSessionLocal
from server.database.models import Anchor, Product, SkuSlice


def test_estimate_loudness_lufs_silence_and_signals():
    """测试静音与各种信号的 LUFS 响度估算"""
    # 纯静音
    silence = np.zeros(16000, dtype=np.float32)
    assert estimate_loudness_lufs(silence) == -70.0

    # 空数组
    assert estimate_loudness_lufs(np.array([], dtype=np.float32)) == -70.0

    # 已知 RMS 的正弦波 (RMS = 1 / sqrt(2) ≈ 0.7071)
    # 20 * log10(0.7071) - 0.691 ≈ -3.01 - 0.691 = -3.70 LUFS
    t = np.linspace(0, 1, 16000, endpoint=False)
    sine = np.sin(2 * np.pi * 440 * t).astype(np.float32)
    lufs = estimate_loudness_lufs(sine)
    assert lufs == pytest.approx(-3.7, abs=0.5)


def test_normalize_loudness_lufs_convergence_and_soft_clipping():
    """测试小信号放大至 -16 LUFS，大信号衰减且软限幅保护不超限"""
    t = np.linspace(0, 1, 16000, endpoint=False)

    # 1. 微弱信号 (初值 RMS ≈ 0.01)
    weak_sine = (0.01 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    init_lufs = estimate_loudness_lufs(weak_sine)
    assert init_lufs < -30.0

    norm_pcm, measured_lufs = normalize_loudness_lufs(weak_sine, target_lufs=-16.0)
    assert measured_lufs == pytest.approx(-16.0, abs=0.5)
    # 峰值绝不破音削顶
    assert np.max(np.abs(norm_pcm)) <= 1.0

    # 2. 强信号归一化测试
    loud_sine = (0.9 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    norm_loud, loud_lufs = normalize_loudness_lufs(loud_sine, target_lufs=-16.0)
    assert loud_lufs == pytest.approx(-16.0, abs=0.5)
    assert np.max(np.abs(norm_loud)) <= 1.0


def test_noise_profile_extraction_and_comfort_noise():
    """测试人声间歇期底噪特征提取与舒适底噪动态生成"""
    sr = 16000

    # 构造前半段有声 (0.5)、后半段微弱背景底噪 (RMS ≈ 0.002, 约 -54dB)
    speech = 0.5 * np.random.normal(0, 1, sr).astype(np.float32)
    background_noise = 0.002 * np.random.normal(0, 1, sr).astype(np.float32)
    audio = np.concatenate([speech, background_noise])

    profile = extract_noise_profile(audio, sample_rate=sr)
    assert "noise_floor_db" in profile
    assert "spectral_centroid_hz" in profile
    assert "variance" in profile
    # 底噪能量应在 -60 ~ -45 dB 附近
    assert -65.0 <= profile["noise_floor_db"] <= -40.0

    # 生成 1 秒舒适底噪
    comfort = generate_comfort_noise(16000, noise_profile=profile)
    assert len(comfort) == 16000
    assert comfort.dtype == np.float32
    # 能量水平与期望相符
    comfort_rms = float(np.sqrt(np.mean(np.square(comfort))))
    expected_rms = math.pow(10.0, profile["noise_floor_db"] / 20.0)
    assert comfort_rms == pytest.approx(expected_rms, rel=0.3)


@pytest.mark.anyio
async def test_sku_slice_renderer_single_and_suite(tmp_path: Path):
    """测试 SkuSliceRenderer 单切片与整套切片渲染持久化"""
    renderer = SkuSliceRenderer(storage_dir=tmp_path)

    async with AsyncSessionLocal() as session:
        # 创建主播
        anchor = Anchor(
            id="test_anchor_renderer_01",
            name="带货主播小张",
            anchor_type="ecommerce",
        )
        session.add(anchor)

        # 创建商品
        prod = Product(
            id="test_prod_renderer_01",
            sku_code="SKU_RENDER_01",
            title="多功能破壁料理机",
            category="小家电",
            live_price=199.0,
            original_price=399.0,
            current_stock=100,
            selling_points='["八叶精钢破壁刀", "低音降噪静音舱"]',
            coupon_script="直播间专享立减 200，下单即送精美冷热双杯！",
        )
        session.add(prod)
        await session.commit()

    # mock TTS 音频返回 1 秒有效 16kHz float32 正弦波
    sine_1s = (0.2 * np.sin(2 * np.pi * 440 * np.linspace(0, 1, 16000))).astype(np.float32)
    fake_wav_bytes = (sine_1s * 32767.0).astype(np.int16).tobytes()

    with patch("server.core.avatar.sku_slice_renderer.synthesize_preview_audio", new=AsyncMock(return_value=(fake_wav_bytes, "audio/wav"))):
        # 渲染单个切片
        slice_result = await renderer.render_single_slice(
            product_id="test_prod_renderer_01",
            slot_code="intro",
            script_text="欢迎各位宝子们来到直播间！今天这款爆款料理机千万别错过！",
            anchor_id="test_anchor_renderer_01",
            slice_type="main_pitch",
            force_engine="latentsync_cloud",
        )

        assert slice_result.slot_code == "intro"
        assert slice_result.duration_sec == pytest.approx(1.0, abs=0.1)
        # 架构诚实 (ADR-16)：本用例未配置可达云端 GPU，渲染实际走占位闭环路径，
        # 必须如实落库为 placeholder_loop，绝不允许伪装成 latentsync_cloud。
        assert slice_result.render_engine == "placeholder_loop"
        assert slice_result.loudness_lufs == pytest.approx(-16.0, abs=0.8)
        assert Path(slice_result.audio_path).exists()
        assert Path(slice_result.video_path).exists()

        # 数据库中可查
        async with AsyncSessionLocal() as session:
            loaded = (await session.execute(
                select(SkuSlice).where(SkuSlice.id == slice_result.id)
            )).scalar_one_or_none()
            assert loaded is not None
            assert loaded.slot_code == "intro"
            assert loaded.render_engine == "placeholder_loop"

        # 渲染整套切片 (intro, selling_point_1, coupon, faq_size, urgency)
        suite = await renderer.render_sku_standard_suite(
            product_id="test_prod_renderer_01",
            anchor_id="test_anchor_renderer_01",
        )
        assert len(suite) == 5
        slot_codes = [s.slot_code for s in suite]
        assert slot_codes == ["intro", "selling_point_1", "coupon", "faq_size", "urgency"]

    # 清理
    async with AsyncSessionLocal() as session:
        del_prod = (await session.execute(
            select(Product).where(Product.id == "test_prod_renderer_01")
        )).scalar_one()
        await session.delete(del_prod)
        del_anchor = (await session.execute(
            select(Anchor).where(Anchor.id == "test_anchor_renderer_01")
        )).scalar_one()
        await session.delete(del_anchor)
        await session.commit()


@pytest.mark.anyio
async def test_render_video_frames_reports_honest_engine(tmp_path: Path):
    """_render_video_frames 必须回报实际生效引擎：云端就绪 -> latentsync_cloud，
    未就绪 -> placeholder_loop。这是"绝不静默降级"承诺的最小可验证契约。"""
    from types import SimpleNamespace

    renderer = SkuSliceRenderer(storage_dir=tmp_path)

    ready_plan = SimpleNamespace(
        use_cloud=True,
        has_cloud_gpu=True,
        cloud_gpu={"is_reachable": True},
    )
    with patch(
        "server.core.avatar.speech_drive_preview.SpeechDrivePreviewService._resolve_compute_plan",
        new=AsyncMock(return_value=ready_plan),
    ), patch.object(
        renderer, "_find_anchor_asset_dir", return_value=tmp_path
    ), patch(
        "server.core.avatar.speech_drive_preview.SpeechDrivePreviewService"
        "._render_cloud_batch_latentsync",
        new=AsyncMock(return_value=SimpleNamespace(
            full_frames=np.zeros((2, 8, 8, 3), dtype=np.uint8),
            face_frames=None,
        )),
    ), patch(
        "server.core.media.rtmp_streamer.find_ffmpeg_binary", return_value=None
    ):
        engine = await renderer._render_video_frames(
            anchor_id="a1",
            pcm=np.zeros(1600, dtype=np.float32),
            audio_path=tmp_path / "a.wav",
            video_out_path=tmp_path / "slice.mp4",
            coords_out_path=tmp_path / "coords.npy",
            duration_sec=0.1,
            engine_type="latentsync_cloud",
        )
    assert engine == "latentsync_cloud"

    down_plan = SimpleNamespace(use_cloud=False, has_cloud_gpu=False, cloud_gpu=None)
    with patch(
        "server.core.avatar.speech_drive_preview.SpeechDrivePreviewService._resolve_compute_plan",
        new=AsyncMock(return_value=down_plan),
    ), patch(
        "server.core.media.rtmp_streamer.find_ffmpeg_binary", return_value=None
    ):
        engine = await renderer._render_video_frames(
            anchor_id="a1",
            pcm=np.zeros(1600, dtype=np.float32),
            audio_path=tmp_path / "a.wav",
            video_out_path=tmp_path / "slice2.mp4",
            coords_out_path=tmp_path / "coords2.npy",
            duration_sec=0.1,
            engine_type="latentsync_cloud",
        )
    assert engine == "placeholder_loop"
