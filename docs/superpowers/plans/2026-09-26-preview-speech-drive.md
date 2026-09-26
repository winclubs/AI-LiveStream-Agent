# 试播台词驱动 · 真实算力闭环 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把「试听台词驱动」从纯前端 Canvas 假驱动（含双下唇 bug）替换为后端真实神经推理（本地 ONNX / 云端 Sidecar）+ 实测遥测，并让前端播放真实帧序列。

**Architecture:** 新增 `SpeechDrivePreviewService` 编排 TTS 合成 → PCM 解码 → 按 `gpu_target` 分流真实推理 → 落盘临时帧 → 返回帧地址与实测遥测；`NeuralLipRenderer` 与 `NeuralSidecarMediaDriver` 均为既有组件，仅做最小改造复用。Spec：`docs/superpowers/specs/2026-09-26-preview-speech-drive-design.md`。

**Tech Stack:** Python 3.12 / FastAPI / ONNX Runtime / OpenCV / NumPy / WebSocket(v3 sidecar) / 原生 JS。

---

## 文件结构

| 文件 | 职责 | 动作 |
|---|---|---|
| `server/core/audio/tts_preview_service.py` | 共享 TTS 试听合成（provider 解析 + 合成，返回 bytes） | 新建 |
| `server/routes/settings.py` | `/tts/preview` 路由 | 改为委托 |
| `server/core/avatar/neural_lip_renderer.py` | 真实本地神经渲染 | 增 `return_face`；`_crop_face_256` 转公共 |
| `server/core/avatar/speech_drive_preview.py` | 试播编排服务（分流/渲染/落盘/遥测/清理） | 新建 |
| `server/routes/anchors.py` | 新端点 + 会话静态帧路由 | 新增路由 |
| `server/static/js/modules/13_anchors.js` | 前端试播流程 | 重写 + 删假驱动 |
| `server/static/components/tab_anchors.html`, `server/static/index.html` | 移除无用 canvas 元素 | 小改 |
| `server/static/js/console.js` | 未被加载的重复假驱动代码 | 删除死代码 |
| `server/tests/test_speech_drive_preview.py` | 服务层 + 端点测试 | 新建 |
| `server/tests/test_tts_preview_service.py` | 合成函数回归测试 | 新建 |

---

## Task 1: 抽取共享 TTS 合成服务

**Files:**
- Create: `server/core/audio/tts_preview_service.py`
- Modify: `server/routes/settings.py`（`preview_tts_audio` 1492-1777；`VOICE_PREVIEW_PROFILES` 1441）
- Test: `server/tests/test_tts_preview_service.py`

- [ ] **Step 1: 写失败测试**

`server/tests/test_tts_preview_service.py`：

```python
# -*- coding: utf-8 -*-
"""共享 TTS 试听合成服务回归测试"""
import pytest


def _patch_edge_tts(monkeypatch):
    """用确定性假音频替换 edge_tts 网络调用"""
    import types

    fake = types.ModuleType("edge_tts")

    class _Stream:
        def __init__(self, payload: bytes):
            self._payload = payload
            self._yielded = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self._yielded:
                raise StopAsyncIteration
            self._yielded = True
            return {"type": "audio", "data": self._payload}

        async def aclose(self):
            pass

    class _Communicate:
        def __init__(self, text: str, voice: str):
            self.text = text
            self.voice = voice

        def stream(self):
            return _Stream(b"FAKE-MP3-BYTES")

    fake.Communicate = _Communicate
    monkeypatch.setitem(__import__("sys").modules, "edge_tts", fake)


@pytest.mark.asyncio
async def test_synthesize_edge_tts_returns_bytes(monkeypatch):
    _patch_edge_tts(monkeypatch)
    from server.core.audio.tts_preview_service import (
        PreviewSpeechParams,
        synthesize_preview_audio,
    )

    data, media_type = await synthesize_preview_audio(
        PreviewSpeechParams(provider_name="edge_tts", voice_name="zh-CN-XiaoxiaoNeural", text="你好")
    )
    assert data == b"FAKE-MP3-BYTES"
    assert media_type == "audio/mpeg"


@pytest.mark.asyncio
async def test_synthesize_empty_text_uses_profile_text(monkeypatch):
    _patch_edge_tts(monkeypatch)
    from server.core.audio.tts_preview_service import (
        PreviewSpeechParams,
        synthesize_preview_audio,
    )
    from fastapi import HTTPException

    # 未知 provider 且无 base_url 时最终走 edge 兜底，空文本会用预置台词
    data, _ = await synthesize_preview_audio(
        PreviewSpeechParams(provider_name="", voice_name="", text="")
    )
    assert data == b"FAKE-MP3-BYTES"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest server/tests/test_tts_preview_service.py -v`
Expected: FAIL — `ModuleNotFoundError: server.core.audio.tts_preview_service`

- [ ] **Step 3: 新建共享合成模块**

`server/core/audio/tts_preview_service.py`：

```python
# -*- coding: utf-8 -*-
"""
TTS 试听音频共享合成服务。
供 /settings/tts/preview (音色试听) 与 /anchors/{id}/avatar/preview-speech-drive
(试播台词驱动) 复用，保证两处音色解析与合成行为 100% 一致。
"""
import json
import logging
from pathlib import Path
from typing import Any, Optional

import httpx
from fastapi import HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from server.config import DATA_DIR, decrypt_secret
from server.database.db import AsyncSessionLocal
from server.database.models import ApiProviderConfig, VoiceProfile

logger = logging.getLogger("LiveAgent.TtsPreview")


class PreviewSpeechParams(BaseModel):
    provider_name: Optional[str] = Field(default="edge_tts", max_length=64)
    model_name: Optional[str] = Field(default=None, max_length=128)
    voice_name: Optional[str] = Field(default=None, max_length=128)
    base_url: Optional[str] = Field(default=None, max_length=512)
    api_key: Optional[str] = Field(default=None, max_length=16_384)
    text: Optional[str] = Field(default="", max_length=2000)


VOICE_PREVIEW_PROFILES = {
    # 与原 settings.py 完全一致的预置音色台词对照表 (key -> (edge_voice, profile_text))
    # NOTE: 从 server/routes/settings.py:1441 原样搬迁，保持两处行为统一。
}
```

> **搬迁说明（执行者必做）**：把 `server/routes/settings.py:1441` 的 `VOICE_PREVIEW_PROFILES = {...}` 整个字典**原样剪切**到本模块（内容逐字不变）。同时把 `settings.py:preview_tts_audio` 函数体（1498-1777）**移动**到本模块并改名为 `synthesize_preview_audio(params: PreviewSpeechParams) -> tuple[bytes, str]`。移动时只改两类东西：

1. 所有 `req.<field>` 改为 `params.<field>`（`req.text`→`params.text`、`req.model_name`→`params.model_name`、`req.base_url`→`params.base_url`、`req.api_key`→`params.api_key`）。
2. 返回值统一改为 `tuple[bytes, str]`，逐站点对照表：

| 原行号 | 原返回 | 新返回 |
|---|---|---|
| 1571 | `return FileResponse(preview_file, media_type=m_type)` | `return preview_file.read_bytes(), m_type` |
| 1636 | `return FileResponse(gen_file, media_type="audio/wav")` | `return gen_file.read_bytes(), "audio/wav"` |
| 1660 | `return Response(content=audio_bytes, media_type="audio/mpeg")` | `return audio_bytes, "audio/mpeg"` |
| 1708 | `return Response(content=cloud_bytes, media_type="audio/mpeg")` | `return cloud_bytes, "audio/mpeg"` |
| 1747 | `return Response(content=resp.content, media_type=media_type)` | `return resp.content, media_type` |
| 1773 | `return Response(content=audio_bytes, media_type="audio/mpeg")` | `return audio_bytes, "audio/mpeg"` |

其余 `raise HTTPException(...)` 站点（1577、1611、1618、1639、1649、1662、1711、1714、1775、1777）**保持原样**。函数签名与依赖导入（`AsyncSessionLocal`、`ApiProviderConfig`、`VoiceProfile`、`DATA_DIR`、`decrypt_secret`、`select`、`Path`、`httpx`、各 provider 的懒加载 import）随体搬迁到本模块顶部。

- [ ] **Step 4: settings 路由改为委托**

把 `server/routes/settings.py` 的 `preview_tts_audio` 替换为：

