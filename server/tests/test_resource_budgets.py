import io
import wave
from pathlib import Path

import pytest
from fastapi import UploadFile
from fastapi.testclient import TestClient
from PIL import Image

from server.app import app
from server.config import DATA_DIR
from server.core import resource_limits


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


def png_bytes(width=8, height=8):
    output = io.BytesIO()
    Image.new("RGB", (width, height), color=(20, 40, 80)).save(output, format="PNG")
    return output.getvalue()


def wav_bytes(duration_sec: float, rate: int = 8000):
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(b"\x00\x00" * int(duration_sec * rate))
    return output.getvalue()


def test_stage_upload_is_chunked_and_cleans_oversize(tmp_path):
    class TrackedUpload:
        def __init__(self):
            self.calls = []
            self.remaining = b"x" * 20

        async def read(self, size):
            self.calls.append(size)
            chunk, self.remaining = self.remaining[:size], self.remaining[size:]
            return chunk

    import asyncio
    upload = TrackedUpload()
    with pytest.raises(Exception):
        asyncio.run(resource_limits.stage_upload(upload, tmp_path, "tracked", 10))
    assert upload.calls and all(size == resource_limits.UPLOAD_CHUNK_BYTES for size in upload.calls)
    assert list(tmp_path.glob("*.part")) == []


def test_product_and_config_model_budgets(client):
    too_many_images = client.post("/api/v1/products/upsert", json={
        "sku_code": "BUDGET-SKU", "title": "预算商品", "images": ["x"] * 9,
    })
    assert too_many_images.status_code == 422

    assert client.post("/api/v1/settings/vision", json={
        "enabled": True, "source": "desktop_screen", "interval_sec": 0.01,
    }).status_code == 422
    assert client.post("/api/v1/settings/audio-device", json={"device_index": -1}).status_code == 422
    assert client.post("/api/v1/settings/tts/preview", json={
        "provider_name": "edge_tts", "text": "x" * 2001,
    }).status_code == 422
    assert client.post("/api/v1/knowledge/search", json={"query": "x", "top_k": 51}).status_code == 422


def test_image_pixel_budget_and_failed_anchor_cleanup(client, monkeypatch):
    monkeypatch.setattr(resource_limits, "MAX_IMAGE_PIXELS", 100)
    product_dir = DATA_DIR / "product_images"
    before_products = set(product_dir.iterdir())
    response = client.post(
        "/api/v1/products/upload-image",
        files={"file": ("large.png", png_bytes(20, 20), "image/png")},
    )
    assert response.status_code == 413
    assert set(product_dir.iterdir()) == before_products

    anchor_dir = DATA_DIR / "anchors"
    before_anchors = set(anchor_dir.iterdir())
    response = client.post(
        "/api/v1/anchors/create",
        data={"name": "预算失败主播"},
        files=[
            ("portrait", ("valid.png", png_bytes(8, 8), "image/png")),
            ("full_body", ("large.png", png_bytes(20, 20), "image/png")),
        ],
    )
    assert response.status_code == 413
    assert set(anchor_dir.iterdir()) == before_anchors


def test_voice_duration_budget_cleans_artifacts(client):
    voice_dir = DATA_DIR / "voices"
    before = set(voice_dir.iterdir())
    response = client.post(
        "/api/v1/voices/clone",
        data={"name": "超时声音样本", "speed": 1.0, "volume": 1.0},
        files={"audio_file": ("long.wav", wav_bytes(31), "audio/wav")},
    )
    assert response.status_code == 413
    assert set(voice_dir.iterdir()) == before


def test_guardrail_and_knowledge_expansion_budgets(client, monkeypatch):
    monkeypatch.setattr("server.routes.guardrails.MAX_GUARDRAIL_ROWS", 3)
    response = client.post("/api/v1/guardrails/words/batch", json={
        "words": "预算词甲,预算词乙,预算词丙,预算词丁",
        "replacement_word": "合规词",
    })
    assert response.status_code == 413
    listed = client.get("/api/v1/guardrails/words").json()["data"]
    assert not any(item["word"].startswith("预算词") for item in listed)

    monkeypatch.setattr("server.routes.knowledge.MAX_KNOWLEDGE_TEXT_CHARS", 100)
    tmp_dir = DATA_DIR / "tmp" / "knowledge"
    response = client.post(
        "/api/v1/knowledge/upload",
        data={"doc_name": "超长文档"},
        files={"file": ("long.txt", ("知识" * 60).encode(), "text/plain")},
    )
    assert response.status_code == 413
    assert not tmp_dir.exists() or list(tmp_dir.glob("*.part")) == []


def test_rag_index_failure_discards_database_and_memory(monkeypatch):
    import asyncio
    from sqlalchemy import select
    from server.core.rag.engine import KnowledgeBaseEngine
    from server.database.db import AsyncSessionLocal
    from server.database.models import KnowledgeChunk

    engine = KnowledgeBaseEngine()

    def fail_build():
        raise RuntimeError("index build failed")

    monkeypatch.setattr(engine, "_build_index", fail_build)
    with pytest.raises(RuntimeError, match="index build failed"):
        asyncio.run(engine.ingest_document("预算原子性文档", "这是用于验证索引失败补偿清理的文档内容。"))
    assert engine.get_status()["chunks"] == 0

    async def count_rows():
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(KnowledgeChunk).where(KnowledgeChunk.doc_name == "预算原子性文档")
            )
            return len(result.scalars().all())

    assert asyncio.run(count_rows()) == 0
