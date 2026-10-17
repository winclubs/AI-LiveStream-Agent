# -*- coding: utf-8 -*-
"""
统一生产开播门禁与质量预检服务 (server.core.operations.preflight)
依据《商业化运营改造方案》v3.0.0 第四章制定。

核心原则：
1. 区分 DEMO / PREVIEW / PILOT / PRODUCTION 运行档位；
2. 任何严重级别为 BLOCK 的检查项未通过时，必须直接 Fail-Closed 阻断开播；
3. 输出带时间戳、有效期、代码/模型摘要的机器可读结构化诊断报告；
4. 杜绝假唇形、杜绝未授权音色、杜绝纯 CPU 跑商业直播。
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

from server.config import DATA_DIR

logger = logging.getLogger("LiveAgent.Preflight")


class ExecutionMode(str, Enum):
    """运行档位"""
    DEMO = "DEMO"                      # 开发/离线演示，允许Mock与程序化
    PREVIEW = "PREVIEW"                # 本地预览/资产验收，禁止平台推流
    PILOT = "PILOT"                    # 受控试点，单平台、小流量、人工值守
    PRODUCTION = "PRODUCTION"          # 商业生产直播，严格全栈神经与合规门禁


class CheckSeverity(str, Enum):
    """检查项严重级别"""
    BLOCK = "BLOCK"                    # 阻断项：失败则直接拒绝开播
    WARN = "WARN"                      # 警告项：记录告警但视档位放行
    INFO = "INFO"                      # 提示项：仅供诊断观察


@dataclass
class CheckItem:
    """单一检查项结果"""
    code: str
    title: str
    severity: CheckSeverity
    passed: bool
    evidence: Dict[str, Any] = field(default_factory=dict)
    remediation: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "title": self.title,
            "severity": self.severity.value,
            "passed": self.passed,
            "evidence": self.evidence,
            "remediation": self.remediation,
        }


@dataclass
class PreflightReport:
    """预检报告"""
    passed: bool
    mode: str
    generated_at: str
    expires_at: str
    code_version: str
    checks: List[CheckItem] = field(default_factory=list)
    blocking_reasons: List[str] = field(default_factory=list)

    def is_valid(self) -> bool:
        """检查报告是否仍在有效期内"""
        try:
            exp = datetime.fromisoformat(self.expires_at)
            return datetime.now(timezone.utc) < exp
        except Exception:
            return False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "passed": self.passed,
            "mode": self.mode,
            "generated_at": self.generated_at,
            "expires_at": self.expires_at,
            "code_version": self.code_version,
            "checks": [c.to_dict() for c in self.checks],
            "blocking_reasons": self.blocking_reasons,
        }


class PreflightService:
    """统一生产预检与开播门禁服务单例"""

    def __init__(self, cache_ttl_seconds: int = 300):
        self.cache_ttl_seconds = cache_ttl_seconds
        self._cached_reports: Dict[str, PreflightReport] = {}

    def _get_code_version(self) -> str:
        """读取当前系统版本号"""
        return os.getenv("LIVE_AGENT_VERSION", "2.0.1+commercial")

    def _check_assets_and_models(
        self, mode: ExecutionMode, avatar_asset_dir: Optional[str]
    ) -> List[CheckItem]:
        """检查 ONNX 模型与主播素材切片"""
        checks: List[CheckItem] = []

        # 1. 神经模型文件检查
        model_path = DATA_DIR / "models" / "onnx_lipsync.onnx"
        model_exists = False
        model_size_mb = 0.0
        try:
            if model_path.exists():
                st_size = getattr(model_path.stat(), "st_size", 0)
                if isinstance(st_size, (int, float)) and st_size > 1024 * 1024:
                    model_exists = True
                    model_size_mb = round(float(st_size) / (1024 * 1024), 2)
        except Exception:
            model_exists = False

        is_production = mode in (ExecutionMode.PRODUCTION, ExecutionMode.PILOT)
        model_severity = CheckSeverity.BLOCK if is_production else CheckSeverity.WARN

        checks.append(CheckItem(
            code="ASSET_ONNX_LIPSYNC_MODEL",
            title="ONNX神经唇形权重文件完整性",
            severity=model_severity,
            passed=model_exists,
            evidence={
                "path": str(model_path),
                "size_mb": model_size_mb,
                "exists": model_exists,
            },
            remediation="请下载或挂载 onnx_lipsync.onnx 权重至 data/models/ 目录" if not model_exists else "",
        ))

        # 2. 主播切片与坐标资产检查
        if avatar_asset_dir:
            asset_dir = Path(avatar_asset_dir)
            coords_file = asset_dir / "coords.pkl"
            face_dir = asset_dir / "face_imgs"
            has_coords = False
            has_face_dir = False
            try:
                if coords_file.exists():
                    c_size = getattr(coords_file.stat(), "st_size", 0)
                    has_coords = isinstance(c_size, (int, float)) and c_size > 0
                has_face_dir = bool(face_dir.exists() and face_dir.is_dir())
            except Exception:
                has_coords = False
                has_face_dir = False


            asset_passed = has_coords and has_face_dir
            asset_severity = CheckSeverity.BLOCK if is_production else CheckSeverity.WARN

            checks.append(CheckItem(
                code="ASSET_ANCHOR_SLICES_AND_COORDS",
                title="主播视频切片与坐标映射资产",
                severity=asset_severity,
                passed=asset_passed,
                evidence={
                    "asset_dir": str(asset_dir),
                    "has_coords": has_coords,
                    "has_face_dir": has_face_dir,
                },
                remediation="请在主播管理中完成素材提取并生成 coords.pkl 切片坐标" if not asset_passed else "",
            ))
        else:
            if is_production:
                checks.append(CheckItem(
                    code="ASSET_ANCHOR_SPECIFIED",
                    title="正式直播必须指定有效主播资产",
                    severity=CheckSeverity.BLOCK,
                    passed=False,
                    evidence={"avatar_asset_dir": None},
                    remediation="正式直播禁止使用默认空白形象，必须指定有效主播素材目录",
                ))

        return checks

    def _check_neural_rendering(self, mode: ExecutionMode) -> List[CheckItem]:
        """检查神经渲染 Provider 与算力支撑"""
        checks: List[CheckItem] = []
        is_production = mode in (ExecutionMode.PRODUCTION, ExecutionMode.PILOT)

        # 探测 ONNX Runtime Provider
        available_providers: List[str] = []
        try:
            import onnxruntime as ort
            available_providers = ort.get_available_providers()
        except Exception:
            available_providers = ["CPUExecutionProvider"]

        has_cuda = "CUDAExecutionProvider" in available_providers
        has_tensorrt = "TensorrtExecutionProvider" in available_providers
        cloud_gpu_url = os.getenv("CLOUD_GPU_URL", "").strip()
        has_cloud_gpu = bool(cloud_gpu_url)

        neural_acceleration_ready = has_cuda or has_tensorrt or has_cloud_gpu

        # 在正式与试点档位下，纯 CPU 绝不允许作为商业数字人驱动
        provider_severity = CheckSeverity.BLOCK if is_production else CheckSeverity.WARN
        checks.append(CheckItem(
            code="RENDER_ACCELERATION_HARDWARE",
            title="神经渲染硬件加速就绪状态 (CUDA/TensorRT/云端Sidecar)",
            severity=provider_severity,
            passed=neural_acceleration_ready,
            evidence={
                "providers": available_providers,
                "has_cuda": has_cuda,
                "has_cloud_gpu": has_cloud_gpu,
                "cloud_gpu_url": cloud_gpu_url[:15] + "..." if has_cloud_gpu else "",
            },
            remediation=(
                "在没有 CUDA 或云端 GPU Sidecar 的机器上，逐帧神经推理无法满足实时直播需求。"
                "请接入独立显卡 (RTX 3060+) 或在环境变量配置 CLOUD_GPU_URL 云端加速节点。"
                if not neural_acceleration_ready else ""
            ),
        ))

        return checks

    def _check_speech_and_tts(self, mode: ExecutionMode, voice_id: Optional[str]) -> List[CheckItem]:
        """检查 TTS 语音服务与音色授权"""
        checks: List[CheckItem] = []
        is_production = mode in (ExecutionMode.PRODUCTION, ExecutionMode.PILOT)

        # 检查是否配置了有效音色或基础引擎
        has_voice = bool(voice_id) or not is_production
        checks.append(CheckItem(
            code="SPEECH_VOICE_CONFIGURED",
            title="TTS主音色配置与授权校验",
            severity=CheckSeverity.BLOCK if is_production else CheckSeverity.INFO,
            passed=has_voice,
            evidence={"voice_id": voice_id},
            remediation="正式直播必须绑定已完成授权的音色 ID，严禁运行时动态切换未经授权音色" if not has_voice else "",
        ))

        return checks

    def _check_llm_and_facts(self, mode: ExecutionMode) -> List[CheckItem]:
        """检查大模型配置与结构化事实源"""
        checks: List[CheckItem] = []
        is_production = mode in (ExecutionMode.PRODUCTION, ExecutionMode.PILOT)

        openai_key = os.getenv("OPENAI_API_KEY", "").strip()
        live_llm_key = os.getenv("LIVE_AGENT_LLM_KEY", "").strip()
        has_llm_key = bool(openai_key or live_llm_key)

        # 如果开启了 Ollama 本地模式，也算通过
        ollama_base = os.getenv("OLLAMA_BASE_URL", "").strip()
        has_ollama = bool(ollama_base)

        llm_ready = has_llm_key or has_ollama
        checks.append(CheckItem(
            code="LLM_FACT_SERVICE_READY",
            title="大模型与事实服务链路就绪状态",
            severity=CheckSeverity.BLOCK if is_production else CheckSeverity.WARN,
            passed=llm_ready,
            evidence={
                "has_api_key": has_llm_key,
                "has_ollama": has_ollama,
            },
            remediation="未检测到有效的大模型凭据或本地推理节点，无法生成直播互动话术" if not llm_ready else "",
        ))

        return checks

    def _check_streaming_and_ffmpeg(self, mode: ExecutionMode) -> List[CheckItem]:
        """检查 FFmpeg 推流二进制与推流配置"""
        checks: List[CheckItem] = []
        from server.core.media.rtmp_streamer import find_ffmpeg_binary

        ffmpeg_bin = find_ffmpeg_binary()
        has_ffmpeg = bool(ffmpeg_bin and os.path.isfile(ffmpeg_bin))

        is_production = mode in (ExecutionMode.PRODUCTION, ExecutionMode.PILOT)
        checks.append(CheckItem(
            code="STREAM_FFMPEG_BINARY_READY",
            title="FFmpeg 音画推流与合流引擎二进制",
            severity=CheckSeverity.BLOCK if is_production else CheckSeverity.WARN,
            passed=has_ffmpeg,
            evidence={"ffmpeg_path": ffmpeg_bin},
            remediation="系统或便携路径中未找到 ffmpeg 可执行文件，无法执行直推或录制" if not has_ffmpeg else "",
        ))

        return checks

    def _check_security_policies(self, mode: ExecutionMode) -> List[CheckItem]:
        """检查生产安全基线 (Token, Remote Auth, CORS)"""
        checks: List[CheckItem] = []
        is_production = mode in (ExecutionMode.PRODUCTION, ExecutionMode.PILOT)

        auth_token = os.getenv("API_AUTH_TOKEN", "").strip() or os.getenv("LIVE_AGENT_TOKEN", "").strip()
        enforce_remote = os.getenv("ENFORCE_REMOTE_AUTH", "0").strip()
        cors_origins = os.getenv("CORS_ALLOW_ORIGINS", "*").strip()

        # PRODUCTION 档位下必须强制 Token，且不能全开放 CORS
        token_safe = bool(auth_token) or (not is_production)
        cors_safe = (cors_origins != "*") or (not is_production)

        checks.append(CheckItem(
            code="SECURITY_PRODUCTION_TOKEN",
            title="生产环境外部访问强制令牌安全策略",
            severity=CheckSeverity.BLOCK if is_production else CheckSeverity.WARN,
            passed=token_safe,
            evidence={
                "auth_token_set": bool(auth_token),
                "enforce_remote_auth": enforce_remote,
            },
            remediation="PRODUCTION 档位下必须在环境变量设置强随机 API_AUTH_TOKEN" if not token_safe else "",
        ))

        checks.append(CheckItem(
            code="SECURITY_CORS_ORIGIN_WHITELIST",
            title="CORS 跨域访问白名单明确性",
            severity=CheckSeverity.BLOCK if is_production else CheckSeverity.INFO,
            passed=cors_safe,
            evidence={"cors_allow_origins": cors_origins},
            remediation="生产环境建议将 CORS_ALLOW_ORIGINS 限制为具体的管理前端域名，禁止使用通配符 *" if not cors_safe else "",
        ))

        checks.append(CheckItem(
            code="SECURITY_ORDER_WEBHOOK_SECRET",
            title="订单 Webhook 生产签名密钥",
            severity=CheckSeverity.BLOCK if is_production else CheckSeverity.INFO,
            passed=bool(os.getenv("ORDER_WEBHOOK_SECRET", "").strip()) or not is_production,
            evidence={"configured": bool(os.getenv("ORDER_WEBHOOK_SECRET", "").strip())},
            remediation="Production/Pilot 必须配置 ORDER_WEBHOOK_SECRET，禁止无签名订单回调" if is_production and not os.getenv("ORDER_WEBHOOK_SECRET", "").strip() else "",
        ))

        return checks

    def _check_compliance_and_governance(self, mode: ExecutionMode, platform: str = "bilibili") -> List[CheckItem]:
        """检查平台合规政策、AI 显著披露与资质完整性"""
        checks: List[CheckItem] = []
        is_production = mode in (ExecutionMode.PRODUCTION, ExecutionMode.PILOT)
        from server.core.compliance.governance import global_compliance_service

        rules = global_compliance_service.get_platform_rules(platform)
        disclosure_text = global_compliance_service.get_disclosure_text()
        has_disclosure = bool(disclosure_text and len(disclosure_text) >= 10)

        checks.append(CheckItem(
            code="COMPLIANCE_AI_DISCLOSURE",
            title="平台 AI 生成合成内容强制显著披露标识",
            severity=CheckSeverity.BLOCK if is_production else CheckSeverity.INFO,
            passed=has_disclosure,
            evidence={
                "platform": platform,
                "requires_disclosure": rules.get("requires_ai_disclosure", True),
                "disclosure_text": disclosure_text,
            },
            remediation="正式商业直播必须配置符合法规要求的 AI 生成内容显著标识声明" if not has_disclosure else "",
        ))

        return checks

    async def _check_sku_slice_fidelity(self, mode: ExecutionMode) -> List[CheckItem]:
        """拦截无唇形同步的占位带货切片上线。

        架构诚实 (ADR-16)：未配置可达云端 GPU 时，切片渲染会退化为主播原始
        闭口帧镜像循环。这些切片本身是合法可播放的 MP4，若不在开播前拦截，
        运营会把"没有唇形同步"的成品当成高精带货切片播出去而不自知。
        """
        checks: List[CheckItem] = []
        is_production = mode in (ExecutionMode.PRODUCTION, ExecutionMode.PILOT)
        placeholder_count = 0
        total_count = 0
        sample_ids: List[str] = []

        try:
            from server.core.avatar.sku_slice_renderer import PLACEHOLDER_ENGINE
            from server.database.db import AsyncSessionLocal
            from server.database.models import SkuSlice
            from sqlalchemy import select

            async with AsyncSessionLocal() as session:
                result = await session.execute(select(SkuSlice))
                for row in result.scalars().all():
                    if not row.is_active:
                        continue
                    total_count += 1
                    if str(row.render_engine or "") == PLACEHOLDER_ENGINE:
                        placeholder_count += 1
                        if len(sample_ids) < 5:
                            sample_ids.append(str(row.id))
        except Exception as exc:
            return [CheckItem(
                code="SKU_SLICE_FIDELITY",
                title="带货切片唇形保真度核验",
                severity=CheckSeverity.INFO,
                passed=False,
                evidence={"error": f"{type(exc).__name__}: {exc}"[:200]},
                remediation="切片资产核验不可用，请检查数据库连接后重试体检",
            )]

        has_placeholder = placeholder_count > 0
        checks.append(CheckItem(
            code="SKU_SLICE_FIDELITY",
            title="带货切片神经唇形保真度 (无占位切片上线)",
            severity=CheckSeverity.BLOCK if is_production else CheckSeverity.WARN,
            passed=not has_placeholder,
            evidence={
                "active_slice_total": total_count,
                "placeholder_slice_count": placeholder_count,
                "placeholder_sample_ids": sample_ids,
            },
            remediation=(
                f"存在 {placeholder_count} 个无唇形同步的占位循环切片（多为未配置云端 GPU 所致）。"
                "请配置可达云端 GPU 并重新生成切片，或在控制台停用这些切片后再开播。"
            ) if has_placeholder else "",
        ))

        return checks

    async def run_preflight(
        self,
        mode: str = "PRODUCTION",
        avatar_asset_dir: Optional[str] = None,
        voice_id: Optional[str] = None,
        platform: str = "bilibili",
        force: bool = False,
    ) -> PreflightReport:
        """执行全栈门禁检查并返回结构化报告"""
        mode_str = mode.upper()
        try:
            exec_mode = ExecutionMode(mode_str)
        except ValueError:
            exec_mode = ExecutionMode.PREVIEW

        cache_key = f"{exec_mode.value}:{avatar_asset_dir or ''}:{voice_id or ''}:{platform}"
        if not force and cache_key in self._cached_reports:
            cached = self._cached_reports[cache_key]
            if cached.is_valid():
                return cached

        now = datetime.now(timezone.utc)
        expires_at = datetime.fromtimestamp(now.timestamp() + self.cache_ttl_seconds, tz=timezone.utc)

        all_checks: List[CheckItem] = []
        all_checks.extend(self._check_assets_and_models(exec_mode, avatar_asset_dir))
        all_checks.extend(self._check_neural_rendering(exec_mode))
        all_checks.extend(self._check_speech_and_tts(exec_mode, voice_id))
        all_checks.extend(self._check_llm_and_facts(exec_mode))
        all_checks.extend(self._check_streaming_and_ffmpeg(exec_mode))
        all_checks.extend(self._check_security_policies(exec_mode))
        all_checks.extend(self._check_compliance_and_governance(exec_mode, platform))
        all_checks.extend(await self._check_sku_slice_fidelity(exec_mode))

        blocking_reasons: List[str] = []
        overall_passed = True

        for check in all_checks:
            if not check.passed and check.severity == CheckSeverity.BLOCK:
                overall_passed = False
                blocking_reasons.append(f"[{check.code}] {check.title}: {check.remediation}")

        report = PreflightReport(
            passed=overall_passed,
            mode=exec_mode.value,
            generated_at=now.isoformat(),
            expires_at=expires_at.isoformat(),
            code_version=self._get_code_version(),
            checks=all_checks,
            blocking_reasons=blocking_reasons,
        )

        self._cached_reports[cache_key] = report
        return report


global_preflight_service = PreflightService()
