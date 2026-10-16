# 设计：`server/routes/live.py` 巨型路由模块拆分

**日期**：2026-10-16
**状态**：设计已确认，待实施
**范围**：`server/routes/live.py`（4928 行）拆分重构 + 安全网修正

---

## 一、问题陈述

`server/routes/live.py` 当前 **4928 行**，承担了三类互不相干的责任：

| 区段 | 行范围 | 行数 | 内容 |
|---|---|---:|---|
| 模块级 helper | 63-225 | 163 | `_stream_output_ready_probe`、`_configure_media_watchdog`、`restore_obs_connection`、`_safe_media_status` 等 |
| 巨型控制器 | 228-2474 | 2247 | `LiveSessionController` |
| 端点区 | 2478-4926 | 2449 | 63 个 HTTP/WS 端点 + 预检检查器 + 请求模型 |

具体超标项：

- `LiveSessionController` 2247 行（含 4 个超长方法：`_speak_sentence` 325、`_consume_queue_loop` 179、`_select_tts_driver` 162、`_speak_pcm_streaming_sentence` 153、`_start_resources` 139）；
- `_legacy_preflight_check` 335 行、`_start_live_unlocked` 282 行。

同时，上一阶段新建的安全网 `server/tests/test_live_route_contract.py` 自身**不可信**，存在三处缺陷（详见第三节）。

---

## 二、目标与非目标

### 2.1 目标

1. `server/routes/live.py` 降至 ≤900 行，退化为薄路由 façade；
2. 无单函数 >200 行、无单类 >400 行，**且该约束覆盖拆分后的全部新模块**（防止指标只是「被搬走」）；
3. 对外 HTTP/WS 契约**逐条不变**（63 条 `(方法, 路径)` 零差异）；
4. 测试对 `server.routes.live.*` 的 patch / 直接调用**行为不变**，且可验证其真实生效；
5. `server/app.py` 的路由挂载代码零改动。

### 2.2 非目标（本次明确不夹带）

- 不新增、不删除、不重命名任何对外端点；
- 不修改 `LiveSessionController` 的业务语义与状态机流转；
- 不做控制器依赖的构造注入改造（见 §4.4 有意分层倒置）；
- 不改动前端、数据库、迁移、模型层；
- 不整理工作区其它未提交改动。

---

## 三、安全网现状缺陷（实测结论）

| # | 缺陷 | 实测证据 | 处置 |
|---|---|---|---|
| D1 | 路由枚举失效。FastAPI 0.141 不再把 `include_router` 的路由摊平进 `app.routes`，而是保留为 `_IncludedRouter` 包装对象 | `_router_prefixed_paths()` 返回 **0** 条 | 递归下钻，兼容新旧两种形态 |
| D2 | `EXPECTED_ROUTES` 是**虚构契约**：61 条中约 20 条端点不存在（`/live/orders/webhook`、`/live/avatar/status`、`/live/gpu/capability`、`/live/orders/cancel`、`/live/killswitch`、`/live/privacy_erase` 等），同时漏掉约 25 条真实端点（`/live/preflight`、`/live/asr/*`、`/live/webrtc/*`、`/live/quality/*`、`/live/governance/*`、`/live/cost/*`、`/live/privacy/erase-user`、`/live/resume-ai`（连字符）等） | 即使修好 D1，该测试仍必挂 | 以**实测 63 条**为唯一契约 |
| D3 | 结构预算只覆盖 `live.py`，拆分后指标会「被搬走」而非真正收敛 | 当前 `live.py` 4928 行 / 控制器 2247 行 | 预算改为**模块清单驱动** |

**契约表采集纪律**：重写后的 `EXPECTED_ROUTES` 必须由 `_router_prefixed_paths()` 实测导出，并在文件头记录采集方式与日期；禁止凭想象补写端点。这是本次事故的直接教训。

---

## 四、架构设计

### 4.1 分层原则与模块布局

`server/routes/live.py` 退化为薄 façade（仅持有全局单例与「缝」），端点按业务域下沉到平铺子模块，会话控制器移到 `server/core/live/`。

命名沿用仓库既有平铺约定（`ws_live.py`、`orders_webhook.py`），**不新建 `live/` 包**，以避免与 `live.py` 模块同名冲突。

**端点归属实测基线**（2026-10-16 实测，非估算）：`/api/v1/live/*` 共 **61** 条由 `live.py` 拥有，逐条落到下列 12 个子模块；另有 `/ws/live_control`、`/ws/danmaku-ingest` **2 条属于 `ws_live.py`，不在本次拆分范围**。契约总数 63 = 61 + 2。

