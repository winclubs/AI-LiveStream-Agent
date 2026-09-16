"""Avatar Provider 显式注册表、配置 schema 与安全 factory。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Literal, Mapping
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from server.adapters.media.avatar_provider import RemoteAvatarProvider
from server.adapters.media.avatar_provider_config import (
    find_sensitive_paths,
    parse_avatar_provider_policy,
)


class AvatarProviderAvailability(StrEnum):
    """Provider 在管理后台中的真实交付状态。"""

    AVAILABLE = "available"
    EXPERIMENTAL = "experimental"
    PLANNED = "planned"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _TimeoutsConfig(_StrictModel):
    connect_ms: int = Field(default=1000, ge=100, le=60_000)
    message_ms: int = Field(default=20_000, ge=100, le=300_000)
    request_ms: int = Field(default=120_000, ge=100, le=900_000)


class _CircuitBreakerConfig(_StrictModel):
    failure_threshold: int = Field(default=3, ge=1, le=100)
    open_ms: int = Field(default=30_000, ge=100, le=3_600_000)
    half_open_max_calls: int = Field(default=1, ge=1, le=1)


class _QuotaConfig(_StrictModel):
    currency: str = Field(default="CNY", min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")
    budget_minor: int | None = Field(default=None, ge=0)
    warning_ratio: float = Field(default=0.8, ge=0.0, le=1.0)
    hard_limit: bool = True
    billing_unit: Literal["request", "audio_second", "audio_minute", "character"] = "request"
    unit_cost_minor: int = Field(default=0, ge=0)
    charge_failed_attempts: bool = True


class _SidecarPolicyConfig(_StrictModel):
    mode: Literal["primary", "fallback", "shadow", "disabled"] = "primary"
    priority: int = Field(default=100, ge=-1_000_000, le=1_000_000)
    max_concurrency: Literal[1] = 1
    render_mode: Literal["realtime"] = "realtime"
    timeouts: _TimeoutsConfig = Field(default_factory=_TimeoutsConfig)
    circuit_breaker: _CircuitBreakerConfig = Field(default_factory=_CircuitBreakerConfig)
    quota: _QuotaConfig = Field(default_factory=_QuotaConfig)


class SidecarV3Config(_StrictModel):
    """`extra_params` 的规范形态；凭据只能放在加密 api_key 字段。"""

    adapter: Literal["sidecar_v3"]
    avatar_provider: _SidecarPolicyConfig = Field(default_factory=_SidecarPolicyConfig)
    title: str | None = Field(default=None, min_length=1, max_length=128)
    backend_id: str = Field(default="auto", min_length=1, max_length=128)
    avatar_id: str = Field(default="default", min_length=1, max_length=128)
    avatar_revision: str = Field(default="", max_length=128)
    avatar_digest: str = Field(default="", max_length=256)
    license_manifest_digest: str = Field(default="", max_length=256)
    weights_sha256: str = Field(default="", max_length=128)
    model_version: str = Field(default="", max_length=128)
    require_neural_lipsync: bool = True
    custom_official_url: str = Field(default="", max_length=1024)


class LiveAvatarLiteConfig(_StrictModel):
    """LiveAvatar LITE 配置；会话与 LiveKit Token 只允许驻留内存。"""

    adapter: Literal["liveavatar_lite"]
    avatar_provider: _SidecarPolicyConfig = Field(default_factory=_SidecarPolicyConfig)
    title: str | None = Field(default=None, min_length=1, max_length=128)
    sandbox_only: bool = False
    avatar_id: str = Field(
        default="dd73ea75-1218-4ef3-92ce-606d5f7fbc0a",
        min_length=1,
        max_length=128,
    )
    max_session_duration: int = Field(default=60, ge=30, le=60)
    keep_alive_seconds: int = Field(default=30, ge=10, le=50)
    custom_official_url: str = Field(default="", max_length=1024)


class AliyunAvatarConfig(_StrictModel):
    """阿里云万相配置；RTC 初始化材料只允许驻留加密 credential bundle。"""

    adapter: Literal["aliyun_avatar"]
    avatar_provider: _SidecarPolicyConfig = Field(default_factory=_SidecarPolicyConfig)
    title: str | None = Field(default=None, min_length=1, max_length=128)
    sandbox_only: bool = False
    expected_sdk_sha256: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-fA-F]{64}$",
    )
    custom_official_url: str = Field(default="", max_length=1024)


class TencentAvatarConfig(_StrictModel):
    """腾讯云智能数智人配置；AppKey/AccessToken 只允许驻留加密 credential bundle。"""

    adapter: Literal["tencent_avatar"]
    avatar_provider: _SidecarPolicyConfig = Field(default_factory=_SidecarPolicyConfig)
    title: str | None = Field(default=None, min_length=1, max_length=128)
    sandbox_only: bool = False
    virtualman_project_id: str = Field(min_length=1, max_length=256)
    protocol: Literal["rtmp", "webrtc"] = "rtmp"
    user_id: str = Field(
        default="las_agent_ivh_experimental",
        min_length=1,
        max_length=64,
    )
    stream_max_interval: int = Field(default=2000, ge=2000, le=6000)
    heartbeat_interval_seconds: int = Field(default=40, ge=31, le=59)
    custom_official_url: str = Field(default="", max_length=1024)


class SSHConfig(_StrictModel):
    """SSH 隧道配置；用于安全连接云主机上的远程渲染服务。"""

    enabled: bool = Field(default=False)
    host: str | None = Field(default=None, max_length=256)
    port: int = Field(default=22, ge=1, le=65535)
    username: str = Field(default="root", max_length=64)
    password: str | None = Field(default=None)
    private_key: str | None = Field(default=None)
    public_key_host: bool = Field(default=True)  # 自动添加未知主机密钥


class CustomAvatarConfig(_StrictModel):
    """自定义数字人 / 远端流服务配置；支持任意通用服务节点与流协议。

    支持通过 SSH 隧道安全连接云主机上的渲染服务。
    """

    adapter: Literal["custom_avatar"]
    avatar_provider: _SidecarPolicyConfig = Field(default_factory=_SidecarPolicyConfig)
    title: str | None = Field(default=None, min_length=1, max_length=128)
    sandbox_only: bool = False
    stream_protocol: str = Field(default="websocket", max_length=32)
    avatar_id: str = Field(default="default", max_length=128)
    custom_official_url: str = Field(default="", max_length=1024)

    # SSH 隧道配置（可选）
    ssh_config: SSHConfig = Field(default_factory=SSHConfig)


@dataclass(frozen=True, slots=True)
class AvatarProviderDescriptor:
    adapter_id: str
    display_name: str
    availability: AvatarProviderAvailability
    description: str
    credential_mode: Literal["none", "single_secret", "credential_bundle"]
    configuration_mode: Literal["builtin", "instance"]
    config_model: type[BaseModel] | None = None
    capabilities: Mapping[str, Any] | None = None
    ui_fields: tuple[Mapping[str, Any], ...] = ()
    tagline: str = ""
    target_audience: str = ""
    visual_style: str = ""
    cost_hardware: str = ""
    plain_explanation: str = ""
    official_url: str = ""

    @property
    def selectable(self) -> bool:
        # 支持 available 及已打通云端直推能力的 provider
        return self.availability in (
            AvatarProviderAvailability.AVAILABLE,
            AvatarProviderAvailability.EXPERIMENTAL,
        )

    def to_public_dict(self) -> dict[str, Any]:
        schema = self.config_model.model_json_schema() if self.config_model else None
        return {
            "id": self.adapter_id,
            "name": self.display_name,
            "status": self.availability.value,
            "selectable": self.selectable,
            "implemented": self.availability is not AvatarProviderAvailability.PLANNED,
            "experimental": self.availability is AvatarProviderAvailability.EXPERIMENTAL,
            "description": self.description,
            "credential_mode": self.credential_mode,
            "configuration_mode": self.configuration_mode,
            "config_schema": schema,
            "capabilities": dict(self.capabilities or {}),
            "ui_fields": [dict(field) for field in self.ui_fields],
            "tagline": self.tagline,
            "target_audience": self.target_audience,
            "visual_style": self.visual_style,
            "cost_hardware": self.cost_hardware,
            "plain_explanation": self.plain_explanation,
            "official_url": self.official_url,
        }


_PROVIDER_REGISTRY = MappingProxyType(
    {
        "local_procedural": AvatarProviderDescriptor(
            adapter_id="local_procedural",
            display_name="选项 1 · 本地轻量卡通形象（新手首选·零门槛）",
            availability=AvatarProviderAvailability.AVAILABLE,
            tagline="零配置 · 免显卡 · 0元免费开箱即用",
            description="最省心、最稳定的推荐方案！由您当前电脑直接生成卡通风格播报画面，不需要任何外部服务器、显卡或账号。同时它是系统的“永久安全保底”——即使未来配置了远端真人渲染，网络闪退也会自动无缝切回本地画面，直播绝不黑屏卡死。",
            target_audience="小白新手试播、轻量带货、普通笔记本或办公电脑用户（强烈推荐）",
            visual_style="2D 灵动卡通主播画面，伴随声音自然眨眼、开口说话",
            cost_hardware="无需独立显卡，任意普通电脑即可流畅运行，0元完全免费",
            plain_explanation="💡 为什么推荐新手先选这个？它就像开箱即用的“安全驾驶模式”，电脑无需任何高性能显卡，打开就能直接开播；并且后续就算你换了更高级的真人服务，系统也会把它作为暗中守护的备用画面，一旦网络中断自动顶上，保障直播不冷场。",
            credential_mode="none",
            configuration_mode="builtin",
            capabilities={"renderer_only": True, "local": True, "shadow_always_on": True},
        ),
        "sidecar_v3": AvatarProviderDescriptor(
            adapter_id="sidecar_v3",
            display_name="选项 2 · 自建云端渲染节点（Sidecar 真人高保真·需独立显卡）",
            availability=AvatarProviderAvailability.AVAILABLE,
            tagline="真人画质 · 口型精准对齐 · 需独立显卡算力",
            description="高级进阶方案：连接您自备的高性能显卡电脑或租用的 GPU 云服务器（需运行 Sidecar 渲染程序），把主播声音实时合成为口型逼真的真人视频画面。适合有独显算力、追求真人质感的用户；普通小白如果没有准备显卡服务器，请直接选上面的本地卡通形象。",
            target_audience="拥有高性能独显电脑（推荐 RTX 3060 6GB+）或租用 GPU 云服务器的专业直播团队",
            visual_style="高清真人形象视频，嘴唇动作与声音发音逐帧严格对齐",
            cost_hardware="需配备独立显卡或按小时支付云服务器租赁费，并需在显卡机器上启动渲染程序",
            plain_explanation="💡 什么是“自建云端渲染 / 神经渲染 Sidecar v3”？大白话来说：让AI生成口型逼真的“真人级视频”非常消耗显卡算力。所谓“Sidecar（伴侣外挂节点）”，就是把这个非常吃显卡的“画面合成”工作，外包给一台带有强劲独立显卡的电脑（或专门租用的 GPU 云服务器）去计算，计算完成后把高清真人视频实时传回这里。普通小白若未准备显卡服务器，请直接选上面的【选项 1 · 本地轻量卡通形象】。",
            credential_mode="single_secret",
            configuration_mode="instance",
            config_model=SidecarV3Config,
            capabilities={
                "renderer_only": True,
                "render_modes": ["realtime"],
                "input_codecs": ["pcm_s16le"],
                "max_safe_concurrency": 1,
            },
            ui_fields=(
                {
                    "path": "base_url",
                    "label": "渲染节点连接地址 (WebSocket):",
                    "type": "url",
                    "required": True,
                    "tip": "正在运行渲染程序的显卡电脑或云端 GPU 地址。本机有独显可填默认地址；无独显可在 AutoDL 等平台按小时租用 GPU（约1.2元/小时）填入公网 IP。",
                    "pills": [
                        {"text": "本机独显: ws://127.0.0.1:8010/ws/render-v3", "val": "ws://127.0.0.1:8010/ws/render-v3"},
                        {"text": "云端GPU示例: ws://<云主机IP>:8010/ws/render-v3", "val": "ws://<云主机IP>:8010/ws/render-v3"}
                    ],
                },
                {
                    "path": "api_key",
                    "label": "访问密码 / Token (选填):",
                    "type": "secret",
                    "required": False,
                    "tip": "渲染服务端启动时设置的安全口令（AUTH_TOKEN），防止外人随意连接占用您的显卡。若服务端未设密码，直接留空即可。",
                },
                {
                    "path": "extra_params.backend_id",
                    "label": "渲染引擎代号 (后端 ID):",
                    "type": "text",
                    "default": "auto",
                    "tip": "渲染程序使用的算法引擎编号。普通用户直接保持默认 auto 即可，系统会自动握手识别。",
                    "pills": [
                        {"text": "默认: auto", "val": "auto"}
                    ],
                },
                {
                    "path": "extra_params.avatar_id",
                    "label": "数字人形象代号 (Avatar ID):",
                    "type": "text",
                    "default": "default",
                    "tip": "您在渲染端准备的形象素材编号。默认填 default；若部署了多个数字人形象可填具体代号。",
                    "pills": [
                        {"text": "默认: default", "val": "default"}
                    ],
                },
                {
                    "path": "extra_params.custom_official_url",
                    "label": "自定义官方/控制台链接 (选填):",
                    "type": "url",
                    "tip": "可填入您的私有部署地址、自定义控制台或项目文档链接。填写后卡片右上角的入口将优先跳转至此处；留空默认使用官方链接。",
                },
            ),
            official_url="https://www.autodl.com",
        ),
        "liveavatar_lite": AvatarProviderDescriptor(
            adapter_id="liveavatar_lite",
            display_name="选项 3 · LiveAvatar 开放平台（云端真人）",
            availability=AvatarProviderAvailability.AVAILABLE,
            tagline="商业数字人 SaaS · 云端画面直推 · 免本地显卡",
            description="对接 LiveAvatar 官方商业数字人平台：由云端机房完成声画实时合成并直接提供 WebRTC/RTMP 流，本地无需独立显卡，适合已开通 LiveAvatar 商业授权的用户。",
            target_audience="拥有 LiveAvatar 商业开发者账号与 API Key 的直播团队",
            visual_style="商业级云端真人数字人超写实视频",
            cost_hardware="需购买 LiveAvatar 商业 API 服务，本地普通办公电脑即可开播",
            plain_explanation="💡 运作机制：视频画面完全在 LiveAvatar 云端合成，通过云端流地址直接输入 OBS，本地电脑 0 显存占用。",
            credential_mode="single_secret",
            configuration_mode="instance",
            config_model=LiveAvatarLiteConfig,
            capabilities={
                "renderer_only": True,
                "render_modes": ["realtime"],
                "input_codecs": ["pcm_s16le_24000_mono"],
                "verification": "verified",
                "sandbox_only": False,
                "local_video_output": True,
                "supports_sample_pts": False,
                "max_safe_concurrency": 1,
            },
            ui_fields=(
                {
                    "path": "base_url",
                    "label": "LiveAvatar API 地址:",
                    "type": "url",
                    "required": True,
                    "default": "https://api.liveavatar.com",
                    "tip": "LiveAvatar 官方 API 网关地址。",
                    "pills": [
                        {"text": "官方默认: https://api.liveavatar.com", "val": "https://api.liveavatar.com"}
                    ],
                },
                {"path": "api_key", "label": "LiveAvatar API Key:", "type": "secret", "required": True, "tip": "在 LiveAvatar 开放平台申请的调用密钥。"},
                {
                    "path": "extra_params.avatar_id",
                    "label": "数字人形象 ID (Avatar ID):",
                    "type": "text",
                    "default": "dd73ea75-1218-4ef3-92ce-606d5f7fbc0a",
                    "tip": "您在 LiveAvatar 后台配置的数字人形象编号。",
                    "pills": [
                        {"text": "默认示例形象", "val": "dd73ea75-1218-4ef3-92ce-606d5f7fbc0a"}
                    ],
                },
                {
                    "path": "extra_params.custom_official_url",
                    "label": "自定义官方/控制台链接 (选填):",
                    "type": "url",
                    "tip": "可填入您的私有部署地址、自定义控制台或项目文档链接。填写后卡片右上角的入口将优先跳转至此处；留空默认使用官方链接。",
                },
            ),
            official_url="https://liveavatar.com",
        ),
        "aliyun_avatar": AvatarProviderDescriptor(
            adapter_id="aliyun_avatar",
            display_name="选项 4 · 阿里云万相数字人（大厂官方）",
            availability=AvatarProviderAvailability.AVAILABLE,
            tagline="阿里云官方超写实 · 云端直推RTMP · 本地免显卡",
            description="对接阿里云万相官方超写实数字人服务：由阿里机房完成实时口型声画合成，直接输出 RTMP/RTC 直播流给直播间或 OBS，本地电脑完全不占显卡。",
            target_audience="拥有阿里云企业商用账号与万相数字人资产的品牌直播间",
            visual_style="阿里云万相超写实逼真真人视频",
            cost_hardware="需阿里云企业商用授权，本地普通电脑畅跑",
            plain_explanation="💡 运作机制：阿里机房合成真人画面并直推 RTMP，本地只需负责控制发音指令，无任何显卡门槛。",
            credential_mode="credential_bundle",
            configuration_mode="instance",
            config_model=AliyunAvatarConfig,
            capabilities={
                "renderer_only": True,
                "render_modes": ["realtime"],
                "input_codecs": ["pcm_s16le"],
                "verification": "verified",
                "sandbox_only": False,
                "media_transport": "aliyun_rtc_via_websdk",
                "local_video_output": True,
                "supports_sample_pts": False,
                "strict_completion": False,
                "max_safe_concurrency": 1,
            },
            ui_fields=(
                {
                    "path": "base_url",
                    "label": "本地 WebSDK Bridge 地址:",
                    "type": "url",
                    "required": True,
                    "default": "wss://127.0.0.1:8020/ws/aliyun-bridge",
                    "tip": "阿里云 WebSDK 本地中转服务通信地址。",
                    "pills": [
                        {"text": "本机默认: wss://127.0.0.1:8020/ws/aliyun-bridge", "val": "wss://127.0.0.1:8020/ws/aliyun-bridge"}
                    ],
                },
                {"path": "api_key", "label": "凭据包 Credential Bundle (JSON):", "type": "secret", "required": True, "tip": "包含阿里云 AccessKey 与 Token 的加密凭据包。"},
                {"path": "extra_params.expected_sdk_sha256", "label": "WebSDK SHA-256 校验指纹:", "type": "text", "required": True, "tip": "官方 SDK 文件的指纹校验值。"},
                {
                    "path": "extra_params.custom_official_url",
                    "label": "自定义官方/控制台链接 (选填):",
                    "type": "url",
                    "tip": "可填入您的私有部署地址、自定义控制台或项目文档链接。填写后卡片右上角的入口将优先跳转至此处；留空默认使用官方链接。",
                },
            ),
            official_url="https://www.aliyun.com/product/ai/avatar",
        ),
        "tencent_avatar": AvatarProviderDescriptor(
            adapter_id="tencent_avatar",
            display_name="选项 5 · 腾讯云智能数智人（大厂官方）",
            availability=AvatarProviderAvailability.AVAILABLE,
            tagline="腾讯云 IVH 官方数智人 · 机房渲染直推RTMP · 免本地显卡",
            description="对接腾讯云官方智能数智人（IVH）交互服务：声画合成完全在腾讯云机房运行，并生成 RTMP 直播流直接供 OBS 拉流或推流至直播间，彻底解决低配显卡直播痛点。",
            target_audience="拥有腾讯云商用账号与数智人并发授权的企业与个人",
            visual_style="腾讯云 IVH 互动数智人高保真视频",
            cost_hardware="按腾讯云数智人并发时长计费，本地办公本零负担",
            plain_explanation="💡 运作机制：腾讯云在云端合成高清视频并生成 PlayStreamAddr（RTMP 播放流），在 OBS 中添加媒体源即可开播，本地 0 显存占用！",
            credential_mode="credential_bundle",
            configuration_mode="instance",
            config_model=TencentAvatarConfig,
            capabilities={
                "renderer_only": True,
                "render_modes": ["realtime"],
                "input_codecs": ["pcm_s16le"],
                "verification": "verified",
                "sandbox_only": False,
                "media_transport": "tencent_ivh_apaas",
                "audio_requirement": "pcm_s16le_16000_mono",
                "local_video_output": True,
                "supports_sample_pts": False,
                "strict_completion": False,
                "max_safe_concurrency": 1,
            },
            ui_fields=(
                {
                    "path": "base_url",
                    "label": "腾讯云 IVH 网关地址:",
                    "type": "url",
                    "required": True,
                    "default": "https://gw.tvs.qq.com",
                    "tip": "腾讯云数智人 IVH API 网关地址，默认使用官方网关。",
                    "pills": [
                        {"text": "腾讯云官方默认: https://gw.tvs.qq.com", "val": "https://gw.tvs.qq.com"}
                    ],
                },
                {"path": "api_key", "label": "凭据包 Credential Bundle (JSON):", "type": "secret", "required": True, "tip": "包含腾讯云 AppKey 和 Secret 的加密凭据包。"},
                {"path": "extra_params.virtualman_project_id", "label": "数智人项目 ID (Project ID):", "type": "text", "required": True, "tip": "在腾讯云控制台开通的数智人项目编号。"},
                {
                    "path": "extra_params.protocol",
                    "label": "流协议 (RTMP / WebRTC):",
                    "type": "text",
                    "default": "rtmp",
                    "tip": "媒体流传输协议，直播推荐填 rtmp。",
                    "pills": [
                        {"text": "直播推荐: rtmp", "val": "rtmp"},
                        {"text": "低延时: webrtc", "val": "webrtc"}
                    ],
                },
                {
                    "path": "extra_params.custom_official_url",
                    "label": "自定义官方/控制台链接 (选填):",
                    "type": "url",
                    "tip": "可填入您的私有部署地址、自定义控制台或项目文档链接。填写后卡片右上角的入口将优先跳转至此处；留空默认使用官方链接。",
                },
            ),
            official_url="https://cloud.tencent.com/product/ivh",
        ),
        "custom_avatar": AvatarProviderDescriptor(
            adapter_id="custom_avatar",
            display_name="选项 6 · 自定义数字人 / 远端流服务（灵活接入）",
            availability=AvatarProviderAvailability.AVAILABLE,
            tagline="开放协议 · 自由直连 · 支持 WebSocket / RTMP / WebRTC",
            description="面向开发者与进阶用户的开放接入方案：对接您自建的数字人渲染节点、第三方未预置的流媒体服务、私有 RTMP 直播流或自定义 WebSocket 网关。只需填入您的服务通信地址与凭据即可连通开播。",
            target_audience="拥有自研数字人系统、私有 GPU 云机房或自定义推拉流地址的高级用户与开发者",
            visual_style="由您自定义的外部服务或自建数字人机房实时合成推送",
            cost_hardware="视您自建机器配置或第三方服务商规则而定，本地电脑零显存负担",
            plain_explanation="💡 什么是“自定义接入”？如果您有自己开发的数字人服务、或者使用了其他厂商的专用推拉流地址，可以直接在这里填入您的服务链接与密钥，系统将通过统一协议为您桥接开播！",
            credential_mode="single_secret",
            configuration_mode="instance",
            config_model=CustomAvatarConfig,
            capabilities={
                "renderer_only": True,
                "render_modes": ["realtime"],
                "input_codecs": ["pcm_s16le"],
                "max_safe_concurrency": 1,
            },
            ui_fields=(
                {
                    "path": "base_url",
                    "label": "自定义服务/流连接地址 (WebSocket / RTMP / HTTP):",
                    "type": "url",
                    "required": True,
                    "default": "ws://127.0.0.1:8010/ws/render-v3",
                    "tip": "您的自建数字人渲染服务、私有网关或 RTMP 流地址（支持 ws://, wss://, http://, https://, rtmp://）。",
                    "pills": [
                        {"text": "本地WS: ws://127.0.0.1:8010/ws/render-v3", "val": "ws://127.0.0.1:8010/ws/render-v3"},
                        {"text": "RTMP流: rtmp://127.0.0.1:1935/live/avatar", "val": "rtmp://127.0.0.1:1935/live/avatar"},
                        {"text": "远程WSS: wss://my-gpu-node.com/ws/render", "val": "wss://my-gpu-node.com/ws/render"}
                    ],
                },
                {
                    "path": "api_key",
                    "label": "访问密码 / Token / API Key (选填):",
                    "type": "secret",
                    "required": False,
                    "tip": "连接您自定义服务端所需的安全认证密钥，系统将加密存储；无鉴权可直接留空。",
                },
                {
                    "path": "extra_params.stream_protocol",
                    "label": "通信协议类型:",
                    "type": "text",
                    "default": "websocket",
                    "tip": "与自定义服务通信所使用的协议（支持 websocket、rtmp、webrtc、http）。",
                    "pills": [
                        {"text": "WebSocket", "val": "websocket"},
                        {"text": "RTMP直播流", "val": "rtmp"},
                        {"text": "WebRTC低延时", "val": "webrtc"},
                        {"text": "HTTP接口", "val": "http"}
                    ],
                },
                {
                    "path": "extra_params.avatar_id",
                    "label": "数字人形象代号 (Avatar ID):",
                    "type": "text",
                    "default": "default",
                    "tip": "向自定义服务请求的数字人形象标识符，默认 default。",
                    "pills": [
                        {"text": "默认: default", "val": "default"}
                    ],
                },
                {
                    "path": "extra_params.custom_official_url",
                    "label": "自定义官方/控制台链接 (选填):",
                    "type": "url",
                    "tip": "可填入您的私有部署地址、自定义控制台或项目文档链接，将在卡片右上角显示快捷跳转入口。",
                },
            ),
            official_url="https://github.com/winclubs/AI-LiveStream-Agent",
        ),
    }
)


def list_avatar_provider_descriptors() -> list[AvatarProviderDescriptor]:
    """返回按注册键稳定排序的只读 descriptor。"""
    return [_PROVIDER_REGISTRY[key] for key in sorted(_PROVIDER_REGISTRY)]


def get_avatar_provider_descriptor(adapter_id: str) -> AvatarProviderDescriptor:
    normalized = str(adapter_id).strip().lower()
    descriptor = _PROVIDER_REGISTRY.get(normalized)
    if descriptor is None:
        raise ValueError(f"未注册的 Avatar Provider adapter={normalized!r}")
    return descriptor


def normalize_avatar_provider_config(
    extra_params: Mapping[str, Any],
    *,
    base_url: str | None,
    credential_present: bool | None = None,
) -> dict[str, Any]:
    """严格校验并返回可完整替换持久化值的规范配置。"""
    if not isinstance(extra_params, Mapping):
        raise ValueError("extra_params 必须是 object")
    sensitive_paths = find_sensitive_paths(extra_params)
    if sensitive_paths:
        raise ValueError(
            "扩展参数禁止保存敏感字段，请使用 api_key: " + ", ".join(sensitive_paths[:5])
        )
    adapter_id = str(extra_params.get("adapter") or "").strip().lower()
    if not adapter_id:
        raise ValueError("extra_params.adapter 不能为空")
    descriptor = get_avatar_provider_descriptor(adapter_id)
    if descriptor.availability is AvatarProviderAvailability.PLANNED:
        raise ValueError(f"Avatar Provider {adapter_id!r} 仍为计划支持，当前不可启用")
    if descriptor.configuration_mode != "instance" or descriptor.config_model is None:
        raise ValueError(f"Avatar Provider {adapter_id!r} 由系统内置管理，不能保存为远端实例")
    try:
        normalized = descriptor.config_model.model_validate(dict(extra_params), strict=True)
    except ValidationError as exc:
        first = exc.errors(include_url=False)[0]
        path = ".".join(str(part) for part in first.get("loc", ())) or "extra_params"
        raise ValueError(f"Avatar Provider 配置无效：{path}: {first.get('msg', '校验失败')}") from exc

    if base_url is not None:
        endpoint = str(base_url).strip()
        parsed = urlparse(endpoint)
        hostname = (parsed.hostname or "").lower()
        if adapter_id == "sidecar_v3":
            loopback = hostname in {"localhost", "127.0.0.1", "::1"}
            if parsed.scheme not in {"ws", "wss"} or not hostname:
                raise ValueError("sidecar_v3 base_url 必须是合法 ws:// 或 wss:// 地址")
            if parsed.scheme == "ws" and not loopback:
                raise ValueError("非本机 sidecar_v3 必须使用 wss://，禁止明文传输媒体和凭据")
            if credential_present is False and not loopback:
                raise ValueError("远程 sidecar_v3 必须通过 api_key 配置鉴权 Token")
        elif adapter_id == "liveavatar_lite":
            if (
                parsed.scheme != "https"
                or hostname != "api.liveavatar.com"
                or parsed.port not in {None, 443}
                or parsed.path.rstrip("/")
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError(
                    "liveavatar_lite base_url 仅允许 https://api.liveavatar.com"
                )
            if credential_present is False:
                raise ValueError("LiveAvatar LITE 必须通过 api_key 配置 API Key")
        elif adapter_id == "aliyun_avatar":
            # bridge 是本机私有 IPC，只允许 loopback wss；凭据必须整体驻留加密字段。
            if (
                parsed.scheme != "wss"
                or hostname not in {"localhost", "127.0.0.1", "::1"}
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError(
                    "aliyun_avatar base_url 仅允许本机 loopback wss:// WebSDK bridge 地址"
                )
            if credential_present is False:
                raise ValueError(
                    "aliyun_avatar 必须通过 api_key 配置 credential bundle JSON"
                )
        elif adapter_id == "tencent_avatar":
            # 只允许官方网关，防止凭据被重定向到任意主机。
            if (
                parsed.scheme != "https"
                or hostname != "gw.tvs.qq.com"
                or parsed.port not in {None, 443}
                or parsed.path.rstrip("/")
                or parsed.query
                or parsed.fragment
                or parsed.username
                or parsed.password
            ):
                raise ValueError(
                    "tencent_avatar base_url 仅允许官方网关 https://gw.tvs.qq.com"
                )
            if credential_present is False:
                raise ValueError(
                    "tencent_avatar 必须通过 api_key 配置 credential bundle JSON"
                )
        elif adapter_id == "custom_avatar":
            if parsed.scheme not in {"ws", "wss", "http", "https", "rtmp", "rtmps"} or not hostname:
                raise ValueError(
                    "custom_avatar base_url 必须是合法的 ws://, wss://, http://, https:// 或 rtmp:// 地址"
                )
        else:  # pragma: no cover - 已实现 adapter 必须显式声明 endpoint 规则
            raise ValueError(f"Avatar Provider {adapter_id!r} 缺少 endpoint 校验规则")

    canonical = normalized.model_dump(mode="json", exclude_none=False)
    parse_avatar_provider_policy(canonical, max_safe_concurrency=1)
    return canonical


def create_avatar_provider(
    *,
    provider_instance_id: str,
    display_name: str,
    base_url: str,
    credential: str,
    model_name: str,
    extra_params: Mapping[str, Any],
    purpose: Literal["live", "sandbox"] = "live",
) -> tuple[RemoteAvatarProvider, Any]:
    """从显式 allowlist 构造 Provider；实验适配器只能用于显式 sandbox。"""
    canonical = normalize_avatar_provider_config(
        extra_params,
        base_url=base_url,
        credential_present=bool(str(credential).strip()),
    )
    adapter_id = canonical["adapter"]
    descriptor = get_avatar_provider_descriptor(adapter_id)

    policy = parse_avatar_provider_policy(canonical, max_safe_concurrency=1)
    if adapter_id == "sidecar_v3":
        from server.adapters.media.neural_sidecar_driver import NeuralSidecarMediaDriver
        from server.adapters.media.neural_sidecar_provider import (
            NeuralSidecarAvatarProvider,
        )

        driver = NeuralSidecarMediaDriver(
            node_url=base_url,
            auth_token=credential,
            backend_id=str(canonical.get("backend_id") or model_name or "auto"),
            avatar_id=str(canonical.get("avatar_id") or "default"),
            avatar_revision=str(canonical.get("avatar_revision") or ""),
            avatar_digest=str(canonical.get("avatar_digest") or ""),
            license_manifest_digest=str(canonical.get("license_manifest_digest") or ""),
            weights_sha256=str(canonical.get("weights_sha256") or ""),
            model_version=str(canonical.get("model_version") or ""),
            require_neural_lipsync=canonical.get("require_neural_lipsync") is True,
            connect_timeout=policy.timeouts.connect_seconds,
            message_timeout=policy.timeouts.message_seconds,
            request_timeout=policy.timeouts.request_seconds,
        )
        provider: RemoteAvatarProvider = NeuralSidecarAvatarProvider(
            provider_id=f"neural_renderer:{provider_instance_id}",
            display_name=display_name,
            driver=driver,
        )
        return provider, policy

    if adapter_id == "liveavatar_lite":
        from server.adapters.media.liveavatar_lite_provider import (
            LiveAvatarLiteAvatarProvider,
        )

        provider = LiveAvatarLiteAvatarProvider(
            provider_id=f"neural_renderer:{provider_instance_id}",
            display_name=display_name,
            api_base_url=base_url,
            api_key=credential,
            avatar_id=str(canonical["avatar_id"]),
            sandbox_only=canonical["sandbox_only"] is True,
            max_session_duration=int(canonical["max_session_duration"]),
            keep_alive_seconds=float(canonical["keep_alive_seconds"]),
            connect_timeout=policy.timeouts.connect_seconds,
            message_timeout=policy.timeouts.message_seconds,
            request_timeout=policy.timeouts.request_seconds,
        )
        return provider, policy

    if adapter_id == "aliyun_avatar":
        from server.adapters.media.aliyun_avatar_provider import AliyunAvatarProvider

        # 构造即解析并校验 credential bundle；无效 JSON 会在装配阶段失败，
        # 而不是等到直播运行时才发现。
        aliyun_provider = AliyunAvatarProvider(
            provider_id=f"neural_renderer:{provider_instance_id}",
            display_name=display_name,
            bridge_url=base_url,
            credential_bundle=credential,
            expected_sdk_sha256=str(canonical["expected_sdk_sha256"]),
            connect_timeout=policy.timeouts.connect_seconds,
            message_timeout=policy.timeouts.message_seconds,
            request_timeout=policy.timeouts.request_seconds,
        )
        return aliyun_provider, policy

    if adapter_id == "tencent_avatar":
        from server.adapters.media.tencent_avatar_provider import (
            TencentIVHAvatarProvider,
        )

        # 构造即解析并校验 credential bundle；凭据格式错误在装配阶段失败。
        tencent_provider = TencentIVHAvatarProvider(
            provider_id=f"neural_renderer:{provider_instance_id}",
            display_name=display_name,
            api_base_url=base_url,
            credential_bundle=credential,
            virtualman_project_id=str(canonical["virtualman_project_id"]),
            user_id=str(canonical["user_id"]),
            protocol=str(canonical["protocol"]),
            stream_max_interval=int(canonical["stream_max_interval"]),
            heartbeat_interval_seconds=int(canonical["heartbeat_interval_seconds"]),
            connect_timeout=policy.timeouts.connect_seconds,
            message_timeout=policy.timeouts.message_seconds,
            request_timeout=policy.timeouts.request_seconds,
        )
        return tencent_provider, policy

    if adapter_id == "custom_avatar":
        from server.adapters.media.neural_sidecar_driver import NeuralSidecarMediaDriver
        from server.adapters.media.neural_sidecar_provider import (
            NeuralSidecarAvatarProvider,
        )

        # 提取 SSH 配置
        ssh_config = canonical.get("ssh_config", {})
        # 将 SSHConfig 对象转换为 dict（如果存在）
        if hasattr(ssh_config, "model_dump"):
            ssh_config = ssh_config.model_dump()

        driver = NeuralSidecarMediaDriver(
            node_url=base_url,
            auth_token=credential,
            backend_id=str(canonical.get("stream_protocol") or model_name or "auto"),
            avatar_id=str(canonical.get("avatar_id") or "default"),
            ssh_config=ssh_config if ssh_config else None,
            connect_timeout=policy.timeouts.connect_seconds,
            message_timeout=policy.timeouts.message_seconds,
            request_timeout=policy.timeouts.request_seconds,
        )
        provider: RemoteAvatarProvider = NeuralSidecarAvatarProvider(
            provider_id=f"neural_renderer:{provider_instance_id}",
            display_name=display_name,
            driver=driver,
        )
        return provider, policy

    raise ValueError(f"Avatar Provider {adapter_id!r} 尚无可用 factory")

