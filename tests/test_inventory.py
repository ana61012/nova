import asyncio
from datetime import timedelta

from sqlalchemy import func, select, update

from app.models import Inventory, Reservation, ReservationStatus, utcnow
from app.services import inventory


async def _try_reserve(sessions, product_id, user, key=None, qty=1, ttl=120):
    async with sessions() as s:
        try:
            r, created = await inventory.reserve(
                s, product_id=product_id, user_id=user, qty=qty,
                idempotency_key=key or f"key-{user}-0001", ttl_seconds=ttl,
            )
            return ("ok", r, created)
        except inventory.SoldOut:
            return ("sold_out", None, False)


async def _available(sessions, product_id):
    async with sessions() as s:
        return (await s.get(Inventory, product_id)).available


async def test_no_oversell_under_concurrency(sessions, product_id):
    """200 buyers race for 10 units: exactly 10 win, stock ends at exactly 0."""
    results = await asyncio.gather(
        *[_try_reserve(sessions, product_id, f"user{i}") for i in range(200)]
    )
    wins = [r for r in results if r[0] == "ok"]
    assert len(wins) == 10
    assert len([r for r in results if r[0] == "sold_out"]) == 190
    assert await _available(sessions, product_id) == 0

    async with sessions() as s:
        held = (await s.execute(select(func.sum(Reservation.qty)))).scalar()
    assert held == 10  # reservations and stock always agree


async def test_idempotent_retry_does_not_double_reserve(sessions, product_id):
    first = await _try_reserve(sessions, product_id, "alice", key="alice-click-1")
    second = await _try_reserve(sessions, product_id, "alice", key="alice-click-1")
    assert first[2] is True and second[2] is False
    assert first[1].id == second[1].id
    assert await _available(sessions, product_id) == 9


async def test_simultaneous_double_click_creates_one_reservation(sessions, product_id):
    results = await asyncio.gather(
        *[_try_reserve(sessions, product_id, "bob", key="bob-click-1") for _ in range(20)]
    )
    assert {r[1].id for r in results} == {results[0][1].id}
    assert sum(1 for r in results if r[2]) == 1  # exactly one created
    assert await _available(sessions, product_id) == 9


async def test_expired_holds_return_stock_exactly_once(sessions, product_id):
    for i in range(3):
        await _try_reserve(sessions, product_id, f"u{i}", ttl=60)
    assert await _available(sessions, product_id) == 7

    # Backdate the holds so they are past their TTL.
    async with sessions() as s:
        await s.execute(update(Reservation).values(expires_at=utcnow() - timedelta(seconds=1)))
        await s.commit()

    # Several sweepers run at once; stock must still only come back once.
    async def sweep():
        async with sessions() as s:
            return await inventory.expire_stale(s)

    counts = await asyncio.gather(sweep(), sweep(), sweep())
    assert sum(counts) == 3
    assert await _available(sessions, product_id) == 10


async def test_cancel_returns_stock_once(sessions, product_id):
    _, res, _ = await _try_reserve(sessions, product_id, "carol")
    async with sessions() as s:
        assert await inventory.release(s, res.id, "carol") is True
    async with sessions() as s:
        assert await inventory.release(s, res.id, "carol") is False  # second cancel is a no-op
    assert await _available(sessions, product_id) == 10


async def test_cannot_cancel_someone_elses_reservation(sessions, product_id):
    _, res, _ = await _try_reserve(sessions, product_id, "dave")
    async with sessions() as s:
        assert await inventory.release(s, res.id, "mallory") is False
    assert await _available(sessions, product_id) == 9
