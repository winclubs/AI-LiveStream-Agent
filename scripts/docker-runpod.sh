#!/usr/bin/env bash
# ============================================================================
# AI-LiveStream-Agent 端云分离模式 (Tier C) 云端 GPU 节点启动脚本
# 两种用法：
#  A) 使用官方预构建镜像 (下方 docker run)；
#  B) 自建节点：在任意带 GPU/CPU 的容器或裸机直接运行本仓库的
#       python scripts/cloud_node_server.py --host 0.0.0.0 --port 8888 --token <token>
#     二者协议一致 (ws://<host>:8888/ws/render)，本地控制台 remote_gpu 配置即可连接。
# ============================================================================
set -e

AUTH_TOKEN="${AUTH_TOKEN:-change_me_secure_token_123456}"
WEIGHTS_DIR="${WEIGHTS_DIR:-/root/weights}"
IMAGE="registry.cn-hangzhou.aliyuncs.com/ai-live/livetalking-node:latest"

echo "=============================================="
echo "  AI-LiveStream-Agent 云端渲染节点启动器"
echo "  镜像: ${IMAGE}"
echo "  权重目录: ${WEIGHTS_DIR}"
echo "=============================================="

mkdir -p "${WEIGHTS_DIR}"

docker run -d --gpus all \
  -p 8888:8888 \
  -p 8554:8554 \
  -e AUTH_TOKEN="${AUTH_TOKEN}" \
  -v "${WEIGHTS_DIR}:/workspace/weights" \
  --name ai-livestream-agent-node \
  --restart unless-stopped \
  "${IMAGE}"

echo ""
echo "[OK] 云端渲染节点已启动"
echo "    - WebSocket 渲染网关: ws://<本机公网IP>:8888/ws/render"
echo "    - RTSP 备用通道    : rtsp://<本机公网IP>:8554/live"
echo "    - 鉴权 Token       : ${AUTH_TOKEN} (请在本地控制台 API 参数中心配置)"
echo ""
echo "[提示] 本地端操作: 控制台 -> API 参数设置 -> 端云分离节点 -> 填入地址与 Token -> 保存并勾选激活"