```python
@router.post("/tts/preview")
async def preview_tts_audio(req: TTSPreviewRequest):
    """
    根据前端当前选型与参数，实时合成一段简短的问候语音流 (MP3/WAV)
    精准匹配每个音色的独特声线与角色台词，让用户在试听切换时清晰感知音色变化
    """
    from server.core.audio.tts_preview_service import (
        PreviewSpeechParams,
        synthesize_preview_audio,
    )

    data, media_type = await synthesize_preview_audio(PreviewSpeechParams(**req.model_dump()))
    return Response(content=data, media_type=media_type)
```

并删除 settings.py 中已搬迁的 `VOICE_PREVIEW_PROFILES` 与 `preview_tts_audio` 旧函数体。确认 settings.py 顶部仍 import 了 `Response`（第 8 行已有）。

- [ ] **Step 5: 跑测试确认通过**

Run: `python -m pytest server/tests/test_tts_preview_service.py -v`
Expected: PASS（2 passed）

- [ ] **Step 6: 回归现有试听测试**

Run: `python -m pytest server/tests/test_api_endpoints.py server/tests/test_moss_tts_integration.py server/tests/test_full_system_verification.py -k "preview or tts" -v`
Expected: 既有 `/settings/tts/preview` 相关断言全部通过（响应仍是音频 bytes，媒体类型不变）。

- [ ] **Step 7: 提交**

```bash
git add server/core/audio/tts_preview_service.py server/routes/settings.py server/tests/test_tts_preview_service.py
git commit -m "refactor(tts): 抽取共享试听合成服务供音色试听与试播驱动复用"
```

---

## Task 2: NeuralLipRenderer 支持返回原始 256 人脸

**Files:**
- Modify: `server/core/avatar/neural_lip_renderer.py`（`render_lip_frame` 344-427；`_crop_face_256` 304-342）
- Test: `server/tests/test_neural_lip_renderer_return_face.py`

- [ ] **Step 1: 写失败测试**

`server/tests/test_neural_lip_renderer_return_face.py`：

```python
# -*- coding: utf-8 -*-
"""NeuralLipRenderer.return_face 契约测试"""
import numpy as np
import pytest


class _FakeSession:
    def __init__(self):
        self._calls = 0

    def get_inputs(self):
        class _I:
            def __init__(self, name, shape):
                self.name = name
                self.shape = shape

        return [_I("face", [1, 6, 256, 256]), _I("mel", [1, 1, 80, 16])]

    def get_outputs(self):
        class _O:
            def __init__(self, name):
                self.name = name

        return [_O("rendered")]

    def get_providers(self):
        return ["CPUExecutionProvider"]

    def run(self, output_names, feed_dict):
        self._calls += 1
        # 返回一张与输入同尺寸的随机人脸 (float [0,1])
        return [np.random.rand(1, 3, 256, 256).astype(np.float32)]


def _make_renderer_with_fake_session(monkeypatch):
    from server.core.avatar import neural_lip_renderer as mod

    monkeypatch.setattr(mod, "ORT_AVAILABLE", True)
    monkeypatch.setattr(mod, "ort", type("ort", (), {"SessionOptions": object}) )
    renderer = mod.NeuralLipRenderer.__new__(mod.NeuralLipRenderer)
    renderer.model_key = "onnx_lipsync"
    renderer.session = _FakeSession()
    renderer.is_ready = True
    renderer.mel_extractor = mod.MelFeatureExtractor()
    renderer.input_names = ["face", "mel"]
    renderer.output_names = ["rendered"]
    renderer.face_input_shape = [1, 6, 256, 256]
    renderer.audio_input_shape = [1, 1, 80, 16]
    renderer.current_anchor_dir = None
    renderer.coords = [(0, 100, 0, 100)]
    renderer.face_imgs = [np.zeros((256, 256, 3), dtype=np.uint8)]
    renderer.full_imgs = []
    renderer.has_anchor_assets = True
    return renderer


def test_return_face_yields_full_and_256(monkeypatch):
    renderer = _make_renderer_with_fake_session(monkeypatch)
    full = np.zeros((200, 200, 3), dtype=np.uint8)
    pcm = (np.sin(np.linspace(0, 40 * np.pi, 3200)) * 0.3).astype(np.float32)
    result = renderer.render_lip_frame(full, 0, pcm, mouth_open=0.8, return_face=True)
    assert isinstance(result, tuple) and len(result) == 2
    blended, face = result
    assert blended.shape[:2] == (200, 200)
    assert face.shape[:2] == (256, 256)


def test_default_return_face_backward_compatible(monkeypatch):
    renderer = _make_renderer_with_fake_session(monkeypatch)
    full = np.zeros((200, 200, 3), dtype=np.uint8)
    pcm = (np.sin(np.linspace(0, 40 * np.pi, 3200)) * 0.3).astype(np.float32)
    result = renderer.render_lip_frame(full, 0, pcm, mouth_open=0.8)
    assert isinstance(result, np.ndarray)  # 单一帧，与既有调用方一致


def test_silent_frame_returns_original_and_face(monkeypatch):
    renderer = _make_renderer_with_fake_session(monkeypatch)
    full = np.zeros((200, 200, 3), dtype=np.uint8)
    full[50:90, 20:80] = 255
    silent = np.zeros(3200, dtype=np.float32)
    blended, face = renderer.render_lip_frame(full, 0, silent, mouth_open=0.0, return_face=True)
    assert np.array_equal(blended, full)  # 静音不改变原帧
    assert face.shape[:2] == (256, 256)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest server/tests/test_neural_lip_renderer_return_face.py -v`
Expected: FAIL — `render_lip_frame() got an unexpected keyword argument 'return_face'`

- [ ] **Step 3: 改造 render_lip_frame**

把 `server/core/avatar/neural_lip_renderer.py:344` 的 `render_lip_frame` 整体替换为：

```python
    def render_lip_frame(
        self,
        full_frame: np.ndarray,
        frame_idx: int,
        pcm_window: np.ndarray,
        mouth_open: float = 0.0,
        override_coord: Optional[Tuple[int, int, int, int]] = None,
        return_face: bool = False,
    ) -> Optional[Union[np.ndarray, Tuple[np.ndarray, np.ndarray]]]:
        """
        执行单帧真实神经唇形重绘管线
        参数:
          - full_frame: 原尺寸底模画面 (BGR 或 RGB)
          - frame_idx: 当前播放帧序号
          - pcm_window: 前后 200ms 的单声道 float32 音频切片 (约 3200 采样点)
          - mouth_open: 当前音频能量，低于静音门限时直接跳过推理
          - override_coord: 外部传入的人脸包围盒 (ymin, ymax, xmin, xmax)。
            用于动作切片等非底图帧：动作帧与待机底片素材不对齐，需显式指定
            当前帧的人脸位置，并从当前帧实时裁剪 256x256 人脸送推理，
            避免把底片素材贴到动作帧的错误位置。
          - return_face: 为 True 时额外返回神经重绘的 256x256 原始人脸，
            供试播主监视舱直接展示神经输出（避免从全图裁剪上采样失真）。
            默认 False，保持真实开播管路调用签名与行为完全不变。
        """
        if not self.is_ready or not self.session:
            return None

        # 外部坐标模式不依赖预切片素材 (动作帧无专属 face_imgs)；底图模式必须有
        if override_coord is None and not self.has_anchor_assets:
            return None

        # 静音、能量极低或音频切片为空时，无需执行深度推理，直接返回原帧保持纯正自然
        if pcm_window is None or len(pcm_window) == 0:
            return self._wrap_silent(full_frame, frame_idx, override_coord, return_face)
        if mouth_open < 0.01 and float(np.max(np.abs(pcm_window))) < 0.01:
            return self._wrap_silent(full_frame, frame_idx, override_coord, return_face)

        if override_coord is not None:
            # 动作帧路径：从当前帧实时裁剪对齐人脸，坐标用外部传入值
            face_256 = self.crop_face_256(full_frame, override_coord)
            if face_256 is None:
                return None
            coord_box = override_coord
        else:
            total_frames = len(self.face_imgs)
            if total_frames == 0 or len(self.coords) == 0:
                return None
            # 循环索引对应切片人脸与坐标
            idx = frame_idx % total_frames
            face_256 = self.face_imgs[idx]
            coord_box = self.coords[idx % len(self.coords)]

        try:
            # 1. 提取 80 维 Mel 窗口 [1, 1, 80, 16]
            mel_tensor = self.mel_extractor.extract_mel_window(pcm_window, target_steps=16)

            # 2. 构建 6 通道输入人脸 [1, 6, 256, 256]
            face_tensor = self._prepare_face_input(face_256)

            # 3. 动态组装输入执行真实前向推理
            feed_dict = {}
            for name in self.input_names:
                if "audio" in name.lower() or "mel" in name.lower():
                    feed_dict[name] = mel_tensor
                else:
                    feed_dict[name] = face_tensor

            out = self.session.run(self.output_names, feed_dict)
            rendered_face = out[0][0]  # shape: (3, 256, 256)

            # 4. 转换维度与色彩为 uint8
            if rendered_face.shape[0] == 3:
                rendered_face = np.transpose(rendered_face, (1, 2, 0))  # (256, 256, 3)

            # 归一化反变换
            if rendered_face.max() <= 1.05:
                rendered_face = np.clip(rendered_face * 255.0, 0, 255).astype(np.uint8)
            else:
                rendered_face = np.clip(rendered_face, 0, 255).astype(np.uint8)

            # 5. 动态无缝羽化融合回贴原帧
            result_frame = full_frame.copy()
            result_frame = self._blend_back(result_frame, rendered_face, coord_box)
            return (result_frame, rendered_face) if return_face else result_frame

        except Exception as e:
            logger.debug(f"神经唇形渲染单帧推理跳过 ({e})，将回退微动态引擎")
            return None

    def _wrap_silent(
        self,
        full_frame: np.ndarray,
        frame_idx: int,
        override_coord: Optional[Tuple[int, int, int, int]],
        return_face: bool,
    ) -> Optional[Union[np.ndarray, Tuple[np.ndarray, np.ndarray]]]:
        """静音帧：跳过推理，返回原帧；需要时附带未形变的对齐人脸"""
        if not return_face:
            return full_frame
        if override_coord is not None:
            face = self.crop_face_256(full_frame, override_coord)
        else:
            total_frames = len(self.face_imgs)
            if total_frames == 0:
                return full_frame
            face = self.face_imgs[frame_idx % total_frames]
        if face is None:
            return full_frame
        return full_frame, face
```

