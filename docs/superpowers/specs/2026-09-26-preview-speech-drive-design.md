# 试播台词驱动 · 真实算力闭环 (方案 C) — 设计规格

- **日期**: 2026-09-26
- **范围**: 数字人资产预览弹窗「试听台词驱动」功能
- **状态**: 已批准，待实施

## 1. 背景与目标

### 1.1 现状硬伤

当前「试听台词驱动」(`testAnchorSpeechDemo()`, `server/static/js/modules/13_anchors.js:1388`) 存在两个核心问题：

1. **算力未真实闭环**：前端只 POST 到 `/api/v1/settings/tts/preview`（`server/routes/settings.py:1492`），该端点**仅做 TTS 音频合成**，不存在任何神经推理。唇形动作是纯前端 Canvas 按**音频 RMS 音量**假驱动 (`renderLiveLipSyncOnCanvas()`)，显卡从未参与。而界面徽章却显示「● 本地CUDA驱动中 (xx%)」，属于 ADR-16「诚实上报」原则下的过度承诺。
2. **渲染逻辑错误**：假驱动在嘴下复制并平移一块含下唇的像素块 (`sx=84, sy=205, sw=84, sh=42` → 贴回 `dy=205+drop`)，原下唇未被移除，产生「上嘴唇 1 + 下嘴唇 2」的重影 bug。

### 1.2 目标

把该弹窗升级为**真正的试播舱**：点击试听时，系统按用户在【GPU 与异构算力配置】中选定的模式，唤醒真实神经渲染引擎（本地 ONNX / 云端 Sidecar）执行前向推理，回传真实重绘口型帧序列，并把**实测**硬件遥测投射到前端界面。

### 1.3 非目标 (Out of Scope)

- 云端 sidecar 服务端本身的改动（`gpu_sidecar/` 保持不变，仅作为被调用方）。
- 真实开播管路的任何改动。
- 流式帧推送（本次已选定批量渲染 + 帧地址清单方案；流式作为后续可能增强）。

## 2. 架构总览

```
┌─────────────────────────────────────────────────────────────────┐
│  前端 13_anchors.js                                             │
│  testAnchorSpeechDemo()                                        │
│    └─ POST /api/v1/anchors/{id}/avatar/preview-speech-drive     │
│       { text, provider_name, voice_name }                       │
└──────────────────────────────┬──────────────────────────────────┘
                               │
┌──────────────────────────────▼──────────────────────────────────┐
│  server/routes/anchors.py                                       │
│  POST /{anchor_id}/avatar/preview-speech-drive                  │
│    1. 校验 anchor 与资产 (face_imgs/ + coords.pkl)               │
│    2. 校验文本长度上限                                           │
└──────────────────────────────┬──────────────────────────────────┘
                               │
┌──────────────────────────────▼──────────────────────────────────┐
│  server/core/avatar/speech_drive_preview.py  (新增)             │
│  SpeechDrivePreviewService                                      │
│    a. synthesize_audio()   ← 复用共享 TTS 合成函数               │
│    b. decode PCM 16k mono  ← audio_decode.decode_audio_to_float32│
│    c. resolve_compute()    ← gpu_target → local/cloud/fallback   │
│    d. render_frames()      ← 分流真实推理，25 FPS                │
│    e. persist()            ← 落盘临时帧 + 清理旧会话              │
└───────┬───────────────────────────────┬───────────────────────────┘
        │ local                         │ cloud
┌───────▼───────────────────┐  ┌───────▼───────────────────────────┐
│ NeuralLipRenderer        │  │ NeuralSidecarMediaDriver          │
│ (ONNX CUDA/CPU, 复用)     │  │ (一次性批量会话: 推 PCM → 收 JPEG) │
└──────────────────────────┘  └───────────────────────────────────┘
```

## 3. 组件设计

### 3.1 API 端点

`POST /api/v1/anchors/{anchor_id}/avatar/preview-speech-drive`

**请求** (JSON):

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `text` | string | 是 | 试听台词，服务端硬限 500 字符 |
| `provider_name` | string | 否 | TTS 引擎标识，缺省时走与现有试听相同的智能匹配逻辑 |
| `voice_name` | string | 否 | 音色 ID |

**响应** (200, JSON):

```jsonc
{
  "code": 0,
  "data": {
    "engine": "neural_local_onnx",       // 见 §3.3 诚实遥测契约
    "mode": "local",                      // local | cloud | fallback
    "device": "NVIDIA GeForce RTX 4070",  // 实测设备名，fallback 时为 "" 
    "providers": ["CUDAExecutionProvider", "CPUExecutionProvider"],
    "mean_inference_ms": 14.3,            // 实测单帧均值
    "total_inference_ms": 1072.5,         // 实测总推理耗时
    "vram_total_gb": 12.0,                // 可选，能测到才报
    "fps": 25,
    "frame_count": 75,
    "audio_url": "/api/v1/anchors/{anchor_id}/preview-sessions/{session_id}/audio.mp3",
    "face_frames": [".../face/0.jpg", ".../face/1.jpg", "..."],
    "full_frames": [".../full/0.jpg", ".../full/1.jpg", "..."],
    "fallback_reason": null               // 降级时填，见 §3.3
  }
}
```

