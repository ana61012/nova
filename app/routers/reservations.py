from fastapi import APIRouter, Depends, Header, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import get_session
from ..metrics import RESERVATIONS
from ..models import Product
from ..ratelimit import rate_limit
from ..schemas import LineOut, ReservationOut, ReserveIn
from ..security import current_user
from ..services import inventory, queue

router = APIRouter(prefix="/api/reservations", tags=["reservations"])


@router.post("", response_model=ReservationOut, status_code=201)
async def create_reservation(
    body: ReserveIn,
    response: Response,
    idempotency_key: str = Header(..., min_length=8, max_length=64),
    user_id: str = Depends(current_user),
    _: None = Depends(rate_limit),
    session: AsyncSession = Depends(get_session),
):
    """Hold a whole cart, all or nothing. Retrying with the same Idempotency-Key is always safe."""
    # The same product listed twice is merged into one line.
    lines = inventory.normalize((line.product_id, line.qty) for line in body.items)
    if len(lines) > settings.max_cart_lines:
        raise HTTPException(422, f"At most {settings.max_cart_lines} different items per cart")
    for _pid, qty in lines:
        if qty > settings.max_qty_per_product:
            raise HTTPException(422, f"At most {settings.max_qty_per_product} of one item per customer")

    rows = (
        await session.execute(select(Product).where(Product.id.in_([pid for pid, _q in lines])))
    ).scalars().all()
    # Plain values: reserve() may roll back, which expires ORM objects.
    names = {p.id: p.name for p in rows}
    drop_ids = {p.drop_id for p in rows}
    for pid, _q in lines:
        if pid not in names:
            raise HTTPException(404, f"Unknown product: {pid}")
    if len(drop_ids) != 1:
        raise HTTPException(422, "All items in a cart must come from the same drop")
    (drop_id,) = drop_ids

    # The waiting room is the front door: no admission, no stock.
    if settings.queue_required and not await queue.is_admitted(session, drop_id, user_id):
        RESERVATIONS.labels("not_admitted").inc()
        raise HTTPException(403, "Join the queue and wait to be admitted")

    try:
        reservation, created = await inventory.reserve(
            session,
            user_id=user_id,
            items=lines,
            idempotency_key=idempotency_key,
            ttl_seconds=settings.reservation_ttl_seconds,
        )
    except inventory.SoldOut as exc:
        RESERVATIONS.labels("sold_out").inc()
        raise HTTPException(409, f"Sold out: {names[exc.product_id]}")
    except inventory.LimitExceeded as exc:
        RESERVATIONS.labels("limit_exceeded").inc()
        raise HTTPException(
            409, f"Limit of {exc.limit} per customer reached for {names[exc.product_id]}"
        )
    RESERVATIONS.labels("created" if created else "replay").inc()
    if not created:
        response.status_code = 200  # replay of an earlier request

    saved = await inventory.reservation_lines(session, reservation.id)
    return ReservationOut(
        id=reservation.id,
        user_id=reservation.user_id,
        status=reservation.status.value,
        expires_at=reservation.expires_at,
        items=[LineOut(product_id=pid, qty=qty) for pid, qty in saved],
    )


@router.delete("/{reservation_id}", status_code=204)
async def cancel_reservation(
    reservation_id: str,
    user_id: str = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    if not await inventory.release(session, reservation_id, user_id):
        raise HTTPException(404, "No active reservation")