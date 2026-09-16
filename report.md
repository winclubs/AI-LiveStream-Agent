# AI-LiveStream-Agent 第五轮整改与验收报告

**报告日期：2026-09-14**
**项目目录：`g:\AI-LiveStream-Agent`**
**审查基线：`8898824`**
**自动化质量门禁：`PASS`**
**生产放行建议：`CONDITIONAL PASS`（代码与自动化验证通过，真实平台和硬件联调仍是上线前置条件）**

## 1. 验收摘要

本轮已针对第五轮审核遗留完成生产路径整改，覆盖 OBS 连接代际/RPC/重连/所有权、弹幕健康与熔断、数字人音画同步与能力契约、事件指标、OBS 配置恢复及前端状态语义。

最终实测结果：

- 专项回归：`154 passed in 19.07s`。
- 全量回归：`215 passed in 26.27s`。
- 分支覆盖率：`65.54%`，达到项目门槛 `55.0%`。
- Python 编译、Ruff、两个 Node.js 语法检查、运行时预检及 `git diff --check` 全部通过。
- 未提交 Git；当前变更保留在工作区。

此前偶发的 aiohttp `ClientResponse` 析构警告已定位到 Edge-TTS 流提前退出后依赖异步生成器 GC 清理。主驱动、试听接口及两个参考服务现均在 `finally` 中显式 `aclose()` 内层流；最终全量运行未再出现 warning。

## 2. 已关闭的关键问题

### 2.1 OBS 连接代际、RPC 与所有权

- Pending RPC 绑定 `(connection_epoch, websocket instance)`，响应监听、发送、状态刷新、开始/停止推流均执行代际 fencing，旧连接不能写入新连接状态。
- Cleanup 仅取消所属连接的 pending 请求；旧 listener/cleanup/event 不再清理当前连接或当前状态。
- Monitor 将错误响应和异常统一计入连续失败，达到阈值后 teardown 并进入重连。
- 显式断开与在途握手使用同一生命周期锁收敛：成功分支创建 monitor 前复核 auto-reconnect，断开后再次取消并等待 monitor，同时清除历史 stale/error 状态。
- Agent 推流所有权由单一布尔值升级为 `session_id + OBS connection_epoch`；仅同场次、同连接代际可停止推流或释放所有权。
- OBS host、port、加密密码及 auto-connect intent 在用户提交连接请求时即通过 `AppSetting` 持久化，而不是等待首次握手成功；即使 OBS 暂时离线，应用重启后也能恢复同一目标并由唯一 monitor 退避重试。服务退出不会篡改用户意图。
- `/obs/disconnect` 会先提交 `auto_connect=false` 的用户意图，再断开当前运行态；若持久化失败则保留当前连接，避免当前进程状态与重启恢复状态分裂。
- 前端明确区分自动重连与“平台状态未知”，状态刷新失败会标记 stale，而不是继续展示为可信实时状态。

### 2.2 弹幕健康、退避与熔断

- `FetcherHealth` 增加 `connection_generation`；物理握手只开启新的故障 episode，不再等价于协议健康或清除累计失败。
- 只有新鲜协议下行或真实事件可证明恢复，并重置熔断失败计数/退避。
- `connection_generation` 在每次独立物理连接尝试开始时推进，因此 DNS/TLS/鉴权等握手前连续失败也能形成独立故障 episode 并触发熔断；成功 open 不会对同一次尝试重复增代。
- 同一连接 generation 的 heartbeat send、recv、monitor 重复报错只计一个 unhealthy edge，避免同一故障双计。
- Douyin 只有收到下行后才重置指数退避；heartbeat 发送失败关闭 socket，由接收主循环统一上报故障。Bilibili 在房间发现 API 调用前以及每个独立节点尝试前都会推进 generation；节点握手或已连接 socket 失败均走权威错误回调，不再因 discovery 失败或切换下一节点漏记 episode。
- `HALF_OPEN` 增加一次性探活 deadline；仅创建后台任务不再被视为恢复成功；停机时会取消并等待 monitor 完整退出后再停止底层抓取器。
- 现有熔断测试已改为用独立 connection generation 表达独立故障；另修复一处测试直接污染真实全局直播控制器的问题。

### 2.3 数字人音画同步与能力契约

- 播放事务顺序调整为：`prepare playback cursor -> feed viseme -> play audio`，避免先投递口型或先播放音频造成竞态。
- 带 `audio_id` 的句子只走 active sentence，不再同时写兼容 `mouth_open_queue`，消除句末无声二次口型并支持有序多句。
- Remote GPU 接收 `audio_id`，按共享播放 cursor 与原始 PTS 选择最新有效帧并跳过过期帧；只有无 cursor 时才退回独立 monotonic 时钟。
- 若本地 PortAudio 因禁用、设备不可用、队列满、解码/流打开失败或异步 `playback_error` 而拒绝/中断 cursor，但浏览器音频仍可发送，Procedural 与 Remote 会切换到浏览器 monotonic fallback，避免“有声无口型”。`playback_error` fallback 会继承 cursor 已播放的 `elapsed_sec`，从当前偏移继续而不是从第 0 帧重放口型；代际过期或主动打断仍立即终止视觉轨。Remote 的所有句子时间线使用同一异步锁串行，与浏览器音频队列保持提交顺序，避免后句 fallback 覆盖前句画面。
- Edge-TTS 主驱动、试听接口与参考服务显式关闭内层 async generator；直播停机也会等待当前 TTS task 退出，避免 aiohttp response 延迟到事件循环关闭后才析构。
- Procedural、Remote、Live2D、Neural 驱动使用统一 capability schema；生产入口使用全局 procedural driver，避免能力查询对象与实际渲染对象不一致。

**能力边界必须如实说明：**当前 `VirtualAudioService` 使用 blocking `OutputStream.write`，只直接累计已提交样本，并结合 `stream.latency` 估算播放头。状态契约明确为：

- `shared_playback_clock: true`（可用时）；
- `hardware_dac_clock: false`；
- `clock_source: "portaudio_latency_estimate"`；
- `clock_precision: "estimated"`。

因此，本实现不是硬件 DAC 回调时钟，也不承诺微秒级物理发声定位。它是统一的 PortAudio 延迟估算时基，用于降低口型抢跑、漂移和 Remote 帧积压。

### 2.4 指标与对外语义

- 事件指标拆分为 `received / accepted / dropped`，并增加 danmaku/gift 对应分项。
- `danmaku_count`、`gift_count`、`gift_income_yuan` 仅在事件成功进入队列后累计；被背压或容量限制拒绝的事件不再污染运营指标。
- 队列驱逐结果携带被驱逐事件的类型与 Mock 归属；真实/Mock 混合流量互相驱逐时，dropped 按旧事件本身统计，不再按新事件错误归属。
- `/live/stats` 暴露新指标及 `queue_metrics`；Prometheus HELP/名称语义同步修正。
- Mock 事件与真实事件继续区分，避免演示流量进入正式运营统计。

## 3. 可复现验证

| 门禁 | 命令 | 实测结果 |
|---|---|---|
| 专项回归 | `python -m pytest -q server/tests/test_audit_remediation.py server/tests/test_p0_reliability_guard.py server/tests/test_obs_websocket.py server/tests/test_audio_reliability.py server/tests/test_viseme_renderer.py server/tests/test_remote_gpu_comprehensive.py server/tests/test_core_engine.py server/tests/test_api_endpoints.py` | `154 passed in 19.07s` |
| 全量与分支覆盖率 | `python -m pytest -q server/tests --cov=server --cov-branch --cov-report=term-missing` | `215 passed in 26.27s`；`65.54%`；无 warning |
| Python 编译 | `python -m compileall -q launcher.py scripts server` | PASS |
| Ruff | `python -m ruff check launcher.py scripts server` | `All checks passed!` |
| Electron 主进程语法 | `node --check "apps/desktop-ui/main.js"` | PASS |
| 控制台脚本语法 | `node --check "server/static/js/console.js"` | PASS |
| 运行时预检 | `python scripts/runtime_preflight.py` | PASS |
| Git 空白/冲突标记 | `git diff --check` | PASS（仅显示 LF/CRLF 工作区提示） |

测试使用独立临时 `LIVE_AGENT_DATA_DIR`，避免读写生产数据库。

## 4. 尚未完成的真实环境验收

以下项目不能由当前 mock/单元/API 回归替代，未宣称已经验证：

1. 真实 OBS WebSocket 断网、重连、旧响应迟到、外部手动开停流及跨场次所有权联调。
2. Bilibili/Douyin 真实账号与直播间的长时下行、网络分区、心跳故障、平台限流及协议变更验证。
3. PortAudio + VB-Cable/实际声卡的设备延迟、缓冲抖动、长句漂移、插拔和打断验收。
4. Remote GPU/TTS 服务的真实网络抖动、PTS 连续性、多句排队及追帧效果验证。
5. 8 小时以上无人值守直播的资源占用、泄漏、重连风暴和音画主观质量验证。
6. 虽然本轮已消除自动化中的 aiohttp 析构 warning，真实网络中断和客户端取消下的 Edge-TTS 资源释放仍应纳入长稳监控。

## 5. 最终结论

第五轮审核中可由代码修复和自动化验证关闭的关键问题已完成整改，自动化门禁通过。由于硬件播放头当前是 `portaudio_latency_estimate` 而非硬件 DAC 时钟，且真实 OBS、平台弹幕、音频设备和 Remote GPU 尚未完成端到端联调，本报告给出 **代码质量 `PASS`、生产放行 `CONDITIONAL PASS`**。正式上线前应完成第 4 节真实环境验收，不应再使用“硬件 DAC 微秒级时钟”或“已完成生产长周期验证”等表述。


---

# 附录 A：与 LiveTalking 的能力对比及优化建议

