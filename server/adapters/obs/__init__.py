"""
OBS Studio 适配器模块
提供 OBS-WebSocket v5 异步协议客户端与推流生命周期联动控制器
"""
from server.adapters.obs.obs_client import ObsWebSocketClient, global_obs_client

__all__ = ["ObsWebSocketClient", "global_obs_client"]
