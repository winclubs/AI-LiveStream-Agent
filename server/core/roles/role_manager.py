from typing import Dict, Optional, List, Any
from server.core.roles.base_role import BaseAnchorRole
from server.core.roles.ecommerce_anchor import EcommerceAnchorRole
from server.core.roles.entertainment_host import EntertainmentHostRole
from server.core.roles.domain_expert import DomainExpertRole
from server.core.roles.chitchat_host import ChitchatHostRole

# 角色类型 -> 实现类映射 (新增角色类型在此登记即可全链路生效)
ROLE_CLASS_MAP = {
    "ecommerce": EcommerceAnchorRole,
    "entertainment": EntertainmentHostRole,
    "expert": DomainExpertRole,
    "chitchat": ChitchatHostRole,
}

class RoleManager:
    """主播角色管理中心，支持运行时动态热切换"""
    def __init__(self):
        self._roles: Dict[str, BaseAnchorRole] = {}
        self._active_role_id: str = "role_ecommerce_default"
        self._init_default_roles()

    def _init_default_roles(self):
        for role_cls in ROLE_CLASS_MAP.values():
            inst = role_cls()
            self._roles[inst.role_id] = inst

    def register_role(self, role: BaseAnchorRole):
        self._roles[role.role_id] = role

    def register_or_update_from_db(self, role_db, *, activate: bool = False) -> BaseAnchorRole:
        """从数据库 ORM 模型构建主播实例；保存配置默认不改变当前角色。"""
        role_id = getattr(role_db, "id", "custom")
        role_type = getattr(role_db, "role_type", "ecommerce")
        role_name = getattr(role_db, "role_name", "自定义主播")
        system_prompt = getattr(role_db, "system_prompt", "")
        speech_speed = getattr(role_db, "speech_speed", 1.0)
        pitch_shift = getattr(role_db, "pitch_shift", 0.0)
        guardrail_group = getattr(role_db, "associated_guardrail_group", "general")

        role_cls = ROLE_CLASS_MAP.get(role_type, EcommerceAnchorRole)
        role_inst = role_cls(
            role_id=role_id,
            role_name=role_name,
            system_prompt=system_prompt,
            speech_speed=speech_speed,
            pitch_shift=pitch_shift,
            guardrail_profile=guardrail_group
        )

        self._roles[role_id] = role_inst
        if activate:
            self._active_role_id = role_id
        return role_inst

    def reset_to_defaults(self):
        """重建纯默认运行态，供启动恢复和测试隔离使用。"""
        self._roles.clear()
        self._active_role_id = "role_ecommerce_default"
        self._init_default_roles()

    async def load_from_db(self):
        """加载全部持久化角色，并恢复唯一激活角色。"""
        from sqlalchemy import select
        from server.database.db import AsyncSessionLocal
        from server.database.models import AnchorRole

        async with AsyncSessionLocal() as session:
            rows = (await session.execute(select(AnchorRole))).scalars().all()
        active_id = None
        for row in rows:
            self.register_or_update_from_db(row, activate=False)
            if bool(row.is_active) and active_id is None:
                active_id = row.id
        if active_id and active_id in self._roles:
            self._active_role_id = active_id

    def get_active_role(self) -> BaseAnchorRole:
        return self._roles.get(self._active_role_id, self._roles["role_ecommerce_default"])

    def switch_role(self, role_id: str) -> bool:
        if role_id in self._roles:
            self._active_role_id = role_id
            return True
        return False

    def list_roles(self) -> List[Dict[str, Any]]:
        return [
            {
                "id": r.role_id,
                "name": r.role_name,
                "role_type": r.role_type,
                "speech_speed": r.speech_speed,
                "pitch_shift": r.pitch_shift,
                "is_active": (r.role_id == self._active_role_id)
            }
            for r in self._roles.values()
        ]

# 全局单例
global_role_manager = RoleManager()
