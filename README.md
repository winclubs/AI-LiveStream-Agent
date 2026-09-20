# AI-LiveStream-Agent · 全栈 AI 虚拟主播商业直播中枢

> **一句话介绍**：一款本地私有化部署的 AI 虚拟主播系统——用一段 1 分钟真人视频即可生成专属数字人，配备「直播策略大脑 + 数字人渲染小脑」双核驱动，实现 7×24 小时全自动电商带货、弹幕互动、智能问答与短视频切片生产。

本地 AI 互动直播执行引擎与 Electron 控制台。当前交付提供本地程序化头像、音频与虚拟摄像头源、商品/订单/角色/知识库管理；**外部直播平台发布不由本程序管理，必须在直播工作台中另行配置并验收**。

永远记住，我们的项目使用者，只有两种人：
1. 硬件要求完全满足的用户（高性能显卡）
2. 硬件不足并且对接了远端租赁GPU的用户

所以，我们某些功能在需要显卡支持的时候，优先读取用户配置，看到底要用本地显卡还是用云端显卡，有判断，两条分支。
---

## 一、这是干什么的？

**AI-LiveStream-Agent 解决传统虚拟直播的四大痛点：**

| 痛点 | 本系统方案 |
|---|---|
| 形象死板、口型对不上 | 真人视频一键切片生成数字人 + 音频能量驱动唇形 + 云端 GPU 神经渲染可选 |
| 话术无转化、不会带货 | 工业级 SKU 货盘 + 促单逼单状态机 + 优惠券倒计时 + 价格防幻觉审计 |
| 对电脑硬件要求极高 | 四级算力自适应（旗舰独显 → 轻薄本纯 CPU），低配电脑可调度云端显卡节点代跑 |
| AI 自说自话、插不上话 | 全双工语音打断：现场开嗓 150ms 内让主播立刻闭嘴并回应 |

适用场景：**电商带货促单、娱乐才艺互动、专业领域咨询、情感闲聊陪伴、短视频口播批量生产**。

---

## 二、开发语言与技术栈

| 层 | 技术选型 |
|---|---|
| **后端** | Python 3.12+ · FastAPI + Uvicorn · 异步全栈（ASGI） |
| **数据** | SQLite (WAL 模式) · SQLAlchemy 2.0 Async · AES-256-GCM + Windows DPAPI 密钥加密 |
| **AI 引擎** | 8 大 LLM（DeepSeek/Qwen/Kimi/GLM/MiniMax/Claude/GPT-4o/Ollama 本地）· SenseVoice/Faster-Whisper 本地 ASR · Edge-TTS/CosyVoice/ElevenLabs/GPT-SoVITS 四引擎 TTS · Aho-Corasick 违禁词引擎 · BM25+稠密向量双路 RAG |
| **音视频** | FFmpeg 管道 · OpenCV · aiortc (WebRTC/WHEP) · pyvirtualcam (OBS 虚拟摄像头) · PyAV |
| **前端** | 原生模块化 JavaScript + HTML5 暗黑控制台 · Electron 桌面版（内置绿色版 Python，免环境安装） |
| **云端** | gpu_sidecar 云端显卡节点（严格 v3 WebSocket 协议 + Wav2Lip 插件后端 + OOM/许可门禁） |

---

## 三、核心功能模块（8 大板块）

