from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import get_session
from ..models import Drop, Inventory, Product
from ..schemas import DropOut, LimitsOut, ProductOut

router = APIRouter(prefix="/api/drops", tags=["drops"])


@router.get("/current", response_model=DropOut)
async def current_drop(session: AsyncSession = Depends(get_session)):
    drop = (await session.execute(select(Drop).order_by(Drop.id.desc()).limit(1))).scalar_one_or_none()
    if drop is None:
        raise HTTPException(404, "No drop scheduled")

    rows = (
        await session.execute(
            select(Product, Inventory)
            .join(Inventory, Inventory.product_id == Product.id)
            .where(Product.drop_id == drop.id)
            .order_by(Product.id)
        )
    ).all()
    return DropOut(
        id=drop.id,
        name=drop.name,
        starts_at=drop.starts_at,
        limits=LimitsOut(
            max_qty_per_product=settings.max_qty_per_product,
            max_cart_lines=settings.max_cart_lines,
        ),
        products=[
            ProductOut(
                id=p.id, name=p.name, price_cents=p.price_cents, image_emoji=p.image_emoji,
                total=inv.total, available=inv.available,
            )
            for p, inv in rows
        ],
    )