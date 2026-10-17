"""
前端 HTML 组件化模板合成器 (静态编译 / 动态组装)
将 1270+ 行大型单页拆解为独立模块组件 (Header, Sidebar, 8大面板, 弹窗)，
在构建或服务启动时无缝编译为完整单页，既便于后期分模块维护，又保证浏览器性能与测试兼容。
"""

import re
from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parent
COMPONENTS_DIR = STATIC_DIR / "components"
TEMPLATE_FILE = STATIC_DIR / "index.template.html"
OUTPUT_FILE = STATIC_DIR / "index.html"


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
    print("[*] 正在执行组件模板合并构建 (HTML)...")
    built_html = build_index_html()
    print(f"[OK] HTML 合成完成，文件大小: {len(built_html)} 字节，行数: {len(built_html.splitlines())}")
    print("[OK] 前端模块化脚本就绪，由 index.html 直接按序加载 modules/*.js。")