| 子模块 | 端点数 | 端点 |
|---|---:|---|
| `live_lifecycle.py` | 9 | `/start` `/stop` `/status` `/interrupt` `/manual-speech` `/takeover` `/pause-ai` `/resume-ai` `/voice-interrupt` |
| `live_obs.py` | 9 | `/obs/*`(5) `/stream/preview` `/virtual-cam/*`(3) |
| `live_stream.py` | 8 | `/rtmp/*`(3) `/llm/budget`(2) `/webrtc/*`(3) |
| `live_governance.py` | 8 | `/quality/*`(2) `/governance/kill-switch*`(3) `/cost/*`(2) `/privacy/erase-user` |
| `live_orders.py` | 6 | `/stats/order` `/orders` `/orders/{order_id}/cancel` `/orders/{order_id}/refund` `/sessions` `/stats` |
| `live_recording.py` | 5 | `/record/*` |
| `live_danmaku.py` | 4 | `/danmaku-webhook` `/danmaku/block` `/danmaku/probe` `/mock-event` |
| `live_asr.py` | 4 | `/asr/ws` `/asr/transcribe` `/asr/install-dependency` `/asr/install-status` |
| `live_avatar_ctl.py` | 3 | `/is-speaking` `/avatar/flush-talk` `/avatar/action` |
| `live_overlay.py` | 3 | `/overlay/coupon` `/overlay/urgency` `/media/status` |
| `live_hardware.py` | 1 | `/hardware` |
| `live_preflight.py` | 1 | `/preflight` |

> 注意：真实契约中**不存在** `/live/gpu/capability`、`/live/gpu/tier_recommend`、`/live/hardware/probe_gpu`、`/live/orders/webhook`、`/live/orders/cancel`、`/live/killswitch` 等端点（旧安全网虚构）。`/live/avatar/action` 存在，但 `/live/avatar/status`、`/live/avatar/actions`、`/live/avatar/preview` 不存在。

```
server/routes/live.py                  ≤900 行  façade：router 聚合 + 19 个 REQUIRED_EXPORTS 兼容导出
server/routes/live_lifecycle.py        start / stop / status / preflight / interrupt /
                                       takeover / pause-ai / resume-ai / voice-interrupt
server/routes/live_obs.py              obs/*、virtual-cam/*、stream/preview、restore_obs_connection
server/routes/live_danmaku.py          danmaku-webhook、danmaku/{block,probe}、mock-event
server/routes/live_hardware.py         hardware/*、_probe_gpu、_recommend_tier
server/routes/live_orders.py           stats/order、orders/*、sessions、stats、_refresh_order_stats
server/routes/live_preflight.py        _pf* 检查器 + _legacy_preflight_check + GET /preflight
server/routes/live_overlay.py          overlay/{coupon,urgency}、GET media/status
server/routes/live_stream.py           rtmp/*、llm/budget、webrtc/*
server/routes/live_avatar_ctl.py       is-speaking、flush-talk、avatar/action
server/routes/live_asr.py              asr/{ws,transcribe,install-dependency,install-status}
server/routes/live_recording.py        record/*
server/routes/live_governance.py       quality/*、governance/kill-switch*、cost/*、privacy/erase-user

server/core/live/controller/
  base.py            LiveSessionBase：__init__ / lifecycle_lock / _track_task / set_state_detail
  lifecycle_mixin.py LifecycleMixin：start / stop / _start_resources / reload_runtime_config /
                     ingest_event / _configure_neural_sidecar
  takeover_mixin.py  TakeoverMixin：takeover / release_takeover / resume_ai /
                     transition_to_ai_paused / _on_media_degraded / obs_owner_* / lifecycle_lock
  speech_mixin.py    SpeechMixin：_select_tts_driver / _speak_* / _prefetch_* /
                     _sanitize_and_humanize / _collect_tts_sentence / _switch_to_edge_tts
  queue_mixin.py     QueueMixin：_consume_queue_loop / _on_barge_in / _on_danmaku_event /
                     _refresh_product_context
  stitcher_mixin.py  StitcherMixin：切片装载 / 帧循环 / 过渡词 / 音频回放
  runtime_mixin.py   RuntimeMixin：_vision_loop / _stats_* / _viewer_count_loop /
                     _safe_close / _log_guardrail_hits / _log_barrage_complete
  controller.py      LiveSessionController：组合以上 7 个（自身 <100 行）
```

