"""
端云分离模式 (Tier C) 远程算力节点渲染驱动。

每次合成请求都按事务处理：只有收到同一 request_id 的 audio_end 后，才向上层提交
完整音频；超时、远端错误或连接异常会丢弃暂存数据并重建连接，避免半句播放或串句。
远端视频帧同样按 request_id 暂存，待完整音频进入本地播放队列后再按时间线发布。
协议 v2 为二进制音频增加 request_id envelope；未协商 v2 的旧节点继续使用单请求原始二进制。
"""
import asyncio
import base64
import inspect
import json
import logging
import time
import uuid
from typing import AsyncGenerator, Optional

from server.adapters.media.base_driver import BaseMediaDriver
from server.core.media.av_sync import global_av_sync
from server.core.media.shared_playback_clock import global_shared_playback_clock

logger = logging.getLogger("LiveAgent.RemoteGPUDriver")

try:
    import websockets
except ImportError:
    websockets = None


class RemoteGPUMediaDriver(BaseMediaDriver):
    """连接远程算力节点的媒体驱动（云端 TTS 与可选视频帧回传）。"""

    CONNECT_TIMEOUT = 5.0
    CHUNK_TIMEOUT = 20.0
    REQUEST_TIMEOUT = 120.0
    PROTOCOL_VERSION = 2
    AUDIO_MAGIC = b"RGA2"
    MAX_REQUEST_ID_BYTES = 64
    MAX_AUDIO_BYTES = 32 * 1024 * 1024
    MAX_VIDEO_FRAMES = 3000
    MAX_VIDEO_BYTES = 128 * 1024 * 1024
    MAX_PENDING_TRANSACTIONS = 8

    def __init__(self, node_url: str = "ws://127.0.0.1:8888/ws/render", auth_token: str = ""):
        super().__init__()
        self.node_url = node_url
        self.auth_token = auth_token
        self.is_running = False
        self.is_connected = False
        self.speed = 1.0
        self._ws = None
        self._lock = asyncio.Lock()
        self._protocol_version = 1
        self._current_request_id: Optional[str] = None
        self._last_completed_request_id: Optional[str] = None
        self._pending_video_transactions: dict[str, list[tuple[int, bytes]]] = {}
        # 保留最近一次事务的兼容视图，旧调用方应改用 request_id 提交。
        self._pending_video_frames: list[tuple[int, bytes]] = []
        self._video_task: Optional[asyncio.Task] = None
        self._video_tasks: set[asyncio.Task] = set()
        self._video_timeline_lock = asyncio.Lock()
        self.latest_jpeg: bytes = b""
        self.frames_received = 0
        # 发布帧携带的采样时钟换算 PTS (由时间线循环在 report_audio_head 后写入)
        self._pending_video_pts_ms: Optional[float] = None

    async def start(self):
        self.is_running = True
        self.is_connected = await self._ensure_connection()

    async def stop(self):
        self.is_running = False
        self.is_speaking = False
        await self._cancel_video_playback(clear_frame=True, clear_pending=True)
        await self._close_connection()

    async def apply_role(self, role):
        """兼容旧接口：从角色对象同步语速到云节点渲染参数。"""
        self.speed = float(getattr(role, "speech_speed", 1.0) or 1.0) if role else 1.0

    async def apply_speech_speed(self, speed: float):
        """应用口播语速倍率。"""
        self.speed = float(speed or 1.0)

    async def _close_connection(self):
        ws = self._ws
        self._ws = None
        self.is_connected = False
        self._protocol_version = 1
        self._current_request_id = None
        if ws is not None:
            try:
                await asyncio.wait_for(ws.close(), timeout=self.CONNECT_TIMEOUT)
            except Exception:
                pass

    async def _ensure_connection(self) -> bool:
        if websockets is None:
            logger.warning("websockets 库未安装，端云分离通道不可用")
            self.is_connected = False
            return False

        if self._ws is not None:
            try:
                pong_waiter = await self._ws.ping()
                if inspect.isawaitable(pong_waiter):
                    await asyncio.wait_for(pong_waiter, timeout=self.CONNECT_TIMEOUT)
                self.is_connected = True
                return True
            except Exception:
                await self._close_connection()

        try:
            self._ws = await asyncio.wait_for(
                websockets.connect(self.node_url, max_size=2 ** 23),
                timeout=self.CONNECT_TIMEOUT,
            )
            await self._ws.send(json.dumps({
                "event": "auth",
                "token": self.auth_token,
                "protocol_version": self.PROTOCOL_VERSION,
            }))
            raw_reply = await asyncio.wait_for(self._ws.recv(), timeout=self.CONNECT_TIMEOUT)
            reply = json.loads(raw_reply)
            if reply.get("event") == "auth_ok":
                try:
                    negotiated = int(reply.get("protocol_version") or 1)
                except (TypeError, ValueError):
                    negotiated = 1
                self._protocol_version = min(self.PROTOCOL_VERSION, max(1, negotiated))
                self.is_connected = True
                logger.info(
                    "云端算力节点连接成功: %s (协议 v%s)",
                    self.node_url,
                    self._protocol_version,
                )
                return True
            logger.error("云端节点鉴权失败: %s", reply)
        except Exception as exc:
            logger.warning("云端算力节点连接失败 (%s)", exc)

        await self._close_connection()
        return False

    async def feed_audio_chunk(self, audio_bytes: bytes, text_snippet: str):
        """兼容媒体驱动接口；远程驱动在本项目中作为 TTS source 使用。"""
        self.is_speaking = bool(audio_bytes)

    @staticmethod
    def _decode_video_frame(b64_data: str) -> bytes:
        if not b64_data:
            return b""
        try:
            return base64.b64decode(b64_data, validate=True)
        except Exception:
            return b""

    @classmethod
    def _decode_audio_envelope(cls, raw: bytes) -> tuple[str, bytes]:
        if not raw.startswith(cls.AUDIO_MAGIC) or len(raw) <= len(cls.AUDIO_MAGIC):
            raise ValueError("远程音频缺少 v2 envelope")
        id_len = raw[len(cls.AUDIO_MAGIC)]
        header_len = len(cls.AUDIO_MAGIC) + 1 + id_len
        if id_len < 1 or id_len > cls.MAX_REQUEST_ID_BYTES or len(raw) < header_len:
            raise ValueError("远程音频 envelope 非法")
        request_id = raw[len(cls.AUDIO_MAGIC) + 1:header_len].decode("utf-8")
        return request_id, raw[header_len:]

    def _stage_video_transaction(self, request_id: str, frames: list[tuple[int, bytes]]) -> None:
        while len(self._pending_video_transactions) >= self.MAX_PENDING_TRANSACTIONS:
            oldest = next(iter(self._pending_video_transactions))
            self._pending_video_transactions.pop(oldest, None)
        self._pending_video_transactions[request_id] = frames
        self._last_completed_request_id = request_id
        self._pending_video_frames = frames

    def _clear_video_transaction(self, request_id: str) -> None:
        self._pending_video_transactions.pop(request_id, None)
        if self._last_completed_request_id == request_id:
            self._last_completed_request_id = None
            self._pending_video_frames = []

    @property
    def last_completed_request_id(self) -> Optional[str]:
        return self._last_completed_request_id

    async def synthesize_stream(self, text: str) -> AsyncGenerator[bytes, None]:
        """请求整句云端合成；仅在收到对应 audio_end 后提交完整结果。"""
        async with self._lock:
            if not await self._ensure_connection():
                raise RuntimeError("远程算力节点不可用")

            request_id = uuid.uuid4().hex
            self._current_request_id = request_id
            audio_chunks: list[bytes] = []
            audio_bytes = 0
            staged_video_frames: list[tuple[int, bytes]] = []
            video_bytes = 0
            completed = False
            deadline = time.monotonic() + self.REQUEST_TIMEOUT
            self.is_speaking = True

            try:
                await self._ws.send(json.dumps({
                    "event": "tts_request",
                    "request_id": request_id,
                    "text": text,
                    "speed": self.speed,
                }))
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise asyncio.TimeoutError
                    raw = await asyncio.wait_for(
                        self._ws.recv(),
                        timeout=min(self.CHUNK_TIMEOUT, remaining),
                    )
                    if isinstance(raw, bytes):
                        if self._protocol_version >= 2:
                            envelope_request_id, audio = self._decode_audio_envelope(raw)
                            if envelope_request_id != request_id:
                                logger.debug("忽略过期远程音频: %s", envelope_request_id)
                                continue
                        else:
                            # 旧协议通过单连接、单请求和失败即断连保证二进制归属。
                            audio = raw
                        if not audio:
                            continue
                        audio_bytes += len(audio)
                        if audio_bytes > self.MAX_AUDIO_BYTES:
                            raise RuntimeError("远程音频超过单请求容量上限")
                        audio_chunks.append(audio)
                        continue

                    msg = json.loads(raw)
                    msg_request_id = msg.get("request_id")
                    if self._protocol_version >= 2:
                        if msg_request_id != request_id:
                            logger.debug("忽略缺失或过期的远程控制帧: %s", msg_request_id)
                            continue
                    elif msg_request_id and msg_request_id != request_id:
                        logger.debug("忽略过期远程响应: %s", msg_request_id)
                        continue

                    event = msg.get("event")
                    if event == "audio_end":
                        if audio_bytes <= 0:
                            raise RuntimeError("远程音频事务为空")
                        declared_bytes = msg.get("bytes")
                        if declared_bytes is not None:
                            try:
                                declared_bytes = int(declared_bytes)
                            except (TypeError, ValueError) as exc:
                                raise RuntimeError("远程 audio_end.bytes 非法") from exc
                            if declared_bytes < 0 or declared_bytes != audio_bytes:
                                raise RuntimeError(
                                    f"远程音频完整性校验失败: declared={declared_bytes}, received={audio_bytes}"
                                )
                        completed = True
                        break
                    if event == "video_frame":
                        jpeg = self._decode_video_frame(msg.get("data", ""))
                        if jpeg:
                            if len(staged_video_frames) >= self.MAX_VIDEO_FRAMES:
                                raise RuntimeError("远程视频帧数超过单请求上限")
                            video_bytes += len(jpeg)
                            if video_bytes > self.MAX_VIDEO_BYTES:
                                raise RuntimeError("远程视频超过单请求容量上限")
                            pts_ms = max(0, int(msg.get("pts_ms", len(staged_video_frames) * 40)))
                            staged_video_frames.append((pts_ms, jpeg))
                        continue
                    if event == "error":
                        raise RuntimeError(msg.get("message") or "远程渲染节点返回错误")

                if not completed:
                    raise RuntimeError("远程合成未收到结束标记")
                self._stage_video_transaction(request_id, staged_video_frames)
                for chunk in audio_chunks:
                    yield chunk
            except GeneratorExit:
                await self._abort_request(request_id, "consumer closed stream")
                raise
            except asyncio.CancelledError:
                await self._abort_request(request_id, "local task cancelled")
                raise
            except asyncio.TimeoutError as exc:
                logger.warning("云端节点响应超时，丢弃本次暂存音视频并重置连接")
                await self._abort_request(request_id, "timeout")
                raise RuntimeError("远程合成响应超时") from exc
            except Exception:
                logger.warning("云端合成失败，丢弃本次暂存音视频并重置连接", exc_info=True)
                await self._abort_request(request_id, "remote error")
                raise
            finally:
                self.is_speaking = False
                if self._current_request_id == request_id:
                    self._current_request_id = None

    async def _abort_request(self, request_id: str, reason: str):
        self._clear_video_transaction(request_id)
        if self._ws is not None:
            try:
                await self._ws.send(json.dumps({
                    "event": "cancel",
                    "request_id": request_id,
                    "reason": reason,
                }))
            except Exception:
                pass
        await self._close_connection()

    async def interrupt(self, reason: str = "Barge-in"):
        """抢占时关闭当前连接，确保旧请求任何迟到数据都不能进入下一句。"""
        self.is_speaking = False
        request_id = self._current_request_id
        await self._cancel_video_playback(clear_frame=True, clear_pending=True)
        if request_id:
            await self._abort_request(request_id, reason)
        else:
            await self._close_connection()
        logger.info("RemoteGPU 收到打断信令 [%s]，已隔离旧请求", reason)

    async def commit_video_frames(
        self,
        audio_bytes: bytes = b"",
        *,
        codec: str = "mp3",
        sample_rate: int = 24000,
        channels: int = 1,
        request_id: Optional[str] = None,
        audio_id: Optional[str] = None,
    ) -> None:
        """提交远程帧；有 audio_id 时由同一 VirtualAudio 播放头决定发布进度。"""
        target_request_id = request_id or self._last_completed_request_id
        frames = self._pending_video_transactions.pop(target_request_id, []) if target_request_id else []
        if not frames and request_id is None:
            frames = self._pending_video_frames
        if target_request_id == self._last_completed_request_id:
            self._last_completed_request_id = None
            self._pending_video_frames = []

        # 旧 API 没有 audio_id 时保持单时间线行为；原子事务模式允许多句各自等待其 cursor。
        if not audio_id:
            await self._cancel_video_playback(clear_frame=True, clear_pending=False)
        if not frames:
            return

        duration = max(0.04, frames[-1][0] / 1000.0 if frames else 0.04)
        if audio_bytes:
            try:
                from server.core.media.audio_decode import decode_audio_to_float32
                samples, decoded_rate = await asyncio.to_thread(
                    decode_audio_to_float32,
                    audio_bytes,
                    sample_rate,
                    codec=codec,
                    channels=channels,
                )
                if samples is not None and decoded_rate > 0:
                    duration = max(0.04, len(samples) / float(decoded_rate))
            except Exception:
                logger.debug("无法读取远程音频时长，使用云帧时间线", exc_info=True)

        task = asyncio.create_task(self._play_video_timeline(frames, duration, audio_id=audio_id))
        self._video_task = task
        self._video_tasks.add(task)
        task.add_done_callback(self._video_tasks.discard)

    async def _play_video_timeline(
        self,
        frames: list[tuple[int, bytes]],
        duration: float,
        audio_id: Optional[str] = None,
    ):
        """按句子提交顺序串行视频时间线，与浏览器串行音频队列保持同序。"""
        async with self._video_timeline_lock:
            await self._play_video_timeline_serial(frames, duration, audio_id=audio_id)

    async def _play_video_timeline_serial(
        self,
        frames: list[tuple[int, bytes]],
        duration: float,
        audio_id: Optional[str] = None,
    ):
        """按原始 PTS 选择当前最新帧；仅在没有可用共享 cursor 时使用 monotonic fallback。"""
        ordered_frames = sorted(frames, key=lambda item: item[0])
        timeline = [(max(0, pts), jpeg) for pts, jpeg in ordered_frames]
        fallback_started = time.monotonic()
        next_index = 0
        published_index = -1
        current_task = asyncio.current_task()
        using_fallback = not audio_id
        if using_fallback:
            logger.debug("RemoteGPU 视频无 audio_id，使用 monotonic fallback 时间线")
        try:
            while next_index < len(timeline):
                clock = None
                if audio_id and not using_fallback:
                    from server.core.media.virtual_audio import global_virtual_audio
                    clock = global_virtual_audio.get_playback_clock(audio_id)
                    reject_reason = clock.get("reject_reason") if clock else None
                    browser_fallback = reject_reason in {
                        "service_disabled",
                        "audio_unavailable",
                        "queue_full",
                        "stream_unavailable",
                        "decode_empty",
                        "playback_error",
                    }
                    if clock and (clock.get("is_interrupted") or clock.get("is_rejected")):
                        if not browser_fallback:
                            return
                        using_fallback = True
                        inherited_elapsed = max(0.0, float(clock.get("elapsed_sec", 0.0) or 0.0))
                        fallback_started = time.monotonic() - inherited_elapsed
                        clock = None
                        logger.debug(
                            "RemoteGPU 本地播放不可用 (%s)，audio_id=%s 改用浏览器 monotonic fallback",
                            reject_reason,
                            audio_id,
                        )
                    if clock is None and not using_fallback:
                        using_fallback = True
                        fallback_started = time.monotonic()
                        logger.debug(
                            "RemoteGPU 找不到 audio_id=%s 播放时钟，使用 monotonic fallback",
                            audio_id,
                        )
                    elif not clock.get("has_started"):
                        await asyncio.sleep(0.01)
                        continue
                if clock is not None:
                    elapsed_ms = max(0.0, float(clock.get("elapsed_sec", 0.0) or 0.0) * 1000.0)
                else:
                    elapsed_ms = max(0.0, (time.monotonic() - fallback_started) * 1000.0)

                latest_due = published_index
                while next_index < len(timeline) and timeline[next_index][0] <= elapsed_ms:
                    latest_due = next_index
                    next_index += 1
                if latest_due > published_index:
                    # 落后时直接跳到当前最新帧，不逐帧补播过期画面。
                    published_index = latest_due
                    try:
                        if clock and not using_fallback and clock.get("has_started"):
                            _audio_head_ms = (float(clock.get("elapsed_sec", 0.0) or 0.0)) * 1000.0
                            global_shared_playback_clock.report_audio_head(audio_id, _audio_head_ms)
                            _drift = global_shared_playback_clock.compute_drift()
                            global_av_sync.apply_drift(_drift, anchored=True)
                            # 发布前把采样时钟锚点传给视频侧，使两侧同源 (消除跨时钟域误差)
                            self._pending_video_pts_ms = _audio_head_ms
                        else:
                            global_av_sync.apply_drift(None, anchored=False)
                            self._pending_video_pts_ms = None
                    except Exception:
                        pass
                    await self._publish_video_frame(timeline[published_index][1])
                    continue
                if clock and clock.get("is_finished"):
                    return
                await asyncio.sleep(0.01)

            if audio_id and not using_fallback:
                while True:
                    from server.core.media.virtual_audio import global_virtual_audio
                    clock = global_virtual_audio.get_playback_clock(audio_id)
                    if not clock or clock.get("is_finished") or clock.get("is_interrupted") or clock.get("is_rejected"):
                        break
                    await asyncio.sleep(0.01)
            else:
                remaining = fallback_started + duration - time.monotonic()
                if remaining > 0:
                    await asyncio.sleep(remaining)
        except asyncio.CancelledError:
            raise
        finally:
            # 旧 task 不得清掉后续句子已经发布的新帧。
            if self._video_task is current_task:
                self.latest_jpeg = b""

    async def _cancel_video_playback(self, clear_frame: bool, clear_pending: bool = True):
        tasks = list(self._video_tasks)
        if self._video_task and self._video_task not in tasks:
            tasks.append(self._video_task)
        self._video_task = None
        self._video_tasks.clear()
        for task in tasks:
            if task and not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if clear_pending:
            self._pending_video_transactions.clear()
            self._last_completed_request_id = None
            self._pending_video_frames = []
        if clear_frame:
            self.latest_jpeg = b""
        try:
            global_shared_playback_clock.reset()
            global_av_sync.apply_drift(None, anchored=False)
        except Exception:
            pass

    async def _publish_video_frame(self, jpeg: bytes):
        if not jpeg:
            return
        self.latest_jpeg = jpeg
        self.frames_received += 1
        try:
            # 视频帧发布打 PTS 锚点；携带采样时钟换算值时与音频侧同源
            global_shared_playback_clock.stamp_video(self.frames_received, self._pending_video_pts_ms)
        except Exception:
            pass
        self._pending_video_pts_ms = None
        try:
            import cv2 as cv2
            import numpy as np
            from server.core.media.virtual_cam import global_virtual_cam

            if global_virtual_cam.is_active:
                arr = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
                if arr is not None:
                    global_virtual_cam.send_frame(
                        cv2.cvtColor(arr, cv2.COLOR_BGR2RGB),
                        owner="legacy_remote_gpu",
                        priority=50,
                    )
        except Exception:
            pass

    async def _handle_video_frame(self, b64_data: str):
        """兼容直接帧注入测试；正常合成路径会先暂存，再由时间线发布。"""
        await self._publish_video_frame(self._decode_video_frame(b64_data))

    @property
    def has_frames(self) -> bool:
        return bool(self.latest_jpeg)

    def get_latest_jpeg(self) -> bytes:
        return self.latest_jpeg

    def get_media_capabilities(self) -> dict:
        """声明远程节点当前仍使用整句事务协议，不虚报本地帧输入。"""
        from server.core.media.audio_frame import MediaCapabilities
        from server.core.media.virtual_audio import global_virtual_audio

        audio_status = global_virtual_audio.get_status()
        return MediaCapabilities(
            accepts_audio_frames=False,
            tts_output_codec=str(getattr(self, "audio_codec", "mp3")),
            chunk_semantics="transactional_sentence",
            transactional_sentence=True,
            supports_cancel=True,
            supports_shared_clock=bool(audio_status.get("shared_playback_clock")),
            remote_protocol_version=self._protocol_version,
            extra={
                "remote_audio_envelope": "RGA2" if self._protocol_version >= 2 else "legacy_binary",
                "video_timeline": "remote_frame_pts",
            },
        ).to_dict()

    def get_capabilities(self) -> dict:
        from server.core.media.virtual_audio import global_virtual_audio

        audio_status = global_virtual_audio.get_status()
        return {
            "driver": "remote_gpu",
            "capabilities": {
                # v1/v2 节点耦合 TTS 且没有 v3 evidence/strict completion，不能证明神经口型。
                "neural_lipsync": False,
                "neural_lipsync_verified": False,
                "verification": "unverified_legacy_v1_v2",
                "viseme_lipsync": False,
                "g2p_aligned": False,
                "alignment_mode": "remote_frame_pts",
                "shared_playback_clock": bool(audio_status.get("shared_playback_clock")),
                "hardware_dac_clock": bool(audio_status.get("hardware_dac_clock")),
                "clock_source": audio_status.get("clock_source"),
                "clock_precision": audio_status.get("clock_precision", "none"),
                "expressions": False,
                "head_motion": False,
                "remote_rendering": True,
            },
        }

    def get_preview_status(self) -> dict:
        return {
            "fps": 25,
            "is_running": self.is_running,
            "is_speaking": self.is_speaking,
            "render_backend": "remote_gpu",
            "remote_connected": self.is_connected,
            "remote_protocol": self._protocol_version,
            "remote_frames": self.frames_received,
            "has_frame": self.has_frames,
            "request_id": self._current_request_id,
            "capabilities": self.get_capabilities()["capabilities"],
        }
