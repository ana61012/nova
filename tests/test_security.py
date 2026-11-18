from app import ratelimit
from app.config import settings
from app.ratelimit import TokenBucket

from .conftest import reserve, signup


async def test_signup_login_flow(api):
    r = await api.post("/api/auth/signup", json={"email": "Ann@Example.com", "password": "password123"})
    assert r.status_code == 201
    assert (await api.post("/api/auth/signup", json={"email": "ann@example.com", "password": "password123"})).status_code == 409

    ok = await api.post("/api/auth/login", json={"email": "ann@example.com", "password": "password123"})
    assert ok.status_code == 200 and ok.json()["token"]
    bad = await api.post("/api/auth/login", json={"email": "ann@example.com", "password": "wrong-password"})
    ghost = await api.post("/api/auth/login", json={"email": "nobody@example.com", "password": "password123"})
    assert bad.status_code == ghost.status_code == 401
    assert bad.json() == ghost.json()  # cannot tell "no such user" from "wrong password"


async def test_protected_routes_need_a_valid_token(api):
    assert (await api.post("/api/queue/join")).status_code == 401
    assert (await api.post("/api/queue/join", headers={"Authorization": "Bearer nonsense"})).status_code == 401
    assert (await api.post("/api/orders", json={"reservation_id": "x"})).status_code == 401


def test_token_bucket_refills_over_time():
    now = [0.0]
    bucket = TokenBucket(capacity=2, refill_per_sec=1, clock=lambda: now[0])
    assert bucket.allow("k")[0] and bucket.allow("k")[0]
    allowed, retry_after = bucket.allow("k")
    assert not allowed and 0 < retry_after <= 1
    assert bucket.allow("other")[0]  # keys are independent
    now[0] += 1.0
    assert bucket.allow("k")[0]


async def test_rate_limit_returns_429_with_retry_after(api, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    monkeypatch.setattr(ratelimit, "user_limiter", TokenBucket(3, 0.001))
    h = await signup(api, "spammer")
    codes = [(await reserve(api, h)).status_code for _ in range(5)]
    assert codes[:3] == [201, 409, 409]  # allowed through (then one-per-customer kicks in)
    assert codes[3:] == [429, 429]
    blocked = await reserve(api, h)
    assert blocked.headers["Retry-After"].isdigit()