同时把 `_crop_face_256`（304-342）**重命名为 `crop_face_256`**（公共方法），并更新类内唯一调用点（原 379 行 `self._crop_face_256(...)` → 已在上文改为 `self.crop_face_256(...)`）。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest server/tests/test_neural_lip_renderer_return_face.py -v`
Expected: PASS（3 passed）

- [ ] **Step 5: 回归开播管路测试**

Run: `python -m pytest server/tests/ -k "neural or lip or avatar_driver or procedural" -v`
Expected: 全部通过（既有调用方均使用默认 `return_face=False`，行为不变）

- [ ] **Step 6: 提交**

```bash
git add server/core/avatar/neural_lip_renderer.py server/tests/test_neural_lip_renderer_return_face.py
git commit -m "feat(neural): render_lip_frame 支持返回原始256人脸并公开裁剪方法"
```

---

## Task 3: 试播编排服务骨架（解码/帧时序/落盘/清理）

**Files:**
- Create: `server/core/avatar/speech_drive_preview.py`
- Test: `server/tests/test_speech_drive_preview.py`

- [ ] **Step 1: 写失败测试（解码与时序部分）**

`server/tests/test_speech_drive_preview.py`：

```python
# -*- coding: utf-8 -*-
"""试播台词驱动编排服务测试"""
import math
from pathlib import Path

import numpy as np
import pytest


def test_resample_to_16k_mono_passthrough():
    from server.core.avatar.speech_drive_preview import _resample_to_16k_mono

    pcm = np.linspace(-1, 1, 16000, dtype=np.float32)
    out = _resample_to_16k_mono(pcm, 16000)
    assert out.dtype == np.float32
    assert np.allclose(out, pcm)


def test_resample_to_16k_mono_downsample_length():
    from server.core.avatar.speech_drive_preview import _resample_to_16k_mono

    pcm = np.linspace(-1, 1, 48000, dtype=np.float32)
    out = _resample_to_16k_mono(pcm, 48000)
    assert abs(len(out) - 16000) <= 1


def test_frame_count_bounds_by_max_frames():
    from server.core.avatar.speech_drive_preview import FPS, MAX_FRAMES, _compute_frame_count

    # 2 秒音频 -> 50 帧
    assert _compute_frame_count(2.0) == 50
    # 20 秒音频 -> 截断为 MAX_FRAMES
    assert _compute_frame_count(20.0) == MAX_FRAMES
    # 零时长
    assert _compute_frame_count(0.0) == 0
    assert FPS == 25
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest server/tests/test_speech_drive_preview.py -v`
Expected: FAIL — `ModuleNotFoundError: server.core.avatar.speech_drive_preview`

- [ ] **Step 3: 实现服务骨架**

`server/core/avatar/speech_drive_preview.py`：

```python
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
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Sequence

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
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest server/tests/test_speech_drive_preview.py -v`
Expected: PASS（3 passed）

- [ ] **Step 5: 提交**

```bash
git add server/core/avatar/speech_drive_preview.py server/tests/test_speech_drive_preview.py
git commit -m "feat(preview): 试播编排服务骨架-解码/帧时序/落盘/会话清理"
```

---

## Task 4: 本地真实推理路径 + 诚实遥测契约

**Files:**
- Modify: `server/core/avatar/speech_drive_preview.py`（实现 `_resolve_compute_plan`、`_dispatch_render`、本地渲染循环）
- Test: `server/tests/test_speech_drive_preview.py`（追加）

- [ ] **Step 1: 写失败测试（分流与遥测）**

追加到 `server/tests/test_speech_drive_preview.py`：

```python
@pytest.fixture
def fake_asset(tmp_path):
    """构造最小可用资产目录 (1 张 face + 1 张 full + coords)"""
    import pickle

    import cv2

    (tmp_path / "face_imgs").mkdir()
    (tmp_path / "full_imgs").mkdir()
    cv2.imwrite(str(tmp_path / "face_imgs" / "0.jpg"), np.zeros((256, 256, 3), dtype=np.uint8))
    cv2.imwrite(str(tmp_path / "full_imgs" / "0.jpg"), np.zeros((200, 200, 3), dtype=np.uint8))
    with open(tmp_path / "coords.pkl", "wb") as f:
        pickle.dump([(0, 100, 0, 100)], f)
    return tmp_path


@pytest.fixture
def no_gpu(monkeypatch):
    """强制 evaluate_compute 返回本地不可用、云端未配置"""
    from server.core.hardware import gpu_capability as gc

    async def _fake_evaluate(*args, **kwargs):
        from server.core.hardware.gpu_capability import ComputePlan, CloudGpuInfo, LocalGpuInfo

        return ComputePlan(
            feature_name="test",
            required_vram_gb=8,
            use_cloud=False,
            can_execute=False,
            is_low_spec_local=True,
            has_cloud_gpu=False,
            local_gpu=LocalGpuInfo(gpu_name="GT 710", vram_total_gb=1.0, cuda_available=False).to_dict(),
            cloud_gpu=None,
            alert_type="insufficient_hardware",
            user_message="test",
            recommended_driver="procedural",
        )

    monkeypatch.setattr(gc, "evaluate_compute", _fake_evaluate)


def test_render_local_sync_produces_frames(monkeypatch, fake_asset):
    from server.core.avatar.speech_drive_preview import (
        FPS,
        MEL_CONTEXT_SAMPLES,
        _render_local_sync,
    )

    class _FakeRenderer:
        def crop_face_256(self, full, coord):
            return cv2.resize(full, (256, 256))

        def render_lip_frame(self, full, idx, pcm, mouth_open, return_face=False):
            face = cv2.resize(full, (256, 256))
            return (full, face) if return_face else full

    pcm = (np.sin(np.linspace(0, 100 * np.pi, 16000)) * 0.2).astype(np.float32)  # 1s
    full_imgs = [cv2.imread(str(fake_asset / "full_imgs" / "0.jpg"))]
    renderer = _FakeRenderer()
    face_out, full_out, timings = _render_local_sync(renderer, pcm, 25, full_imgs)
    assert len(face_out) == 25 and len(full_out) == 25
    assert all(f.shape[:2] == (256, 256) for f in face_out)
    assert len(timings) == 25 and all(t >= 0.0 for t in timings)