1. **🎬 数字人资产工场**：上传 1~2 分钟真人说话视频 → 自动逐帧切片、人脸检测对齐、平滑抗抖动、伴音分轨、元数据归档，一键设为开播形象（异步任务队列，进度 0~100% 实时可视）
2. **🎭 动作状态机**：待机呼吸/挥手欢迎/求关注/指引购物车/鞠躬致谢五大动作 + 自定义扩展；话术关键词与场控事件双轨触发，优先级抢占，`mirror_index` 数学镜像算法消除切片循环撕裂
3. **🧠 直播决策大脑**：8 大 LLM 一键热切换，4 大主播人设（带货/娱乐/专家/闲聊），断网自动降级离线话术兜底，直播永不冷场
4. **🛒 电商带货中枢**：SKU 商品库（卖点/FAQ/尺码表/催单话术 JSON）、库存实时查询、优惠券倒计时画层、商品特写画层、下单播报营造抢购氛围
5. **📡 全平台弹幕监听**：抖音（真实 WSS + 零依赖 protobuf 解析）、B站（真实 WSS + zlib）、快手/视频号（长轮询）；断线指数退避自愈 + 熔断器；相似弹幕滑动窗口聚合集中答复
6. **🛡️ 合规风控护栏**：Aho-Corasick 毫秒级多模匹配（数万词库 <1ms），智能同义替换/整句熔断/静默警示三重处置，违规审计日志随时复盘，广告法极限词/医疗承诺/政治低俗多品类规则库
7. **🔊 声音资产管理**：10 秒人声克隆入库、专属 Voice-ID 绑定、0.5x~2.0x 语速微调、在线试听、多引擎聚合切换
8. **🎥 多路推流分发**：OBS 虚拟摄像头（抖音/快手直播伴侣直连）、RTMP 公网直推（H.264+AAC 自动重连）、WebRTC/WHEP 毫秒级预览、1080P 带货切片一键录制归档

---

## 四、怎么用？（按本地电脑配置二选一）

### 方式 1：本地电脑配置达标（有独显）

- **配置基线**：CPU 6核以上（i5/R5）· 内存 16GB~32GB · 显卡 RTX 3060 / 4060 / 2080Ti（显存 6GB~12GB）；头部品牌直播间建议 RTX 3090 / 4080 / 4090（显存 16GB~24GB+）
- **推荐运行模式**：全本地旗舰模式（Tier A）/ 端云混合模式（Tier B），开播向导会按硬件**自动推荐档位**
- **使用流程**：
  1. 按下方「启动」章节启动系统，自动呼起控制台 `http://127.0.0.1:18080/console`
  2. 在【开播向导】按推荐档位确认运行模式
  3. 在【系统设置】配置 LLM 与 TTS 的 API Key（或本地 Ollama 全离线运行）
  4. 在【主播管理】选择形象 / 上传真人视频一键生成数字人；在【商品管理】上架商品
  5. 通过控制台顶部【10 项开播真实体检】（模式/角色/LLM 真实 Ping/TTS 实测/驱动状态/虚拟摄像头/商品货盘/直播主题/违禁词库/显卡云端调度）→ 一键开播
- 本地独显承载数字人渲染；LLM 可走本地 Ollama 实现数据 100% 绝对私有

### 方式 2：本地电脑配置不达标（核显 / 低显存 / 老电脑）

- **配置基线**：普通笔记本/办公机（4~8核）· 内存 8GB~16GB · 显卡为核显 / MX 系列 / GTX 1050 / 显存 <= 2GB · 上行宽带 >= 10Mbps
- **方案 2a（云端显卡直推模式，推荐低配用户）**：
  1. 在【系统设置 -> 显卡与渲染设置】配置自建云端显卡算力节点（Sidecar / 远程 GPU，如 AutoDL / 腾讯云 / 阿里云，约 2-3 元/小时）
  2. 系统检测到云端节点后**自动优先调度**：本地仅跑 CPU 调度中枢，核心渲染运算全部外包云端 GPU，本地接收高清视频流再推流，本地轻量低发热
- **方案 2b（轻量 2D 程序化渲染模式）**：0 显存依赖，纯 CPU 音频能量驱动嘴型与微呼吸动作，所有 AI 算法走轻量 CPU 或云端 API，永不崩溃
- **系统自动保护**：本地显存不足且未配置云端显卡时，控制台、API 与日志会输出**明确警示与前往配置引导**，并自动平滑降级至 CPU 兼容模式，严禁静默崩溃或黑盒卡死

---

## 五、启动（两种方式）

### 方式 1：一键启动（推荐）

