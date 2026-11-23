"""Multi-item carts: several products and quantities reserved, paid and returned together.

Seed data: product 1 = 10 units @ 18900, 2 = 15 @ 24900, 3 = 25 @ 4500, 4 = 12 @ 12900.
The per-customer cap is 3 of any one product.
"""
import asyncio

from sqlalchemy import func, select, update

from app.config import settings
from app.db import SessionLocal
from app.models import CustomerQuota, Inventory, SalesStat
from app.services import outbox

from .conftest import buy_to_order, get_stats, reserve, signup, stock
from .test_orders import expire_everything, pay

CART = [{"product_id": 1, "qty": 2}, {"product_id": 3, "qty": 1}]  # 2*18900 + 4500 = 42300


async def set_available(product_id, n):
    async with SessionLocal() as s:
        await s.execute(update(Inventory).where(Inventory.product_id == product_id).values(available=n))
        await s.commit()


async def quota_total(product_id):
    async with SessionLocal() as s:
        q = select(func.coalesce(func.sum(CustomerQuota.qty), 0)).where(CustomerQuota.product_id == product_id)
        return (await s.execute(q)).scalar_one()


async def relay():
    return await outbox.relay_once(SessionLocal)


async def test_cart_with_several_products_and_quantities(api):
    h = await signup(api, "shopper")
    res = await reserve(api, h, items=CART)
    assert res.status_code == 201
    assert res.json()["items"] == [{"product_id": 1, "qty": 2}, {"product_id": 3, "qty": 1}]
    assert (await stock(api, 1))["held"] == 2 and (await stock(api, 1))["available"] == 8
    assert (await stock(api, 3))["held"] == 1 and (await stock(api, 3))["available"] == 24

    order = (await api.post("/api/orders", json={"reservation_id": res.json()["id"]}, headers=h)).json()
    assert order["amount_cents"] == 42300
    assert order["items"] == [
        {"product_id": 1, "qty": 2, "unit_price_cents": 18900},
        {"product_id": 3, "qty": 1, "unit_price_cents": 4500},
    ]

    assert (await pay(api, h, order["id"]))["order_status"] == "paid"
    assert (await stock(api, 1))["sold"] == 2 and (await stock(api, 3))["sold"] == 1
    assert (await get_stats(api))["all_consistent"] is True


async def test_same_product_listed_twice_is_merged(api):
    h = await signup(api, "dupes")
    res = await reserve(api, h, items=[{"product_id": 1, "qty": 1}, {"product_id": 1, "qty": 2}])
    assert res.status_code == 201
    assert res.json()["items"] == [{"product_id": 1, "qty": 3}]
    assert (await stock(api, 1))["held"] == 3


async def test_invalid_carts_are_rejected(api, monkeypatch):
    h = await signup(api, "invalid")
    assert (await reserve(api, h, items=[])).status_code == 422
    assert (await reserve(api, h, items=[{"product_id": 1, "qty": 0}])).status_code == 422
    assert (await reserve(api, h, qty=4)).status_code == 422                       # over the cap of 3
    assert (await reserve(api, h, items=[{"product_id": 999, "qty": 1}])).status_code == 404
    monkeypatch.setattr(settings, "max_cart_lines", 2)
    three = [{"product_id": p, "qty": 1} for p in (1, 2, 3)]
    assert (await reserve(api, h, items=three)).status_code == 422                 # too many lines
    assert (await stock(api, 1))["available"] == 10                                # nothing was held


async def test_cart_is_all_or_nothing_when_one_line_is_sold_out(api):
    h = await signup(api, "unlucky")
    await set_available(2, 0)  # the jacket is gone
    res = await reserve(api, h, items=[{"product_id": 1, "qty": 2}, {"product_id": 2, "qty": 1}])
    assert res.status_code == 409
    assert "Atlas Puffer Jacket" in res.json()["detail"]
    assert (await stock(api, 1))["available"] == 10   # the sneakers were NOT left held
    assert await quota_total(1) == 0                  # and no quota was consumed
    assert (await reserve(api, h, items=[{"product_id": 1, "qty": 2}])).status_code == 201


async def test_customer_cap_applies_across_carts_and_frees_on_release(api):
    h = await signup(api, "capped")
    first = await reserve(api, h, qty=2)
    assert (await reserve(api, h, qty=1)).status_code == 201     # 3 of product 1 in total
    over = await reserve(api, h, qty=1)
    assert over.status_code == 409 and "Limit of 3" in over.json()["detail"]
    assert (await reserve(api, h, product_id=3, qty=1)).status_code == 201   # other products unaffected

    await api.delete(f"/api/reservations/{first.json()['id']}", headers=h)   # give 2 back
    assert (await reserve(api, h, qty=2)).status_code == 201