@pytest.mark.asyncio
async def test_service_falls_back_honestly(monkeypatch, fake_asset, no_gpu):
    """无 GPU 且无云端时必须诚实降级，且不得伪造设备信息"""
    from server.core.avatar.speech_drive_preview import (
        ENGINE_FALLBACK,
        get_speech_drive_preview_service,
    )

    service = get_speech_drive_preview_service()
    service._sessions_root = fake_asset / "sessions"
    # 降级渲染用真实素材路径
    audio = _silence_mp3()
    result = await service.run("anchor_test", fake_asset, audio)
    assert result.engine == ENGINE_FALLBACK
    assert result.mode == "fallback"
    assert result.device == ""
    assert result.providers == []
    assert result.fallback_reason is not None
    assert result.frame_count > 0


def _silence_mp3() -> bytes:
    """生成一段可被 soundfile 解码的最小 WAV（测试统一用 wav 容器）"""
    import io

    import soundfile as sf  # noqa: PLC0415  (测试环境依赖)

    buf = io.BytesIO()
    sf.write(buf, np.zeros(int(16000 * 1.0), dtype=np.float32), 16000, format="WAV")
    return buf.getvalue()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest server/tests/test_speech_drive_preview.py -v`
Expected: FAIL — `_resolve_compute_plan` / `_dispatch_render` 仍为 NotImplementedError；`_render_local_sync` 未定义

- [ ] **Step 3: 实现分流与本地渲染**

在 `server/core/avatar/speech_drive_preview.py` 中替换 Task 3 的两个 `NotImplementedError` 方法，并新增同步渲染循环与降级渲染：

```python
    # ------------------------------------------------------------------
    # 算力分流
    # ------------------------------------------------------------------
    async def _resolve_compute_plan(self):
        from server.core.hardware.gpu_capability import evaluate_compute

        return await evaluate_compute(feature_name="试播台词驱动")

    async def _dispatch_render(self, plan, asset_dir, pcm, n_frames):
        """返回 (outcome, engine, mode, fallback_reason)。云端失败自动回退本地。"""
        local_ok = bool(
            (plan.local_gpu or {}).get("cuda_available")
            and float((plan.local_gpu or {}).get("vram_total_gb") or 0) >= 2.0
        )
        cloud = getattr(plan, "cloud_gpu", None)
        cloud_ok = bool(plan.use_cloud and plan.has_cloud_gpu and cloud and cloud.is_reachable)

        if cloud_ok:
            try:
                outcome = await self._render_cloud(cloud, asset_dir, pcm, n_frames)
                if outcome is not None and outcome.full_frames:
                    return outcome, ENGINE_CLOUD, "cloud", None
                logger.warning("云端试播渲染未产出帧，自动回退本地引擎")
            except Exception as e:
                logger.warning(f"云端试播渲染失败，自动回退本地引擎: {e}")
            if local_ok:
                outcome = self._render_local(asset_dir, pcm, n_frames)
                if outcome is not None:
                    return outcome, ENGINE_LOCAL, "local", "sidecar_unreachable"

        if local_ok:
            outcome = self._render_local(asset_dir, pcm, n_frames)
            if outcome is not None:
                return outcome, ENGINE_LOCAL, "local", None
            return self._render_fallback(asset_dir, n_frames), ENGINE_FALLBACK, "fallback", "render_error"

        # 本地无 CUDA：仍尝试 CPU 推理，模型缺失/异常时诚实降级
        outcome = self._render_local(asset_dir, pcm, n_frames, allow_cpu=True)
        if outcome is not None:
            return outcome, ENGINE_LOCAL, "local", "cuda_unavailable_cpu_fallback"
        reason = self._diagnose_local_failure()
        return self._render_fallback(asset_dir, n_frames), ENGINE_FALLBACK, "fallback", reason

    # ------------------------------------------------------------------
    # 本地 ONNX 推理
    # ------------------------------------------------------------------
    def _render_local(
        self,
        asset_dir: Path,
        pcm: np.ndarray,
        n_frames: int,
        allow_cpu: bool = False,
    ) -> Optional[RenderOutcome]:
        from server.core.avatar.neural_lip_renderer import NeuralLipRenderer
        from server.core.avatar.neural_model_manager import global_neural_model_manager

        if not global_neural_model_manager.is_model_available("onnx_lipsync"):
            logger.info("神经唇形模型未安装，试播跳过本地推理")
            return None
        try:
            from server.core.cpu_worker import run_cpu_bound

            outcome = run_cpu_bound(_render_local_sync, asset_dir.as_posix(), pcm, n_frames, allow_cpu)
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
```

在模块级新增以下辅助函数（`_render_local_sync` 必须是模块级纯函数，供 `run_cpu_bound` 线程内执行）：

```python
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


def _render_local_sync(asset_dir: str, pcm: np.ndarray, n_frames: int, allow_cpu: bool = False) -> Optional[RenderOutcome]:
    """线程内同步执行：构建 renderer -> 载入资产 -> 25FPS 逐帧真实推理"""
    from server.core.avatar.neural_lip_renderer import NeuralLipRenderer

    asset_path = Path(asset_dir)
    renderer = NeuralLipRenderer()
    if not renderer.is_ready:
        return None
    if not renderer.load_anchor_assets(asset_path):
        return None

    full_imgs = _load_full_imgs(asset_path)
    if not full_imgs:
        return None

    outcome = RenderOutcome()
    outcome.providers = list(getattr(renderer, "session").get_providers() if renderer.session else [])
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
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest server/tests/test_speech_drive_preview.py -v`
Expected: PASS（5 passed）—— 注意 `test_render_local_sync_produces_frames` 用的 `_FakeRenderer` 只要 `render_lip_frame(..., return_face=True)` 返回元组即可。

- [ ] **Step 5: 提交**

```bash
git add server/core/avatar/speech_drive_preview.py server/tests/test_speech_drive_preview.py
git commit -m "feat(preview): 本地ONNX真实推理路径与ADR-16诚实遥测降级"
```

---

## Task 5: HTTP 端点与会话静态帧路由

**Files:**
- Modify: `server/routes/anchors.py`
- Test: `server/tests/test_speech_drive_preview.py`（追加端点集成测试）

- [ ] **Step 1: 写失败测试（端点契约）**

追加到 `server/tests/test_speech_drive_preview.py`：

```python
import respx  # noqa: F401  (若项目无 respx，改用 monkeypatch httpx)


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from server.app import app

    return TestClient(app)


@pytest.fixture
def anchor_with_asset(tmp_path, fake_asset):
    """在 DB 注册一个主播，资产指向临时目录"""
    from server.database.db import AsyncSessionLocal
    from server.database.models import Anchor

    import asyncio

    async def _seed():
        async with AsyncSessionLocal() as db:
            db.add(Anchor(id="anchor_demo", name="演示主播", avatar_asset_dir=str(fake_asset)))
            await db.commit()

    asyncio.get_event_loop().run_until_complete(_seed()) if False else None
    return "anchor_demo"


def test_endpoint_rejects_missing_anchor(client):
    res = client.post("/api/v1/anchors/no_such_anchor/avatar/preview-speech-drive", json={"text": "你好"})
    assert res.status_code == 404


def test_endpoint_rejects_empty_text(client):
    res = client.post("/api/v1/anchors/anchor_demo/avatar/preview-speech-drive", json={"text": "   "})
    assert res.status_code == 422


def test_endpoint_rejects_too_long_text(client):
    res = client.post(
        "/api/v1/anchors/anchor_demo/avatar/preview-speech-drive",
        json={"text": "字" * 501},
    )
    assert res.status_code == 422
```

> **执行者注意**：`anchor_with_asset` fixture 需按项目现有测试的 DB 引导方式补全（参考 `server/tests/test_api_endpoints.py` 的数据库 fixture）。上面留了占位，请改用项目既有 DB 初始化工具（如 `server/tests/conftest.py` 中的 fixture 或直接建表 + 提交），确保 `anchor_demo` 存在且 `avatar_asset_dir` 指向 `fake_asset`。

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest server/tests/test_speech_drive_preview.py -v -k endpoint`
Expected: FAIL — 404 路由不存在（FastAPI 默认 404 而非我们的业务 404，断言 status_code 行为一致即可，先确认路由未注册：`AttributeError`/404）

- [ ] **Step 3: 实现端点**

在 `server/routes/anchors.py` 顶部新增导入：

```python
import re
from server.core.audio.tts_preview_service import PreviewSpeechParams, synthesize_preview_audio
from server.core.avatar.speech_drive_preview import get_speech_drive_preview_service
```

