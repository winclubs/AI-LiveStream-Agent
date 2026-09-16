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

# 允许跨域请求 (为 Electron/WebUI 桌面端提供支持)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

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
    return FileResponse(index_file)

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

