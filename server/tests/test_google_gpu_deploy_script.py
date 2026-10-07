# -*- coding: utf-8 -*-
"""google_gpu.md 与 intern_gpu.md 云端 GPU 部署脚本可执行自动化护栏测试。

测试目标：
1. 部署脚本中内嵌的 sidecar 服务端源码能够正确提取并编译（语法与结构有效性）；
2. 文本占位符（Token、路径、设备型号、目标分辨率）完全同构且无遗漏；
3. FastAPI 端点健全性与鉴权守卫（/health、/assets/*、/render/batch）；
4. ADR-16 架构诚实性门禁：/health 与能力集必须严格反映 engine.is_ready 真实状态，
   禁止只看 CUDA 驱动就谎报就绪（彻底消除 P0-2 静默灰屏根因）；
5. 官方 LatentSync 80 维全局 Mel 频谱滑动窗口契约与视听预动量支持。
"""
from __future__ import annotations

import ast
import re
import types
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

DEPLOY_DOCS = ["google_gpu.md", "intern_gpu.md"]

_PLACEHOLDERS = {
    "__GPU_DEVICE__": repr("Tesla T4, 15360 MiB"),
    "__AUTH_TOKEN__": repr("test-token-abc123"),
    "__CHECKPOINT_DIR__": repr("/tmp/does-not-exist/ckpt"),
    "__ASSET_ROOT__": repr("/tmp/does-not-exist/assets"),
    "__SIDECAR_HOST__": repr("127.0.0.1"),
    "__SIDECAR_PORT__": repr("8010"),
    "__LATENTSYNC_TIER__": repr("latentsync_1_5"),
    "__TARGET_RESOLUTION__": repr(256),
    "__ENGINE_LABEL__": repr("ByteDance LatentSync 1.5 (256x256 test)"),
}

_DOC_EXTRA_PLACEHOLDERS = {
    "intern_gpu.md": {"__VRAM_TOTAL_GB__": repr(15.0)},
}


def _extract_sidecar_source(doc_name: str) -> str:
    """从部署脚本中抽出内嵌 sidecar 服务端源码并完成占位符替换。"""
    doc = (REPO_ROOT / doc_name).read_text(encoding="utf-8")
    match = re.search(r"sidecar_server_code = '''(.*?)'''", doc, re.S)
    assert match, f"{doc_name} 中找不到 sidecar_server_code 字符串字面量"
    code = ast.literal_eval("'''" + match.group(1) + "'''")
    placeholders = dict(_PLACEHOLDERS)
    placeholders.update(_DOC_EXTRA_PLACEHOLDERS.get(doc_name, {}))
    for placeholder, literal in placeholders.items():
        assert placeholder in code, f"{doc_name}: 占位符 {placeholder} 不在源码中，脚本结构可能已变"
        code = code.replace(placeholder, literal)
    return code


class _SidecarNS:
    """代理到 exec 命名空间本体。"""

    def __init__(self, ns: dict) -> None:
        object.__setattr__(self, "_ns", ns)

    def __getattr__(self, name):
        return self._ns[name]

    def __setattr__(self, name, value):
        self._ns[name] = value

    def __getitem__(self, name):
        return self._ns[name]


def load_sidecar(doc_name: str) -> _SidecarNS:
    """执行内嵌 sidecar 源码并返回其命名空间代理。"""
    source = _extract_sidecar_source(doc_name)
    namespace: dict = {"__name__": "sidecar_under_test", "__file__": "sidecar_server.py"}
    namespace["__builtins__"] = __builtins__
    compiled = compile(source, f"{doc_name}:sidecar", "exec")

    import logging

    original_basic_config = logging.basicConfig

    def _noop_basic_config(*_a, **_k):
        return None

    logging.basicConfig = _noop_basic_config
    try:
        exec(compiled, namespace)  # noqa: S102
    finally:
        logging.basicConfig = original_basic_config
    return _SidecarNS(namespace)


@pytest.fixture(autouse=True)
def _isolate_global_state():
    """隔离 exec 内嵌脚本造成的全局日志与状态副作用。"""
    import logging

    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    saved_basic = logging.root.manager.disable
    try:
        yield
    finally:
        for handler in list(root.handlers):
            if handler not in saved_handlers:
                root.removeHandler(handler)
                try:
                    handler.close()
                except Exception:
                    pass
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
        logging.root.manager.disable = saved_basic


# ===========================================================================
# 1. 结构与可执行性护栏测试
# ===========================================================================

@pytest.mark.parametrize("doc_name", DEPLOY_DOCS)
def test_sidecar_source_is_extractable_and_compiles(doc_name):
    """内嵌源码必须可成功提取并编译执行。"""
    code = _extract_sidecar_source(doc_name)
    compile(code, f"{doc_name}:sidecar", "exec")