- **双击 `run_agent.bat`**：自动探测本机 Python 环境（.venv/venv/系统 PATH/py 启动器/常见安装路径）→ 执行商用全景体检（硬件/依赖/音画设备）→ 端口冲突自动回收 → 启动服务并**自动呼起浏览器控制台**；无 Python 时给出图文安装引导
- **Electron 桌面版**（可选内置绿色版 Python 3.12.10，用户免配环境，双击即用）：
  ```powershell
  npm.cmd ci --prefix apps/desktop-ui
  npm.cmd start --prefix apps/desktop-ui
  ```
  Electron 自动将数据目录设为其 `userData\data`，不会写入安装资源目录

### 方式 2：命令启动

- **智能启动器**（含商用体检、依赖缺失时从国内镜像自动安装、端口清理、就绪后自动呼起浏览器）：
  ```powershell
  $env:LIVE_AGENT_DATA_DIR = "G:\ai-live-agent-data"
  python launcher.py
  ```
  常用参数：`--no-browser`（不呼起浏览器）· `--check-only`（仅体检不启动）· `--auto-install`（静默自动安装缺失依赖）· `--restart` / `--force`（强制回收旧进程清理端口）
- **稳定生产运行**（跳过启动器，直接常驻服务）：
  ```powershell
  python -m uvicorn server.app:app --host 127.0.0.1 --port 18080
  ```

启动完成后访问：**控制台** `http://127.0.0.1:18080/console` · **接口文档** `http://127.0.0.1:18080/docs`。全部配置（API Key 等）经 AES-256-GCM + Windows DPAPI 加密落盘，本机零摩擦免密；如需对外暴露，可设置 `API_AUTH_TOKEN` 环境变量强制 Bearer 令牌鉴权。

---

## 六、六大吸引人的亮点

1. **💎 硬件普惠**：专为低配电脑深度优化——轻薄本纯 CPU 即可开播；显存不足且未配云端时**明确警示 + 自动平滑降级**，严禁静默崩溃或黑盒卡死（gpu_capability.py 四级算力调度引擎）
2. **⚡ 全双工真人级对答**：浏览器麦克风 16kHz PCM 直通 WebSocket，RMS 能量 VAD <100ms 响应，观众/主播开嗓瞬间触发 `flush_talk` 清空口型与音频队列，杜绝拖尾延迟
3. **🧩 工业级可靠性工程**：优先级打断队列（P0 打赏 > P5 逼单 > P2 欢迎）、弹幕代际隔离与粘滞令牌、TTS 运行期故障原子降级 Edge-TTS、超长音频看门狗、VRAM/CPU 内存自适应降频看门狗、熔断器、慢客户端背压淘汰
4. **🔒 本地私有化**：数据 100% 本地留存（SQLite WAL），密钥 DPAPI 硬件级保护，ASR 全离线处理，无云端依赖也能跑
5. **🖥️ 双形态交付**：Web 暗黑控制台 + Electron 桌面版（内置绿色版 Python 运行时，用户免配环境，双击即用）
6. **✅ 300 项自动化测试 100% 通过**：覆盖 API 路由、核心引擎、算力调度、端云通信、数字人四阶段演进专项用例，前端 JS 全量语法校验零错误

---

## 七、成熟技术细节

- **架构分层**：`server/core`（决策引擎）/ `server/adapters`（弹幕/TTS/媒体驱动适配器 + 注册中心工厂）/ `server/routes`（40+ RESTful 端点 + 3 组 WebSocket）/ `gpu_sidecar`（云端渲染节点）——驱动、Provider、弹幕平台全部注册中心 + 工厂模式，可插拔扩展
- **数字人双核驱动**：本地程序化渲染器（零权重、纯 CPU、25fps 常驻 shadow）+ 云端 Wav2Lip 插件后端（严格 activity/cancellation 契约、warmup 门禁、OOM 永久熔断）双轨并行，远端接管失败自动回退本地画面
- **音画同步**：20ms 流式音频块增量帧化事务（staged PCM frames）、代际 fence 原子提交、有界解码管线，杜绝内存膨胀与音画错位
- **诚实契约设计**：能力边界（如抖音未实现 a_bogus 签名、平台开播状态保持 null）全部在代码与文档中显式声明，绝不静默伪造数据
- **数据可视化**：弹幕瀑布流、进度环形图、优惠券倒计时、商品特写画层、实时指标 Prometheus 格式导出（/metrics）