**错误码**:

| HTTP | 触发条件 | detail |
|---|---|---|
| 404 | anchor 不存在 | 主播不存在 |
| 409 | 资产未就绪（无 face_imgs/coords.pkl） | 该主播尚未完成数字人资产训练，请先完成切片生成 |
| 422 | 文本为空或超 500 字 | 台词长度必须为 1 到 500 字符 |
| 503 | TTS 合成失败 | 主播绑定的音色有误，请检查（沿用现有提炼规则） |
| 504 | 云端 sidecar 握手/渲染超时 | 云端算力节点无响应，已回退本地引擎 |
| 500 | 其他未预期异常 | 试播驱动失败，请重试 |

### 3.2 共享 TTS 合成函数 (定向重构)

把 `server/routes/settings.py:preview_tts_audio()` 中** provider 解析 + 音频合成**的核心逻辑抽为共享函数：

```python
# server/core/audio/tts_preview_service.py (新增)
async def synthesize_preview_audio(
    provider_name: str,
    voice_name: str,
    text: str,
    anchor_id: str | None = None,
) -> tuple[bytes, str]:
    """合成试听音频，返回 (mp3_bytes, resolved_provider_name)"""
```

- `settings.py` 的 `/tts/preview` 路由改为委托调用该函数（行为不变，保持现有测试全部通过）。
- 新端点复用同一函数，保证试听音色与现有音色试听页**完全一致**。

### 3.3 算力分流与诚实遥测契约 (ADR-16)

`SpeechDrivePreviewService.resolve_compute()` 依据 `/settings/gpu-target` 偏好 + 实际能力决定引擎：

| 条件 | engine | 说明 |
|---|---|---|
| target=local/auto 且 ONNX 模型就绪且资产齐备 | `neural_local_onnx` | 真实本地推理，`providers` 取 `session.get_providers()` 实测值 |
| target=cloud 且 sidecar 握手成功 | `neural_cloud_sidecar` | 真实云端推理 |
| 上述均不满足 | `procedural_fallback` | 诚实降级，徽章显示「⚠️ 回退微动态」，**禁止**声称 GPU |

**遥测字段只能是实测值**：
- `mean_inference_ms` / `total_inference_ms`：逐帧 `time.perf_counter()` 计时求均值。
- `device`：本地取 ONNX provider/CUDA 设备名；云端取握手返回的 backend descriptor 设备名。
- `vram_total_gb`：能取到（如 pynvml 可用）才填，否则**省略该字段**，绝不编造。

**降级原因枚举** (`fallback_reason`)：
- `model_not_installed` — ONNX 权重未下载（前端可引导前往 GPU 设置页下载）
- `no_anchor_assets` — 主播资产缺失（正常应已被 409 拦截，双保险）
- `onnx_runtime_unavailable` — onnxruntime 未安装
- `sidecar_unreachable` — 云端节点不可达（超时后**自动回退本地**，而非直接失败）
- `render_error` — 推理过程异常

### 3.4 本地推理路径 (复用 NeuralLipRenderer)

- `NeuralLipRenderer.load_anchor_assets(anchor.avatar_asset_dir)` 载入 `face_imgs/` 与 `coords.pkl`。
- 帧数 = `floor(audio_duration * 25)`，上限 300 帧（约 12 秒）。
- 逐帧调用 `render_lip_frame(full_frame, frame_idx, pcm_window, mouth_open)`：
  - `full_frame` = `full_imgs[i % len(full_imgs)]`（闭口母轨循环）。
  - `pcm_window` = 以该帧时刻为中心的 ±200ms float32 切片。
  - `mouth_open` = 该窗口 RMS 归一化能量（静音帧 renderer 内部自动跳过推理，返回原帧）。
- **定向改造**：`render_lip_frame()` 增加可选参数 `return_face=True`，同时返回 `(full_frame_blended, rendered_face_256)`，让主监视舱直接拿到神经输出的 256×256 人脸，而非从全图裁剪上采样（避免失真）。默认 `False`，保持真实开播管路调用签名不变。

### 3.5 云端推理路径 (复用 NeuralSidecarMediaDriver)

一次性批量会话，而非接管实时推流：

1. 按 `gpu_target` 配置构造 driver（node_url / auth_token / avatar 标识）。
2. `start()` 建立握手，校验 neural backend descriptor。
3. 将整段 PCM 切分为 AudioFrame 流推送（复用 `encode_audio_frame` / v3 envelope）。
4. 通过 timeline consumer 收集回传 JPEG 帧，按 sequence 排序，`finish`/strict_completion 语义判定结束。
5. `stop()` 释放连接。
6. **握手/渲染失败 → 自动回退本地 ONNX 路径**（并置 `fallback_reason="sidecar_unreachable"`，若本地也不可用则进入 `procedural_fallback`）。
7. 会话期间持有锁，避免与真实开播管路并发占用同一 sidecar 连接（`max_safe_concurrency: 1`）。

