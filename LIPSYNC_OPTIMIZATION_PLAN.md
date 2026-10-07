# AI 数字人唇形拟真度与顶尖口型一致性方案 (v4.0)
## —— 纯 LatentSync 官方扩散模型架构（SyncNet 业界顶尖拟人口型）

> **版本**：v4.0.0 (纯 LatentSync 官方架构落地版)  
> **更新时间**：2026-10-03  
> **适用范围**：云端 GPU 渲染节点（`google_gpu.md` / `intern_gpu.md`）、本地媒体编排中枢（`server/adapters/media/`）、试播与直播预取管线  
> **核心目标**：**彻底移除旧版 Wav2Lip**，全平台收敛至 ByteDance LatentSync 官方扩散模型架构。依托主播真实底模视频 (`source.mp4`) 与 Whisper 音素级语义特征，实现 100% 业界顶尖的 SyncNet 口型拟真度与自然肌肉咬合。

---

## 一、架构收敛背景与技术对比

### 1.1 为什么必须彻底淘汰 Wav2Lip？
1. **Wav2Lip 的缺陷本质**：
   - 依赖 80 维粗糙 Mel 能量图与静态单帧 L1 损失，导致出现机械式开合、双唇闭音漏风合不拢、元音变形生硬、牙齿唇纹模糊成色块。
   - 无法捕捉真人说话时下颌骨、咬肌、颊部肌肉的时序连带运动。
2. **官方 ByteDance LatentSync 的决定性优势**：
   - **音素语义引导**：采用 OpenAI Whisper 提取语音特征 Embedding，而非简单声音振幅，让数字人真正“懂发音规则”（如发 `/b/`, `/p/`, `/m/` 闭唇，发 `/a/`, `/o/` 下颌下拉）；
   - **UNet3D 时序去噪扩散**：在潜空间中以 16~25 帧连续切片为时序块进行联合去噪，唇部动作柔和连贯，绝无单帧闪烁；
   - **高清细节还真**：原生重绘逼真牙齿微光、唇纹纹理与下唇弧度，在权威 SyncNet 评测中达到业界天花板。

### 1.2 LatentSync 核心资源依赖解析
* **为什么必须是底模视频素材 (`source.mp4`)？**
  - 真人在说话时，头部保持自然的呼吸微动、微眨眼和肌肉弹性。单张静态图片或孤立切片完全丢失了时序动态与三维光影。
  - LatentSync 原生要求以 25 FPS 的连续视频作为输入基准（`--video_path source.mp4`），结合输入的驱动音频（`--audio_path speech.wav`）进行扩散去噪重绘。
* **本地资产对接**：
  - 本地项目每个主播训练时，用户上传的正是真人录制的底模视频（`source.mp4`）。
  - 系统在同步云端资产时，直接打包 `source.mp4` 作为 LatentSync 的底片源，保障了极致自然的真人发音质感。

---

## 二、纯 LatentSync 预渲染驱动架构

```
                     ┌──────────────────────────────────────────────┐
                     │          本地直播中控台 (Agent Core)          │
                     └──────────────────────┬───────────────────────┘
                                            │
                ┌───────────────────────────┴───────────────────────────┐
                ▼                                                       ▼
    【试讲演示 / 单次试听】                                    【正式直播 / 脚本讲解】
   (POST preview-speech-drive)                               (带货主脚本 / 预设问答切片)
                │                                                       │
          TTS 语音生成                                            TTS 前瞻预取生成 (提前 1~2 句)
                │                                                       │
                ▼                                                       ▼
    SpeechDrivePreviewService                                LatentSyncBatchAvatarProvider
    (调用云端 /render/batch)                                  (整段异步批处理预取)
                │                                                       │
                └───────────────────────────┬───────────────────────────┘
                                            │
                                            ▼
                          云端 GPU: ByteDance LatentSync
                     (Whisper 音素提取 + UNet3D 扩散去噪)
                                            │
                                            ▼
                         高拟真口型帧序列 (25 FPS, 牙齿唇纹清晰)
                                            │
                                            ▼
                            自适应 LAB 色彩对齐与双级高斯羽化
                                            │
                                            ▼
                                画面混流 / 演示播放
```

