import asyncio
import json
import base64
import hashlib
import pytest
from server.adapters.obs.obs_client import ObsWebSocketClient, global_obs_client


class MockObsServer:
    """轻量模拟 OBS Studio v5 WebSocket 服务端"""
    def __init__(self, host="127.0.0.1", port=14455, password=""):
        self.host = host
        self.port = port
        self.password = password
        self.server = None
        self.is_streaming = False
        self.output_bytes = 0

    async def _handler(self, ws):
        # 1. 下发 Op=0 Hello
        auth_data = None
        if self.password:
            auth_data = {
                "challenge": "mock_challenge_12345",
                "salt": "mock_salt_67890"
            }
        hello_msg = {
            "op": 0,
            "d": {
                "obsWebSocketVersion": "5.4.0",
                "rpcVersion": 1,
                "authentication": auth_data
            }
        }
        await ws.send(json.dumps(hello_msg))

        # 2. 接收 Op=1 Identify
        identify_raw = await ws.recv()
        identify_msg = json.loads(identify_raw)
        assert identify_msg.get("op") == 1

        if self.password:
            expected_secret = base64.b64encode(
                hashlib.sha256((self.password + "mock_salt_67890").encode("utf-8")).digest()
            ).decode("utf-8")
            expected_auth = base64.b64encode(
                hashlib.sha256((expected_secret + "mock_challenge_12345").encode("utf-8")).digest()
            ).decode("utf-8")
            actual_auth = identify_msg["d"].get("authentication")
            if actual_auth != expected_auth:
                await ws.close(code=4009, reason="Authentication failed")
                return

        # 3. 发送 Op=2 Identified
        await ws.send(json.dumps({"op": 2, "d": {"negotiatedRpcVersion": 1}}))

        # 4. 消息响应循环
        try:
            async for raw in ws:
                req = json.loads(raw)
                if req.get("op") == 6:
                    req_type = req["d"]["requestType"]
                    req_id = req["d"]["requestId"]
                    if req_type == "GetStreamStatus":
                        if self.is_streaming:
                            self.output_bytes += 50000
                        resp = {
                            "op": 7,
                            "d": {
                                "requestType": req_type,
                                "requestId": req_id,
                                "requestStatus": {"result": True, "code": 100},
                                "responseData": {
                                    "outputActive": self.is_streaming,
                                    "outputReconnecting": False,
                                    "outputTimecode": "00:01:23.000",
                                    "outputDuration": 83000,
                                    "outputBytes": self.output_bytes if self.is_streaming else 0,
                                    "outputSkippedFrames": 0,
                                    "outputTotalFrames": 2075,
                                }
                            }
                        }
                        await ws.send(json.dumps(resp))
                    elif req_type == "GetStats":
                        resp = {
                            "op": 7,
                            "d": {
                                "requestType": req_type,
                                "requestId": req_id,
                                "requestStatus": {"result": True, "code": 100},
                                "responseData": {
                                    "activeFps": 60.0,
                                    "cpuUsage": 12.5,
                                    "memoryUsage": 350.0,
                                }
                            }
                        }
                        await ws.send(json.dumps(resp))
                    elif req_type == "StartStream":
                        self.is_streaming = True
                        resp = {
                            "op": 7,
                            "d": {
                                "requestType": req_type,
                                "requestId": req_id,
                                "requestStatus": {"result": True, "code": 100},
                                "responseData": {}
                            }
                        }
                        await ws.send(json.dumps(resp))
                    elif req_type == "StopStream":
                        self.is_streaming = False
                        self.output_bytes = 0
                        resp = {
                            "op": 7,
                            "d": {
                                "requestType": req_type,
                                "requestId": req_id,
                                "requestStatus": {"result": True, "code": 100},
                                "responseData": {}
                            }
                        }
                        await ws.send(json.dumps(resp))
        except Exception:
            pass

    async def start(self):
        import websockets
        self.server = await websockets.serve(self._handler, self.host, self.port)

    async def stop(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()


def test_obs_client_connect_and_rpc():
    """测试 OBS-WebSocket 握手、带密码认证、推流启停与状态回读 (遵守 OBS v5 标准)"""
    async def _run():
        server = MockObsServer(port=14455, password="secret_password_123")
        await server.start()

        client = ObsWebSocketClient(port=14455, password="secret_password_123")
        try:
            connected = await client.connect(timeout=2.0)
            assert connected is True
            assert client.is_connected is True

            # 拉取初始状态 (未开播)
            stats = await client.refresh_stream_status()
            assert stats.get("active") is False

            # 控制 OBS 开始推流
            start_res = await client.start_stream()
            assert start_res.get("result") is True
            assert client.is_streaming is True

            # 验证开播幂等性
            idem_start = await client.start_stream()
            assert idem_start.get("result") is True
            assert idem_start.get("already_streaming") is True

            # 等待微小时钟步长确保采样差分时间推进
            await asyncio.sleep(0.01)

            # 再次拉取状态 (应显示推流中，基于 outputBytes 计算码率，基于 GetStats 获得 fps)
            stats_after = await client.refresh_stream_status()
            assert stats_after.get("active") is True
            assert stats_after.get("fps") == 60.0
            assert stats_after.get("cpu_usage") == 12.5
            assert stats_after.get("kbits_per_sec") > 0

            # 控制 OBS 停止推流
            stop_res = await client.stop_stream()
            assert stop_res.get("result") is True
            assert client.is_streaming is False

            # 验证停播幂等性
            idem_stop = await client.stop_stream()
            assert idem_stop.get("result") is True
            assert idem_stop.get("already_stopped") is True

        finally:
            await client.disconnect()
            await server.stop()

    asyncio.run(_run())


def test_obs_api_endpoints_integration():
    """测试 live.py 中 OBS 相关接口路由集成"""
    from fastapi.testclient import TestClient
    from server.app import app

    client = TestClient(app)

    # 1. 查询初始 OBS 状态 (未连接状态)
    res = client.get("/api/v1/live/obs/status")
    assert res.status_code == 200
    data = res.json()
    assert data["code"] == 0
    assert data["data"]["is_connected"] is False

    # 2. 查询 live status，external_publish 应保持 not_managed
    res_status = client.get("/api/v1/live/status")
    assert res_status.status_code == 200
    pub = res_status.json()["external_publish"]
    assert pub["status"] == "not_managed"
    assert pub["platform_live"] is None
