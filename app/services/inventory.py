"""Inventory reservation core.

The one rule: stock is only ever decremented by a *conditional* UPDATE
(`... SET available = available - :q WHERE available >= :q`). The database
evaluates the check and the write as one atomic step, so two buyers can never
both take the last unit, with no application-level locking needed. This works
on Postgres and SQLite alike.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Inventory, Order, OrderStatus, Product, Reservation, ReservationStatus
from .outbox import emit, order_payload


class SoldOut(Exception):
    pass


class AlreadyPurchased(Exception):
    """This customer already holds or bought this product (one per customer)."""


class ProductNotFound(Exception):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _by_idempotency_key(session: AsyncSession, user_id: str, key: str):
    return (
        await session.execute(
            select(Reservation).where(
                Reservation.user_id == user_id, Reservation.idempotency_key == key
            )
        )
    ).scalar_one_or_none()


async def reserve(
    session: AsyncSession,
    *,
    product_id: int,
    user_id: str,
    qty: int,
    idempotency_key: str,
    ttl_seconds: int,
) -> tuple[Reservation, bool]:
    """Hold `qty` units for `user_id`. Returns (reservation, created).

    `created` is False when the idempotency key was already used, in which
    case the original reservation is returned and stock is untouched.
    """
    existing = await _by_idempotency_key(session, user_id, idempotency_key)
    if existing:
        return existing, False

    if await session.get(Product, product_id) is None:
        raise ProductNotFound(product_id)

    # Atomic check-and-decrement. rowcount == 0 means not enough stock.
    result = await session.execute(
        update(Inventory)
        .where(Inventory.product_id == product_id, Inventory.available >= qty)
        .values(available=Inventory.available - qty)
    )
    if result.rowcount == 0:
        await session.rollback()
        raise SoldOut(product_id)

    reservation = Reservation(
        product_id=product_id,
        user_id=user_id,
        qty=qty,
        idempotency_key=idempotency_key,
        expires_at=_now() + timedelta(seconds=ttl_seconds),
    )
    session.add(reservation)
    try:
        # Decrement and insert commit together, so a failed insert undoes the stock hold.
        await session.commit()
    except IntegrityError:
        # Either two identical requests raced (idempotency key), or this customer
        # already has a live reservation for the product. The rollback also undoes
        # the stock decrement above.
        await session.rollback()
        existing = await _by_idempotency_key(session, user_id, idempotency_key)
        if existing:
            return existing, False
        raise AlreadyPurchased(product_id)
    return reservation, True


async def release(session: AsyncSession, reservation_id: str, user_id: str) -> bool:
    """User cancels an active hold. Returns True if stock was returned."""
    return await _close(session, reservation_id, ReservationStatus.RELEASED, user_id=user_id)


async def _close(
    session: AsyncSession,
    reservation_id: str,
    new_status: ReservationStatus,
    user_id: str | None = None,
) -> bool:
    """Move ACTIVE -> new_status and give the stock back, exactly once.

    The status flip is itself a conditional UPDATE: only the caller that sees
    rowcount == 1 owns the transition, so concurrent sweeper/cancel calls can
    never return the same stock twice.
    """
    cond = [Reservation.id == reservation_id, Reservation.status == ReservationStatus.ACTIVE]
    if user_id is not None:
        cond.append(Reservation.user_id == user_id)

    row = (await session.execute(select(Reservation).where(*cond))).scalar_one_or_none()
    if row is None:
        await session.rollback()
        return False
    qty, product_id = row.qty, row.product_id

    flipped = await session.execute(
        update(Reservation).where(*cond).values(status=new_status)
    )
    if flipped.rowcount != 1:
        await session.rollback()
        return False

    await session.execute(
        update(Inventory)
        .where(Inventory.product_id == product_id)
        .values(available=Inventory.available + qty)
    )

    # An unpaid order for this hold dies with it. Same transaction, so the order
    # state, the stock and the outbox event can never disagree.
    pending = (
        await session.execute(
            select(Order).where(
                Order.reservation_id == reservation_id,
                Order.status == OrderStatus.PENDING_PAYMENT,
            )
        )
    ).scalar_one_or_none()
    if pending is not None:
        closed = await session.execute(
            update(Order)
            .where(Order.id == pending.id, Order.status == OrderStatus.PENDING_PAYMENT)
            .values(status=OrderStatus.EXPIRED, updated_at=_now())
        )
        if closed.rowcount == 1:
            await emit(session, "order.expired", order_payload(pending))

    await session.commit()
    return True


async def expire_stale(session: AsyncSession, limit: int = 500) -> int:
    """Return stock for reservations whose TTL has passed. Safe to run concurrently."""
    ids = (
        await session.execute(
            select(Reservation.id)
            .where(
                Reservation.status == ReservationStatus.ACTIVE,
                Reservation.expires_at < _now(),
            )
            .limit(limit)
        )
    ).scalars().all()
    count = 0
    for rid in ids:
        if await _close(session, rid, ReservationStatus.EXPIRED):
            count += 1
    return count
