import asyncio
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import select

from .config import settings
from .db import SessionLocal, init_db
from .metrics import HTTP_LATENCY, QUEUE_ADMITTED, QUEUE_WAITING
from .models import Drop, Inventory, Product
from .routers import auth, drops, orders, queue as queue_router, reservations, stats
from .services import inventory, outbox, queue

FRONTEND = Path(__file__).resolve().parent.parent / "frontend"

SEED = [
    # image_emoji holds a short key; the frontend shows /static/clothes/<key>.webp
    ("Tidepool Set", 14900, "aqua", 12),
    ("Rosewood Lace Set", 16900, "rouge", 8),
    ("Fern & Butterfly Set", 15900, "sage", 15),
    ("Pearl Corset Dress", 18900, "pearl", 10),
]


async def seed_if_empty():
    async with SessionLocal() as session:
        if (await session.execute(select(Drop.id).limit(1))).first():
            return
        drop = Drop(name="The Moonlit Edit", starts_at=datetime.now(timezone.utc) - timedelta(minutes=1))
        session.add(drop)
        await session.flush()
        for name, price, emoji, stock in SEED:
            p = Product(drop_id=drop.id, name=name, price_cents=price, image_emoji=emoji)
            session.add(p)
            await session.flush()
            session.add(Inventory(product_id=p.id, total=stock, available=stock))
        await session.commit()


async def _loop(name: str, interval: float, step):
    """Run `step` forever; a failing tick is logged, never fatal."""
    while True:
        try:
            await step()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"{name} error: {exc!r}")
        await asyncio.sleep(interval)


async def sweep_step():
    async with SessionLocal() as session:
        await inventory.expire_stale(session)


async def admit_step():
    async with SessionLocal() as session:
        drop_id = await queue.current_drop_id(session)
        if drop_id is None:
            return
        await queue.admit_batch(
            session, drop_id, batch=settings.queue_batch_size,
            max_active=settings.queue_max_active, window_seconds=settings.queue_admit_window_seconds,
        )
        snapshot = await queue.counts(session, drop_id)
        QUEUE_WAITING.set(snapshot["waiting"])
        QUEUE_ADMITTED.set(snapshot["admitted"])


async def outbox_step():
    await outbox.relay_once(SessionLocal)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.assert_safe_for_production()
    await init_db()
    await seed_if_empty()
    tasks = [
        asyncio.create_task(_loop("sweeper", settings.sweeper_interval_seconds, sweep_step)),
        asyncio.create_task(_loop("outbox", settings.outbox_interval_seconds, outbox_step)),
    ]
    if settings.queue_required:
        tasks.append(asyncio.create_task(_loop("admitter", settings.queue_interval_seconds, admit_step)))
    yield
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)  # let them unwind before the loop closes


app = FastAPI(title="nova", lifespan=lifespan)
for r in (auth.router, auth.admin_router, drops.router, queue_router.router, reservations.router, orders.router, stats.router):
    app.include_router(r)


@app.middleware("http")
async def record_latency(request: Request, call_next):
    start = time.perf_counter()
    status = 500  # what a crashing handler ends up as
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        route = request.scope.get("route")
        HTTP_LATENCY.labels(request.method, route.path if route else "unmatched", str(status)).observe(
            time.perf_counter() - start
        )


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/metrics")
async def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


app.mount("/static", StaticFiles(directory=FRONTEND), name="static")


@app.get("/")
async def index():
    return FileResponse(FRONTEND / "index.html")


@app.get("/dashboard")
async def dashboard():
    # no-store: the browser must not keep this page (and its JS state) for back/forward restores.
    return FileResponse(FRONTEND / "dashboard.html", headers={"Cache-Control": "no-store"})