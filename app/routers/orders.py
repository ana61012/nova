import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import get_session
from ..metrics import WEBHOOKS
from ..models import Order
from ..ratelimit import rate_limit
from ..schemas import OrderIn, OrderItemOut, OrderOut
from ..security import current_user
from ..services import orders as svc

router = APIRouter(tags=["orders"])

_EVENT_TYPES = {"success": "payment.succeeded", "fail": "payment.failed", "refund": "payment.refunded"}


async def _own_order(session: AsyncSession, order_id: str, user_id: str) -> Order:
    order = (
        await session.execute(select(Order).where(Order.id == order_id, Order.user_id == user_id))
    ).scalar_one_or_none()
    if order is None:
        raise HTTPException(404, "Order not found")
    return order


async def _order_out(session: AsyncSession, order: Order) -> OrderOut:
    items = await svc.order_items(session, order.id)
    return OrderOut(
        id=order.id,
        reservation_id=order.reservation_id,
        amount_cents=order.amount_cents,
        status=order.status.value,
        payment_intent_id=order.payment_intent_id,
        items=[
            OrderItemOut(product_id=i.product_id, qty=i.qty, unit_price_cents=i.unit_price_cents)
            for i in items
        ],
    )


@router.post("/api/orders", response_model=OrderOut, status_code=201)
async def create_order(
    body: OrderIn,
    user_id: str = Depends(current_user),
    _: None = Depends(rate_limit),
    session: AsyncSession = Depends(get_session),
):
    try:
        order = await svc.create_order(session, user_id=user_id, reservation_id=body.reservation_id)
    except svc.ReservationInvalid:
        raise HTTPException(409, "Reservation is missing, expired or not yours")
    return await _order_out(session, order)


@router.get("/api/orders/{order_id}", response_model=OrderOut)
async def get_order(
    order_id: str, user_id: str = Depends(current_user), session: AsyncSession = Depends(get_session)
):
    return await _order_out(session, await _own_order(session, order_id, user_id))


@router.post("/webhooks/payments")
async def payment_webhook(request: Request, session: AsyncSession = Depends(get_session)):
    """Entry point a real gateway would call. Authenticated by HMAC signature."""
    raw = await request.body()
    try:
        result = await svc.process_webhook(session, raw, request.headers.get("X-Signature"))
    except svc.InvalidSignature:
        raise HTTPException(401, "Bad signature")
    except (json.JSONDecodeError, KeyError):
        raise HTTPException(400, "Malformed event")
    except svc.InvalidTransition:
        raise HTTPException(409, "Conflicting state, retry later")  # gateway retries on non-2xx
    WEBHOOKS.labels(result["outcome"]).inc()
    return result


@router.post("/api/dev/payments/{order_id}/simulate")
async def simulate_payment(
    order_id: str,
    outcome: str = "success",
    duplicate: bool = False,
    user_id: str = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    """Stand-in for the payment gateway: builds a signed event and feeds it to the
    same code path as the real webhook. `duplicate=true` delivers it twice."""
    if not settings.dev_mode:
        raise HTTPException(404, "Not found")
    if outcome not in _EVENT_TYPES:
        raise HTTPException(422, f"outcome must be one of {sorted(_EVENT_TYPES)}")
    order = await _own_order(session, order_id, user_id)

    event = {
        "id": "evt_" + uuid.uuid4().hex,
        "type": _EVENT_TYPES[outcome],
        "data": {"payment_intent_id": order.payment_intent_id},
    }
    raw = json.dumps(event).encode()
    signature = svc.sign(raw)
    outcomes = []
    for _ in range(2 if duplicate else 1):
        result = await svc.process_webhook(session, raw, signature)
        WEBHOOKS.labels(result["outcome"]).inc()
        outcomes.append(result["outcome"])
    fresh = (await session.execute(select(Order.status).where(Order.id == order_id))).scalar_one()
    return {"outcomes": outcomes, "order_status": fresh.value}