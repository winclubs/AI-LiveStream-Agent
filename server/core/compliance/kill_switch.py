# -*- coding: utf-8 -*-
"""
server/core/compliance/kill_switch.py
灰度发布、动态 Feature Flag 与紧急熔断 (Kill Switch) 中枢 (依据《商业化运营改造方案》§10 与 §12 P1)

【设计目标】
1. 支持全局、平台级 (douyin/kuaishou/wechat/bilibili) 与单主播级的动态 Feature Flag 细粒度控制；
2. 覆盖关键功能开关：平台推流 (stream_push)、数字人渲染 (avatar_rendering)、语音播报 (voice_synthesis)、
   电商带货促单 (commerce_agent)、弹幕自动回复 (danmaku_interaction)；
3. 一键紧急熔断 (Emergency Kill All)：当平台发生风控警告或故障时，秒级切断指定范围或全局功能；
4. 版本兼容与发布防劣化门禁：升级失败或模型清单哈希不符时，自动触发 Kill Switch 禁止开播。
"""

import time
import logging
from typing import Dict, Any, Optional, Tuple

logger = logging.getLogger("LiveAgent.KillSwitch")


class FeatureKillSwitchManager:
    """动态灰度与熔断控制器"""

    SUPPORTED_FEATURES = {
        "stream_push",          # 平台推流
        "avatar_rendering",     # 数字人视频渲染
        "voice_synthesis",      # TTS 语音合成
        "commerce_agent",       # 电商商品口播与弹窗促单
        "danmaku_interaction",  # 弹幕自动互动
    }

    def __init__(self):
        # 默认全部功能开启
        self._global_switches: Dict[str, bool] = {feat: True for feat in self.SUPPORTED_FEATURES}
        # 平台级覆盖：{platform: {feature: bool}}
        self._platform_switches: Dict[str, Dict[str, bool]] = {}
        # 主播级覆盖：{anchor_id: {feature: bool}}
        self._anchor_switches: Dict[str, Dict[str, bool]] = {}
        # 熔断历史操作审计日志
        self._audit_log: list = []

    def reset(self, *, keep_audit: bool = False) -> None:
        """恢复默认全开状态；测试和新场次隔离时使用，不绕过生产开关。"""
        self._global_switches = {feat: True for feat in self.SUPPORTED_FEATURES}
        self._platform_switches.clear()
        self._anchor_switches.clear()
        if not keep_audit:
            self._audit_log.clear()

    def is_feature_enabled(
        self,
        feature: str,
        platform: Optional[str] = None,
        anchor_id: Optional[str] = None,
    ) -> bool:
        """
        判断指定功能在当前上下文是否允许运行。
        匹配优先级：主播级覆盖 > 平台级覆盖 > 全局默认。
        """
        if feature not in self.SUPPORTED_FEATURES:
            return True

        # 1. 检查主播级别显式开关
        if anchor_id and anchor_id in self._anchor_switches:
            if feature in self._anchor_switches[anchor_id]:
                return self._anchor_switches[anchor_id][feature]

        # 2. 检查平台级别显式开关
        if platform and platform in self._platform_switches:
            if feature in self._platform_switches[platform]:
                return self._platform_switches[platform][feature]

        # 3. 回退至全局开关
        return self._global_switches.get(feature, True)

    def set_switch(
        self,
        feature: str,
        enabled: bool,
        platform: Optional[str] = None,
        anchor_id: Optional[str] = None,
        operator: str = "system",
        reason: str = "",
    ) -> Dict[str, Any]:
        """
        更新特定范围的 Feature Flag。
        """
        if feature not in self.SUPPORTED_FEATURES:
            raise ValueError(f"不支持的功能标识: {feature}，支持范围: {self.SUPPORTED_FEATURES}")

        scope = "global"
        target_id = ""

        if anchor_id:
            scope = "anchor"
            target_id = anchor_id
            if anchor_id not in self._anchor_switches:
                self._anchor_switches[anchor_id] = {}
            self._anchor_switches[anchor_id][feature] = enabled
        elif platform:
            scope = "platform"
            target_id = platform
            if platform not in self._platform_switches:
                self._platform_switches[platform] = {}
            self._platform_switches[platform][feature] = enabled
        else:
            self._global_switches[feature] = enabled

        record = {
            "timestamp": time.time(),
            "operator": operator,
            "feature": feature,
            "enabled": enabled,
            "scope": scope,
            "target_id": target_id,
            "reason": reason,
        }
        self._audit_log.append(record)
        logger.warning(
            "🔒【Feature Switch 变更】[%s] feature=%s -> %s (scope=%s, target=%s, operator=%s, reason=%s)",
            "ENABLED" if enabled else "DISABLED",
            feature,
            enabled,
            scope,
            target_id,
            operator,
            reason,
        )
        return record

    def emergency_kill_all(
        self,
        platform: Optional[str] = None,
        anchor_id: Optional[str] = None,
        operator: str = "admin",
        reason: str = "紧急风控熔断",
    ) -> Dict[str, Any]:
        """
        一键紧急切断全部功能 (Kill Switch)。
        """
        scope = "global"
        if anchor_id:
            scope = f"anchor:{anchor_id}"
            self._anchor_switches[anchor_id] = {feat: False for feat in self.SUPPORTED_FEATURES}
        elif platform:
            scope = f"platform:{platform}"
            self._platform_switches[platform] = {feat: False for feat in self.SUPPORTED_FEATURES}
        else:
            self._global_switches = {feat: False for feat in self.SUPPORTED_FEATURES}

        record = {
            "timestamp": time.time(),
            "operator": operator,
            "action": "EMERGENCY_KILL_ALL",
            "scope": scope,
            "reason": reason,
        }
        self._audit_log.append(record)
        logger.critical(
            "🚨🚨🚨【紧急熔断执行 KILL SWITCH】已切断全部功能！范围: %s, 操作人: %s, 理由: %s",
            scope,
            operator,
            reason,
        )
        return record

    def validate_and_enforce_version_manifest(
        self,
        anchor_id: str,
        platform: str,
        current_version: str,
        min_required_version: str,
        expected_model_hashes: Dict[str, str],
        actual_model_hashes: Dict[str, str],
    ) -> Tuple[bool, str]:
        """
        发布升级防劣化校验：若版本不匹配或模型哈希不符合预期，自动禁用该主播在对应平台的推流与渲染。
        """
        from packaging import version
        try:
            curr_v = version.parse(current_version)
            req_v = version.parse(min_required_version)
            if curr_v < req_v:
                reason = f"当前系统版本 {current_version} 低于商业发布准入最低版本 {min_required_version}"
                self.set_switch("stream_push", False, platform=platform, anchor_id=anchor_id, operator="version_guard", reason=reason)
                self.set_switch("avatar_rendering", False, platform=platform, anchor_id=anchor_id, operator="version_guard", reason=reason)
                return False, reason
        except Exception:
            # 简易字符串对比容错
            if current_version < min_required_version:
                reason = f"当前系统版本 {current_version} 未达到准入版本 {min_required_version}"
                self.set_switch("stream_push", False, platform=platform, anchor_id=anchor_id, operator="version_guard", reason=reason)
                return False, reason

        # 校验模型哈希完整性
        for model_name, expected_hash in expected_model_hashes.items():
            actual_hash = actual_model_hashes.get(model_name)
            if not actual_hash or actual_hash != expected_hash:
                reason = f"模型 {model_name} 哈希校验失败: 期望 {expected_hash}, 实际 {actual_hash}，存在被篡改或损坏风险"
                self.set_switch("stream_push", False, platform=platform, anchor_id=anchor_id, operator="integrity_guard", reason=reason)
                self.set_switch("avatar_rendering", False, platform=platform, anchor_id=anchor_id, operator="integrity_guard", reason=reason)
                return False, reason

        return True, "版本与模型哈希完整性校验通过"

    def get_switches_status(self) -> Dict[str, Any]:
        """获取全量开关与审计日志快照"""
        return {
            "global": self._global_switches,
            "platform_overrides": self._platform_switches,
            "anchor_overrides": self._anchor_switches,
            "audit_log": self._audit_log[-20:],
        }


# 全局单例
global_kill_switch = FeatureKillSwitchManager()
