import uuid
import json
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from pydantic import BaseModel, Field, model_validator
from typing import List, Optional, Any
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from server.database.db import get_db
from server.database.models import Product
from server.config import DATA_DIR
from server.core.resource_limits import (
    MAX_IMAGE_BYTES,
    cleanup_paths,
    publish_staged,
    stage_upload,
    validate_image_budget,
)

router = APIRouter(prefix="/products", tags=["商品管理"])

PRODUCT_IMAGES_DIR = DATA_DIR / "product_images"
PRODUCT_IMAGES_DIR.mkdir(parents=True, exist_ok=True)


def _json_value(raw: Any, fallback: Any) -> Any:
    if not raw:
        return fallback
    try:
        value = json.loads(str(raw))
    except (TypeError, json.JSONDecodeError):
        return fallback
    return value if isinstance(value, type(fallback)) else fallback


def _safe_float(val: Any, fallback: float = 0.0) -> float:
    try:
        return float(val) if val is not None else fallback
    except (TypeError, ValueError):
        return fallback


def _safe_int(val: Any, fallback: int = 0) -> int:
    try:
        return int(val) if val is not None else fallback
    except (TypeError, ValueError):
        return fallback


def product_to_context(product: Product) -> dict:
    """将数据库商品转换为直播运行时结构，并限制单项体积。"""
    return {
        "id": str(product.id),
        "sku": str(product.sku_code),
        "title": str(product.title or "")[:200],
        "category": str(product.category or "")[:100],
        "original_price": _safe_float(product.original_price, 0.0),
        "live_price": _safe_float(product.live_price, 0.0),
        "current_stock": _safe_int(product.current_stock, 0),
        "selling_points": _json_value(product.selling_points, [])[:12],
        "faq_data": _json_value(product.faq_data, [])[:20],
        "size_chart": _json_value(product.size_chart, {}),
        "coupon_script": str(product.coupon_script or "")[:1000],
        "description": str(product.description or "")[:2000],
        "images": _json_value(product.images, [])[:8],
    }


class ProductUpsertRequest(BaseModel):
    id: Optional[str] = Field(default=None, max_length=64)
    sku_code: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=256)
    category: Optional[str] = Field(default="通用", max_length=64)
    original_price: float = Field(default=0.0, ge=0, le=100_000_000)
    live_price: float = Field(default=0.0, ge=0, le=100_000_000)
    current_stock: int = Field(default=100, ge=0, le=100_000_000)
    selling_points: List[str] = Field(default_factory=list, max_length=12)
    faq_data: List[dict] = Field(default_factory=list, max_length=20)
    size_chart: dict = Field(default_factory=dict)
    coupon_script: Optional[str] = Field(default="", max_length=1000)
    description: Optional[str] = Field(default="", max_length=2000)
    images: List[str] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def enforce_nested_budgets(self):
        if any(len(item) > 500 for item in self.selling_points):
            raise ValueError("单条商品卖点最多 500 字")
        if any(len(item) > 512 for item in self.images):
            raise ValueError("单个图片路径最多 512 字")
        nested = json.dumps(
            {"faq_data": self.faq_data, "size_chart": self.size_chart},
            ensure_ascii=False,
        )
        if len(nested) > 100_000:
            raise ValueError("FAQ 与尺码表展开量超过 10 万字符预算")
        return self


@router.get("/list")
async def list_products(db: AsyncSession = Depends(get_db)):
    """获取所有电商直播带货商品。"""
    result = await db.execute(select(Product))
    products = result.scalars().all()
    return {
        "code": 0,
        "total": len(products),
        "data": [
            {
                "id": str(p.id),
                "sku_code": str(p.sku_code),
                "title": str(p.title or ""),
                "category": str(p.category or ""),
                "original_price": _safe_float(p.original_price, 0.0),
                "live_price": _safe_float(p.live_price, 0.0),
                "current_stock": _safe_int(p.current_stock, 0),
                "selling_points": _json_value(p.selling_points, []),
                "faq_data": _json_value(p.faq_data, []),
                "size_chart": _json_value(p.size_chart, {}),
                "description": str(p.description or ""),
                "images": _json_value(p.images, []),
                "coupon_script": str(p.coupon_script or ""),
                "is_active": bool(p.is_active),
            }
            for p in products
        ],
    }


