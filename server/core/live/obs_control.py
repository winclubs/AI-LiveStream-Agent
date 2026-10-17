# -*- coding: utf-8 -*-
"""OBS 所有权属性与运行时配置热加载。

由 server/routes/live.py 的 LiveSessionController 拆出。
缝在**函数体内**从 server.routes.live 解析，见 test_live_seam_late_binding.py。
"""
import logging
from sqlalchemy import select
from typing import Optional
logger = logging.getLogger("LiveAgent.LiveController")


class ObsControlMixin:
    """OBS 所有权属性与运行时配置热加载"""
    @property
    def obs_owner_session_id(self) -> Optional[str]:
        return self._obs_owner_session_id


    @obs_owner_session_id.setter
    def obs_owner_session_id(self, session_id: Optional[str]) -> None:
        """兼容旧调用：直接认领 owner 时同步记录当前 OBS epoch。"""
        from server.routes.live import global_obs_client
        self._obs_owner_session_id = session_id
        if session_id is None:
            self.obs_owner_connection_epoch = None
        elif self.obs_owner_connection_epoch is None:
            current_epoch = getattr(global_obs_client, "connection_epoch", None)
            self.obs_owner_connection_epoch = current_epoch if isinstance(current_epoch, int) else 0


    @property
    def obs_stream_started_by_agent(self) -> bool:
        """推流所有权严格绑定当前会话和 OBS 连接 epoch。"""
        from server.routes.live import global_obs_client
        current_epoch = getattr(global_obs_client, "connection_epoch", None)
        if not isinstance(current_epoch, int):
            current_epoch = self.obs_owner_connection_epoch
        return bool(
            self.obs_owner_session_id
            and self.session_id
            and self.obs_owner_session_id == self.session_id
            and self.obs_owner_connection_epoch is not None
            and self.obs_owner_connection_epoch == current_epoch
        )


    @obs_stream_started_by_agent.setter
    def obs_stream_started_by_agent(self, val: bool):
        from server.routes.live import global_obs_client
        if val and self.session_id:
            self.obs_owner_session_id = self.session_id
            current_epoch = getattr(global_obs_client, "connection_epoch", None)
            self.obs_owner_connection_epoch = current_epoch if isinstance(current_epoch, int) else 0
        else:
            self.obs_owner_session_id = None
            self.obs_owner_connection_epoch = None


    async def reload_runtime_config(self, section: str = "all"):
        """直播运行中配置热重载：不关播无感刷新商品、违禁词、主播人设与配置上下文"""
        from server.routes.live import global_role_manager
        logger.info("执行运行时配置热重载, section=%s", section)
        if section in ("all", "products"):
            try:
                await self._refresh_product_context()
            except Exception:
                logger.exception("热重载商品上下文失败")
        if section in ("all", "guardrails"):
            try:
                from server.database.db import AsyncSessionLocal
                from server.routes.guardrails import reload_guardrails
                async with AsyncSessionLocal() as session:
                    await reload_guardrails(session)
            except Exception:
                logger.exception("热重载违禁词引擎失败")
        if section in ("all", "roles", "anchors"):
            try:
                from server.database.db import AsyncSessionLocal
                from server.database.models import AnchorRole
                async with AsyncSessionLocal() as session:
                    active_role = global_role_manager.get_active_role()
                    if active_role:
                        res = await session.execute(select(AnchorRole).where(AnchorRole.id == active_role.role_id))
                        role_db = res.scalar_one_or_none()
                        if role_db:
                            global_role_manager.register_or_update_from_db(role_db, activate=True)
            except Exception:
                logger.exception("热重载当前角色配置失败")
        if section in ("all", "settings"):
            try:
                await self._configure_neural_sidecar()
            except Exception:
                logger.exception("热重载远端 Avatar Provider 失败，继续使用程序化 shadow")
