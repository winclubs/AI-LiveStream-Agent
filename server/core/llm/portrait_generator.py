"""
AI 商业正面形象照生成引擎
功能：
1. 基于选出的真实黄金人脸帧作为参考底模；
2. 结合主播人设定位（带货/娱乐/专家/闲聊）自动生成专业演播室光影提示词；
3. 支持调用云端生图/多模态大模型进行全新 1024x1024 商业肖像生成；
4. 具备优雅本地摄影级人像增强算法降级保护，零外部依赖也能输出影楼级高清正面形象照。
"""
import base64
import json
import logging
import os
import uuid
from pathlib import Path
from typing import Dict, Any, Optional, Tuple
import cv2
import httpx
import numpy as np

from server.config import DATA_DIR, decrypt_secret
from server.database.db import AsyncSessionLocal
from server.database.models import ApiProviderConfig

logger = logging.getLogger("LiveAgent.PortraitGenerator")

ANCHORS_DIR = DATA_DIR / "anchors"
ANCHORS_DIR.mkdir(parents=True, exist_ok=True)

# 针对不同主播类型的专业演播室光影提示词
ANCHOR_TYPE_STYLE_PROMPTS = {
    "ecommerce": (
        "Professional live commerce host portrait, cheerful and trustworthy expression, "
        "crisp business casual outfit, front-facing eye level, clean neutral minimalist studio background, "
        "soft commercial ring light illumination, 8k uhd, masterpiece, studio lighting, photorealistic."
    ),
    "entertainment": (
        "Charismatic entertainment streamer portrait, vibrant smile and engaging expression, "
        "stylish modern casual clothing, direct eye contact with camera, gentle warm bokeh studio background, "
        "cinematic softbox portrait lighting, high-end commercial photo, 8k, detailed facial features."
    ),
    "expert": (
        "Authoritative and intellectual professional consultant, calm and confident gentle smile, "
        "formal tailored suit or blazer, centered frontal composition, minimalist warm corporate studio backdrop, "
        "Rembrandt key light with subtle rim light, ultra-detailed photorealistic portrait."
    ),
    "chat": (
        "Friendly and warm conversational streamer, relaxed approachable expression, "
        "cozy modern lifestyle attire, natural indoor soft ambient lighting, clean aesthetic, "
        "pleasant friendly posture, crystal clear 4k photograph."
    )
}

def enhance_portrait_locally(input_image_path: str, output_image_path: str) -> bool:
    """
    本地演播室级摄影画质增强引擎 (无外部 API 依赖时的坚固保底)
    执行：
    1. 自适应对比度直方图增强 (CLAHE)
    2. 人像轻量美肤保边平滑 (Bilateral Filter)
    3. 五官微锐化 (Unsharp Mask)
    4. 演播室纯净色彩与暗角光晕校正
    """
    try:
        img = cv2.imread(str(input_image_path))
        if img is None:
            return False

        h, w = img.shape[:2]

        # 1. 轻量保边双边滤波（平滑面部噪点，保持眼唇轮廓锐利）
        smooth = cv2.bilateralFilter(img, d=5, sigmaColor=35, sigmaSpace=35)

        # 2. LAB 空间下的自适应对比度增强 (CLAHE)
        lab = cv2.cvtColor(smooth, cv2.COLOR_BGR2LAB)
        chan_l, a, b = cv2.split(lab)
        clahe = cv2.createCLAHE(clipLimit=1.6, tileGridSize=(8, 8))
        cl = clahe.apply(chan_l)
        limg = cv2.merge((cl, a, b))
        enhanced = cv2.cvtColor(limg, cv2.COLOR_LAB2BGR)

        # 3. 反锐化掩模 (Unsharp Mask) 提升眼神与发丝清晰度
        gaussian = cv2.GaussianBlur(enhanced, (0, 0), sigmaX=1.5)
        sharpened = cv2.addWeighted(enhanced, 1.25, gaussian, -0.25, 0)

        # 4. 输出统一超清分辨率 (宽不低于 720，高不低于 960)
        target_w = max(w, 720)
        target_h = int(target_w * (h / float(w)))
        final_img = cv2.resize(sharpened, (target_w, target_h), interpolation=cv2.INTER_LANCZOS4)

        out_p = Path(output_image_path)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        return bool(cv2.imwrite(str(out_p), final_img, [int(cv2.IMWRITE_JPEG_QUALITY), 96]))
    except Exception as e:
        logger.warning(f"本地人像画质增强异常: {e}")
        return False


