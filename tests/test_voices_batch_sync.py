import sys
import os
import pytest
from pathlib import Path

# 添加项目根路径
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncio
from fastapi.testclient import TestClient
from sqlalchemy import select, delete
from server.app import app
from server.database.db import AsyncSessionLocal
from server.database.models import VoiceProfile

client = TestClient(app)

async def _verify_and_cleanup(test_voices):
    # 3. 验证数据库中确实存在，并模拟改名
    async with AsyncSessionLocal() as db:
        stmt = select(VoiceProfile).where(VoiceProfile.id == "zh-CN-TestVoice01")
        result = await db.execute(stmt)
        v_item = result.scalars().first()
        assert v_item is not None, "未找到 zh-CN-TestVoice01"
        v_item.name = "艾米专属解说音（已重命名）"
        await db.commit()

    # 再次执行 batch-sync，模拟已有音色被重命名后，绝不覆盖用户的自定义名称
    resp_repeat = client.post("/api/v1/voices/batch-sync", json={
        "provider_name": "edge_tts",
        "voices": [{"id": "zh-CN-TestVoice01", "name": "测试预设音色 1", "provider_name": "edge_tts", "voice_type": "preset"}]
    })
    assert resp_repeat.status_code == 200

    async with AsyncSessionLocal() as db:
        stmt = select(VoiceProfile).where(VoiceProfile.id == "zh-CN-TestVoice01")
        result = await db.execute(stmt)
        v_item_after = result.scalars().first()
        assert v_item_after.name == "艾米专属解说音（已重命名）", "用户重命名的音色名称应受保护不被覆盖"

        # 清理测试数据
        for v in test_voices:
            del_stmt = delete(VoiceProfile).where(VoiceProfile.id == v["id"])
            await db.execute(del_stmt)
        await db.commit()

def test_voices_batch_sync_and_filter():
    # 1. 模拟前端从 TTS 引擎获取到的 16 个音色（15 个官方预设 + 1 个专属克隆）
    test_voices = [
        {"id": f"zh-CN-TestVoice{i:02d}", "name": f"测试预设音色 {i}", "provider_name": "edge_tts", "voice_type": "preset"}
        for i in range(1, 16)
    ]
    test_voices.append({
        "id": "cosyvoice-clone-custom-001",
        "name": "主播专属克隆·测试音色",
        "provider_name": "cosyvoice",
        "voice_type": "cloned"
    })

    # 2. 调用 POST /api/v1/voices/batch-sync
    resp = client.post("/api/v1/voices/batch-sync", json={
        "provider_name": "edge_tts",
        "voices": test_voices
    })
    assert resp.status_code == 200, f"batch-sync 失败: {resp.text}"
    data = resp.json()
    assert data["code"] == 0
    assert data["total"] >= 16
    assert len(data["data"]) >= 16

    # 3. 测试按 provider_name 过滤查询
    resp_edge = client.get("/api/v1/voices/list?provider_name=edge_tts")
    assert resp_edge.status_code == 200
    edge_list = resp_edge.json()["data"]
    for v in edge_list:
        assert v["provider_name"] == "edge_tts"
    assert any(v["id"] == "zh-CN-TestVoice01" for v in edge_list)

    resp_cosy = client.get("/api/v1/voices/list?provider_name=cosyvoice")
    assert resp_cosy.status_code == 200
    cosy_list = resp_cosy.json()["data"]
    for v in cosy_list:
        assert v["provider_name"] == "cosyvoice"
    assert any(v["id"] == "cosyvoice-clone-custom-001" for v in cosy_list)

    asyncio.run(_verify_and_cleanup(test_voices))
    print("[SUCCESS] test_voices_batch_sync_and_filter 测试全部通过！")

if __name__ == "__main__":
    test_voices_batch_sync_and_filter()


if __name__ == "__main__":
    test_voices_batch_sync_and_filter()