在文件中新增（建议放在 `/{anchor_id}/avatar-asset-frame` 路由之后）：

```python
# ---------------------------------------------------------------------------
# 试播台词驱动：真实算力闭环 (方案 C)
# ---------------------------------------------------------------------------
PREVIEW_SESSION_ROOT = DATA_DIR / "preview_speech_sessions"
_SESSION_ID_RE = re.compile(r"^\d{20}$")  # %Y%m%d%H%M%S%f


class PreviewSpeechDriveRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=500, description="试听台词")
    provider_name: Optional[str] = Field(default=None, max_length=64)
    voice_name: Optional[str] = Field(default=None, max_length=128)


def _resolve_preview_provider(anchor: Anchor, voice_name: Optional[str]) -> str:
    """与前端一致的音色引擎智能匹配（voice_id 前缀启发式）"""
    voice_id = (voice_name or anchor.voice_id or "").strip()
    if voice_id.startswith("voice_moss_") or voice_id.startswith("moss_"):
        return "moss_tts_nano"
    if "bailian" in voice_id or "cosyvoice" in voice_id or voice_id.startswith("long"):
        return "cosyvoice"
    if "eleven" in voice_id:
        return "elevenlabs"
    return "moss_tts_nano"


@router.post("/{anchor_id}/avatar/preview-speech-drive")
async def preview_speech_drive(anchor_id: str, req: PreviewSpeechDriveRequest):
    """
    试播台词驱动：唤醒真实神经渲染引擎（本地 ONNX / 云端 Sidecar），
    逐帧重绘口型并回传帧地址与实测硬件遥测。
    """
    from server.database.db import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        anchor = await db.get(Anchor, anchor_id)
    if not anchor:
        raise HTTPException(status_code=404, detail="主播不存在")

    asset_dir = (anchor.avatar_asset_dir or "").strip()
    ad = Path(asset_dir) if asset_dir else None
    if (
        not ad
        or not ad.exists()
        or not (ad / "face_imgs").exists()
        or not (ad / "coords.pkl").exists()
        or not (ad / "full_imgs").exists()
    ):
        raise HTTPException(
            status_code=409,
            detail="该主播尚未完成数字人资产训练，请先完成切片生成",
        )

    text = (req.text or "").strip()
    if not text or len(text) > 500:
        raise HTTPException(status_code=422, detail="台词长度必须为 1 到 500 字符")

    provider = (req.provider_name or "").strip() or _resolve_preview_provider(anchor, req.voice_name)
    try:
        audio_bytes, _media_type = await synthesize_preview_audio(
            PreviewSpeechParams(
                provider_name=provider,
                voice_name=(req.voice_name or anchor.voice_id or "").strip() or None,
                text=text,
            )
        )
    except HTTPException as e:
        raise HTTPException(status_code=503, detail=e.detail)
    except Exception as e:
        logger.warning(f"试播 TTS 合成失败: {e}", exc_info=True)
        raise HTTPException(status_code=503, detail="主播绑定的音色有误，请检查")
    if not audio_bytes:
        raise HTTPException(status_code=503, detail="语音引擎返回音频为空，请检查音色配置")

    service = get_speech_drive_preview_service()
    try:
        result = await service.run(
            anchor_id=anchor_id,
            asset_dir=ad,
            audio_bytes=audio_bytes,
        )
    except Exception as e:
        logger.warning(f"试播渲染失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="试播驱动失败，请重试")

    base = f"/api/v1/anchors/{anchor_id}/preview-sessions/{result.session_id}"
    data: dict = {
        "engine": result.engine,
        "mode": result.mode,
        "device": result.device,
        "providers": result.providers,
        "mean_inference_ms": result.mean_inference_ms,
        "total_inference_ms": result.total_inference_ms,
        "fps": result.fps,
        "frame_count": result.frame_count,
        "audio_url": f"{base}/audio.mp3",
        "face_frames": [f"{base}/face/{i}.jpg" for i in range(result.frame_count)],
        "full_frames": [f"{base}/full/{i}.jpg" for i in range(result.frame_count)],
        "fallback_reason": result.fallback_reason,
    }
    if result.vram_total_gb is not None:
        data["vram_total_gb"] = result.vram_total_gb
    return {"code": 0, "data": data}


@router.get("/{anchor_id}/preview-sessions/{session_id}/audio.mp3")
async def get_preview_session_audio(anchor_id: str, session_id: str):
    return _serve_preview_asset(anchor_id, session_id, None, "audio.mp3")


@router.get("/{anchor_id}/preview-sessions/{session_id}/{kind}/{idx}.jpg")
async def get_preview_session_frame(anchor_id: str, session_id: str, kind: str, idx: str):
    if kind not in ("face", "full"):
        raise HTTPException(status_code=404, detail="帧类型不存在")
    return _serve_preview_asset(anchor_id, session_id, kind, f"{idx}.jpg")


def _serve_preview_asset(anchor_id: str, session_id: str, kind: Optional[str], filename: str):
    """只读会话资产，严格防路径穿越"""
    if not _SESSION_ID_RE.match(session_id):
        raise HTTPException(status_code=404, detail="会话不存在")
    if "/" in filename or "\\" in filename or filename.startswith("."):
        raise HTTPException(status_code=404, detail="资产不存在")
    if idx_invalid(filename):
        raise HTTPException(status_code=404, detail="帧不存在")

    parts = [PREVIEW_SESSION_ROOT, anchor_id, session_id]
    if kind:
        parts.append(kind)
    target = Path(*parts) / filename
    if not target.is_file():
        raise HTTPException(status_code=404, detail="资产不存在")
    media = "audio/mpeg" if filename.endswith(".mp3") else "image/jpeg"
    return FileResponse(target.as_posix(), media_type=media)


def idx_invalid(filename: str) -> bool:
    """帧文件名必须是 纯数字.jpg"""
    if not filename.endswith(".jpg"):
        return filename != "audio.mp3"
    stem = filename[:-4]
    return not (stem.isdigit() and int(stem) >= 0)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest server/tests/test_speech_drive_preview.py -v -k endpoint`
Expected: PASS（3 passed：404/422/422）

- [ ] **Step 5: 端到端冒烟（成功路径）**

补一个成功路径测试：mock `synthesize_preview_audio` 返回静音 WAV bytes（`_silence_mp3()`），以 `client` POST 端点，断言 `code==0`、`frame_count>0`、`face_frames` 长度与 `frame_count` 一致、`audio_url` 可 GET 到 200。因为静音音频在本地无模型环境会走 `procedural_fallback`，`engine` 断言为 `procedural_fallback` 且 `fallback_reason == "model_not_installed"`。

Run: `python -m pytest server/tests/test_speech_drive_preview.py -v`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add server/routes/anchors.py server/tests/test_speech_drive_preview.py
git commit -m "feat(preview): 试播台词驱动HTTP端点与会话帧路由"
```

---

## Task 6: 云端 Sidecar 批量推理路径 + 自动回退

**Files:**
- Modify: `server/core/avatar/speech_drive_preview.py`（实现 `_render_cloud`）
- Modify: `server/core/avatar/speech_drive_preview.py`（新增 AudioFrame 批次构造）
- Test: `server/tests/test_speech_drive_preview.py`（追加云端测试）

- [ ] **Step 1: 写失败测试（云端批量收集 + 失败回退）**

追加到 `server/tests/test_speech_drive_preview.py`：

```python
@pytest.mark.asyncio
async def test_render_cloud_collects_frames(monkeypatch, fake_asset):
    """mock 驱动：验证云端会话收集 JPEG 帧并按序号排序"""
    from server.core.avatar.speech_drive_preview import (
        SpeechDrivePreviewService,
        _build_audio_frame_batch,
    )

    pcm = (np.sin(np.linspace(0, 100 * np.pi, 16000)) * 0.2).astype(np.float32)
    frames = _build_audio_frame_batch(pcm)
    assert frames[0].is_first and frames[-1].is_final
    assert frames[-1].pts_samples + frames[-1].duration_samples == len(pcm)

    class _FakeDriver:
        def __init__(self):
            self.started = False
            self.stopped = False
            self._timeline_task = None
            self._selected_descriptor = {"model_version": "musetalk-0.1", "device": "NVIDIA RTX 4090"}

        async def start(self):
            self.started = True

        async def stop(self):
            self.stopped = True

        async def feed_audio_frames(self, frames):
            # 模拟 sidecar 回传 2 帧后完成
            for i in range(2):
                ok, buf = cv2.imencode(".jpg", np.full((64, 64, 3), 30 + i, dtype=np.uint8))
                self._publish(buf.tobytes(), None)

        def _publish(self, jpeg, owner):
            self._publish_sink.append(jpeg)

        _publish_sink: list = []

    service = SpeechDrivePreviewService()
    service._sessions_root = fake_asset / "sessions"

    async def _fake_resolve(cloud_gpu):
        return _FakeConnection()

    monkeypatch.setattr(service, "_resolve_sidecar_connection", _fake_resolve)

    cloud = _fake_cloud_info()
    outcome = await service._render_cloud(cloud, fake_asset, pcm, 2)
    assert outcome is not None
    assert len(outcome.full_frames) == 2
    assert all(f.shape[0] == 64 for f in outcome.full_frames)
    assert len(outcome.face_frames) == 2


