# -*- coding: utf-8 -*-
"""
短视频与带货切片一键录制导出系统 (VideoAudioRecorder & RecordManager)
阶段四核心模块：
1. 直播讲解切片一键启动/停止录制；
2. 捕获数字人主驱动音画帧流，输出无损 1080P/720P 标准 H.264+AAC MP4 视频；
3. 自动生成切片封面图 preview.jpg 与切片元数据；
4. 提供切片列表查询、状态轮询与安全下载删除管理。
"""
import io
import json
import logging
import os
import shutil
import subprocess
import time
import uuid
import wave
from pathlib import Path
from typing import Any, Dict, List, Optional

import cv2
import numpy as np

from server.config import DATA_DIR

logger = logging.getLogger("LiveAgent.Recorder")

RECORDINGS_DIR = DATA_DIR / "recordings"
RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
METADATA_FILE = RECORDINGS_DIR / "records_index.json"


class VideoAudioRecorder:
    """单个短视频带货切片录制会话"""

    def __init__(
        self,
        record_id: str,
        title: str = "未命名讲解切片",
        sku: str = "",
        width: int = 1280,
        height: int = 720,
        fps: int = 25,
    ):
        self.record_id = record_id
        self.title = title
        self.sku = sku
        self.width = width
        self.height = height
        self.fps = fps

        self.start_time: float = 0.0
        self.end_time: float = 0.0
        self.total_frames: int = 0
        self.is_recording: bool = False

        self.record_dir = RECORDINGS_DIR / self.record_id
        self.raw_video_path = self.record_dir / "temp_video.mp4"
        self.raw_audio_path = self.record_dir / "temp_audio.wav"
        self.final_mp4_path = self.record_dir / f"clip_{self.record_id}.mp4"
        self.preview_path = self.record_dir / "preview.jpg"

        self._video_writer: Optional[cv2.VideoWriter] = None
        self._audio_buffer = bytearray()

    def start(self) -> bool:
        """开始录制"""
        self.record_dir.mkdir(parents=True, exist_ok=True)
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        self._video_writer = cv2.VideoWriter(
            str(self.raw_video_path),
            fourcc,
            float(self.fps),
            (self.width, self.height),
        )
        if not self._video_writer.isOpened():
            logger.error(f"无法初始化视频写入器: {self.raw_video_path}")
            return False

        self.start_time = time.time()
        self.is_recording = True
        self.total_frames = 0
        self._audio_buffer.clear()
        logger.info(f"切片录制开始: #{self.record_id} 【{self.title}】({self.width}x{self.height} @ {self.fps}FPS)")
        return True

    def feed_frame(self, frame_bgr: np.ndarray):
        """写入一帧画面"""
        if not self.is_recording or self._video_writer is None or frame_bgr is None:
            return
        if frame_bgr.shape[1] != self.width or frame_bgr.shape[0] != self.height:
            frame_bgr = cv2.resize(frame_bgr, (self.width, self.height))

        # 保存第一帧作为封面预览图
        if self.total_frames == 0:
            cv2.imwrite(str(self.preview_path), frame_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 90])

        self._video_writer.write(frame_bgr)
        self.total_frames += 1

    def feed_audio(self, pcm_bytes: bytes):
        """写入音频流 (16kHz 16-bit 单声道 PCM)"""
        if not self.is_recording or not pcm_bytes:
            return
        self._audio_buffer.extend(pcm_bytes)

    def stop(self) -> Dict[str, Any]:
        """停止录制并封装最终 MP4 视频"""
        if not self.is_recording:
            return {}

        self.is_recording = False
        self.end_time = time.time()
        duration_sec = round(self.end_time - self.start_time, 2)

        if self._video_writer:
            self._video_writer.release()
            self._video_writer = None

        # 保存音频 wav
        has_audio = False
        if len(self._audio_buffer) > 0:
            with wave.open(str(self.raw_audio_path), "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(16000)
                wf.writeframes(bytes(self._audio_buffer))
            has_audio = True

        # 合并音视频并重编码为标准 H.264 + AAC
        ffmpeg_cmd = None
        if has_audio and self.raw_audio_path.exists():
            ffmpeg_cmd = [
                "ffmpeg", "-y",
                "-i", str(self.raw_video_path),
                "-i", str(self.raw_audio_path),
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast",
                "-c:a", "aac", "-b:a", "128k",
                str(self.final_mp4_path)
            ]
        else:
            ffmpeg_cmd = [
                "ffmpeg", "-y",
                "-i", str(self.raw_video_path),
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast",
                str(self.final_mp4_path)
            ]

        try:
            res = subprocess.run(ffmpeg_cmd, capture_output=True, timeout=30)
            if res.returncode != 0:
                logger.warning(f"FFmpeg 封装警告: {res.stderr.decode('utf-8', errors='ignore')}")
                # 若 ffmpeg 封装异常，直接将 temp_video 作为备选产物
                if self.raw_video_path.exists() and not self.final_mp4_path.exists():
                    shutil.copy(str(self.raw_video_path), str(self.final_mp4_path))
        except Exception as e:
            logger.warning(f"调用 FFmpeg 失败，使用基础无音频 MP4: {e}")
            if self.raw_video_path.exists() and not self.final_mp4_path.exists():
                shutil.copy(str(self.raw_video_path), str(self.final_mp4_path))

        # 清理临时中间文件
        try:
            if self.raw_video_path.exists():
                self.raw_video_path.unlink()
            if self.raw_audio_path.exists():
                self.raw_audio_path.unlink()
        except Exception:
            pass

        file_size = self.final_mp4_path.stat().st_size if self.final_mp4_path.exists() else 0
        logger.info(f"切片录制完成: {self.final_mp4_path.name} ({duration_sec}s, {file_size / 1024 / 1024:.2f}MB, {self.total_frames} 帧)")

        return {
            "record_id": self.record_id,
            "title": self.title,
            "sku": self.sku,
            "video_path": self.final_mp4_path.as_posix(),
            "preview_path": self.preview_path.as_posix() if self.preview_path.exists() else "",
            "duration_sec": duration_sec,
            "total_frames": self.total_frames,
            "file_size_bytes": file_size,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.start_time)),
        }


