"""Transactional outbox.

State changes and the events describing them are written in the SAME database
transaction (`emit` never commits). A separate relay later delivers each event
to its consumers. That removes the classic dual-write bug where the order row
commits but the "order paid" message is lost (or the reverse).

Delivery is at-least-once; consumers are made idempotent with `processed_events`,
so the net effect is exactly once.
"""
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Awaitable, Callable

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..models import (
    Notification, Order, OrderItem, OutboxEvent, ProcessedEvent, SalesStat,
)

Event = Any  # anything with .id, .topic, .payload
Consumer = Callable[[AsyncSession, Event], Awaitable[None]]


async def emit(session: AsyncSession, topic: str, payload: dict) -> None:
    session.add(OutboxEvent(topic=topic, payload=payload))


async def emit_order(session: AsyncSession, topic: str, order: Order, **extra) -> None:
    """Emit an order event whose payload lists every line, so consumers need no lookups."""
    rows = (
        await session.execute(
            select(OrderItem.product_id, OrderItem.qty, OrderItem.unit_price_cents)
            .where(OrderItem.order_id == order.id)
            .order_by(OrderItem.product_id)
        )
    ).all()
    await emit(session, topic, {
        "order_id": order.id,
        "user_id": order.user_id,
        "amount_cents": order.amount_cents,
        "items": [{"product_id": p, "qty": q, "unit_price_cents": c} for p, q, c in rows],
        **extra,
    })


# ---------------------------------------------------------------- consumers

_EMAIL = {
    "order.paid": "Your order is confirmed. Thanks for copping!",
    "order.expired": "Your reservation expired and the items went back on sale.",
    "order.refunded": "Your order was refunded.",
}


async def email_consumer(session: AsyncSession, ev: Event) -> None:
    session.add(Notification(user_id=ev.payload["user_id"], kind=ev.topic, body=_EMAIL[ev.topic]))


async def analytics_consumer(session: AsyncSession, ev: Event) -> None:
    if ev.topic == "order.refunded" and not ev.payload.get("was_paid"):
        return  # refunded before it was ever counted as a sale (late payment, items gone)
    sign = {"order.paid": 1, "order.refunded": -1}[ev.topic]
    for it in ev.payload["items"]:
        units = sign * it["qty"]
        revenue = units * it["unit_price_cents"]
        bumped = await session.execute(
            update(SalesStat).where(SalesStat.product_id == it["product_id"]).values(
                orders=SalesStat.orders + sign,
                units=SalesStat.units + units,
                revenue_cents=SalesStat.revenue_cents + revenue,
            )
        )
        if bumped.rowcount == 0:
            session.add(SalesStat(
                product_id=it["product_id"], orders=sign, units=units, revenue_cents=revenue,
            ))


# topic -> [(consumer name, handler)]
CONSUMERS: dict[str, list[tuple[str, Consumer]]] = {
    "order.paid": [("email", email_consumer), ("analytics", analytics_consumer)],
    "order.expired": [("email", email_consumer)],
    "order.refunded": [("email", email_consumer), ("analytics", analytics_consumer)],
}


# ---------------------------------------------------------------- relay

async def _deliver(session: AsyncSession, name: str, handler: Consumer, ev: Event) -> None:
    """Run one consumer for one event, at most once (processed_events is the guard)."""
    session.add(ProcessedEvent(consumer=name, event_id=ev.id))
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()   # already handled on an earlier attempt
        return
    await handler(session, ev)
    await session.commit()         # marker and effect commit together


async def relay_once(factory: async_sessionmaker, limit: int = 100) -> int:
    """Deliver pending events in order. Returns how many were fully published."""
    published = 0
    async with factory() as session:
        rows = (
            await session.execute(
                select(OutboxEvent)
                .where(OutboxEvent.published_at.is_(None))
                .order_by(OutboxEvent.id)
                .limit(limit)
            )
        ).scalars().all()
        # Plain copies: a rollback expires ORM objects, and async sessions cannot lazy-reload them.
        events = [SimpleNamespace(id=r.id, topic=r.topic, payload=r.payload) for r in rows]
        for ev in events:
            try:
                for name, handler in CONSUMERS.get(ev.topic, []):
                    await _deliver(session, name, handler, ev)
                await session.execute(
                    update(OutboxEvent)
                    .where(OutboxEvent.id == ev.id, OutboxEvent.published_at.is_(None))
                    .values(published_at=datetime.now(timezone.utc))
                )
                await session.commit()
                published += 1
            except Exception as exc:  # leave unpublished; it is retried next tick
                await session.rollback()
                print(f"outbox: event {ev.id} ({ev.topic}) failed: {exc!r}")
    return published
