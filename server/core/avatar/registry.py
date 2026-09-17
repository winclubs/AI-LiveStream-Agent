# -*- coding: utf-8 -*-
"""
数字人驱动注册中心与安全工厂 (Avatar Driver Registry & Factory)
支持按配置与硬件自动匹配/切换最佳驱动实现：
  - livetalking: 本地 LiveTalking 深度学习驱动 (D:/LiveTalking)
  - cloud_sidecar: 本地 CPU+内存 + 第三方云端显卡对接模式
  - procedural: 本地轻量免显卡 2D 程序化渲染
  - mock: 仿真测试驱动
"""
import logging
from typing import Any, Callable, Dict, Optional, Type
from server.core.avatar.base_driver import BaseAvatarDriver
from server.core.hardware.gpu_capability import (
    probe_local_gpu,
    get_active_cloud_gpu_sync,
    MIN_HIGH_PERFORMANCE_VRAM_GB,
)

logger = logging.getLogger("LiveAgent.AvatarRegistry")

_DRIVER_REGISTRY: Dict[str, Type[BaseAvatarDriver]] = {}


def register_avatar_driver(driver_type: str) -> Callable:
    """注册装饰器：将驱动器类注入注册中心"""
    def decorator(cls: Type[BaseAvatarDriver]):
        clean_type = driver_type.strip().lower()
        _DRIVER_REGISTRY[clean_type] = cls
        cls.driver_type = clean_type  # 赋予类型标记
        logger.info(f"已注册数字人驱动器类型: [{clean_type}] -> {cls.__name__}")
        return cls
    return decorator


class AvatarDriverFactory:
    """数字人驱动安全构建工厂"""

    @classmethod
    def list_available_types(cls) -> list[str]:
        """获取所有已注册的驱动类型列表"""
        return list(_DRIVER_REGISTRY.keys())

    @classmethod
    def create(cls, driver_type: str, config: Optional[Dict[str, Any]] = None) -> BaseAvatarDriver:
        """根据类型与配置创建驱动实例，支持未知类型安全回退到 procedural 兜底"""
        clean_type = (driver_type or "procedural").strip().lower()

        warning_msg: Optional[str] = None

        # 模式 1：auto 自动感知推荐
        if clean_type in ("auto", "default"):
            cloud_gpu = get_active_cloud_gpu_sync()
            if cloud_gpu.configured:
                clean_type = "cloud_sidecar"
            else:
                local_gpu = probe_local_gpu()
                clean_type = "procedural" if local_gpu.is_low_spec else "livetalking"

        # 模式 2：livetalking 深度学习渲染（需高性能显卡 >2GB）
        if clean_type == "livetalking":
            cloud_gpu = get_active_cloud_gpu_sync()
            # 规则一：若开启 prefer_cloud 或 auto_fallback，优先调度云端显卡
            if cloud_gpu.configured and (config and (config.get("prefer_cloud") or config.get("auto_fallback"))):
                logger.info(
                    f"【云端显卡优先调度】检测到系统已配置云端显卡节点 [{cloud_gpu.provider_name}]，"
                    f"自动优先切换至云端显卡驱动 (cloud_sidecar)，释放本地计算负载"
                )
                clean_type = "cloud_sidecar"
                cfg_merged = dict(config or {})
                if not cfg_merged.get("sidecar_url") and cloud_gpu.base_url:
                    cfg_merged["sidecar_url"] = cloud_gpu.base_url
                config = cfg_merged
            else:
                # 规则二：未配置云端显卡且本地显卡不足（<2GB）时，明确警示
                local_gpu = probe_local_gpu()
                if local_gpu.is_low_spec:
                    warning_msg = (
                        f"⚠️【硬件显存不足警示】数字人深度学习渲染需要高性能显卡（显存需 > {MIN_HIGH_PERFORMANCE_VRAM_GB}GB）。"
                        f"检测到您的本地显卡为 {local_gpu.gpu_name or '核显/无独显'} (显存仅 {local_gpu.vram_total_gb}GB)，"
                        f"且系统当前尚未配置云端显卡（Sidecar / 远程 GPU）！\n"
                        f"👉 建议方案：\n"
                        f"1. 前往【系统设置 -> 显卡与渲染设置】配置自建云端显卡算力节点 (Sidecar)；\n"
                        f"2. 建议切换为【轻量 CPU 免显卡模式】，保障直播中枢平稳运行。"
                    )
                    logger.warning(warning_msg)
                    if config and config.get("strict_gpu"):
                        raise RuntimeError(warning_msg)
                    if config and config.get("auto_fallback"):
                        clean_type = "procedural"

        target_cls = _DRIVER_REGISTRY.get(clean_type)

        if target_cls is None:
            logger.warning(f"未找到驱动类型 [{clean_type}]，自动回退到 [procedural] 程序化渲染驱动")
            target_cls = _DRIVER_REGISTRY.get("procedural", _DRIVER_REGISTRY.get("mock"))

        if target_cls is None:
            raise RuntimeError(f"数字人驱动注册表中未注册任何可用驱动 (请求类型: {driver_type})")

        driver_instance = target_cls(config or {})
        if warning_msg:
            driver_instance.hardware_warning = warning_msg
        return driver_instance

    @classmethod
    def create_driver(cls, driver_type: str, config: Optional[Dict[str, Any]] = None) -> BaseAvatarDriver:
        """create 方法的语义别名，提供统一工厂接口风格"""
        return cls.create(driver_type, config)

