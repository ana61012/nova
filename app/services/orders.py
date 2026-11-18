"""Orders and payment webhooks.

Payment gateways are messy: events can be duplicated, late, or out of order.
The rules here:

* Every event id is stored once (primary key), so a replayed event is a no-op.
* Order status only moves along ALLOWED transitions, via conditional UPDATEs.
* A payment that lands AFTER the hold expired tries to re-take stock. If the
  item is still available the order is paid; if someone else bought it, the
  order is refunded instead of overselling.
* A refund event that arrives before its payment event is parked as ignored
  (a real system would retry it later).
"""
import hashlib
import hmac
import json
from datetime import datetime, timezone
import uuid

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..models import (
    Inventory, Order, OrderStatus, Product, Reservation, ReservationStatus, WebhookEvent,
)
from . import queue
from .outbox import emit, order_payload

S = OrderStatus
ALLOWED: dict[OrderStatus, set[OrderStatus]] = {
    S.PENDING_PAYMENT: {S.PAID, S.EXPIRED},
    S.EXPIRED: {S.PAID, S.REFUNDED},   # late payment: paid again, or refunded if sold out
    S.PAID: {S.REFUNDED},
    S.REFUNDED: set(),
}


class ReservationInvalid(Exception):
    pass


class InvalidSignature(Exception):
    pass


class InvalidTransition(Exception):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def sign(body: bytes, secret: str | None = None) -> str:
    return hmac.new((secret or settings.webhook_secret).encode(), body, hashlib.sha256).hexdigest()


# ---------------------------------------------------------------- creating orders

async def create_order(session: AsyncSession, *, user_id: str, reservation_id: str) -> Order:
    """Turn a live reservation into an order awaiting payment. Idempotent per reservation."""
    existing = (
        await session.execute(
            select(Order).where(Order.reservation_id == reservation_id, Order.user_id == user_id)
        )
    ).scalar_one_or_none()
    if existing:
        return existing

    res = (
        await session.execute(
            select(Reservation).where(
                Reservation.id == reservation_id, Reservation.user_id == user_id,
                Reservation.status == ReservationStatus.ACTIVE, Reservation.expires_at > _now(),
            )
        )
    ).scalar_one_or_none()
    if res is None:
        raise ReservationInvalid(reservation_id)

    product = await session.get(Product, res.product_id)
    order = Order(
        user_id=user_id, reservation_id=res.id, product_id=res.product_id, qty=res.qty,
        amount_cents=product.price_cents * res.qty, payment_intent_id="pi_" + uuid.uuid4().hex,
    )
    session.add(order)
    try:
        await session.commit()
    except IntegrityError:  # double submit raced us; unique(reservation_id) picked the winner
        await session.rollback()
        return (
            await session.execute(select(Order).where(Order.reservation_id == reservation_id))
        ).scalar_one()
    return order


# ---------------------------------------------------------------- state machine

async def _transition(session: AsyncSession, order: Order, to: OrderStatus, topic: str, **extra) -> None:
    """Conditional status change plus its outbox event, in the caller's transaction."""
    sources = [s for s, targets in ALLOWED.items() if to in targets]
    res = await session.execute(
        update(Order)
        .where(Order.id == order.id, Order.status.in_(sources))
        .values(status=to, updated_at=_now())
    )
    if res.rowcount != 1:
        raise InvalidTransition(f"{order.id}: -> {to.value}")
    await emit(session, topic, order_payload(order, **extra))


async def _order_by_intent(session: AsyncSession, intent_id: str) -> Order | None:
    return (
        await session.execute(select(Order).where(Order.payment_intent_id == intent_id))
    ).scalar_one_or_none()


async def _on_succeeded(session: AsyncSession, intent_id: str) -> str:
    order = await _order_by_intent(session, intent_id)
    if order is None:
        return "unknown_order"
    if order.status == S.PAID:
        return "already_paid"
    if order.status == S.REFUNDED:
        return "ignored_after_refund"

    product = await session.get(Product, order.product_id)

    if order.status == S.PENDING_PAYMENT:
        # Normal path: the hold is still ours, so turn it into a sale.
        flipped = await session.execute(
            update(Reservation)
            .where(Reservation.id == order.reservation_id, Reservation.status == ReservationStatus.ACTIVE)
            .values(status=ReservationStatus.CONVERTED)
        )
        if flipped.rowcount == 1:
            await _transition(session, order, S.PAID, "order.paid")
            await queue.finish(session, product.drop_id, order.user_id)  # frees the queue slot
            return "paid"
        # The sweeper expired the hold a moment ago; fall through to the late path.

    # Late payment: the stock went back on the shelf. Try to take it again.
    taken = await session.execute(
        update(Inventory)
        .where(Inventory.product_id == order.product_id, Inventory.available >= order.qty)
        .values(available=Inventory.available - order.qty)
    )
    if taken.rowcount == 1:
        await session.execute(
            update(Reservation)
            .where(
                Reservation.id == order.reservation_id,
                Reservation.status.in_([ReservationStatus.EXPIRED, ReservationStatus.RELEASED]),
            )
            .values(status=ReservationStatus.CONVERTED)
        )
        await _transition(session, order, S.PAID, "order.paid", late=True)
        await queue.finish(session, product.drop_id, order.user_id)
        return "paid_late"

    await _transition(session, order, S.REFUNDED, "order.refunded", reason="sold_out_after_expiry")
    return "refunded_late"


async def _on_refunded(session: AsyncSession, intent_id: str) -> str:
    order = await _order_by_intent(session, intent_id)
    if order is None:
        return "unknown_order"
    if order.status != S.PAID:
        return "ignored_out_of_order"
    await _transition(session, order, S.REFUNDED, "order.refunded", reason="gateway_refund")
    await session.execute(
        update(Reservation)
        .where(Reservation.id == order.reservation_id, Reservation.status == ReservationStatus.CONVERTED)
        .values(status=ReservationStatus.RELEASED)
    )
    await session.execute(
        update(Inventory)
        .where(Inventory.product_id == order.product_id)
        .values(available=Inventory.available + order.qty)
    )
    return "refunded"


async def process_webhook(session: AsyncSession, raw: bytes, signature: str | None) -> dict:
    """Verify, dedupe, apply. Returns {"outcome": ...}. Commits on success."""
    if not hmac.compare_digest(sign(raw), signature or ""):
        raise InvalidSignature()
    event = json.loads(raw)
    intent_id = event["data"]["payment_intent_id"]

    session.add(WebhookEvent(event_id=event["id"], type=event["type"]))
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        return {"outcome": "duplicate_event"}

    try:
        if event["type"] == "payment.succeeded":
            outcome = await _on_succeeded(session, intent_id)
        elif event["type"] == "payment.refunded":
            outcome = await _on_refunded(session, intent_id)
        elif event["type"] == "payment.failed":
            outcome = "payment_failed_retry_allowed"  # the hold stays; the buyer can retry
        else:
            outcome = "ignored_unknown_type"
        await session.execute(
            update(WebhookEvent).where(WebhookEvent.event_id == event["id"]).values(outcome=outcome)
        )
        await session.commit()
    except Exception:
        await session.rollback()  # nothing applied, event id not recorded: the gateway will retry
        raise
    return {"outcome": outcome}
