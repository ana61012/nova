import os
import tempfile
import time

# The app builds its engine at import time, so point it at a throwaway file
# before anything imports `app`.
_API_DB = os.path.join(tempfile.mkdtemp(), "api_test.db")
os.environ["DROP_DATABASE_URL"] = f"sqlite+aiosqlite:///{_API_DB}"
# Env vars beat the developer's local .env, so real Razorpay keys (or a non-dev
# mode) in .env can't leak into the suite. Tests expect the simulated gateway.
for _name, _value in {
    "DROP_RAZORPAY_KEY_ID": "",
    "DROP_RAZORPAY_KEY_SECRET": "",
    "DROP_RAZORPAY_WEBHOOK_SECRET": "",
    "DROP_DEV_MODE": "true",
    "DROP_WEBHOOK_SECRET": "whsec_dev",
}.items():
    os.environ[_name] = _value

import httpx  # noqa: E402
import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from app import ratelimit
from app import db as db_mod  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import init_db, make_engine  # noqa: E402
from app.models import Drop, Inventory, Product, utcnow  # noqa: E402
from app.services import mailer as mailer_mod  # noqa: E402


def _remove_db_files(path: str) -> None:
    """Delete a temp SQLite file and its WAL/SHM siblings.

    On Windows a just-closed SQLite file can stay locked for a moment (WinError 32),
    so retry briefly and then give up quietly: a leftover temp file must never
    turn a passing test into an error.
    """
    for suffix in ("", "-wal", "-shm"):
        for attempt in range(5):
            try:
                os.remove(path + suffix)
                break
            except FileNotFoundError:
                break
            except PermissionError:
                time.sleep(0.05 * (attempt + 1))


@pytest.fixture(autouse=True)
def _simulated_payments(monkeypatch):
    """Every test starts on the simulated gateway, whatever the local .env says."""
    monkeypatch.setattr(settings, "razorpay_key_id", "")
    monkeypatch.setattr(settings, "razorpay_key_secret", "")
    monkeypatch.setattr(settings, "razorpay_webhook_secret", "")
    monkeypatch.setattr(settings, "dev_mode", True)


# (name, price_cents, image key, stock) -> product ids 1..4
TEST_SEED = [
    ("Test Sneaker", 18900, "pearl", 10),
    ("Atlas Puffer Jacket", 24900, "aqua", 15),
    ("Test Product 3", 4500, "sage", 25),
    ("Test Product 4", 12900, "rouge", 12),
]


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
    _remove_db_files(path)


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
    import app.main as main_mod
    from app.main import app, seed_if_empty

    # Tests rely on a fixed catalogue, independent of the storefront's real seed data.
    monkeypatch.setattr(main_mod, "SEED", TEST_SEED)
    monkeypatch.setattr(settings, "queue_required", False)
    monkeypatch.setattr(settings, "rate_limit_enabled", False)
    monkeypatch.setattr(mailer_mod, "mailer", mailer_mod.MemoryMailer())  # capture "emails" in memory

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    test_engine = make_engine(f"sqlite+aiosqlite:///{path}", poolclass=NullPool)
    orig_engine = db_mod.engine
    db_mod.engine = test_engine
    db_mod.SessionLocal.configure(bind=test_engine)

    try:
        await init_db(test_engine)
        await seed_if_empty()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client
    finally:
        await test_engine.dispose()
        db_mod.engine = orig_engine
        db_mod.SessionLocal.configure(bind=orig_engine)
        _remove_db_files(path)
        ratelimit.user_limiter = ratelimit.TokenBucket(settings.rl_user_capacity, settings.rl_user_refill_per_sec)
        ratelimit.ip_limiter = ratelimit.TokenBucket(settings.rl_ip_capacity, settings.rl_ip_refill_per_sec)
        ratelimit.auth_ip_limiter = ratelimit.TokenBucket(settings.rl_auth_capacity, settings.rl_auth_refill_per_sec)

# ---- helpers shared by API tests -------------------------------------------

async def request_code(api, email: str):
    return await api.post("/api/auth/request-code", json={"email": email})


def last_code(email: str) -> str:
    return mailer_mod.mailer.latest[email.lower()]


async def signup(api, name: str) -> dict:
    """Sign in the passwordless way: request a code, read it from the fake mailbox, verify."""
    email = f"{name}@example.com"
    assert (await request_code(api, email)).status_code == 202
    r = await api.post("/api/auth/verify-code", json={"email": email, "code": last_code(email)})
    assert r.status_code == 200, r.text
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


async def admin_login(api, username=None, password=None):
    return await api.post("/api/admin/login", json={
        "username": username or settings.admin_username, "password": password or settings.admin_password,
    })


async def admin_headers(api) -> dict:
    r = await admin_login(api)
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


async def get_stats(api) -> dict:
    r = await api.get("/api/stats", headers=await admin_headers(api))
    assert r.status_code == 200, r.text
    return r.json()


async def stock(api, product_id=1) -> dict:
    stats = await get_stats(api)
    return next(p for p in stats["products"] if p["id"] == product_id)