from app import ratelimit, security
from app.config import settings
from app.ratelimit import TokenBucket

from .conftest import admin_headers, admin_login, signup


async def test_stats_requires_admin_login(api):
    assert (await api.get("/api/stats")).status_code == 401
    assert (await api.get("/api/stats", headers={"Authorization": "Bearer nonsense"})).status_code == 401
    assert (await api.get("/api/stats", headers=await admin_headers(api))).status_code == 200


async def test_wrong_admin_credentials_are_rejected(api):
    assert (await admin_login(api, password="nope")).status_code == 401
    assert (await admin_login(api, username="someone-else")).status_code == 401


async def test_shop_token_cannot_open_the_dashboard(api):
    shopper = await signup(api, "curious")
    assert (await api.get("/api/stats", headers=shopper)).status_code == 401


async def test_admin_token_is_not_a_shop_token(api):
    admin = await admin_headers(api)
    assert (await api.post("/api/queue/join", headers=admin)).status_code == 401
    assert (await api.post("/api/orders", json={"reservation_id": "x"}, headers=admin)).status_code == 401


async def test_dashboard_login_dies_on_server_restart(api, monkeypatch):
    h = await admin_headers(api)
    assert (await api.get("/api/stats", headers=h)).status_code == 200
    monkeypatch.setattr(security, "BOOT_ID", "a-new-process")  # simulate a server restart
    assert (await api.get("/api/stats", headers=h)).status_code == 401


async def test_admin_login_is_rate_limited(api, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    monkeypatch.setattr(ratelimit, "auth_ip_limiter", TokenBucket(3, 0.001))
    codes = [(await admin_login(api, password="wrong")).status_code for _ in range(5)]
    assert codes[:3] == [401, 401, 401] and codes[3:] == [429, 429]


async def test_dashboard_page_is_not_cached(api):
    r = await api.get("/dashboard")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"