### 4.2 为什么用 mixin 而不是拆成独立服务

`LiveSessionController` 2247 行的本质是「一个会话对象承担过多职责」。mixin 按职责切分能把每个文件压进 400 行预算，同时**不改变对象语义**：仍是同一个 `global_live_controller` 单例，`isinstance` 关系不变，测试的 `monkeypatch.setattr(LiveSessionController, ...)` 全部照常生效。拆成独立服务则需要改写全部调用点，风险与工作量都不成比例。

### 4.3 对外零变更的实现方式

`server/app.py` 的 `app.include_router(live_router, prefix="/api/v1")` **一行不改**。

`live.py` 的 `router` 保持 `APIRouter(prefix="/live", tags=["直播控场与调度"])`，内部改为聚合体：每个子模块自带一个**无前缀** `APIRouter`，其端点路径写成 `/live/xxx`（例如 `@router.post("/live/orders/cancel")`），再由 `live.py` 依次 `router.include_router(子路由)`。拼接后对外 URL 为 `/api/v1` + `/live/orders/cancel`，与现状逐字节一致，由安全网的 63 条契约校验。

> 选择「子路由自带 `/live`」而非「父级持 `/live`、子级持相对段」的原因：父级已持 `/live` 时子路由再 `include_router` 需要 `prefix=""` 覆写，跨 FastAPI 版本的拼接行为更难验证；让每条路径在子模块里就是完整 `/live/...`，契约表与代码可直接逐行对照。

---

## 五、缝机制与 patch 契约（核心设计）

### 5.1 「缝」的定义

必须留在 `server/routes/live.py` 命名空间、且必须在**调用时**解析的符号，共 19 个（与安全网 `REQUIRED_EXPORTS` 一一对应）：

| 类别 | 符号 | 数量 |
|---|---|---:|
| 全局单例 | `global_live_controller` `global_obs_client` `global_role_manager` `global_virtual_audio` `global_media_driver` | 5 |
| 缝函数 | `restore_obs_connection` `_hardware_payload` `_probe_gpu` `_refresh_order_stats` `_start_live_unlocked` `_stop_live_unlocked` `_configure_media_watchdog` `_stream_output_ready_probe` `_build_external_publish_status` `_recommend_tier` | 10 |
| 按引用再导出 | `router`、`LiveSessionController`（类）、`LiveStartRequest`（模型）、`get_live_status` | 4 |
| **合计** | | **19** |

「按引用再导出」的 4 个只需保证 `from server.routes.live import X` 不报错（测试直接调用/构造），无需调用时解析。

### 5.2 双向透传规则（本设计的唯一硬规则）

> **任何实现模块调用「缝」时，一律写在函数体内做 `from server.routes.live import X`，禁止模块顶层 import。**

```python
# server/routes/live.py —— 缝本体
def _probe_gpu() -> dict:
    from server.routes.live_hardware import probe_gpu   # 函数体内 → 调用时解析
    return probe_gpu()

# server/routes/live_hardware.py —— 实现侧，反向亦然
def _gpu_section() -> dict:
    from server.routes.live import _probe_gpu            # 调用时取 live 上的当前值
    return _probe_gpu()
```

全局单例同理：`live_obs.get_obs_status()` 函数体内 `from server.routes.live import global_obs_client`。

### 5.3 为什么必须双向，而不只是「符号还在」

单向 `from x import y` 搬走会让 `patch("server.routes.live.X")` **静默失效**：测试照样绿，线上行为却变了。这比测试直接报错更危险。

现有 `server/tests/test_core_engine.py:474` 已直接证明「取值时机」才是契约本体——它做 `_live_mod._probe_gpu = slow_probe`，随后调用 `_live_mod._hardware_payload()` 并断言 `slow_probe` 被打到。

### 5.4 有意保留的一处分层倒置（记账项）

`server/core/live/controller/*.py`（core 层）需读取 `global_*` 缝，构成 core→routes 的反向依赖。处理方式：

1. 全部写成**函数体内延迟 import**，不产生 import 环；
2. 每处加显式注释标注为「测试缝（late-binding seam）」；
3. 在本文件 §5.4 记账，不做隐性处理。

正确解法（构造注入依赖）列为独立后续任务，本次不夹带。

---

## 六、安全网修正方案

### 6.1 `server/tests/test_live_route_contract.py`

