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
from typing import List, Dict, Any, Optional

from server.core.media.audio_decode import decode_audio_to_float32

logger = logging.getLogger("LiveAgent.VirtualAudio")

try:
    import sounddevice as sd
    SD_AVAILABLE = True
except Exception:  # pragma: no cover
    sd = None
    SD_AVAILABLE = False

# 待播队列上限：超出时丢弃最新切片，防止打断失效后旧音频堆积
MAX_PENDING_CHUNKS = 64


class VirtualAudioService:
    # 工作线程写流的细粒度 (约 50ms@24kHz)，打断中止延迟的上限
    WRITE_SLICE_SAMPLES = 1200

    def __init__(self):
        self.device_index: Optional[int] = None
        self.is_enabled: bool = True
        self.last_error: str = ""
        self.total_chunks_played: int = 0
        self.dropped_chunks: int = 0
        self._queue: "queue.Queue[tuple]" = queue.Queue(maxsize=MAX_PENDING_CHUNKS)
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
        """获取当前虚拟声卡/物理音频服务运行状态"""
        active_device_name = "系统默认设备"
        if SD_AVAILABLE and sd is not None and self.device_index is not None:
            try:
                d = sd.query_devices(self.device_index)
                active_device_name = d.get("name", f"设备 {self.device_index}")
            except Exception:
                active_device_name = f"未知设备({self.device_index})"

        return {
            "available": self.available,
            "is_enabled": self.is_enabled,
            "device_index": self.device_index,
            "device_name": active_device_name,
            "total_chunks_played": self.total_chunks_played,
            "dropped_chunks": self.dropped_chunks,
            "pending_chunks": self._queue.qsize(),
            "last_error": self.last_error
        }

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
    ):
        """非阻塞入队；每个 packet 都携带格式及 audio/session generation。"""
        if not self.is_enabled or not self.available or sd is None or not audio_bytes:
            return
        with self._generation_lock:
            if audio_generation is not None:
                if audio_generation < self._accepted_audio_generation:
                    self.dropped_chunks += 1
                    return
                self._accepted_audio_generation = max(
                    self._accepted_audio_generation, int(audio_generation)
                )
            if session_generation is not None:
                if session_generation < self._accepted_session_generation:
                    self.dropped_chunks += 1
                    return
                self._accepted_session_generation = max(
                    self._accepted_session_generation, int(session_generation)
                )
        self._ensure_worker()
        packet = (
            audio_bytes,
            int(fallback_sample_rate),
            codec,
            int(channels or 1),
            audio_generation,
            session_generation,
            bool(allow_raw_pcm),
        )
        try:
            self._queue.put_nowait(packet)
        except queue.Full:
            self.dropped_chunks += 1

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
        try:
            while not self._shutdown_event.is_set():
                if self._stream_refresh_event.is_set():
                    self._discard_stream_locked(abort=True)
                    self._stream_refresh_event.clear()
                try:
                    (
                        audio_bytes,
                        fallback_sr,
                        codec,
                        channels,
                        audio_generation,
                        session_generation,
                        allow_raw_pcm,
                    ) = self._queue.get(timeout=0.05)
                except queue.Empty:
                    continue
                try:
                    if not self._packet_is_current(audio_generation, session_generation):
                        self.dropped_chunks += 1
                        continue
                    samples, sr = decode_audio_to_float32(
                        audio_bytes,
                        fallback_sr,
                        codec=codec,
                        channels=channels,
                        allow_raw_pcm=allow_raw_pcm,
                    )
                    if samples is None or len(samples) == 0:
                        continue
                    if not self._packet_is_current(audio_generation, session_generation):
                        self.dropped_chunks += 1
                        continue
                    gen = self._stream_gen
                    stream = self._get_stream(sr, gen)
                    if stream is None:
                        self.dropped_chunks += 1
                        continue
                    for start in range(0, len(samples), self.WRITE_SLICE_SAMPLES):
                        if (
                            gen != self._stream_gen
                            or self._shutdown_event.is_set()
                            or not self._packet_is_current(audio_generation, session_generation)
                        ):
                            break
                        piece = samples[start:start + self.WRITE_SLICE_SAMPLES]
                        stream.write(piece.reshape(-1, 1))
                    packet_current = self._packet_is_current(audio_generation, session_generation)
                    if gen != self._stream_gen or self._shutdown_event.is_set() or not packet_current:
                        self._discard_stream_locked(abort=True)
                        if not packet_current:
                            self.dropped_chunks += 1
                    else:
                        self.total_chunks_played += 1
                        self.last_error = ""
                except Exception as e:
                    self.last_error = str(e)
                    self.dropped_chunks += 1
                    self._discard_stream_locked(abort=True)
                    logger.debug(f"物理音频设备播放片段失败 (软降级忽略): {e}")
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
                self._queue.get_nowait()
            except queue.Empty:
                break
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
