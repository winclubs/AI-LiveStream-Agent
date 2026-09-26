from abc import ABC, abstractmethod
from typing import AsyncGenerator, Dict, Any, Optional

class BaseAnchorRole(ABC):
    """主播角色人设抽象基类"""
    def __init__(
        self,
        role_id: str,
        role_name: str,
        role_type: str,
        system_prompt: str,
        speech_speed: float = 1.0,
        pitch_shift: float = 0.0,
        guardrail_profile: str = "general"
    ):
        self.role_id = role_id
        self.role_name = role_name
        self.role_type = role_type          # ecommerce / entertainment / expert
        self.system_prompt = system_prompt
        self.speech_speed = speech_speed
        self.pitch_shift = pitch_shift
        self.guardrail_profile = guardrail_profile

    async def build_rag_context_hint(self, query: str, top_k: int = 3) -> str:
        """
        全角色共享的本地知识库检索增强 (规划 §5.4 / README「商品货盘 RAG」)。

        检索命中且过置信度门限时返回「【本地知识库】…」注入提示词；
        未命中或无知识库时返回空串，保持原提示词链路不变 (ADR-11 严格门控)。
        任何异常都不阻断直播主流程。
        """
        try:
            from server.core.rag.engine import global_rag
            if not query.strip():
                return ""
            result = await global_rag.search_async(query, top_k=top_k)
            if not result.get("matched"):
                return ""
            block = global_rag.build_context_block(result)
            return block
        except Exception:
            return ""

    @abstractmethod
    async def process_event(
        self,
        event_type: str,
        user_name: str,
        payload: Dict[str, Any],
        live_context: Dict[str, Any]
    ) -> AsyncGenerator[str, None]:
        """
        处理直播间事件并流式生成回复片段 (Tokens/Chunks)
        :param event_type: 事件类型 (chat / gift / follow / idle_filler)
        :param user_name: 发起用户
        :param payload: 事件附加内容 (如弹幕正文 text，礼物名 gift_name)
        :param live_context: 直播间当前上下文 (当前商品、在看人数等)
        :return: 异步生成器，依次输出文本切片
        """
        if False:
            yield ""
