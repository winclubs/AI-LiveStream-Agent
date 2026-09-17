# -*- coding: utf-8 -*-
"""
数字人驱动架构核心包
"""
from server.core.avatar.base_driver import BaseAvatarDriver
from server.core.avatar.registry import AvatarDriverFactory, register_avatar_driver
from server.core.avatar.drivers import (
    LocalLiveTalkingDriver,
    CloudSidecarDriver,
    Procedural2DDriver,
    MockAvatarDriver,
)

from server.core.avatar.task_manager import (
    AvatarTaskManager,
    get_avatar_task_manager,
)
from server.core.avatar.action_state_machine import (
    ActionStateMachine,
    get_action_state_machine,
    mirror_index,
)

from typing import Optional

_active_avatar_driver: Optional[BaseAvatarDriver] = None

def get_active_avatar_driver() -> BaseAvatarDriver:
    """获取当前处于激活状态的全局数字人驱动实例"""
    global _active_avatar_driver
    if _active_avatar_driver is None:
        _active_avatar_driver = AvatarDriverFactory.create_driver("procedural")
    return _active_avatar_driver

def set_active_avatar_driver(driver: BaseAvatarDriver) -> None:
    """动态替换当前激活的数字人驱动"""
    global _active_avatar_driver
    _active_avatar_driver = driver

__all__ = [
    "BaseAvatarDriver",
    "AvatarDriverFactory",
    "register_avatar_driver",
    "LocalLiveTalkingDriver",
    "CloudSidecarDriver",
    "Procedural2DDriver",
    "MockAvatarDriver",
    "get_active_avatar_driver",
    "set_active_avatar_driver",
    "AvatarTaskManager",
    "get_avatar_task_manager",
    "ActionStateMachine",
    "get_action_state_machine",
    "mirror_index",
]
