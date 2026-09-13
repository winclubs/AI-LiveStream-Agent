import json
import logging
from typing import AsyncGenerator, Optional, Dict, Any
import httpx
from sqlalchemy import select
from server.database.db import AsyncSessionLocal
from server.database.models import ApiProviderConfig
from server.config import decrypt_secret

logger = logging.getLogger("LiveAgent.LLMClient")

class LLMClient:
    """
    统一大语言模型异步流式调用客户端
    支持：
    1. OpenAI 兼容接口（DeepSeek, Kimi, Qwen, GLM, OpenAI 等）
    2. 本地 Ollama 离线流式接口
    3. 自动从本地数据库读取当前激活配置并进行 AES-256 密钥解密
    4. 优雅异常兜底：在未配置 Key 或网络不可达时无缝回退至动态人设话术，确保直播不冷场
    """

    @classmethod
    async def get_active_llm_config(cls) -> Dict[str, Any]:
        """获取当前激活的 LLM 服务商配置"""
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(ApiProviderConfig)
                .where(ApiProviderConfig.config_group == "llm")
                .where(ApiProviderConfig.is_active == 1)
            )
            cfg = result.scalars().first()
            if not cfg:
                # 默认回退到 OpenAI 格式配置
                return {
                    "provider_name": "openai_compatible",
                    "base_url": "https://api.deepseek.com/v1",
                    "model_name": "deepseek-chat",
                    "api_key": "",
                    "extra_params": {},
                }
            raw_key = decrypt_secret(cfg.encrypted_api_key) if cfg.encrypted_api_key else ""
            try:
                extra_params = json.loads(cfg.extra_params_json or "{}")
            except Exception:
                extra_params = {}
            return {
                "provider_name": cfg.provider_name,
                "base_url": cfg.base_url or "https://api.deepseek.com/v1",
                "model_name": cfg.model_name or "deepseek-chat",
                "api_key": raw_key,
                "extra_params": extra_params,
            }

    @staticmethod
    def _build_product_context(products: list) -> str:
        """构造受控长度的完整商品上下文，避免字段缺失或提示词无限膨胀。"""
        blocks = []
        for product in products[:5]:
            selling_points = [str(item)[:120] for item in (product.get("selling_points") or [])[:8]]
            faq_items = []
            for item in (product.get("faq_data") or product.get("faq") or [])[:8]:
                if isinstance(item, dict):
                    question = str(item.get("question") or item.get("q") or "")[:160]
                    answer = str(item.get("answer") or item.get("a") or "")[:240]
                    faq_items.append(f"{question}=>{answer}")
            size_chart = json.dumps(product.get("size_chart") or {}, ensure_ascii=False, separators=(",", ":"))[:1200]
            images = [str(path)[:240] for path in (product.get("images") or [])[:3]]
            blocks.append("\n".join([
                f"商品:{str(product.get('title') or '')[:200]} | SKU:{str(product.get('sku') or '')[:100]} | 类目:{str(product.get('category') or '')[:100]}",
                f"原价:{product.get('original_price')} | 直播价:{product.get('live_price')} | 库存:{product.get('current_stock')}",
                f"卖点:{'；'.join(selling_points)}",
                f"描述:{str(product.get('description') or '')[:800]}",
                f"优惠话术:{str(product.get('coupon_script') or '')[:400]}",
                f"FAQ:{'；'.join(faq_items)}",
                f"尺码表:{size_chart}",
                f"图片元数据:{'；'.join(images)}",
            ]))
        return "\n\n".join(blocks)[:6000]

    @classmethod
    async def generate_stream(
        cls,
        system_prompt: str,
        user_message: str,
        history: Optional[list] = None,
        context: Optional[Dict[str, Any]] = None,
        temperature: float = 0.7
    ) -> AsyncGenerator[str, None]:
        """
        向当前激活的大模型发起流式生成请求，逐字/逐词 yield 返回文本片段
        """
        cfg = await cls.get_active_llm_config()
        base_url = cfg["base_url"].rstrip("/")
        model = cfg["model_name"]
        api_key = cfg["api_key"]
        provider = cfg["provider_name"].lower()
        extras = cfg.get("extra_params") or {}
        prompt_caching = bool(extras.get("prompt_caching"))

        # 构造上下文与消息
        messages = [{"role": "system", "content": system_prompt}]
        if history:
            messages.extend(history[-4:])

        user_content = user_message
        if context and context.get("products"):
            prod_info = cls._build_product_context(context["products"])
            user_content = f"【当前实时商品信息】:\n{prod_info}\n\n观众提问/互动:\n{user_message}"

        # 显式提示词缓存：Anthropic 系需要 context 级 cache_control；
        # OpenAI/DeepSeek 系为自动前缀缓存，稳定 system 前缀已可命中
        if prompt_caching and ("anthropic" in base_url.lower() or "claude" in model.lower()):
            messages[0] = {
                "role": "system",
                "content": [{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
            }

        # 多模态视觉感知：按需智能注入 (规划 §2.2 / §9.3 智能唤醒与 Token 节流)
        from server.core.vision.capture import is_vision_query
        vision_b64 = (context or {}).get("vision_image_b64") if context else None
        force_vision = bool((context or {}).get("force_vision"))
        should_attach_vision = bool(vision_b64 and (force_vision or is_vision_query(user_message)))

        # Ollama native /api/chat 使用独立 images 字段承载图像，content 必须为纯字符串；
        # OpenAI 兼容接口使用 content 数组 + image_url (Tier A 全本地多模态兼容)
        use_ollama_native = "ollama" in provider and not base_url.endswith("/v1")

        if should_attach_vision and use_ollama_native:
            messages.append({"role": "user", "content": user_content, "images": [vision_b64]})
        elif should_attach_vision:
            user_payload = [
                {"type": "text", "text": user_content},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{vision_b64}"}},
            ]
            messages.append({"role": "user", "content": user_payload})
        else:
            messages.append({"role": "user", "content": user_content})

        # 判断是否具备在线大模型真实调用的条件
        if api_key or "ollama" in provider:
            try:
                headers = {"Content-Type": "application/json"}
                if api_key:
                    headers["Authorization"] = f"Bearer {api_key}"

                payload = {
                    "model": model,
                    "messages": messages,
                    "stream": True,
                    "temperature": temperature,
                    "max_tokens": 256
                }

                endpoint = f"{base_url}/chat/completions"
                if use_ollama_native:
                    endpoint = f"{base_url}/api/chat"

                logger.info(f"正在调用 LLM 大脑流式思考: provider={provider}, model={model}, endpoint={endpoint}")

                async with httpx.AsyncClient(timeout=15.0) as client:
                    async with client.stream("POST", endpoint, headers=headers, json=payload) as response:
                        if response.status_code == 200:
                            yielded_any = False
                            async for line in response.aiter_lines():
                                if not line:
                                    continue
                                if line.startswith("data:"):
                                    line_data = line[5:].strip()
                                    if line_data == "[DONE]":
                                        break
                                    try:
                                        chunk_json = json.loads(line_data)
                                        choices = chunk_json.get("choices", [])
                                        if choices:
                                            delta = choices[0].get("delta", {})
                                            content = delta.get("content", "")
                                            if content:
                                                yielded_any = True
                                                yield content
                                    except Exception:
                                        continue
                                elif "ollama" in provider and line.startswith("{"):
                                    try:
                                        chunk_json = json.loads(line)
                                        content = chunk_json.get("message", {}).get("content", "")
                                        if content:
                                            yielded_any = True
                                            yield content
                                        if chunk_json.get("done", False):
                                            break
                                    except Exception:
                                        continue
                            if yielded_any:
                                return
                            logger.warning("LLM 接口返回 HTTP 200 但未产生有效文本，触发动态兜底生成")
                        else:
                            logger.warning(f"LLM 接口返回状态码异常: {response.status_code}，触发动态兜底生成")
            except Exception as e:
                if locals().get("yielded_any", False):
                    logger.warning("LLM 远程流已产生部分文本后中断 (%s)，保留已输出内容且不拼接离线兜底", e)
                    return
                logger.warning(f"LLM 远程流式请求异常 ({e})，触发动态兜底生成")

        # 优雅降级兜底生成逻辑（在无 Key 或断网时触发）
        logger.info("使用离线智能人设模板生成话术")
        fallback_reply = cls._generate_fallback(system_prompt, user_message, context)
        # 模拟自然流式切分
        for i in range(0, len(fallback_reply), 3):
            yield fallback_reply[i:i+3]

    @staticmethod
    def _generate_fallback(system_prompt: str, user_message: str, context: Optional[dict]) -> str:
        """根据人设与输入生成的离线兜底话术"""
        if "带货" in system_prompt or "艾米" in system_prompt:
            if context and "products" in context and context["products"]:
                p = context["products"][0]
                return f"宝子们看过来！关于大家关心的【{p.get('title')}】，今天直播间专享到手只要¥{p.get('live_price')}！品质保真，库存拍一件少一件，需要的抓紧去右下角小黄车1号链接下单抢购哦！"
            return "家人们积极参与互动，今天直播间好物满满，右下角小黄车多款爆款现货直发，早拍早发货，不要错过限时福利哦！"
        elif "陪伴" in system_prompt or "娜娜" in system_prompt:
            if "送出" in user_message:
                return f"哇！太感谢啦！{user_message}！娜娜太激动啦，每天都在这里陪大家唠嗑解闷，给直播间的大哥比心啦~"
            return "哈哈，看到朋友们的弹幕啦！娜娜每天都在这里陪大家唠嗑解闷，给直播间的家人们比心啦~"
        elif "唠嗑" in system_prompt or "老王" in system_prompt or "闲聊" in system_prompt:
            if "送出" in user_message:
                return f"哎哟！{user_message}！老王我谢谢老板，咱这缘分必须唠到位！"
            return "哎我跟你说，咱这直播间就是这么自在，想到啥聊啥！你最近有啥好玩的事儿没？说出来大伙乐呵乐呵！"

        else:
            # 专家角色离线兜底：按领域动态生成，严禁跨领域张冠李戴 (ADR-09)
            from server.core.roles.expert_domains import infer_expert_domain, build_expert_offline_reply
            return build_expert_offline_reply(infer_expert_domain(system_prompt))

