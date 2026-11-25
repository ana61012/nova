# nova

A limited-drop commerce backend (small-run fashion releases) built for the moment thousands of people try to buy a few hundred items at once. The goal is simple to state and hard to get right: **never oversell, never double-charge, stay fair under load.**

**Stack:** FastAPI (async) · SQLAlchemy 2 · PostgreSQL (SQLite for zero-setup local runs) · JWT auth · Server-Sent Events · Prometheus · Angular 19 (standalone components, signals, animations) · Razorpay · Docker

## Run it

```bash
(cd frontend && npm install && npm run build)   # builds the Angular app into frontend/dist
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

uvicorn app.main:app --reload --timeout-keep-alive 30
# shop:      http://localhost:8000
# dashboard: http://localhost:8000/dashboard   (operator login: admin / admin-dev-change-me)
# API docs:  http://localhost:8000/docs
# metrics:   http://localhost:8000/metrics

pytest -q                       # 73 tests
docker compose up --build       # Postgres + API + Prometheus
```

Every setting is an environment variable with a `DROP_` prefix (or a `.env` file); see `app/config.py`. To put it online, see [Deploy](#deploy).

Requires Python 3.12 and Node 20+. For live-reload while working on the UI, keep `uvicorn` running and start `cd frontend && npm start`, then open http://localhost:4200 (it proxies `/api` to port 8000).

## Deploy

One container serves the API and the built Angular app, so the whole thing deploys as a single web service plus a Postgres database. These steps use **Render** (web service, Docker) and **Neon** (free Postgres); any host that runs one long-lived container works.

> **Run exactly one instance.** The waiting-room admitter, stock sweeper and outbox relay are background loops inside the API process (see [Limitations](#limitations-known-deliberate)). Do not scale out.

### 1. Database (Neon)

Create a Neon project and copy the **direct** connection string (not the `-pooler` host). asyncpg does not understand libpq options, so rewrite it:

```
postgresql://USER:PASS@HOST/DB?sslmode=require&channel_binding=require      # what Neon gives you
postgresql+asyncpg://USER:PASS@HOST/DB?ssl=require                           # what DROP_DATABASE_URL needs
```

Tables are created on first boot and the four demo products are seeded (edit `SEED` in `app/main.py` before the first launch to change them).

### 2. Web service (Render)

1. Push the repo to GitHub.
2. Render: **New, Web Service**, pick the repo, **Language: Docker**, set the **Health Check Path** to `/health`.
3. Set the environment variables below, then deploy. The first build compiles the Angular app and takes a few minutes.

| Variable | Value |
|---|---|
| `PORT` | `8000` (the Dockerfile binds this port) |
| `FORWARDED_ALLOW_IPS` | `*` (so per-IP rate limits see real client IPs, not the proxy's) |
| `DROP_DATABASE_URL` | the `postgresql+asyncpg://...?ssl=require` URL from step 1 |
| `DROP_DEV_MODE` | `false` for a real store, `true` for a demo with simulated payments |
| `DROP_JWT_SECRET` | random, e.g. `python -c "import secrets; print(secrets.token_hex(32))"` |
| `DROP_WEBHOOK_SECRET` | random (still required when dev mode is off, even if you only use Razorpay) |
| `DROP_ADMIN_PASSWORD` | random; this is the `/dashboard` login for `DROP_ADMIN_USERNAME` (default `admin`) |
| `DROP_EMAIL_BACKEND` | `brevo` (real email over HTTPS, works on Render's free tier), `smtp`, or `console` (codes are printed to the service log) |
| `DROP_BREVO_API_KEY`, `DROP_BREVO_SENDER_EMAIL`, `DROP_BREVO_SENDER_NAME` | when using `brevo`; the API key needs only the **Transactional email** permission, and the sender email must be verified in Brevo |
| `DROP_SMTP_HOST`, `DROP_SMTP_PORT`, `DROP_SMTP_USER`, `DROP_SMTP_PASSWORD`, `DROP_SMTP_FROM` | when using `smtp` |
| `DROP_RAZORPAY_KEY_ID`, `DROP_RAZORPAY_KEY_SECRET`, `DROP_RAZORPAY_WEBHOOK_SECRET` | when taking real payments |

Your local `.env` is gitignored and is **not** uploaded; every variable has to be entered in Render. With `DROP_DEV_MODE=false` the app refuses to boot while the JWT secret, webhook secret or admin password still has its built-in default, so a boot failure naming those three means they are missing or misspelled (the `DROP_` prefix is required).

### 3. Razorpay

1. Generate an API key pair (use **Test Mode** first). The key id is `DROP_RAZORPAY_KEY_ID`; the secret, shown once, is `DROP_RAZORPAY_KEY_SECRET`.
2. Dashboard, **Settings, Webhooks, Add New Webhook**:
   - URL: `https://YOUR-APP.onrender.com/webhooks/razorpay`
   - Secret: a random string you invent. Put the **same** string in `DROP_RAZORPAY_WEBHOOK_SECRET`. It is not `DROP_WEBHOOK_SECRET` (that one signs the dev simulator) and not the API key secret.
   - Events: `payment.captured`, `order.paid`, `payment.failed`, `refund.processed`
3. Make a test purchase and check the webhook delivery log: `200` means the secret matches, `401` means it does not. With no webhook secret configured the server rejects every webhook.

When Razorpay keys are set, the dev payment simulator is disabled regardless of `DROP_DEV_MODE`.

### 4. Hosting on a free tier

Render's free plan works for demos, with three caveats:

- **It sleeps after 15 minutes without traffic** and takes about a minute to wake, and the background loops stop while it sleeps (nobody is admitted from the queue). Point a free uptime monitor (UptimeRobot, cron-job.org) at `/health` every 5 to 10 minutes.
- **Outbound SMTP ports 25, 465 and 587 are blocked** on free web services, so Gmail SMTP will not work. Use `DROP_EMAIL_BACKEND=brevo`, which sends over HTTPS, or `DROP_EMAIL_BACKEND=console` and read codes from the logs.
- **Render's own free Postgres expires after 30 days** and has no backups, which is why the steps above use Neon.

### 5. Verify

- `/` loads the shop; sign up and receive a code.
- `/dashboard` accepts `DROP_ADMIN_USERNAME` / `DROP_ADMIN_PASSWORD` and the inventory audit passes.
- Join the queue, reserve, check out with a Razorpay **test** card, and confirm the order reaches `paid`.

### Self-hosting with Docker Compose

`docker-compose.yml` is a local development setup (Postgres on a published port with a throwaway password, no volume, no HTTPS). For a VPS, use a private database network, a named volume for Postgres data, a reverse proxy such as Caddy for HTTPS (with response buffering off for `/api/queue/stream`), `--proxy-headers --forwarded-allow-ips="*"` on uvicorn, and keep `/metrics` off the public internet.

## Frontend

A single Angular 19 app (standalone components, signals, TypeScript strict mode) serves both the shop and the operator dashboard. FastAPI serves the built bundle from `/static/` and returns it for `/` and `/dashboard`; the Angular router picks the page. `frontend/src/styles.css` holds the shared design tokens (colours, type, spacing).

- **Shop:** collection slider with layered parallax and swipe/keyboard control, email-code or password sign-in, live queue position over Server-Sent Events, client-side wish-list cart, reservation hold with a countdown, simulated or Razorpay checkout.
- **Dashboard:** operator login, live tiles (queue, orders per second, revenue, outbox backlog), per-product stock bars and the oversell audit, refreshed every second.
- **State:** one `ShopService` holds all shop state as signals. Sessions and tokens live in memory only, so a reload or leaving the page signs you out.
- **Motion:** route transitions, staggered product cards, panels that unfold and fold away, animated list rows, number roll and count-up, toast and error feedback. Angular animation triggers handle enter/leave; plain CSS handles the rest. All of it switches off under `prefers-reduced-motion`.

## How a purchase flows

```
 signup/login ──► join queue ──► wait (live position over SSE)
                                      │ admitter lets a few in at a time
                                      ▼
                              admitted: reserve stock   ◄── Idempotency-Key, one per customer
                                      │ 2 min hold (TTL)
                                      ▼
                               create order (pending_payment)
                                      │
                       payment gateway ──signed webhook──► verify, dedupe, apply
                                      ▼
                  paid ─────► outbox event ─► relay ─► email + analytics consumers
```

## The guarantees, and what enforces each

| Problem | Solution | Proven by |
|---|---|---|
| Two buyers, one unit left | Single conditional `UPDATE ... WHERE available >= qty`, atomic in the DB; `CHECK (available >= 0)` as backstop | `test_no_oversell_under_concurrency` (200 buyers, 10 units) |
| Double-click / network retry | `Idempotency-Key` header + `UNIQUE(user_id, idempotency_key)` | `test_simultaneous_double_click...` |
| One customer hoarding | Partial unique index on `(user_id, product_id)` for live reservations, so even concurrent requests with different keys cannot both win | `test_one_per_customer_even_with_different_keys` |
| Abandoned checkouts hoard stock | Reservation TTL and a background sweeper | `test_expired_holds_return_stock_exactly_once` |
| Sweeper and cancel race | The status flip is itself a conditional `UPDATE`; only the caller with `rowcount == 1` returns the stock | same test, 3 concurrent sweepers |
| Stampede on the inventory | Virtual waiting room: FIFO queue, batch admission, capped concurrency, no admission means no reservation | `tests/test_queue.py` |
| Jumping the line | Queue position is the autoincrement id; missing your window sends you to the back | `test_expired_user_rejoins_at_the_back` |
| Duplicate webhook | Event id is a primary key; replays are no-ops | `test_duplicate_webhook...`, `test_same_event_delivered_concurrently...` |
| Forged webhook | HMAC-SHA256 signature check | `test_bad_signature_is_rejected` |
| Payment arrives after the hold expired | Try to re-take stock: paid if still available, **refunded** if someone else bought it | `test_late_payment_*` |
| Refund event before the payment event | Parked as `ignored_out_of_order` | `test_refund_event_before_payment_is_ignored` |
| Order saved but "paid" event lost (dual write) | Transactional outbox: state change and event commit together | `tests/test_outbox.py` |
| Consumer crashes mid-delivery | At-least-once delivery + `processed_events` makes consumers idempotent | `test_failing_consumer_is_retried...` |
| Bots and spam | Per-IP and per-user token buckets, `429` with `Retry-After` | `test_rate_limit_returns_429...` |

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
|---|---|---|
| POST | `/api/auth/request-code`, `/api/auth/verify-code` | Emailed one-time code sign-in, returns a JWT |
| POST | `/api/auth/signup`, `/api/auth/login` | Password sign-in, returns a JWT |
| GET | `/api/drops/current` | Products with live stock |
| POST | `/api/queue/join` · `/leave` | Join or leave the waiting room |
| GET | `/api/queue/status` | Your state and position |
| GET | `/api/queue/stream?token=` | SSE live updates (EventSource cannot send headers, so the token is a query param) |
| POST | `/api/reservations` | Header `Idempotency-Key`. 201 created, 200 replay, 403 not admitted, 409 sold out or already owned, 429 rate limited |
| DELETE | `/api/reservations/{id}` | Release a hold |
| POST | `/api/orders` | Create an order from a live reservation (idempotent per reservation) |
| GET | `/api/orders/{id}` | Your order |
| POST | `/webhooks/payments` | Simulated-gateway entry point, HMAC-signed with `DROP_WEBHOOK_SECRET` |
| GET | `/api/payments/config` | Which provider is active (`razorpay` or `simulated`) |
| POST | `/api/payments/razorpay/order` · `/verify` | Create a Razorpay order for checkout; verify the checkout signature |
| POST | `/webhooks/razorpay` | Razorpay entry point, HMAC-signed with `DROP_RAZORPAY_WEBHOOK_SECRET` |
| POST | `/api/dev/payments/{order_id}/simulate?outcome=success\|fail\|refund&duplicate=` | Fake gateway for demos (disabled when `DROP_DEV_MODE=false` or Razorpay keys are set) |
| POST | `/api/admin/login` | `{username, password}` -> short-lived admin token (dashboard only; separate from the shop's emailed-code login) |
| GET | `/api/stats` | **Admin token required.** Dashboard data plus the **inventory audit** (`available + held + sold == total`) |
| GET | `/metrics` | Prometheus |

## Load test

`scripts/loadtest.py` runs the whole funnel (signup, queue, reserve, order, pay) with N shoppers released at the same instant, then audits the server. It exits non-zero on any oversell, and it is wired into CI.

```bash
DROP_RATE_LIMIT_ENABLED=false DROP_QUEUE_BATCH_SIZE=25 DROP_QUEUE_INTERVAL_SECONDS=0.5 \
DROP_QUEUE_MAX_ACTIVE=50 DROP_QUEUE_ADMIT_WINDOW_SECONDS=30 \
  uvicorn app.main:app --port 8000 --timeout-keep-alive 30
python scripts/loadtest.py --users 300
```

One recorded run (300 shoppers, 10 units, SQLite, one uvicorn worker, load generator on the same machine):

| Result | |
|---|---|
| Paid / sold out / errors | 10 / 290 / 0 (all 300 accounted for) |
| Oversold | **0**; audit `available + held + sold == total` passed |
| Outbox backlog after the run | 0 |
| Queue join latency | p50 3.9 s, p95 7.4 s |
| Reserve latency | p50 0.6 s, p95 4.3 s |

The correctness result is the point. The latencies are **not** a performance claim: 300 simultaneous requests against SQLite's single writer, with the load generator competing for the same CPU, is a worst case. They are not measured on Postgres.

An early run of this test dropped a few client connections under saturation (a keep-alive race, no server errors). The fix was `--timeout-keep-alive 30` plus client-side retries, which are safe because every write is idempotent. That is the idempotency design doing its job.

## Design notes

- **Conditional `UPDATE` instead of `SELECT ... FOR UPDATE`.** The row lock is held for one statement instead of across a round trip, and no application-level locking is needed. Comparing it against `SKIP LOCKED` and a Redis Lua decrement under real Postgres load would be a good follow-up; it is not done here.
- **Why hold stock at reservation, not at payment?** Otherwise people pay for items that no longer exist. The TTL and sweeper bound the cost of abandoned holds.
- **Why a waiting room?** It turns a thundering herd on the hot inventory row into cheap queue inserts, and gives a fair, explainable order.
- **Outbox, not "publish after commit".** Publishing after commit can lose the event on a crash; publishing before can announce something that rolls back. One transaction removes both.
- **Rate limiter is a seam.** `ratelimit.RateLimiter` is a two-line protocol. The default is an in-process token bucket; assign your own Redis-backed limiter to `user_limiter` / `ip_limiter` to share limits across workers.

## Limitations (known, deliberate)

- **Scaling out:** the admitter, sweeper and outbox relay run inside the API process, and the default rate limiter is per-process, so run one worker. The queue and reservation SQL is concurrency-safe by construction (conditional updates, unique indexes), but multi-worker needs background jobs split into their own process and a shared limiter. The outbox relay already tolerates overlap (`processed_events`); `SKIP LOCKED` would remove the duplicated work.
- **Payments:** Razorpay checkout and webhooks are implemented (signature check, dedupe, state machine). Without Razorpay keys the gateway is a simulated dev endpoint. Start with Razorpay **test** keys.
- **Tests and the recorded load test run on SQLite.** The app is deployable on Postgres (Neon via asyncpg, see [Deploy](#deploy)), but the concurrency test suite and the latency numbers above have not been run against Postgres. Run a staging drop before a real one.
- **Free-tier hosting** sleeps when idle, which pauses the queue admitter and sweeper; see [Deploy](#deploy).
- **No migrations tool:** tables are created on boot and only missing nullable columns are added in place.
- **Auth is minimal:** no email verification, refresh tokens or password reset. scrypt parameters are lowered for speed; raise them for real use. Set your own `DROP_JWT_SECRET`, `DROP_WEBHOOK_SECRET` and `DROP_ADMIN_PASSWORD` before deploying (with `DROP_DEV_MODE=false` the server refuses to boot while any still has its default). Never commit `.env`. The dashboard login is one shared operator account (`DROP_ADMIN_USERNAME` / `DROP_ADMIN_PASSWORD`, token lifetime `DROP_ADMIN_JWT_TTL_SECONDS`). Its token is kept only in page memory, so reloading, re-opening the page or restarting the server always returns to the login screen.
- Prometheus is scraped, but no Grafana dashboard ships; `/dashboard` covers live viewing.

## Project layout

```
app/
  main.py            app wiring, background loops (sweeper, admitter, outbox relay)
  models.py          all tables, including the constraints that enforce the guarantees
  services/          inventory (reservations), queue, orders (state machine + webhooks), outbox
  routers/           HTTP layer: auth, drops, queue, reservations, orders, stats
  security.py        scrypt + JWT          ratelimit.py   token bucket + dependency
frontend/            Angular app (build output: frontend/dist/nova/browser)
  src/app/core/        ShopService (state), ApiService (fetch + auth), ToastService, models
  src/app/shop/        stage (slider), auth, queue, cart, hold, grid components
  src/app/dashboard/   operator login and live stats
  src/app/shared/      animation triggers, animated number, toast
  src/styles.css       shared design tokens and styles
scripts/loadtest.py  drop simulator and auditor
tests/               73 tests: concurrency, queue, payments, outbox, auth, rate limiting
```