@pytest.mark.asyncio
async def test_render_cloud_failure_returns_none(monkeypatch, fake_asset):
    """握手失败时返回 None，交由上层回退本地/降级"""
    from server.core.avatar.speech_drive_preview import SpeechDrivePreviewService

    class _BoomDriver:
        async def start(self):
            raise RuntimeError("connection refused")

        async def stop(self):
            pass

    service = SpeechDrivePreviewService()

    async def _fake_resolve(cloud_gpu):
        return _FakeConnection(driver=_BoomDriver())

    monkeypatch.setattr(service, "_resolve_sidecar_connection", _fake_resolve)
    outcome = await service._render_cloud(_fake_cloud_info(), fake_asset, np.zeros(1600, dtype=np.float32), 1)
    assert outcome is None


def _fake_cloud_info():
    from server.core.hardware.gpu_capability import CloudGpuInfo

    return CloudGpuInfo(
        configured=True,
        provider_name="sidecar_v3",
        adapter="sidecar_v3",
        base_url="ws://127.0.0.1:8890/ws/render-v3",
        is_active=True,
        is_reachable=True,
        api_key="",
    )


@dataclass
class _FakeConnection:
    driver: object | None = None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest server/tests/test_speech_drive_preview.py -v -k cloud`
Expected: FAIL — `_build_audio_frame_batch` 与 `_render_cloud` 未定义

- [ ] **Step 3: 实现云端路径**

在 `server/core/avatar/speech_drive_preview.py` 顶部补导入：

```python
from dataclasses import dataclass
```
（已有则跳过）

新增模块级帧批次构造与连接解析：

```python
def _build_audio_frame_batch(pcm16k: np.ndarray):
    """把 16k 单声道 PCM 切分为 sidecar v3 要求的连续 AudioFrame 批次（每帧 400ms）"""
    from server.core.media.audio_frame import AudioFrame, AudioFormat, validate_audio_frame_batch

    fmt = AudioFormat(codec="pcm_s16le", sample_rate=16000, channels=1)
    int16 = np.clip(pcm16k * 32767.0, -32768.0, 32767.0).astype(np.int16)
    frame_samples = 16000 * 0.4  # 400ms/帧，低于 1s 上限
    audio_id = uuid.uuid4().hex
    frames = []
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
```

在 `SpeechDrivePreviewService` 中实现 `_render_cloud` 与连接解析（替换 Task 4 中云端分支的占位）：

```python
    # ------------------------------------------------------------------
    # 云端 Sidecar 批量推理
    # ------------------------------------------------------------------
    async def _resolve_sidecar_connection(self, cloud_gpu) -> Optional[SidecarConnection]:
        """复用 evaluate_compute 的探活结果，从同一 DB 记录取连接参数"""
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
        canonical = normalize_avatar_provider_config(
            extra,
            base_url=base_url,
            credential_present=bool(str(credential).strip()),
        )
        return SidecarConnection(
            node_url=base_url,
            auth_token=str(credential),
            canonical=canonical,
            model_name=str(getattr(record, "model_name", "") or "auto"),
        )

    async def _render_cloud(self, cloud_gpu, asset_dir: Path, pcm: np.ndarray, n_frames_hint: int) -> Optional[RenderOutcome]:
        from server.adapters.media.neural_sidecar_driver import NeuralSidecarMediaDriver

        conn = await self._resolve_sidecar_connection(cloud_gpu)
        if conn is None:
            logger.warning("无法解析云端 sidecar 连接参数，跳过云端试播")
            return None

        frames = _build_audio_frame_batch(pcm)
        driver = NeuralSidecarMediaDriver(
            node_url=conn.node_url,
            auth_token=conn.auth_token,
            backend_id=str(conn.canonical.get("backend_id") or conn.model_name or "auto"),
            avatar_id=str(conn.canonical.get("avatar_id") or "default"),
            avatar_revision=str(conn.canonical.get("avatar_revision") or ""),
            avatar_digest=str(conn.canonical.get("avatar_digest") or ""),
            license_manifest_digest=str(conn.canonical.get("license_manifest_digest") or ""),
            weights_sha256=str(conn.canonical.get("weights_sha256") or ""),
            model_version=str(conn.canonical.get("model_version") or ""),
            require_neural_lipsync=conn.canonical.get("require_neural_lipsync") is True,
        )

        collected: list[bytes] = []

        def _collect(jpeg: bytes, owner) -> None:
            # 预览专用收集器：不接管虚拟摄像头/直播画面，仅缓存 JPEG
            if jpeg:
                collected.append(jpeg)

        # 包裹自己的实例（预览会话独占，不与直播管路共享连接）
        driver._publish_video_frame = _collect  # noqa: SLF001

        started = time.monotonic()
        try:
            await driver.start()
        except Exception as e:
            logger.warning(f"云端 sidecar 握手失败: {e}")
            return None

        try:
            await driver.feed_audio_frames(frames)
            # 时间线 consumer 按实时节奏发帧，等待其排空
            timeline_task = getattr(driver, "_timeline_task", None)
            if timeline_task is not None and not timeline_task.done():
                timeout = (len(pcm) / 16000.0) + 10.0
                await asyncio.wait_for(asyncio.shield(timeline_task), timeout=timeout)
        finally:
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
```

> **说明**：`test_render_cloud_collects_frames` 中的 `_FakeDriver` 无 `_timeline_task` 属性，`getattr(driver, "_timeline_task", None)` 返回 None，跳过等待，直接从 `_publish_sink` 收集。注意 fake 的 `_publish` 写入 `_publish_sink`；真实驱动被替换为 `_collect`。执行者在跑测试前请把 `_FakeConnection.driver` 的 `_publish` 也接到 `collected`——最简做法：让 `_FakeConnection` 直接返回一个具备 `start/stop/feed_audio_frames` 的 driver，并在测试中把 `service._render_cloud` 内的 `driver._publish_video_frame` 赋值对 fake 生效（fake 需暴露该属性）。若 fake 不支持属性赋值，请改为在 fake 类里加 `def __setattr__` 透传或直接在类内持有 `_publish_video_frame = None` 字段后由服务端赋值覆盖。**测试以真实驱动接口为准，fake 需补齐该字段。**

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest server/tests/test_speech_drive_preview.py -v -k cloud`
Expected: PASS（2 passed）

- [ ] **Step 5: 回归全量服务测试**

Run: `python -m pytest server/tests/test_speech_drive_preview.py -v`
Expected: 全部 PASS

- [ ] **Step 6: 提交**

```bash
git add server/core/avatar/speech_drive_preview.py server/tests/test_speech_drive_preview.py
git commit -m "feat(preview): 云端Sidecar批量推理路径与握手失败自动回退"
```

---

## Task 7: 前端切换到真实帧 + 真实遥测徽章

**Files:**
- Modify: `server/static/js/modules/13_anchors.js`（`testAnchorSpeechDemo` 1388-1550；删除假驱动函数群 1099-1299；调整 `renderStreamFrameAt`/`renderSingleFaceLipSync`）
- Modify: `server/static/components/tab_anchors.html`（删除 `<canvas id="slice-face-canvas">`）
- Modify: `server/static/index.html`（同上，保持与组件同步）

- [ ] **Step 1: 删除假 Canvas 驱动代码**

在 `server/static/js/modules/13_anchors.js` 中删除以下整段（约 1099-1299）：
- `_liveAudioBuffer`、`_liveAudioCtx`、`_isSpeechDrivingActive`、`_prevMouthOpen`、`_prevMouthForm`、`_offscreenLowerCanvas`、`_lipSyncRafId` 变量声明
- `startContinuousLipSyncLoop()`、`resetLiveLipSyncState()`、`calculateLiveViseme()`、`renderLiveLipSyncOnCanvas()`、`updateLiveLipSyncUI()`、`renderSingleFaceLipSync()`