---

## 三、核心模块与契约设计

### 3.1 远端 Provider 批处理扩展契约 (`AvatarRenderMode.BATCH`)
在 `server/adapters/media/avatar_provider.py` 契约中，激活并强化 `AvatarRenderMode.BATCH`：
- **声明能力**：`render_modes=frozenset({AvatarRenderMode.BATCH, AvatarRenderMode.REALTIME})`
- **输入**：标准 `Sequence[AudioFrame]`（整句完整 PCM）或 WAV 字节流；
- **输出**：`ProviderRenderResult`，包含已按 25FPS 带有精确时间戳（PTS）的一整批渲染帧切片。

### 3.2 本地预渲染调度器 (`SlicePrefetcher`)
在 `server/core/avatar/slice_prefetcher.py` 维护滑动预取窗口：
- **容量与调度**：保持前瞻 1~2 个切片（每个切片 3~8 秒），当前切片播放进度达 60% 时，自动触发下一切片后台预取；
- **打断保护**：用户插播或跳过时，一键清空预取切片环并通知云端取消正在计算的 batch 任务。

---

## 四、落地实施全景清单

| 阶段 | 交付项 | 对应文件 / 模块 | 核心工作 | 状态 |
| :--- | :--- | :--- | :--- | :--- |
| **阶段 1** | **云端 LatentSync 批处理服务与部署脚本** | `google_gpu.md`、`intern_gpu.md` | 彻底移除 Wav2Lip，在云端集成 `/render/batch` 端点与 ByteDance LatentSync 官方权重 | ✅ 已交付 |
| **阶段 2** | **本地 Batch 模式 Avatar Provider** | `server/adapters/media/latentsync_batch_provider.py` | 实现 `RemoteAvatarProvider` 契约，支持整句批处理预渲染、异步请求与切片转换 | ✅ 已交付 |
| **阶段 3** | **配置注册与 Schema 纳管** | `server/adapters/media/avatar_provider_registry.py` | 注册 `latentsync_batch` 适配器，支持在后台配置并发数、超时、guidance scale 等参数 | ✅ 已交付 |
| **阶段 4** | **话术前瞻预取管理器** | `server/core/avatar/slice_prefetcher.py` | 维护滑动窗口前瞻调度、已渲染切片缓存环、快速打断与清空机制 | ✅ 已交付 |
| **阶段 5** | **全链路自动化测试与契约守护** | `server/tests/test_latentsync_batch_provider.py` | 覆盖单测：批处理请求构造、切片回包解析、超时与熔断保护、打断取消行为 | ✅ 已交付 |
| **阶段 6** | **试播驱动弹窗全面收敛至 LatentSync** | `server/routes/anchors.py`、`server/core/avatar/speech_drive_preview.py`、前端模块 | 主播试讲弹窗全面移除 Wav2Lip，唯一使用 LatentSync 官方扩散模型驱动 | ✅ 已交付 |

---

## 五、验收基准与量化指标

1. **口型拟真度**：
   - 辅音（`/b/`, `/p/`, `/m/`）闭唇动作闭合度 100%，无残留唇缝；
   - 元音（`/a/`, `/o/`, `/u/`）开口饱满，随发音强弱自然伸缩；
   - 彻底消除机械开合与孤立抖动帧。
2. **直播流畅性**：
   - 预取命中率 ≥ 95% 时，切片过渡播放帧率稳定在 25 FPS，端到端无卡顿；
   - 突发弹幕打断切换延迟 ≤ 150ms。
3. **资源与健壮性**：
   - 本地仅做切片解码与混流，CPU 占用率低于 15%；
   - 云端 GPU 支持请求超时与优雅取消，显存常驻稳定不溢出。
