# nova

A limited-drop commerce backend for small-run fashion releases, built for the moment thousands of people try to buy a few hundred items at once. The goal is simple to state and hard to get right: **never oversell, never double-charge, stay fair under load.**

**Live:** https://nova-2w36.onrender.com · 

> Hosted on Render (Docker) with a Neon serverless Postgres database. The free Render tier sleeps when idle, so the first request after a quiet period can take a while.

**Stack:** FastAPI (async) · SQLAlchemy 2 · PostgreSQL (Neon in production, SQLite for zero-setup local runs) · JWT auth · Server-Sent Events · Razorpay · Prometheus · Angular 19 (standalone components, signals, animations) · Docker · GitHub Actions

## Results at a glance

| Check | Result |
| --- | --- |
| 300 shoppers racing for 10 units | **0 oversold, 0 errors** |
| 100 shoppers in one simultaneous drop | **0 oversold, 0 errors** |
| Server-side inventory audit vs client orders | Matched exactly |
| Automated tests | 88 pytest tests, 76% coverage |
| Coverage on critical modules | 94-96% inventory and outbox, 100% security and rate limiting |
| CI | GitHub Actions fails the build on any oversell |

## Features

- **Oversell-proof inventory:** atomic conditional `UPDATE`s, a database `CHECK (available >= 0)` backstop, and idempotent reservations.
- **Built for retries and failures:** idempotency keys, a transactional outbox with at-least-once delivery, and a FIFO waiting room with batch admission and capped concurrency.
- **Payments:** Razorpay checkout verified on the server, HMAC-SHA256-verified and deduplicated webhooks, and automatic refunds for late payments.
- **Abuse protection:** per-IP and per-user token-bucket rate limiting.
- **Secure OTP login:** 6-digit codes stored hashed, with attempt limits, expiry, single-use enforcement and resend throttling, so brute-force and replay attacks fail even under concurrent load.
- **Observability:** Prometheus metrics at `/metrics` and a live operator dashboard.
- **Frontend:** an Angular 19 shop (live queue position over SSE, reservation countdown) and an operator dashboard.

## Run it locally

Requires Python 3.12 and Node 20+.

```bash
(cd frontend && npm install && npm run build)   # builds the Angular app into frontend/dist
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

uvicorn app.main:app --reload --timeout-keep-alive 30
# shop:      http://localhost:8000
# dashboard: http://localhost:8000/dashboard   (operator login: admin / admin-dev-change-me)
# API docs:  http://localhost:8000/docs
# metrics:   http://localhost:8000/metrics

pytest -q                       # 88 tests
docker compose up --build       # Postgres + API + Prometheus
```

Every setting is an environment variable with a `DROP_` prefix (or a `.env` file); see `app/config.py`.

For live reload while working on the UI, keep `uvicorn` running, start `cd frontend && npm start`, and open <http://localhost:4200> (it proxies `/api` to port 8000).

## Deployment (Render + Neon)

### 1. Database on Neon

1. Create a Neon project and copy the connection string from the dashboard.
2. Make sure the URL requires SSL (Neon does by default). Use the SSL parameter syntax your Postgres driver expects.
3. Neon scales to zero when idle, so the first query after a pause can be slow. That is a cold start, not a bug.

### 2. Web service on Render

1. Create a new **Web Service** from this GitHub repo and choose the **Docker** runtime. The multi-stage Dockerfile builds the Angular app and then the API image.
2. Set the environment variables below.
3. Start command (if not set by the Dockerfile):

   ```bash
   uvicorn app.main:app --host 0.0.0.0 --port $PORT --timeout-keep-alive 30
   ```

4. Set the health check path to `/docs` (or any lightweight route you prefer).
5. In the Razorpay dashboard, point the webhook at `https://YOUR-APP.onrender.com/webhooks/payments` and use the same secret you set in `DROP_WEBHOOK_SECRET`.

### Environment variables

| Variable | Purpose |
| --- | --- |
| Database URL (see `app/config.py` for the exact `DROP_` name) | Neon connection string |
| `DROP_DEV_MODE=false` | Disables the fake payment endpoint; the server refuses to boot if any secret below is still a default |
| `DROP_JWT_SECRET` | Signs user tokens |
| `DROP_WEBHOOK_SECRET` | Verifies Razorpay webhook signatures (HMAC-SHA256) |
| `DROP_ADMIN_USERNAME` / `DROP_ADMIN_PASSWORD` | Operator dashboard login |
| `DROP_ADMIN_JWT_TTL_SECONDS` | Operator token lifetime |
| Razorpay key id and secret (see `app/config.py`) | Server-verified checkout |

Run **one worker**. The admitter, sweeper and outbox relay run inside the API process (see Limitations).

## How a purchase flows

