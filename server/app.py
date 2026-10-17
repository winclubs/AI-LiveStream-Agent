import json
import logging
import os
import asyncio
import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# 终端编码兜底：避免在 GBK 控制台因 emoji 日志触发 UnicodeEncodeError 导致启动崩溃
import sys as _sys
if hasattr(_sys.stdout, "reconfigure"):
    try:
        _sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
if hasattr(_sys.stderr, "reconfigure"):
    try:
        _sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from server.config import SERVER_HOST, SERVER_PORT, APP_VERSION, DATA_DIR
from server.observability.logging_config import configure_logging, flush_logging
from server.core.security.auth_policy import (
    evaluate_access,
    is_commercial_mode,
    is_local_client,
    resolve_auth_token,
)

configure_logging()
logger = logging.getLogger("LiveAgent.App")

# 服务主事件循环引用：供同步调用栈 (路由工作线程等) 安全调度异步热重载
_service_loop = None

from server.database.db import init_db, AsyncSessionLocal, engine
from server.routes.guardrails import reload_guardrails
from server.routes.roles import router as roles_router
from server.routes.guardrails import router as guardrails_router
from server.routes.settings import router as settings_router
from server.routes.products import router as products_router
from server.routes.avatars import router as avatars_router
from server.routes.voices import router as voices_router
from server.routes.live import router as live_router
from server.routes.ws_live import router as ws_router
from server.routes.knowledge import router as knowledge_router
from server.routes.system import router as system_router
from server.routes.anchors import router as anchors_router
from server.routes.oss import router as oss_router
from server.routes.orders_webhook import router as orders_webhook_router
from server.routes.health import router as health_router
from server.routes.metrics import router as metrics_router