async def test_cap_holds_when_one_person_fires_many_carts_at_once(api):
    h = await signup(api, "hammer")
    results = await asyncio.gather(*[reserve(api, h) for _ in range(12)])  # 12 different keys
    codes = [r.status_code for r in results]
    assert codes.count(201) == 3 and codes.count(409) == 9
    assert (await stock(api, 1))["available"] == 7
    assert await quota_total(1) == 3


async def test_overlapping_carts_under_load_never_oversell_or_leak(api):
    """30 shoppers each want 2 sneakers + 1 jacket. Sneakers allow only 5 such carts.

    The 25 who lose on the sneakers must not leave a jacket held behind.
    """
    users = [await signup(api, f"u{i}") for i in range(30)]
    cart = [{"product_id": 2, "qty": 1}, {"product_id": 1, "qty": 2}]  # deliberately unsorted

    async def attempt(h):
        res = await reserve(api, h, items=cart)
        if res.status_code != 201:
            return res.status_code
        order = (await api.post("/api/orders", json={"reservation_id": res.json()["id"]}, headers=h)).json()
        await pay(api, h, order["id"])
        return 201

    codes = await asyncio.gather(*[attempt(h) for h in users])
    assert codes.count(201) == 5 and codes.count(409) == 25
    sneakers, jackets = await stock(api, 1), await stock(api, 2)
    assert (sneakers["available"], sneakers["sold"]) == (0, 10)
    assert (jackets["available"], jackets["sold"], jackets["held"]) == (10, 5, 0)  # nothing leaked
    assert (await get_stats(api))["all_consistent"] is True


async def test_expiry_returns_every_line_and_the_quota(api):
    h = await signup(api, "timeout")
    await buy_to_order(api, h, items=CART)
    assert await expire_everything() == 1
    assert (await stock(api, 1))["available"] == 10 and (await stock(api, 3))["available"] == 25
    assert await quota_total(1) == 0 and await quota_total(3) == 0
    assert (await reserve(api, h, items=CART)).status_code == 201   # can shop again


async def test_refund_returns_every_line(api):
    h = await signup(api, "refund")
    order = await buy_to_order(api, h, items=CART)
    await pay(api, h, order["id"])
    assert (await pay(api, h, order["id"], "refund"))["order_status"] == "refunded"
    assert (await stock(api, 1))["available"] == 10 and (await stock(api, 3))["available"] == 25
    assert await quota_total(1) == 0
    assert (await get_stats(api))["all_consistent"] is True


async def test_late_payment_takes_the_whole_cart_when_still_available(api):
    h = await signup(api, "late")
    order = await buy_to_order(api, h, items=CART)
    await expire_everything()
    result = await pay(api, h, order["id"])
    assert result["outcomes"] == ["paid_late"] and result["order_status"] == "paid"
    assert (await stock(api, 1))["sold"] == 2 and (await stock(api, 3))["sold"] == 1
    assert await quota_total(1) == 2
    assert (await get_stats(api))["all_consistent"] is True


async def test_late_payment_is_all_or_nothing(api):
    """Sneakers are still free but the jacket sold out: refund, and leak nothing."""
    h = await signup(api, "latecart")
    order = await buy_to_order(api, h, items=[{"product_id": 1, "qty": 2}, {"product_id": 2, "qty": 1}])
    await expire_everything()
    await set_available(2, 0)

    result = await pay(api, h, order["id"])
    assert result["outcomes"] == ["refunded_late"] and result["order_status"] == "refunded"
    sneakers = await stock(api, 1)
    assert sneakers["available"] == 10 and sneakers["sold"] == 0 and sneakers["held"] == 0
    assert await quota_total(1) == 0


async def test_multi_item_order_feeds_analytics_per_product(api):
    h = await signup(api, "stats")
    order = await buy_to_order(api, h, items=CART)
    await pay(api, h, order["id"])
    await relay()
    async with SessionLocal() as s:
        stats = {r.product_id: (r.orders, r.units, r.revenue_cents)
                 for r in (await s.execute(select(SalesStat))).scalars()}
    assert stats == {1: (1, 2, 37800), 3: (1, 1, 4500)}

    await pay(api, h, order["id"], "refund")
    await relay()
    async with SessionLocal() as s:
        stats = {r.product_id: (r.orders, r.units, r.revenue_cents)
                 for r in (await s.execute(select(SalesStat))).scalars()}
    assert stats == {1: (0, 0, 0), 3: (0, 0, 0)}


async def test_refunded_late_payment_never_counts_as_a_sale(api):
    """Regression: a payment refunded because the items were gone must not push analytics negative."""
    h = await signup(api, "ghost")
    order = await buy_to_order(api, h)
    await expire_everything()
    await set_available(1, 0)
    assert (await pay(api, h, order["id"]))["outcomes"] == ["refunded_late"]
    await relay()
    async with SessionLocal() as s:
        assert (await s.execute(select(func.count()).select_from(SalesStat))).scalar_one() == 0