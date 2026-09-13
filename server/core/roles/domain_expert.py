import asyncio
from typing import AsyncGenerator, Dict, Any
from server.core.roles.base_role import BaseAnchorRole
from server.core.llm.client import LLMClient
from server.core.rag.engine import global_rag
from server.core.roles.expert_domains import infer_expert_domain, build_expert_idle_hint

class DomainExpertRole(BaseAnchorRole):
    """
    行业专家主播人设大脑（接入真实 LLM 思考生成）
    具备客观理性、前置合规免责声明、结构化三步解答与权威依据引用能力；
    领域按角色名与提示词动态推断，离线兜底与冷场话术随领域适配 (ADR-09)
    """
    def __init__(
        self,
        role_id: str = "role_expert_default",
        role_name: str = "资深行业咨询专家·陈老师",
        system_prompt: str = "",
        speech_speed: float = 0.95,
        pitch_shift: float = -0.5,
        guardrail_profile: str = "expert"
    ):
        prompt = system_prompt or (
            "你是一位拥有15年从业经验的资深行业顾问【陈老师】。\n"
            "【解答风格】：严谨、儒雅、客观、条理清晰；\n"
            "【合规前置】：回答法律或医疗相关个案咨询时，务必温和说明'本回答仅供交流参考，非正式代理或诊疗依据'；\n"
            "【结构要求】：1.点出核心焦点 -> 2.阐述法理或实操规范 -> 3.给出具体行动建议；字数保持在 50~80 字之间。"
        )
        super().__init__(
            role_id=role_id,
            role_name=role_name,
            role_type="expert",
            system_prompt=prompt,
            speech_speed=speech_speed,
            pitch_shift=pitch_shift,
            guardrail_profile=guardrail_profile
        )
        self.domain = infer_expert_domain(f"{role_name}\n{prompt}")

    def build_idle_prompt(self, live_context: Dict[str, Any]) -> str:
        """冷场垫场提示词 (按推断领域动态生成，不再硬编码特定领域话题)"""
        hint = build_expert_idle_hint(self.domain)
        return (
            f"当前公屏提问较少，请以{self.role_name}的身份围绕{self.domain['topic_label']}，"
            f"{hint}（50字左右，客观儒雅）。"
        )

    async def process_event(
        self,
        event_type: str,
        user_name: str,
        payload: Dict[str, Any],
        live_context: Dict[str, Any]
    ) -> AsyncGenerator[str, None]:
        if event_type == "gift":
            gift_name = payload.get("gift_name", "心意")
            yield f"非常感谢【{user_name}】朋友对本场咨询探讨的鼓励与支持。大家有问题可以随时在公屏提出，我们共同探讨交流。"
            return

        if event_type == "follow":
            yield f"欢迎【{user_name}】加入我们的行业案例研讨直播间，欢迎随时探讨专业问题。"
            return

        if event_type == "idle_filler":
            prompt_input = self.build_idle_prompt(live_context)
            async for chunk in LLMClient.generate_stream(self.system_prompt, prompt_input, history=live_context.get("history"), context=live_context, temperature=0.3):
                yield chunk
            return

        # 观众专业咨询提问 (chat)
        text = payload.get("text", "")

        # ---- 双路混合 RAG 严谨知识库检索 (规划 §5.4) ----
        rag_result = await global_rag.search_async(text, top_k=3)

        if not rag_result["matched"]:
            # 置信度不足，主动触发安全兜底，杜绝张冠李戴
            yield (
                f"回【{user_name}】朋友：特别声明，本回答仅供交流参考，非正式代理或诊疗依据。"
                "关于您提到的这个问题，涉及具体个案细节与事实材料，为避免给出不严谨的判断，"
                "建议您稍后通过私信或线下渠道提供完整材料，我会结合知识库为您做针对性分析。"
            )
            return

        knowledge_block = global_rag.build_context_block(rag_result)
        prompt_input = (
            f"观众【{user_name}】提问咨询：\"{text}\"。\n"
            f"【企业知识库检索结果】（回答必须严格基于以下资料，并自然引用对应知识片段来源）：\n{knowledge_block}\n\n"
            f"请以{self.role_name}的身份，进行条理清晰、有法理与实操依据的专业解答。"
            f"重要提醒：个案解答前务必包含温和的前置免责声明，总字数控制在60字左右。"
        )
        async for chunk in LLMClient.generate_stream(
            self.system_prompt, prompt_input, history=live_context.get("history"), context=live_context, temperature=0.3
        ):
            yield chunk
