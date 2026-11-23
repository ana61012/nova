from sqlalchemy import func, select

from app.db import SessionLocal
from app.models import Notification, OutboxEvent, ProcessedEvent, SalesStat
from app.services import outbox

from .conftest import buy_to_order, get_stats, signup
from .test_orders import expire_everything, pay


async def relay():
    return await outbox.relay_once(SessionLocal)


async def count(model):
    async with SessionLocal() as s:
        return (await s.execute(select(func.count()).select_from(model))).scalar_one()


async def test_paid_order_reaches_email_and_analytics(api):
    h = await signup(api, "fan")
    order = await buy_to_order(api, h)
    await pay(api, h, order["id"])
    assert await count(Notification) == 0  # nothing happens until the relay runs

    assert await relay() == 1
    async with SessionLocal() as s:
        note = (await s.execute(select(Notification))).scalar_one()
        stat = (await s.execute(select(SalesStat))).scalar_one()
    assert note.kind == "order.paid"
    assert (stat.orders, stat.units, stat.revenue_cents) == (1, 1, 18900)
    stats = await get_stats(api)
    assert stats["outbox_pending"] == 0 and stats["analytics"]["revenue_cents"] == 18900


async def test_running_the_relay_again_delivers_nothing_twice(api):
    h = await signup(api, "once")
    await pay(api, h, (await buy_to_order(api, h))["id"])
    await relay()
    assert await relay() == 0
    assert await count(Notification) == 1


async def test_refund_reverses_the_analytics(api):
    h = await signup(api, "back")
    order = await buy_to_order(api, h)
    await pay(api, h, order["id"])
    await pay(api, h, order["id"], "refund")
    await relay()
    async with SessionLocal() as s:
        stat = (await s.execute(select(SalesStat))).scalar_one()
    assert (stat.orders, stat.revenue_cents) == (0, 0)
    assert await count(Notification) == 2  # paid + refunded emails


async def test_expired_order_sends_only_an_email(api):
    h = await signup(api, "gone")
    await buy_to_order(api, h)
    await expire_everything()
    await relay()
    assert await count(Notification) == 1 and await count(SalesStat) == 0


async def test_failing_consumer_is_retried_without_repeating_the_others(api, monkeypatch):
    h = await signup(api, "flaky")
    await pay(api, h, (await buy_to_order(api, h))["id"])

    calls = {"n": 0}
    real = outbox.analytics_consumer

    async def flaky_analytics(session, ev):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("analytics database is down")
        await real(session, ev)

    monkeypatch.setitem(
        outbox.CONSUMERS, "order.paid", [("email", outbox.email_consumer), ("analytics", flaky_analytics)]
    )

    assert await relay() == 0               # analytics failed, so the event stays pending
    assert await count(Notification) == 1   # email already went out
    async with SessionLocal() as s:
        assert (await s.execute(select(OutboxEvent.published_at))).scalar_one() is None

    assert await relay() == 1               # retry succeeds
    assert await count(Notification) == 1   # email was NOT sent a second time
    assert await count(SalesStat) == 1
    assert await count(ProcessedEvent) == 2
