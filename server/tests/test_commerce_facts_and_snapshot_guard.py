import asyncio
from unittest.mock import AsyncMock, patch

from server.core.commerce.facts_service import (
    ProductFact,
    ProductFactService,
    global_product_fact_service,
)
from server.core.guardrails.price_auditor import PriceAuditor
from server.core.roles.ecommerce_anchor import EcommerceAnchorRole
from server.core.tools.tool_bus import query_stock


def test_product_fact_validation_rules():
    """验证商品结构化事实快照准入校验规则"""
    # 正常商品事实
    valid_fact = ProductFact(
        sku="SKU-8888",
        title="高端蚕丝被",
        category="家纺",
        live_price=299.0,
        original_price=599.0,
        floor_price=250.0,
        current_stock=100,
    )
    assert valid_fact.is_valid() is True

    # 标价非法 (<= 0)
    invalid_price_fact = ProductFact(
        sku="SKU-8888",
        title="高端蚕丝被",
        category="家纺",
        live_price=0.0,
        original_price=599.0,
        floor_price=0.0,
        current_stock=100,
    )
    assert invalid_price_fact.is_valid() is False

    # 缺失 SKU
    invalid_sku_fact = ProductFact(
        sku="",
        title="高端蚕丝被",
        category="家纺",
        live_price=299.0,
        original_price=599.0,
        floor_price=250.0,
        current_stock=100,
    )
    assert invalid_sku_fact.is_valid() is False


def test_fact_service_circuit_breaker():
    """验证事实服务熔断时返回不可用"""
    svc = ProductFactService()
    svc.set_circuit_breaker(True, "库存同步通道断开")

    async def _run():
        fact, err = await svc.get_verified_product({
            "sku": "SKU-001",
            "title": "咖啡豆",
            "live_price": 50.0,
        })
        assert fact is None
        assert "商品事实服务不可用" in err

        res = await svc.query_realtime_stock(sku="SKU-001")
        assert res["ok"] is False
        assert res["error"] == "FACT_SERVICE_UNAVAILABLE"

    asyncio.run(_run())


def test_query_stock_tool_fail_closed():
    """验证 query_stock 工具在事实源不可用时 Fail-Closed"""
    async def _run():
        with patch.object(global_product_fact_service, "query_realtime_stock", new_callable=AsyncMock) as mock_q:
            mock_q.return_value = {
                "ok": False,
                "found": False,
                "error": "FACT_SERVICE_UNAVAILABLE",
                "message": "服务熔断",
            }
            res = await query_stock(sku="SKU-999")
            assert res["ok"] is False
            assert res["error"] == "FACT_SERVICE_UNAVAILABLE"

    asyncio.run(_run())


def test_price_auditor_blocks_price_when_no_fact_exists():
    """验证无商品事实时大模型口播了虚假价格被强制阻断"""
    # 无任何商品价格事实
    sentence = "家人们今天只要199元就能包邮带回家！"
    modified, triggered, detail = PriceAuditor.audit_sentence(
        sentence, current_product=None, all_products=[]
    )
    assert triggered is True
    assert detail["reason"] == "MISSING_PRICE_FACT"
    assert "199" not in modified
    assert "以小黄车为准" in modified


def test_ecommerce_anchor_blocks_pitch_when_fact_unavailable():
    """验证电商主播在商品事实不可用时阻断带货价格话术，改播安全互动"""
    async def _run():
        role = EcommerceAnchorRole()
        invalid_product = {"sku": "", "title": "未核验商品", "live_price": -10}

        chunks = []
        async for chunk in role.process_event(
            "idle_filler", "系统调度", {}, {"products": [invalid_product]}
        ):
            chunks.append(chunk)

        result_text = "".join(chunks)
        # 必须阻断具体价格口播并回退安全话术
        assert "小黄车商品信息与专享优惠正在实时同步中" in result_text
        assert "-10" not in result_text

    asyncio.run(_run())
