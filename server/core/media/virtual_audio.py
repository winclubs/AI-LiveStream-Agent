"""
本地虚拟声卡 / 物理音频设备输出服务 (规划 §7.1 / §7.2)
基于 sounddevice OutputStream + 播放队列：将 TTS 生成的音频切片按顺序连续写入
本地物理声卡或虚拟声卡 (如 CABLE Input (VB-Audio Virtual Cable))，
实现与 OBS 虚拟音频输入的硬件级直连。切片在单一播放线程内串行完整播出，
后句绝不截断前句；stop() 立即清空待播队列并中止当前输出 (Barge-in 语义)。
若环境未安装 sounddevice 或无可用音频设备，自动平滑软降级，
绝不影响现有前端 WebSocket 音频播放。
"""
import logging
import queue
import threading
import time
import sys
from typing import List, Dict, Any, Optional

from server.core.media.audio_decode import decode_audio_to_float32
from server.core.media.audio_frame import validate_audio_frame_batch

logger = logging.getLogger("LiveAgent.VirtualAudio")

try:
    import sounddevice as sd
    SD_AVAILABLE = True
except Exception:  # pragma: no cover
    sd = None
    SD_AVAILABLE = False

# 待播队列同时限制事务数和 PCM/容器总字节，避免压缩炸弹或长句堆积耗尽内存。
MAX_PENDING_CHUNKS = 64
MAX_AUDIO_TRANSACTION_BYTES = 32 * 1024 * 1024
MAX_PENDING_AUDIO_BYTES = 64 * 1024 * 1024


