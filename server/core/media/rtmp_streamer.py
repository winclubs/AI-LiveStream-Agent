"""
内置 RTMP 直推引擎服务 (RtmpStreamerService)
基于 FFmpeg 管道的多线程音画合流引擎，实现脱离外部 OBS 的一键推流能力。
支持抖音、快手、B站、视频号、淘宝等任意支持 RTMP 协议的主流直播平台。
"""

import asyncio
import logging
import os
import shutil
import socket
import subprocess
import threading
import time
from typing import Any, Dict, Optional, Tuple

try:
    import numpy as np
    NUMPY_AVAILABLE = True
except ImportError:  # pragma: no cover
    np = None
    NUMPY_AVAILABLE = False

logger = logging.getLogger("LiveAgent.RtmpStreamer")


def find_ffmpeg_binary() -> Optional[str]:
    """探测系统中的 ffmpeg 可执行文件路径"""
    custom = os.getenv("LIVE_AGENT_FFMPEG_PATH")
    if custom and os.path.isfile(custom):
        return custom
    return shutil.which("ffmpeg")


class RtmpStreamerService:
    """管理 FFmpeg 推流子进程与音视频数据喂入的单例服务"""

    STATE_STOPPED = "stopped"
    STATE_STARTING = "starting"
    STATE_STREAMING = "streaming"
    STATE_ERROR = "error"

    def __init__(self):
        self.state = self.STATE_STOPPED
        self.rtmp_url = ""
        self.stream_key = ""
        self.width = 1280
        self.height = 720
        self.fps = 25
        self.bitrate_kbps = 2500
        self.audio_sample_rate = 16000
        self.audio_channels = 1

        self._process: Optional[subprocess.Popen] = None
        self._video_pipe = None
        self._audio_server_socket: Optional[socket.socket] = None
        self._audio_client_socket: Optional[socket.socket] = None
        self._audio_port: int = 0

        self._lock = threading.RLock()
        self._start_time: float = 0.0
        self._frames_sent: int = 0
        self._audio_bytes_sent: int = 0
        self._last_error: str = ""
        self._audio_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    @property
    def is_streaming(self) -> bool:
        with self._lock:
            return self.state == self.STATE_STREAMING and self._process is not None and self._process.poll() is None

    def get_status(self) -> Dict[str, Any]:
        """获取当前推流运行状态与实时指标"""
        with self._lock:
            running = self._process is not None and self._process.poll() is None
            if self.state == self.STATE_ERROR:
                current_state = self.STATE_ERROR
            elif running:
                current_state = self.state
            else:
                current_state = self.STATE_STOPPED
            duration = (time.time() - self._start_time) if (running and self._start_time > 0) else 0.0
            return {
                "is_streaming": running and (current_state == self.STATE_STREAMING),
                "state": current_state,
                "target_url": self._mask_stream_url(self.rtmp_url, self.stream_key),
                "width": self.width,
                "height": self.height,
                "fps": self.fps,
                "bitrate_kbps": self.bitrate_kbps,
                "frames_sent": self._frames_sent,
                "audio_bytes_sent": self._audio_bytes_sent,
                "duration_seconds": round(duration, 1),
                "last_error": self._last_error,
            }

    @staticmethod
    def _mask_stream_url(url: str, key: str) -> str:
        if not url:
            return ""
        clean_url = url.rstrip("/")
        if not key:
            return clean_url
        masked_key = (key[:4] + "****" + key[-4:]) if len(key) > 8 else "****"
        return f"{clean_url}/{masked_key}"

    def start(
        self,
        rtmp_url: str,
        stream_key: str = "",
        width: int = 1280,
        height: int = 720,
        fps: int = 25,
        bitrate_kbps: int = 2500,
        audio_sample_rate: int = 16000,
    ) -> Tuple[bool, str]:
        """启动 RTMP 直推引擎"""
        with self._lock:
            if self.is_streaming:
                return True, "推流服务已在运行中"

            url = str(rtmp_url).strip()
            key = str(stream_key).strip()
            if not url:
                self._last_error = "推流地址不能为空"
                self.state = self.STATE_ERROR
                return False, self._last_error

            ffmpeg_bin = find_ffmpeg_binary()
            if not ffmpeg_bin:
                self._last_error = "未在系统中检测到 FFmpeg，请安装或设置 LIVE_AGENT_FFMPEG_PATH"
                self.state = self.STATE_ERROR
                logger.error(self._last_error)
                return False, self._last_error

            # 组装完整推流目的地址
            if key and not url.endswith(key):
                full_destination = f"{url.rstrip('/')}/{key}"
            else:
                full_destination = url

            self.rtmp_url = url
            self.stream_key = key
            self.width = max(320, width)
            self.height = max(240, height)
            self.fps = max(15, min(fps, 60))
            self.bitrate_kbps = max(500, bitrate_kbps)
            self.audio_sample_rate = audio_sample_rate
            self.state = self.STATE_STARTING
            self._frames_sent = 0
            self._audio_bytes_sent = 0
            self._last_error = ""
            self._stop_event.clear()

            # 建立本地临时 TCP 服务端，专门接收并分发 PCM 音频
            try:
                self._audio_server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self._audio_server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                self._audio_server_socket.bind(("127.0.0.1", 0))
                self._audio_server_socket.listen(1)
                self._audio_port = self._audio_server_socket.getsockname()[1]
            except Exception as e:
                self._last_error = f"无法初始化本地音频端口: {e}"
                self.state = self.STATE_ERROR
                logger.error(self._last_error)
                return False, self._last_error

            # 组装跨平台极速低延迟推流命令行
            gop = int(self.fps * 2)  # 2秒 GOP 关键帧间隔
            cmd = [
                ffmpeg_bin,
                "-y",
                "-hide_banner",
                "-loglevel", "error",
                # 视频输入 (从标准输入读取原始 RGB24 帧)
                "-f", "rawvideo",
                "-vcodec", "rawvideo",
                "-pix_fmt", "rgb24",
                "-s", f"{self.width}x{self.height}",
                "-r", str(self.fps),
                "-i", "-",
                # 音频输入 (从本地 TCP 连接读取原生 PCM 数据)
                "-f", "s16le",
                "-ac", str(self.audio_channels),
                "-ar", str(self.audio_sample_rate),
                "-i", f"tcp://127.0.0.1:{self._audio_port}",
                # 视频编码选项 (H.264，实时低延时)
                "-c:v", "libx264",
                "-preset", "veryfast",
                "-tune", "zerolatency",
                "-b:v", f"{self.bitrate_kbps}k",
                "-maxrate", f"{int(self.bitrate_kbps * 1.2)}k",
                "-bufsize", f"{self.bitrate_kbps * 2}k",
                "-pix_fmt", "yuv420p",
                "-g", str(gop),
                # 音频编码选项 (AAC)
                "-c:a", "aac",
                "-b:a", "128k",
                "-ar", str(self.audio_sample_rate),
                # 输出格式封装为 FLV 推流至 RTMP
                "-f", "flv",
                full_destination,
            ]

            try:
                self._process = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    bufsize=10 * 1024 * 1024,
                )
                self._video_pipe = self._process.stdin
            except Exception as e:
                self._last_error = f"启动 FFmpeg 进程失败: {e}"
                self.state = self.STATE_ERROR
                logger.error(self._last_error)
                self._cleanup_sockets()
                return False, self._last_error

            # 启动音频等待连接线程 (等待 FFmpeg 握手连接本地 TCP)
            def accept_audio_client():
                try:
                    self._audio_server_socket.settimeout(6.0)
                    client, _ = self._audio_server_socket.accept()
                    with self._lock:
                        self._audio_client_socket = client
                        if self.state == self.STATE_STARTING:
                            self.state = self.STATE_STREAMING
                            self._start_time = time.time()
                    logger.info("FFmpeg 音频管道建立成功，RTMP 直推开始传输")
                except Exception as ex:
                    if not self._stop_event.is_set():
                        logger.warning(f"FFmpeg 音频连接握手超时或异常: {ex}")

            self._audio_thread = threading.Thread(target=accept_audio_client, daemon=True)
            self._audio_thread.start()

            # 稍作等待以确认没有即刻启动报错
            time.sleep(0.1)
            if self._process.poll() is not None:
                err = self._process.stderr.read().decode("utf-8", errors="ignore") if self._process.stderr else ""
                self._last_error = f"FFmpeg 启动后立即退出 (code={self._process.returncode}): {err[:200]}"
                self.state = self.STATE_ERROR
                self._cleanup_sockets()
                return False, self._last_error

            logger.info(f"RTMP 直推引擎已启动，目标: {self._mask_stream_url(self.rtmp_url, self.stream_key)}")
            return True, "推流引擎已成功启动"

    def send_video_frame(self, frame_rgb) -> bool:
        """向推流管道写入一帧视频画面 (RGB24 格式)"""
        if not self.is_streaming or self._video_pipe is None:
            return False

        if NUMPY_AVAILABLE and isinstance(frame_rgb, np.ndarray):
            raw_bytes = frame_rgb.tobytes()
        elif isinstance(frame_rgb, (bytes, bytearray)):
            raw_bytes = bytes(frame_rgb)
        else:
            return False

        try:
            self._video_pipe.write(raw_bytes)
            self._video_pipe.flush()
            with self._lock:
                self._frames_sent += 1
            return True
        except (BrokenPipeError, OSError) as e:
            with self._lock:
                self._last_error = f"视频管道写入失败: {e}"
                self.state = self.STATE_ERROR
            logger.warning(self._last_error)
            return False

    def send_audio_pcm(self, pcm_data: bytes) -> bool:
        """向推流管道写入一段 PCM 音频数据 (单声道 16bit 小端)"""
        if not self.is_streaming:
            return False

        sock = self._audio_client_socket
        if not sock:
            return False

        if not isinstance(pcm_data, (bytes, bytearray)) or not pcm_data:
            return False

        try:
            sock.sendall(pcm_data)
            with self._lock:
                self._audio_bytes_sent += len(pcm_data)
            return True
        except (BrokenPipeError, OSError) as e:
            with self._lock:
                self._last_error = f"音频流写入失败: {e}"
            return False

    def stop(self) -> bool:
        """停止推流并安全释放所有进程与套接字资源"""
        self._stop_event.set()
        with self._lock:
            self.state = self.STATE_STOPPED
            if self._video_pipe:
                try:
                    self._video_pipe.close()
                except Exception:
                    pass
                self._video_pipe = None

            if self._process:
                try:
                    self._process.terminate()
                    self._process.wait(timeout=1.5)
                except Exception:
                    try:
                        self._process.kill()
                    except Exception:
                        pass
                self._process = None

            self._cleanup_sockets()
            logger.info("RTMP 直推引擎已停止")
            return True

    def _cleanup_sockets(self):
        if self._audio_client_socket:
            try:
                self._audio_client_socket.close()
            except Exception:
                pass
            self._audio_client_socket = None

        if self._audio_server_socket:
            try:
                self._audio_server_socket.close()
            except Exception:
                pass
            self._audio_server_socket = None


# 全局单例
global_rtmp_streamer = RtmpStreamerService()
