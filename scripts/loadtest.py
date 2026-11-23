#!/usr/bin/env python
"""Simulate a drop: N shoppers go signup -> queue -> reserve a cart -> order -> pay.

Each shopper builds a random cart: 1..--max-qty units of the main product, and
(half the time) one or two units of another product. Carts are all-or-nothing, so
many shoppers lose on a sold-out line. Afterwards the server's per-product
numbers are compared with what the clients say they paid for.

Run against a FRESH server so the stock numbers start clean. Rate limiting
should be off for the server, because every simulated shopper shares one IP:

    DROP_EMAIL_BACKEND=memory DROP_RATE_LIMIT_ENABLED=false DROP_QUEUE_BATCH_SIZE=25 DROP_QUEUE_INTERVAL_SECONDS=0.5 \\
    DROP_QUEUE_MAX_ACTIVE=50 DROP_QUEUE_ADMIT_WINDOW_SECONDS=30 \\
        uvicorn app.main:app --port 8000 --timeout-keep-alive 30
    python scripts/loadtest.py --users 300

Exit code is non-zero if the inventory audit fails, anything was oversold, or the
units clients paid for differ from the units the server recorded as sold.
"""
import argparse
import asyncio
import os
import random
import sys
import time
import uuid
from collections import defaultdict

import httpx


def pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * p))]


async def call(client: httpx.AsyncClient, out: dict, method: str, url: str, tries: int = 4, **kw):
    """Like a real client: retry transport failures (dropped keep-alive connections,
    resets under load). Safe here because every write is idempotent: reservations
    reuse their Idempotency-Key, orders are unique per reservation, and webhook
    events are deduplicated server-side."""
    for attempt in range(tries):
        try:
            return await client.request(method, url, **kw)
        except httpx.TransportError:
            out["retries"] += 1
            if attempt == tries - 1:
                raise
            await asyncio.sleep(0.25 * (attempt + 1))


def build_cart(i: int, args, others: list[int]) -> dict[int, int]:
    rng = random.Random(i)  # reproducible: shopper i always wants the same cart
    cart = {args.product: rng.randint(1, args.max_qty)}
    if others and rng.random() < 0.5:
        cart[rng.choice(others)] = rng.randint(1, 2)
    return cart


async def shopper(i: int, client: httpx.AsyncClient, args, run: str, gate: asyncio.Event, out: dict, others):
    email = f"load-{run}-{i}@example.com"
    sent = await call(client, out, "POST", "/api/auth/request-code", json={"email": email})
    assert sent.status_code == 202, sent.text
    box = await call(client, out, "GET", "/api/auth/dev/mailbox", params={"email": email})
    assert box.status_code == 200, "start the server with DROP_EMAIL_BACKEND=memory"
    r = await client.post("/api/auth/verify-code", json={"email": email, "code": box.json()["code"]})
    r.raise_for_status()
    h = {"Authorization": f"Bearer {r.json()['token']}"}
    out["signed_up"] += 1

    await gate.wait()  # everyone is registered; now the drop opens for all at once

    t = time.perf_counter()
    r = await call(client, out, "POST", "/api/queue/join", headers=h)
    out["join_ms"].append((time.perf_counter() - t) * 1000)
    if r.status_code != 200:
        out["errors"] += 1
        return

    deadline = time.monotonic() + args.timeout
    while True:
        s = (await call(client, out, "GET", "/api/queue/status", headers=h)).json()
        if s["state"] == "admitted":
            break
        if time.monotonic() > deadline or s["state"] in ("expired", "done"):
            out["never_admitted"] += 1
            return
        await asyncio.sleep(args.poll)

    cart = build_cart(i, args, others)
    items = [{"product_id": pid, "qty": qty} for pid, qty in cart.items()]
    key = uuid.uuid4().hex  # reused on every retry of this reservation
    t = time.perf_counter()
    r = await call(
        client, out, "POST", "/api/reservations", json={"items": items},
        headers={**h, "Idempotency-Key": key},
    )
    out["reserve_ms"].append((time.perf_counter() - t) * 1000)

    if r.status_code in (200, 201):
        o = await call(client, out, "POST", "/api/orders", json={"reservation_id": r.json()["id"]}, headers=h)
        p = await call(client, out, "POST", f"/api/dev/payments/{o.json()['id']}/simulate", headers=h)
        if p.json().get("order_status") == "paid":
            out["paid"] += 1
            for pid, qty in cart.items():
                out["units"][pid] += qty
        else:
            out["errors"] += 1
    elif r.status_code == 409:
        out["sold_out"] += 1
        await call(client, out, "POST", "/api/queue/leave", headers=h)  # free the slot for others
    else:
        out["errors"] += 1


