"""
人脸关键点/区域检测 (规划 §4.2 单图数字人形象特征预提取)
- 使用 OpenCV 内置 Haar Cascade 检测人脸框与眼睛区域 (零额外权重依赖)
- 检测结果以 JSON/Pickle 缓存，供数字人渲染定位口型/眨眼区域
- 未安装 opencv 或未检出人脸时回退为画面中心框，保证流程闭环
"""
import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger("LiveAgent.FaceLandmarks")

try:
    import cv2
    CV_AVAILABLE = True
except Exception:  # pragma: no cover
    cv2 = None
    CV_AVAILABLE = False


def _center_fallback(width: int, height: int) -> dict:
    w = int(width * 0.42) or 1
    h = int(height * 0.42) or 1
    return {
        "method": "center_fallback",
        "face_count": 0,
        "face_box": [int((width - w) / 2), int(height * 0.18), w, h],
        "eyes": [],
        "image_size": [width, height],
    }


def _detect_mediapipe(img_bgr) -> Optional[dict]:
    """优先使用 MediaPipe FaceMesh 提取 468 点关键点 (未安装则返回 None)"""
    try:
        import mediapipe as mp
    except Exception:
        return None
    try:
        h, w = img_bgr.shape[:2]
        with mp.solutions.face_mesh.FaceMesh(
            static_image_mode=True, max_num_faces=1, refine_landmarks=True,
            min_detection_confidence=0.5
        ) as face_mesh:
            res = face_mesh.process(cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB))
        if not res.multi_face_landmarks:
            return None
        lm = res.multi_face_landmarks[0].landmark
        xs = [p.x * w for p in lm]
        ys = [p.y * h for p in lm]
        box = [int(min(xs)), int(min(ys)), int(max(xs) - min(xs)), int(max(ys) - min(ys))]

        def pt(i):
            return [int(lm[i].x * w), int(lm[i].y * h)]

        return {
            "method": "mediapipe_facemesh",
            "face_count": 1,
            "face_box": box,
            "landmark_count": len(lm),
            "key_points": {
                "left_eye": pt(33),
                "right_eye": pt(263),
                "mouth_top": pt(13),
                "mouth_bottom": pt(14),
            },
            "eyes": [[pt(33)[0], pt(33)[1], 12, 12], [pt(263)[0], pt(263)[1], 12, 12]],
            "image_size": [int(w), int(h)],
        }
    except Exception as e:
        logger.debug(f"MediaPipe 人脸检测异常，回退 Haar: {e}")
        return None


def detect_face_landmarks(image_path: str, cache_path: Optional[str] = None) -> dict:
    """检测单张肖像的人脸框与眼睛区域并落盘缓存 (MediaPipe 优先，Haar 兜底)"""
    if not CV_AVAILABLE:
        return {"method": "opencv_unavailable", "face_count": 0, "face_box": [], "eyes": [], "image_size": [0, 0]}

    img = cv2.imread(str(image_path))
    if img is None:
        logger.warning(f"无法读取肖像图片: {image_path}")
        return {"method": "read_failed", "face_count": 0, "face_box": [], "eyes": [], "image_size": [0, 0]}

    height, width = img.shape[:2]

    # 1. MediaPipe FaceMesh (468 点，精度最高)
    result = _detect_mediapipe(img)

    # 2. OpenCV Haar 兜底
    if result is None:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        gray = cv2.equalizeHist(gray)
        try:
            face_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
            eye_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_eye.xml")
            faces = face_cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(60, 60))
            if len(faces) > 0:
                fx, fy, fw, fh = max(faces, key=lambda f: f[2] * f[3])
                roi_gray = gray[fy:fy + fh, fx:fx + fw]
                eyes = eye_cascade.detectMultiScale(roi_gray, scaleFactor=1.1, minNeighbors=6, minSize=(18, 18))
                eye_boxes = [
                    [int(fx + ex), int(fy + ey), int(ew), int(eh)]
                    for ex, ey, ew, eh in eyes[:2]
                ]
                result = {
                    "method": "haarcascade",
                    "face_count": int(len(faces)),
                    "face_box": [int(fx), int(fy), int(fw), int(fh)],
                    "eyes": eye_boxes,
                    "image_size": [int(width), int(height)],
                }
        except Exception as e:
            logger.warning(f"Haar 人脸检测异常，使用中心框兜底: {e}")

    if result is None:
        result = _center_fallback(width, height)

    if cache_path:
        try:
            Path(cache_path).parent.mkdir(parents=True, exist_ok=True)
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False)
        except Exception as e:
            logger.debug(f"人脸特征缓存写入失败: {e}")
    return result
