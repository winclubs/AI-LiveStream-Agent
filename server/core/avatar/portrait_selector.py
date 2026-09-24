"""
视频多画面智能采样与黄金人脸肖像提取器
功能：
1. 解决单纯截取第 1 帧容易取到黑屏、片头 LOGO、侧脸或无脸废片的问题；
2. 在视频多时间点（10% ~ 80%）密集采样候选帧；
3. 基于 OpenCV Haar 级联人脸检测、拉普拉斯边缘清晰度方差、居中度与亮度综合评分；
4. 毫秒级选出最高质量的真人正面肖像，存入主播 photo_portrait。
"""
import logging
import os
import uuid
from pathlib import Path
from typing import Optional, Tuple
import cv2
import numpy as np

logger = logging.getLogger("LiveAgent.PortraitSelector")

# 加载人脸检测分类器
_FACE_CASCADE = None

def _get_face_cascade():
    global _FACE_CASCADE
    if _FACE_CASCADE is None:
        cascade_path = os.path.join(cv2.data.haarcascades, "haarcascade_frontalface_default.xml")
        if os.path.exists(cascade_path):
            _FACE_CASCADE = cv2.CascadeClassifier(cascade_path)
        else:
            logger.warning("未找到 haarcascade_frontalface_default.xml，人脸检测将使用备用清晰度评估")
    return _FACE_CASCADE


def evaluate_frame_quality(frame: np.ndarray) -> Tuple[float, Optional[Tuple[int, int, int, int]]]:
    """
    评估单帧画面的肖像质量评分
    返回: (综合评分, 最大人脸包围盒 (x, y, w, h) 或 None)
    评分越高代表画面越清晰、人脸越正中端庄
    """
    if frame is None or frame.size == 0:
        return -9999.0, None

    height, width = frame.shape[:2]
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

    # 1. 亮度过滤：过暗（黑屏/过渡）或过曝（纯白）直接大幅扣分
    mean_brightness = float(np.mean(gray))
    if mean_brightness < 28.0:
        # 黑屏或严重暗光帧
        return -1000.0 + mean_brightness, None
    if mean_brightness > 240.0:
        # 极度过曝白屏
        return -500.0, None

    # 2. 拉普拉斯算子清晰度方差 (Variance of Laplacian)
    laplacian_var = float(cv2.Laplacian(gray, cv2.CV_64F).var())

    # 3. 人脸检测与多维度评分
    cascade = _get_face_cascade()
    best_face = None
    face_score = 0.0

    if cascade is not None and not cascade.empty():
        # 检测人脸：minNeighbors=4, minSize=(50, 50)
        faces = cascade.detectMultiScale(
            gray,
            scaleFactor=1.1,
            minNeighbors=4,
            minSize=(int(width * 0.08), int(height * 0.08)),
        )

        if len(faces) > 0:
            # 选取面积最大且最接近中央的人脸
            max_face_area = 0
            for (fx, fy, fw, fh) in faces:
                area = fw * fh
                if area > max_face_area:
                    max_face_area = area
                    best_face = (int(fx), int(fy), int(fw), int(fh))

            if best_face:
                fx, fy, fw, fh = best_face
                face_ratio = (fw * fh) / float(width * height)
                # 针对人脸区域单独计算清晰度
                face_roi = gray[fy : fy + fh, fx : fx + fw]
                face_laplacian = float(cv2.Laplacian(face_roi, cv2.CV_64F).var()) if face_roi.size > 0 else laplacian_var

                # 居中度评分 (0~100)
                center_x = fx + fw / 2.0
                center_y = fy + fh / 2.0
                dist_from_center = np.sqrt(((center_x - width / 2.0) / width) ** 2 + ((center_y - height / 2.0) / height) ** 2)
                center_score = max(0.0, (1.0 - dist_from_center * 2.0)) * 100.0

                # 基础权重 2000 分，确保有人脸帧绝不落后于无人脸帧
                # 人脸比例适中（0.06 ~ 0.35）得分最高
                ratio_bonus = 300.0 if (0.05 <= face_ratio <= 0.40) else 100.0
                face_score = 2000.0 + min(face_laplacian * 2.0, 800.0) + ratio_bonus + center_score

    # 若未检出人脸，退化为全图画面清晰度与亮度综合评分
    if best_face is None:
        overall_score = min(laplacian_var, 300.0) + (mean_brightness * 0.2)
        return overall_score, None

    return face_score, best_face


def smart_extract_best_face_portrait(
    video_path: str,
    output_image_path: str,
    sample_count: int = 10,
) -> Tuple[bool, str, dict]:
    """
    智能多时间点抽样，选取最佳真人正面人脸帧并保存为高质量肖像图
    参数:
        video_path: 本地视频文件路径
        output_image_path: 目标正面肖像保存路径
        sample_count: 采样帧数 (默认 10 帧均匀分布)
    返回:
        (是否成功, 实际保存路径或错误信息, 详细元数据)
    """
    v_path = Path(video_path)
    if not v_path.exists():
        return False, f"视频文件不存在: {video_path}", {}

    cap = cv2.VideoCapture(str(v_path))
    if not cap.isOpened():
        return False, "无法通过 OpenCV 打开视频文件", {}

    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    duration_sec = total_frames / fps if total_frames > 0 else 0.0

    candidates = []

    try:
        # 1. 计算采样位置 (避开首尾 5% ~ 10% 的黑屏或片头)
        if total_frames > 15:
            # 选取 10% 到 85% 之间的均匀分布点
            percentages = np.linspace(0.10, 0.85, num=max(sample_count, 6))
            target_indices = [int(p * total_frames) for p in percentages]
        else:
            # 极短视频，按顺序读取
            target_indices = list(range(max(1, total_frames)))

        for idx in target_indices:
            cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
            ret, frame = cap.read()
            if not ret or frame is None:
                continue

            score, face_rect = evaluate_frame_quality(frame)
            time_sec = idx / fps if fps > 0 else 0.0
            candidates.append({
                "frame_index": idx,
                "time_sec": round(time_sec, 2),
                "score": round(score, 2),
                "has_face": face_rect is not None,
                "face_rect": face_rect,
                "frame": frame,
            })

    finally:
        cap.release()

    if not candidates:
        return False, "未能从视频中读取到任何有效画面", {}

    # 2. 按综合评分从高到低排序
    candidates.sort(key=lambda x: float(x["score"]), reverse=True)
    best = candidates[0]
    best_frame: np.ndarray = best["frame"]

    # 3. 写入输出文件 (JPEG 95 高画质)
    out_file = Path(output_image_path)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    success = cv2.imwrite(str(out_file), best_frame, [int(cv2.IMWRITE_JPEG_QUALITY), 95])

    meta = {
        "total_frames_inspected": total_frames,
        "duration_sec": round(duration_sec, 2),
        "sampled_frames_count": len(candidates),
        "selected_frame_index": best["frame_index"],
        "selected_time_sec": best["time_sec"],
        "selected_score": best["score"],
        "has_detected_face": best["has_face"],
    }

    if success:
        logger.info(
            f"智能多帧优选成功: 选定第 {best['frame_index']} 帧 ({best['time_sec']}s, 得分: {best['score']}, "
            f"检出人脸: {best['has_face']}) 保存至 {output_image_path}"
        )
        return True, str(out_file), meta

    return False, "写入正面形象照文件失败", meta