@asynccontextmanager
async def lifespan(app: FastAPI):
    """初始化依赖，并让启动失败与正常关闭共享同一条容错清理路径。"""
    from server.health import health_state

    service_lock = DATA_DIR / ".service.lock"
    startup_succeeded = False
    health_state.starting()
    global _service_loop
    _service_loop = asyncio.get_running_loop()
    try:
        try:
            service_lock.write_text(
                json.dumps({"pid": os.getpid(), "started_at": health_state.started_at}),
                encoding="utf-8",
            )
            logger.info("正在初始化本地持久化数据库 (SQLite WAL)")
            await init_db()

            # 录制产物保留策略：7x24 商用节点若无自动回收必然磁盘耗尽
            # （上线诊断 BLOCKER-2）。刻意放在 init_db 之后、OBS 恢复之前，
            # 且完全隔离失败：清理异常绝不能阻断服务启动。
            try:
                from server.core.media.retention import RetentionPolicy, run_startup_prune

                policy = RetentionPolicy.from_env()
                if policy.is_enabled:
                    prune_stats = await asyncio.to_thread(run_startup_prune)
                    logger.info(
                        "录制产物保留策略已执行: 回收 %d 个切片，释放 %.1fMB，剩余 %d 个 / %.1fMB",
                        prune_stats.total_pruned,
                        prune_stats.freed_bytes / 1024 / 1024,
                        prune_stats.remaining_count,
                        prune_stats.remaining_bytes / 1024 / 1024,
                    )
                else:
                    logger.info("录制产物保留策略已由运维关闭 (LIVE_AGENT_RECORDING_RETENTION_DAYS=0)")
            except Exception:
                logger.warning("录制产物保留策略执行失败，已跳过（不影响服务启动）", exc_info=True)

            try:
                from server.routes.live import restore_obs_connection
                async with AsyncSessionLocal() as session:
                    await restore_obs_connection(session)
            except Exception:
                logger.warning("启动时恢复 OBS 配置/连接失败，服务将继续启动", exc_info=True)

            async with AsyncSessionLocal() as session:
                await reload_guardrails(session)
            logger.info("违禁词 Aho-Corasick 内存匹配引擎加载就绪")

            from server.core.rag.engine import global_rag
            await global_rag.load_from_db()
            logger.info("本地 RAG 双路索引加载就绪")

            # MOSS-TTS-Nano 运行时后台预热：消除首次克隆合成的 ~7s ORT 冷启动，不阻塞服务就绪
            async def _moss_warmup():
                try:
                    import time as _t
                    _start = _t.perf_counter()
                    from server.core.audio.moss_nano.moss_cloner import moss_cloner
                    runtime = moss_cloner._get_runtime()
                    await asyncio.to_thread(runtime.warmup)
                    logger.info(
                        "MOSS-TTS-Nano 运行时后台预热完成 (%.2fs)，provider=%s",
                        _t.perf_counter() - _start,
                        getattr(runtime, "execution_provider", "unknown"),
                    )
                except Exception as warmup_err:
                    logger.warning(f"MOSS-TTS-Nano 运行时预热失败 (首次合成时将自动重试): {warmup_err}")

            asyncio.create_task(_moss_warmup())

            # 可选依赖后台自愈：缺失时自动下载神经唇形权重 / 安装 ASR 转写引擎。
            # 全程后台执行，不阻塞服务就绪；失败仅记录日志，直播主流程不受影响。
            async def _optional_dependencies_auto_heal():
                try:
                    from server.config import auto_download_enabled
                    if not auto_download_enabled():
                        logger.info("自动下载总开关已关闭 (LIVE_AGENT_DISABLE_AUTO_DOWNLOAD=1)，跳过可选依赖自愈")
                        return
                    from server.core.avatar.lipsync_weight_downloader import get_lipsync_weight_downloader
                    get_lipsync_weight_downloader().start_download(source="modelscope")
                    from server.core.audio.asr_dependency_installer import maybe_start_auto_install
                    maybe_start_auto_install()
                except Exception:
                    logger.warning("可选依赖后台自愈触发失败 (直播主流程不受影响)", exc_info=True)

            asyncio.create_task(_optional_dependencies_auto_heal())

            # 周期性录制产物回收：仅启动时清理不足以覆盖 7x24 长跑节点
            # （一场直播就可能新增数百 MB），必须周期性兜底。
            asyncio.create_task(_recording_retention_loop())

            health_state.ready()
            startup_succeeded = True
            logger.info("本地服务启动完成: http://%s:%s", SERVER_HOST, SERVER_PORT)
            yield
        except BaseException as exc:
            if not startup_succeeded:
                health_state.failed(exc)
                logger.exception("服务初始化失败")
            raise
    finally:
        if startup_succeeded:
            health_state.stopping()
        _service_loop = None

        from server.routes.live import global_live_controller
        from server.core.media.virtual_audio import global_virtual_audio

        try:
            await global_live_controller.stop()
        except BaseException:
            logger.exception("关闭直播控制器失败")
        try:
            global_virtual_audio.shutdown()
        except BaseException:
            logger.exception("关闭虚拟音频失败")
        try:
            from server.core.media.virtual_cam import global_virtual_cam
            global_virtual_cam.stop()
        except BaseException:
            logger.exception("关闭虚拟摄像头失败")
        try:
            from server.core.vision.capture import global_vision
            global_vision.close()
        except BaseException:
            logger.exception("关闭视觉采集失败")
        try:
            from server.adapters.obs.obs_client import global_obs_client
            await global_obs_client.disconnect()
        except BaseException:
            logger.exception("关闭 OBS 客户端连接失败")
        try:
            await engine.dispose()
        except BaseException:
            logger.exception("释放数据库引擎失败")
        try:
            lock_data = json.loads(service_lock.read_text(encoding="utf-8")) if service_lock.exists() else {}
            if lock_data.get("pid") == os.getpid():
                service_lock.unlink(missing_ok=True)
        except (OSError, ValueError, json.JSONDecodeError):
            logger.exception("清理服务锁失败")
        try:
            logger.info("服务资源清理完成")
            flush_logging()
        except BaseException:
            logger.exception("刷新服务日志失败")

app = FastAPI(
    title="AI-LiveStream-Agent 核心执行引擎",
    description="本地私有化 AI 互动直播/专家带货系统后端服务，支持全双工流式互动、音画同步与打断调度",
    version=APP_VERSION,
    lifespan=lifespan
)

