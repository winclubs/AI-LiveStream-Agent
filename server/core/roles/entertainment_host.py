import asyncio
from typing import AsyncGenerator, Dict, Any
from server.core.roles.base_role import BaseAnchorRole
from server.core.llm.client import LLMClient

class EntertainmentHostRole(BaseAnchorRole):
    """
    娱乐陪伴主播人设大脑（接入真实 LLM 思考生成）
    具备幽默接梗、高情商情绪陪伴、成语互动与分级打赏鸣谢能力
    """
    def __init__(
        self,
        role_id: str = "role_entertainment_default",
        role_name: str = "元气偶像陪伴主播·娜娜",
        system_prompt: str = "",
        speech_speed: float = 1.0,
        pitch_shift: float = 0.0,
        guardrail_profile: str = "entertainment"
    ):
        prompt = system_prompt or (
            "你是一位活泼幽默、高情商的陪伴型虚拟偶像主播【娜娜】。\n"
            "【直播风格】：元气活泼像老朋友，爱接梗、爱自嘲，说话富有感染力，善于提供积极的情绪价值；\n"
            "【语言特征】：适当运用语气词（哈哈、哇塞、绝了、么么哒）；\n"
            "【字数控制】：单次发言保持在 30~50 字，口语化自然吐字。"
        )
        super().__init__(
            role_id=role_id,
            role_name=role_name,
            role_type="entertainment",
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
            from server.core.roles.games import gift_tier
            gift_name = payload.get("gift_name", "小礼物")
            total_coin = int(payload.get("total_coin", 0) or 0)
            level, guide = gift_tier(total_coin)
            prompt_input = (
                f"观众【{user_name}】给主播送出了【{gift_name}】(价值 {total_coin} 金瓜子，档位 {level})！"
                f"请用充满元气、甜美激动的情绪花式鸣谢，{guide}，给观众比心，40字以内。"
            )
            async for chunk in LLMClient.generate_stream(self.system_prompt, prompt_input, history=live_context.get("history"), context=live_context, temperature=0.85):
                yield chunk
            return

        if event_type == "follow":
            yield f"哇！感谢【{user_name}】点亮关注！娜娜每天都在直播间陪你唠嗑，爱你哟！"
            return

        if event_type == "idle_filler":
            theme = live_context.get("theme") or ""
            theme_hint = f"今日直播主题是【{theme}】。" if theme else ""
            prompt_input = f"{theme_hint}当前直播间弹幕较少，请围绕主题发起一个有趣的互动话题、脑筋急转弯，或活泼俏皮的日常唠嗑（40字以内），带动公屏观众积极打字互动！"
            async for chunk in LLMClient.generate_stream(self.system_prompt, prompt_input, history=live_context.get("history"), context=live_context, temperature=0.85):
                yield chunk
            return

        # 观众常规聊天互动 (chat)：优先驱动内置小游戏引擎
        text = payload.get("text", "")
        from server.core.roles.games import global_mini_games

        if "成语接龙" in text:
            yield global_mini_games.start_idiom()
            return
        if any(k in text for k in ["脑筋急转弯", "猜谜", "灯谜", "谜语"]):
            yield global_mini_games.start_riddle()
            return
        if global_mini_games.mode == "idiom":
            reply = global_mini_games.continue_idiom(text)
            if reply:
                yield reply
                return
        if global_mini_games.mode == "riddle":
            reply = global_mini_games.answer_riddle(text)
            if reply:
                yield reply
                return

        theme = live_context.get("theme") or ""
        theme_hint = f"今日直播主题是【{theme}】，互动尽量贴合主题。" if theme else ""
        prompt_input = f"{theme_hint}观众【{user_name}】发弹幕说：\"{text}\"。请以元气活泼的主播娜娜身份，幽默搞笑且充满情商地回应这位朋友（40字左右）！"
        async for chunk in LLMClient.generate_stream(self.system_prompt, prompt_input, history=live_context.get("history"), context=live_context, temperature=0.85):
            yield chunk
