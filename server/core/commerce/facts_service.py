# -*- coding: utf-8 -*-
"""
电商结构化商品事实服务 (依据《商业化运营改造方案》v3.0.0 第 4.2 D 与 7.2 节)
AI 只能读取经过验证的结构化商品快照，不得自行决定售价、库存、优惠额度与承诺。
当事实服务不可用或商品事实未验证时，严格 Fail-Closed 阻断相关商品话术。
"""
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("LiveAgent.ProductFactService")


@dataclass(frozen=True)
class ProductFact:
    """不可变结构化商品事实快照"""
    sku: str
    title: str
    category: str
    live_price: float
    original_price: float
    floor_price: float
    current_stock: int
    coupons: List[str] = field(default_factory=list)
    selling_points: List[str] = field(default_factory=list)
    shipping_promise: str = "48小时内发货"
    refund_policy: str = "七天无理由退换"
    snapshot_version: str = "v1"
    verified_at: float = field(default_factory=time.time)

    def is_valid(self) -> bool:
        """检查商品核心交易事实是否满足商业准入硬门禁"""
        if not self.sku or not self.sku.strip():
            return False
        if not self.title or not self.title.strip():
            return False
        if self.live_price <= 0:
            return False
        if self.floor_price <= 0 or self.floor_price > self.live_price:
            return False
        if self.current_stock < 0:
            return False
        return True


class ProductFactService:
    """商品事实与权威裁决服务中心"""

    def __init__(self):
        self._circuit_broken: bool = False
        self._break_reason: Optional[str] = None

    def set_circuit_breaker(self, broken: bool, reason: str = ""):
        """用于故障注入或事实源宕机时的主动熔断"""
        self._circuit_broken = broken
        self._break_reason = reason if broken else None

    @property
    def is_healthy(self) -> bool:
        return not self._circuit_broken

    @staticmethod
    def create_fact_from_dict(prod: Dict[str, Any]) -> Tuple[Optional[ProductFact], Optional[str]]:
        """从字典构建并严格校验商品事实"""
        sku = str(prod.get("sku") or prod.get("sku_code") or "").strip()
        title = str(prod.get("title") or "").strip()
        try:
            live_price = float(prod.get("live_price") or 0.0)
            orig_price = float(prod.get("original_price") or live_price)
        except (ValueError, TypeError):
            return None, "价格格式解析异常"

        if not sku:
            return None, "缺失商品 SKU 标识"
        if not title:
            return None, "缺失商品名称"
        if live_price <= 0:
            return None, f"商品 {sku} 标价非法 ({live_price} <= 0)"

        # 默认底价为标价的 85%
        floor_price = round(live_price * 0.85, 2)
        try:
            stock = int(prod.get("current_stock") or 0)
        except (ValueError, TypeError):
            stock = 0

        fact = ProductFact(
            sku=sku,
            title=title,
            category=str(prod.get("category") or "通用"),
            live_price=live_price,
            original_price=orig_price,
            floor_price=floor_price,
            current_stock=stock,
            coupons=list(prod.get("coupons") or []),
            selling_points=list(prod.get("selling_points") or []),
            snapshot_version=str(prod.get("snapshot_version") or "v1"),
            verified_at=time.time(),
        )
        if not fact.is_valid():
            return None, "商品事实校验未通过"
        return fact, None

    async def get_verified_product(self, product_data: Optional[Dict[str, Any]]) -> Tuple[Optional[ProductFact], Optional[str]]:
        """
        获取经过事实核验的商品快照。
        若事实服务熔断或数据不合规，返回 (None, reason)。
        """
        if self._circuit_broken:
            return None, f"商品事实服务不可用: {self._break_reason or 'circuit_open'}"

        if not product_data:
            return None, "未指定商品上下文"

        return self.create_fact_from_dict(product_data)

    async def query_realtime_stock(self, sku: str = "", title: str = "") -> Dict[str, Any]:
        """查询事实库存 (由服务端事实中心统一裁决)"""
        if self._circuit_broken:
            logger.error("商品事实服务已熔断，拒绝提供库存事实数据")
            return {
                "ok": False,
                "found": False,
                "error": "FACT_SERVICE_UNAVAILABLE",
                "message": f"事实服务不可用: {self._break_reason or 'circuit_open'}",
            }

        from sqlalchemy import select
        from server.database.db import AsyncSessionLocal
        from server.database.models import Product

        try:
            async with AsyncSessionLocal() as session:
                query = select(Product).where(Product.is_active == 1)
                if sku:
                    query = query.where(Product.sku_code == sku)
                elif title:
                    query = query.where(Product.title.contains(title))
                res = await session.execute(query.limit(1))
                product = res.scalar_one_or_none()

            if not product:
                return {"ok": True, "found": False, "title": title or sku, "stock": 0}

            fact, err = self.create_fact_from_dict({
                "sku": product.sku_code,
                "title": product.title,
                "live_price": product.live_price,
                "original_price": product.original_price,
                "current_stock": product.current_stock,
            })
            if not fact:
                return {"ok": False, "found": True, "error": "FACT_INVALID", "message": err}

            return {
                "ok": True,
                "found": True,
                "title": fact.title,
                "sku": fact.sku,
                "stock": fact.current_stock,
                "live_price": fact.live_price,
                "floor_price": fact.floor_price,
            }
        except Exception as exc:
            logger.exception("事实服务查询数据库异常: %s", exc)
            return {
                "ok": False,
                "found": False,
                "error": "DATABASE_ERROR",
                "message": str(exc),
            }


global_product_fact_service = ProductFactService()