async def fetch_stats(client, args) -> dict:
    """/api/stats is operator-only: sign in as the dashboard admin, then read it."""
    r = await client.post("/api/admin/login", json={"username": args.admin_user, "password": args.admin_password})
    r.raise_for_status()
    r = await client.get("/api/stats", headers={"Authorization": "Bearer " + r.json()["token"]})
    r.raise_for_status()
    return r.json()


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://localhost:8000")
    ap.add_argument("--users", type=int, default=300)
    ap.add_argument("--product", type=int, default=1, help="the contested product everyone wants")
    ap.add_argument("--max-qty", type=int, default=2, help="max units of the main product per cart")
    ap.add_argument("--poll", type=float, default=1.0, help="seconds between queue status polls")
    ap.add_argument("--timeout", type=float, default=180, help="give up waiting for admission after this long")
    ap.add_argument("--admin-user", default=os.environ.get("DROP_ADMIN_USERNAME", "admin"))
    ap.add_argument("--admin-password", default=os.environ.get("DROP_ADMIN_PASSWORD", "admin-dev-change-me"))
    args = ap.parse_args()

    run = uuid.uuid4().hex[:6]
    out = {"signed_up": 0, "paid": 0, "sold_out": 0, "never_admitted": 0, "errors": 0, "retries": 0,
           "join_ms": [], "reserve_ms": [], "units": defaultdict(int)}
    limits = httpx.Limits(max_connections=args.users + 10, max_keepalive_connections=args.users + 10)
    async with httpx.AsyncClient(base_url=args.base_url, timeout=60, limits=limits) as client:
        before = await fetch_stats(client, args)
        others = [p["id"] for p in before["products"] if p["id"] != args.product]

        gate = asyncio.Event()
        tasks = [
            asyncio.create_task(shopper(i, client, args, run, gate, out, others))
            for i in range(args.users)
        ]
        t0 = time.perf_counter()
        while out["signed_up"] < args.users:  # wait for the registration phase
            await asyncio.sleep(0.1)
            if all(t.done() for t in tasks):
                break
        signup_s = time.perf_counter() - t0

        t1 = time.perf_counter()
        gate.set()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        drop_s = time.perf_counter() - t1
        crashed = [r for r in results if isinstance(r, BaseException)]
        out["errors"] += len(crashed)  # a shopper that blew up is an error, never silently dropped
        for exc in crashed[:3]:
            print(f"shopper crashed: {exc!r}", file=sys.stderr)

        # let the outbox relay catch up so the backlog figure is meaningful
        await asyncio.sleep(2)
        stats = await fetch_stats(client, args)

    products = stats["products"]
    oversold = [p["name"] for p in products if p["sold"] > p["total"] or p["available"] < 0]
    mismatched = [p["name"] for p in products if p["sold"] != out["units"].get(p["id"], 0)]
    accounted = out["paid"] + out["sold_out"] + out["never_admitted"] + out["errors"]

    table = "\n".join(
        f"  {p['name']:<22} total={p['total']:<3} sold={p['sold']:<3} paid-by-clients={out['units'].get(p['id'], 0):<3} "
        f"available={p['available']:<3} held={p['held']:<2} {'ok' if p['consistent'] else 'INCONSISTENT'}"
        for p in products
    )
    print(f"""
Drop simulation: {args.users} shoppers with random carts (contested product: {args.product})
  signup phase          {signup_s:6.1f}s
  drop phase            {drop_s:6.1f}s (join -> admitted -> reserve cart -> pay)

  paid carts            {out['paid']:6d}
  sold out (409)        {out['sold_out']:6d}
  never admitted        {out['never_admitted']:6d}
  errors                {out['errors']:6d}
  client retries        {out['retries']:6d}   (dropped connections, retried idempotently)
  accounted for         {accounted:6d} of {args.users}

  queue join latency    p50 {pct(out['join_ms'], .5):7.1f} ms   p95 {pct(out['join_ms'], .95):7.1f} ms
  reserve latency       p50 {pct(out['reserve_ms'], .5):7.1f} ms   p95 {pct(out['reserve_ms'], .95):7.1f} ms   ({len(out['reserve_ms'])} calls)
  carts paid / sec      {out['paid'] / drop_s if drop_s else 0:6.2f}

Audit (server side, every product)
{table}
  available + held + sold == total for all : {stats['all_consistent']}
  outbox backlog                           : {stats['outbox_pending']}
  revenue (paid orders)                    : {stats['orders']['revenue_cents'] / 100:.2f}
""")
    ok = (not oversold and not mismatched and stats["all_consistent"] and out["errors"] == 0
          and accounted == args.users)
    if oversold:
        print("OVERSOLD:", oversold)
    if mismatched:
        print("SERVER vs CLIENT MISMATCH:", mismatched)
    print("RESULT:", "PASS, zero oversell" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))