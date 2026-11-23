import os
import tempfile

import pytest
from sqlalchemy import text

from app.db import init_db, make_engine
from app.security import hash_password, verify_password


def test_password_hash_roundtrip():
    h = hash_password("correct horse")
    assert h != hash_password("correct horse")           # salted
    assert verify_password("correct horse", h)
    assert not verify_password("wrong", h)
    assert not verify_password("anything", None)          # account without a password
    assert not verify_password("anything", "garbage")


async def test_signup_then_login(api):
    r = await api.post("/api/auth/signup", json={"email": "New@Example.com", "password": "hunter2hunter2"})
    assert r.status_code == 201, r.text
    assert r.json()["email"] == "new@example.com"
    token = r.json()["token"]
    # the token really works on a protected route
    assert (await api.post("/api/queue/join", headers={"Authorization": f"Bearer {token}"})).status_code != 401

    ok = await api.post("/api/auth/login", json={"email": "new@example.com", "password": "hunter2hunter2"})
    assert ok.status_code == 200 and ok.json()["user_id"] == r.json()["user_id"]


async def test_bad_logins_look_identical(api):
    await api.post("/api/auth/signup", json={"email": "real@example.com", "password": "hunter2hunter2"})
    wrong = await api.post("/api/auth/login", json={"email": "real@example.com", "password": "nope-nope"})
    ghost = await api.post("/api/auth/login", json={"email": "ghost@example.com", "password": "nope-nope"})
    assert wrong.status_code == ghost.status_code == 401
    assert wrong.json() == ghost.json()


async def test_signup_rejects_duplicates_and_short_passwords(api):
    body = {"email": "dup@example.com", "password": "hunter2hunter2"}
    assert (await api.post("/api/auth/signup", json=body)).status_code == 201
    assert (await api.post("/api/auth/signup", json=body)).status_code == 409
    assert (await api.post("/api/auth/signup", json={"email": "s@example.com", "password": "short"})).status_code == 422


async def test_code_only_account_cannot_be_claimed_by_password_signup(api):
    from .conftest import signup
    await signup(api, "codey")  # created through the emailed-code flow
    assert (await api.post("/api/auth/signup", json={"email": "codey@example.com", "password": "hunter2hunter2"})).status_code == 409
    assert (await api.post("/api/auth/login", json={"email": "codey@example.com", "password": "hunter2hunter2"})).status_code == 401


async def test_old_database_without_password_column_is_upgraded():
    """A dropengine.db made before password_hash existed must keep working."""
    fd, path = tempfile.mkstemp(suffix=".db"); os.close(fd)
    eng = make_engine(f"sqlite+aiosqlite:///{path}")
    try:
        async with eng.begin() as c:
            await c.execute(text("CREATE TABLE users (id VARCHAR(36) PRIMARY KEY, email VARCHAR(254) UNIQUE, created_at DATETIME)"))
            await c.execute(text("INSERT INTO users VALUES ('u1', 'old@example.com', '2025-01-01')"))
        await init_db(eng)
        async with eng.begin() as c:
            cols = [r[1] for r in (await c.execute(text("PRAGMA table_info(users)"))).all()]
            assert "password_hash" in cols
            assert (await c.execute(text("SELECT count(*) FROM users"))).scalar_one() == 1
    finally:
        await eng.dispose()
        for suf in ("", "-wal", "-shm"):
            try: os.remove(path + suf)
            except FileNotFoundError: pass