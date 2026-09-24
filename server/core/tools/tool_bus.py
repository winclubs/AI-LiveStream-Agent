"""
MCP 异步工具调用总线 (规划 §2.3 / §5.2)
口播的同时并行触发库存查询、优惠券下发等工具，结果回注 LLM Prompt
"""
import logging
from typing import Any, Dict, Callable, Awaitable, Optional

from server.core.media.scene_overlay import global_scene_overlay_state

logger = logging.getLogger("LiveAgent.MCPTools")

ToolFunc = Callable[..., Awaitable[Any]]


class MCPToolBus:
    """轻量异步工具注册与调用中心 (兼容 MCP 语义)"""

    def __init__(self):
        self._tools: Dict[str, ToolFunc] = {}

    def register(self, name: str, func: ToolFunc):
        self._tools[name] = func

    def list_tools(self) -> list:
        return list(self._tools.keys())

    async def call(self, name: str, **kwargs) -> Any:
        func = self._tools.get(name)
        if not func:
            logger.warning(f"未注册的 MCP 工具调用: {name}")
            return {"error": f"tool_not_found: {name}"}
        try:
            return await func(**kwargs)
        except Exception as e:
            logger.error(f"MCP 工具 {name} 执行异常: {e}", exc_info=True)
            return {"error": str(e)}


# ---------------------------------------------------------------------------
# 内置工具实现
# ---------------------------------------------------------------------------
async def query_stock(title: str = "", sku: str = "") -> Dict[str, Any]:
    """查询商品实时库存"""
    from sqlalchemy import select
    from server.database.db import AsyncSessionLocal
    from server.database.models import Product

    async with AsyncSessionLocal() as session:
        query = select(Product).where(Product.is_active == 1)
        if sku:
            query = query.where(Product.sku_code == sku)
        elif title:
            query = query.where(Product.title.contains(title))
        res = await session.execute(query.limit(1))
        product = res.scalar_one_or_none()

    if not product:
        return {"found": False, "title": title or sku, "stock": 0}
    return {
        "found": True,
        "title": product.title,
        "sku": product.sku_code,
        "stock": product.current_stock,
        "live_price": product.live_price,
    }


async def trigger_onscreen_coupon(
    desc: str = "限时专享优惠", seconds: int = 180,
    sku: str = "", title: str = "",
) -> Dict[str, Any]:
    """触发控制台优惠券倒计时画层。"""
    from server.routes.ws_live import ws_manager
    duration_sec: int = max(1, min(int(seconds), 3600))
    payload = {
        "sku": sku,
        "title": title,
        "desc": desc,
        "seconds": duration_sec,
    }
    global_scene_overlay_state.set_coupon(payload, duration_sec)
    await ws_manager.broadcast("ONSCREEN_COUPON", payload)
    logger.info(f"优惠券倒计时画层已显示: {desc} ({duration_sec}s)")
    return {"ok": True, **payload}


async def product_closeup(
    sku: str = "", title: str = "", image: str = "", seconds: int = 6,
) -> Dict[str, Any]:
    """在控制台监视器显示商品特写画层。"""
    from server.routes.ws_live import ws_manager
    duration_sec: int = max(1, min(int(seconds), 60))
    payload = {
        "sku": sku,
        "title": title,
        "image": image,
        "seconds": duration_sec,
    }
    global_scene_overlay_state.set_scene("closeup", payload, duration_sec)
    await ws_manager.broadcast("CAMERA_CLOSEUP", payload)
    logger.info(f"商品特写画层已显示: {title or sku} ({duration_sec}s)")
    return {"ok": True, **payload}


async def show_size_chart(
    sku: str = "", title: str = "", size_chart: Optional[dict] = None,
    seconds: int = 20,
) -> Dict[str, Any]:
    """在控制台监视器显示真实尺码数据画层。"""
    from server.routes.ws_live import ws_manager
    duration_sec: int = max(1, min(int(seconds), 120))
    payload = {
        "sku": sku,
        "title": title,
        "size_chart": size_chart or {},
        "seconds": duration_sec,
    }
    global_scene_overlay_state.set_scene("size_chart", payload, duration_sec)
    await ws_manager.broadcast("SIZE_CHART", payload)
    logger.info(f"尺码对照画层已显示: {title or sku}")
    return {"ok": True, **payload}


def register_builtin_tools():
    """注册内置工具集"""
    global_mcp_tools.register("query_stock", query_stock)
    global_mcp_tools.register("trigger_onscreen_coupon", trigger_onscreen_coupon)
    global_mcp_tools.register("product_closeup", product_closeup)
    global_mcp_tools.register("show_size_chart", show_size_chart)


# 全局单例 (先于工具注册创建)
global_mcp_tools = MCPToolBus()

register_builtin_tools()