保留 `_demoAudioPlayer` 声明（若仅在此使用，迁到新函数附近）。

同时修改 `renderStreamFrameAt()`：删除其中调用 `renderLiveLipSyncOnCanvas` 的块（约 1376-1382），保留 `fullImg.src` / `faceImg.src` / bbox 更新逻辑。

修改 `startSliceAnimationPlay()`：删除 `else: renderSingleFaceLipSync()` 分支（约 1316-1319），改为空操作（切片流为空时静默等待）。

- [ ] **Step 2: 重写 testAnchorSpeechDemo**

把 `13_anchors.js:1388` 的 `testAnchorSpeechDemo` 整体替换为：

```javascript
// -----------------------------------------------------------------------------
// 试播主播台词驱动演示 · 真实算力闭环 (后端神经推理 → 前端播放真实帧)
// -----------------------------------------------------------------------------
let _demoAudioPlayer = null;
let _previewPlaying = false;

async function testAnchorSpeechDemo() {
    const textEl = document.getElementById("preview-speech-text");
    const statusEl = document.getElementById("preview-speech-status");
    const btnEl = document.getElementById("btn-preview-speech");
    const bboxBox = document.getElementById("slice-bbox-box");

    const text = (textEl ? textEl.value : "").trim();
    if (!text) {
        alert("请输入测试台词");
        return;
    }

    const d = _currentPreviewDetail || {};
    let voiceId = d.voice_id || "";
    let providerName = d.voice_provider || "";

    if (!providerName && window._voiceProfilesCache && Array.isArray(window._voiceProfilesCache)) {
        const matchedVoice = window._voiceProfilesCache.find(v => v.id === voiceId || v.name === voiceId);
        if (matchedVoice && matchedVoice.provider_name) {
            providerName = matchedVoice.provider_name;
        }
    }
    if (!providerName) {
        if (voiceId.startsWith("voice_moss_") || voiceId.startsWith("moss_")) {
            providerName = "moss_tts_nano";
        } else if (voiceId.includes("bailian") || voiceId.includes("cosyvoice") || voiceId.startsWith("long")) {
            providerName = "cosyvoice";
        } else if (voiceId.includes("eleven")) {
            providerName = "elevenlabs";
        } else {
            providerName = "moss_tts_nano";
        }
    }

    switchPreviewSubTab('slices');

    if (statusEl) {
        statusEl.style.color = "#38bdf8";
        statusEl.innerHTML = `⏳ 正在唤醒神经口型驱动引擎，合成音频并执行真实推理...`;
    }
    if (btnEl) btnEl.disabled = true;

    try {
        const res = await fetch(`${API_BASE}/anchors/${d.id}/avatar/preview-speech-drive`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                text: text,
                provider_name: providerName,
                voice_name: voiceId || null
            })
        });

        if (!res.ok) {
            let errorDetail = "";
            try {
                const errJson = await res.json();
                errorDetail = errJson.detail || errJson.message || "";
            } catch (_) {
                errorDetail = await res.text().catch(() => "");
            }

            let conciseMsg = "主播绑定的音色有误，请检查";
            const detailLower = (errorDetail || "").toLowerCase();
            if (res.status === 404 || detailLower.includes("不存在") || detailLower.includes("未找到")) {
                conciseMsg = "主播不存在，请刷新页面重试";
            } else if (res.status === 409 || detailLower.includes("资产")) {
                conciseMsg = "该主播尚未完成数字人资产训练，请先完成切片生成";
            } else if (res.status === 422) {
                conciseMsg = "台词长度必须为 1 到 500 字符";
            } else if (res.status === 503 || detailLower.includes("音色")) {
                conciseMsg = "主播绑定的音色有误，请检查";
            } else if (detailLower.includes("网络") || detailLower.includes("timeout") || detailLower.includes("failed to fetch")) {
                conciseMsg = "网络连接超时，无法连接语音服务";
            } else if (errorDetail && errorDetail.length <= 25) {
                conciseMsg = errorDetail;
            }

            const err = new Error(conciseMsg);
            err.rawDetail = errorDetail;
            throw err;
        }

        const json = await res.json();
        const data = json.data || {};

        // 预加载全部帧，消除播放期解码卡顿
        const faceImgs = (data.face_frames || []).map(url => {
            const img = new Image();
            img.src = url;
            return img;
        });
        const fullImgs = (data.full_frames || []).map(url => {
            const img = new Image();
            img.src = url;
            return img;
        });

        if (_demoAudioPlayer) {
            _demoAudioPlayer.pause();
            _demoAudioPlayer = null;
        }

        _demoAudioPlayer = new Audio(data.audio_url);
        _previewPlaying = true;
        const frameCount = Math.max(1, data.frame_count || faceImgs.length);

        _demoAudioPlayer.onended = () => {
            _previewPlaying = false;
            if (statusEl) {
                statusEl.style.color = "#10b981";
                statusEl.innerHTML = `✓ 试听驱动演示完毕 · 神经引擎: <strong>${escapeHtml(data.device || data.engine)}</strong> · 单帧 <strong>${data.mean_inference_ms}ms</strong> · 共 ${frameCount} 帧`;
            }
            if (bboxBox) bboxBox.style.boxShadow = "none";
            if (btnEl) btnEl.disabled = false;
        };

        _demoAudioPlayer.onerror = () => {
            _previewPlaying = false;
            if (statusEl) {
                statusEl.style.color = "#f87171";
                statusEl.innerHTML = "⚠️ 音频播放异常，请重试";
            }
            if (btnEl) btnEl.disabled = false;
        };

        // 真实遥测徽章 (引擎/设备/帧耗时，全部来自后端实测)
        updateSpeechDriveBadge(data);

        if (bboxBox) bboxBox.style.boxShadow = "0 0 18px rgba(16, 185, 129, 0.95)";
        await _demoAudioPlayer.play();

        // 25 FPS 帧推进：以音频时钟为准
        const faceImgEl = document.getElementById("slice-face-img");
        const fullImgEl = document.getElementById("slice-full-img");
        const FPS = data.fps || 25;

        function flip() {
            if (!_previewPlaying || !_demoAudioPlayer || _demoAudioPlayer.paused) return;
            const idx = Math.min(frameCount - 1, Math.floor(_demoAudioPlayer.currentTime * FPS));
            if (faceImgs[idx] && faceImgEl && faceImgs[idx].complete) {
                faceImgEl.src = faceImgs[idx].src;
            }
            if (fullImgs[idx] && fullImgEl && fullImgs[idx].complete) {
                fullImgEl.src = fullImgs[idx].src;
            }
            requestAnimationFrame(flip);
        }
        requestAnimationFrame(flip);

        if (statusEl) {
            statusEl.style.color = "#34d399";
            const engineText = data.engine === "neural_cloud_sidecar" ? "云端 GPU 神经渲染"
                : data.engine === "neural_local_onnx" ? "本地 ONNX 神经渲染"
                : "微动态降级 (神经引擎未就绪)";
            statusEl.innerHTML = `🔊 正在试听发音 (声线: <strong>${escapeHtml(d.voice_name || voiceId || '默认')}</strong> · <strong>${escapeHtml(engineText)}</strong>${data.device ? ` · ${escapeHtml(data.device)}` : ""} · ${data.mean_inference_ms}ms/帧)`;
        }
    } catch (e) {
        _previewPlaying = false;
        let msg = e.message || "主播绑定的音色有误，请检查";
        if (msg === "Failed to fetch") {
            msg = "无法连接至后端服务，请确认服务已启动";
        }
        if (statusEl) {
            statusEl.style.color = "#fbbf24";
            const detailTip = e.rawDetail ? ` title="${escapeHtml(e.rawDetail)}"` : "";
            statusEl.innerHTML = `<span${detailTip}>⚠️ <strong>试听驱动失败：</strong>${escapeHtml(msg)}</span>`;
        }
    } finally {
        if (btnEl) btnEl.disabled = false;
    }
}

function updateSpeechDriveBadge(data) {
    const badge = document.getElementById("slice-face-gpu-badge");
    const syncBadge = document.getElementById("slice-face-sync-badge");
    if (!badge) return;

    if (data.engine === "neural_cloud_sidecar") {
        badge.style.background = "rgba(56, 189, 248, 0.15)";
        badge.style.borderColor = "rgba(56, 189, 248, 0.35)";
        badge.style.color = "#38bdf8";
        badge.innerHTML = `⚡ <span style="font-weight:700;">[云端显卡]</span> ${escapeHtml(data.device || "远端 GPU")} <span style="font-size:9.5px;opacity:0.85;">(${data.mean_inference_ms}ms/帧)</span>`;
    } else if (data.engine === "neural_local_onnx") {
        badge.style.background = "rgba(16, 185, 129, 0.15)";
        badge.style.borderColor = "rgba(16, 185, 129, 0.35)";
        badge.style.color = "#34d399";
        badge.innerHTML = `🟢 <span style="font-weight:700;">[本地独显]</span> ${escapeHtml(data.device || "ONNX")} <span style="font-size:9.5px;opacity:0.85;">(${data.mean_inference_ms}ms/帧)</span>`;
    } else {
        // 诚实降级：绝不声称 GPU，引导用户前往设置页
        badge.style.background = "rgba(251, 191, 36, 0.15)";
        badge.style.borderColor = "rgba(251, 191, 36, 0.35)";
        badge.style.color = "#fbbf24";
        const reason = data.fallback_reason === "model_not_installed"
            ? "神经模型未下载"
            : (data.fallback_reason === "sidecar_unreachable" ? "云端节点未连通" : "神经引擎未就绪");
        badge.innerHTML = `⚠️ <span style="font-weight:700;">[已回退]</span> ${escapeHtml(reason)} · 前往【GPU算力配置】启用`;
    }

    if (syncBadge) {
        syncBadge.style.display = "block";
        syncBadge.innerHTML = `● 真实神经驱动中 (${data.frame_count || 0} 帧)`;
    }

    const tip = document.getElementById("slice-face-tip");
    if (tip) {
        tip.style.borderColor = "rgba(16, 185, 129, 0.5)";
        tip.style.background = "rgba(16, 185, 129, 0.12)";
        tip.style.color = "#34d399";
        tip.innerHTML = `🔊 <strong style="color:#10b981;">真实神经重绘:</strong> 引擎 <strong>${escapeHtml(data.engine)}</strong>${data.device ? ` · ${escapeHtml(data.device)}` : ""} · 单帧 <strong>${data.mean_inference_ms}ms</strong>`;
    }
}
```

