# 核心目的修复设计：音画同步 / 神经唇形权重 / ASR / 抖音 a_bogus

日期：2026-09-24。围绕核心目的"智能化 AI 数字人直播，画面高清流畅，音画同步，画面逼真动作自然仿真人"修复四项已确认短板。

## 修复一：音画同步 —— 共享单调时钟 + 漂移补偿

### 现状
- `av_sync.py` 的 `AVSyncController` 仅采样渲染耗时，`recommended_delay_ms = base + 渲染耗时均值`，是"软件近似"。
- `media_router._with_capability_boundary` 硬编码 `shared_playback_clock=False / clock_source=None / clock_precision="none"`。
- 现成可复用设施：`virtual_audio.get_playback_clock(audio_id)` 已提供真实音频播放头（`samples_played`/`elapsed_sec`/`latency_sec`），`musetalk_driver` 已用它驱动口型。

### 设计
新增 `server/core/media/shared_playback_clock.py`，`SharedPlaybackClock` 单例：
- 会话级单调时钟基准（`time.monotonic()`），`stamp_video(frame_idx)` 在视频帧发布处打 PTS。
- `report_audio_head(audio_id, pts_ms)` 从 `virtual_audio` 采集音频播放头。
- `compute_drift()`：`drift_ms = 音频头PTS − 视频最新PTS`，EMA 平滑（alpha=0.2），输出 `drift_ms`、`recommended_delay_ms`（钳制 0~300ms）、`frame_pacing_hint`（漂移 >120ms 时建议丢帧/复帧）。
- `get_alignment_status()` 上报 `clock_source="shared_monotonic_pts"`、`clock_precision="sample_aligned"`、`alignment_mode="shared_monotonic_pts"`。

改造点：
- `base_driver.publish_frame` 与 `musetalk_driver._publish_frame`：发布帧时 `stamp_video`。
- `av_sync.py`：新增 `apply_drift()`，`recommended_delay_ms` 由漂移驱动（渲染耗时作为基线之一保留）。
- `media_router._with_capability_boundary`：读时钟实况，能力契约 `supports_shared_clock` 打开。
- `routes/live.py` AUDIO_CHUNK：`delay_ms` 携带漂移补偿。
- 线程安全：用 `threading.Lock`； PTS 单调递增不回退（flush 时重置基准）。

### 验收
单测：时钟单调性、漂移 EMA 收敛、正负漂移补偿方向正确、钳制边界、flush 后基准重置。

## 修复二：神经唇形权重体检按需触发

### 现状
`onnx_lipsync.onnx`（约45MB）默认缺失，`NeuralLipRenderer` 平滑回退 RealAvatarLite。`scripts/download_weights.py` 已实现多源流式下载（ModelScope 优先，原子重命名）。

### 设计
- 体检新增第 13 项 `lipsync_weight`：已就绪 `pass`；缺失 `warn` + 一键下载指引。
- 新增 `POST /api/v1/anchors/avatar/download-lipsync-weight`：后台线程执行 `download_model("onnx-lipsync")`，全局状态机 `task_id / state(pending|downloading|installed|failed) / progress / message`。
- 新增 `GET /api/v1/anchors/avatar/download-status` 轮询。
- 下载完成热挂载：清 `NeuralLipRenderer` 单例缓存，下次渲染自动真实推理。
- 并发互斥：同一时刻只允许一个下载任务。

### 验收
单测：状态机迁移、并发请求拒绝、已存在文件跳过下载、失败可重试。

## 修复三：ASR faster-whisper 优先

### 现状
`asr_engine._load_asr_model` 优先 SenseVoice（依赖 torch 约2GB），未装时转写返回空字符串；VAD 打断不受影响。

### 设计
- 调整优先级：faster-whisper 提为第一优先（轻量 ctranslate2），SenseVoice 保留为高精度可选。
- 首次加载自动下载 `tiny` 模型并落盘 `data/models/faster-whisper/`（`WhisperModel(model_size, download_root=...)`）。
- 新增 `get_asr_status()`：后端类型、模型名、就绪态。
- `launcher.py` 的 `MEDIA_DEPENDENCIES` 增 `faster_whisper`；`server/requirements-optional.txt` 补 `faster-whisper`。
- 体检新增 ASR 项：缺失时 `warn`（"语音转写不可用，打断仍可用"）+ 自动补装指引。

### 验收
单测：后端选择优先级、状态上报、模型缓存路径、降级路径（两引擎都无时返回空且不抛异常）。

## 修复四：抖音 a_bogus 签名

### 现状
`douyin_fetcher._listen_loop` 构造 WS URL 无签名，诚实声明"未携带 a_bogus 签名，风控拒绝时请配置 ttwid/msToken"。

### 设计
新增 `server/adapters/danmaku/abogus.py`，纯 Python 生成器：
- 输入：查询参数 dict + User-Agent + 时间戳。
- 流程：参数按 key 排序规范化拼接 → 与 UA、时间戳混合 → 自定义摘要（位运算）→ RC4 变换 → base64 输出。
- 零外部依赖；模块头诚实声明"社区公开算法移植，随平台风控演进可能需要更新"。
- 结果缓存（同参数同时间窗内复用）。

接入：
- `douyin_fetcher` 构造 WS URL 时追加 `&a_bogus=<sig>`。
- 握手失败计数：连续失败达阈值（默认3次）自动回退 ttwid/msToken 中继模式，日志如实记录失败原因。

### 验收
单测：签名确定性（同输入同输出）、输出长度/字符集合法、不同参数产生不同签名、缓存命中、回退阈值触发。

## 风险与回退
- 四项相互独立，各自可独立开关；均不改变现有 365 项测试的既有行为（新增测试，不改旧断言）。
- 所有新能力缺失时保持现有降级路径不变（恪守 ADR-16 架构诚实）。
