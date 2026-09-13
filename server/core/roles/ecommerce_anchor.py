import asyncio
from typing import AsyncGenerator, Dict, Any, List
from server.core.roles.base_role import BaseAnchorRole
from server.core.llm.client import LLMClient
from server.core.tools.tool_bus import global_mcp_tools


class EcommerceState:
    """带货促单状态机 (规划 §5.2)"""
    CAROUSEL = "CAROUSEL"      # 无弹幕时轮播讲解商品
    QA_CLOSING = "QA_CLOSING"  # 针对弹幕答疑并强势促单
    URGENCY = "URGENCY"        # 降价发券高潮倒计时逼单


class EcommerceAnchorRole(BaseAnchorRole):
    """
    电商带货主播人设大脑（接入真实 LLM 思考生成）
    具备针对商品卖点讲解、答疑逼单、限时促销倒计时催单能力；
    运行 §5.2 三态促单状态机 (轮播/答疑/逼单高潮)
    """
    QA_INTENT_KEYWORDS = ["多少钱", "价格", "怎么买", "怎么卖", "优惠", "券", "库存", "有货", "现货", "便宜", "发货"]

    def __init__(
        self,
        role_id: str = "role_ecommerce_default",
        role_name: str = "金牌带货推荐官·艾米",
        system_prompt: str = "",
        speech_speed: float = 1.1,
        pitch_shift: float = 1.0,
        guardrail_profile: str = "ecommerce"
    ):
        prompt = system_prompt or (
            "你是一位充满激情的金牌好物带货主播【艾米】。\n"
            "【直播风格】：热情亲切、善用'宝子们'、'家人们'；\n"
            "【话术结构】：快速抓痛点 -> 结合商品参数卖点 -> 算出超值折扣 -> 倒计时催单拍下；\n"
            "【约束红线】：严禁违法违规与绝对化用语，字数保持在 30~60 字之间，短促有力、适合口语播报。"
        )
        super().__init__(
            role_id=role_id,
            role_name=role_name,
            role_type="ecommerce",
            system_prompt=prompt,
            speech_speed=speech_speed,
            pitch_shift=pitch_shift,
            guardrail_profile=guardrail_profile
        )
        self.current_state = EcommerceState.CAROUSEL
        self.qa_streak = 0
        self.carousel_index = 0

    def transition(self, event_type: str, payload: dict) -> str:
        """
        §5.2 促单状态机迁移：根据事件推进销售状态并返回新状态
        - 促单提问 -> QA_CLOSING (连续答疑 >= 2 次后冷场 -> URGENCY)
        - 运营 flash_sale 指令 -> 直接 URGENCY
        - 打赏鸣谢不改变销售状态
        """
        if event_type == "gift":
            return self.current_state

        if event_type in ("chat", "danmaku"):
            if payload.get("flash_sale"):
                self.current_state = EcommerceState.URGENCY
                self.qa_streak = 0
            elif any(kw in (payload.get("text") or "") for kw in self.QA_INTENT_KEYWORDS):
                if self.current_state != EcommerceState.QA_CLOSING:
                    self.current_state = EcommerceState.QA_CLOSING
                    self.qa_streak = 0
                self.qa_streak += 1
            return self.current_state

        if event_type == "idle_filler":
            if self.current_state == EcommerceState.QA_CLOSING and self.qa_streak >= 2:
                self.current_state = EcommerceState.URGENCY
            else:
                self.current_state = EcommerceState.CAROUSEL
            return self.current_state

        return self.current_state

    async def _emit_urgency_pitch(self, current_product, live_context):
        """URGENCY 逼单高潮：下发优惠券倒计时 + 倒计时催单话术"""
        title = current_product.get("title", "全场好物") if current_product else "全场好物"
        price = current_product.get("live_price", 99) if current_product else 99
        await global_mcp_tools.call(
            "trigger_onscreen_coupon",
            sku=current_product.get("sku", "") if current_product else "",
            title=title,
            desc=f"{title}限时专享立减",
            seconds=180,
        )
        prompt_input = (
            f"现在是全场逼单高潮时刻！当前主推【{title}】专享价只要{price}元。"
            f"请组织一段紧张刺激的倒计时催单话术（40字以内），强调库存告急、优惠即将失效，催促观众立刻拍下！"
        )
        async for chunk in LLMClient.generate_stream(
            self.system_prompt, prompt_input, history=live_context.get("history"), context=live_context, temperature=0.85
        ):
            yield chunk
        # 逼单播报完成，复位回轮播态
        self.current_state = EcommerceState.CAROUSEL
        self.qa_streak = 0

    async def process_event(
        self,
        event_type: str,
        user_name: str,
        payload: Dict[str, Any],
        live_context: Dict[str, Any]
    ) -> AsyncGenerator[str, None]:
        products: List[Dict[str, Any]] = live_context.get("products", [])
        current_product = products[self.carousel_index] if products else None

        if event_type == "gift":
            gift_name = payload.get("gift_name", "好礼")
            prompt_input = f"观众【{user_name}】给主播送出了【{gift_name}】！请用热情激动、富有感谢力的话术即兴鸣谢，给观众牌面！"
            async for chunk in LLMClient.generate_stream(self.system_prompt, prompt_input, history=live_context.get("history"), context=live_context, temperature=0.8):
                yield chunk
            return

        if event_type == "follow":
            yield f"欢迎【{user_name}】关注艾米！今天直播间超值福利专场，右下角小黄车好物不要错过哦！"
            return

        if event_type == "idle_filler":
            state = self.transition("idle_filler", {})
            if state == EcommerceState.URGENCY:
                # §5.2 URGENCY_BURST：发券倒计时逼单高潮
                async for chunk in self._emit_urgency_pitch(current_product, live_context):
                    yield chunk
                return

            # CAROUSEL 轮播介绍当前主推商品
            prod_name = current_product.get("title", "限时爆款") if current_product else "精选好货"
            price = current_product.get("live_price", 99) if current_product else 99
            prompt_input = f"现在进入直播间日常轮播介绍环节。当前主推商品为【{prod_name}】，专享价只要{price}元。请组织一段30字的激情带货叫卖话术，催促观众去右下角小黄车下单！"
            async for chunk in LLMClient.generate_stream(self.system_prompt, prompt_input, history=live_context.get("history"), context=live_context, temperature=0.8):
                yield chunk
            if products:
                self.carousel_index = (self.carousel_index + 1) % len(products)
            return

        # 观众常规弹幕互动 (chat/danmaku)：先过状态机，运营 flash_sale 指令进入逼单高潮
        text = payload.get("text", "")
        state = self.transition("danmaku" if event_type == "danmaku" else "chat", payload)
        tool_hint = ""

        if state == EcommerceState.URGENCY:
            async for chunk in self._emit_urgency_pitch(current_product, live_context):
                yield chunk
            return

        # MCP 异步工具并行调用：库存实时查询
        if any(kw in text for kw in ["库存", "有货", "现货", "还有吗"]):
            stock_result = await global_mcp_tools.call(
                "query_stock",
                title=current_product.get("title", "") if current_product else ""
            )
            if stock_result.get("found"):
                tool_hint = f"【MCP工具实时库存】商品《{stock_result['title']}》当前库存 {stock_result['stock']} 件，请在话术中如实播报库存，严禁夸大。"
            else:
                tool_hint = "【MCP工具实时库存】商品库中未查询到该商品，请引导观众咨询客服或查看购物车链接。"

        # MCP 异步工具并行调用：优惠券倒计时下发
        if any(kw in text for kw in ["优惠券", "券", "便宜点", "优惠"]):
            coupon_desc = f"{current_product.get('title', '全场好物')}限时专享立减" if current_product else "全场限时专享立减"
            coupon_result = await global_mcp_tools.call(
                "trigger_onscreen_coupon",
                sku=current_product.get("sku", "") if current_product else "",
                title=current_product.get("title", "") if current_product else "",
                desc=coupon_desc,
                seconds=180,
            )
            if coupon_result.get("ok"):
                tool_hint += f"【MCP工具已执行】屏幕优惠券倒计时角标已下发({coupon_desc}，180秒)，请口播引导观众立即抢券并营造紧迫感。"

        # MCP 异步工具并行调用：商品特写镜头切换
        if any(kw in text for kw in ["细节", "特写", "看看", "看一下", "材质", "面料", "做工"]):
            closeup = await global_mcp_tools.call(
                "product_closeup",
                sku=current_product.get("sku", "") if current_product else "",
                title=current_product.get("title", "") if current_product else "",
                image=(current_product.get("images") or [""])[0] if current_product else "",
                seconds=6,
            )
            if closeup.get("ok"):
                tool_hint += "【MCP工具已执行】已在控制台显示商品细节特写画层，请配合画层讲解材质与做工。"

        # MCP 异步工具并行调用：尺码对照表画层
        if any(kw in text for kw in ["尺码", "码数", "尺寸", "大小", "身高", "体重", "偏大", "偏小"]):
            size_res = await global_mcp_tools.call(
                "show_size_chart",
                sku=current_product.get("sku", "") if current_product else "",
                title=current_product.get("title", "") if current_product else "",
                size_chart=current_product.get("size_chart", {}) if current_product else {},
            )
            if size_res.get("ok"):
                tool_hint += "【MCP工具已执行】已在控制台显示尺码对照画层，请引导观众根据真实尺码数据选择。"

        prompt_input = (
            f"观众【{user_name}】发弹幕提问：\"{text}\"。"
            f"请结合当前商品卖点和带货主播人设，给出热情、接地气、有促单力度的简短解答（50字左右），引导去小黄车下单！"
            f"{tool_hint}"
        )
        async for chunk in LLMClient.generate_stream(
            self.system_prompt, prompt_input, history=live_context.get("history"), context=live_context, temperature=0.8
        ):
            yield chunk