class RecordManager:
    """带货切片录制全局服务管理器"""

    def __init__(self):
        self.active_recorder: Optional[VideoAudioRecorder] = None
        self._records_cache: List[Dict[str, Any]] = []
        self._load_records_index()

    def _load_records_index(self):
        """加载切片元数据索引"""
        if METADATA_FILE.exists():
            try:
                data = json.loads(METADATA_FILE.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    self._records_cache = data
            except Exception:
                self._records_cache = []

    def _save_records_index(self):
        """保存切片元数据索引"""
        try:
            METADATA_FILE.write_text(json.dumps(self._records_cache, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            logger.error(f"保存录制索引失败: {e}")

    def is_recording(self) -> bool:
        """查询是否正在录制"""
        return self.active_recorder is not None and self.active_recorder.is_recording

    def get_status(self) -> Dict[str, Any]:
        """获取当前录制状态与指标"""
        if not self.is_recording() or not self.active_recorder:
            return {"is_recording": False, "duration_sec": 0, "frames": 0}
        rec = self.active_recorder
        dur = round(time.time() - rec.start_time, 1)
        return {
            "is_recording": True,
            "record_id": rec.record_id,
            "title": rec.title,
            "sku": rec.sku,
            "duration_sec": dur,
            "total_frames": rec.total_frames,
        }

    def start_recording(self, title: str = "商品讲解切片", sku: str = "", width: int = 1280, height: int = 720) -> Dict[str, Any]:
        """启动新录制"""
        if self.is_recording():
            return {"code": 1, "message": "已有正在进行的录制任务，请先停止当前录制"}

        rec_id = f"rec_{uuid.uuid4().hex[:8]}"
        recorder = VideoAudioRecorder(
            record_id=rec_id,
            title=title.strip() or "带货讲解切片",
            sku=sku.strip(),
            width=width,
            height=height,
            fps=25,
        )
        if not recorder.start():
            return {"code": 1, "message": "启动录制管道失败"}

        self.active_recorder = recorder
        return {"code": 0, "message": f"切片录制已开始: 【{recorder.title}】", "data": {"record_id": rec_id, "title": recorder.title}}

    def stop_recording(self) -> Dict[str, Any]:
        """停止录制"""
        if not self.is_recording() or not self.active_recorder:
            return {"code": 1, "message": "当前没有正在进行的录制"}

        info = self.active_recorder.stop()
        self.active_recorder = None

        if info and "record_id" in info:
            self._records_cache.insert(0, info)
            self._save_records_index()
            return {"code": 0, "message": f"切片【{info.get('title')}】录制完成并已封装 MP4", "data": info}

        return {"code": 1, "message": "录制封装异常"}

    def feed_frame(self, frame_bgr: np.ndarray):
        """分发视频帧到当前活跃录制器"""
        if self.is_recording() and self.active_recorder:
            self.active_recorder.feed_frame(frame_bgr)

    def feed_audio(self, pcm_bytes: bytes):
        """分发音频帧到当前活跃录制器"""
        if self.is_recording() and self.active_recorder:
            self.active_recorder.feed_audio(pcm_bytes)

    def list_recordings(self) -> List[Dict[str, Any]]:
        """获取所有已录制切片列表"""
        # 过滤已不存在的文件
        valid = []
        for r in self._records_cache:
            vp = Path(r.get("video_path", ""))
            if vp.exists():
                valid.append(r)
        self._records_cache = valid
        return self._records_cache

    def delete_recording(self, record_id: str) -> bool:
        """删除指定的切片文件与目录"""
        target_dir = RECORDINGS_DIR / record_id
        if target_dir.exists():
            try:
                shutil.rmtree(target_dir, ignore_errors=True)
            except Exception:
                pass
        self._records_cache = [r for r in self._records_cache if r.get("record_id") != record_id]
        self._save_records_index()
        return True


_global_record_manager: Optional[RecordManager] = None


def get_record_manager() -> RecordManager:
    """获取全局切片录制服务单例"""
    global _global_record_manager
    if _global_record_manager is None:
        _global_record_manager = RecordManager()
    return _global_record_manager