- [ ] **Step 3: 删除 HTML 中的无用 canvas 元素**

在 `server/static/components/tab_anchors.html` 中删除这一行（约 737 行）：

```html
<canvas id="slice-face-canvas" width="256" height="256" ...></canvas>
```

在 `server/static/index.html` 中删除对应的同一行（约 1414 行，聚合页保持与组件同步）。两处删除后，`slice-face-img` 的 `position:absolute` 样式可保留（无害）。

- [ ] **Step 4: 语法自检**

Run: `node --check server/static/js/modules/13_anchors.js`
Expected: 无输出（语法合法）。若无 node，用浏览器打开页面后查看控制台无 `Uncaught SyntaxError`。

- [ ] **Step 5: 手动验证**

1. 启动服务：`python -m server.run`（或既有启动方式）。
2. 打开主播管理页 → 点击某主播「预览」→ 输入台词 → 点击「试听台词驱动」。
3. 预期：
   - 状态条显示「正在唤醒神经口型驱动引擎…」随后变为引擎与耗时；
   - 主监视舱（215×215）随音频播放逐帧切换真实重绘人脸，**不再出现双下唇**；
   - 顶部算力胶囊显示真实引擎/设备/帧耗时；模型未安装时显示「⚠️ [已回退] 神经模型未下载 · 前往【GPU算力配置】启用」；
   - Network 面板可见 `preview-speech-drive` 请求与 `preview-sessions/.../face/N.jpg` 帧加载。
4. 打开 DevTools 控制台，确认无 `renderLiveLipSyncOnCanvas is not defined` 之类的残留引用错误（全局搜索 `13_anchors.js` 确认无其它调用方）。

- [ ] **Step 6: 提交**

```bash
git add server/static/js/modules/13_anchors.js server/static/components/tab_anchors.html server/static/index.html
git commit -m "feat(preview): 前端切换到真实神经帧播放与实测遥测徽章，移除假Canvas驱动"
```

---

## Task 8: 清理死代码与全量回归

**Files:**
- Modify: `server/static/js/console.js`（未被加载的重复假驱动代码）
- Test: 全量 `server/tests/`

- [ ] **Step 1: 确认 console.js 无引用**

Run: `grep -rn "console.js" server/static/*.html server/static/components/*.html`
Expected: 只剩注释或无匹配（此前已确认 `index.html` / `index.template.html` 仅加载 `js/modules/*.js`）。若有任何 `<script src="...console.js">`，**停止本任务**（说明该文件仍被使用，仅删除其中的假驱动函数即可，删除范围与 Task 7 Step 1 相同的函数名）。

- [ ] **Step 2: 删除 console.js 中的假驱动重复实现**

删除 `server/static/js/console.js` 中与以下同名的函数及其变量声明（约 8221-8700 区间）：`startContinuousLipSyncLoop`、`resetLiveLipSyncState`、`calculateLiveViseme`、`renderLiveLipSyncOnCanvas`、`updateLiveLipSyncUI`、`renderSingleFaceLipSync`，以及 `_lipSyncRafId`、`_offscreenLowerCanvas`、`_liveAudioBuffer`、`_liveAudioCtx`、`_isSpeechDrivingActive`、`_prevMouthOpen`、`_prevMouthForm`。同时把 `testAnchorSpeechDemo` 的旧实现替换为 Task 7 的版本（保持与 modules 一致），或直接删除整个 `console.js`（Step 1 确认无引用时）。

Run: `node --check server/static/js/console.js`（若保留文件）
Expected: 无输出。

- [ ] **Step 3: 全量后端回归**

Run: `python -m pytest server/tests/ -q`
Expected: 全部通过；新增 `test_speech_drive_preview.py`、`test_tts_preview_service.py`、`test_neural_lip_renderer_return_face.py` 在内。

- [ ] **Step 4: 类型检查（项目配置了 mypy/ruff 时）**

Run: `python -m mypy server/core/avatar/speech_drive_preview.py server/core/audio/tts_preview_service.py server/routes/anchors.py 2>&1 | tail -20` 以及 `python -m ruff check server/core/avatar/speech_drive_preview.py server/routes/anchors.py`
Expected: 无新增错误（与改动前基线一致）。

- [ ] **Step 5: 提交**

```bash
git add server/static/js/console.js
git commit -m "chore: 清理未被加载的假唇动驱动重复代码"
```

---

## 自检 (Self-Review)

- **Spec 覆盖**：§3.1 端点 → Task 5；§3.2 共享 TTS → Task 1；§3.3 遥测/降级 → Task 4（`_dispatch_render`/`_diagnose_local_failure` 覆盖全部 fallback_reason 枚举）；§3.4 本地路径与 `return_face` → Task 2+4；§3.5 云端路径 → Task 6；§3.6 存储与清理 → Task 3（`_persist`/`_cleanup_old_sessions`）+ Task 5（静态帧路由 + 路径穿越校验）；§3.7 前端 → Task 7；§5 测试 → 各任务 Step 1。§1.3 非目标（不动 sidecar 服务端、不改开播管路）已遵守。
- **占位符扫描**：Task 5 Step 1 的 `anchor_with_asset` fixture 留有「执行者注意」说明（项目 DB 引导方式需对齐既有 fixture），这是有意的环境对齐说明而非含糊 TODO；其余步骤均给出完整代码与确切命令。
- **类型一致性**：`RenderOutcome`/`SpeechDriveResult` 字段在 Task 3 定义、Task 4/6 填充、Task 5 端点序列化时名称一致（`engine`/`mode`/`device`/`providers`/`mean_inference_ms`/`total_inference_ms`/`vram_total_gb`/`fps`/`frame_count`/`session_id`/`fallback_reason`）；`_render_local_sync`、`_build_audio_frame_batch`、`_crop_like`、`_load_coords`、`_load_face_imgs` 在 Task 3/4/6 中被引用且签名一致；`crop_face_256`（Task 2 改名）在 Task 4 的 `_crop_like` 注释中被引用为同口径参照。

## 执行选择

Plan complete and saved to `docs/superpowers/plans/2026-09-26-preview-speech-drive.md`. Two execution options:

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

Which approach?
