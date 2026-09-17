# -*- coding: utf-8 -*-
"""
硬件显卡与云端算力智能调度及显存保护自动化测试套件
验证用户核心需求：
1. 本地显卡配置很低时，在需要高性能显卡时尽可能优先调用云端显卡；
2. 如果功能必须大于 2G 显存，本地不足且未配置云端显卡时，向用户提供明确提示并阻断/降级；
3. 全局开播预检与硬件接口正确输出算力研判数据。
"""
import pytest
from unittest.mock import patch
from httpx import AsyncClient, ASGITransport

from server.app import app
from server.core.hardware.gpu_capability import (
    GpuInfo,
    CloudGpuInfo,
    ComputePlan,
    probe_local_gpu,
    evaluate_compute,
    MIN_HIGH_PERFORMANCE_VRAM_GB,
)
from server.core.avatar.registry import AvatarDriverFactory
from server.core.avatar.drivers import LocalLiveTalkingDriver, CloudSidecarDriver, Procedural2DDriver


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_low_spec_without_cloud_gpu_prompts_user():
    """
    场景 1：本地显卡配置低 (显存 1.0GB, 无 CUDA)，未对接云端显卡
    预期：
      - 明确提示用户硬件不足且未配置云端显卡；
      - evaluate_compute 明确给出警示；
      - AvatarDriverFactory 安全降级至 procedural，并挂载 hardware_warning；
      - 若 strict_gpu=True 则抛出明确的 RuntimeError。
    """
    fake_low_gpu = GpuInfo(
        gpu_name="Intel UHD Graphics 620",
        vram_total_gb=1.0,
        vram_used_gb=0.2,
        cuda_available=False,
        is_low_spec=True,
    )
    fake_no_cloud = CloudGpuInfo(configured=False)

    with patch("server.core.hardware.gpu_capability.probe_local_gpu", return_value=fake_low_gpu):
        plan = await evaluate_compute("数字人高保真渲染", required_vram_gb=2.0, cloud_gpu_override=fake_no_cloud)

        assert plan.can_execute is False
        assert plan.use_cloud is False
        assert plan.is_low_spec_local is True
        assert plan.has_cloud_gpu is False
        assert plan.alert_type == "insufficient_hardware"
        # 必须明确包含用户提示关键字
        assert "硬件显存不足警示" in plan.user_message
        assert "尚未对接云端显卡" in plan.user_message
        assert "系统设置" in plan.user_message

        with patch("server.core.avatar.registry.probe_local_gpu", return_value=fake_low_gpu), \
             patch("server.core.avatar.registry.get_active_cloud_gpu_sync", return_value=fake_no_cloud):
            # 自动降级模式
            driver = AvatarDriverFactory.create("livetalking", config={"session_id": "test_s1", "auto_fallback": True})
            assert isinstance(driver, Procedural2DDriver)
            assert hasattr(driver, "hardware_warning")
            assert "硬件显存不足警示" in driver.hardware_warning

            # 显式保留类型时挂载警示
            driver_explicit = AvatarDriverFactory.create("livetalking", config={"session_id": "test_s1_exp"})
            assert isinstance(driver_explicit, LocalLiveTalkingDriver)
            assert hasattr(driver_explicit, "hardware_warning")
            assert "硬件显存不足警示" in driver_explicit.hardware_warning

            # 严格模式抛出异常
            with pytest.raises(RuntimeError) as exc_info:
                AvatarDriverFactory.create("livetalking", config={"strict_gpu": True})
            assert "硬件显存不足警示" in str(exc_info.value)


@pytest.mark.anyio
async def test_low_spec_with_cloud_gpu_prioritizes_cloud():
    """
    场景 2：本地显卡配置低 (显存 1.0GB)，但已配置云端显卡 (Sidecar)
    预期：
      - 优先自动调度云端显卡；
      - use_cloud=True, can_execute=True；
      - AvatarDriverFactory.create("livetalking", prefer_cloud=True) 自动切换为 CloudSidecarDriver。
    """
    fake_low_gpu = GpuInfo(
        gpu_name="Intel Iris Xe",
        vram_total_gb=1.5,
        vram_used_gb=0.1,
        cuda_available=False,
        is_low_spec=True,
    )
    fake_cloud = CloudGpuInfo(
        configured=True,
        provider_name="sidecar_v3",
        adapter="sidecar_v3",
        base_url="ws://gpu-cloud.example.com:8765",
        is_active=True,
        api_key="secret-token-123",
    )

    with patch("server.core.hardware.gpu_capability.probe_local_gpu", return_value=fake_low_gpu):
        plan = await evaluate_compute("数字人高保真口型", required_vram_gb=2.0, cloud_gpu_override=fake_cloud)

        assert plan.can_execute is True
        assert plan.use_cloud is True
        assert plan.has_cloud_gpu is True
        assert plan.alert_type == "cloud_dispatched"
        assert "云端显卡加速" in plan.user_message
        assert plan.recommended_driver == "cloud_sidecar"

        with patch("server.core.avatar.registry.probe_local_gpu", return_value=fake_low_gpu), \
             patch("server.core.avatar.registry.get_active_cloud_gpu_sync", return_value=fake_cloud):
            driver = AvatarDriverFactory.create("livetalking", config={"session_id": "test_s2", "prefer_cloud": True})
            assert isinstance(driver, CloudSidecarDriver)
            assert driver.sidecar_url == "ws://gpu-cloud.example.com:8765"


@pytest.mark.anyio
async def test_high_spec_without_cloud_runs_locally():
    """
    场景 3：本地拥有高端显卡 (如 RTX 4090, 24GB)，未配置云端显卡
    预期：
      - 允许在本地高性能执行；
      - alert_type 为 none。
    """
    fake_high_gpu = GpuInfo(
        gpu_name="NVIDIA GeForce RTX 4090",
        vram_total_gb=24.0,
        vram_used_gb=2.0,
        cuda_available=True,
        is_low_spec=False,
    )
    fake_no_cloud = CloudGpuInfo(configured=False)

    with patch("server.core.hardware.gpu_capability.probe_local_gpu", return_value=fake_high_gpu):
        plan = await evaluate_compute("数字人本地渲染", required_vram_gb=2.0, cloud_gpu_override=fake_no_cloud)

        assert plan.can_execute is True
        assert plan.use_cloud is False
        assert plan.is_low_spec_local is False
        assert plan.alert_type == "none"
        assert plan.recommended_driver == "livetalking"


@pytest.mark.anyio
async def test_api_preflight_and_hardware_contain_gpu_capability():
    """
    场景 4：验证 /api/v1/live/hardware 和 /api/v1/live/preflight 包含算力研判
    """
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 1. 验证 hardware 接口
        res_hw = await client.get("/api/v1/live/hardware")
        assert res_hw.status_code == 200
        hw_data = res_hw.json()["data"]
        assert "gpu_capability" in hw_data
        cap = hw_data["gpu_capability"]
        assert "use_cloud" in cap
        assert "is_low_spec_local" in cap
        assert "has_cloud_gpu" in cap

        # 2. 验证 preflight 检查清单中新增了 gpu_cloud_dispatch 项
        res_pf = await client.get("/api/v1/live/preflight")
        assert res_pf.status_code == 200
        pf_data = res_pf.json()["data"]
        check_keys = [c["key"] for c in pf_data["checks"]]
        assert "gpu_cloud_dispatch" in check_keys
