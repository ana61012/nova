"""Passwordless sign-in: email a one-time code, exchange it for a token."""
import asyncio

from sqlalchemy import select

from app import ratelimit
from app.config import settings
from app.db import SessionLocal
from app.models import LoginCode, User
from app.ratelimit import TokenBucket

from .conftest import last_code, request_code


async def verify(api, email, code):
    return await api.post("/api/auth/verify-code", json={"email": email, "code": code})


async def expire_cooldown(email):
    """Pretend the resend cooldown has passed by back-dating the existing codes."""
    from datetime import timedelta
    async with SessionLocal() as s:
        for row in (await s.execute(select(LoginCode).where(LoginCode.email == email))).scalars():
            row.created_at = row.created_at - timedelta(seconds=settings.otp_resend_cooldown_seconds + 1)
        await s.commit()


async def test_code_signs_you_in_and_creates_the_account(api):
    r = await request_code(api, "Ann@Example.com")
    assert r.status_code == 202
    code = last_code("ann@example.com")
    assert len(code) == 6 and code.isdigit()

    ok = await verify(api, "ann@example.com", code)
    assert ok.status_code == 200 and ok.json()["email"] == "ann@example.com"
    me = await api.post("/api/queue/join", headers={"Authorization": f"Bearer {ok.json()['token']}"})
    assert me.status_code != 401  # the token works


async def test_second_sign_in_reuses_the_same_user(api):
    await request_code(api, "ann@example.com")
    first = (await verify(api, "ann@example.com", last_code("ann@example.com"))).json()
    await expire_cooldown("ann@example.com")
    await request_code(api, "ann@example.com")
    second = (await verify(api, "ann@example.com", last_code("ann@example.com"))).json()
    assert first["user_id"] == second["user_id"]
    async with SessionLocal() as s:
        assert len((await s.execute(select(User))).scalars().all()) == 1


async def test_code_is_single_use(api):
    await request_code(api, "ann@example.com")
    code = last_code("ann@example.com")
    assert (await verify(api, "ann@example.com", code)).status_code == 200
    assert (await verify(api, "ann@example.com", code)).status_code == 401


async def test_simultaneous_submissions_of_one_code_only_one_wins(api):
    await request_code(api, "ann@example.com")
    code = last_code("ann@example.com")
    results = await asyncio.gather(*[verify(api, "ann@example.com", code) for _ in range(8)])
    assert sorted(r.status_code for r in results).count(200) == 1


async def test_wrong_code_is_rejected_and_locks_out_after_max_attempts(api):
    await request_code(api, "ann@example.com")
    good = last_code("ann@example.com")
    bad = "000000" if good != "000000" else "111111"
    for _ in range(settings.otp_max_attempts):
        assert (await verify(api, "ann@example.com", bad)).status_code == 401
    # The code is now dead, even though this one is correct.
    assert (await verify(api, "ann@example.com", good)).status_code == 401


async def test_expired_code_is_rejected(api):
    from datetime import timedelta
    await request_code(api, "ann@example.com")
    async with SessionLocal() as s:
        row = (await s.execute(select(LoginCode))).scalar_one()
        row.expires_at = row.expires_at - timedelta(seconds=settings.otp_ttl_seconds + 1)
        await s.commit()
    assert (await verify(api, "ann@example.com", last_code("ann@example.com"))).status_code == 401


async def test_new_code_retires_the_old_one(api):
    await request_code(api, "ann@example.com")
    old = last_code("ann@example.com")
    await expire_cooldown("ann@example.com")
    await request_code(api, "ann@example.com")
    new = last_code("ann@example.com")
    if old != new:  # (a 1-in-a-million collision would make them equal)
        assert (await verify(api, "ann@example.com", old)).status_code == 401
    assert (await verify(api, "ann@example.com", new)).status_code == 200


async def test_code_only_works_for_the_address_it_was_sent_to(api):
    await request_code(api, "ann@example.com")
    code = last_code("ann@example.com")
    assert (await verify(api, "bob@example.com", code)).status_code == 401


async def test_resend_cooldown(api):
    assert (await request_code(api, "ann@example.com")).status_code == 202
    again = await request_code(api, "ann@example.com")
    assert again.status_code == 429 and again.headers["Retry-After"].isdigit()


async def test_hourly_cap_on_codes_per_address(api):
    for _ in range(settings.otp_max_codes_per_hour):
        assert (await request_code(api, "ann@example.com")).status_code == 202
        await expire_cooldown("ann@example.com")  # skip the 60s wait, the hourly cap still counts
    assert (await request_code(api, "ann@example.com")).status_code == 429


async def test_request_does_not_reveal_whether_an_account_exists(api):
    await request_code(api, "known@example.com")
    await verify(api, "known@example.com", last_code("known@example.com"))
    await expire_cooldown("known@example.com")
    known = await request_code(api, "known@example.com")
    unknown = await request_code(api, "stranger@example.com")
    assert known.status_code == unknown.status_code == 202
    assert known.json() == unknown.json()
    wrong_known = await verify(api, "known@example.com", "123456")
    wrong_unknown = await verify(api, "nobody@example.com", "123456")
    assert wrong_known.status_code == wrong_unknown.status_code == 401
    assert wrong_known.json() == wrong_unknown.json()


async def test_plaintext_code_is_never_stored(api):
    await request_code(api, "ann@example.com")
    code = last_code("ann@example.com")
    async with SessionLocal() as s:
        row = (await s.execute(select(LoginCode))).scalar_one()
    assert code not in row.code_hash and len(row.code_hash) == 64


async def test_malformed_input_is_rejected(api):
    assert (await api.post("/api/auth/request-code", json={"email": "not-an-email"})).status_code == 422
    for bad in ("12345", "1234567", "abcdef", ""):
        assert (await verify(api, "ann@example.com", bad)).status_code == 422


async def test_old_password_endpoints_are_gone(api):
    r = await api.post("/api/auth/signup", json={"email": "a@example.com", "password": "password123"})
    assert r.status_code in (404, 405)


async def test_auth_endpoints_are_rate_limited_per_ip(api, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    monkeypatch.setattr(ratelimit, "auth_ip_limiter", TokenBucket(3, 0.001))
    codes = [(await verify(api, "ann@example.com", "123456")).status_code for _ in range(5)]
    assert codes[:3] == [401, 401, 401] and codes[3:] == [429, 429]