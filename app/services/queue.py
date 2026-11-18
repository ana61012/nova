"""Virtual waiting room.

Buyers join a FIFO line (the autoincrement id is the order). A background
admitter lets a few through at a time, never exceeding `max_active` people
shopping at once. Only admitted buyers may reserve stock, so the stampede is
absorbed by cheap queue rows instead of hammering the inventory.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Drop, QueueEntry, QueueStatus


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)  # SQLite drops tzinfo


async def current_drop_id(session: AsyncSession) -> int | None:
    return (await session.execute(select(Drop.id).order_by(Drop.id.desc()).limit(1))).scalar_one_or_none()


async def _entry(session: AsyncSession, drop_id: int, user_id: str) -> QueueEntry | None:
    return (
        await session.execute(
            select(QueueEntry).where(QueueEntry.drop_id == drop_id, QueueEntry.user_id == user_id)
        )
    ).scalar_one_or_none()


async def join(session: AsyncSession, drop_id: int, user_id: str) -> None:
    """Join the line. Idempotent: joining twice keeps your original place."""
    existing = await _entry(session, drop_id, user_id)
    lapsed = existing is not None and (
        # Missed the window, or finished/left: the old entry is over, so you may rejoin.
        existing.status in (QueueStatus.EXPIRED, QueueStatus.DONE)
        # Window ran out but the admitter has not swept the row yet: same thing.
        or (existing.status == QueueStatus.ADMITTED and _aware(existing.admit_expires_at) <= _now())
    )
    if existing is not None and not lapsed:
        return  # still waiting or actively shopping: keep your place
    if existing is not None:
        # Rejoining always goes to the back, never back to your old spot.
        await session.execute(delete(QueueEntry).where(QueueEntry.id == existing.id))
    session.add(QueueEntry(drop_id=drop_id, user_id=user_id))
    try:
        await session.commit()
    except IntegrityError:  # simultaneous double join
        await session.rollback()


async def leave(session: AsyncSession, drop_id: int, user_id: str) -> None:
    """Give up your place (or your shopping slot) so others can move up."""
    await session.execute(
        update(QueueEntry)
        .where(
            QueueEntry.drop_id == drop_id, QueueEntry.user_id == user_id,
            QueueEntry.status.in_([QueueStatus.WAITING, QueueStatus.ADMITTED]),
        )
        .values(status=QueueStatus.DONE)
    )
    await session.commit()


finish = leave  # buying something frees your slot the same way


async def admit_batch(
    session: AsyncSession, drop_id: int, *, batch: int, max_active: int, window_seconds: int
) -> int:
    """Admit up to `batch` people from the front of the line. Returns how many."""
    now = _now()
    # Anyone admitted but idle past their window loses the slot.
    await session.execute(
        update(QueueEntry)
        .where(
            QueueEntry.drop_id == drop_id, QueueEntry.status == QueueStatus.ADMITTED,
            QueueEntry.admit_expires_at < now,
        )
        .values(status=QueueStatus.EXPIRED)
    )
    active = (
        await session.execute(
            select(func.count()).select_from(QueueEntry).where(
                QueueEntry.drop_id == drop_id, QueueEntry.status == QueueStatus.ADMITTED
            )
        )
    ).scalar_one()
    slots = min(batch, max_active - active)
    admitted = 0
    if slots > 0:
        ids = (
            await session.execute(
                select(QueueEntry.id)
                .where(QueueEntry.drop_id == drop_id, QueueEntry.status == QueueStatus.WAITING)
                .order_by(QueueEntry.id)
                .limit(slots)
            )
        ).scalars().all()
        for entry_id in ids:
            # Conditional update: safe even if two admitters ever overlap.
            res = await session.execute(
                update(QueueEntry)
                .where(QueueEntry.id == entry_id, QueueEntry.status == QueueStatus.WAITING)
                .values(
                    status=QueueStatus.ADMITTED, admitted_at=now,
                    admit_expires_at=now + timedelta(seconds=window_seconds),
                )
            )
            admitted += res.rowcount
    await session.commit()
    return admitted


async def counts(session: AsyncSession, drop_id: int) -> dict[str, int]:
    """Waiting and admitted headcounts in one cheap grouped query."""
    rows = (
        await session.execute(
            select(QueueEntry.status, func.count())
            .where(QueueEntry.drop_id == drop_id)
            .group_by(QueueEntry.status)
        )
    ).all()
    by_status = {st.value: n for st, n in rows}
    return {"waiting": by_status.get("waiting", 0), "admitted": by_status.get("admitted", 0)}


async def is_admitted(session: AsyncSession, drop_id: int, user_id: str) -> bool:
    count = (
        await session.execute(
            select(func.count()).select_from(QueueEntry).where(
                QueueEntry.drop_id == drop_id, QueueEntry.user_id == user_id,
                QueueEntry.status == QueueStatus.ADMITTED,
                QueueEntry.admit_expires_at > _now(),
            )
        )
    ).scalar_one()
    return count > 0


async def status_payload(session: AsyncSession, drop_id: int, user_id: str) -> dict:
    waiting_total = (
        await session.execute(
            select(func.count()).select_from(QueueEntry).where(
                QueueEntry.drop_id == drop_id, QueueEntry.status == QueueStatus.WAITING
            )
        )
    ).scalar_one()
    entry = await _entry(session, drop_id, user_id)
    if entry is None:
        return {"state": "none", "position": None, "waiting_total": waiting_total, "expires_in": None}

    position = expires_in = None
    state = entry.status.value
    if entry.status == QueueStatus.WAITING:
        ahead = (
            await session.execute(
                select(func.count()).select_from(QueueEntry).where(
                    QueueEntry.drop_id == drop_id, QueueEntry.status == QueueStatus.WAITING,
                    QueueEntry.id < entry.id,
                )
            )
        ).scalar_one()
        position = ahead + 1
    elif entry.status == QueueStatus.ADMITTED:
        left = int((_aware(entry.admit_expires_at) - _now()).total_seconds())
        if left <= 0:
            state = "expired"  # the admitter has not swept it yet, but it is over
        else:
            expires_in = left
    return {"state": state, "position": position, "waiting_total": waiting_total, "expires_in": expires_in}