# 允许跨域请求。
# 默认白名单而非 "*"：全开放跨域会让任意网页都能对本机控制台发起带凭据的
# 控制类请求（开播/改配置/读密钥），配合本地免密即为可被网页触发的控制面。
# 桌面端 Electron 以 file:// 或 app:// 加载，同源请求不受 CORS 约束。
_CORS_LOCALHOST_DEFAULT = ",".join([
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    f"http://127.0.0.1:{SERVER_PORT}",
    f"http://localhost:{SERVER_PORT}",
])
cors_env = os.getenv("CORS_ALLOW_ORIGINS", "").strip()
if not cors_env:
    cors_origins = [o.strip() for o in _CORS_LOCALHOST_DEFAULT.split(",") if o.strip()]
elif cors_env == "*":
    # 显式配置 * 时保留放行能力，但记为安全风险而非静默默认。
    cors_origins = ["*"]
    logger.warning(
        "🚨 [安全风险提示] CORS_ALLOW_ORIGINS 被显式配置为 '*'，任意站点均可对本机控制台发起跨域请求，请确认这是有意为之。"
    )
else:
    cors_origins = [o.strip() for o in cors_env.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


async def _recording_retention_loop() -> None:
    """周期性执行录制产物保留策略。

    仅在启动时清理不足以覆盖 7x24 长跑节点：单场直播就可能新增数百 MB。
    间隔由 LIVE_AGENT_RECORDING_SWEEP_INTERVAL_MIN 控制（默认 60 分钟）。

    退出条件使用健康状态而非取消信号：服务关闭时健康状态先转为 stopping，
    循环随即退出，避免关停阶段的取消异常噪音。
    """
    from server.health import health_state

    try:
        interval_min = max(1, int(float(os.getenv("LIVE_AGENT_RECORDING_SWEEP_INTERVAL_MIN", "60"))))
    except (TypeError, ValueError):
        interval_min = 60
    interval_sec = interval_min * 60

    while True:
        try:
            await asyncio.sleep(interval_sec)
            if health_state.phase in ("stopping", "failed"):
                return
            from server.core.media.retention import RetentionPolicy, run_startup_prune

            if not RetentionPolicy.from_env().is_enabled:
                continue
            stats = await asyncio.to_thread(run_startup_prune)
            if stats.total_pruned:
                logger.info(
                    "[录制保留策略] 周期回收 %d 个切片，释放 %.1fMB，剩余 %d 个 / %.1fMB",
                    stats.total_pruned,
                    stats.freed_bytes / 1024 / 1024,
                    stats.remaining_count,
                    stats.remaining_bytes / 1024 / 1024,
                )
        except asyncio.CancelledError:
            return
        except Exception:
            logger.warning("[录制保留策略] 周期清理异常，已跳过本轮", exc_info=True)


@app.middleware("http")
async def security_auth_middleware(request, call_next):
    """
    自适应安全鉴权中间件 (解决 7.4-13 服务非本地暴露裸奔风险)

    判定逻辑收敛至 `server.core.security.auth_policy.evaluate_access`，与
    WebSocket 守卫共用同一函数 —— 历史上二者各自实现且 HTTP 侧缺少商业档位
    判断，导致「WebSocket 已锁死、REST API 全开」的安全倒挂 (BLOCKER-1)。

    - 静态资源与健康探针 (/static, /health, /livez 等) 恒定放行；
    - 本地单机回环仅在非商业档位零摩擦免密；
    - PILOT/PRODUCTION 档位下，本地回环与外部网络一律要求有效令牌；
    - 配置了 API_AUTH_TOKEN 时，对所有非豁免路径强制校验 Bearer / X-API-Key。
    """
    client_host = request.client.host if request.client else ""

    path = request.url.path
    # 静态资源与健康探针恒定豁免：商业档位也不阻断，否则守护进程/K8s 会误杀实例。
    if path.startswith("/static") or path in (
        "/health", "/livez", "/readyz", "/startupz", "/favicon.ico", "/console"
    ):
        return await call_next(request)

    auth_token = resolve_auth_token()
    allowed, reason = evaluate_access(client_host, request.headers, request.cookies)

    if not allowed:
        if reason == "invalid_token":
            return JSONResponse(
                status_code=401,
                content={"code": 401, "message": "未授权访问：请在请求头提供有效的 Bearer 令牌或 X-API-Key"}
            )
        return JSONResponse(
            status_code=403,
            content={
                "code": 403,
                "message": (
                    "已拦截访问：服务端未配置 API_AUTH_TOKEN 鉴权令牌。"
                    "商业档位 (PILOT/PRODUCTION) 强制要求令牌；"
                    "请在部署环境配置 API_AUTH_TOKEN 以确保安全。"
                )
            }
        )

    if reason == "" and not auth_token and not is_local_client(client_host):
        logger.warning(
            "🚨 [安全风险提示] 收到非本地回环外部网络请求 (%s -> %s)，但服务端未设置 API_AUTH_TOKEN 令牌！建议配置令牌防公网未授权访问。",
            client_host,
            path,
        )

    response = await call_next(request)
    if not auth_token and not is_local_client(client_host) and not is_commercial_mode():
        response.headers["X-Security-Warning"] = "Unprotected-Remote-Access"
    return response

from pathlib import Path
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from fastapi.exceptions import RequestValidationError

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request, exc: RequestValidationError):
    errors = exc.errors()
    msg_parts = []
    for err in errors:
        loc = ".".join(str(p) for p in err.get("loc", ()) if p != "body")
        msg = err.get("msg", "参数格式错误")
        msg_parts.append(f"{loc}: {msg}" if loc else msg)
    clean_msg = "；".join(msg_parts) if msg_parts else "请求参数验证失败"
    return JSONResponse(
        status_code=422,
        content={
            "code": 422,
            "message": f"参数校验失败: {clean_msg}",
            "detail": clean_msg
        }
    )

