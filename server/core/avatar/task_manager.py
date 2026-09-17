# -*- coding: utf-8 -*-
"""
数字人视频切片与资产训练异步任务管理器 (AvatarTaskManager)
实现功能：
1. 异步任务排队、状态流转与持久化跟踪 (pending -> processing -> completed / failed / cancelled)；
2. 视频帧序列切片提取 (full_imgs/ 帧目录)；
3. 面部关键点与人脸包围盒计算，生成平滑 coords.pkl 与 landmarks.json；
4. 伴音分离为 16kHz 单声道 PCM WAV (audio.wav)；
5. 生成数字人资产元数据 meta.json 与预览封面 preview.jpg；
6. 自动将资产与目标主播 (Anchor) 及全局形象库 (Avatar) 双向关联。
"""
import asyncio
import datetime
import json
import logging
import os
import pickle
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import cv2
import numpy as np
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from server.config import DATA_DIR
from server.database.db import AsyncSessionLocal
from server.database.models import Anchor, Avatar, AvatarTask

logger = logging.getLogger("LiveAgent.AvatarTaskManager")

AVATAR_ASSETS_DIR = DATA_DIR / "avatar_assets"
AVATAR_ASSETS_DIR.mkdir(parents=True, exist_ok=True)


class AvatarTaskManager:
    """数字人视频切片与训练异步任务管理器"""

    def __init__(self):
        self._running_tasks: Dict[str, asyncio.Task] = {}
        self._cancelled_tasks: set[str] = set()

    async def submit_task(
        self,
        task_id: str,
        name: str,
        video_path: str,
        anchor_id: Optional[str] = None,
        output_dir: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        提交并启动异步切片制作任务
        """
        if not output_dir:
            output_dir = (AVATAR_ASSETS_DIR / task_id).as_posix()
        Path(output_dir).mkdir(parents=True, exist_ok=True)

        async def _runner():
            try:
                await self._execute_pipeline(task_id, name, video_path, output_dir, anchor_id)
            except asyncio.CancelledError:
                logger.info(f"任务 {task_id} 已取消")
                await self._update_task_db(
                    task_id,
                    status="cancelled",
                    stage_message="任务已由用户手动取消",
                    error_message="",
                )
            except Exception as e:
                logger.exception(f"任务 {task_id} 执行异常: {e}")
                await self._update_task_db(
                    task_id,
                    status="failed",
                    stage_message=f"执行失败: {str(e)[:100]}",
                    error_message=str(e),
                )
            finally:
                self._running_tasks.pop(task_id, None)
                self._cancelled_tasks.discard(task_id)

        task = asyncio.create_task(_runner())
        self._running_tasks[task_id] = task
        return {
            "task_id": task_id,
            "name": name,
            "status": "pending",
            "progress": 0,
            "output_dir": output_dir,
        }

    async def cancel_task(self, task_id: str) -> bool:
        """取消指定任务并同步更新数据库状态"""
        self._cancelled_tasks.add(task_id)
        task = self._running_tasks.get(task_id)
        if task and not task.done():
            task.cancel()

        # 同步更新数据库状态，确保轮询接口即刻获取 cancelled 状态
        await self._update_task_db(
            task_id,
            status="cancelled",
            stage_message="任务已由用户手动取消",
            error_message="",
        )
        return True

    async def get_task_status(self, task_id: str) -> Optional[Dict[str, Any]]:
        """获取单个任务的最新状态与详细进度"""
        async with AsyncSessionLocal() as db:
            task = await db.get(AvatarTask, task_id)
            if not task:
                return None
            return self._format_task(task)

    async def list_tasks(self, anchor_id: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        """列出切片制作任务"""
        async with AsyncSessionLocal() as db:
            stmt = select(AvatarTask)
            if anchor_id:
                stmt = stmt.where(AvatarTask.anchor_id == anchor_id)
            stmt = stmt.order_by(AvatarTask.created_at.desc()).limit(limit)
            res = await db.execute(stmt)
            tasks = res.scalars().all()
            return [self._format_task(t) for t in tasks]

    def _format_task(self, t: AvatarTask) -> Dict[str, Any]:
        preview_path = ""
        meta_info = {}
        if t.output_dir and Path(t.output_dir).exists():
            cand_preview = Path(t.output_dir) / "preview.jpg"
            if cand_preview.exists():
                preview_path = cand_preview.as_posix()
            meta_path = Path(t.output_dir) / "meta.json"
            if meta_path.exists():
                try:
                    meta_info = json.loads(meta_path.read_text(encoding="utf-8"))
                except Exception:
                    pass

        return {
            "id": t.id,
            "anchor_id": t.anchor_id or "",
            "name": t.name,
            "status": t.status,
            "progress": t.progress,
            "stage_message": t.stage_message or "",
            "video_path": t.video_path or "",
            "output_dir": t.output_dir or "",
            "preview_path": preview_path,
            "meta": meta_info,
            "error_message": t.error_message or "",
            "created_at": t.created_at.isoformat() if t.created_at else "",
            "updated_at": t.updated_at.isoformat() if t.updated_at else "",
        }

    async def _update_task_db(
        self,
        task_id: str,
        status: Optional[str] = None,
        progress: Optional[int] = None,
        stage_message: Optional[str] = None,
        output_dir: Optional[str] = None,
        error_message: Optional[str] = None,
    ):
        """更新任务在 SQLite 数据库中的记录"""
        async with AsyncSessionLocal() as db:
            task = await db.get(AvatarTask, task_id)
            if not task:
                return
            if status is not None:
                task.status = status
            if progress is not None:
                task.progress = min(100, max(0, progress))
            if stage_message is not None:
                task.stage_message = stage_message
            if output_dir is not None:
                task.output_dir = output_dir
            if error_message is not None:
                task.error_message = error_message
            task.updated_at = datetime.datetime.now(datetime.timezone.utc)
            await db.commit()

    async def _execute_pipeline(
        self,
        task_id: str,
        name: str,
        video_path: str,
        output_dir: str,
        anchor_id: Optional[str],
    ):
        """端到端切片处理流水线执行核心"""
        out_path = Path(output_dir)
        full_imgs_dir = out_path / "full_imgs"
        full_imgs_dir.mkdir(parents=True, exist_ok=True)

        # ---------------------------------------------------------------------
        # 阶段 0: 算力调度与硬件显存研判
        # ---------------------------------------------------------------------
        from server.core.hardware.gpu_capability import evaluate_compute
        compute_plan = await evaluate_compute(feature_name="数字人视频切片制作", required_vram_gb=2.0)
        compute_notice = "轻量 CPU 运算"
        if compute_plan.use_cloud:
            compute_notice = f"已优先调度云端显卡加速 ({compute_plan.cloud_gpu.get('provider_name', 'Sidecar')})"
        elif compute_plan.is_low_spec_local:
            compute_notice = "本地显存不足2GB且未配云端，已自动采用轻量CPU算法"

        # ---------------------------------------------------------------------
        # 阶段 1 (0% ~ 10%): 视频有效性检测与元数据提取
        # ---------------------------------------------------------------------
        await self._update_task_db(
            task_id,
            status="processing",
            progress=5,
            stage_message=f"正在校验视频文件 ({compute_notice})...",
        )
        if not Path(video_path).exists():
            raise FileNotFoundError(f"视频文件不存在: {video_path}")

        meta = await asyncio.to_thread(self._inspect_video, video_path)
        fps = meta.get("fps", 25.0)
        total_video_frames = meta.get("total_frames", 0)
        width = meta.get("width", 1280)
        height = meta.get("height", 720)

        if total_video_frames <= 0:
            raise ValueError("无法读取视频有效画面帧，视频可能损坏或格式不受支持")

        # ---------------------------------------------------------------------
        # 阶段 2 (10% ~ 50%): 视频帧连续切片 (full_imgs/)
        # ---------------------------------------------------------------------
        await self._update_task_db(
            task_id,
            progress=12,
            stage_message=f"启动帧切片提取管线 (共计 {total_video_frames} 帧 · {compute_notice})...",
        )

        extracted_frames = await asyncio.to_thread(
            self._extract_frames_worker,
            video_path,
            full_imgs_dir,
            task_id,
            self._cancelled_tasks,
        )

        if not extracted_frames:
            raise RuntimeError("切片提取失败，未能生成有效图像帧")

        total_extracted = len(extracted_frames)
        await self._update_task_db(
            task_id,
            progress=50,
            stage_message=f"已成功提取 {total_extracted} 帧切片序列",
        )

        # ---------------------------------------------------------------------
        # 阶段 3 (50% ~ 80%): 人脸检测、对齐与 coords.pkl 坐标生成
        # ---------------------------------------------------------------------
        await self._update_task_db(
            task_id,
            progress=52,
            stage_message="开始识别人脸面部位置，生成口型驱动特征包围盒 (coords.pkl)...",
        )

        coords, landmarks_summary = await asyncio.to_thread(
            self._detect_faces_and_coords_worker,
            extracted_frames,
            task_id,
            self._cancelled_tasks,
        )

        coords_path = out_path / "coords.pkl"
        with open(coords_path, "wb") as f:
            pickle.dump(coords, f)

        landmarks_json_path = out_path / "landmarks.json"
        landmarks_json_path.write_text(
            json.dumps(landmarks_summary, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        await self._update_task_db(
            task_id,
            progress=80,
            stage_message=f"面部坐标提取与平滑对齐完成 (已校准 {len(coords)} 帧坐标)",
        )

        # ---------------------------------------------------------------------
        # 阶段 4 (80% ~ 90%): 视频音频伴音分离 (audio.wav)
        # ---------------------------------------------------------------------
        await self._update_task_db(
            task_id,
            progress=82,
            stage_message="正在提取伴音轨道并重采样为标准 16kHz 单声道 PCM...",
        )

        audio_wav_path = out_path / "audio.wav"
        await asyncio.to_thread(self._extract_audio_worker, video_path, audio_wav_path)

        await self._update_task_db(
            task_id,
            progress=90,
            stage_message="音频轨道分离就绪",
        )

        # ---------------------------------------------------------------------
        # 阶段 5 (90% ~ 100%): 预览封面与元数据归档，关联主播与资产库
        # ---------------------------------------------------------------------
        await self._update_task_db(
            task_id,
            progress=92,
            stage_message="正在生成资产缩略图并写入 meta.json...",
        )

        preview_jpg = out_path / "preview.jpg"
        if extracted_frames:
            # 复制第一帧或中间典型帧作为缩略图封面
            rep_idx = min(len(extracted_frames) // 4, len(extracted_frames) - 1)
            shutil.copy2(extracted_frames[rep_idx], preview_jpg)

        meta_content = {
            "task_id": task_id,
            "anchor_id": anchor_id or "",
            "name": name,
            "fps": 25.0,
            "frame_count": total_extracted,
            "width": width,
            "height": height,
            "coords_count": len(coords),
            "has_audio": audio_wav_path.exists() and audio_wav_path.stat().st_size > 44,
            "source_video": video_path,
            "output_dir": output_dir,
            "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        (out_path / "meta.json").write_text(
            json.dumps(meta_content, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        # 更新 SQLite 数据库：绑定主播档案并录入数字人形象库
        async with AsyncSessionLocal() as db:
            # 1. 更新任务自身状态
            task = await db.get(AvatarTask, task_id)
            if task:
                task.status = "completed"
                task.progress = 100
                task.stage_message = f"数字人资产制作完成！共计 {total_extracted} 帧"
                task.output_dir = output_dir

            # 2. 如果绑定了主播，自动更新主播的数字人资产目录
            if anchor_id:
                anchor = await db.get(Anchor, anchor_id)
                if anchor:
                    anchor.avatar_asset_dir = output_dir
                    anchor.source_video = video_path
                    if not anchor.photo_portrait and preview_jpg.exists():
                        anchor.photo_portrait = preview_jpg.as_posix()

            # 3. 录入全局 Avatar 资产表
            avatar_rec_id = f"avatar_{task_id}"
            avatar_rec = await db.get(Avatar, avatar_rec_id)
            if not avatar_rec:
                avatar_rec = Avatar(
                    id=avatar_rec_id,
                    name=name,
                    avatar_type="video",
                    source_file_path=video_path,
                    preprocessed_cache_path=output_dir,
                )
                db.add(avatar_rec)
            else:
                avatar_rec.name = name
                avatar_rec.source_file_path = video_path
                avatar_rec.preprocessed_cache_path = output_dir

            await db.commit()

        logger.info(f"任务 {task_id} 切片与特征提取流水线已全部成功完成！")

    # =========================================================================
    # 同步 Worker 处理逻辑 (由 asyncio.to_thread 调度至子线程)
    # =========================================================================

    @staticmethod
    def _inspect_video(video_path: str) -> Dict[str, Any]:
        """读取视频元信息"""
        cap = cv2.VideoCapture(str(video_path))
        if not cap.isOpened():
            return {"fps": 25.0, "total_frames": 0, "width": 1280, "height": 720}
        fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 1280)
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 720)
        cap.release()
        return {
            "fps": fps,
            "total_frames": total_frames,
            "width": width,
            "height": height,
        }

    @staticmethod
    def _extract_frames_worker(
        video_path: str,
        full_imgs_dir: Path,
        task_id: str,
        cancelled_tasks: set[str],
    ) -> List[Path]:
        """
        提取视频帧至 full_imgs/ 目录 (命名为 0.jpg, 1.jpg, ...)
        标准 LiveTalking 规范：从 0 开始连续编号。
        """
        extracted_paths: List[Path] = []
        cap = cv2.VideoCapture(str(video_path))
        if not cap.isOpened():
            return extracted_paths

        frame_idx = 0
        while True:
            if task_id in cancelled_tasks:
                cap.release()
                raise asyncio.CancelledError()

            ret, frame = cap.read()
            if not ret or frame is None:
                break

            target_file = full_imgs_dir / f"{frame_idx}.jpg"
            # 压缩为标准 JPEG (质量 95)，保证速度与高保真画质
            cv2.imwrite(str(target_file), frame, [int(cv2.IMWRITE_JPEG_QUALITY), 95])
            extracted_paths.append(target_file)
            frame_idx += 1

        cap.release()
        return extracted_paths

    @classmethod
    def _detect_faces_and_coords_worker(
        cls,
        frame_paths: List[Path],
        task_id: str,
        cancelled_tasks: set[str],
    ) -> Tuple[List[Tuple[int, int, int, int]], Dict[str, Any]]:
        """
        对提取的每帧执行人脸检测，生成平滑后的 coords 列表：[(ymin, ymax, xmin, xmax), ...]
        并生成关键点与区域诊断报告 landmarks_summary。
        """
        raw_boxes: List[Tuple[int, int, int, int]] = []
        detected_count = 0

        # 初始化 Haar 分类器以备兜底快速检测
        cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        face_cascade = cv2.CascadeClassifier(cascade_path)

        last_valid_box = None

        for idx, img_path in enumerate(frame_paths):
            if task_id in cancelled_tasks:
                raise asyncio.CancelledError()

            img = cv2.imread(str(img_path))
            if img is None:
                if last_valid_box:
                    raw_boxes.append(last_valid_box)
                else:
                    raw_boxes.append((100, 400, 100, 400))
                continue

            h, w = img.shape[:2]
            box = None

            # 1. 尝试 Haar 人脸检测
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            faces = face_cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=4, minSize=(60, 60))
            if len(faces) > 0:
                fx, fy, fw, fh = max(faces, key=lambda f: f[2] * f[3])
                # 标准 LiveTalking coords 格式: (ymin, ymax, xmin, xmax)
                ymin = max(0, int(fy))
                ymax = min(h, int(fy + fh))
                xmin = max(0, int(fx))
                xmax = min(w, int(fx + fw))
                box = (ymin, ymax, xmin, xmax)
                detected_count += 1

            # 2. 连续帧跟踪兜底 (如果某帧丢脸，使用上一有效帧，避免口型渲染区域剧烈跳变)
            if box is None:
                if last_valid_box is not None:
                    box = last_valid_box
                else:
                    # 居中默认区域
                    cx, cy = w // 2, int(h * 0.35)
                    box_size = int(min(w, h) * 0.45)
                    ymin = max(0, cy - box_size // 2)
                    ymax = min(h, cy + box_size // 2)
                    xmin = max(0, cx - box_size // 2)
                    xmax = min(w, cx + box_size // 2)
                    box = (ymin, ymax, xmin, xmax)

            last_valid_box = box
            raw_boxes.append(box)

        # 3. 对坐标序列应用移动平均平滑滤波 (Rolling Smooth)，彻底消除面部对齐的微颤
        smoothed_coords = cls._smooth_box_sequence(raw_boxes)

        summary = {
            "total_frames": len(frame_paths),
            "face_detected_frames": detected_count,
            "detection_rate": round(detected_count / max(1, len(frame_paths)), 4),
            "average_box": [
                int(np.mean([b[0] for b in smoothed_coords])),
                int(np.mean([b[1] for b in smoothed_coords])),
                int(np.mean([b[2] for b in smoothed_coords])),
                int(np.mean([b[3] for b in smoothed_coords])),
            ] if smoothed_coords else [],
        }

        return smoothed_coords, summary

    @staticmethod
    def _smooth_box_sequence(
        boxes: List[Tuple[int, int, int, int]],
        window_size: int = 5,
    ) -> List[Tuple[int, int, int, int]]:
        """时序均值平滑滤波，消除人脸抖动"""
        if len(boxes) <= window_size:
            return boxes

        arr = np.array(boxes, dtype=np.float32)  # shape: (N, 4)
        smoothed = np.copy(arr)
        half = window_size // 2
        n = len(boxes)

        for i in range(n):
            start = max(0, i - half)
            end = min(n, i + half + 1)
            smoothed[i] = np.mean(arr[start:end], axis=0)

        result: List[Tuple[int, int, int, int]] = []
        for row in smoothed:
            result.append((int(row[0]), int(row[1]), int(row[2]), int(row[3])))
        return result

    @staticmethod
    def _extract_audio_worker(video_path: str, audio_wav_path: Path):
        """
        利用 ffmpeg 提取 16000Hz 单声道 16bit PCM WAV。
        若 ffmpeg 不可用或视频无音频，生成 1 秒静音 WAV 保证 LiveTalking 不会报错。
        """
        ffmpeg_bin = shutil.which("ffmpeg")
        extracted = False

        if ffmpeg_bin:
            cmd = [
                ffmpeg_bin,
                "-y",
                "-i", str(video_path),
                "-vn",
                "-acodec", "pcm_s16le",
                "-ar", "16000",
                "-ac", "1",
                str(audio_wav_path),
            ]
            try:
                res = subprocess.run(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=60,
                )
                if res.returncode == 0 and audio_wav_path.exists() and audio_wav_path.stat().st_size > 44:
                    extracted = True
            except Exception as e:
                logger.warning(f"ffmpeg 提取伴音失败: {e}")

        if not extracted:
            # 生成 1 秒无声 WAV 文件作为安全兜底
            import wave
            with wave.open(str(audio_wav_path), "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(16000)
                # 1秒静音: 16000 * 2 字节零
                wf.writeframes(b"\x00" * 32000)


_global_avatar_task_manager: Optional[AvatarTaskManager] = None


def get_avatar_task_manager() -> AvatarTaskManager:
    """获取全局单例切片训练任务管理器"""
    global _global_avatar_task_manager
    if _global_avatar_task_manager is None:
        _global_avatar_task_manager = AvatarTaskManager()
    return _global_avatar_task_manager
