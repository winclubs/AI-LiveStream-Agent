# -*- coding: utf-8 -*-
"""
神经唇形实时算力预检 (LIPSYNC_OPTIMIZATION_PLAN.md v3.2 §7)

背景
----
实测（本机12 核 CPU，无 CUDA）::

    单帧 ONNX 推理   176.4 ms
    25fps 帧预算      40.0 ms      -> 超 4.4 倍
    实际可达帧率      5.5 fps

也就是说：**在没有 CUDA 的机器上，逐帧神经重绘在架构上就不可能达到 25fps**。
渲染线程会满负荷空转（`time.sleep(max(0.001, target_sleep))` 里target_sleep
恒为 0.001），画面持续掉帧、唇形逐帧跳变，而上层毫无提示。

用户开了播才发现卡住 —— 这是最差的体验。本模块把这个问题前移到开播前，
如实告知「本地只能预览，实时请走云端」，而不是让用户自己撞上。

设计原则
--------
1. **实测优先**：不靠"估算公式"，直接跑几帧真实推理测延迟。
2. **诚实**：探测失败/超时一律按「不可行」处理，不做乐观假设。
3. **非阻塞**：探测在后台线程执行，且结果可缓存，避免每次开播都付代价。
4. **不拦截**：本模块只**告知**，不阻断开播。是否开播由用户决定 ——
   阻断是产品决策，不是算力模块该越权的事。
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger("LiveAgent.LipSyncPreflight")

# 25fps 下每帧的绝对预算（毫秒）
FRAME_BUDGET_MS_25FPS = 1000.0 / 25.0      # 40.0
# 允许的安全余量：实测延迟需低于预算的该比例才判定为"宽裕"
SAFETY_MARGIN = 0.85
# 实测时使用的帧数（太少噪声大，太多浪费时间）
PROBE_FRAMES = 5
# 单帧探测超时上限（毫秒）—— 超过即视为不可行，不再等待
PROBE_FRAME_TIMEOUT_MS = 2000.0

# 判定档位
LEVEL_REALTIME = "realtime"       # 满足 25fps 且有余量
LEVEL_TIGHT = "tight"             # 勉强满足 25fps
LEVEL_PREVIEW_ONLY = "preview_only"   # 达不到 25fps，只适合预览
LEVEL_UNAVAILABLE = "unavailable"     # 引擎未就绪/探测失败


@dataclass
class PreflightResult:
    """算力预检结论"""

    level: str = LEVEL_UNAVAILABLE
    ok: bool = False
    provider: str = ""                  # CPUExecutionProvider / CUDAExecutionProvider
    model_key: str = ""
    measured_ms: float = 0.0            # 实测单帧推理耗时
    budget_ms: float = FRAME_BUDGET_MS_25FPS
    target_fps: int = 25
    achievable_fps: float = 0.0
    headroom: float = 0.0               # 预算占比，<=1 表示跑得完
    reason: str = ""
    hints: List[str] = field(default_factory=list)
    measured_at: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def summary(self) -> str:
        """面向用户的一句话结论"""
        if not self.ok:
            return f"神经唇形不可用：{self.reason}"
        if self.level == LEVEL_REALTIME:
            return (
                f"神经唇形就绪：{self.provider} 实测 {self.measured_ms:.0f}ms/帧，"
                f"可达 {self.achievable_fps:.1f}fps（预算 {self.budget_ms:.0f}ms）"
            )
        if self.level == LEVEL_TIGHT:
            return (
                f"神经唇形算力紧张：{self.provider} 实测 {self.measured_ms:.0f}ms/帧，"
                f"勉强可达 {self.achievable_fps:.1f}fps，偶发掉帧"
            )
        return (
            f"算力不足：{self.provider} 实测 {self.measured_ms:.0f}ms/帧，"
            f"仅为 25fps 预算（{self.budget_ms:.0f}ms）的 {self.measured_ms / self.budget_ms:.1f} 倍，"
            f"实际约 {self.achievable_fps:.1f}fps —— 本地仅适合预览/调试，实时直播请走云端 GPU"
        )


class LipSyncPreflight:
    """神经唇形实时算力预检器（单例）。

    结果按 (model_key, provider) 缓存：同一配置下重复开播无需重复实测。
    """

    _instance: Optional["LipSyncPreflight"] = None
    _instance_lock = threading.Lock()
    _cache: Dict[str, PreflightResult]
    _cache_lock: threading.Lock

    def __new__(cls) -> "LipSyncPreflight":
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    instance = super().__new__(cls)
                    instance._cache = {}
                    instance._cache_lock = threading.Lock()
                    cls._instance = instance
        return cls._instance

    def __init__(self) -> None:
        if not hasattr(self, "_cache"):
            self._cache = {}
        if not hasattr(self, "_cache_lock"):
            self._cache_lock = threading.Lock()

    def _cache_key(self, model_key: str) -> str:
        return model_key or "onnx_lipsync"

    def cached(self, model_key: str = "") -> Optional[PreflightResult]:
        key = self._cache_key(model_key)
        with self._cache_lock:
            return self._cache.get(key)

    def invalidate(self, model_key: str = "") -> None:
        key = self._cache_key(model_key)
        with self._cache_lock:
            self._cache.pop(key, None)

    # ------------------------------------------------------------------ 探测

    def probe(
        self,
        renderer: Any = None,
        model_key: str = "",
        target_fps: int = 25,
        use_cache: bool = True,
        force: bool = False,
    ) -> PreflightResult:
        """实测神经渲染单帧耗时并给出可行性判定。

        `renderer` 为已就绪的 NeuralLipRenderer；不传则自行构造。
        本方法**阻塞**，调用方应放在线程里执行。
        """
        if use_cache and not force:
            hit = self.cached(model_key)
            if hit is not None:
                return hit

        result = self._do_probe(renderer, model_key, target_fps)
        with self._cache_lock:
            self._cache[self._cache_key(model_key)] = result
        return result

    def _do_probe(self, renderer: Any, model_key: str, target_fps: int) -> PreflightResult:
        budget = 1000.0 / max(1, target_fps)

        own_renderer = False
        if renderer is None:
            try:
                from server.core.avatar.neural_lip_renderer import NeuralLipRenderer

                renderer = NeuralLipRenderer(model_key=model_key or "onnx_lipsync")
                own_renderer = True
            except Exception as e:
                return PreflightResult(
                    level=LEVEL_UNAVAILABLE,
                    ok=False,
                    reason=f"神经渲染引擎构造失败: {e}",
                    budget_ms=budget,
                    target_fps=target_fps,
                    measured_at=time.time(),
                )

        try:
            key = getattr(renderer, "model_key", model_key or "onnx_lipsync")
            session = getattr(renderer, "session", None)
            if session is None or not getattr(renderer, "is_ready", False):
                return PreflightResult(
                    level=LEVEL_UNAVAILABLE,
                    ok=False,
                    model_key=key,
                    budget_ms=budget,
                    target_fps=target_fps,
                    reason="神经渲染引擎未就绪（模型权重缺失或加载失败）",
                    hints=["请确认已安装 onnx_lipsync 权重（设置页 → 模型管理）"],
                    measured_at=time.time(),
                )

            providers = []
            try:
                providers = list(session.get_providers())
            except Exception:
                pass
            provider = providers[0] if providers else "unknown"
            is_cuda = any("CUDA" in p or "Tensorrt" in p for p in providers)

            extractor = getattr(renderer, "mel_extractor", None)
            if extractor is None:
                return PreflightResult(
                    level=LEVEL_UNAVAILABLE, ok=False, provider=provider,
                    model_key=key, budget_ms=budget, target_fps=target_fps,
                    reason="缺少 mel 特征提取器", measured_at=time.time(),
                )

            # 构造与真实链路一致的输入：256x256 人脸 + 6400 采样音频窗口
            import numpy as np

            face = np.zeros((256, 256, 3), dtype=np.uint8)
            face[100:160] = 180
            try:
                face_tensor = renderer._prepare_face_input(face)
            except Exception:
                face_tensor = np.zeros((1, 6, 256, 256), dtype=np.float32)

            pcm = (np.random.default_rng(0).standard_normal(6400) * 0.1).astype(np.float32)
            feed: Dict[str, Any] = {}
            for name in getattr(renderer, "input_names", []):
                if "audio" in name.lower() or "mel" in name.lower():
                    feed[name] = extractor.extract_mel_window(pcm, target_steps=16)
                else:
                    feed[name] = face_tensor

            out_names = getattr(renderer, "output_names", [])
            # 预热（首次调用含图优化开销，不计入实测）
            session.run(out_names, feed)

            per_frame: List[float] = []
            for _ in range(PROBE_FRAMES):
                t0 = time.perf_counter()
                mel = extractor.extract_mel_window(pcm, target_steps=16)
                for name in getattr(renderer, "input_names", []):
                    if "audio" in name.lower() or "mel" in name.lower():
                        feed[name] = mel
                session.run(out_names, feed)
                dt = (time.perf_counter() - t0) * 1000.0
                per_frame.append(dt)
                if dt > PROBE_FRAME_TIMEOUT_MS:
                    break

            if not per_frame:
                return PreflightResult(
                    level=LEVEL_UNAVAILABLE, ok=False, provider=provider,
                    model_key=key, budget_ms=budget, target_fps=target_fps,
                    reason="推理探测未产生有效采样", measured_at=time.time(),
                )

            # 取中位数：单帧抖动不应影响判定
            per_frame.sort()
            measured = per_frame[len(per_frame) // 2]
            achievable = 1000.0 / measured if measured > 0 else 0.0
            headroom = measured / budget

            hints: List[str] = []
            if headroom <= SAFETY_MARGIN:
                level, ok = LEVEL_REALTIME, True
            elif headroom <= 1.0:
                level, ok = LEVEL_TIGHT, True
                hints.append("算力接近上限，建议关闭其他高负载程序或改用云端 GPU")
            else:
                level, ok = LEVEL_PREVIEW_ONLY, False
                if not is_cuda:
                    hints.append(
                        "当前为纯 CPU 推理。LatentSync 逐帧高精重绘在 CPU 上无法达到 25fps"
                        f"（实测超出预算 {headroom:.1f} 倍）"
                    )
                else:
                    hints.append(
                        f"即使使用 GPU，实测仍超出 25fps 预算 {headroom:.1f} 倍，"
                        f"建议降低分辨率/帧率或改用云端节点"
                    )
                hints.append("本地仅适合试听预览与参数调试；正式直播请使用云端 GPU 节点")
                hints.append(
                    "若必须在本地实时，可尝试 INT8 量化（scripts/quantize_onnx.py）后重新实测"
                )

            if is_cuda and headroom <= SAFETY_MARGIN:
                hints.append("GPU 推理余量充足，可开启时序模型（T=5）进一步消除帧间抖动")

            return PreflightResult(
                level=level,
                ok=ok,
                provider=provider,
                model_key=key,
                measured_ms=round(measured, 2),
                budget_ms=round(budget, 2),
                target_fps=target_fps,
                achievable_fps=round(achievable, 2),
                headroom=round(headroom, 3),
                reason="" if ok else f"单帧 {measured:.0f}ms 超出 25fps 预算 {budget:.0f}ms",
                hints=hints,
                measured_at=time.time(),
            )
        except Exception as e:
            logger.warning("神经唇形算力预检失败: %s", e, exc_info=True)
            return PreflightResult(
                level=LEVEL_UNAVAILABLE, ok=False,
                budget_ms=budget, target_fps=target_fps,
                reason=f"算力预检异常: {e}",
                measured_at=time.time(),
            )


# 全局单例
global_lipsync_preflight = LipSyncPreflight()


def probe_async(renderer: Any = None, model_key: str = "",
                target_fps: int = 25, **kwargs) -> "Any":
    """在后台线程执行预检，返回 awaitable（供 async 调用方使用）。

    用法::

        result = await probe_async(renderer=drv.lip_renderer)
        if not result.ok:
            logger.warning(result.summary())
    """
    import asyncio

    async def _run() -> PreflightResult:
        return await asyncio.to_thread(
            global_lipsync_preflight.probe, renderer, model_key, target_fps, **kwargs
        )

    return _run()


def probe_blocking(renderer: Any = None, model_key: str = "",
                   target_fps: int = 25, **kwargs) -> PreflightResult:
    """同步探测（供脚本/CLI 使用）。"""
    return global_lipsync_preflight.probe(renderer, model_key, target_fps, **kwargs)