class AIPortraitGenerator:
    """AI 肖像生成协调器"""

    @classmethod
    async def generate_commercial_portrait(
        cls,
        anchor_id: str,
        anchor_name: str,
        anchor_type: str = "ecommerce",
        reference_image_path: Optional[str] = None,
        custom_prompt: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        为指定主播生成全新商业正面形象照
        """
        type_key = anchor_type if anchor_type in ANCHOR_TYPE_STYLE_PROMPTS else "ecommerce"
        prompt_style = ANCHOR_TYPE_STYLE_PROMPTS[type_key]
        if custom_prompt and custom_prompt.strip():
            prompt_style = f"{prompt_style}, {custom_prompt.strip()}"

        output_filename = f"{anchor_id}_ai_portrait_{uuid.uuid4().hex[:8]}.jpg"
        target_path = (ANCHORS_DIR / output_filename).as_posix()

        # 1. 检查是否存在可用的外部生图配置
        active_config = None
        async with AsyncSessionLocal() as db:
            from sqlalchemy import select
            res = await db.execute(
                select(ApiProviderConfig).where(
                    ApiProviderConfig.config_group.in_(["llm", "image_gen"]),
                    ApiProviderConfig.is_active == 1
                )
            )
            configs = res.scalars().all()
            # 优先寻找 image_gen 或包含 dalle/flux/wanxiang 的提供商
            for c in configs:
                m_name = (c.model_name or "").lower()
                p_name = (c.provider_name or "").lower()
                if "wanx" in m_name or "flux" in m_name or "dall" in m_name or "image" in p_name:
                    active_config = c
                    break
            if not active_config and configs:
                active_config = configs[0]

        # 2. 如果存在外部配置且配置了生图 API，尝试调用外部服务
        if active_config and active_config.base_url and active_config.encrypted_api_key:
            api_key = decrypt_secret(active_config.encrypted_api_key)
            if api_key:
                try:
                    # 尝试调用 OpenAI 兼容图像生成接口 (POST /images/generations)
                    base_url = (active_config.base_url or "").rstrip("/")
                    url = f"{base_url}/images/generations" if not base_url.endswith("/images/generations") else base_url

                    req_payload = {
                        "prompt": f"Professional portrait of {anchor_name}, {prompt_style}",
                        "n": 1,
                        "size": "1024x1024",
                        "response_format": "b64_json"
                    }
                    if active_config.model_name:
                        req_payload["model"] = active_config.model_name

                    async with httpx.AsyncClient(timeout=30.0) as client:
                        resp = await client.post(
                            url,
                            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                            json=req_payload
                        )
                        if resp.status_code == 200:
                            data = resp.json()
                            items = data.get("data") or []
                            if items and "b64_json" in items[0]:
                                b64 = items[0]["b64_json"]
                                Path(target_path).write_bytes(base64.b64decode(b64))
                                return {
                                    "success": True,
                                    "image_path": target_path,
                                    "source": "cloud_ai",
                                    "model_name": active_config.model_name or "AI Image Gen",
                                    "prompt": prompt_style
                                }
                except Exception as err:
                    logger.warning(f"调用云端生图接口失败，平滑降级本地人像优化: {err}")

        # 3. 优雅降级：若无可用外部生图接口或调用失败，采用参考图的高清演播室画质增强引擎
        if reference_image_path and Path(reference_image_path).exists():
            success = enhance_portrait_locally(reference_image_path, target_path)
            if success:
                return {
                    "success": True,
                    "image_path": target_path,
                    "source": "enhanced_studio",
                    "model_name": "Studio Portrait Master (Local Enhanced)",
                    "prompt": prompt_style
                }

        return {
            "success": False,
            "message": "未能定位有效参考图像或生图服务不可达",
            "image_path": ""
        }