# 挂载业务路由
app.include_router(roles_router, prefix="/api/v1")
app.include_router(guardrails_router, prefix="/api/v1")
app.include_router(settings_router, prefix="/api/v1")
app.include_router(products_router, prefix="/api/v1")
app.include_router(avatars_router, prefix="/api/v1")
app.include_router(voices_router, prefix="/api/v1")
app.include_router(live_router, prefix="/api/v1")
app.include_router(knowledge_router, prefix="/api/v1")
app.include_router(system_router, prefix="/api/v1")
app.include_router(anchors_router, prefix="/api/v1")
app.include_router(oss_router, prefix="/api/v1")
app.include_router(orders_webhook_router, prefix="/api/v1")
app.include_router(ws_router)
app.include_router(health_router)
app.include_router(metrics_router)

# 挂载静态前端资源与中控台路由
static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

@app.get("/console")
async def serve_console():
    """打开现代科技感直播中控控制台"""
    template_file = static_dir / "index.template.html"
    index_file = static_dir / "index.html"
    if template_file.exists():
        try:
            from server.static.builder import build_index_html
            build_index_html()
        except Exception:
            logger.exception("控制台 index.html 模板合成失败，继续返回现有静态产物")
    return FileResponse(
        index_file,
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0"
        }
    )

@app.get("/avatar-viewport")
async def serve_avatar_viewport():
    """专供抖音直播伴侣/快手直播伴侣/微信视频号窗口捕获的高清绿幕视窗"""
    viewport_file = static_dir / "avatar_viewport.html"
    if not viewport_file.exists():
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="视窗模板文件不存在")
    return FileResponse(viewport_file)

@app.get("/static-file")
async def serve_data_file(path: str):
    """安全访问 data/ 目录下的上传文件 (主播照片/商品图片/音色样本)"""
    from fastapi import HTTPException
    from server.config import DATA_DIR
    target = (DATA_DIR / path).resolve()
    # 路径穿越防护：必须严格位于 DATA_DIR 内 (is_relative_to 防止兄弟目录前缀绕过)
    if not target.is_relative_to(DATA_DIR.resolve()):
        raise HTTPException(status_code=403, detail="非法路径")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(target)

@app.get("/")
async def root():
    from server.config import APP_VERSION
    return {
        "system": "AI-LiveStream-Agent Local Core Engine",
        "status": "running",
        "version": APP_VERSION,
        "console_ui": f"http://{SERVER_HOST}:{SERVER_PORT}/console",
        "docs_url": f"http://{SERVER_HOST}:{SERVER_PORT}/docs"
    }

if __name__ == "__main__":
    uvicorn.run("server.app:app", host=SERVER_HOST, port=SERVER_PORT, reload=False)

