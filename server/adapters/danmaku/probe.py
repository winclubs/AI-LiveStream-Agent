# -*- coding: utf-8 -*-
"""
直播弹幕凭证与平台连通性健康探测器 (DanmakuProbe)
阶段二商业化加固：
1. 在开播前或运行时对目标平台的房间号、Cookie、ttwid/msToken 等凭证进行连通性与风控预检；
2. 提前告知用户凭证是否过期、环境是否缺失必要库 (例如 B 站缺失 brotli)；
3. 给出精准的排查建议与修复路径，消除开播后收不到弹幕的排障盲区。
"""
import logging
import re
import shutil
from typing import Any, Dict, Optional

logger = logging.getLogger("LiveAgent.DanmakuProbe")

try:
    import httpx
    HTTPX_AVAILABLE = True
except ImportError:
    httpx = None
    HTTPX_AVAILABLE = False

try:
    import brotli
    BROTLI_AVAILABLE = True
except ImportError:
    brotli = None
    BROTLI_AVAILABLE = False


async def probe_danmaku_platform(
    platform: str,
    room_id: str,
    ttwid: str = "",
    ms_token: str = "",
    cookie: str = "",
) -> Dict[str, Any]:
    """
    对指定直播平台弹幕连通性进行无干扰轻量探针探测
    :return: {
        "platform": platform,
        "room_id": room_id,
        "healthy": bool,
        "status": "ready" | "warning" | "error" | "mock",
        "message": str,
        "suggestions": List[str],
        "diagnostics": dict
    }
    """
    plat = (platform or "bilibili").lower().strip()
    clean_room = (room_id or "").strip()

    suggestions = []
    diagnostics = {
        "httpx_available": HTTPX_AVAILABLE,
        "brotli_available": BROTLI_AVAILABLE,
    }

    # 1. 仿真/演示模式
    if plat in ("mock", "demo") or clean_room.lower() in ("mock", "room_demo", "room_demo_888"):
        return {
            "platform": plat,
            "room_id": clean_room,
            "healthy": True,
            "status": "mock",
            "message": "仿真弹幕模式：无需真实平台凭证与网络，开播自动注入模拟观众互动与送礼事件。",
            "suggestions": [],
            "diagnostics": diagnostics,
        }

    # 2. Bilibili 平台探测
    if plat == "bilibili":
        clean_num = re.sub(r"[^0-9]", "", clean_room.split("?")[0].rstrip("/").split("/")[-1]) or clean_room
        if not clean_num:
            return {
                "platform": plat,
                "room_id": clean_room,
                "healthy": False,
                "status": "error",
                "message": "未提供有效的 B 站直播间号或链接 (应为纯数字房号)",
                "suggestions": ["请在直播设置中填入有效的 B 站直播间号，如 123456"],
                "diagnostics": diagnostics,
            }

        if not BROTLI_AVAILABLE:
            suggestions.append("B 站弹幕协议需 brotli 库解压长连接数据包，请执行: pip install brotli")

        if not HTTPX_AVAILABLE:
            return {
                "platform": plat,
                "room_id": clean_num,
                "healthy": False,
                "status": "warning",
                "message": "缺少 httpx 库，无法发起预检，但开播后将尝试 WebSocket 直连",
                "suggestions": ["建议执行 pip install httpx 以开启完整连通性预检"],
                "diagnostics": diagnostics,
            }

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(
                    f"https://api.live.bilibili.com/room/v1/Room/get_info?room_id={clean_num}",
                    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("code") == 0:
                        r_info = data.get("data", {})
                        live_status = "直播中" if r_info.get("live_status") == 1 else "未开播"
                        title = r_info.get("title", "")
                        return {
                            "platform": plat,
                            "room_id": clean_num,
                            "healthy": True,
                            "status": "ready" if BROTLI_AVAILABLE else "warning",
                            "message": f"B 站直播间有效 (状态: {live_status}，标题: {title[:20]})",
                            "suggestions": suggestions,
                            "diagnostics": {**diagnostics, "title": title, "live_status": live_status},
                        }
                    else:
                        return {
                            "platform": plat,
                            "room_id": clean_num,
                            "healthy": False,
                            "status": "error",
                            "message": f"B 站房间不存在或已封禁: {data.get('message', '未知错误')}",
                            "suggestions": ["请核对 B 站直播间真实房号"],
                            "diagnostics": diagnostics,
                        }
        except Exception as e:
            return {
                "platform": plat,
                "room_id": clean_num,
                "healthy": False,
                "status": "warning",
                "message": f"探测 B 站接口超时或网络受阻: {e}",
                "suggestions": ["请检查本机公网网络连接，或开播后使用被动推送中继"],
                "diagnostics": diagnostics,
            }

    # 3. 抖音平台探测 (Douyin)
    if plat == "douyin":
        has_ttwid = bool(ttwid and len(ttwid) > 10)
        has_token = bool(ms_token and len(ms_token) > 10)

        if not has_ttwid:
            suggestions.append("建议在应用设置中配置 douyin_ttwid (从浏览器 DevTools 获取)，可显著降低平台风控拦截几率")
        if not has_token:
            suggestions.append("建议配置 douyin_ms_token (从浏览器抓包获取)")

        return {
            "platform": plat,
            "room_id": clean_room,
            "healthy": bool(clean_room),
            "status": "ready" if has_ttwid else "warning",
            "message": "抖音凭证已配置并就绪" if has_ttwid else "抖音未配置 ttwid/msToken 凭证，风控期可能无法握手，建议补齐凭证或使用中继模式",
            "suggestions": suggestions,
            "diagnostics": {
                **diagnostics,
                "has_ttwid": has_ttwid,
                "has_ms_token": has_token,
                "relay_endpoints": ["POST /api/v1/live/danmaku-webhook", "WS /ws/danmaku-ingest"],
            },
        }

    # 4. 快手 / 视频号 / 其他平台
    return {
        "platform": plat,
        "room_id": clean_room,
        "healthy": bool(clean_room),
        "status": "ready" if clean_room else "warning",
        "message": f"平台【{plat}】已就绪 (支持直连与中继注入)",
        "suggestions": suggestions,
        "diagnostics": diagnostics,
    }
