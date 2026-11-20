import os
import tempfile

# The app builds its engine at import time, so point it at a throwaway file
# before anything imports `app`.
_API_DB = os.path.join(tempfile.mkdtemp(), "api_test.db")
os.environ["DROP_DATABASE_URL"] = f"sqlite+aiosqlite:///{_API_DB}"

import httpx  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker  # noqa: E402

from app import ratelimit  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import Base, engine, init_db, make_engine  # noqa: E402
from app.models import Drop, Inventory, Product, utcnow  # noqa: E402


@pytest_asyncio.fixture
async def sessions():
    """Fresh file-backed DB per test, so many sessions can write concurrently."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    eng = make_engine(f"sqlite+aiosqlite:///{path}")
    await init_db(eng)
    factory = async_sessionmaker(eng, expire_on_commit=False)
    yield factory
    await eng.dispose()
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(path + suffix)
        except FileNotFoundError:
            pass


@pytest_asyncio.fixture
async def product_id(sessions):
    """A product with exactly 10 units in stock."""
    async with sessions() as s:
        drop = Drop(name="Test Drop", starts_at=utcnow())
        s.add(drop)
        await s.flush()
        p = Product(drop_id=drop.id, name="Test Sneaker", price_cents=10000)
        s.add(p)
        await s.flush()
        s.add(Inventory(product_id=p.id, total=10, available=10))
        await s.commit()
        return p.id


@pytest_asyncio.fixture
async def api(monkeypatch):
    """The real app over ASGI with a fresh seeded database.

    Product 1 has 10 units. The queue and rate limiter are off by default;
    tests that exercise them switch them on.
    """
    from app.main import app, seed_if_empty

    monkeypatch.setattr(settings, "queue_required", False)
    monkeypatch.setattr(settings, "rate_limit_enabled", False)
    await engine.dispose()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    await seed_if_empty()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    await engine.dispose()
    ratelimit.user_limiter = ratelimit.TokenBucket(settings.rl_user_capacity, settings.rl_user_refill_per_sec)
    ratelimit.ip_limiter = ratelimit.TokenBucket(settings.rl_ip_capacity, settings.rl_ip_refill_per_sec)


# ---- helpers shared by API tests -------------------------------------------

async def signup(api, name: str) -> dict:
    r = await api.post("/api/auth/signup", json={"email": f"{name}@example.com", "password": "password123"})
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


async def reserve(api, headers, product_id=1, key=None, qty=1, items=None):
    """Reserve one product (default) or a whole cart via `items=[{"product_id":..,"qty":..}]`."""
    items = items if items is not None else [{"product_id": product_id, "qty": qty}]
    return await api.post(
        "/api/reservations", json={"items": items},
        headers={**headers, "Idempotency-Key": key or f"key-{os.urandom(4).hex()}-abcd"},
    )


async def buy_to_order(api, headers, product_id=1, qty=1, items=None) -> dict:
    """Reserve and create an order; returns the order JSON."""
    res = await reserve(api, headers, product_id, qty=qty, items=items)
    assert res.status_code == 201, res.text
    order = await api.post("/api/orders", json={"reservation_id": res.json()["id"]}, headers=headers)
    assert order.status_code == 201, order.text
    return order.json()


async def stock(api, product_id=1) -> dict:
    stats = (await api.get("/api/stats")).json()
    return next(p for p in stats["products"] if p["id"] == product_id)