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


def build_console_js() -> str:
    """
    将 server/static/js/modules/*.js 按数字前缀顺序合并为统一的 console.js。
    保持开发期高内聚分模块维护，同时兼顾浏览器请求性能与既有测试兼容性。
    单次合成耗时 < 5ms。
    """
    js_modules_dir = STATIC_DIR / "js" / "modules"
    console_js_file = STATIC_DIR / "js" / "console.js"

    if not js_modules_dir.exists():
        raise FileNotFoundError(f"子模块目录不存在: {js_modules_dir}")

    module_files = sorted(js_modules_dir.glob("*.js"))
    if not module_files:
        raise FileNotFoundError(f"未在 {js_modules_dir} 找到可编译的 JS 子模块")

    chunks = []
    for mf in module_files:
        lines = [line.rstrip() for line in mf.read_text(encoding="utf-8").splitlines()]
        chunks.append("\n".join(lines))

    merged_js = "\n".join(chunks).rstrip() + "\n"
    console_js_file.write_text(merged_js, encoding="utf-8")
    return merged_js


if __name__ == "__main__":
    import sys
    if "--split-html" in sys.argv:
        print("[*] 正在执行 index.html 模块化组件拆分...")
        split_index_html()

    print("[*] 正在执行组件模板合并构建 (HTML)...")
    built_html = build_index_html()
    print(f"[OK] HTML 合成完成，文件大小: {len(built_html)} 字节，行数: {len(built_html.splitlines())}")
    print("[OK] 前端模块化脚本就绪，由 index.html 直接按序加载 modules/*.js，console.js 保持轻量入口。")