```
 login (OTP) ──► join queue ──► wait (live position over SSE)
                                      │ admitter lets a few in at a time
                                      ▼
                              admitted: reserve stock   ◄── Idempotency-Key, one per customer
                                      │ 2 min hold (TTL)
                                      ▼
                               create order (pending_payment)
                                      │
                    Razorpay ──signed webhook──► verify, dedupe, apply
                                      ▼
                  paid ─────► outbox event ─► relay ─► email + analytics consumers
```

## The guarantees, and what enforces each

| Problem | Solution | Proven by |
| --- | --- | --- |
| Two buyers, one unit left | Single conditional `UPDATE ... WHERE available >= qty`, atomic in the DB; `CHECK (available >= 0)` as backstop | `test_no_oversell_under_concurrency` (200 buyers, 10 units) |
| Double-click / network retry | `Idempotency-Key` header + `UNIQUE(user_id, idempotency_key)` | `test_simultaneous_double_click...` |
| One customer hoarding | Partial unique index on `(user_id, product_id)` for live reservations, so concurrent requests with different keys cannot both win | `test_one_per_customer_even_with_different_keys` |
| Abandoned checkouts hoard stock | Reservation TTL and a background sweeper | `test_expired_holds_return_stock_exactly_once` |
| Sweeper and cancel race | The status flip is itself a conditional `UPDATE`; only the caller with `rowcount == 1` returns the stock | same test, 3 concurrent sweepers |
| Stampede on the inventory | Virtual waiting room: FIFO queue, batch admission, capped concurrency; no admission means no reservation | `tests/test_queue.py` |
| Jumping the line | Queue position is the autoincrement id; missing your window sends you to the back | `test_expired_user_rejoins_at_the_back` |
| Duplicate webhook | Event id is a primary key; replays are no-ops | `test_duplicate_webhook...`, `test_same_event_delivered_concurrently...` |
| Forged webhook | HMAC-SHA256 signature check | `test_bad_signature_is_rejected` |
| Payment arrives after the hold expired | Try to re-take stock: paid if still available, **refunded** if someone else bought it | `test_late_payment_*` |
| Refund event before the payment event | Parked as `ignored_out_of_order` | `test_refund_event_before_payment_is_ignored` |
| Order saved but "paid" event lost (dual write) | Transactional outbox: state change and event commit together | `tests/test_outbox.py` |
| Consumer crashes mid-delivery | At-least-once delivery + `processed_events` makes consumers idempotent | `test_failing_consumer_is_retried...` |
| Bots and spam | Per-IP and per-user token buckets, `429` with `Retry-After` | `test_rate_limit_returns_429...` |
| OTP brute force and replay | Hashed codes, attempt limits, expiry, single-use enforcement, resend throttling | auth tests |

### Order state machine

```
pending_payment ──paid──► paid ──refund──► refunded
      │                                       ▲
      └─expired (hold lapsed)─► expired ──────┘ (late payment, item sold out)
                                   └──paid again (late payment, item still available)
```

Transitions are conditional `UPDATE`s checked against an `ALLOWED` table, so an illegal move raises instead of corrupting state.

## API

| Method | Path | Notes |
| --- | --- | --- |
| POST | `/api/auth/*` | OTP request and verify, returns a JWT (see `/docs` for exact routes) |
| GET | `/api/drops/current` | Products with live stock |
| POST | `/api/queue/join` · `/leave` | Join or leave the waiting room |
| GET | `/api/queue/status` | Your state and position |
| GET | `/api/queue/stream?token=` | SSE live updates (EventSource cannot send headers, so the token is a query param) |
| POST | `/api/reservations` | Header `Idempotency-Key`. 201 created, 200 replay, 403 not admitted, 409 sold out or already owned, 429 rate limited |
| DELETE | `/api/reservations/{id}` | Release a hold |
| POST | `/api/orders` | Create an order from a live reservation (idempotent per reservation) |
| GET | `/api/orders/{id}` | Your order |
| POST | `/webhooks/payments` | Razorpay entry point, HMAC-signed |
| POST | `/api/dev/payments/{order_id}/simulate?outcome=success\|fail\|refund&duplicate=` | Fake gateway for demos (`DROP_DEV_MODE=false` disables it) |
| POST | `/api/admin/login` | `{username, password}` -> short-lived admin token (dashboard only) |
| GET | `/api/stats` | **Admin token required.** Dashboard data plus the **inventory audit** (`available + held + sold == total`) |
| GET | `/metrics` | Prometheus |

## Frontend

A single Angular 19 app (standalone components, signals, TypeScript strict mode) serves both the shop and the operator dashboard. FastAPI serves the built bundle from `/static/` and returns it for `/` and `/dashboard`; the Angular router picks the page.

