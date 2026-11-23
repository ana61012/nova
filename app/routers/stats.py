from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..models import (
    Drop, Inventory, Order, OrderStatus, OutboxEvent, Product, QueueEntry, Reservation,
    ReservationItem, ReservationStatus, SalesStat,
)
from ..security import require_admin
from ..services import queue

router = APIRouter(prefix="/api", tags=["stats"], dependencies=[Depends(require_admin)])


@router.get("/stats")
async def stats(session: AsyncSession = Depends(get_session)):
    """Everything the dashboard shows, plus an inventory audit.

    The audit is the oversell detector: for every product,
    available + held + sold must equal total.
    """
    drop_id = await queue.current_drop_id(session)
    drop = await session.get(Drop, drop_id) if drop_id else None

    held: dict[tuple[int, ReservationStatus], int] = {
        (pid, st): qty
        for pid, st, qty in (
            await session.execute(
                select(ReservationItem.product_id, Reservation.status, func.sum(ReservationItem.qty))
                .join(Reservation, Reservation.id == ReservationItem.reservation_id)
                .group_by(ReservationItem.product_id, Reservation.status)
            )
        ).all()
    }
    products = []
    for p, inv in (
        await session.execute(
            select(Product, Inventory).join(Inventory, Inventory.product_id == Product.id).order_by(Product.id)
        )
    ).all():
        active = held.get((p.id, ReservationStatus.ACTIVE), 0)
        sold = held.get((p.id, ReservationStatus.CONVERTED), 0)
        products.append({
            "id": p.id, "name": p.name, "emoji": p.image_emoji, "total": inv.total,
            "available": inv.available, "held": active, "sold": sold,
            "consistent": inv.available + active + sold == inv.total and inv.available >= 0,
        })

    queue_counts = {
        st.value: n
        for st, n in (
            await session.execute(
                select(QueueEntry.status, func.count()).where(QueueEntry.drop_id == drop_id).group_by(QueueEntry.status)
            )
        ).all()
    } if drop_id else {}

    order_rows = (await session.execute(select(Order.status, func.count(), func.sum(Order.amount_cents)).group_by(Order.status))).all()
    order_counts = {st.value: n for st, n, _ in order_rows}
    revenue = sum(total or 0 for st, _, total in order_rows if st == OrderStatus.PAID)

    outbox_pending = (
        await session.execute(select(func.count()).select_from(OutboxEvent).where(OutboxEvent.published_at.is_(None)))
    ).scalar_one()
    analytics = (await session.execute(select(func.sum(SalesStat.orders), func.sum(SalesStat.revenue_cents)))).one()

    return {
        "drop": drop.name if drop else None,
        "products": products,
        "all_consistent": all(p["consistent"] for p in products),
        "queue": {"waiting": queue_counts.get("waiting", 0), "admitted": queue_counts.get("admitted", 0)},
        "orders": {"by_status": order_counts, "total": sum(order_counts.values()), "revenue_cents": revenue},
        "outbox_pending": outbox_pending,
        "analytics": {"orders": analytics[0] or 0, "revenue_cents": analytics[1] or 0},
    }