class VirtualAudioService:
    # 工作线程写流的细粒度 (约 50ms@24kHz)，打断中止延迟的上限
    WRITE_SLICE_SAMPLES = 1200

    def __init__(self):
        self.device_index: Optional[int] = None
        self.is_enabled: bool = True
        self.last_error: str = ""
        self.total_chunks_played: int = 0
        self.dropped_chunks: int = 0
        self.frame_batches_submitted: int = 0
        self.frames_submitted: int = 0
        self.frame_pcm_bytes_submitted: int = 0
        self._queue: "queue.Queue[tuple]" = queue.Queue(maxsize=MAX_PENDING_CHUNKS)
        self._pending_bytes = 0
        self._pending_bytes_lock = threading.Lock()
        self._stream = None
        self._stream_sr: Optional[int] = None
        self._stream_gen_used: int = -1
        # 流代际计数：stop()/set_device() 递增即宣告当前流失效，
        # 由播放工作线程在安全点自行 abort/close——严禁跨线程操作 PortAudio 流 (原生崩溃)
        self._stream_gen: int = 0
        # 外部音频代际 fence。锁同时保护 accepted audio/session generation，
        # 使 stop 与迟到 producer/worker 的检查在不同线程间保持原子可见。
        self._generation_lock = threading.Lock()
        self._accepted_audio_generation: int = 0
        self._accepted_session_generation: int = 0
        self._stream_lock = threading.Lock()
        self._worker: Optional[threading.Thread] = None
        self._worker_lock = threading.Lock()
        self._stream_refresh_event = threading.Event()
        self._shutdown_event = threading.Event()

        # 声卡物理 DAC 播放时钟游标跟踪 (供口型与外部驱动绝对对齐)
        self._cursor_lock = threading.Lock()
        self._cursors: Dict[str, dict] = {}

    @property
    def available(self) -> bool:
        return bool(SD_AVAILABLE)

    def list_devices(self) -> List[Dict[str, Any]]:
        """枚举系统中所有支持音频输出的设备 (物理扬声器、耳机、VB-Cable 等)"""
        if not SD_AVAILABLE or sd is None:
            return []
        devices = []
        try:
            default_out = None
            try:
                default_device = sd.default.device
                if isinstance(default_device, (list, tuple)) and len(default_device) >= 2:
                    default_out = default_device[1]
                elif isinstance(default_device, int):
                    default_out = default_device
            except Exception:
                pass

            all_devs = sd.query_devices()
            for idx, d in enumerate(all_devs):
                max_out = d.get("max_output_channels", 0)
                if max_out > 0:
                    name = str(d.get("name", f"Device {idx}"))
                    is_cable = "cable" in name.lower() or "virtual" in name.lower()
                    devices.append({
                        "index": idx,
                        "name": name,
                        "max_output_channels": max_out,
                        "default_samplerate": int(d.get("default_samplerate", 44100)),
                        "is_default": (idx == default_out),
                        "is_virtual_cable": is_cable
                    })
        except Exception as e:
            logger.warning(f"枚举音频输出设备异常: {e}")
            self.last_error = str(e)
        return devices

    def set_device(self, device_index: Optional[int]):
        """设置当前输出设备索引，None 表示使用系统默认输出 (由工作线程在安全点重建流)"""
        self.device_index = int(device_index) if device_index is not None else None
        self._stream_gen += 1
        self._stream_refresh_event.set()
        logger.info(f"已设置音频输出设备索引: {self.device_index}")

    def get_status(self) -> Dict[str, Any]:
        """获取输出服务状态及可供媒体驱动使用的真实时钟能力。"""
        active_device_name = "系统默认设备"
        if SD_AVAILABLE and sd is not None and self.device_index is not None:
            try:
                d = sd.query_devices(self.device_index)
                active_device_name = d.get("name", f"设备 {self.device_index}")
            except Exception:
                active_device_name = f"未知设备({self.device_index})"

        shared_clock = bool(self.is_enabled and self.available and sd is not None)
        return {
            "available": self.available,
            "is_enabled": self.is_enabled,
            "device_index": self.device_index,
            "device_name": active_device_name,
            "total_chunks_played": self.total_chunks_played,
            "dropped_chunks": self.dropped_chunks,
            "pending_chunks": self._queue.qsize(),
            "pending_audio_bytes": self._pending_bytes,
            "max_pending_audio_bytes": MAX_PENDING_AUDIO_BYTES,
            "max_audio_transaction_bytes": MAX_AUDIO_TRANSACTION_BYTES,
            "frame_batches_submitted": self.frame_batches_submitted,
            "frames_submitted": self.frames_submitted,
            "frame_pcm_bytes_submitted": self.frame_pcm_bytes_submitted,
            # 第一阶段按批次入队，worker 内仍以约 50ms 切片写声卡；不得虚报为 TTS 真流式。
            "audio_frame_input": True,
            "frame_batch_mode": "coalesced_transaction",
            "last_error": self.last_error,
            "shared_playback_clock": shared_clock,
            # blocking OutputStream 没有 callback outputBufferDacTime，不能宣称硬件 DAC 时钟。
            "hardware_dac_clock": False,
            "clock_source": "portaudio_latency_estimate" if shared_clock else None,
            "clock_precision": "estimated" if shared_clock else "none",
        }

    def _reject_cursor(self, audio_id: Optional[str], reason: str) -> None:
        if not audio_id:
            return
        with self._cursor_lock:
            cursor = self._cursors.get(audio_id)
            if cursor is not None:
                cursor["status"] = "rejected"
                cursor["is_rejected"] = True
                cursor["is_interrupted"] = True
                cursor["reject_reason"] = reason
                cursor["last_update_at"] = time.monotonic()

    def prepare_playback(
        self,
        audio_id: str,
        fallback_sample_rate: int = 24000,
        *,
        audio_generation: Optional[int] = None,
        session_generation: Optional[int] = None,
    ) -> bool:
        """原子预注册播放游标但不入队，使口型可观察明确的 prepared 状态。"""
        if not audio_id:
            return False
        now = time.monotonic()
        # 与 stop 保持 generation -> cursor 的统一锁序，避免 fence 推进后插入孤儿 prepared cursor。
        with self._generation_lock:
            generation_current = (
                (audio_generation is None or audio_generation >= self._accepted_audio_generation)
                and (session_generation is None or session_generation >= self._accepted_session_generation)
            )
            with self._cursor_lock:
                existing = self._cursors.get(audio_id)
                if existing is not None:
                    return not existing.get("is_rejected", False)
                if len(self._cursors) > 128:
                    finished = [
                        key
                        for key, value in self._cursors.items()
                        if value.get("is_finished") or value.get("is_interrupted")
                    ]
                    for old_key in finished[:64]:
                        self._cursors.pop(old_key, None)
                self._cursors[audio_id] = {
                    "audio_id": audio_id,
                    "total_samples": 0,
                    "submitted_samples": 0,
                    "samples_played": 0,
                    "sample_rate": int(fallback_sample_rate),
                    "started_at": None,
                    "last_update_at": now,
                    "playback_anchor_at": None,
                    "playback_anchor_samples": 0,
                    "latency_sec": 0.0,
                    "status": "prepared" if generation_current else "rejected",
                    "is_finished": False,
                    "is_interrupted": not generation_current,
                    "is_rejected": not generation_current,
                    "reject_reason": None if generation_current else "stale_generation",
                    "clock_source": "host_write_estimate",
                    "precision": "estimated",
                }
        return generation_current

    # 兼容采用 register 命名的调用方。
    register_playback = prepare_playback

    def reject_playback(self, audio_id: str, reason: str = "commit_aborted") -> None:
        """终止尚未成功提交的预备事务；已播放/已终止 cursor 不被覆盖。"""
        with self._cursor_lock:
            cursor = self._cursors.get(audio_id)
            if cursor is None or cursor.get("status") not in {"prepared", "queued"}:
                return
            cursor["status"] = "rejected"
            cursor["is_rejected"] = True
            cursor["is_interrupted"] = True
            cursor["reject_reason"] = reason
            cursor["last_update_at"] = time.monotonic()

    def _reserve_pending_bytes(self, size: int) -> bool:
        if size <= 0 or size > MAX_AUDIO_TRANSACTION_BYTES:
            return False
        with self._pending_bytes_lock:
            if self._pending_bytes + size > MAX_PENDING_AUDIO_BYTES:
                return False
            self._pending_bytes += size
        return True

    def _release_pending_packet(self, packet: tuple) -> None:
        size = len(packet[0]) if packet and isinstance(packet[0], bytes) else 0
        with self._pending_bytes_lock:
            self._pending_bytes = max(0, self._pending_bytes - size)

    def play_chunk(
        self,
        audio_bytes: bytes,
        fallback_sample_rate: int = 24000,
        *,
        codec: str = "pcm_s16le",
        channels: int = 1,
        audio_generation: Optional[int] = None,
        session_generation: Optional[int] = None,
        allow_raw_pcm: bool = False,
        audio_id: Optional[str] = None,
    ) -> bool:
        """提交已准备的游标并非阻塞入队；现有 cursor 只更新，绝不覆盖。"""
        if audio_id and audio_id not in self._cursors:
            self.prepare_playback(
                audio_id,
                fallback_sample_rate,
                audio_generation=audio_generation,
                session_generation=session_generation,
            )
        if not self.is_enabled or not self.available or sd is None or not audio_bytes:
            self._reject_cursor(audio_id, "service_disabled" if not self.is_enabled else "audio_unavailable")
            return False
        with self._generation_lock:
            if audio_generation is not None:
                if audio_generation < self._accepted_audio_generation:
                    self.dropped_chunks += 1
                    self._reject_cursor(audio_id, "stale_audio_generation")
                    return False
                self._accepted_audio_generation = max(self._accepted_audio_generation, int(audio_generation))
            if session_generation is not None:
                if session_generation < self._accepted_session_generation:
                    self.dropped_chunks += 1
                    self._reject_cursor(audio_id, "stale_session_generation")
                    return False
                self._accepted_session_generation = max(self._accepted_session_generation, int(session_generation))

        if audio_id:
            with self._cursor_lock:
                cursor = self._cursors.get(audio_id)
                if cursor is None or cursor.get("is_rejected") or cursor.get("status") != "prepared":
                    return False
                cursor["status"] = "queued"
                cursor["sample_rate"] = int(fallback_sample_rate)
                cursor["last_update_at"] = time.monotonic()

        packet = (
            audio_bytes,
            int(fallback_sample_rate),
            codec,
            int(channels or 1),
            audio_generation,
            session_generation,
            bool(allow_raw_pcm),
            audio_id,
        )
        if not self._reserve_pending_bytes(len(audio_bytes)):
            self.dropped_chunks += 1
            self._reject_cursor(audio_id, "audio_capacity_exceeded")
            return False
        try:
            self._queue.put_nowait(packet)
        except queue.Full:
            self._release_pending_packet(packet)
            self.dropped_chunks += 1
            self._reject_cursor(audio_id, "queue_full")
            return False
        self._ensure_worker()
        return True

    def play_frames(self, frames) -> bool:
        """提交标准 PCM 帧批次；兼容阶段合并为一个有界播放事务。

        PortAudio worker 仍以 ``WRITE_SLICE_SAMPLES`` 细粒度写入。合并只发生在
        已受 AudioFramePipeline 时长上限保护的单句内，避免改变现有 cursor 和
        打断线程所有权语义。
        """
        try:
            frame_list = validate_audio_frame_batch(
                frames,
                max_total_bytes=MAX_AUDIO_TRANSACTION_BYTES,
            )
        except ValueError:
            candidate_frames = tuple(frames or ())
            if candidate_frames:
                self._reject_cursor(candidate_frames[0].audio_id, "invalid_frame_batch")
            return False
        first = frame_list[0]
        accepted = self.play_chunk(
            b"".join(frame.data for frame in frame_list),
            fallback_sample_rate=first.format.sample_rate,
            codec=first.format.codec,
            channels=first.format.channels,
            audio_generation=first.audio_generation,
            session_generation=first.session_generation,
            allow_raw_pcm=True,
            audio_id=first.audio_id,
        )
        if accepted:
            self.frame_batches_submitted += 1
            self.frames_submitted += len(frame_list)
            self.frame_pcm_bytes_submitted += sum(len(frame.data) for frame in frame_list)
        return accepted

    def get_playback_clock(self, audio_id: str) -> Optional[Dict[str, Any]]:
        """返回动态估算的物理播放头；阻塞 write 仅代表提交，绝不虚报为 DAC 已播。"""
        with self._cursor_lock:
            cursor = self._cursors.get(audio_id)
            if not cursor:
                return None
            c = dict(cursor)
            sample_rate = int(c.get("sample_rate") or 0)
            submitted = int(c.get("submitted_samples", c.get("samples_played", 0)) or 0)
            played = int(c.get("samples_played", 0) or 0)
            anchor_at = c.get("playback_anchor_at")
            playback_failed = c.get("reject_reason") == "playback_error"
            if anchor_at is not None and sample_rate > 0 and (
                not c.get("is_interrupted") or playback_failed
            ):
                advance_until = float(c.get("last_update_at") or time.monotonic()) if playback_failed else time.monotonic()
                advanced = max(0.0, advance_until - float(anchor_at))
                played = min(submitted, int(c.get("playback_anchor_samples", 0) + advanced * sample_rate))
            # 保留旧测试/诊断直接设置 samples_played 的兼容语义。
            played = max(played, int(c.get("samples_played", 0) or 0))
            finished = bool(c.get("is_finished"))
            if c.get("status") == "draining" and played >= int(c.get("total_samples", 0) or 0):
                finished = True
                cursor["is_finished"] = True
                cursor["status"] = "finished"
                cursor["samples_played"] = played
            status = cursor.get("status", "prepared")
            return {
                "audio_id": audio_id,
                "has_started": bool(c.get("started_at") is not None and time.monotonic() >= float(c["started_at"])),
                "started_at": c.get("started_at"),
                "submitted_samples": submitted,
                "samples_played": played,
                "total_samples": int(c.get("total_samples", 0) or 0),
                "sample_rate": sample_rate,
                "elapsed_sec": float(played) / float(sample_rate) if sample_rate > 0 else 0.0,
                "latency_sec": float(c.get("latency_sec", 0.0) or 0.0),
                "clock_source": c.get("clock_source", "host_write_estimate"),
                "precision": c.get("precision", "estimated"),
                "status": status,
                "is_finished": finished,
                "is_interrupted": bool(c.get("is_interrupted")),
                "is_rejected": bool(c.get("is_rejected")),
                "reject_reason": c.get("reject_reason"),
            }

    def _packet_is_current(self, audio_generation, session_generation) -> bool:
        with self._generation_lock:
            return (
                (audio_generation is None or audio_generation >= self._accepted_audio_generation)
                and (session_generation is None or session_generation >= self._accepted_session_generation)
            )

    def _ensure_worker(self):
        with self._worker_lock:
            if self._worker is None or not self._worker.is_alive():
                self._shutdown_event.clear()
                self._stream_refresh_event.clear()
                self._worker = threading.Thread(
                    target=self._playback_loop,
                    name="VirtualAudioPlayback",
                    daemon=True
                )
                self._worker.start()

    def _playback_loop(self):
        """单一播放线程：串行写流，并响应空闲关闭与 shutdown 信号。"""
        if sys.platform == "win32":
            try:
                import ctypes
                ctypes.windll.kernel32.SetThreadPriority(ctypes.windll.kernel32.GetCurrentThread(), 2)
            except Exception:
                pass
        try:
            while not self._shutdown_event.is_set():
                if self._stream_refresh_event.is_set():
                    self._discard_stream_locked(abort=True)
                    self._stream_refresh_event.clear()
                try:
                    packet_item = self._queue.get(timeout=0.05)
                    if len(packet_item) == 8:
                        (
                            audio_bytes,
                            fallback_sr,
                            codec,
                            channels,
                            audio_generation,
                            session_generation,
                            allow_raw_pcm,
                            audio_id,
                        ) = packet_item
                    else:
                        (
                            audio_bytes,
                            fallback_sr,
                            codec,
                            channels,
                            audio_generation,
                            session_generation,
                            allow_raw_pcm,
                        ) = packet_item
                        audio_id = None
                except queue.Empty:
                    continue
                self._release_pending_packet(packet_item)
                try:
                    if not self._packet_is_current(audio_generation, session_generation):
                        self.dropped_chunks += 1
                        if audio_id:
                            with self._cursor_lock:
                                c = self._cursors.get(audio_id)
                                if c:
                                    c["is_interrupted"] = True
                                    c["status"] = "interrupted"
                        continue
                    samples, sr = decode_audio_to_float32(
                        audio_bytes,
                        fallback_sr,
                        codec=codec,
                        channels=channels,
                        allow_raw_pcm=allow_raw_pcm,
                    )
                    if samples is None or len(samples) == 0:
                        self._reject_cursor(audio_id, "decode_empty")
                        continue
                    if not self._packet_is_current(audio_generation, session_generation):
                        self.dropped_chunks += 1
                        if audio_id:
                            with self._cursor_lock:
                                c = self._cursors.get(audio_id)
                                if c:
                                    c["is_interrupted"] = True
                                    c["status"] = "interrupted"
                        continue
                    gen = self._stream_gen
                    stream = self._get_stream(sr, gen)
                    if stream is None:
                        try:
                            from server.core.media.rtmp_streamer import global_rtmp_streamer
                            if global_rtmp_streamer.is_streaming:
                                int16_bytes = (samples.clip(-1.0, 1.0) * 32767.0).astype("int16").tobytes()
                                global_rtmp_streamer.send_audio_pcm(int16_bytes)
                                time.sleep(len(samples) / float(sr))
                        except Exception:
                            pass
                        self.dropped_chunks += 1
                        self._reject_cursor(audio_id, "stream_unavailable")
                        continue

                    # blocking write 只表示提交给 PortAudio。以其输出 latency 建立诚实的估算播放头，
                    # 直到预计排空前保持 draining；callback DAC time 不可得，因此不宣称硬件精度。
                    try:
                        raw_latency = getattr(stream, "latency", 0.0)
                        if isinstance(raw_latency, (tuple, list)):
                            raw_latency = raw_latency[-1] if raw_latency else 0.0
                        output_latency = max(0.0, float(raw_latency or 0.0))
                    except (TypeError, ValueError):
                        output_latency = 0.0
                    if audio_id:
                        now = time.monotonic()
                        with self._cursor_lock:
                            c = self._cursors.get(audio_id)
                            if c:
                                c["total_samples"] = len(samples)
                                c["sample_rate"] = sr
                                c["started_at"] = now + output_latency
                                c["last_update_at"] = now
                                c["playback_anchor_at"] = now + output_latency
                                c["playback_anchor_samples"] = 0
                                c["latency_sec"] = output_latency
                                c["clock_source"] = (
                                    "portaudio_latency_estimate" if output_latency > 0.0 else "host_write_estimate"
                                )
                                c["precision"] = "estimated"
                                c["status"] = "playing"

                    for start in range(0, len(samples), self.WRITE_SLICE_SAMPLES):
                        if (
                            gen != self._stream_gen
                            or self._shutdown_event.is_set()
                            or not self._packet_is_current(audio_generation, session_generation)
                        ):
                            if audio_id:
                                with self._cursor_lock:
                                    c = self._cursors.get(audio_id)
                                    if c:
                                        c["is_interrupted"] = True
                                    c["status"] = "interrupted"
                            break
                        piece = samples[start:start + self.WRITE_SLICE_SAMPLES]
                        stream.write(piece.reshape(-1, 1))
                        try:
                            from server.core.media.rtmp_streamer import global_rtmp_streamer
                            if global_rtmp_streamer.is_streaming:
                                int16_bytes = (piece.clip(-1.0, 1.0) * 32767.0).astype("int16").tobytes()
                                global_rtmp_streamer.send_audio_pcm(int16_bytes)
                        except Exception:
                            pass
                        if audio_id:
                            with self._cursor_lock:
                                c = self._cursors.get(audio_id)
                                if c:
                                    c["submitted_samples"] += len(piece)
                                    c["last_update_at"] = time.monotonic()

                    packet_current = self._packet_is_current(audio_generation, session_generation)
                    if gen != self._stream_gen or self._shutdown_event.is_set() or not packet_current:
                        self._discard_stream_locked(abort=True)
                        if not packet_current:
                            self.dropped_chunks += 1
                        if audio_id:
                            with self._cursor_lock:
                                c = self._cursors.get(audio_id)
                                if c:
                                    c["is_interrupted"] = True
                                    c["status"] = "interrupted"
                    else:
                        self.total_chunks_played += 1
                        self.last_error = ""
                        if audio_id:
                            with self._cursor_lock:
                                c = self._cursors.get(audio_id)
                                if c:
                                    c["status"] = "draining"
                                    c["last_update_at"] = time.monotonic()
                except Exception as e:
                    self.last_error = str(e)
                    self.dropped_chunks += 1
                    if audio_id:
                        with self._cursor_lock:
                            c = self._cursors.get(audio_id)
                            if c:
                                c["is_interrupted"] = True
                                c["status"] = "interrupted"
                                c["reject_reason"] = "playback_error"
                                c["last_update_at"] = time.monotonic()
                    self._discard_stream_locked(abort=True)
                    logger.debug(f"物理音频设备播放片段失败 (软降级忽略): {e}")
                finally:
                    self._queue.task_done()
        finally:
            self._discard_stream_locked(abort=True)

    def _get_stream(self, sample_rate: int, gen: int):
        """惰性打开/复用输出流 (仅播放线程调用；代际不匹配或采样率变化时重建)"""
        with self._stream_lock:
            if (
                self._stream is not None
                and self._stream_sr == sample_rate
                and self._stream_gen_used == gen
                and getattr(self._stream, "active", True)
            ):
                return self._stream
            self._close_stream_locked()
            try:
                self._stream = sd.OutputStream(
                    samplerate=sample_rate,
                    channels=1,
                    dtype="float32",
                    device=self.device_index,
                )
                self._stream.start()
                self._stream_sr = sample_rate
                self._stream_gen_used = gen
                return self._stream
            except Exception as e:
                self.last_error = str(e)
                self._stream = None
                self._stream_sr = None
                return None

    def _close_stream_locked(self):
        if self._stream is not None:
            try:
                self._stream.close()
            except Exception:
                pass
            self._stream = None
            self._stream_sr = None
            self._stream_gen_used = -1

    def _discard_stream_locked(self, abort: bool = False):
        """在播放线程内安全地中止/关闭当前流 (严禁从其他线程调用)"""
        with self._stream_lock:
            if self._stream is not None:
                if abort:
                    try:
                        self._stream.abort()
                    except Exception:
                        pass
                self._close_stream_locked()

    def stop(self, next_generation: Optional[int] = None):
        """推进可选 audio generation fence，清队列并让 worker 自行关闭流。"""
        with self._generation_lock:
            if next_generation is not None:
                self._accepted_audio_generation = max(
                    self._accepted_audio_generation, int(next_generation)
                )
            self._stream_gen += 1
        while True:
            try:
                packet = self._queue.get_nowait()
                self._release_pending_packet(packet)
                self._queue.task_done()
            except queue.Empty:
                break
        with self._cursor_lock:
            for c in self._cursors.values():
                if not c.get("is_finished"):
                    c["is_interrupted"] = True
                    c["status"] = "interrupted"
                    c["reject_reason"] = "stopped"
                    c["last_update_at"] = time.monotonic()
        self._stream_refresh_event.set()

    def shutdown(self, timeout: float = 2.0):
        """终止并等待 worker；后续 play_chunk 可创建全新 worker。"""
        self.stop()
        self._shutdown_event.set()
        worker = self._worker
        if worker and worker.is_alive() and worker is not threading.current_thread():
            worker.join(timeout=timeout)
        with self._worker_lock:
            if self._worker is worker and (worker is None or not worker.is_alive()):
                self._worker = None


# 全局物理/虚拟音频输出单例
global_virtual_audio = VirtualAudioService()