- **Shop:** collection slider, OTP sign-in, live queue position over SSE, reservation hold with a countdown, and server-verified Razorpay checkout.
- **Dashboard:** operator login, live tiles (queue, orders per second, revenue, outbox backlog), per-product stock bars and the oversell audit, refreshed every second.
- **State:** one `ShopService` holds all shop state as signals. Sessions and tokens live in memory only, so a reload signs you out.
- **Motion:** route transitions, staggered cards, animated lists and counters. All of it switches off under `prefers-reduced-motion`.

## Testing and CI

```bash
pytest -q          # 88 tests
pytest --cov=app   # coverage report (76% overall)
```

The suite includes a 200-buyer concurrency test against 10 units. GitHub Actions runs the tests and the load test on every push and **fails the build on any oversell**.

### Load test

`scripts/loadtest.py` runs the whole funnel (signup, queue, reserve, order, pay) with N shoppers released at the same instant, then audits the server. It exits non-zero on any oversell.

```bash
DROP_RATE_LIMIT_ENABLED=false DROP_QUEUE_BATCH_SIZE=25 DROP_QUEUE_INTERVAL_SECONDS=0.5 \
DROP_QUEUE_MAX_ACTIVE=50 DROP_QUEUE_ADMIT_WINDOW_SECONDS=30 \
  uvicorn app.main:app --port 8000 --timeout-keep-alive 30
python scripts/loadtest.py --users 300
```

Recorded run (300 shoppers, 10 units, SQLite, one uvicorn worker, load generator on the same machine):

| Result | |
| --- | --- |
| Paid / sold out / errors | 10 / 290 / 0 (all 300 accounted for) |
| Oversold | **0**; audit `available + held + sold == total` passed |
| Outbox backlog after the run | 0 |
| Queue join latency | p50 3.9 s, p95 7.4 s |
| Reserve latency | p50 0.6 s, p95 4.3 s |

A separate 100-shopper simultaneous drop also finished with 0 oversold units and 0 errors.

The correctness result is the point. The latencies above are **not** a performance claim: 300 simultaneous requests against SQLite's single writer, with the load generator competing for the same CPU, is a worst case.

An early run dropped a few client connections under saturation (a keep-alive race, no server errors). The fix was `--timeout-keep-alive 30` plus client-side retries, which are safe because every write is idempotent.

## Design notes

- **Conditional `UPDATE` instead of `SELECT ... FOR UPDATE`.** The row lock is held for one statement instead of across a round trip, and no application-level locking is needed.
- **Hold stock at reservation, not at payment.** Otherwise people pay for items that no longer exist. The TTL and sweeper bound the cost of abandoned holds.
- **Why a waiting room?** It turns a thundering herd on the hot inventory row into cheap queue inserts, and gives a fair, explainable order.
- **Outbox, not "publish after commit".** Publishing after commit can lose the event on a crash; publishing before can announce something that rolls back. One transaction removes both.
- **Rate limiter is a seam.** `ratelimit.RateLimiter` is a two-line protocol. The default is an in-process token bucket; assign a Redis-backed limiter to `user_limiter` / `ip_limiter` to share limits across workers.

## Limitations (known, deliberate)

- **Scaling out:** the admitter, sweeper and outbox relay run inside the API process, and the default rate limiter is per-process, so run one worker. The queue and reservation SQL is concurrency-safe by construction, but multi-worker needs background jobs split into their own process and a shared limiter. `SKIP LOCKED` on the outbox would remove duplicated relay work.
- **Free-tier hosting:** Render's free instances sleep when idle and Neon scales to zero, so expect a cold start on the first request.
- **Load-test latencies** were measured on SQLite, not on the Neon deployment.
- **Auth is minimal:** no refresh tokens or password reset. Change `DROP_JWT_SECRET`, `DROP_WEBHOOK_SECRET` and `DROP_ADMIN_PASSWORD` before deploying. The dashboard uses one shared operator account, and its token lives only in page memory.
- Prometheus is scraped, but no Grafana dashboard ships; `/dashboard` covers live viewing.

## Project layout

```
app/
  main.py            app wiring, background loops (sweeper, admitter, outbox relay)
  models.py          all tables, including the constraints that enforce the guarantees
  services/          inventory (reservations), queue, orders (state machine + webhooks), outbox
  routers/           HTTP layer: auth, drops, queue, reservations, orders, stats
  security.py        hashing + JWT + OTP      ratelimit.py   token bucket + dependency
frontend/            Angular app (build output: frontend/dist/nova/browser)
  src/app/core/        ShopService (state), ApiService (fetch + auth), ToastService, models
  src/app/shop/        stage (slider), auth, queue, cart, hold, grid components
  src/app/dashboard/   operator login and live stats
  src/app/shared/      animation triggers, animated number, toast
  src/styles.css       shared design tokens and styles
scripts/loadtest.py  drop simulator and auditor
tests/               88 tests: concurrency, queue, payments, outbox, auth, rate limiting
Dockerfile           multi-stage build (Angular, then API)
.github/workflows/   CI: tests plus oversell-gated load test
```
