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
    ("Shadow Runner 01", 18900, "👟", 10),
    ("Atlas Puffer Jacket", 24900, "🧥", 15),
    ("Mono Bucket Hat", 4500, "🧢", 25),
    ("Vector Cargo Pants", 12900, "👖", 12),
]


async def seed_if_empty():
    async with SessionLocal() as session:
        if (await session.execute(select(Drop.id).limit(1))).first():
            return
        drop = Drop(name="Winter Drop 001", starts_at=datetime.now(timezone.utc) - timedelta(minutes=1))
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


app = FastAPI(title="DropEngine", lifespan=lifespan)
for r in (auth.router, drops.router, queue_router.router, reservations.router, orders.router, stats.router):
    app.include_router(r)


@app.middleware("http")
async def record_latency(request: Request, call_next):
    start = time.perf_counter()
    response = await call_next(request)
    route = request.scope.get("route")
    HTTP_LATENCY.labels(request.method, route.path if route else "unmatched", str(response.status_code)).observe(
        time.perf_counter() - start
    )
    return response


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
    return FileResponse(FRONTEND / "dashboard.html")