@router.post("/upsert")
async def upsert_product(req: ProductUpsertRequest, db: AsyncSession = Depends(get_db)):
    """添加或更新完整商品卡片。"""
    record = None
    if req.id:
        res = await db.execute(select(Product).where(Product.id == req.id))
        record = res.scalar_one_or_none()

    if not record:
        res = await db.execute(select(Product).where(Product.sku_code == req.sku_code))
        record = res.scalar_one_or_none()

    values = {
        "sku_code": req.sku_code,
        "title": req.title,
        "category": req.category,
        "original_price": req.original_price,
        "live_price": req.live_price,
        "current_stock": req.current_stock,
        "selling_points": json.dumps(req.selling_points, ensure_ascii=False),
        "faq_data": json.dumps(req.faq_data, ensure_ascii=False),
        "size_chart": json.dumps(req.size_chart, ensure_ascii=False),
        "coupon_script": req.coupon_script or "",
        "description": req.description or "",
        "images": json.dumps(req.images, ensure_ascii=False),
        "is_active": 1,
    }

    if record:
        for key, value in values.items():
            setattr(record, key, value)
    else:
        record = Product(id=f"prod_{uuid.uuid4().hex[:8]}", **values)
        db.add(record)

    await db.commit()

    # 直播运行中无感热重载商品上下文
    from server.routes.live import global_live_controller
    if global_live_controller.is_live:
        await global_live_controller.reload_runtime_config("products")

    return {"code": 0, "message": "商品保存成功", "data": {"id": record.id}}


@router.post("/upload-image")
async def upload_product_image(file: UploadFile = File(...)):
    """上传有界商品图片，验证通过后原子发布。"""
    if not file.filename:
        raise HTTPException(status_code=400, detail="未选择图片文件")
    ext = Path(file.filename).suffix.lower() or ".jpg"
    if ext not in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
        raise HTTPException(status_code=400, detail="仅支持 jpg/png/webp/gif 图片格式")
    staged = target = None
    try:
        staged, _ = await stage_upload(file, PRODUCT_IMAGES_DIR, "product", MAX_IMAGE_BYTES)
        validate_image_budget(staged)
        image_id = f"img_{uuid.uuid4().hex[:10]}"
        target = PRODUCT_IMAGES_DIR / f"{image_id}{ext}"
        publish_staged(staged, target)
        return {"code": 0, "data": {"path": target.as_posix()}}
    except BaseException:
        cleanup_paths([staged, target])
        raise


@router.post("/{prod_id}/flash-sale")
async def flash_sale(prod_id: str, db: AsyncSession = Depends(get_db)):
    """
    立即促单逼单：以 P0 优先级抢占直播话术通道
    AI 主播立刻中断当前内容，转而播报该商品的限时逼单话术
    """
    res = await db.execute(select(Product).where(Product.id == prod_id))
    product = res.scalar_one_or_none()
    if not product:
        raise HTTPException(status_code=404, detail="商品不存在")

    from server.routes.live import global_live_controller
    if not global_live_controller.is_live:
        raise HTTPException(status_code=400, detail="直播间尚未开播，请先在直播大屏一键开播")

    sku_code = str(product.sku_code or "")
    title = str(product.title or "")
    live_price = _safe_float(product.live_price, 0.0)
    current_stock = _safe_int(product.current_stock, 0)
    promo_text = str(product.coupon_script or "") or (
        f"家人们注意了！{title}直播专享价只要{live_price}元！"
        f"库存只剩{current_stock}件，拍一件少一件，马上点击小黄车{sku_code}号链接下单！"
    )
    await global_live_controller.event_queue.put(
        event_id=f"evt_flash_{uuid.uuid4().hex[:6]}",
        event_type="chat",
        user_name="运营促单指令",
        payload={"text": promo_text, "flash_sale": True, "sku": sku_code},
        priority=0
    )

    from server.routes.ws_live import ws_manager
    global_live_controller.stats["flash_sales"] += 1
    await ws_manager.broadcast("FLASH_SALE", {
        "sku": sku_code,
        "title": title,
        "live_price": live_price,
        "stock": current_stock
    })
    return {"code": 0, "message": f"【{title}】促单逼单指令已抢占插播！"}


@router.delete("/{prod_id}")
async def delete_product(prod_id: str, db: AsyncSession = Depends(get_db)):
    """无历史引用时删除；已有订单时安全下架以保留审计记录。"""
    from server.database.models import Order
    record = await db.get(Product, prod_id)
    if not record:
        raise HTTPException(status_code=404, detail="商品不存在")
    referenced = (await db.execute(select(Order.id).where(Order.product_id == prod_id).limit(1))).first()
    if referenced:
        setattr(record, "is_active", 0)
        await db.commit()
        # 下架同样需要热重载，保证主播不再播报已下架商品
        from server.routes.live import global_live_controller
        if global_live_controller.is_live:
            await global_live_controller.reload_runtime_config("products")
        return {"code": 0, "message": "商品存在历史订单，已安全下架并保留记录", "soft_deleted": True}
    await db.delete(record)
    await db.commit()
    from server.routes.live import global_live_controller
    if global_live_controller.is_live:
        await global_live_controller.reload_runtime_config("products")
    return {"code": 0, "message": "商品已删除", "soft_deleted": False}
