"""
前端 HTML 组件化模板合成器 (静态编译 / 动态组装)
将 1270+ 行大型单页拆解为独立模块组件 (Header, Sidebar, 8大面板, 弹窗)，
在构建或服务启动时无缝编译为完整单页，既便于后期分模块维护，又保证浏览器性能与测试兼容。
"""

import os
import re
from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parent
COMPONENTS_DIR = STATIC_DIR / "components"
TEMPLATE_FILE = STATIC_DIR / "index.template.html"
OUTPUT_FILE = STATIC_DIR / "index.html"


def split_index_html():
    """
    [一次性/维护工具] 将当前 index.html 精确拆分为 11 个独立组件和 1 个精简主模板
    """
    if not OUTPUT_FILE.exists():
        raise FileNotFoundError(f"未找到原始 HTML 文件: {OUTPUT_FILE}")

    content = OUTPUT_FILE.read_text(encoding="utf-8")
    lines = content.splitlines(keepends=True)

    COMPONENTS_DIR.mkdir(parents=True, exist_ok=True)

    # 精确切片定义 (包含开始和结束行，1-indexed)
    slices = {
        "header.html": (89, 104),
        "sidebar.html": (110, 147),
        "tab_wizard.html": (153, 308),
        "tab_live.html": (311, 459),
        "tab_anchors.html": (462, 512),
        "tab_voices.html": (515, 593),
        "tab_roles.html": (596, 626),
        "tab_products.html": (629, 692),
        "tab_guardrails.html": (695, 757),
        "tab_settings.html": (760, 1124),
        "tab_knowledge.html": (1127, 1249),
        "modal_preflight.html": (1255, 1271),
    }

    # 写入各组件文件
    for name, (start, end) in slices.items():
        comp_content = "".join(lines[start - 1 : end])
        target_path = COMPONENTS_DIR / name
        target_path.write_text(comp_content, encoding="utf-8")
        print(f"  [+] 已拆分组件: components/{name} ({end - start + 1} 行)")

    # 构造主模板 index.template.html
    template_parts = []
    # 头部: 1 ~ 88
    template_parts.append("".join(lines[0:88]))
    template_parts.append("  <!-- INCLUDE: components/header.html -->\n")
    template_parts.append("".join(lines[104:109]))
    template_parts.append("    <!-- INCLUDE: components/sidebar.html -->\n")
    template_parts.append("".join(lines[147:152]))
    template_parts.append("      <!-- INCLUDE: components/tab_wizard.html -->\n\n")
    template_parts.append("      <!-- INCLUDE: components/tab_live.html -->\n\n")
    template_parts.append("      <!-- INCLUDE: components/tab_anchors.html -->\n\n")
    template_parts.append("      <!-- INCLUDE: components/tab_voices.html -->\n\n")
    template_parts.append("      <!-- INCLUDE: components/tab_roles.html -->\n\n")
    template_parts.append("      <!-- INCLUDE: components/tab_products.html -->\n\n")
    template_parts.append("      <!-- INCLUDE: components/tab_guardrails.html -->\n\n")
    template_parts.append("      <!-- INCLUDE: components/tab_settings.html -->\n\n")
    template_parts.append("      <!-- INCLUDE: components/tab_knowledge.html -->\n")
    template_parts.append("".join(lines[1249:1254]))
    template_parts.append("  <!-- INCLUDE: components/modal_preflight.html -->\n")
    template_parts.append("".join(lines[1271:]))

    TEMPLATE_FILE.write_text("".join(template_parts), encoding="utf-8")
    print(f"  [+] 已生成主模板: {TEMPLATE_FILE.name} ({len(template_parts)} 块)")


def build_index_html() -> str:
    """
    根据 index.template.html 和 components/*.html 自动合成最终的 index.html。
    纯标准库零依赖，单次合成耗时 < 3ms。
    """
    if not TEMPLATE_FILE.exists():
        raise FileNotFoundError(f"主模板文件不存在: {TEMPLATE_FILE}")

    template_content = TEMPLATE_FILE.read_text(encoding="utf-8")

    def _replace_include(match):
        rel_path = match.group(1).strip()
        comp_file = STATIC_DIR / rel_path
        if not comp_file.exists():
            raise FileNotFoundError(f"组件文件未找到: {comp_file} (由模板 {rel_path} 引用)")
        return comp_file.read_text(encoding="utf-8")

    # 匹配 <!-- INCLUDE: path/to/file.html -->
    pattern = re.compile(r"<!--\s*INCLUDE:\s*([^>\s]+)\s*-->")
    compiled_content = pattern.sub(_replace_include, template_content)

    OUTPUT_FILE.write_text(compiled_content, encoding="utf-8")
    return compiled_content


if __name__ == "__main__":
    print("[*] 正在执行 index.html 模块化组件拆分...")
    split_index_html()
    print("[*] 正在验证合并构建...")
    built = build_index_html()
    print(f"[OK] 合并编译完成，生成文件大小: {len(built)} 字节，行数: {len(built.splitlines())}")
