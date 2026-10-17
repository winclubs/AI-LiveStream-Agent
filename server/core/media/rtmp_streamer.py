"""
内置 RTMP 直推引擎服务 (RtmpStreamerService)
基于 FFmpeg 管道的多线程音画合流引擎，实现脱离外部 OBS 的一键推流能力。
支持抖音、快手、B站、视频号、淘宝等任意支持 RTMP 协议的主流直播平台。
"""

import logging
import os
import queue
from collections import deque
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from server.core.media.dsp import resample_s16le as _resample_s16le

try:
    import numpy as np
    NUMPY_AVAILABLE = True
except ImportError:  # pragma: no cover
    np = None
    NUMPY_AVAILABLE = False

logger = logging.getLogger("LiveAgent.RtmpStreamer")


def find_ffmpeg_binary() -> Optional[str]:
    """探测系统或内置便携目录中的 ffmpeg 可执行文件路径"""
    # 1. 优先读取自定义环境变量
    custom = os.getenv("LIVE_AGENT_FFMPEG_PATH")
    if custom and os.path.isfile(custom):
        return custom

    # 2. 探测本地桌面端内置与项目相对便携目录
    from server.config import ROOT_DIR, DATA_DIR
    exe_name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    candidates = [
        ROOT_DIR / "apps" / "desktop-ui" / "resources" / "ffmpeg" / "bin" / exe_name,
        ROOT_DIR / "resources" / "ffmpeg" / "bin" / exe_name,
        DATA_DIR / "bin" / exe_name,
        ROOT_DIR / "bin" / exe_name,
        ROOT_DIR / exe_name,
        Path("C:/ffmpeg/bin/ffmpeg.exe"),
    ]
    for cand in candidates:
        if cand.exists() and cand.is_file():
            return cand.as_posix()

    # 3. 回退系统全局 PATH
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
        # 与数字人竖屏渲染器保持一致；路由层仍可显式覆盖输出规格。
        self.width = 720
        self.height = 960
        self.fps = 25
        self.bitrate_kbps = 2500
        self.audio_sample_rate = 16000
        self.audio_channels = 1
        # H.264 profile 钉死为 high，兼容主流 CDN；如需 baseline 可通过环境变量覆盖
        self.h264_profile = os.getenv("LIVE_AGENT_RTMP_H264_PROFILE", "high").lower()

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

        # 断线重连看门狗 (P0-2：支持无限重连与主动健康探测)
        self._reconnect_thread: Optional[threading.Thread] = None
        self._reconnect_attempts: int = 0
        self._reconnect_success_total: int = 0
        self._max_reconnect_attempts = int(os.getenv("LIVE_AGENT_RTMP_MAX_RECONNECTS", "10"))
        self._reconnect_base_delay = float(os.getenv("LIVE_AGENT_RTMP_RECONNECT_BASE_DELAY", "1.0"))
        self._reconnect_max_delay = float(os.getenv("LIVE_AGENT_RTMP_RECONNECT_MAX_DELAY", "15.0"))
        self._last_reconnect_error: str = ""

        # 异步推流有界队列与反压防护 (P2-13)
        self._video_queue: "queue.Queue[Optional[bytes]]" = queue.Queue(maxsize=30)
        self._video_worker_thread: Optional[threading.Thread] = None
        self._dropped_frames_total: int = 0
        # 音频前置延迟补偿缓冲队列 (将 delay_ms 延伸至 RTMP 音频轨)
        self._audio_delay_queue: deque = deque(maxlen=200)

        # 主动流健康看门狗 (P0-2)
        self._watchdog_thread: Optional[threading.Thread] = None
        self._last_frame_sent_time: float = 0.0

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
            is_unlimited = (self._max_reconnect_attempts <= 0)
            return {
                "is_streaming": running and (current_state == self.STATE_STREAMING),
                "state": current_state,
                "target_url": self._mask_stream_url(self.rtmp_url, self.stream_key),
                "width": self.width,
                "height": self.height,
                "fps": self.fps,
                "bitrate_kbps": self.bitrate_kbps,
                "h264_profile": self.h264_profile,
                "frames_sent": self._frames_sent,
                "audio_bytes_sent": self._audio_bytes_sent,
                "dropped_frames_total": self._dropped_frames_total,
                "duration_seconds": round(duration, 1),
                "last_error": self._last_error,
                "reconnect_attempts": self._reconnect_attempts,
                "reconnect_success_total": self._reconnect_success_total,
                "last_reconnect_error": self._last_reconnect_error,
                "unlimited_reconnect": is_unlimited,
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
        width: int = 720,
        height: int = 960,
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
            self._reconnect_attempts = 0
            self._last_reconnect_error = ""
            self._stop_event.clear()

            ok, msg = self._spawn_process()
            if not ok:
                return False, msg

            logger.info(f"RTMP 直推引擎已启动，目标: {self._mask_stream_url(self.rtmp_url, self.stream_key)}")
            return True, "推流引擎已成功启动"

    def _build_ffmpeg_cmd(self, full_destination: str, ffmpeg_bin: Optional[str] = None) -> list[str]:
        """组装跨平台极速低延迟推流命令行 (钉死 H.264 profile，显式 GOP)"""
        bin_path: str = ffmpeg_bin or find_ffmpeg_binary() or "ffmpeg"
        gop = int(self.fps * 2)  # 2秒 GOP 关键帧间隔
        cmd: list[str] = [
            bin_path,
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
            "-profile:v", self.h264_profile,
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
        return cmd

    def _spawn_process(self) -> Tuple[bool, str]:
        """ (重新) 创建 FFmpeg 子进程与音频握手线程；重连时复用同一推流参数。"""
        with self._lock:
            if not self.rtmp_url:
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
            if self.stream_key and not self.rtmp_url.endswith(self.stream_key):
                full_destination = f"{self.rtmp_url.rstrip('/')}/{self.stream_key}"
            else:
                full_destination = self.rtmp_url

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

            cmd = self._build_ffmpeg_cmd(full_destination, ffmpeg_bin=ffmpeg_bin)
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
                            self._last_frame_sent_time = time.time()
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

            # 启动独立推流工作队列线程与流健康看门狗线程 (P2-13, P0-2)
            self._last_frame_sent_time = time.time()
            self._video_worker_thread = threading.Thread(target=self._video_drain_loop, daemon=True)
            self._video_worker_thread.start()

            self._watchdog_thread = threading.Thread(target=self._flow_watchdog_loop, daemon=True)
            self._watchdog_thread.start()

            return True, "推流引擎已成功启动"

    def _video_drain_loop(self) -> None:
        """独立后台工作线程：从有界队列消费视频帧并写入 FFmpeg 标准输入管道，解耦主渲染循环 (P2-13)"""
        logger.info("RTMP 视频推流工作线程已启动")
        while not self._stop_event.is_set():
            try:
                frame_bytes = self._video_queue.get(timeout=0.2)
            except queue.Empty:
                continue

            if frame_bytes is None:
                break

            pipe = self._video_pipe
            if pipe is None:
                continue

            try:
                pipe.write(frame_bytes)
                pipe.flush()
                with self._lock:
                    self._frames_sent += 1
                    self._last_frame_sent_time = time.time()
            except (BrokenPipeError, OSError) as e:
                with self._lock:
                    self._last_error = f"视频管道写入失败: {e}"
                    self.state = self.STATE_ERROR
                logger.warning(self._last_error)
                self._schedule_reconnect()
                break
            except Exception as e:
                logger.error(f"视频管道写入发生意外错误: {e}")
                break
        logger.info("RTMP 视频推流工作线程已退出")

    def _flow_watchdog_loop(self) -> None:
        """主动流健康看门狗：监控 FFmpeg 存活与帧率下限，防止 0 FPS 假死 (P0-2)"""
        logger.info("RTMP 主动流健康看门狗已启动")
        while not self._stop_event.is_set():
            time.sleep(2.0)
            if self._stop_event.is_set():
                break

            with self._lock:
                if self.state != self.STATE_STREAMING:
                    continue
                proc = self._process
                last_sent = self._last_frame_sent_time
                start_t = self._start_time

            # 1. 检查子进程是否已非预期退出
            if proc is not None and proc.poll() is not None:
                err = ""
                if proc.stderr:
                    try:
                        err = proc.stderr.read().decode("utf-8", errors="ignore")
                    except Exception:
                        pass
                logger.warning(f"检测到 FFmpeg 子进程退出 (code={proc.returncode}): {err[:200]}，看门狗触发重连")
                self._schedule_reconnect()
                break

            # 2. 检查是否发生长达 8 秒未发帧 (推流假死 / 0 FPS)
            now = time.time()
            if start_t > 0 and (now - start_t > 6.0) and last_sent > 0:
                if (now - last_sent) > 8.0:
                    logger.warning("RTMP 超过 8 秒未写入视频帧 (推流假死)，看门狗主动触发重连自愈")
                    self._schedule_reconnect()
                    break
        logger.info("RTMP 主动流健康看门狗已退出")

    def send_video_frame(self, frame_rgb) -> bool:
        """向推流管道非阻塞投递一帧视频画面 (RGB24 格式)，支持反压自动丢帧与尺寸自适应校验 (P2-13, P1)"""
        if not self.is_streaming or self._video_pipe is None:
            return False

        if NUMPY_AVAILABLE and isinstance(frame_rgb, np.ndarray):
            # 尺寸一致性强制校验与自适应 resize，杜绝向 FFmpeg 输入不匹配步长字节流造成花屏
            if frame_rgb.shape[1] != self.width or frame_rgb.shape[0] != self.height:
                try:
                    import cv2
                    frame_rgb = cv2.resize(frame_rgb, (self.width, self.height), interpolation=cv2.INTER_LINEAR)
                except Exception:
                    return False
            raw_bytes = frame_rgb.tobytes()
        elif isinstance(frame_rgb, (bytes, bytearray)):
            if not frame_rgb:
                return False
            raw_bytes = bytes(frame_rgb)
        else:
            return False

        try:
            self._video_queue.put_nowait(raw_bytes)
            return True
        except queue.Full:
            # 队列满时丢弃最旧一帧并推入最新帧，保证低延时与主线程不卡死
            with self._lock:
                self._dropped_frames_total += 1
            try:
                _ = self._video_queue.get_nowait()
                self._video_queue.put_nowait(raw_bytes)
                return True
            except Exception:
                return False

    def send_audio_pcm(self, pcm_data: bytes, sample_rate: int = 0) -> bool:
        """向推流管道写入一段 PCM 音频数据 (单声道 16bit 小端)，带非阻塞超时保护防止卡死本地声卡

        修复前：FFmpeg 声明 -ar 16000 但送入的是 TTS 原生采样率 (24k/48k)，
        导致推流音调/速度错误。sample_rate 用于在采样率不一致时线性插值重采样。
        """
        if not self.is_streaming:
            return False

        sock = self._audio_client_socket
        if not sock:
            return False

        if not isinstance(pcm_data, (bytes, bytearray)) or not pcm_data:
            return False

        # 采样率不一致时重采样至 FFmpeg 声明的输入采样率
        if sample_rate and sample_rate > 0 and sample_rate != self.audio_sample_rate:
            pcm_data = _resample_s16le(bytes(pcm_data), sample_rate, self.audio_sample_rate)

        # 音频前置延迟补偿机制 (将 delay_ms 延伸至 RTMP 音频轨，消除公网推流音画不同步)
        packets_to_send = []
        delay_ms = 0
        try:
            from server.core.media.av_sync import global_av_sync
            delay_ms = int(getattr(global_av_sync, "recommended_delay_ms", 0) or 0)
        except Exception:
            delay_ms = 0

        now = time.monotonic()
        if delay_ms > 0:
            delay_sec = delay_ms / 1000.0
            self._audio_delay_queue.append((now, pcm_data))
            while self._audio_delay_queue:
                t_enq, queued_data = self._audio_delay_queue[0]
                if now - t_enq >= delay_sec:
                    self._audio_delay_queue.popleft()
                    packets_to_send.append(queued_data)
                else:
                    break
        else:
            # 零延迟直通模式：清空历史排队后直接直通
            while self._audio_delay_queue:
                packets_to_send.append(self._audio_delay_queue.popleft()[1])
            packets_to_send.append(pcm_data)

        if not packets_to_send:
            return True

        for chunk in packets_to_send:
            try:
                # 设置极小超时 (50ms)，一旦公网管道反压，绝不阻塞上游本地声卡回放线程
                sock.settimeout(0.05)
                sock.sendall(chunk)
                with self._lock:
                    self._audio_bytes_sent += len(chunk)
            except (socket.timeout, BlockingIOError):
                logger.warning("RTMP 音频套接字写入超时 (网络反压)，跳过本段 PCM 防上游声卡阻塞")
                return False
            except (BrokenPipeError, OSError) as e:
                with self._lock:
                    self._last_error = f"音频流写入失败: {e}"
                logger.warning(self._last_error)
                self._schedule_reconnect()
                return False
        return True

    def _schedule_reconnect(self) -> None:
        """安排一次指数退避重连；重复调用只保留一个待执行看门狗。支持无限重连 (P0-2)"""
        if self._stop_event.is_set():
            return
        with self._lock:
            if self._reconnect_thread and self._reconnect_thread.is_alive():
                return
            is_unlimited = (self._max_reconnect_attempts <= 0)
            if not is_unlimited and self._reconnect_attempts >= self._max_reconnect_attempts:
                self._last_reconnect_error = (
                    f"已达到最大重连次数 {self._max_reconnect_attempts}，停止自愈；"
                    "请检查推流地址/网络后重新调用 /rtmp/start"
                )
                logger.error(self._last_reconnect_error)
                return
            self._reconnect_thread = threading.Thread(target=self._reconnect_loop, daemon=True)
            self._reconnect_thread.start()

    def _notify_media_watchdog(self, event_type: str, details: Optional[dict] = None) -> None:
        """把断线/重连事件上报媒体质量看门狗。

        架构诚实：这是纯旁路遥测，任何异常都绝不影响推流与重连主链路。
        """
        try:
            from server.core.monitoring.media_quality_watchdog import global_media_watchdog
            global_media_watchdog.record_rtmp_event(event_type, details)
        except Exception:
            pass

    def _reconnect_loop(self) -> None:
        """指数退避重建 FFmpeg 推流管道 (1s -> 2s -> 4s ... 上限 15s)。支持无限重连 (P0-2)"""
        try:
            # 先收敛旧进程资源，避免端口/句柄残留
            self._teardown_process()
            while not self._stop_event.is_set():
                with self._lock:
                    is_unlimited = (self._max_reconnect_attempts <= 0)
                    if not is_unlimited and self._reconnect_attempts >= self._max_reconnect_attempts:
                        return
                    self._reconnect_attempts += 1
                    attempt = self._reconnect_attempts
                delay = min(
                    self._reconnect_base_delay * (2 ** (attempt - 1)),
                    self._reconnect_max_delay,
                )
                attempts_display = "无限" if is_unlimited else str(self._max_reconnect_attempts)
                logger.info(f"RTMP 断线重连 (第 {attempt}/{attempts_display} 次)，{delay:.1f}s 后重试...")
                self._notify_media_watchdog("disconnect", {"attempt": attempt, "delay_sec": round(delay, 2)})
                if self._stop_event.wait(timeout=delay):
                    return
                with self._lock:
                    self.state = self.STATE_STARTING
                ok, msg = self._spawn_process()
                if ok:
                    with self._lock:
                        self._reconnect_success_total += 1
                        self._last_reconnect_error = ""
                    logger.info(f"RTMP 断线重连成功 (第 {attempt} 次)，恢复推流")
                    self._notify_media_watchdog("reconnect", {"attempt": attempt})
                    return
                with self._lock:
                    self._last_reconnect_error = msg
                    self.state = self.STATE_ERROR
        except Exception as exc:
            with self._lock:
                self._last_reconnect_error = f"重连看门狗异常: {exc}"
                self.state = self.STATE_ERROR
            logger.exception("RTMP 重连看门狗异常")

    def _teardown_process(self) -> None:
        """收敛 FFmpeg 子进程与工作线程、套接字资源，保留推流参数供重连复用。"""
        # 1. 抽干旧队列中积压的未消费视频帧，确保能成功推入 None 退出哨兵
        while not self._video_queue.empty():
            try:
                self._video_queue.get_nowait()
            except Exception:
                break
        try:
            self._video_queue.put_nowait(None)
        except Exception:
            pass

        # 2. 等待旧视频消费工作线程平稳退出，避免与重连后的新线程并发争抢队列导致帧交错
        if self._video_worker_thread and self._video_worker_thread.is_alive():
            try:
                self._video_worker_thread.join(timeout=1.0)
            except Exception:
                pass
            self._video_worker_thread = None

        with self._lock:
            # 重置一个全新的干净队列，切断新老线程数据交叉
            self._video_queue = queue.Queue(maxsize=15)
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
        self._audio_delay_queue.clear()

    def stop(self) -> bool:
        """停止推流并安全释放所有进程与套接字资源"""
        self._stop_event.set()
        with self._lock:
            self.state = self.STATE_STOPPED
        # 等待重连看门狗退出，避免 stop 后又自动拉起推流
        if self._reconnect_thread and self._reconnect_thread.is_alive():
            self._reconnect_thread.join(timeout=2.0)
        self._teardown_process()
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
