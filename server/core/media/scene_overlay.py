"""Thread-safe publish-scene state and bounded RGB frame overlays."""
from __future__ import annotations

import copy
import math
import os
import threading
import time
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Dict, Optional

try:
    import cv2
    import numpy as np
except Exception:  # pragma: no cover - optional media dependencies
    cv2 = None
    np = None


class SceneOverlayState:
    """Store independent coupon and scene snapshots for the render thread."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._coupon: Optional[Dict[str, Any]] = None
        self._scene: Optional[Dict[str, Any]] = None

    def set_coupon(self, payload: Dict[str, Any], seconds: int) -> None:
        self._set("coupon", "coupon", payload, seconds)

    def set_scene(self, kind: str, payload: Dict[str, Any], seconds: int) -> None:
        if kind not in {"closeup", "size_chart"}:
            raise ValueError(f"unsupported scene overlay: {kind}")
        self._set("scene", kind, payload, seconds)

    def _set(self, slot: str, kind: str, payload: Dict[str, Any], seconds: int) -> None:
        duration = max(0.0, float(seconds))
        entry = {
            "kind": kind,
            "payload": copy.deepcopy(payload),
            "deadline": self._clock() + duration,
        }
        with self._lock:
            setattr(self, f"_{slot}", entry)

    def snapshot(self) -> Dict[str, Optional[Dict[str, Any]]]:
        now = self._clock()
        with self._lock:
            coupon = self._live_snapshot("coupon", now)
            scene = self._live_snapshot("scene", now)
        return {"coupon": coupon, "scene": scene}

    def _live_snapshot(self, slot: str, now: float) -> Optional[Dict[str, Any]]:
        attribute = f"_{slot}"
        entry = getattr(self, attribute)
        if entry is None:
            return None
        remaining = entry["deadline"] - now
        if remaining <= 0:
            setattr(self, attribute, None)
            return None
        result = copy.deepcopy(entry)
        result.pop("deadline", None)
        result["remaining_seconds"] = max(1, int(math.ceil(remaining)))
        return result

    def clear(self) -> None:
        with self._lock:
            self._coupon = None
            self._scene = None


def _text(value: Any, limit: int = 48) -> str:
    return str(value or "").replace("\n", " ")[:limit]


@lru_cache(maxsize=1)
def _unicode_font_path() -> Optional[str]:
    """Find a Unicode-capable font, preferring an optional packaged asset."""
    configured = os.getenv("LIVE_AGENT_OVERLAY_FONT", "")
    root = Path(__file__).resolve().parents[3]
    candidates = [
        configured,
        str(root / "server" / "static" / "fonts" / "NotoSansCJKsc-Regular.otf"),
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/System/Library/Fonts/PingFang.ttc",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    return None


def renderable_text(value: Any, limit: int = 48) -> str:
    """Preserve Unicode with a capable font, otherwise expose stable codepoints."""
    text = _text(value, limit)
    if _unicode_font_path() or text.isascii():
        return text
    return "".join(character if character.isascii() else f"U+{ord(character):04X}" for character in text)


def _draw_lines(frame, lines, x: int, y: int, max_width: int, line_height: int) -> None:
    if cv2 is None:
        return
    font_scale = max(0.35, min(0.7, frame.shape[1] / 900.0))
    font_path = _unicode_font_path()
    if font_path:
        try:
            from PIL import Image, ImageDraw, ImageFont
            image = Image.fromarray(frame)
            draw = ImageDraw.Draw(image)
            font = ImageFont.truetype(font_path, max(11, int(line_height * 0.72)))
            for index, line in enumerate(lines[:7]):
                baseline_y = y + (index * line_height)
                if baseline_y >= frame.shape[0] - 4:
                    break
                draw.text((x, baseline_y), _text(line, max(8, max_width // 8)), font=font, fill=(245, 245, 245))
            frame[:] = np.asarray(image)
            return
        except Exception:
            pass
    for index, line in enumerate(lines[:7]):
        baseline_y = y + ((index + 1) * line_height)
        if baseline_y >= frame.shape[0] - 4:
            break
        cv2.putText(
            frame,
            renderable_text(line, max(8, max_width // 8)),
            (x, baseline_y),
            cv2.FONT_HERSHEY_SIMPLEX,
            font_scale,
            (245, 245, 245),
            1,
            cv2.LINE_AA,
        )


def _draw_coupon(frame, coupon: Dict[str, Any]) -> None:
    height, width = frame.shape[:2]
    card_w = max(80, min(width - 16, int(width * 0.52)))
    card_h = max(52, min(height - 16, int(height * 0.24)))
    x0, y0 = 8, height - card_h - 8
    overlay = frame.copy()
    cv2.rectangle(overlay, (x0, y0), (x0 + card_w, y0 + card_h), (28, 46, 190), -1)
    cv2.rectangle(overlay, (x0, y0), (x0 + card_w, y0 + card_h), (255, 205, 60), 2)
    cv2.addWeighted(overlay, 0.88, frame, 0.12, 0, dst=frame)
    payload = coupon.get("payload") or {}
    lines = [payload.get("title") or "LIMITED COUPON", payload.get("desc") or "LIVE OFFER"]
    lines.append(f"{coupon.get('remaining_seconds', 1)}s")
    _draw_lines(frame, lines, x0 + 8, y0 + 4, card_w - 16, max(14, card_h // 4))


def _load_closeup_image(path_value: Any, width: int, height: int):
    if cv2 is None or not path_value:
        return None
    try:
        path = Path(str(path_value))
        image = cv2.imread(str(path)) if path.is_file() else None
        if image is None:
            return None
        image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        scale = min(width / image.shape[1], height / image.shape[0])
        resized = cv2.resize(
            image,
            (max(1, int(round(image.shape[1] * scale))), max(1, int(round(image.shape[0] * scale)))),
            interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR,
        )
        canvas = np.zeros((height, width, 3), dtype=np.uint8)
        y0 = (height - resized.shape[0]) // 2
        x0 = (width - resized.shape[1]) // 2
        canvas[y0:y0 + resized.shape[0], x0:x0 + resized.shape[1]] = resized
        return canvas
    except Exception:
        return None


def _draw_scene(frame, scene: Dict[str, Any]) -> None:
    height, width = frame.shape[:2]
    card_w = max(96, min(width - 16, int(width * 0.56)))
    card_h = max(72, min(height - 16, int(height * 0.48)))
    x0, y0 = width - card_w - 8, 8
    overlay = frame.copy()
    cv2.rectangle(overlay, (x0, y0), (x0 + card_w, y0 + card_h), (22, 31, 48), -1)
    cv2.rectangle(overlay, (x0, y0), (x0 + card_w, y0 + card_h), (80, 210, 245), 2)
    cv2.addWeighted(overlay, 0.9, frame, 0.1, 0, dst=frame)

    payload = scene.get("payload") or {}
    lines = [payload.get("title") or ("PRODUCT" if scene.get("kind") == "closeup" else "SIZE CHART")]
    if scene.get("kind") == "closeup":
        image_h = max(1, card_h - 48)
        image_w = max(1, card_w // 2)
        image = _load_closeup_image(payload.get("image"), image_w, image_h)
        if image is not None:
            frame[y0 + 34:y0 + 34 + image_h, x0 + 6:x0 + 6 + image_w] = image
        lines.extend([payload.get("sku"), "PRODUCT CLOSEUP"])
    else:
        chart = payload.get("size_chart") or {}
        columns = list(chart.get("columns") or [])[:4]
        rows = list(chart.get("rows") or [])[:5]
        if columns:
            lines.append(" | ".join(_text(value, 12) for value in columns))
        lines.extend(" | ".join(_text(value, 12) for value in list(row)[:4]) for row in rows)
    _draw_lines(frame, lines, x0 + 8, y0 + 4, card_w - 16, max(14, card_h // 8))


def compose_scene_overlays(frame_rgb, snapshot: Dict[str, Any]):
    """Return a composed frame without mutating the caller's RGB frame."""
    if np is None or cv2 is None or frame_rgb is None:
        return frame_rgb
    try:
        output = np.asarray(frame_rgb).copy()
        if output.ndim != 3 or output.shape[2] != 3 or output.size == 0:
            return output
        coupon = (snapshot or {}).get("coupon")
        scene = (snapshot or {}).get("scene")
        if coupon:
            _draw_coupon(output, coupon)
        if scene:
            _draw_scene(output, scene)
        return output
    except Exception:
        return np.asarray(frame_rgb).copy()


global_scene_overlay_state = SceneOverlayState()
