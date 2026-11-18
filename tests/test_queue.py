from datetime import timedelta

from sqlalchemy import update

from app.config import settings
from app.db import SessionLocal
from app.models import QueueEntry, QueueStatus, utcnow
from app.services import queue

from .conftest import reserve, signup


async def _admit(batch=2, max_active=10, window=60):
    async with SessionLocal() as s:
        drop_id = await queue.current_drop_id(s)
        return await queue.admit_batch(s, drop_id, batch=batch, max_active=max_active, window_seconds=window)


async def _status(api, headers):
    return (await api.get("/api/queue/status", headers=headers)).json()


async def test_fifo_admission_and_live_positions(api):
    people = [await signup(api, f"p{i}") for i in range(5)]
    for h in people:
        assert (await api.post("/api/queue/join", headers=h)).status_code == 200

    assert (await _status(api, people[0]))["position"] == 1
    assert (await _status(api, people[4]))["position"] == 5

    assert await _admit(batch=2) == 2
    assert (await _status(api, people[0]))["state"] == "admitted"
    assert (await _status(api, people[1]))["state"] == "admitted"
    third = await _status(api, people[2])
    assert third["state"] == "waiting" and third["position"] == 1  # moved up
    assert (await _status(api, people[4]))["position"] == 3


async def test_joining_twice_keeps_your_place(api):
    a, b = await signup(api, "a"), await signup(api, "b")
    await api.post("/api/queue/join", headers=a)
    await api.post("/api/queue/join", headers=b)
    await api.post("/api/queue/join", headers=a)  # again
    assert (await _status(api, a))["position"] == 1
    assert (await _status(api, b))["position"] == 2


async def test_max_active_caps_admission_and_expiry_frees_slots(api):
    people = [await signup(api, f"q{i}") for i in range(6)]
    for h in people:
        await api.post("/api/queue/join", headers=h)

    assert await _admit(batch=10, max_active=3) == 3
    assert await _admit(batch=10, max_active=3) == 0  # room is full

    async with SessionLocal() as s:  # the three admitted people dawdle past their window
        await s.execute(update(QueueEntry).where(QueueEntry.status == QueueStatus.ADMITTED)
                        .values(admit_expires_at=utcnow() - timedelta(seconds=1)))
        await s.commit()
    assert await _admit(batch=10, max_active=3) == 3  # next three get in


async def test_leaving_frees_a_slot(api):
    a, b, c = [await signup(api, n) for n in "abc"]
    for h in (a, b, c):
        await api.post("/api/queue/join", headers=h)
    assert await _admit(batch=10, max_active=2) == 2
    await api.post("/api/queue/leave", headers=a)
    assert await _admit(batch=10, max_active=2) == 1
    assert (await _status(api, c))["state"] == "admitted"


async def test_expired_user_rejoins_at_the_back(api):
    a, b = await signup(api, "a"), await signup(api, "b")
    await api.post("/api/queue/join", headers=a)
    await api.post("/api/queue/join", headers=b)
    await _admit(batch=1, max_active=1, window=60)  # a is in
    async with SessionLocal() as s:
        await s.execute(update(QueueEntry).where(QueueEntry.status == QueueStatus.ADMITTED)
                        .values(admit_expires_at=utcnow() - timedelta(seconds=1)))
        await s.commit()
    assert (await _status(api, a))["state"] == "expired"
    await api.post("/api/queue/join", headers=a)
    assert (await _status(api, b))["position"] == 1
    assert (await _status(api, a))["position"] == 2  # behind b now


async def test_can_rejoin_after_finishing_or_leaving(api):
    a, b = await signup(api, "a"), await signup(api, "b")
    await api.post("/api/queue/join", headers=a)
    await api.post("/api/queue/join", headers=b)
    await _admit(batch=1, max_active=1)          # a is shopping
    await api.post("/api/queue/leave", headers=a)  # a taps "Done shopping"
    assert (await _status(api, a))["state"] == "done"

    rejoined = (await api.post("/api/queue/join", headers=a)).json()
    assert rejoined["state"] == "waiting"
    assert rejoined["position"] == 2             # behind b, not back at the front
    assert (await _status(api, b))["position"] == 1

    # Leaving while still waiting also lets you come back.
    await api.post("/api/queue/leave", headers=b)
    assert (await api.post("/api/queue/join", headers=b)).json()["state"] == "waiting"


async def test_reserving_requires_admission(api, monkeypatch):
    monkeypatch.setattr(settings, "queue_required", True)
    h = await signup(api, "eager")
    assert (await reserve(api, h)).status_code == 403  # never joined

    await api.post("/api/queue/join", headers=h)
    assert (await reserve(api, h)).status_code == 403  # joined but still waiting

    await _admit()
    assert (await reserve(api, h)).status_code == 201  # admitted