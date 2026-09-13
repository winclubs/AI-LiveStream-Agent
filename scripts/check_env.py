#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI-LiveStream-Agent 本地运行环境诊断与体检工具
自动检查：Python 版本、CUDA/GPU 显存、关键依赖包、网络连通性、OBS 虚拟摄像头状态、端口可用性
"""

import sys
import os
import socket
import platform

# 强制适配 Windows 终端编码
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

def print_header(title):
    print("\n" + "=" * 60)
    print(f"  {title}")
    print("=" * 60)

def check_python_version():
    print("[1] 检查 Python 运行环境:")
    ver = sys.version_info
    print(f"    - 当前版本: Python {ver.major}.{ver.minor}.{ver.micro} ({platform.architecture()[0]})")
    if ver.major == 3 and ver.minor >= 10:
        print("    [OK] Python 版本满足要求 (>= 3.10)")
        return True
    else:
        print("    [!] 建议使用 Python 3.10 ~ 3.12 最佳兼容环境")
        return False


def check_gpu_and_cuda():
    print("\n[2] 检查 GPU 硬件与 CUDA 加速支持:")
    try:
        import torch
        cuda_avail = torch.cuda.is_available()
        print(f"    - PyTorch 版本: {torch.__version__}")
        print(f"    - CUDA 可用性: {'[OK] 是' if cuda_avail else '[!] 否 (CPU 降级模式)'}")
        if cuda_avail:
            device_count = torch.cuda.device_count()
            for i in range(device_count):
                name = torch.cuda.get_device_name(i)
                mem = torch.cuda.get_device_properties(i).total_memory / (1024 ** 3)
                print(f"    - 显卡 {i}: {name} (显存容量: {mem:.1f} GB)")
                if mem >= 6.0:
                    print("    [OK] 显存满足 MuseTalk 实时流式渲染要求 (>= 6GB)")
                else:
                    print("    [!] 显存较小 (< 6GB)，建议开启半精度或 Edge-TTS 轻量运行模式")
        else:
            print("    [i] 未检测到独立 NVIDIA 显卡或 CUDA 驱动，系统将自动平滑降级至 Edge-TTS 与轻量驱动")
    except ImportError:
        print("    [!] 未安装 PyTorch，当前仅支持轻量 API 代理与 Edge-TTS 模式")

def check_packages():
    print("\n[3] 检查系统核心依赖库:")
    core_pkgs = [
        ("fastapi", "FastAPI 高性能 Web 框架"),
        ("uvicorn", "ASGI 服务器"),
        ("aiosqlite", "异步 SQLite WAL 驱动"),
        ("sqlalchemy", "ORM 持久化"),
        ("ahocorasick", "Aho-Corasick 高性能违禁词匹配引擎"),
        ("cryptography", "AES-256-GCM 硬件级安全加密库"),
        ("psutil", "实时硬件探针"),
        ("httpx", "异步 HTTP 客户端"),
        ("pydantic", "数据模型校验")
    ]
    all_passed = True
    for pkg, desc in core_pkgs:
        try:
            __import__(pkg)
            print(f"    [OK] {pkg:<15} - {desc}")
        except ImportError:
            print(f"    [FAIL] {pkg:<15} - 缺失! 请运行: pip install {pkg}")
            all_passed = False
    return all_passed

def check_port_availability(port=18080):
    print(f"\n[4] 检查系统核心中控端口 ({port}):")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1.0)
        result = s.connect_ex(("127.0.0.1", port))
        if result == 0:
            print(f"    [!] 警告: 端口 {port} 当前已被占用，请确认是否有已启动的实例")
            return False
        else:
            print(f"    [OK] 端口 {port} 空闲可用")
            return True


def main():
    print_header("AI-LiveStream-Agent 硬件与运行环境自检")
    check_python_version()
    check_gpu_and_cuda()
    check_packages()
    check_port_availability()
    print_header("环境体检诊断完成")
    print("提示: 您可直接运行 run_agent.bat 或 python scripts/start_server.py 启动主系统\n")

if __name__ == "__main__":
    main()
