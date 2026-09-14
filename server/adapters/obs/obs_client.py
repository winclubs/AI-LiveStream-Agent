"""
OBS-WebSocket v5 异步协议客户端
支持与 OBS Studio 28+ 内置的 WebSocket 服务器通信，
实现：自动握手认证、推流状态监控 (码率/丢帧/时长)、一键控制开播/停播与场景探测。
"""
import asyncio
import base64
import hashlib
import json
import logging
import time
import uuid
from typing import Optional, Dict, Any, Callable

logger = logging.getLogger("LiveAgent.OBSClient")

try:
    import websockets
except ImportError:  # pragma: no cover
    websockets = None


class ObsWebSocketClient:
    """OBS Studio v5 WebSocket 协议全双工客户端"""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 4455,
        password: str = "",
        on_stream_state_change: Optional[Callable[[bool, str], Any]] = None,
    ):
        self.host = host
        self.port = int(port)
        self.password = password
        self._stream_state_listeners: list = []
        if on_stream_state_change:
            self._stream_state_listeners.append(on_stream_state_change)

        self.ws: Optional[Any] = None
        self.is_connected = False
        self.is_streaming = False
        self.is_stale = False
        self.stream_stats: Dict[str, Any] = {}

        self._pending_requests: Dict[str, asyncio.Future] = {}
        self._receive_task: Optional[asyncio.Task] = None
        self._monitor_task: Optional[asyncio.Task] = None
        self._auto_reconnect = True
        self._lock = asyncio.Lock()
        self._connection_epoch: int = 0

        # 实时码率采样差分基准
        self._last_stats_time: Optional[float] = None
        self._last_stats_bytes: int = 0
        self._connected_host: str = ""
        self._connected_port: int = 0
        self._connected_password: str = ""

    @property
    def connection_epoch(self) -> int:
        return self._connection_epoch

    def add_stream_state_listener(self, listener: Callable[..., Any]):
        """注册推流状态变更监听回调 (支持 (active, state) 或 (active, state, epoch))"""
        if listener not in self._stream_state_listeners:
            self._stream_state_listeners.append(listener)

    @property
    def url(self) -> str:
        return f"ws://{self.host}:{self.port}"

    async def connect(self, timeout: float = 3.0) -> bool:
        """连接到 OBS Studio 并完成 v5 握手认证 (每次连接分配单调递增的 connection epoch)"""
        if websockets is None:
            logger.warning("未安装 websockets 库，无法建立 OBS-WebSocket 连接")
            return False

        async with self._lock:
            self._auto_reconnect = True
            # 若已连接且配置未变，直接复用
            if (
                self.is_connected
                and self.ws
                and not getattr(self.ws, "closed", False)
                and self._connected_host == self.host
                and self._connected_port == self.port
                and self._connected_password == self.password
            ):
                return True

            if self.is_connected or self.ws:
                logger.info("OBS 配置变更或重置，清理旧连接: %s:%s (epoch=%d)", self._connected_host, self._connected_port, self._connection_epoch)
                await self._cleanup(epoch=self._connection_epoch, reason="reconnect_cleanup")

            # 分配全局唯一单调递增的连接代际
            epoch = self._connection_epoch + 1
            self._connection_epoch = epoch
            ws = None

            try:
                ws = await asyncio.wait_for(
                    websockets.connect(self.url, ping_interval=10, ping_timeout=5),
                    timeout=timeout
                )
                # 接收 Op=0 (Hello)
                hello_raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                hello_msg = json.loads(hello_raw)
                if hello_msg.get("op") != 0:
                    raise RuntimeError(f"预期 Op=0 Hello 消息，实际收到: {hello_msg}")

                hello_data = hello_msg.get("d", {})
                auth_req = hello_data.get("authentication")

                # 构造 Op=1 (Identify)
                identify_payload: Dict[str, Any] = {
                    "op": 1,
                    "d": {
                        "rpcVersion": 1,
                        "eventSubscriptions": 33  # General (1) + Outputs (32)
                    }
                }

                if auth_req:
                    # 加盐哈希认证
                    challenge = auth_req.get("challenge", "")
                    salt = auth_req.get("salt", "")
                    secret_hash = base64.b64encode(
                        hashlib.sha256((self.password + salt).encode("utf-8")).digest()
                    ).decode("utf-8")
                    auth_resp = base64.b64encode(
                        hashlib.sha256((secret_hash + challenge).encode("utf-8")).digest()
                    ).decode("utf-8")
                    identify_payload["d"]["authentication"] = auth_resp

                await ws.send(json.dumps(identify_payload))

                # 接收 Op=2 (Identified)
                identified_raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                identified_msg = json.loads(identified_raw)
                if identified_msg.get("op") != 2:
                    raise RuntimeError(f"OBS 认证失败，预期 Op=2 Identified，实际收到: {identified_msg}")

                self.ws = ws
                self.is_connected = True
                self.is_stale = False
                self._connected_host = self.host
                self._connected_port = self.port
                self._connected_password = self.password
                self._last_stats_time = None
                self._last_stats_bytes = 0

                self._receive_task = asyncio.create_task(self._listen_loop(epoch, ws))
                if not self._monitor_task or self._monitor_task.done():
                    self._monitor_task = asyncio.create_task(self._monitor_loop())
                logger.info("已成功建立与 OBS Studio (WebSocket v5) 的受控连接: %s (epoch=%d)", self.url, epoch)

                # 初始拉取一次推流状态与硬件监控
                await self.refresh_stream_status()
                return True

            except Exception as e:
                logger.debug("连接 OBS-WebSocket 失败 (%s, epoch=%d): %s", self.url, epoch, e)
                await self._cleanup(epoch=epoch, ws_instance=ws, reason="connect_failed")
                return False

    def _notify_stream_state(self, active: bool, state: str, epoch: Optional[int] = None):
        """统一分发流状态变更给所有监听者 (携带 epoch，严防旧连接迟到事件误伤新场次所有权)"""
        notify_epoch = epoch if epoch is not None else self._connection_epoch
        for listener in list(self._stream_state_listeners):
            try:
                try:
                    res = listener(active, state, notify_epoch)
                except TypeError:
                    # 兼容仅接受 2 个参数的旧签名回调
                    res = listener(active, state)
                if asyncio.iscoroutine(res):
                    asyncio.create_task(res)
            except Exception as e:
                logger.warning("推流状态变更监听回调执行异常: %s", e)

    async def _cleanup(self, epoch: Optional[int] = None, ws_instance: Optional[Any] = None, reason: str = ""):
        """安全清理连接资源；旧 epoch 的迟到退出绝不触碰全局活跃连接与事件广播"""
        if epoch is not None and epoch < self._connection_epoch:
            logger.debug("忽略过时旧连接的迟到清理 (epoch=%d < current=%d, reason=%s)", epoch, self._connection_epoch, reason)
            if ws_instance:
                try:
                    await ws_instance.close()
                except Exception:
                    pass
            return

        was_streaming = self.is_streaming
        was_connected = self.is_connected
        current_epoch = self._connection_epoch
        self.is_connected = False
        self.is_streaming = False

        if was_streaming or was_connected:
            self._notify_stream_state(False, "Disconnected", epoch=current_epoch)

        if self._receive_task and not self._receive_task.done():
            if self._receive_task is not asyncio.current_task():
                self._receive_task.cancel()
            self._receive_task = None

        target_ws = ws_instance or self.ws
        if target_ws:
            try:
                await target_ws.close()
            except Exception:
                pass
        if self.ws is target_ws or not ws_instance:
            self.ws = None

        # 清除未完成的 request futures
        for req_id, fut in list(self._pending_requests.items()):
            if not fut.done():
                fut.cancel()
        self._pending_requests.clear()

        self._last_stats_time = None
        self._last_stats_bytes = 0

    async def _monitor_loop(self):
        """后台采样与断线自动指数退避重连 (连续采样错误主动 teardown 进入退避自愈)"""
        backoff = 1.0
        sample_failures = 0
        while True:
            try:
                await asyncio.sleep(3.0)
                if self.is_connected and self.ws:
                    try:
                        stats = await self.refresh_stream_status()
                        if stats and stats.get("error"):
                            self.is_stale = True
                            sample_failures += 1
                            if sample_failures >= 3:
                                logger.warning("OBS 后台采样连续 %d 次失败，主动拆除失效连接并触发重连", sample_failures)
                                await self._cleanup(epoch=self._connection_epoch, reason="sample_errors_exceeded")
                                sample_failures = 0
                        else:
                            self.is_stale = False
                            sample_failures = 0
                        backoff = 1.0
                    except Exception as e:
                        self.is_stale = True
                        logger.debug("OBS 后台指标采样异常: %s", e)
                elif self._auto_reconnect and self._connected_host:
                    self.is_stale = True
                    logger.info("OBS 连接中断，尝试后台自动重连 (退避 %.1fs)...", backoff)
                    ok = await self.connect(timeout=2.0)
                    if ok:
                        logger.info("OBS 自动重连并恢复握手与订阅成功！")
                        backoff = 1.0
                        sample_failures = 0
                    else:
                        backoff = min(15.0, backoff * 2.0)
                    await asyncio.sleep(backoff)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug("OBS 监控巡检异常: %s", e)

    async def disconnect(self):
        """主动断开连接"""
        self._auto_reconnect = False
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            self._monitor_task = None
        async with self._lock:
            await self._cleanup(epoch=None, reason="manual_disconnect")
            logger.info("OBS-WebSocket 连接已安全断开")

    async def _listen_loop(self, epoch: int, ws: Any):
        """后台循环处理 OBS 事件与 RPC 回包 (严格绑定连接 epoch 与对应 ws 实例)"""
        try:
            while self.is_connected and self._connection_epoch == epoch and ws and not getattr(ws, "closed", False):
                msg_raw = await ws.recv()
                if not msg_raw:
                    continue
                if self._connection_epoch != epoch:
                    break
                msg = json.loads(msg_raw)
                op = msg.get("op")
                d = msg.get("d", {})

                if op == 7:  # RequestResponse
                    req_id = d.get("requestId")
                    if req_id and req_id in self._pending_requests:
                        fut = self._pending_requests.pop(req_id)
                        if not fut.done():
                            fut.set_result(d)

                elif op == 5:  # Event
                    event_type = d.get("eventType")
                    event_data = d.get("eventData", {})
                    await self._handle_event(event_type, event_data, epoch)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning("OBS-WebSocket 接收监听异常 (epoch=%d): %s", epoch, e)
        finally:
            await self._cleanup(epoch=epoch, ws_instance=ws, reason="listen_loop_finally")

    async def _handle_event(self, event_type: str, event_data: dict, epoch: int):
        """处理 OBS 推流/录制状态变动事件 (过时 epoch 事件直接静默丢弃)"""
        if epoch != self._connection_epoch:
            logger.debug("丢弃来自过时连接的 OBS 事件: epoch=%d < current=%d", epoch, self._connection_epoch)
            return
        if event_type == "StreamStateChanged":
            active = event_data.get("outputActive", False)
            state = event_data.get("outputState", "")
            self.is_streaming = active
            logger.info("OBS 推流状态变动通知: active=%s, state=%s (epoch=%d)", active, state, epoch)
            self._notify_stream_state(active, state, epoch=epoch)

    async def send_request(self, request_type: str, request_data: Optional[dict] = None, timeout: float = 4.0) -> dict:
        """发送 v5 RPC 请求 (Op=6) 并等待响应 (Op=7)"""
        if not self.is_connected or not self.ws:
            # 尝试自愈重连一次
            reconnected = await self.connect(timeout=2.0)
            if not reconnected:
                return {"result": False, "error": "OBS 客户端未连接"}

        req_id = f"req_{uuid.uuid4().hex[:8]}"
        payload = {
            "op": 6,
            "d": {
                "requestType": request_type,
                "requestId": req_id,
                "requestData": request_data or {}
            }
        }

        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._pending_requests[req_id] = fut

        try:
            await self.ws.send(json.dumps(payload))
            resp = await asyncio.wait_for(fut, timeout=timeout)
            status = resp.get("requestStatus", {})
            if status.get("result"):
                return {"result": True, "data": resp.get("responseData", {})}
            else:
                return {
                    "result": False,
                    "code": status.get("code"),
                    "comment": status.get("comment", "请求失败")
                }
        except asyncio.TimeoutError:
            self._pending_requests.pop(req_id, None)
            return {"result": False, "error": f"请求 OBS [{request_type}] 超时"}
        except Exception as e:
            self._pending_requests.pop(req_id, None)
            return {"result": False, "error": str(e)}

    async def refresh_stream_status(self) -> Dict[str, Any]:
        """
        获取并刷新 OBS 推流状态
        规范遵循 OBS WebSocket v5 标准：
        1. 使用 GetStreamStatus 读取 outputBytes、outputSkippedFrames、outputTotalFrames；
        2. 使用 GetStats 获取 activeFps、cpuUsage、memoryUsage 等全局运行指标；
        3. 基于连续采样的 outputBytes 与时间差分真实计算实时码率 (kbits_per_sec)。
        """
        resp = await self.send_request("GetStreamStatus")
        if not resp.get("result"):
            self.stream_stats = {"active": False, "error": resp.get("error") or resp.get("comment")}
            return self.stream_stats

        data = resp.get("data", {})
        self.is_streaming = data.get("outputActive", False)
        curr_bytes = data.get("outputBytes", 0)

        # 真实码率差分计算 (基于连续采样 outputBytes 或 duration 保底)
        now = time.monotonic()
        kbps = 0.0
        if self._last_stats_time is not None and (now - self._last_stats_time) > 0.001:
            dt = now - self._last_stats_time
            bytes_diff = max(0, curr_bytes - self._last_stats_bytes)
            kbps = round((bytes_diff * 8) / (1000.0 * dt), 1)
        elif data.get("outputDuration", 0) > 0 and curr_bytes > 0:
            dur_sec = data.get("outputDuration", 0) / 1000.0
            kbps = round((curr_bytes * 8) / (1000.0 * dur_sec), 1)

        self._last_stats_time = now
        self._last_stats_bytes = curr_bytes

        # 请求 GetStats 补充真实 fps 与资源消耗 (OBS v5 标准)
        stats_resp = await self.send_request("GetStats")
        stats_data = stats_resp.get("data", {}) if stats_resp.get("result") else {}

        active_fps = round(float(stats_data.get("activeFps", 0.0)), 1)
        cpu_usage = round(float(stats_data.get("cpuUsage", 0.0)), 1)
        mem_usage = round(float(stats_data.get("memoryUsage", 0.0)), 1)

        self.stream_stats = {
            "active": self.is_streaming,
            "reconnecting": data.get("outputReconnecting", False),
            "timecode": data.get("outputTimecode", "00:00:00"),
            "duration_sec": int(data.get("outputDuration", 0) / 1000),
            "bytes": curr_bytes,
            "kbits_per_sec": kbps,
            "skipped_frames": data.get("outputSkippedFrames", 0),
            "total_frames": data.get("outputTotalFrames", 0),
            "fps": active_fps,
            "cpu_usage": cpu_usage,
            "memory_usage": mem_usage,
        }
        return self.stream_stats

    async def start_stream(self) -> Dict[str, Any]:
        """通知 OBS 开始推流 (具备严格幂等性)"""
        if self.is_connected:
            try:
                stats = await self.refresh_stream_status()
                if stats and stats.get("error"):
                    return {"result": False, "error": f"刷新推流状态失败: {stats.get('error')}"}
            except Exception as e:
                return {"result": False, "error": f"刷新推流状态异常: {e}"}
        if self.is_streaming:
            logger.info("OBS 当前已处于推流状态，无需重复触发")
            return {"result": True, "already_streaming": True}

        res = await self.send_request("StartStream")
        if res.get("result"):
            self.is_streaming = True
            logger.info("已成功通过 WebSocket 触发 OBS 开始推流！")
        else:
            logger.warning("触发 OBS 开始推流失败: %s", res)
        return res

    async def stop_stream(self) -> Dict[str, Any]:
        """通知 OBS 停止推流 (具备严格幂等性)"""
        if self.is_connected:
            try:
                stats = await self.refresh_stream_status()
                if stats and stats.get("error"):
                    return {"result": False, "error": f"刷新推流状态失败: {stats.get('error')}"}
            except Exception as e:
                return {"result": False, "error": f"刷新推流状态异常: {e}"}
        if not self.is_streaming:
            logger.info("OBS 当前未在推流，无需重复停止")
            return {"result": True, "already_stopped": True}

        res = await self.send_request("StopStream")
        if res.get("result"):
            self.is_streaming = False
            logger.info("已成功通过 WebSocket 触发 OBS 停止推流！")
        else:
            logger.warning("触发 OBS 停止推流失败: %s", res)
        return res

    async def get_scenes(self) -> Dict[str, Any]:
        """获取 OBS 场景列表与当前活动场景"""
        return await self.send_request("GetSceneList")

    def get_summary(self) -> Dict[str, Any]:
        """获取轻量摘要供 API 和控制台直接渲染"""
        return {
            "is_connected": self.is_connected,
            "is_streaming": self.is_streaming,
            "is_stale": getattr(self, "is_stale", False),
            "host": self.host,
            "port": self.port,
            "stats": self.stream_stats
        }


# 全局单例 OBS 客户端
global_obs_client = ObsWebSocketClient()

