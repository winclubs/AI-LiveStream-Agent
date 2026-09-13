#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
一键将 components/*.html 合成编译到 server/static/index.html
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.static.builder import build_index_html

if __name__ == "__main__":
    content = build_index_html()
    print(f"[OK] 成功从 components/ 编译合并 index.html (行数: {len(content.splitlines())}, 大小: {len(content)} 字节)")