@pytest.mark.parametrize("doc_name", DEPLOY_DOCS)
def test_generated_sidecar_source_structure(doc_name):
    """内嵌源码必须语法合法，且包含关键组件。"""
    code = _extract_sidecar_source(doc_name)
    tree = ast.parse(code)
    func_names = [n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    assert "health_endpoint" in func_names, f"{doc_name} 缺失 health_endpoint"
    assert "get_asset_manifest" in func_names, f"{doc_name} 缺失 get_asset_manifest"
    assert "upload_asset_single" in func_names, f"{doc_name} 缺失 upload_asset_single"
    assert "render_batch_endpoint" in func_names, f"{doc_name} 缺失 render_batch_endpoint"


@pytest.mark.parametrize("doc_name", DEPLOY_DOCS)
def test_required_placeholders_exist(doc_name):
    """文档正文中必须包含所有关键配置占位符。"""
    doc = (REPO_ROOT / doc_name).read_text(encoding="utf-8")
    for ph in [
        "__GPU_DEVICE__", "__AUTH_TOKEN__", "__CHECKPOINT_DIR__",
        "__ASSET_ROOT__", "__SIDECAR_HOST__", "__SIDECAR_PORT__",
        "__LATENTSYNC_TIER__", "__TARGET_RESOLUTION__", "__ENGINE_LABEL__"
    ]:
        assert ph in doc, f"{doc_name} 缺失必要占位符 {ph}"


# ===========================================================================
# 2. 诚实性与 ADR-16 架构门禁守卫 (P0-2 回归保护)
# ===========================================================================

@pytest.mark.parametrize("doc_name", DEPLOY_DOCS)
def test_latentsync_health_contract_and_readiness_guard(doc_name):
    """测试 /health 诚实反映引擎就绪状态，不盲目宣称可用 (P0-2 守卫)"""
    ns = load_sidecar(doc_name)
    from fastapi.testclient import TestClient
    client = TestClient(ns.app)

    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert "backend_ready" in data
    assert "renderer_available" in data
    assert "providers" in data
    assert "syncnet_score" not in data, "不得出现未经测量的营销性 syncnet_score 硬编码"

    # 当 engine 未真正就绪时，backend_ready 与 renderer_available 必须严格为 False
    # (杜绝有显卡驱动即报就绪导致的客户端灰屏事故)
    if not (getattr(ns.engine, "is_ready", False)):
        assert data["backend_ready"] is False
        assert data["renderer_available"] is False


@pytest.mark.parametrize("doc_name", DEPLOY_DOCS)
def test_sidecar_engine_contract(doc_name):
    """InferenceEngine 必须遵循 LatentSync UNet3D 规范"""
    ns = load_sidecar(doc_name)
    assert hasattr(ns, "LatentSyncInferenceEngine")
    assert ns.TARGET_RESOLUTION == 256
    assert hasattr(ns.engine, "is_ready")


# ===========================================================================
# 3. 鉴权与安全防护测试
# ===========================================================================

@pytest.mark.parametrize("doc_name", DEPLOY_DOCS)
def test_asset_endpoints_require_auth(doc_name):
    """资产端点必须具备严格鉴权守卫"""
    ns = load_sidecar(doc_name)
    from fastapi.testclient import TestClient
    client = TestClient(ns.app)

    # 1. 资产清单未带 Token 必须 401
    res = client.get("/assets/default")
    assert res.status_code == 401, f"未鉴权必须返回 401，实际: {res.status_code}"

    # 2. 携带有效 Token 必须放行鉴权 (未录入资产时返回 404，已录入返回 200，绝不得为 401/403)
    raw_token = _PLACEHOLDERS["__AUTH_TOKEN__"].strip("'\"")
    res_authed = client.get("/assets/default", headers={"Authorization": f"Bearer {raw_token}"})
    assert res_authed.status_code in (200, 404), f"有效 Token 必须通过鉴权，实际: {res_authed.status_code}"


@pytest.mark.parametrize("doc_name", DEPLOY_DOCS)
def test_latentsync_batch_render_requires_auth(doc_name):
    """/render/batch 鉴权依赖必须严格阻断无凭证调用"""
    ns = load_sidecar(doc_name)
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        ns.require_auth(None)
    assert exc.value.status_code in (401, 403)


@pytest.mark.parametrize("doc_name", DEPLOY_DOCS)
def test_render_batch_response_has_no_marketing_claims(doc_name):
    """内嵌服务端源码不得残留未经实测的营销性 syncnet_score (ADR-16)。

    历史缺陷：该字段最初出现在 `/health`，被移除后又被挪进 `/render/batch`
    的 JSONResponse —— 而既有护栏只检查 `/health` 的响应体，覆盖不到那里。
    SyncNet 的 LSE-C/LSE-D 是有明确数值区间的可测量指标，"peak_quality"
    不是测量结果，属对客户端的虚假质量声明，必须彻底移除而非换个端点藏起来。

    这里刻意检查**整份源码**而非某个端点的响应：exec 内嵌代码里 pydantic 的
    前向引用无法在测试命名空间解析，无法可靠地 POST 走端点；而"源码中不得
    出现该字段"是更强、且能覆盖未来任何新增端点的约束。
    """
    source = _extract_sidecar_source(doc_name)
    assert "syncnet_score" not in source, (
        f"{doc_name}: 内嵌服务端残留未经实测的营销性 syncnet_score 硬编码"
    )
    # 同理禁止其他未经实测即断言质量达标的营销字段
    for banned in ("peak_quality", '"lse_c"', '"lse_d"'):
        assert banned not in source, (
            f"{doc_name}: 内嵌服务端残留未经实测的质量断言字段 {banned}"
        )


# ===========================================================================
# 4. 音频特征工程契约测试
# ===========================================================================

@pytest.mark.parametrize("doc_name", DEPLOY_DOCS)
def test_sidecar_mel_extractors_contract(doc_name):
    """内嵌服务端必须包含标准 Mel 特征提取器且契约一致"""
    ns = load_sidecar(doc_name)
    assert hasattr(ns, "LatentSyncMelExtractor")
    extractor = ns.LatentSyncMelExtractor()
    pcm = np.zeros(16000, dtype=np.float32)
    mel = extractor.extract_full_mel(pcm)
    assert mel.shape[0] == 80, f"Mel 特征必须是 80 维度，实际: {mel.shape}"
