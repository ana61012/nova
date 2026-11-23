"""Razorpay endpoints.

    GET  /api/payments/config               -> {provider, key_id}   (public; tells the page which checkout to use)
    POST /api/payments/razorpay/order       {order_id}  -> data for Razorpay Checkout
    POST /api/payments/razorpay/verify      {order_id, razorpay_payment_id, razorpay_order_id, razorpay_signature}
    POST /webhooks/razorpay                 Razorpay -> us, HMAC-signed
"""
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import get_session
from ..metrics import WEBHOOKS
from ..models import Order, OrderStatus
from ..ratelimit import rate_limit
from ..security import current_user
from ..services import orders as svc
from ..services import razorpay as rzp

router = APIRouter(tags=["payments"])


class RzpOrderIn(BaseModel):
    order_id: str


class RzpVerifyIn(BaseModel):
    order_id: str
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str


def _require_razorpay() -> None:
    if not settings.razorpay_enabled:
        raise HTTPException(404, "Razorpay is not enabled")


@router.get("/api/payments/config")
async def payments_config():
    if settings.razorpay_enabled:
        return {"provider": "razorpay", "key_id": settings.razorpay_key_id}
    return {"provider": "simulated", "key_id": None}


async def _own_order(session: AsyncSession, order_id: str, user_id: str) -> Order:
    order = (await session.execute(select(Order).where(Order.id == order_id, Order.user_id == user_id))).scalar_one_or_none()
    if order is None:
        raise HTTPException(404, "Order not found")
    return order


@router.post("/api/payments/razorpay/order")
async def create_razorpay_order(
    body: RzpOrderIn, user_id: str = Depends(current_user), _: None = Depends(rate_limit),
    session: AsyncSession = Depends(get_session),
):
    _require_razorpay()
    order = await _own_order(session, body.order_id, user_id)
    if order.status in (OrderStatus.PAID, OrderStatus.REFUNDED):
        raise HTTPException(409, f"Order is already {order.status.value}")

    if order.payment_intent_id.startswith("order_"):  # already has a Razorpay order: reuse it (retry / reopen)
        rzp_id = order.payment_intent_id
    else:
        try:
            created = await rzp.create_order(
                amount_paise=order.amount_cents, receipt=order.id, notes={"nova_order_id": order.id},
            )
        except rzp.RazorpayError as exc:
            print(f"razorpay create_order failed for {order.id}: {exc}", flush=True)
            raise HTTPException(502, "Could not start the payment. Please try again.")
        rzp_id = created["id"]
        # Conditional so two simultaneous clicks cannot both rewrite it; the loser re-reads the winner's id.
        won = await session.execute(
            update(Order).where(Order.id == order.id, Order.payment_intent_id == order.payment_intent_id)
            .values(payment_intent_id=rzp_id)
        )
        await session.commit()
        if won.rowcount != 1:
            rzp_id = (await session.execute(select(Order.payment_intent_id).where(Order.id == order.id))).scalar_one()
    return {
        "key_id": settings.razorpay_key_id, "razorpay_order_id": rzp_id,
        "amount": order.amount_cents, "currency": "INR", "name": "nova", "order_id": order.id,
    }


@router.post("/api/payments/razorpay/verify")
async def verify_razorpay_payment(
    body: RzpVerifyIn, user_id: str = Depends(current_user), _: None = Depends(rate_limit),
    session: AsyncSession = Depends(get_session),
):
    """Called by the browser right after Checkout succeeds. The signature proves Razorpay
    (not the browser) vouches for this payment id on this order."""
    _require_razorpay()
    order = await _own_order(session, body.order_id, user_id)
    order_pk, intent_id = order.id, order.payment_intent_id  # apply_event may roll back, which expires `order`
    if intent_id != body.razorpay_order_id:
        raise HTTPException(400, "That payment does not belong to this order")
    try:
        rzp.verify_checkout_signature(body.razorpay_order_id, body.razorpay_payment_id, body.razorpay_signature)
    except rzp.BadSignature:
        raise HTTPException(400, "Payment signature did not match")
    try:
        result = await svc.apply_event(session, f"chk_{body.razorpay_payment_id}", "payment.succeeded", intent_id)
    except svc.InvalidTransition:
        raise HTTPException(409, "Conflicting state, retry shortly")
    WEBHOOKS.labels(result["outcome"]).inc()
    fresh = (await session.execute(select(Order.status).where(Order.id == order_pk))).scalar_one()
    return {"outcome": result["outcome"], "order_status": fresh.value}


@router.post("/webhooks/razorpay")
async def razorpay_webhook(request: Request, session: AsyncSession = Depends(get_session)):
    raw = await request.body()
    try:
        rzp.verify_webhook_signature(raw, request.headers.get("X-Razorpay-Signature"))
    except rzp.BadSignature:
        raise HTTPException(401, "Bad signature")
    try:
        translated = rzp.translate_webhook(raw, request.headers.get("X-Razorpay-Event-Id"))
    except (json.JSONDecodeError, KeyError, ValueError, AttributeError):
        raise HTTPException(400, "Malformed event")
    if translated is None:
        return {"outcome": "ignored_unknown_type"}  # 200 so Razorpay stops retrying events we don't use
    event_id, event_type, order_id = translated
    try:
        result = await svc.apply_event(session, event_id, event_type, order_id)
    except svc.InvalidTransition:
        raise HTTPException(409, "Conflicting state, retry later")  # Razorpay retries on non-2xx
    WEBHOOKS.labels(result["outcome"]).inc()
    return result