---

## 运行要求

- 64 位 CPython 3.12 或 3.13
- Node.js 22（仅桌面开发与打包需要）
- 核心依赖：`python -m pip install -r requirements.txt`（或 `python -m pip install -r server/requirements.txt`）
- 测试依赖：`python -m pip install -r server/requirements-test.txt`
- 可选媒体能力：见 `server/requirements-optional.txt`

桌面端支持**可选内置便携版 Python (3.12.10)**（通过 `scripts/build_portable_python.py` 构建并置于 `apps/desktop-ui/resources/python/` 目录下）；未打包内置环境时，Electron 启动时会自动平滑回退并逐项核对系统 Python 版本、架构、核心依赖精确版本和 `server.app` 导入结果；预检失败时不会创建正常控制台窗口。

## 生产部署与文档导航

- 📘 [生产环境部署指南 (docs/deployment.md)](docs/deployment.md)：服务守护（NSSM / systemd）、环境变量配置清单、反向代理与探针监控
- 🎥 [OBS 与直播伴侣协同指南 (docs/obs_integration.md)](docs/obs_integration.md)：数字人浏览器源接入、音画延迟对齐（A/V Sync）与开播冒烟检查清单
- 🛡️ [运维、备份与回滚手册 (docs/operations.md)](docs/operations.md)：停服一致备份、数据恢复与版本升级标准作业程序

## 健康、监控与日志

- 存活：`GET /livez`
- 启动完成：`GET /startupz`
- 就绪（含 SQLite 查询）：`GET /readyz`
- 监控指标 (Prometheus)：`GET /metrics`
- 实时健康摘要看板：`GET /api/v1/system/health-summary`
- 实例版本/数据目录：`GET /api/v1/system/version`
- 轮转 JSON Lines 日志：`<LIVE_AGENT_DATA_DIR>\logs\server.log`

## 备份、恢复、升级

完整备份包含 SQLite 自包含快照、`.master.key` 和持久媒体资产，且明确要求停服：

```powershell
python scripts/backup_data.py --data-dir "G:\ai-live-agent-data" --output "G:\backups\before-upgrade"
python scripts/restore_data.py --backup "G:\backups\before-upgrade" --data-dir "G:\ai-live-agent-data" --confirm
```

恢复会校验 manifest、SHA-256 与 SQLite 完整性，并保留 `data.rollback-<时间>`。Windows DPAPI 保护的主密钥只承诺原机器、原 Windows 用户恢复。升级与回滚步骤见 [docs/operations.md](docs/operations.md)。

## 质量检查

推荐使用统一检查脚本，一键完成语法编译、代码风格、类型检查、前端解析与覆盖率验证：

```powershell
# Windows PowerShell 运行：
powershell .\scripts\check.ps1

# 或跨平台 Python 运行：
python scripts/check.py
```

也可手动分步执行：
```powershell
python -m compileall -q launcher.py scripts server
python -m ruff check launcher.py scripts server
python -m mypy server/core/queue/priority_queue.py server/core/cpu_worker.py
node --check apps/desktop-ui/main.js
node --check server/static/js/console.js
$env:LIVE_AGENT_DATA_DIR = Join-Path $env:TEMP ('ai-live-agent-test-' + [guid]::NewGuid().ToString('N'))
python -m pytest -q server/tests --cov=server --cov-branch
```

测试和本机验收不得调用收费 API 或真实直播平台；外部 LLM、TTS、OBS/直播工作台能力使用 mock、本地降级或单独外部验收。
