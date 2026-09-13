import asyncio
from typing import AsyncGenerator, Dict, Any
from server.core.roles.base_role import BaseAnchorRole
from server.core.llm.client import LLMClient

class ChitchatHostRole(BaseAnchorRole):
    """
    闲聊扯谈主播人设大脑（接入真实 LLM 思考生成）
    特点：无特定领域约束、轻松随意、什么都 能聊、接梗唠嗑、大白话拉家常
    """
    def __init__(
        self,
        role_id: str = "role_chitchat_default",
        role_name: str = "闲聊扯淡搭子·老王",
        system_prompt: str = "",
        speech_speed: float = 1.0,
        pitch_shift: float = 0.0,
        guardrail_profile: str = "chitchat"
    ):
        prompt = system_prompt or (
            "你是一位特别能唠嗑的闲聊主播【老王】。\n"
            "【直播风格】：像街坊邻居唠家常，轻松随意、大白话、想到啥聊啥，天上地下时事趣闻都能扯；\n"
            "【语言特征】：口语化、接地气，常用'哎你说''我跟你说''可不是嘛'等唠嗑口头禅；\n"
            "【互动原则】：观众聊什么就顺着接什么，主动抛新话题带动气氛，绝不冷场；"
            "涉及政治敏感、违法违规内容时立即岔开话题；\n"
            "【字数控制】：单次发言保持在 30~50 字，像真人唠嗑一样自然。"
        )
        super().__init__(
            role_id=role_id,
            role_name=role_name,
            role_type="chitchat",
            system_prompt=prompt,
            speech_speed=speech_speed,
            pitch_shift=pitch_shift,
            guardrail_profile=guardrail_profile
        )

    async def process_event(
        self,
        event_type: str,
        user_name: str,
        payload: Dict[str, Any],
        live_context: Dict[str, Any]
    ) -> AsyncGenerator[str, None]:
        if event_type == "gift":
            gift_name = payload.get("gift_name", "小礼物")
            prompt_input = f"观众【{user_name}】给你刷了个【{gift_name}】！用唠嗑的口吻随性地感谢一下，别太正式，像老朋友一样。"
            async for chunk in LLMClient.generate_stream(self.system_prompt, prompt_input, history=live_context.get("history"), context=live_context, temperature=0.9):
                yield chunk
            return

        if event_type == "follow":
            yield f"哎【{user_name}】来了！搬个小板凳坐下唠，正好聊到有意思的地方！"
            return

        if event_type == "idle_filler":
            theme = live_context.get("theme") or ""
            theme_hint = f"今日直播主题是【{theme}】。" if theme else ""
            prompt_input = f"{theme_hint}直播间有点安静了，随口起个大家都能插上话的闲聊话题（今天吃了啥、最近的趣事、天气、生活小吐槽之类的），像唠嗑一样自然，40字以内。"
            async for chunk in LLMClient.generate_stream(self.system_prompt, prompt_input, history=live_context.get("history"), context=live_context, temperature=0.9):
                yield chunk
            return

        # 观众常规弹幕 (chat)：顺着聊、接梗、抛话题
        text = payload.get("text", "")
        theme = live_context.get("theme") or ""
        theme_hint = f"今日直播主题是【{theme}】，话题尽量往这上面引。" if theme else ""
        prompt_input = (
            f"{theme_hint}观众【{user_name}】在公屏说：\"{text}\"。"
            f"顺着这个话茬接下去唠，像真人唠嗑一样自然回应，可以适当延伸抛出新话题让大家接着聊（40字左右）。"
        )
        async for chunk in LLMClient.generate_stream(
            self.system_prompt, prompt_input, history=live_context.get("history"), context=live_context, temperature=0.9
        ):
            yield chunk
