import asyncio
import json
from datetime import timedelta

from sqlalchemy import func, select, update

from app.db import SessionLocal
from app.models import Order, OutboxEvent, Reservation, ReservationStatus, utcnow
from app.services import inventory
from app.services.orders import sign

from .conftest import buy_to_order, reserve, signup, stock


async def pay(api, headers, order_id, outcome="success", duplicate=False):
    r = await api.post(
        f"/api/dev/payments/{order_id}/simulate", params={"outcome": outcome, "duplicate": duplicate},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    return r.json()


async def expire_everything():
    """Backdate every hold past its TTL and run the sweeper once."""
    async with SessionLocal() as s:
        await s.execute(update(Reservation).where(Reservation.status == ReservationStatus.ACTIVE)
                        .values(expires_at=utcnow() - timedelta(seconds=1)))
        await s.commit()
    async with SessionLocal() as s:
        return await inventory.expire_stale(s)


async def outbox_topics():
    async with SessionLocal() as s:
        return [t for (t,) in (await s.execute(select(OutboxEvent.topic).order_by(OutboxEvent.id))).all()]


async def test_happy_path_paid_order_keeps_its_stock(api):
    h = await signup(api, "buyer")
    order = await buy_to_order(api, h)
    assert order["status"] == "pending_payment" and order["amount_cents"] == 18900

    assert (await pay(api, h, order["id"]))["order_status"] == "paid"
    assert (await stock(api))["sold"] == 1

    # The hold is now a sale: the sweeper must never hand that stock back.
    assert await expire_everything() == 0
    s = await stock(api)
    assert s["available"] == 9 and s["sold"] == 1 and s["consistent"]
    assert await outbox_topics() == ["order.paid"]


async def test_creating_an_order_twice_returns_the_same_order(api):
    h = await signup(api, "dbl")
    res = await reserve(api, h)
    body = {"reservation_id": res.json()["id"]}
    first = await api.post("/api/orders", json=body, headers=h)
    second = await api.post("/api/orders", json=body, headers=h)
    assert first.json()["id"] == second.json()["id"]


async def test_cannot_order_someone_elses_reservation(api):
    a, b = await signup(api, "a"), await signup(api, "b")
    res = await reserve(api, a)
    stolen = await api.post("/api/orders", json={"reservation_id": res.json()["id"]}, headers=b)
    assert stolen.status_code == 409


async def test_one_per_customer_even_with_different_keys(api):
    h = await signup(api, "greedy")
    assert (await reserve(api, h)).status_code == 201
    assert (await reserve(api, h)).status_code == 409
    assert (await stock(api))["available"] == 9  # the failed attempt did not leak stock


async def test_duplicate_webhook_event_is_applied_once(api):
    h = await signup(api, "dup")
    order = await buy_to_order(api, h)
    result = await pay(api, h, order["id"], duplicate=True)
    assert result["outcomes"] == ["paid", "duplicate_event"]
    assert (await stock(api))["sold"] == 1
    assert await outbox_topics() == ["order.paid"]  # one event, not two


async def test_same_event_delivered_concurrently_is_applied_once(api):
    h = await signup(api, "race")
    order = await buy_to_order(api, h)
    async with SessionLocal() as s:
        intent = (await s.execute(select(Order.payment_intent_id))).scalar_one()
    raw = json.dumps({"id": "evt_same", "type": "payment.succeeded", "data": {"payment_intent_id": intent}}).encode()
    sig = {"X-Signature": sign(raw)}
    results = await asyncio.gather(*[api.post("/webhooks/payments", content=raw, headers=sig) for _ in range(15)])
    outcomes = [r.json()["outcome"] for r in results if r.status_code == 200]
    assert outcomes.count("paid") == 1
    assert (await stock(api))["sold"] == 1
    assert await outbox_topics() == ["order.paid"]


async def test_bad_signature_is_rejected(api):
    raw = json.dumps({"id": "evt_x", "type": "payment.succeeded", "data": {"payment_intent_id": "pi_x"}}).encode()
    r = await api.post("/webhooks/payments", content=raw, headers={"X-Signature": "0" * 64})
    assert r.status_code == 401
    assert (await api.post("/webhooks/payments", content=raw)).status_code == 401


async def test_payment_failure_keeps_the_hold_so_the_buyer_can_retry(api):
    h = await signup(api, "retry")
    order = await buy_to_order(api, h)
    assert (await pay(api, h, order["id"], "fail"))["order_status"] == "pending_payment"
    assert (await pay(api, h, order["id"], "success"))["order_status"] == "paid"


async def test_expired_hold_expires_the_order_and_returns_stock(api):
    h = await signup(api, "slow")
    order = await buy_to_order(api, h)
    assert await expire_everything() == 1
    fetched = (await api.get(f"/api/orders/{order['id']}", headers=h)).json()
    assert fetched["status"] == "expired"
    assert (await stock(api))["available"] == 10
    assert await outbox_topics() == ["order.expired"]


async def test_late_payment_is_honoured_when_stock_is_still_there(api):
    h = await signup(api, "late")
    order = await buy_to_order(api, h)
    await expire_everything()
    assert (await stock(api))["available"] == 10

    result = await pay(api, h, order["id"])  # money arrives after the hold died
    assert result["outcomes"] == ["paid_late"] and result["order_status"] == "paid"
    s = await stock(api)
    assert s["available"] == 9 and s["sold"] == 1 and s["consistent"]


async def test_late_payment_is_refunded_when_someone_else_took_the_stock(api):
    slow = await signup(api, "slow")
    order = await buy_to_order(api, slow)
    await expire_everything()

    # Ten other shoppers now buy out the whole product.
    for i in range(10):
        h = await signup(api, f"fast{i}")
        o = await buy_to_order(api, h)
        await pay(api, h, o["id"])
    assert (await stock(api))["available"] == 0

    result = await pay(api, slow, order["id"])
    assert result["outcomes"] == ["refunded_late"] and result["order_status"] == "refunded"
    s = await stock(api)
    assert s["available"] == 0 and s["sold"] == 10 and s["consistent"]  # never oversold
    assert (await outbox_topics())[-1] == "order.refunded"


async def test_refund_event_before_payment_is_ignored(api):
    h = await signup(api, "early")
    order = await buy_to_order(api, h)
    assert (await pay(api, h, order["id"], "refund"))["outcomes"] == ["ignored_out_of_order"]
    assert (await pay(api, h, order["id"], "success"))["order_status"] == "paid"


async def test_refund_after_payment_returns_stock_and_frees_the_customer(api):
    h = await signup(api, "refunded")
    order = await buy_to_order(api, h)
    await pay(api, h, order["id"])
    result = await pay(api, h, order["id"], "refund")
    assert result["order_status"] == "refunded"
    s = await stock(api)
    assert s["available"] == 10 and s["sold"] == 0 and s["consistent"]
    assert (await reserve(api, h)).status_code == 201  # may buy again
    assert await outbox_topics() == ["order.paid", "order.refunded"]


async def test_paying_the_same_order_after_refund_does_nothing(api):
    h = await signup(api, "after")
    order = await buy_to_order(api, h)
    await pay(api, h, order["id"])
    await pay(api, h, order["id"], "refund")
    assert (await pay(api, h, order["id"]))["outcomes"] == ["ignored_after_refund"]
    assert (await stock(api))["available"] == 10


async def test_no_oversell_through_the_whole_http_stack(api):
    """60 real users race through signup, reserve, order and pay for 10 units."""
    users = [await signup(api, f"u{i}") for i in range(60)]

    async def attempt(h):
        res = await reserve(api, h)
        if res.status_code != 201:
            return res.status_code
        order = await api.post("/api/orders", json={"reservation_id": res.json()["id"]}, headers=h)
        await pay(api, h, order.json()["id"])
        return 201

    codes = await asyncio.gather(*[attempt(h) for h in users])
    assert codes.count(201) == 10 and codes.count(409) == 50
    s = await stock(api)
    assert s["available"] == 0 and s["sold"] == 10 and s["consistent"]
    async with SessionLocal() as sess:
        paid = (await sess.execute(select(func.count()).select_from(OutboxEvent))).scalar_one()
    assert paid == 10
