"""Inventory reservation core, for carts.

A reservation is a cart: several (product, qty) lines held together, all or nothing.

The one rule: stock is only ever decremented by a *conditional* UPDATE
(`... SET available = available - :q WHERE available >= :q`). The database checks
and writes in one atomic step, so two buyers can never both take the last unit.
The per-customer cap works the same way (`... WHERE qty + :q <= :limit`).

Lines are always processed in ascending product-id order. Every writer takes its
row locks in the same order, so two carts that share products cannot deadlock
each other on Postgres.
"""
from datetime import datetime, timedelta, timezone
from typing import Iterable

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..models import (
    CustomerQuota, Inventory, Order, OrderStatus, Product, Reservation, ReservationItem,
    ReservationStatus, new_id,
)
from .outbox import emit_order

Line = tuple[int, int]  # (product_id, qty)


class SoldOut(Exception):
    def __init__(self, product_id: int):
        super().__init__(f"sold out: {product_id}")
        self.product_id = product_id


class LimitExceeded(Exception):
    """The customer would hold or own more than the per-customer cap of a product."""

    def __init__(self, product_id: int, limit: int):
        super().__init__(f"limit {limit} exceeded: {product_id}")
        self.product_id = product_id
        self.limit = limit


class ProductNotFound(Exception):
    def __init__(self, product_id: int):
        super().__init__(f"unknown product: {product_id}")
        self.product_id = product_id


def _now() -> datetime:
    return datetime.now(timezone.utc)


def normalize(lines: Iterable[Line]) -> list[Line]:
    """Merge repeated products and sort by product id (the lock order)."""
    merged: dict[int, int] = {}
    for product_id, qty in lines:
        merged[product_id] = merged.get(product_id, 0) + qty
    return sorted(merged.items())


async def _by_idempotency_key(session: AsyncSession, user_id: str, key: str):
    return (
        await session.execute(
            select(Reservation).where(
                Reservation.user_id == user_id, Reservation.idempotency_key == key
            )
        )
    ).scalar_one_or_none()


async def reservation_lines(session: AsyncSession, reservation_id: str) -> list[Line]:
    rows = (
        await session.execute(
            select(ReservationItem.product_id, ReservationItem.qty)
            .where(ReservationItem.reservation_id == reservation_id)
            .order_by(ReservationItem.product_id)
        )
    ).all()
    return [(pid, qty) for pid, qty in rows]


# ---------------------------------------------------------------- taking and returning units

async def _ensure_quota_rows(session: AsyncSession, user_id: str, lines: list[Line]) -> None:
    """A conditional UPDATE needs a row to act on, so create missing zero rows first."""
    insert = pg_insert if session.get_bind().dialect.name == "postgresql" else sqlite_insert
    for product_id, _ in lines:
        await session.execute(
            insert(CustomerQuota)
            .values(user_id=user_id, product_id=product_id, qty=0)
            .on_conflict_do_nothing()
        )


async def take_lines(session: AsyncSession, user_id: str, lines: list[Line], limit: int) -> None:
    """Take quota and stock for every line, all or nothing.

    On failure everything taken so far is undone here (no rollback is needed, so
    callers in the middle of a bigger transaction can use this safely), then
    SoldOut or LimitExceeded is raised. `lines` must come from `normalize`.
    """
    await _ensure_quota_rows(session, user_id, lines)
    taken: list[tuple[str, int, int]] = []
    try:
        for product_id, qty in lines:
            got = await session.execute(
                update(CustomerQuota)
                .where(
                    CustomerQuota.user_id == user_id,
                    CustomerQuota.product_id == product_id,
                    CustomerQuota.qty + qty <= limit,
                )
                .values(qty=CustomerQuota.qty + qty)
            )
            if got.rowcount == 0:
                raise LimitExceeded(product_id, limit)
            taken.append(("quota", product_id, qty))

            got = await session.execute(
                update(Inventory)
                .where(Inventory.product_id == product_id, Inventory.available >= qty)
                .values(available=Inventory.available - qty)
            )
            if got.rowcount == 0:
                raise SoldOut(product_id)
            taken.append(("stock", product_id, qty))
    except (SoldOut, LimitExceeded):
        for kind, product_id, qty in reversed(taken):
            if kind == "stock":
                await session.execute(
                    update(Inventory).where(Inventory.product_id == product_id)
                    .values(available=Inventory.available + qty)
                )
            else:
                await session.execute(
                    update(CustomerQuota)
                    .where(CustomerQuota.user_id == user_id, CustomerQuota.product_id == product_id)
                    .values(qty=CustomerQuota.qty - qty)
                )
        raise


async def give_back_lines(session: AsyncSession, user_id: str, lines: list[Line]) -> None:
    """Return stock and quota for every line. Same quota-then-stock order as take_lines."""
    for product_id, qty in lines:
        await session.execute(
            update(CustomerQuota)
            .where(CustomerQuota.user_id == user_id, CustomerQuota.product_id == product_id)
            .values(qty=CustomerQuota.qty - qty)
        )
        await session.execute(
            update(Inventory).where(Inventory.product_id == product_id)
            .values(available=Inventory.available + qty)
        )


# ---------------------------------------------------------------- reserve / release / expire

async def reserve(
    session: AsyncSession,
    *,
    user_id: str,
    items: Iterable[Line],
    idempotency_key: str,
    ttl_seconds: int,
    max_per_product: int | None = None,
) -> tuple[Reservation, bool]:
    """Hold a cart for `user_id`. Returns (reservation, created).

    `created` is False when the idempotency key was already used, in which case
    the original reservation is returned and stock is untouched.
    """
    limit = settings.max_qty_per_product if max_per_product is None else max_per_product

    existing = await _by_idempotency_key(session, user_id, idempotency_key)
    if existing:
        return existing, False

    lines = normalize(items)
    found = set(
        (await session.execute(select(Product.id).where(Product.id.in_([p for p, _ in lines]))))
        .scalars()
    )
    for product_id, _ in lines:
        if product_id not in found:
            raise ProductNotFound(product_id)

    try:
        await take_lines(session, user_id, lines, limit)
    except (SoldOut, LimitExceeded):
        await session.rollback()
        # A simultaneous duplicate of this very request may have just won. Then this
        # is a replay, not a failure.
        existing = await _by_idempotency_key(session, user_id, idempotency_key)
        if existing:
            return existing, False
        raise

    reservation = Reservation(
        id=new_id(), user_id=user_id, idempotency_key=idempotency_key,
        expires_at=_now() + timedelta(seconds=ttl_seconds),
    )
    try:
        session.add(reservation)
        await session.flush()
        session.add_all(
            [ReservationItem(reservation_id=reservation.id, product_id=p, qty=q) for p, q in lines]
        )
        # Stock, quota, header and lines commit together, or not at all.
        await session.commit()
    except IntegrityError:
        # Two identical requests raced; the unique key let only one win. Rolling back
        # also undoes the stock and quota this attempt took.
        await session.rollback()
        existing = await _by_idempotency_key(session, user_id, idempotency_key)
        if existing:
            return existing, False
        raise
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
    """Move ACTIVE -> new_status and give every line back, exactly once.

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
    owner = row.user_id
    lines = await reservation_lines(session, reservation_id)

    flipped = await session.execute(
        update(Reservation).where(*cond).values(status=new_status)
    )
    if flipped.rowcount != 1:
        await session.rollback()
        return False

    await give_back_lines(session, owner, lines)

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
            await emit_order(session, "order.expired", pending)

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