**对比日期：2026-09-14**<br>
**当前项目基线：`8898824`（工作区含未提交修改）**<br>
**目标仓库基线：[`lipku/LiveTalking@b3e7490a`](https://github.com/lipku/LiveTalking/tree/b3e7490a20e7a6330492f8a6ea8200a5d50279d1)（2026-09-13）**<br>
**分析方式：当前项目源码核查 + LiveTalking README、API 文档和关键实现源码核查；未运行 LiveTalking 模型和真实 GPU 性能测试。**

## A.1 结论摘要

两者不是可以直接互换的同类系统：

- **AI-LiveStream-Agent 是直播业务控制平面**：优势在直播平台弹幕、优先级与抢占、角色话术、商品/订单、RAG、合规、OBS 控制、配置安全、监控和桌面交付。
- **LiveTalking 是数字人实时媒体平面**：优势在真实神经口型、多 Avatar 后端、音视频帧流水线、WebRTC/WHEP、RTMP、录制、视频生成 Avatar、动作素材编排和单进程多会话。

因此，推荐方向不是用 LiveTalking 替换当前项目，而是：

> **保留当前项目的直播业务与可靠性控制层，新增可替换的 GPU 数字人媒体服务，逐步补齐神经口型、流式音频、Avatar 资产生产和标准实时传输。**

最值得优先补齐的三项能力是：

1. **真实神经数字人渲染**：当前 `musetalk_driver.py` 实际是 OpenCV 程序化头像，LiveTalking 的 MuseTalk/Wav2Lip 路径则真实加载 VAE、UNet、Whisper/Mel 特征并做口型区域贴回。
2. **真正的帧级流式音频/视频管线**：当前项目按“完整句音频”收集后才播放；LiveTalking 将音频切成约 20ms 帧，持续提取特征、推理和输出，更适合低延迟口型。
3. **Avatar 资产生产流水线**：当前项目主要上传肖像底图；LiveTalking 提供从视频生成 Wav2Lip/MuseTalk Avatar 的异步任务 API，覆盖预处理、进度和回调。

WebRTC、直接 RTMP、多会话和 ASR 也有价值，但应根据当前产品仍以“单机直播 + OBS”为主的定位分阶段引入，避免把系统过早改造成重型 SaaS 媒体服务器。

## A.2 对比依据与能力真实性说明

LiveTalking 的 README 宣称支持多数字人模型、声音克隆、打断、动作编排、WebRTC/RTMP/虚拟摄像头和多并发，参见其[项目说明](https://github.com/lipku/LiveTalking/blob/b3e7490a20e7a6330492f8a6ea8200a5d50279d1/README.md)。本报告没有只采信功能清单，还核查了以下实现：

- [`app.py`](https://github.com/lipku/LiveTalking/blob/b3e7490a20e7a6330492f8a6ea8200a5d50279d1/app.py)：启动模型、会话管理、WebRTC/WHEP 入口及 RTMP/虚拟摄像头模式。
- [`avatars/base_avatar.py`](https://github.com/lipku/LiveTalking/blob/b3e7490a20e7a6330492f8a6ea8200a5d50279d1/avatars/base_avatar.py)：20ms 音频帧、推理/合帧/输出线程、打断、动作素材和录制。
- [`avatars/musetalk_avatar.py`](https://github.com/lipku/LiveTalking/blob/b3e7490a20e7a6330492f8a6ea8200a5d50279d1/avatars/musetalk_avatar.py)：真实 MuseTalk 模型加载、特征批处理、GPU 推理和画面贴回。
- [`server/rtc_manager.py`](https://github.com/lipku/LiveTalking/blob/b3e7490a20e7a6330492f8a6ea8200a5d50279d1/server/rtc_manager.py) 与 [`server/session_manager.py`](https://github.com/lipku/LiveTalking/blob/b3e7490a20e7a6330492f8a6ea8200a5d50279d1/server/session_manager.py)：WebRTC 生命周期和 `sessionid` 会话容量管理。
- [`server/routes.py`](https://github.com/lipku/LiveTalking/blob/b3e7490a20e7a6330492f8a6ea8200a5d50279d1/server/routes.py) 与 [API 文档](https://github.com/lipku/LiveTalking/blob/b3e7490a20e7a6330492f8a6ea8200a5d50279d1/docs/api.md)：文本/音频驱动、打断、录制、SSE 和动作状态接口。
- [`server/asr_server.py`](https://github.com/lipku/LiveTalking/blob/b3e7490a20e7a6330492f8a6ea8200a5d50279d1/server/asr_server.py)：可选本地 SenseVoice/FunASR 识别端点。
- [`server/task_manager.py`](https://github.com/lipku/LiveTalking/blob/b3e7490a20e7a6330492f8a6ea8200a5d50279d1/server/task_manager.py) 与 [Avatar API](https://github.com/lipku/LiveTalking/blob/b3e7490a20e7a6330492f8a6ea8200a5d50279d1/docs/avatar_api.md)：视频预处理任务。
- [`streamout/rtmp.py`](https://github.com/lipku/LiveTalking/blob/b3e7490a20e7a6330492f8a6ea8200a5d50279d1/streamout/rtmp.py) 与 [`registry.py`](https://github.com/lipku/LiveTalking/blob/b3e7490a20e7a6330492f8a6ea8200a5d50279d1/registry.py)：直接 RTMP 输出和轻量注册表。

需要注意的文档/实现差异：

- README 功能清单仍提到 ER-NeRF，但当前 `app.py` 的运行时模型映射实际只有 `musetalk`、`wav2lip`、`ultralight`，因此本报告不把 ER-NeRF 计为当前主分支已接通能力。
- LiveTalking 内部名为 `WhisperASR` 的一部分模块主要用于提取口型模型所需声学特征，不等于语音转文字；真正 STT 是可选的 SenseVoice/FunASR WebSocket 服务。
- SenseVoice 服务加载了 VAD 模型，但当前浏览器流程由用户手动结束录音后统一推理并返回 final 文本，数字人说话时还会停止录音，因此属于**可用的半双工语音入口**，不是持续流式、AEC 完整的全双工语音打断。
- LiveTalking 的多会话是单进程内共享模型、每会话线程/队列的并发，并不等同于跨节点调度、租户隔离或水平扩缩容。

## A.3 能力矩阵

| 能力 | AI-LiveStream-Agent | LiveTalking（代码核查） | 判断与建议 |
|---|---|---|---|
| 产品主定位 | AI 互动直播运营中控 | 实时数字人媒体引擎 | 互补，不建议整体替换 |
| 神经口型 | 未交付；当前为程序化 G2P/能量口型 | Wav2Lip、MuseTalk、Ultralight 实际推理 | **最高优先级补齐** |
| 自定义形象 | 肖像底图 + 关键点缓存 | 上传视频生成模型专用 Avatar 资产 | 建议新增持久化资产任务 |
| 媒体粒度 | TTS 整句收集后一次提交 | 约 20ms PCM 帧持续进入特征/推理/输出 | **最高优先级重构** |
| 浏览器实时媒体 | MJPEG 视频 + 独立 WebSocket base64 音频 | WebRTC + WHEP 音视频轨道 | 互动/远程预览场景建议引入 |
| 直接推流 | 依赖 OBS 消费虚拟设备/浏览器源 | 直接 RTMP、RTC push、虚拟摄像头 | 保留 OBS 默认，直接 RTMP 做可选后端 |
| 音频直接驱动 | 无通用上传音频驱动数字人 API | `/humanaudio` 可直接驱动口型 | 建议补齐，复用录音/素材更方便 |
| ASR/VAD | 无 | 可选 SenseVoice/FunASR；当前半双工、final-only | 作为客服/大屏模式扩展，不宜先于神经渲染 |
| 打断 | P0 优先队列、双 generation fence、TTS/音频/口型联动 | `flush_talk()` 清空当前会话队列 | 当前项目更强，应保留现有语义 |
| 多会话 | 全局单个 `LiveSessionController` | `sessionid` 隔离，受 `max_session` 限制 | SaaS/多直播间需求明确后引入 |
| 服务端录制 | 无统一 A/V MP4 录制下载 | FFmpeg 音视频录制、合并与下载 | 低成本高价值，建议引入 |
| 动作编排 | 程序化呼吸/眨眼/运镜及业务画层 | 静默视频、指定动作素材与全身视频循环 | 可融合为素材化状态机 |
| TTS 扩展 | Edge、CosyVoice、MiniMax、Remote GPU | Edge、GPT-SoVITS、XTTS、腾讯、豆包、Azure、Qwen、OmniTTS | 双方各有优势；统一接口而非追求数量 |
| 插件机制 | 多处手工选择；工具总线为固定内置工具 | Avatar/TTS/Output 装饰器注册 + 显式模块映射 | 借鉴统一契约，不照搬动态任意加载 |
| 直播平台互动 | Bilibili/Douyin 原生 + webhook/WS 中继 | 核心仓库无直播弹幕闭环 | 当前项目明显更强 |
| 直播业务 | 商品、订单、角色、冷场、RAG、合规、价格审计 | 核心仓库主要是通用媒体 API | 当前项目明显更强 |
| OBS 控制 | OBS WebSocket v5、状态、重连、所有权 | 非核心能力 | 当前项目明显更强 |
| 配置与密钥 | SQLite 配置中心、AES-GCM/DPAPI | CLI/YAML/.env 为主 | 当前项目明显更强 |
| 可观测性与测试 | 健康探针、Prometheus、事件指标、215 项历史回归 | FPS 日志、管理页；当前树仅见少量 ASR 单测 | 当前项目明显更强 |
| Docker/GPU 部署 | 无容器化 GPU 媒体服务 | 有 Dockerfile，但内容仍是旧 Python/CUDA/目录配置 | 可借鉴方向，不能直接照搬文件 |

## A.4 LiveTalking 中值得吸收的能力

### A.4.1 真实神经口型与多渲染后端（P0）

**当前缺口**：`server/adapters/media/musetalk_driver.py` 明确不加载 MuseTalk/UNet，`Live2DDriver` 和 `NeuralLipSyncDriver` 也仍是不可用占位。当前画面足以做低资源演示，但人物真实感、发音精度和商业观感存在硬上限。

**建议**：

- 不把 PyTorch/CUDA/模型权重直接塞入现有 Electron 内置 Python；新增独立 `avatar-render-node` GPU 服务。
- 第一阶段优先 Wav2Lip 路线，用较低显卡门槛验证端到端协议；第二阶段再加入 MuseTalk 作为高质量档。
- 保留现有 `ProceduralAvatarDriver` 作为无 GPU、故障降级和 CI 测试后端。
- 扩展现有 `RemoteGPUMediaDriver` 协议，传输音频帧、统一 PTS、generation、`audio_id`、能力清单、队列水位和推理 FPS，而不是只返回离散 JPEG。
- 渲染节点启动时完成模型预热和 Avatar 资产缓存，直播主链禁止临时加载权重。

**验收建议**：在目标显卡上持续 30 分钟 `final_fps >= 25`；A/V 偏差绝对值 P95 不高于 120ms；P0 打断后旧音频与旧口型 P95 在 300ms 内停止；GPU OOM 时自动回退程序化渲染且直播会话不中断。

### A.4.2 真正流式的 TTS → 口型 → 播放管线（P0）

**当前缺口**：`LiveSessionController._collect_tts_sentence()` 会先拼接整句音频，`_speak_sentence()` 再统一送入口型和声卡。即使上游 TTS 支持 streaming，用户也要等整句生成完成。

**建议**：

- 定义内部标准 `AudioFrame`：`session_id`、`session_generation`、`audio_generation`、`audio_id`、`pts_ms`、`sample_rate`、`channels`、`pcm_s16le`、`is_first/is_last`。
- TTS 适配器输出先统一解码成 20～40ms PCM 帧，写入有界队列；播放、口型和浏览器输出消费同一 PTS 时间线。
- 保留现有 generation fence，将取消语义下沉到每一帧，禁止旧 producer 在取消后继续入队。
- 让虚拟声卡 callback/消费位置成为主时钟；浏览器路径则以 WebRTC RTP timestamp 为主时钟，避免同时维护多个弱关联时基。
- 在句子层保留合规审核，但通过更短的安全语义块启动 TTS；不能为了降延迟绕过价格审计和违禁词检查。

**验收建议**：相对当前基线，首包可听延迟 P95 至少下降 30%；长句不再按整句音频线性占用内存；连续 100 次打断无旧音频复活、幽灵口型或句间乱序。

### A.4.3 从视频生成 Avatar 的资产流水线（P0/P1）

**当前缺口**：主播管理已有肖像字段，但没有面向神经模型的原始视频校验、人脸检测、裁剪、mask/latent 缓存、进度查询和失败恢复。

**建议**：

- 新增 `AvatarAsset` 与 `AvatarBuildJob` 持久化模型，状态为 `pending/running/completed/failed/cancelled`。
- API 支持上传视频、查询进度、取消、重试、删除和产物校验；任务不能只保存在进程内存。
- CPU 预处理和 GPU latent 生成使用独立 worker；限制文件大小、时长、分辨率、帧率和并发。
- FFmpeg 必须使用参数数组并校验本地工作目录，禁止将文件名拼入 shell 命令。
- 产物 manifest 记录模型类型、模型版本、源文件 SHA-256、输出尺寸、帧数和构建参数，模型升级后可判断是否需要重建。

**验收建议**：服务重启后任务状态不丢失；同源同参数可幂等复用；失败任务保留诊断；非法视频、路径穿越和超预算输入被拒绝；生成后的 Avatar 可在预览会话直接试播。

### A.4.4 WebRTC/WHEP 低延迟预览（P1，按场景启用）

**价值**：MJPEG 与独立音频 WebSocket 不具备统一媒体时钟、拥塞控制和浏览器标准播放链。WebRTC 可改善远程控制台预览，并为未来语音客服/互动大屏提供双向媒体基础。

**建议**：

- 第一阶段只做服务端发送 A/V 的 WHEP/offer 会话，不立即重写 OBS 发布链。
- WebRTC 会话作为预览消费者，不应反向控制直播业务场次生命周期。
- 配置 STUN/TURN、ICE 超时、连接上限、鉴权和 Origin allowlist；禁止沿用任意来源 CORS。
- 保留 MJPEG 作为低依赖诊断与兼容后备。

**验收建议**：同一直播会话允许多个只读预览客户端；断开后资源可回收；弱网下能降码率而非拖垮主渲染；浏览器 A/V 不出现持续漂移。

### A.4.5 服务端录制、回放与素材复盘（P1）

**价值**：LiveTalking 的 `/record` 和下载接口虽实现较朴素，但产品能力很实用。当前项目已有场次、弹幕、订单和 AI 回复日志，若增加统一录制，可形成“视频 + 业务事件时间线”的完整复盘资产。

**建议**：

- 在发布画面分发点录制与 OBS/预览一致的最终合成帧，而不是单独重渲染。
- 音频与视频使用统一 PTS 直接封装 MP4/MKV，避免结束后再用临时文件拼接。
- 将打断、商品切换、礼物、合规命中、订单等事件写入 sidecar JSON 时间线。
- 增加磁盘配额、最短剩余空间、异常断电可恢复容器和自动清理策略。

### A.4.6 素材化动作编排（P1）

**价值**：当前程序化眨眼、呼吸和运镜是良好基础，但真人数字人只靠口型会显得机械。LiveTalking 的静默/动作视频循环可补充全身姿态和特定手势。

**建议**：

- 建立 `IDLE / LISTENING / SPEAKING / THANKING / PRODUCT_FOCUS / URGENCY` 媒体状态机。
- 每个角色可配置视频片段、进入/退出条件、循环策略、优先级和冷却时间。
- 业务事件只发语义动作，例如 `THANK_GIFT`，由媒体层选择素材，避免角色代码绑定文件路径。
- 在素材切换点做关键帧、音频边界和淡入淡出处理，避免跳帧。

### A.4.7 统一 Avatar/TTS/Output 插件契约（P1）

**当前缺口**：当前项目已有多个驱动和 router，但选择逻辑仍集中在直播控制器，新增后端会继续放大条件分支。

**建议**：

- 定义三个稳定接口：`TTSProvider`、`AvatarRenderer`、`MediaOutput`。
- 每个插件声明 `capabilities`、配置 schema、健康状态、成本等级、并发上限、预热状态和可取消能力。
- 通过显式 allowlist 和启动时注册加载，避免运行时导入任意用户模块。
- 将当前的 Edge/CosyVoice/MiniMax/Remote GPU 与 Procedural 后端迁移到同一契约，再新增神经渲染和 WebRTC/RTMP 输出。

### A.4.8 本地 ASR/VAD 语音入口（P2，场景驱动）

直播弹幕仍是当前核心输入，因此 ASR 不应挤占神经渲染和流式管线优先级。若产品扩展到展厅、客服、连麦或运营口述控制，再引入：

- 浏览器或麦克风 PCM → VAD endpointing → 流式/两阶段 ASR → 文本事件。
- ASR 结果统一进入现有优先级队列，并区分观众语音、运营指令和环境噪声。
- 真正语音打断需要 AEC、回声参考、最小发声阈值和误触发抑制；不能仅凭 VAD 检测到声音就触发 P0。
- ASR 模型池不能使用全局推理锁串行所有会话，应按 CPU/GPU 预算建立 worker pool。

### A.4.9 直接 RTMP 与多会话（P2，可选）

- **直接 RTMP**：适合无 OBS 的云端无人直播，但会增加编码、重连、密钥管理、平台验收和事故恢复责任。建议保留 OBS 为桌面默认路径，将 RTMP 做成服务器部署档位。
- **多会话**：只有在“一个服务同时运营多个直播间”成为明确需求时才实施。运行态应从全局控制器拆成 `SessionRegistry + LiveSessionController per session`，数据库、队列、统计、OBS/推流资源和媒体节点必须按 `session_id` 隔离。
- 不建议直接复制 LiveTalking 的“单进程 + 每会话多线程”模式；应采用控制平面、媒体 worker 和 GPU 调度分离，并定义每张卡的并发/显存准入策略。

## A.5 推荐目标架构

```text
Bilibili / Douyin / Webhook / Operator / ASR(可选)
                         │
                         ▼
        AI-LiveStream-Agent Control Plane（保留）
  Priority Queue → Role/LLM/RAG/Tools → Guardrails → TTS
                         │
             标准 AudioFrame + generation + PTS
                         ▼
          Media Session Gateway（新增抽象层）
              │                   │
              ▼                   ▼
  Procedural Renderer       GPU Avatar Render Node
      （降级/CI）          Wav2Lip / MuseTalk / future
              │                   │
              └─────────┬─────────┘
                        ▼
            Compositor + Unified A/V Clock
              │          │          │
              ▼          ▼          ▼
        VirtualCam     WebRTC      Recorder
              │          │
              ▼          └── Remote Console / Interactive Client
             OBS
              │
              ▼
       Bilibili / Douyin / other platform

可选云端档：Compositor → RTMP Output → Platform/CDN
```

关键设计原则：

1. **控制平面不感知模型细节**：角色、商品、弹幕和合规层只产生已审核文本/动作意图。
2. **媒体平面不拥有业务真相**：媒体节点可以失败和替换，但不能修改订单、商品或直播场次状态。
3. **全链路同一代际与时基**：`session_generation`、`audio_generation`、`audio_id` 和 PTS 从 TTS 一直传播到播放、口型、录制和预览。
4. **降级优先**：GPU 节点不可用时切回程序化头像；WebRTC 不可用时保留 MJPEG；直接 RTMP 不影响 OBS 路径。
5. **能力如实上报**：区分 `procedural`、`neural_lipsync`、`forced_alignment`、`streaming_tts`、`webrtc`、`direct_rtmp`，UI 不得把配置项显示成已具备能力。

## A.6 分阶段实施路线

### 阶段 0：媒体契约和基线指标（约 1 周，P0）

- 抽取 `AvatarRenderer`、`AudioFrame`、`MediaOutput` 契约。
- 为现有 Procedural/Remote/VirtualAudio/MJPEG 路径补统一能力和延迟指标。
- 记录 TTS 首包、首个可听帧、首个口型帧、A/V 偏差、打断停止时间、队列水位。
- 不改变用户可见功能，先固定回归基线。

**完成标准**：现有 215 项历史测试继续通过；程序化路径行为无回退；所有媒体事件可由 `audio_id` 串联诊断。

### 阶段 1：神经渲染 sidecar + 帧级流式音频（约 3～5 周，P0）

- 建立独立 GPU 服务和版本化协议。
- 接通 Wav2Lip 作为第一后端，保留程序化自动降级。
- 将 TTS 改为安全语义块 + PCM 帧级消费。
- 实现模型预热、Avatar 缓存、GPU 预算和节点健康检查。

**完成标准**：满足 A.4.1/A.4.2 的 FPS、A/V、打断、首包和故障降级指标；真实显卡连续运行 30 分钟无资源增长失控。

### 阶段 2：Avatar 任务、动作状态机和录制（约 2～3 周，P1）

- 完成持久化 Avatar 构建任务与 UI。
- 增加素材化动作状态机。
- 增加统一录制和业务事件时间线。

**完成标准**：视频上传到试播形成完整闭环；进程重启不丢任务；录制文件 A/V 同步并可关联场次审计。

### 阶段 3：WebRTC/WHEP 预览（约 2～3 周，P1）

- 增加只读预览会话、鉴权、ICE/TURN 配置和连接资源回收。
- 控制台优先 WebRTC，失败回退 MJPEG + WebSocket 音频。

**完成标准**：局域网和公网/TURN 各完成一次长时预览；多观察者不会创建重复 GPU 推理；弱网不会反压主直播。

### 阶段 4：场景化扩展（P2）

按产品需求选择，不建议同时启动：

- 客服/展厅：ASR + VAD + AEC + 语音打断。
- 云端无人直播：直接 RTMP、密钥保险库、重连和平台接收校验。
- 多直播间 SaaS：Session Registry、租户隔离、GPU scheduler、配额和水平扩缩容。

## A.7 不建议直接照搬的实现

1. **不要整体复制仓库**：会引入另一套 aiohttp、线程、配置、会话和媒体生命周期，与当前 FastAPI/Electron 架构冲突。
2. **不要照搬每会话多线程扩展模式**：LiveTalking 最近仍修复过会话移除后线程未停止的问题；当前项目应继续使用明确所有权、代际 fencing 和可等待清理。
3. **不要照搬全局串行 ASR 锁**：它保护共享模型，但会把所有请求排队，不适合作为未来多会话架构。
4. **不要使用仅内存的 Avatar 任务表**：进程重启即丢任务状态，且单 worker 无恢复/租约机制。
5. **不要照搬任意来源 CORS 和无鉴权媒体 API**：文本驱动、录制、会话管理、WHEP 和推流接口必须鉴权、限流和限制 Origin。
6. **不要直接采用其 Dockerfile**：该文件仍基于旧 CUDA 11.6/Python 3.10/旧目录名，而 README 的当前测试基线是 Python 3.12、PyTorch 2.9.1、CUDA 12.8，两者不一致，参见其[当前 Dockerfile](https://github.com/lipku/LiveTalking/blob/b3e7490a20e7a6330492f8a6ea8200a5d50279d1/Dockerfile)。
7. **不要复用 shell 拼接式 FFmpeg 命令**：所有外部进程参数必须数组化、路径化和超时化，防止命令注入及僵尸进程。
8. **不要把 README FPS 当作本项目验收结果**：性能数据只能作为容量规划参考，必须在本项目目标显卡、Avatar、分辨率和并发条件下重新压测。

## A.8 许可、模型与品牌风险

LiveTalking 仓库标注 Apache-2.0，但 README 同时声明基于该项目发布到部分平台的视频需带 LiveTalking 水印/标识；神经模型代码、权重、第三方人脸检测组件和训练素材还可能有各自许可。实施前应完成：

- 代码文件 SPDX/NOTICE 与第三方依赖清单审查；
- Wav2Lip、MuseTalk、Ultralight 代码和权重的商用许可逐项确认；
- README 水印声明与 Apache-2.0 的适用关系由法务确认；
- 主播肖像、声音克隆样本、训练视频的授权与可撤回记录；
- 禁止未经授权直接把目标仓库模型权重打入 Electron 安装包。

本次仅做架构和功能分析，**未复制 LiveTalking 源码，也未把其依赖或模型引入当前项目**。

## A.9 最终建议

若只批准一个研发方向，应选择：

> **先把现有 Remote GPU 协议升级为标准帧级媒体协议，接入独立 Wav2Lip/MuseTalk 渲染节点，同时将整句 TTS 改造成可取消的 PCM 帧流水线。**

这条路线直接解决当前项目最大的产品短板——数字人真实感和首句延迟，同时最大程度复用已经较成熟的弹幕、运营、合规、抢占、OBS 和监控能力。Avatar 生成、录制、动作编排紧随其后；WebRTC、ASR、直接 RTMP 和多会话按明确业务场景开启，避免无目标扩张。

**资料合规说明：Content was rephrased for compliance with licensing restrictions.**

---

# 附录 B：媒体基础改造第一阶段实施记录

**实施日期：2026-09-14**<br>
**范围：统一音频帧契约、整句事务后帧化、媒体能力声明、状态与 Prometheus 指标、现有程序化及 Remote GPU 后端兼容。**

## B.1 本阶段完成项

1. **统一媒体契约**
   - 新增不可变 `AudioFormat`、`AudioFrame`、`MediaCapabilities`，统一使用 `pcm_s16le`、sample-based PTS、连续 `sequence`、`audio_id`、`audio_generation` 和 `session_generation`。
   - `BaseMediaDriver` 只增加非抽象默认能力，不改变原有抽象方法，旧驱动和旧测试替身仍可继续实现两参数 `feed_audio_chunk(data, text)`。

2. **保留可靠性边界的事务后帧化**
   - `_collect_tts_sentence()` 仍先完成整句 TTS 事务；只有整句成功后，`AudioFramePipeline` 才解码并按默认 40ms 帧长生成连续 PCM 帧。
   - 单句解码上限为 30 秒；解码器、codec 或输入声明异常时自动回退旧整句路径，不把 partial TTS 音频泄漏到任何 sink。
   - 浏览器 `AUDIO_CHUNK` 和 Remote GPU 提交仍使用完整、可独立播放的原始容器，未改变旧客户端协议。

3. **现有播放与渲染后端适配**
   - `VirtualAudio.play_frames()` 校验批次连续性和双代际后，第一阶段先合并 PCM 并复用现有单 worker、播放 cursor 和细粒度写入逻辑；状态明确标记 `frame_batch_mode=coalesced_transaction`。
   - 程序化 Avatar 可接收标准帧批次，但仍对完整文本执行一次 G2P，并复用共享播放 cursor 对齐，避免逐帧重复解码/G2P 和句间上下文丢失。
   - `MediaRouter` 优先调用新驱动批次入口；旧驱动则合并为一次 PCM 调用，保持旧签名兼容。
   - Remote GPU 只增加机器可读能力声明，明确 `accepts_audio_frames=false`、`chunk_semantics=transactional_sentence`；WebSocket v1/v2 envelope、鉴权、request ID、`audio_end`、cancel 和视频 PTS 语义未改动。

4. **状态、队列与指标**
   - `/api/v1/live/media/status` 和健康摘要新增 `audio_pipeline`、`virtual_audio` 与当前驱动 `media_contract`。
   - Prometheus 新增帧化事务总数、成功/回退数、帧总数、最近解码耗时、VirtualAudio 待处理队列等指标。
   - 能力如实上报 `stage=transaction_post_decode`、`true_streaming_tts=false`，不把整句完成后的帧化描述成 TTS 首包流式优化。

## B.2 保持不变的兼容契约

- TTS partial 失败不得进入媒体、声卡或浏览器 sink。
- P0/Barge-in 仍先推进并停止 VirtualAudio generation，再打断媒体、TTS 和客户端广播；旧 producer 恢复后不能复活旧音频或口型。
- 旧 `feed_audio_chunk(data, text)` 与 `play_chunk(data, fallback_sample_rate)` 替身继续工作。
- `AUDIO_CHUNK` 继续是完整容器；Remote v2 继续等待匹配的 `audio_end` 后才提交事务。
- OBS、弹幕、运营业务、RAG/合规和桌面控制平面没有迁移到第二套服务。

## B.3 实际验证结果

| 门禁 | 命令 | 实测结果 |
|---|---|---|
| 音频可靠性 | `python -m pytest -q server/tests/test_audio_reliability.py` | `11 passed` |
| Remote GPU 协议 | `python -m pytest -q server/tests/test_remote_gpu_comprehensive.py` | `6 passed` |
| 媒体能力边界 | `python -m pytest -q server/tests/test_media_capability_boundaries.py` | `5 passed` |
| TTS/播放/打断专项 | `python -m pytest -q server/tests/test_audit_remediation.py -k "tts or audio or playback or barge"` | `4 passed, 18 deselected` |
| 核心媒体专项 | `python -m pytest -q server/tests/test_core_engine.py -k "media or audio or virtual_audio or av_sync or remote_gpu or minimax or cosyvoice"` | `11 passed, 41 deselected` |
| API/状态/指标专项 | `python -m pytest -q server/tests/test_api_endpoints.py -k "virtual_audio or live or media or metrics"` | `11 passed, 42 deselected` |
| 全量服务端测试 | `python -m pytest -q server/tests` | `215 passed in 23.64s` |
| Python lint | `python -m ruff check launcher.py scripts server` | `All checks passed!` |
| Python 编译 | `python -m compileall -q launcher.py scripts server` | 通过 |
| Web/Desktop JS 语法 | `node --check "server/static/js/console.js"`；`node --check "apps/desktop-ui/main.js"` | 均通过 |
| 运行时预检 | `python scripts/runtime_preflight.py` | 通过 |
| 补丁空白检查 | `git diff --check` | 通过；仅有工作区既有 LF→CRLF 提示 |

验证期间发现并修复了程序化渲染器的一处状态竞态：线程池解码期间，渲染线程可能因暂时空队列提前清除 `is_speaking`。现在在提交完成点重新确认“已接受待播音频”，后续仍由下一渲染 tick 按共享时钟或队列真实状态收敛，代际打断逻辑保持不变。

## B.4 尚未完成与后续接入点

本阶段建立的是可回滚媒体基础，不包含以下能力，也不应据此宣称已经完成：

1. **真正流式 TTS 首包**：当前仍是整句事务成功后解码和帧化，未降低 TTS 首包等待；下一阶段需要可取消的增量 PCM decoder、显式背压和“提交/回滚”隔离，才能兼顾低延迟与 partial 不外泄。
2. **真实神经渲染**：未引入 Wav2Lip/MuseTalk 源码、模型权重或 CUDA 重依赖；后续应以独立 GPU sidecar 接入，复用 `AudioFrame`、双代际、`audio_id` 和 Remote 请求协议，并先完成许可与模型资产审查。
3. **逐帧 VirtualAudio 队列**：当前帧批次会在 30 秒上限内合并后进入现有 worker；下一阶段可改为有界 packet 队列，但必须保持单一播放线程所有权、cursor 单调性和 P0 清队列语义。
4. **WebRTC/WHEP 与统一媒体时钟**：浏览器预览仍使用现有 MJPEG/完整音频容器路径；应在神经渲染和真流式链路稳定后增加只读 WebRTC 输出，避免先引入第二套生命周期。
5. **真实环境验收**：GPU FPS、A/V P95、物理声卡、OBS 平台推流及 30 分钟长稳仍需目标硬件和真实账号验证；自动化通过不能替代这些门禁。

下一阶段建议优先实现“版本化神经渲染 sidecar + 可取消增量 PCM 解码”，先在现有 Remote GPU 边界后接入并保留程序化自动降级，再评估 WebRTC、Avatar 资产流水线和录制能力。

---

# 附录 C：第一阶段复核与媒体二阶段实施记录

**实施日期：2026-09-14**<br>
**交付范围：第一阶段查漏补缺、可取消增量 PCM 原子事务、神经渲染 sidecar v3 接入边界、程序化 shadow 降级与可观测性。**

## C.1 第一阶段复核与补缺

复核确认原有核心契约仍成立：TTS 正常耗尽后才进入 sink，P0/Barge-in 先推进 VirtualAudio generation fence，旧两参数媒体/播放替身继续工作，浏览器仍按完整 `AUDIO_CHUNK` 播放，Remote v2 仍等待匹配 `request_id` 的 `audio_end`。

本轮补齐了以下未闭合边界：

1. `AudioFormat`、`AudioFrame` 和完整帧批次增加采样率、声道、单帧、总字节、总帧数、总时长限制；共享校验器统一检查 format、`audio_id`、双 generation、sequence、sample PTS、唯一首帧和唯一终帧。
2. TTS 整句收集和完整容器解码前增加 32 MiB 上限，避免只在解码后按时长拒绝导致内存峰值失控。
3. `AudioFramePipeline` 增加取消计数和原子完成记录；调用协程取消后，`active_transactions` 会等底层线程真正结束再回落，统计不再提前宣告空闲。
4. VirtualAudio 同时限制单事务与队列总字节，补齐 `Queue.task_done()`，状态暴露 pending bytes；原有单 worker 和 worker-only PortAudio close 所有权不变。
5. Remote v2 在 `audio_end` 携带 `bytes` 时校验其与实际累计音频严格一致，同时拒绝空音频事务；未携带 `bytes` 的旧合法节点仍兼容。
6. 控制器尊重 `prepare_playback=False`；本地声卡提交返回 false 时明确记录为浏览器软降级，不再把分 sink 提交描述成不可实现的跨设备全有或全无事务。
7. raw PCM 浏览器音频统一封装为完整 WAV，事件新增 `source_codec` 保存原始格式，避免把裸 `audio/L16` 交给 HTMLAudio。
8. `/live/media/status` 和系统指标采集对各子组件独立隔离；组件故障时返回 `stale/component_up=false`，不会让整个诊断端点变成 500。

## C.2 可取消增量 PCM 原子事务

新增 `server/core/media/incremental_audio.py`，提供 `IncrementalPCMTransaction` 和全局 `IncrementalAudioPipeline`：

- 支持 TTS transport chunk 落在任意字节位置，按样本边界持续重组为固定时长 `AudioFrame`。
- 始终保留最后一个 payload，直到 `finish()` 时才生成唯一 `is_final=true` 的终帧。
- 状态为 `open -> committed/aborted`；`abort()` 幂等清除所有暂存 PCM，提交校验异常也会自动释放活跃事务槽位。
- 同时限制时长、字节和活跃事务数，并暴露 opened/committed/aborted/active、帧数、PCM 字节和最近结果指标。
- `LiveSessionController` 在合成前生成 `audio_id`。CosyVoice 等声明为 PCM16 的 provider 会在 chunk 到达时增量帧化；MP3/容器 provider 和旧测试替身继续使用完整容器管线。

当前模式明确为 `transaction_mode=atomic_staged`、`progressive_playback=false`、`true_streaming_tts=false`。也就是说，它降低完整 PCM 的重复解码和峰值处理开销，并建立取消/背压基础，但仍在完整语义块成功后才进入 renderer、声卡和浏览器。若未来要降低可听首包延迟，应先把已审核文本切成更短、可独立提交的语义块；已经写入 DAC 的 speculative 音频无法回滚。

## C.3 神经渲染 sidecar v3 边界

新增：

- `server/core/media/sidecar_protocol.py`：独立协议 v3、严格长度前缀二进制 envelope、PCM/JPEG payload 上限、JSON header 校验，以及 request/audio ID、双 generation、sequence、sample PTS 字段。
- `server/adapters/media/neural_sidecar_driver.py`：只负责 Avatar renderer，不承担 TTS；支持 auth/capabilities、显式 credit 背压、render open/finish/cancel、视频事务容量、PTS 上界、取消后连接 epoch 隔离和共享播放 cursor 时间线。
- `scripts/neural_sidecar_server.py`：可运行的程序化协议夹具，用于联调 v3；它明确上报 `neural_lipsync=false`，不会冒充真实 Wav2Lip/MuseTalk。

路由行为：

1. 本地程序化 renderer 始终先消费同一完整帧批次，作为 shadow fallback。
2. sidecar 投递由受控后台任务执行，不阻塞本地声卡和浏览器音频关键路径；sidecar 繁忙时当前句直接保持 shadow，而不是无界排队。
3. 只有通过能力协商、显式声明 renderer/backend 可用且产生合法 JPEG 后，sidecar 画面才获得预览优先级；错误 payload、断连、超时或无可用模型会自动回到本地画面。
4. 视频 PTS 必须位于对应音频总样本范围内；时间线有任务数、总视频字节和“音频时长 + grace”deadline，避免超大 PTS 长期占锁。
5. 打断先由既有 VirtualAudio generation fence 生效，再取消受控 sidecar 任务；停止过程中无论 sidecar 是否超时，本地 renderer 的 `stop()` 都在 `finally` 中必达。
6. 安全策略只允许 loopback 使用 `ws://`；非本机节点必须使用 `wss://` 且配置 token，默认保留 TLS 证书验证。

旧 `RemoteGPUMediaDriver` 的 v1/v2 TTS+视频协议未升级或替换；新的 renderer-only v3 使用独立配置组和独立 driver，两条路径可以渐进迁移。

## C.4 配置与联调

在 `ApiProviderConfig` 中增加一条激活配置即可挂载新 renderer，现有数据库 schema 无需迁移：

| 字段 | 值/说明 |
|---|---|
| `config_group` | `neural_renderer` |
| `base_url` | 本机可用 `ws://127.0.0.1:8890/ws/render-v3`；远程必须 `wss://...` |
| `encrypted_api_key` | sidecar token；远程节点必须配置 |
| `model_name` | backend ID，未指定时为 `auto` |
| `extra_params_json` | 可选 `{"backend_id":"...","avatar_id":"..."}` |
| `is_active` | `1` |

协议联调时由用户在独立终端运行：

```powershell
python scripts/neural_sidecar_server.py --host 127.0.0.1 --port 8890 --token "<local-test-token>"
```

该参考节点只验证协议和故障降级，不是神经模型性能基线。正式 sidecar 应在独立 GPU 镜像中实现相同 v3 协议，并在 capabilities 中如实给出 backend、模型版本、warmup 和 `neural_lipsync` 状态。

## C.5 可观测性

新增或扩展的状态/Prometheus 指标包括：

- 完整容器帧化取消数，以及最近 started/completed outcome。
- 增量 PCM opened、committed、aborted、active、帧数、PCM 字节和事务耗时。
- VirtualAudio pending bytes、单事务/队列总字节预算。
- sidecar connected、ready、degraded、协议/节点/backend 版本、事务成功/失败、丢帧、pending timeline 和 pending video bytes。
- 媒体状态区分 `neural_lipsync_supported` 与当前 active，断连或空 capabilities 不会 fail-open。

## C.6 实际验证结果

| 门禁 | 实测结果 |
|---|---|
| 第一阶段媒体专项（音频可靠性、Remote GPU、能力边界） | `22 passed` |
| TTS/音频/播放/打断专项 | `15 passed, 18 deselected` |
| API live/media/metrics/virtual_audio 专项 | `11 passed, 42 deselected` |
| Core media/audio/AV/Remote/TTS 专项 | `11 passed, 41 deselected` |
| 全量服务端测试 | `215 passed in 22.95s` |
| 增量 PCM append/finish/abort smoke | PASS |
| sidecar v3 envelope round-trip smoke | PASS |
| finish 失败事务槽位回收 smoke | PASS |
| sidecar 非阻塞 shadow smoke | PASS |
| 非本机明文 `ws://` 拒绝 smoke | PASS |
| `python -m ruff check launcher.py scripts server` | PASS |
| `python -m compileall -q launcher.py scripts server` | PASS |
| Web/Desktop `node --check` | PASS |
| `python scripts/runtime_preflight.py` | PASS |
| `git diff --check` | PASS；仅有工作区既有 LF→CRLF 提示 |

## C.7 仍需真实环境完成的工作

本轮完成的是神经渲染协议、客户端、控制面、程序化协议夹具和自动降级，**没有复制 LiveTalking 源码，没有引入 Wav2Lip/MuseTalk 权重，也没有宣称真实神经渲染已经交付**。以下仍需单独执行：

1. 完成模型源码、权重、训练素材、肖像和品牌水印的许可审查。
2. 在独立 GPU sidecar 镜像实现真实 backend 的 load/warmup/open/push/finish/cancel，并如实上报模型版本与就绪状态。
3. 在目标显卡验证 25fps、显存峰值、OOM 降级、取消收敛和 30 分钟长稳。
4. 使用真实声卡、OBS 和平台端验证 A/V P95、漂移、打断后旧音频/旧口型停止时间。
5. Edge MP3 的真正增量解码仍需 stateful codec parser（例如受控 FFmpeg/PyAV）；当前只有原生 PCM provider 进入增量事务。
6. 浏览器 progressive PCM/WebRTC、逐 packet VirtualAudio 队列、Avatar 资产流水线和服务端录制仍属于后续阶段。

下一步应先在隔离 GPU 环境接入一个经过许可审核的 Wav2Lip backend，沿 v3 协议完成真实硬件门禁；协议稳定后再接 MuseTalk，并继续保留程序化 shadow 和旧 Remote v1/v2 回退。

---

# 附录 D：已授权 Wav2Lip GPU sidecar 实施与验收边界

**实施日期：2026-09-14**<br>
**范围：商业授权 fail-closed 门禁、外部插件/权重/Avatar 身份校验、独立 GPU sidecar、v3 流式事务、严格取消、资源遥测和 30 分钟验收工具。**<br>
**本机结论：`CODE/PROTOCOL PASS`；真实授权模型与 GPU 验收 `SKIPPED_BLOCKED`。**

## D.1 许可结论与资产边界

官方 [Wav2Lip README](https://github.com/Rudrabha/Wav2Lip/blob/master/README.md) 将公开仓库、开源结果和权重限定为个人、研究、学术或非商业用途，并要求商业使用另行联系权利方。因此，本阶段没有 clone、vendor、复制或下载官方 Wav2Lip 源码/权重，也没有把许可不明确资产打入主 Python 或 Electron 包。商业直播只能使用用户提供、已完成人工审核并有明确商业授权记录的实现和权重。

新增门禁默认拒绝运行：license manifest 必须同时提供 `accepted=true`、实现来源/许可/允许用途/人工审批记录、权重来源/许可/商业授权记录，以及 implementation、weights 的 SHA-256；Avatar manifest 必须提供 `avatar_id`、revision、源文件摘要、预处理 profile 和插件资产摘要。示例文件故意保留 `accepted=false`、`human_approved=false`、`commercial_use_authorized=false` 和占位摘要，不能直接启动生产服务。

**资料合规说明：Content was rephrased for compliance with licensing restrictions.**

## D.2 已实现内容

1. **独立且可插拔的 GPU backend**
   - 新增 `gpu_sidecar/`，只定义授权 manifest、配置、动态 `module:callable` factory、engine/session 契约和 v3 server，不携带 Wav2Lip 实现或权重。
   - 校验原始 implementation、weights、Avatar source/assets 的 SHA-256 后，建立只读 content-addressed 私有临时 snapshot；插件只能从已校验 snapshot import/open，避免摘要检查后继续使用可变源路径。
   - 只有 `torch.cuda.is_available() is True`、CUDA device 合法、factory/engine/session 契约完整且 warmup 成功，descriptor 才会同时声明 `available/neural/warmed/license_approved=true`。

2. **并发、取消和故障隔离**
   - receiver、模型 worker、唯一 sender 分离；ingress/egress、credit、连接数和活动渲染数均有界。
   - `prepare/open_session/push/finish/close` 使用真实 executor task 所有权；工作线程不直接发布 ready 状态。snapshot、warmup 或模型调用 timeout/cancel 后，若线程无法在 grace period 内退出，会撤销 ready、永久隔离 model lock、保留 snapshot，并要求外部 supervisor 强制终止进程，禁止不可杀线程后台继续时接收新 GPU 工作。
   - owned worker 与 late reaper 负责关闭晚到 engine/取消晚到 session；`close()` 只有取得模型锁并证明 engine 已静止时才清理 snapshot，避免 TOCTOU 和 use-after-cleanup。
   - stream reservation 在 async iterator 返回前同步登记，关闭“已创建事务但尚未登记 producer”时 cancel 先 ACK、插件后启动的竞态。
   - 插件必须严格声明 `cancel_threadsafe=true`、`cancel_quiesces=true` 并暴露标准 `threading.Event inference_active`。backend 只有在 `session.cancel()`、producer future、`inference_active` 和 model lock 全部收敛后才允许 server 发送 `render_cancelled {cancel_ack:true, quiesced:true}`。

3. **v3 协议和主进程集成**
   - 保持 `PROTOCOL_VERSION=3`、`LAS3` 及既有 audio/video envelope；新增 additive `render_started` control 和 `supports_render_started=true`，不破坏旧 Remote v1/v2。
   - 客户端默认 `require_neural_lipsync=true`，严格交叉核对 backend/model/weights/license/avatar descriptor；首张合法 JPEG 到达后立即进入与 VirtualAudio 共享的 sample-PTS timeline，不等待整句 `render_complete`。
   - 本地程序化 renderer 始终作为 shadow fallback；真实节点断连、OOM、timeout、契约失败或能力不符时，直播控制面仍可降级，不把程序化画面冒充 neural lipsync。

4. **黑盒验收工具**
   - `scripts/wav2lip_gpu_preflight.py` 输出结构化 Python/torch/CUDA/GPU、manifest、warmup 和资源状态，且不自动安装或下载依赖。
   - `scripts/wav2lip_sidecar_acceptance.py` 校验 descriptor 身份、strict totals/credit、JPEG、media FPS、throughput FPS、RTF、frame/timeline coverage、sample-PTS cadence、endpoint PTS gap、首帧、取消 barrier/ACK、ACK 后旧帧、取消后 model-lock 探针，以及 CUDA allocated/reserved/RSS 的斜率和峰值增长。
   - 30 分钟长稳使用 absolute deadline；错误、OOM、fallback 或 CUDA 遥测缺失均不会被记为通过。

## D.3 本机实测结果

当前机器为 Python `3.13.7`，PATH 中没有 `nvidia-smi`，且没有 `torch`/CUDA。以下是本轮最终复验，不包含推断值：

| 门禁 | 命令 | 实测结果 |
|---|---|---|
| 全量服务端测试 | `python -m pytest -q server/tests` | `215 passed in 23.37s` |
| Python 编译 | `python -m compileall -q launcher.py scripts server gpu_sidecar` | PASS |
| Ruff | `python -m ruff check launcher.py scripts server gpu_sidecar` | `All checks passed!` |
| sidecar 定向 mypy | `python -m mypy gpu_sidecar/backend.py gpu_sidecar/contracts.py gpu_sidecar/server.py scripts/wav2lip_sidecar_acceptance.py` | `Success: no issues found in 4 source files` |
| Electron/Web JS | `node --check apps/desktop-ui/main.js`；`node --check server/static/js/console.js` | 均 PASS |
| 主运行时预检 | `python scripts/runtime_preflight.py` | `ok=true`，Python `3.13.7`，exit `0` |
| 补丁空白检查 | `git diff --check` | exit `0`；仅有工作区既有 LF→CRLF 提示 |
| 并发生命周期内存 smoke | blocked warmup late return、pending-stream cancel、late open cleanup | PASS；未创建新测试文件 |
| 独立语义复审 | 最终 sidecar lifecycle/cancel 复审 | `APPROVED`；无高/中问题；低风险项为未新增持久化竞态单测 |
| GPU 环境预检 | `python scripts/wav2lip_gpu_preflight.py --environment-only` | `SKIPPED_BLOCKED`，exit `2`；`nvidia-smi` 不可用，`No module named 'torch'` |
| 默认许可门禁 | 使用三个 example 文件启动 `wav2lip_sidecar_server.py` | 明确拒绝 `accepted=false`，exit `1` |
| 不可达节点验收 | acceptance 指向未监听 loopback 端口，`--stability-minutes 0` | `SKIPPED_BLOCKED`，exit `2` |

此前 procedural v3 fixture 已完成完整事务、generation/connection epoch、降级和 raw `render_cancel` ACK smoke；fixture 明确声明 `neural_lipsync=false`，该结果只证明协议与降级路径，不是 Wav2Lip 质量或性能证明。

## D.4 未执行和不得宣称通过的项目

以下项目在本机均未执行，不能由 `215 passed`、程序化 fixture 或验收工具存在本身替代：

- 已授权真实 Wav2Lip factory、权重和 Avatar snapshot 的 import/load/warmup；
- 目标 GPU 的 media FPS、throughput FPS、RTF、首帧和实际 CUDA allocated/reserved VRAM；
- 真实推理中的 cancel latency、GPU kernel/producer 收敛和 post-cancel model-lock 探针；
- 真实节点连续 30 分钟运行、显存/RSS 斜率、峰值增长、OOM 和 fallback 计数；
- 物理声卡、VB-Cable、OBS/平台接收端的 A/V presentation：`NOT_MEASURED`；
- 人工观看的口型自然度、身份保持和主观质量：`NOT_MEASURED`。

验收脚本中的 A/V 数值是协议 sample-PTS cadence/coverage/endpoint gap，**不是**扬声器、虚拟声卡、OBS 编码或平台播放端的物理 A/V drift。当前仓库也未收到商业授权证明、获批实现目录、权重、真实 SHA-256、Avatar manifest 或目标 GPU 主机，因此真实 neural/GPU 状态保持 `SKIPPED_BLOCKED`。

## D.5 目标 GPU 环境执行步骤

执行前必须由用户/法务提供并审核：独立 GPU Python 环境及匹配的 CUDA PyTorch、获批实现目录、获批权重文件、implementation/weights SHA-256、商业授权与人工审批引用、Avatar source/assets 及摘要、factory `module:callable`、目标 backend/model/avatar 标识，以及远程 TLS 证书、私钥、CA 和高熵 token。不要把占位 example 改成 `true` 来绕过审核。

1. 复制三个 example 到受控配置目录并替换全部占位值，然后先运行完整门禁：

```powershell
python scripts/wav2lip_gpu_preflight.py `
  --config "<approved-sidecar-config.json>" `
  --license "<approved-license-manifest.json>" `
  --avatar "<approved-avatar-manifest.json>"
```

只有报告 `status=PASS`，并且 descriptor 中 backend/model/weights/license/avatar/GPU 字段与审批记录逐项一致，才可启动服务。

2. 远程 GPU 节点必须使用原生 TLS；只有 loopback 或受信本机反向代理终止 TLS 时才允许明文：

```powershell
python scripts/wav2lip_sidecar_server.py `
  --config "<approved-sidecar-config.json>" `
  --license "<approved-license-manifest.json>" `
  --avatar "<approved-avatar-manifest.json>" `
  --host "0.0.0.0" --port 8890 `
  --token "<high-entropy-token>" `
  --tls-cert "<server-cert.pem>" `
  --tls-key "<server-key.pem>"
```

3. 从验收主机执行真实 30 分钟黑盒门禁。所有摘要和标识必须取自已批准 manifest/full preflight，不能使用示例值：

```powershell
python scripts/wav2lip_sidecar_acceptance.py `
  --url "wss://<gpu-host>:8890/ws/render-v3" `
  --token "<high-entropy-token>" `
  --tls-ca "<trusted-ca.pem>" `
  --backend-id "<approved-backend-id>" `
  --model-version "<approved-model-version>" `
  --weights-sha256 "<approved-weights-sha256>" `
  --license-manifest-digest "<approved-license-manifest-sha256>" `
  --avatar-id "<approved-avatar-id>" `
  --avatar-revision "<approved-avatar-revision>" `
  --avatar-digest "<approved-avatar-manifest-sha256>" `
  --audio-wav "<representative-pcm16.wav>" `
  --stability-minutes 30 `
  --report "<acceptance-report.json>"
```

4. acceptance 必须整体为 `PASS`，且 performance、cancellation、resource telemetry、long stability 均分别通过；随后仍需用物理声卡/虚拟声卡、OBS 录制和平台回放测量真实 A/V presentation，并单独完成人工口型质量签收。任何 `SKIPPED_BLOCKED`、`BLOCKED`、`SKIPPED_NOT_RUN`、遥测缺失、fallback、OOM 或 supervisor restart 要求都不能作为生产放行依据。

## D.6 阶段结论

本阶段交付的是**已授权外部模型的安全 adapter、严格 v3 sidecar、主进程流式 timeline、fail-closed 门禁和可复现验收工具**，不是内置模型交付。代码、协议 fixture、并发生命周期和当前环境可执行门禁已经通过；真实授权模型加载、GPU FPS/VRAM/cancel/30 分钟长稳仍为 `SKIPPED_BLOCKED`，物理 A/V 与主观口型仍为 `NOT_MEASURED`。在 D.5 所列输入和目标硬件齐备前，不应把该阶段描述为“真实 Wav2Lip 已上线”或“GPU 验收通过”。

---

# 附录 E：2GB 本地 GPU 场景的远端 Avatar Provider 编排

**实施日期：2026-09-14**<br>
**适用场景：个人自用、单直播会话、本地仅 2GB GPU，神经 Avatar 推理由远端节点或厂商 API 承担。**<br>
**阶段结论：厂商无关控制层、sidecar v3 adapter、自动编排与运营保护已完成；具体第三方厂商 API 尚未接通，不能宣称真实厂商渲染或 GPU 验收通过。**

## E.1 2GB 本地 GPU 的明确边界

本地 2GB GPU 不加载 Wav2Lip、MuseTalk 或其他神经 Avatar 模型，也不承担 CUDA 推理、模型 warmup 或权重常驻。主机只运行直播控制面、TTS/PCM 音频管线、共享播放时钟、程序化 Avatar 热 shadow、远端 Provider 编排、预览与 OBS/虚拟摄像头输出。这样可以避免神经模型挤占显存后引发 OOM、音频卡顿或直播控制面失稳。

本地程序化 Avatar 会先消费每句相同的 `AudioFrame` 批次，因此远端不可达、并发繁忙、超时、熔断或额度耗尽时不需要临时启动本地神经模型，也不会阻断音频播放。该 shadow 是程序化口型降级，不是神经口型质量等价替代。

当前机器仍没有 `nvidia-smi`，当前 Python 环境也没有 `torch`/CUDA；本阶段没有据此推断任何本地或远端 GPU 型号、显存占用、FPS 或神经口型质量。

## E.2 已实现的厂商无关能力

1. **Renderer-only Provider 契约**
   - `RemoteAvatarProvider` 只消费已经解码并按采样边界切分的 PCM16 `AudioFrame`，不接收文本作为主渲染输入，也不负责 TTS。
   - 能力模型区分 `realtime` 与 `batch`，并记录协议、输入 codec、严格完成、取消、credit、`render_started`、sample PTS 和能力验证状态。
   - 实时节点进入 auto 池前必须证明：`verified`、神经口型、整句事务、strict completion、cancel ACK、credit backpressure、`render_started`、sample PTS 和 PCM16 输入。
   - 批处理契约已经定义，但当前仓库只注册 `sidecar_v3` 实时 adapter；在没有厂商官方文档前没有虚构通用 HTTP batch 协议。

2. **句级自动选择**
   - 节点按 `mode`（`primary`/`fallback`）和数字 `priority` 稳定排序，数字越小越优先。
   - Provider 在每句开始前固定。当前句失败时只保留已经运行的 procedural shadow，不把同一句迁移、重放到第二个 Provider；下一句才重新选择。
   - `shadow` 和 `disabled` 节点不进入自动承接池。
   - 视频时间线与虚拟摄像头是单发布资源，因此编排器全局只允许一个远端整句在途；不排队，繁忙句立即保留本地 shadow。

3. **熔断、超时和生命周期恢复**
   - 每节点维护 `CLOSED`、`OPEN`、`HALF_OPEN`，支持失败阈值、开路时长和半开探测上限。
   - connect/message/request timeout 均可配置；sidecar 所有发送和接收都受绝对事务 deadline 约束。
   - Provider 首次启动或握手失败不会阻断开播，会按熔断冷却时间在后台单实例重试；stop 会取消重试，并阻止停止后的 late lifecycle commit。
   - 直播中热加载配置会先停止旧 orchestrator。旧 epoch 未确认停止时中止替换，禁止新旧 Provider epoch 并行发布。

4. **严格取消和连接隔离**
   - sidecar render owner 是 WebSocket 的唯一 `recv()` 所有者；外部 interrupt 取消并等待 owner，由 owner 发送 `render_cancel`、读取匹配 request 的 `render_cancelled`。
   - 只有 `cancel_ack=true` 且 `quiesced=true` 才视为远端已静止；无法证明时记录 `CANCEL_UNCONFIRMED`、关闭 connection epoch 并直接打开该 Provider 熔断器。
   - Router 先执行协议取消，再在有界时间内清理后台任务；远端清理卡住也不能无限阻塞本地 generation fence 和设备停止。

5. **额度与并发保护**
   - 策略支持币种、运行期预算（最小币种单位）、告警比例、硬限制、计费单位和单位成本。
   - 请求开始前原子预留，完成后按 Provider 结果核销；无法取得真实账单时使用配置估值。默认对失败和取消尝试保守计费，避免因未知厂商扣费而低估预算。
   - 该额度是单进程运行期保护，不是厂商账单，也不跨进程重启持久化；正式厂商 adapter 应使用厂商返回的 billed units/cost 对账。

6. **配置与密钥安全**
   - 复用 `ApiProviderConfig.extra_params_json` 保存非敏感 policy，不需要数据库迁移。
   - `neural_renderer.is_active` 表示“已启用”，允许多条配置同时 enabled；LLM、TTS 等旧配置组仍保持同组默认项互斥。
   - API key/token 只能通过 `api_key` 写入 `encrypted_api_key`。`extra_params` 会递归拒绝 `api_key`、`auth_token`、`authorization`、`private_key`、password、secret、credential 等敏感键，同时不会误伤 `max_tokens`、`token_limit` 等非凭据字段。
   - `/settings/configs` 保持旧客户端所需的 `extra_params` JSON string 形态，但递归省略历史明文敏感键，并额外返回无密钥的 normalized `provider_policy`。控制台不会回填 Avatar Provider 密钥。
   - adapter 使用显式 allowlist；当前只允许 `sidecar_v3`，数据库字符串不能动态 import 任意模块。

7. **状态、指标和输出仲裁**
   - `/api/v1/live/media/status` 稳定返回 `avatar_provider` 和 `avatar_providers`，包含 selected、up/ready、in-flight、circuit、outcomes、latency、quota 和 capability 状态；即使没有远端首帧也存在。
   - Prometheus 导出 Provider up、ready、selected、in-flight、circuit、requests、outcomes、last latency 和 quota 指标，标签只使用受控 Provider ID 和低基数 outcome。
   - 远端故障不门控 `/readyz`，因为本地 procedural shadow 仍可维持服务。
   - VirtualCameraService 使用短租约 owner/priority 仲裁：神经 sidecar 高于旧 Remote v1/v2，旧 Remote 高于本地程序化帧；高优先级远端停止后，本地 shadow 在租约到期后恢复，避免多 writer 交替覆盖 OBS 帧。

## E.3 v1/v2/v3 兼容结论

- 旧 `RemoteGPUMediaDriver` v1/v2 仍保留在原有 TTS+视频复合分支，没有被静默改成 renderer-only，也不进入新 auto 池。
- 旧 v1/v2 节点缺少 v3 strict completion、credit、sample PTS 和 evidence，因此能力上报改为 `neural_lipsync=false`、`verification=unverified_legacy_v1_v2`；这不否定其旧协议可用性，只是不再把程序化/未知远端画面冒充已验证神经口型。
- `NeuralSidecarMediaDriver` v3 继续保持 renderer-only 协议，并由 `NeuralSidecarAvatarProvider` 适配到新契约。
- `MediaRouter.sidecar_driver` 兼容视图仍保留；多 Provider 调度由 `avatar_orchestrator` 承担。通用 Provider 只有显式满足 `AvatarPreviewProvider` 时才会被 Router 当作 JPEG/媒体预览源。
- 本地 procedural shadow、旧 Remote v1/v2 和 sidecar v3 的预览优先级保持兼容；本阶段没有删除旧 API 或数据库字段。

## E.4 配置示例

以下示例只表示 policy 结构。鉴权值必须通过设置 API 的顶层 `api_key` 字段写入，不得把 token 放进 `extra_params`：

```json
{
  "config_group": "neural_renderer",
  "provider_name": "my-sidecar-v3-primary",
  "base_url": "wss://avatar-node.example/ws/render-v3",
  "model_name": "auto",
  "api_key": "<write-only-secret>",
  "is_active": true,
  "extra_params": {
    "adapter": "sidecar_v3",
    "backend_id": "auto",
    "avatar_id": "default",
    "avatar_revision": "<expected-revision>",
    "avatar_digest": "<expected-sha256>",
    "license_manifest_digest": "<approved-manifest-sha256>",
    "weights_sha256": "<approved-weights-sha256>",
    "model_version": "<expected-model-version>",
    "avatar_provider": {
      "mode": "primary",
      "priority": 10,
      "max_concurrency": 1,
      "render_mode": "realtime",
      "timeouts": {
        "connect_ms": 1000,
        "message_ms": 20000,
        "request_ms": 120000
      },
      "circuit_breaker": {
        "failure_threshold": 3,
        "open_ms": 30000,
        "half_open_max_calls": 1
      },
      "quota": {
        "currency": "CNY",
        "budget_minor": 1000,
        "warning_ratio": 0.8,
        "hard_limit": true,
        "billing_unit": "request",
        "unit_cost_minor": 1,
        "charge_failed_attempts": true
      }
    }
  }
}
```

远程地址必须使用 `wss://`；只有 `localhost`、`127.0.0.1` 或 `::1` 回环地址允许 `ws://`。示例域名、摘要、版本和预算均为占位符，不代表已有真实服务。

## E.5 本轮实际验证

| 门禁/检查 | 实测结果 |
|---|---|
| `python -m pytest -q server/tests` | **PASS：215 passed in 23.37s** |
| `python -m compileall -q launcher.py scripts server gpu_sidecar` | **PASS** |
| `python -m ruff check launcher.py scripts server gpu_sidecar` | **PASS：All checks passed** |
| 新增 Provider/Orchestrator/配置/adapter 定向 mypy | **PASS：4 files，no issues** |
| `node --check apps/desktop-ui/main.js` | **PASS** |
| `node --check server/static/js/console.js` | **PASS** |
| `python scripts/runtime_preflight.py` | **PASS：`ok=true`，外部 Python 3.13.7/64bit** |
| `git diff --check` | **PASS：exit 0；仅工作区既有 LF→CRLF warning** |
| priority、OPEN/HALF_OPEN、句间 failover、quota hard limit | **PASS（inline/in-memory smoke）** |
| 全局并发繁忙时立即 shadow、无候选时 procedural fallback | **PASS（inline/in-memory smoke）** |
| 300 次调度后的 in-flight/reservation 与 tracemalloc | **PASS：无残留；峰值约 71KB** |
| 单 WebSocket recv owner、cancel ACK/quiesced | **PASS（确定性 FakeWS smoke，max concurrent recv=1）** |
| 首次启动失败后台恢复、取消保守计费 | **PASS（inline/in-memory smoke）** |
| 敏感键/别名递归拒绝且不误伤 `max_tokens` | **PASS（inline smoke）** |
| VirtualCamera owner/priority 仲裁 | **PASS（inline/in-memory smoke）** |
| 独立语义复审 | **APPROVED；无剩余高/中风险** |

本阶段遵守“不自动新增持久化测试文件”的约束；上述新增行为使用 inline/in-memory smoke 验证，现有 215 项测试保持通过。

## E.6 尚未接通、不得宣称完成的内容

当前没有收到任何具体远端厂商名称、官方 API 文档、endpoint、鉴权规范、请求/响应示例、错误码、计费单位、并发限制、数据保留政策或可用凭据。因此：

- 没有实现或验证某个第三方厂商的 HTTP/WebSocket adapter；
- 没有向第三方发送项目代码、用户数据、音频、Avatar 资产或凭据；
- 没有测量真实公网首帧延迟、持续 FPS、抖动、丢帧、A/V presentation drift、取消停止时间或厂商账单；
- 没有验证厂商是否返回 strict completion、cancel ACK、sample PTS、license/model/avatar evidence；
- 没有完成真实 OBS 录制、平台回放和人工口型质量签收；
- 没有完成任何本地 2GB GPU 或远端 GPU 的 Wav2Lip/MuseTalk 性能验收。

因此准确表述应为：**“远端 Avatar Provider 基础设施与 sidecar v3 adapter 已实现并通过本地自动化/内存验证；真实厂商 API 接入和真实 GPU/平台验收待输入材料齐备后执行。”**

## E.7 接入真实厂商前置条件与验收顺序

接入一个真实厂商至少需要用户提供并确认：

1. 厂商与产品名称、官方文档 URL、服务区域和 API 版本；
2. 认证方式、正式 `wss://`/HTTPS endpoint、证书/CA 要求和测试凭据；
3. renderer-only 实时或批处理请求/响应示例，音频 codec/sample rate/channels 限制；
4. 视频帧格式、PTS/timebase、首帧/完成事件、取消 ACK 与 quiesced 语义；
5. typed error、retryable/restart flags、限流、并发、任务幂等和服务端资源状态；
6. 计费单位、失败/取消是否计费、预算查询/账单对账接口；
7. 音频与 Avatar 数据的存储区域、保留期、训练使用、删除和隐私条款；
8. Avatar、模型、权重和输出内容的个人/商业使用许可证明；
9. 目标 Avatar ID/revision/digest、模型版本、权重摘要及许可 manifest evidence；
10. 厂商沙箱验收窗口和故障注入条件。

材料齐备后应先新增显式 allowlist adapter 和协议级测试替身，再执行沙箱连接、鉴权失败、超时、限流、额度、取消、断线、重复消息和错误 request ID 验收。最后在真实 OBS/音频设备/平台链路完成至少 30 分钟长稳、A/V presentation、P0 打断停止时间、人工口型质量和账单对账。任何本地 fixture、`215 passed` 或 Provider 状态 `ready` 都不能替代真实厂商与真实平台验收。

## E.8 阶段结论

对于本地仅 2GB GPU 的个人自用场景，当前合理运行方式是：本地保留音频、控制和程序化热 shadow，把已审核的 PCM 帧按句交给远端 renderer-only Provider；远端不可用时立即保持本地画面，下一句再自动选择节点。该控制层、运营保护、配置安全和可观测性已经完成并通过现有环境验证。

真实第三方厂商尚未指定，也没有凭据或协议资料，所以本阶段不把“可接入”写成“已接通”，不把 sidecar fixture 写成“真实神经 Avatar”，不把当前机器写成“GPU 验收通过”。生产或真实直播放行仍取决于 E.7 的厂商、许可、硬件和平台验收。

---

# 附录 F：Avatar Provider 第三阶段——阿里云万相与腾讯云数智人实验接入

**实施日期：2026-09-15**<br>
**范围：阿里云万相实验 Provider 注册表集成、腾讯云智能数智人（IVH）实验 Provider 新实现、注册表/设置 API/向导/preflight 全链路接入与验证。**<br>
**阶段结论：两个大陆厂商均以 experimental/unverified 状态进入后端权威注册表，只能通过显式 sandbox 构造，不可在向导启用，不可进入自动渲染池；真实厂商沙箱验收仍未执行，不能宣称已接通。**

## F.1 阶段边界与状态

| Adapter | 注册表状态 | 实现方式 | 可在向导选择 |
|---|---|---|---|
| `local_procedural` | available | 内置程序化 + shadow | 是（默认） |
| `sidecar_v3` | available | 自建 v3 sidecar（附录 C/E） | 是 |
| `liveavatar_lite` | experimental | 官方 Sandbox 控制面（第二阶段） | 否 |
| `aliyun_avatar` | experimental | 本地 WebSDK bridge（本轮集成） | 否 |
| `tencent_avatar` | experimental | 官方 aPaaS 音频驱动 API（本轮新实现） | 否 |

注册表不再存在 PLANNED 条目；“调研过”不等于“已接入”的原则通过三态状态（available/experimental/planned）与 `selectable` 门禁继续生效。

## F.2 阿里云万相实验 Provider（注册表集成）

前序会话已完成 `AliyunAvatarProvider` 与私有 `las_aliyun_websdk_bridge` 协议（本地 loopback wss bridge 调用公开 `lm-avatar-chat-sdk` v1.1.0）。本轮补齐注册表集成：

1. 新增 `AliyunAvatarConfig` 严格 schema：`sandbox_only` 强制 true、`expected_sdk_sha256`（64 位 hex）、共享 policy；`avatar_provider` 沿用既有策略结构。
2. descriptor 从 PLANNED 升级为 EXPERIMENTAL，挂接 config schema、ui_fields 与诚实能力声明（`verification=unverified`、`local_video_output=false`、`supports_sample_pts=false`、`strict_completion=false`）。
3. endpoint 校验：仅允许本机 loopback `wss://` bridge 地址（禁 query/fragment/URL 凭据），凭据必须以 credential bundle JSON 形式驻留加密 `api_key` 字段；`extra_params` 继续递归拒绝敏感键。
4. `create_avatar_provider` 增加 aliyun factory 分支：构造即解析校验 credential bundle（`bridge_auth_token` + 短期 RTC 初始化材料），experimental 在非 `purpose="sandbox"` 时拒绝装配。

能力边界保持不变：WebSDK 无法提供 request-correlated 完成或视频静止证明，`render_sentence` 只以 `audio_push_accepted` 为诊断终点（`completion_scope=websdk_input_accepted_only`），`interrupt` 后关闭连接并抛 `CANCEL_UNCONFIRMED`。

## F.3 腾讯云智能数智人实验 Provider（新实现）

依据官方公开文档（cloud.tencent.com/document/product/1240，2026-09 检索）实现 `TencentIVHAvatarProvider`，协议事实全部来自文档原文：

- **接入模式**：云渲染会话交互 aPaaS。会话管理走 HTTPS（`https://gw.tvs.qq.com`）：`createsession`（`DriverType=3` 音频驱动、`Protocol` rtmp/webrtc、`StreamMaxInterval`）→ `statsession` 轮询就绪（状态 1/2/3/4）→ `startsession` → `closesession`（释放并发与计费）。
- **鉴权**：AppKey + AccessToken（数智人平台资源管理中心获取）；query 参数按字典序拼接后以 AccessToken 做 HmacSha256 + Base64 + URL 编码生成 signature。签名实现用官方文档 107197 的两处示例向量做了逐字节验证。
- **音频驱动**：WSS `commandchannel` 长连接，`SEND_AUDIO` 指令（PCM 16kHz/16bit/mono Base64，`Seq` 从 1 起）；前 6 个 160ms/5120B 片包全速发送，后续按 120ms 间隔；数据包发完后必须再发送 `IsFinal=true` 空包；`Interrupt=true` 打断当前驱动。
- **下行消息**：`Type=3` 播报状态（`AudioStart`/`AudioOver` 终态，`FinalType` 1=客户 final、2=服务端超时、3=中断）与 `Type=9` 驱动失败，均携带 `ReqId` 可关联。
- **心跳**：`SEND_HEARTBEAT`（`Data.Text=PING`），间隔 31–59 秒（默认 40s）。

实现要点：

1. **fail-closed 生命周期**：start 失败（含轮询超时/失败）时对已创建会话尽力 `closesession` 释放计费后再置 failed；stop 先 `closesession` 再关闭 WSS 与心跳任务。
2. **endpoint 固定官方网关**：base_url 仅允许 `https://gw.tvs.qq.com`（443、无路径/查询/凭据），防止 AccessToken 被重定向到任意主机。
3. **credential bundle v1**：`{version, app_key, access_token}` 整体驻留加密字段；`virtualman_project_id`/`protocol`/`user_id` 等非敏感参数留在受 schema 约束的 `extra_params`。
4. **音频适配**：接受 pipeline `pcm_s16le` 帧批次，线性插值重采样到 16kHz mono（官方唯一接受格式）；BATCH 模式与非 mono 输入按 `CAPABILITY_MISMATCH` 拒绝。
5. **诚实完成语义**：`render_sentence` 等待 `ReqId` 关联的 `AudioOver` 终态才返回（`completion_scope=audio_over_observed`、`request_correlated_completion=true`），并记录 `final_type` 与观测状态序列；`Interrupt` 后即使观察到 `FinalType=3` 也只记录证据，仍按 `CANCEL_UNCONFIRMED` 处理——视频轨静止无法证明。
6. **计费敏感字段不外泄**：`PlayStreamAddr`（可能携带 TRTC userSig）不进入状态或日志，仅暴露 `stream_protocol`。
7. **TRTC 协议未开放**：实验阶段仅允许 rtmp/webrtc；trtc 需要房间与签名配置，留待验收阶段评估。

## F.4 注册表与门禁

- `normalize_avatar_provider_config` 为两个新 adapter 增加 endpoint 规则与凭据存在性校验；`create_avatar_provider` 增加 factory 分支并维持 experimental 沙箱门禁（live 装配直接拒绝）。
- 前端无需改动：向导按注册表动态渲染，experimental 显示为禁用卡片（“实验/未验证，未通过验收不可启用”）；设置页“新增自定义节点”仍固定 `sidecar_v3`，experimental 实例只能通过 API/沙箱脚本创建，符合“实验 Provider 不进入普通用户创建路径”的门禁设计。
- preflight 对 experimental 配置给出明确警告：不进入自动渲染池，不能视为已验收。
- neural_renderer 凭据继续禁止通过 `/configs/{id}/raw-key` 回填明文。

## F.5 本轮实际验证

| 门禁/检查 | 实测结果 |
|---|---|
| 阿里云注册表/工厂/生命周期 inline smoke | PASS：49/49（含 FakeBridge 握手、音频 envelope、BATCH 拒绝、CANCEL_UNCONFIRMED、握手失败路径） |
| 腾讯签名官方测试向量 | PASS：两处示例（HTTPS 与 WSS 多参数）逐字节一致 |
| 腾讯注册表/工厂/生命周期 inline smoke | PASS：52/52（含 24k→16k 重采样、Seq/pacing/final 包、FinalType=1/3、轮询、业务错误、失败路径 closesession 释放计费、非官方网关构造拒绝） |
| API 全链路 inline smoke（TestClient + 独立临时数据目录） | PASS：43/43（providers 接口 5 adapter 状态、阿里云/腾讯实例保存、同厂商多实例 ID 不覆盖、trtc 拒绝、脱敏与 policy、raw-key 403、同 ID 更新不新增、preflight experimental 警告、删除清理） |
| 全量服务端测试 `python -m pytest -q server/tests` | **PASS：215 passed**（本轮三次复跑均通过） |
| `python -m compileall -q launcher.py scripts server gpu_sidecar` | PASS |
| `python -m ruff check launcher.py scripts server gpu_sidecar` | PASS：All checks passed |
| 新增/改动 Provider 定向 mypy（aliyun/tencent/registry） | PASS：3 files, no issues |
| `node --check` console.js / main.js | 均 PASS |
| `python scripts/runtime_preflight.py` | PASS：`ok=true`，Python 3.13.7 |
| `git diff --check` | PASS：仅工作区既有 LF→CRLF 提示 |

本轮遵守“不自动新增持久化测试文件”约束，全部新行为以 inline/in-memory smoke 验证，现有 215 项测试保持通过。

## F.6 尚未完成、不得宣称通过的内容

1. **未执行任何真实厂商调用**：没有阿里云万相或腾讯云 IVH 的账号、AppKey/AccessToken、已购形象与交互并发、测试凭据或沙箱窗口；本机验证全部使用协议替身（FakeBridge/FakeIVHApi/FakeCommandChannel）。
2. **真实厂商语义未验收**：鉴权失败错误码映射、限流与并发行为、计费单位与账单对账、`AudioOver` 与视频画面的真实时序、`FinalType=3` 后视频是否静止、真实 16kHz 音频的口型质量，均未经真实链路验证。
3. **视频轨未消费**：两个实验 Provider 均声明 `local_video_output=false`；腾讯 `PlayStreamAddr`（rtmp/webrtc/trtc）未接入本地虚拟摄像头或预览，`supports_sample_pts=false`。
4. **未升级 availability**：experimental → available 需按 E.7 顺序完成沙箱验收（连接、鉴权失败、超时、限流、额度、取消、断线、重复消息、错误 request ID），再经 30 分钟长稳、A/V presentation、打断停止时间、人工口型质量与账单对账；当前不满足任何一项。
5. **腾讯端渲染/播报服务未纳入**：端渲染 SDK（UE/H5）与播报视频生成 API 与 renderer-only 实时契约不匹配，未实现也不应宣称支持。

## F.7 真实验收前置条件（在 E.7 基础上细化）

**阿里云万相**：数智人平台账号与已购形象、短期 RTC 初始化材料获取方式、公开 WebSDK 版本与 SHA-256 固定、本地 bridge 的部署与鉴权、音频驱动会话的计费单位。

**腾讯云数智人**：数智人平台（xiaowei.cloud.tencent.com/ivh）项目与 `VirtualmanProjectId`、`AppKey/AccessToken`、已购交互并发与形象、错误码列表（文档 100406）、音频驱动会话计费口径（会话时长/并发）、`PlayStreamAddr` 的消费方式（rtmp 拉流或 webrtc）与首帧/时延测量手段。

材料齐备后按 F.6 第 4 条顺序执行；在此之前，向导与自动渲染池不会出现这两个厂商，直播画面继续由本地程序化 shadow 承接。