**修 1 — 路由枚举**：改为递归下钻，兼容新旧两种 FastAPI 形态：

```python
def _iter_routes(routes, prefix=""):
    for r in routes:
        if type(r).__name__ == "_IncludedRouter":          # FastAPI ≥0.1xx 懒包含
            ctx = getattr(r, "include_context", None)
            yield from _iter_routes(
                r.original_router.routes, prefix + (getattr(ctx, "prefix", "") or "")
            )
        else:
            yield r
```

**修 2 — 契约表重写**：以实测 63 条 `(方法, 路径)` 为准（61 条属 `live.py`，2 条属 `ws_live.py`），按域分组并加注释；文件头记录采集方式与采集日期。契约中应显式标注 `ws_live.py` 拥有的 2 条，使「拆分未触碰 WS 路由」成为可验证事实。

**修 3 — 结构预算模块清单驱动**：

| 模块组 | 行数预算 | 单函数预算 | 单类预算 |
|---|---:|---:|---:|
| `server/routes/live.py` | 900 | 200 | 400 |
| `server/routes/live_*.py` | 600 | 200 | 400 |
| `server/core/live/**/*.py` | 700 | 200 | 400 |

**修 4 — 阶段语义写进文件头**：路由契约 / 导出符号 / patch 目标三条**必须在拆分前全绿**（characterization 前提）；结构预算三条**在拆分后才转绿**，它们是目标态而非回归线。

### 6.2 新增 `server/tests/test_live_seam_late_binding.py`

断言「透传真实生效」，而非「符号存在」。对每个缝：改写 `server.routes.live` 上的属性 → 调用真实端点/函数链路 → 断言桩被调用。

覆盖：`_probe_gpu → _hardware_payload`、`global_obs_client`、`global_media_driver`、`global_role_manager`、`global_virtual_audio`、`_start_live_unlocked`、`_stop_live_unlocked`、`_configure_media_watchdog` / `_stream_output_ready_probe`。

---

## 七、落地顺序

每阶段结束必须绿，不允许「最后一起修」。

| 阶段 | 内容 | 结束时 `live.py` 行数 | 该阶段红线 |
|---|---|---:|---|
| **S0** | 只修安全网（枚举 + 契约表 + 预算扩展 + 新增透传测试），不动生产代码 | 4928 | 契约 3 条绿；结构 3 条红（预期） |
| **S1** | 端点区 2478-4926 按域下沉 12 个子模块 | ~2450 | 契约 + 透传测试 + 全量回归绿 |
| **S2** | `LiveSessionController` 拆 7 个 mixin → `core/live/controller/` | ~200 | 各 mixin ≤400 行 |
| **S3** | 3 个超长函数内聚拆分：`_speak_sentence`(325)、`_legacy_preflight_check`(335)、`_start_live_unlocked`(282) | ~200 | 无函数 >200 行 |
| **S4** | 全量回归 + `check.py` | ~200 | 全部绿 |

S1 排在最前是因为它风险最低（不触碰控制器内部状态机）；S2/S3 才是真正的重构。

---

## 八、验证口径

```powershell
# 契约与透传（每阶段必跑）
python -m pytest server/tests/test_live_route_contract.py server/tests/test_live_seam_late_binding.py -q

# 全量回归（每阶段必跑，基线 737 passed / 28 skipped / 0 failed）
python -m pytest server/tests -q

# 收官（compileall + ruff + 前端语法）
python scripts/check.py
```

**通过判据**（三条同时成立）：

1. `test_live_route_contract.py` 全绿，且 `EXPECTED_ROUTES` 与实现零差异（`missing` 与 `added` 均为空）；
2. 全量回归 **0 failed**，通过数 ≥737（新增测试只会多不会少），跳过数 28 不得靠改标记消化；
3. `scripts/check.py` 退出码 0。

---

## 九、回滚

每阶段独立提交。任一阶段回归红 → `git checkout` 该阶段触碰的文件即回到上一绿点。

> 注：本仓库工作区当前存在大量与本次任务无关的未提交改动。实施过程中**不执行 `git commit`**（除非用户明确要求）；阶段完成时仅报告可提交范围。

---

## 十、已知遗留

- §5.4 的 core→routes 分层倒置（记账，未解决）；
- 控制器依赖未做构造注入（独立后续任务）；
- `docs/LOW_SPEC_BROADCAST_SOP.md` 等旧文档中若存在 live.py 行数/模块引用，需在 S4 后统一校正（本次不做）。