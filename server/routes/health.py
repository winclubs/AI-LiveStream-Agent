"""进程存活、启动完成与流量就绪探针。"""
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from server.health import health_state, readiness_details

router = APIRouter(tags=["健康探针"])


def _response(ok: bool, probe: str, details: dict | None = None) -> JSONResponse:
    payload = {
        "status": "ok" if ok else "unavailable",
        "probe": probe,
        "phase": health_state.phase,
        "started_at": health_state.started_at,
        "ready_at": health_state.ready_at,
    }
    if health_state.last_error:
        payload["last_error"] = health_state.last_error
    if details is not None:
        payload["details"] = details
    return JSONResponse(payload, status_code=200 if ok else 503)


@router.get("/livez")
async def liveness():
    return _response(health_state.phase != "stopping", "liveness")


@router.get("/startupz")
async def startup():
    return _response(health_state.phase == "ready", "startup")


@router.get("/readyz")
async def readiness():
    ok, details = await readiness_details()
    return _response(ok, "readiness", details)
