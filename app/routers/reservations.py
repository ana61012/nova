from fastapi import APIRouter, Depends, Header, HTTPException, Response
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import get_session
from ..metrics import RESERVATIONS
from ..models import Product
from ..ratelimit import rate_limit
from ..schemas import ReservationOut, ReserveIn
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
    """Hold stock for a buyer. Retrying with the same Idempotency-Key is always safe."""
    if body.qty > settings.max_qty_per_order:
        raise HTTPException(422, f"Max {settings.max_qty_per_order} per order")

    product = await session.get(Product, body.product_id)
    if product is None:
        raise HTTPException(404, "Unknown product")

    # The waiting room is the front door: no admission, no stock.
    if settings.queue_required and not await queue.is_admitted(session, product.drop_id, user_id):
        RESERVATIONS.labels("not_admitted").inc()
        raise HTTPException(403, "Join the queue and wait to be admitted")

    try:
        reservation, created = await inventory.reserve(
            session,
            product_id=body.product_id,
            user_id=user_id,
            qty=body.qty,
            idempotency_key=idempotency_key,
            ttl_seconds=settings.reservation_ttl_seconds,
        )
    except inventory.SoldOut:
        RESERVATIONS.labels("sold_out").inc()
        raise HTTPException(409, "Sold out")
    except inventory.AlreadyPurchased:
        RESERVATIONS.labels("already_purchased").inc()
        raise HTTPException(409, "You already have this item (one per customer)")
    RESERVATIONS.labels("created" if created else "replay").inc()
    if not created:
        response.status_code = 200  # replay of an earlier request
    return reservation


@router.delete("/{reservation_id}", status_code=204)
async def cancel_reservation(
    reservation_id: str,
    user_id: str = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    if not await inventory.release(session, reservation_id, user_id):
        raise HTTPException(404, "No active reservation")
