# -*- coding: utf-8 -*-
"""
商用控制台不得夹带未审阅的第三方外链营销页（上线诊断 RISK-9）。

现状：``tab_operations.html`` 是一个纯静态的第三方工具外链目录，
共 7 张卡片、7 条外链（视频大师 / 好看视频 / 各类转码压缩工具等）。

对**商用交付物**而言有三个问题：
    1. 品牌风险：在自家付费控制台里推荐竞品与无关 SaaS；
    2. 合规风险：外链站点未经任何审核即随产品分发，域名失效或内容
       变更都不可控，等于把法务与声誉风险打包进安装包；
    3. 静默腐化：纯静态 HTML，域名过期后无人知晓，页面持续展示失效入口。

决策：从**交付物**中移除该页面（导航项 + 面板 + 脚本 + 组件文件）。
仓库内不保留死代码 —— 保留只会在下次有人「顺手恢复」时把风险带回来。
需要保留运营素材时应走独立的、经过审核的渠道。

本文件锁定「交付物中不含第三方营销外链」这一不变量。
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "server" / "static"

#: 控制台自有域名/来源（本地服务与静态资源），其余外链一律视为第三方
ALLOWED_HOST_PREFIXES = ("http://127.0.0.1", "http://localhost")

#: 经审阅、**属于功能必需**的供应商深链白名单。
#: 与「营销外链目录」的区别：这是使用该供应商能力前用户**必须**访问的
#: 官方控制台（注册音色克隆、取得 Voice-ID），删除会直接中断产品文档中
#: 描述的配置流程 —— 属于功能依赖，而非导流。
ALLOWED_EXTERNAL_LINKS = {
    # 阿里云百炼「声音复刻」控制台：CosyVoice 音色克隆的官方注册入口。
    # 用户必须在此上传 10~20 秒音频并取得 Voice-ID，才能在本地完成绑定。
    "https://bailian.console.aliyun.com/cn-beijing/model/experience/voice/sound-cloning":
        "阿里云百炼声音复刻官方控制台 —— CosyVoice 音色克隆功能必需流程",
}

#: 明确禁止的组件与脚本
REMOVED_COMPONENTS = (
    "components/tab_operations.html",
    "js/modules/17_operations.js",
)


def _text(rel: str) -> str:
    path = STATIC / rel
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _external_links(html: str) -> list[str]:
    """返回未审阅的第三方外链（已剔除本机来源与经审阅的供应商深链白名单）。"""
    return [
        url
        for url in re.findall(r'href="(https?://[^"]+)"', html)
        if not url.startswith(ALLOWED_HOST_PREFIXES)
        and url not in ALLOWED_EXTERNAL_LINKS
    ]


# ---------------------------------------------------------------------------
# 已移除项
# ---------------------------------------------------------------------------

def test_operations_component_files_are_removed():
    """第三方外链运营页的组件与脚本必须从仓库移除（不留死代码）。"""
    for rel in REMOVED_COMPONENTS:
        assert not (STATIC / rel).exists(), (
            f"{rel} 仍存在：这是未审阅的第三方营销外链页，"
            f"不得随商用交付物分发。上线诊断 RISK-9。"
        )


def test_operations_tab_not_referenced_anywhere():
    """模板、侧边栏与合成产物中均不得再引用运营页。"""
    for rel in ("index.template.html", "index.html", "components/sidebar.html"):
        text = _text(rel)
        assert "tab_operations.html" not in text, f"{rel} 仍引用 tab_operations.html"
        assert 'data-tab="operations"' not in text, f"{rel} 仍存在运营页导航项"
        assert 'id="tab-operations"' not in text, f"{rel} 仍存在运营页面板"
        assert "17_operations.js" not in text, f"{rel} 仍加载运营页脚本"


def test_operations_js_module_not_loaded():
    """合成产物不得加载运营页脚本。"""
    assert "17_operations.js" not in _text("index.html")
    assert "17_operations.js" not in _text("index.template.html")


# ---------------------------------------------------------------------------
# 通用不变量：交付物不含未审阅外链
# ---------------------------------------------------------------------------

def test_built_console_ships_no_third_party_marketing_links():
    """合成后的 index.html 不得夹带任何第三方外链。"""
    links = _external_links(_text("index.html"))
    assert not links, (
        "合成产物 index.html 含未审阅的第三方外链（商业交付物合规风险）:\n  "
        + "\n  ".join(links)
    )


def test_all_remaining_components_ship_no_third_party_links():
    """所有仍随交付物分发的组件都不得含第三方外链。"""
    offenders: list[str] = []
    for comp in sorted((STATIC / "components").glob("*.html")):
        for url in _external_links(comp.read_text(encoding="utf-8")):
            offenders.append(f"{comp.name}: {url}")
    assert not offenders, (
        "以下组件含未审阅第三方外链:\n  " + "\n  ".join(offenders)
    )