### 3.6 存储与清理

- 落盘根目录：`DATA_DIR/preview_speech_sessions/{anchor_id}/{session_id}/`
  - `audio.mp3`、`face/{i}.jpg`、`full/{i}.jpg`
- `session_id` = `%Y%m%d%H%M%S%f` 时间戳，天然有序。
- 帧图复用现有 `avatar-asset-frame` 的 JPEG 编码方式（`cv2.imencode`，质量 90）。
- **清理策略**：每次新建会话后，按 anchor 保留最近 3 个会话目录，超出则删除最旧；进程退出时不做额外清理（临时目录随 DATA_DIR 管理）。
- 帧地址通过新的只读路由暴露：`GET /api/v1/anchors/{anchor_id}/preview-sessions/{session_id}/{kind}/{idx}.jpg`（kind ∈ face/full；音频为同层 `audio.mp3`），做路径穿越校验（`..`/绝对路径拒绝），并校验 session_id 归属当前 anchor。

### 3.7 前端改造 (13_anchors.js)

1. `testAnchorSpeechDemo()` 重写：调用新端点，删除 `/settings/tts/preview` 直调和全部假 Canvas 驱动路径（`renderLiveLipSyncOnCanvas` / `calculateLiveViseme` / `_offscreenLowerCanvas` 等，连同双下唇 bug 一并移除）。
2. 播放：`new Audio(audio_url)`；`requestAnimationFrame` 中以 `audio.currentTime * 25` 计算帧索引，切换 `slice-face-img`（主监视舱，真实神经 256 人脸）与 `slice-full-img`（副监视窗，回贴全图）的 `src`。兼顾浏览器解码调度，预加载前后 ±3 帧。
3. 徽章 `slice-face-gpu-badge` / `slice-face-sync-badge` / `slice-face-tip` 全部改用**响应里的真实遥测**：
   - `neural_local_onnx` → `🟢 本地独显: {device} · {providers 简写} · {mean_inference_ms}ms/帧`
   - `neural_cloud_sidecar` → `⚡ 云端算力: {device} · 握手成功 · {mean_inference_ms}ms/帧`
   - `procedural_fallback` → `⚠️ 神经引擎未就绪 ({fallback_reason}) · 已回退微动态 · 前往【GPU算力配置】启用`
4. 状态条与「试听完毕」提示展示真实 `mean_inference_ms`、`frame_count`、`device`。

## 4. 并发与性能考量

- 本地推理在 `run_cpu_bound`/线程池中执行（ONNX session 非线程安全，端点内串行渲染单次会话即可，会话级互斥）。
- 同一 anchor 的试播请求加进程内互斥锁，防止重复渲染与目录竞争。
- 文本硬限 500 字符 + 帧数上限 300， bound 住最坏 CPU/IO 耗时。
- 云端会话遵守 `REQUEST_TIMEOUT`（120s）既有截止时间语义。

## 5. 测试策略

沿用 `server/tests/` 现有风格（pytest + httpx AsyncClient / TestClient）：

1. **`test_preview_speech_drive_local.py`**：mock `NeuralLipRenderer` session，验证端到端帧数 = duration×25、遥测字段存在且为实测来源、帧 URL 可访问。
2. **诚实性测试**：模型未安装时 `engine == "procedural_fallback"` 且 `fallback_reason == "model_not_installed"`，且响应不含伪造 `device`/`providers`。
3. **云端分支**：mock `NeuralSidecarMediaDriver`，验证握手失败时自动回退本地；本地也不可用时降级为 `procedural_fallback` 而非 500。
4. **边界**：空文本/超长文本 → 422；资产缺失 → 409；帧数上限截断生效。
5. **回归**：现有 `/tts/preview` 全部既有测试保持通过（验证 §3.2 重构无行为变化）。
6. **路径穿越**：`preview-sessions` 静态路由对 `../` 请求返回 404。

## 6. 实施顺序建议

1. §3.2 抽取共享 TTS 合成函数 + 回归测试。
2. §3.4 `render_lip_frame` 增 `return_face`（默认 False，零行为变化）。
3. §3.1 + §3.3 + §3.6 后端端点、服务、存储与静态帧路由（先 local 路径）。
4. §3.5 云端路径 + 自动回退。
5. §3.7 前端切换到真实帧 + 真实徽章，移除假 Canvas 代码。
6. §5 测试补齐与全量回归。

## 7. 后续可能的增强 (明确不在本期)

- 流式帧推送（SSE/WebSocket）以消除首字延迟。
- 云端 sidecar 支持批量 HTTP 渲染端点，免去 WS 会话开销。
- 试播片段一键导出为视频文件。
