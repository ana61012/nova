# nova

A limited-drop commerce backend (small-run fashion releases) built for the moment thousands of people try to buy a few hundred items at once. The goal is simple to state and hard to get right: **never oversell, never double-charge, stay fair under load.**

**Stack:** FastAPI (async) · SQLAlchemy 2 · PostgreSQL (SQLite for zero-setup local runs) · JWT auth (password or emailed one-time code) · Razorpay (or a built-in simulated gateway) · Server-Sent Events · Prometheus · vanilla JS frontend

## Run it

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

uvicorn app.main:app --reload --timeout-keep-alive 30
# shop:      http://localhost:8000
# dashboard: http://localhost:8000/dashboard   (operator login: admin / admin-dev-change-me)
# API docs:  http://localhost:8000/docs
# metrics:   http://localhost:8000/metrics

pip install -r requirements-dev.txt
pytest -q                       # 88 tests
docker compose up --build       # Postgres + API + Prometheus (needs a .env file, can be empty)
```

Every setting is an environment variable with a `DROP_` prefix (or a `.env` file); see `app/config.py`. On first boot an empty database is seeded with one drop ("The Moonlit Edit") and four products.

By default the emailed sign-in code is **printed to the server log** (`DROP_EMAIL_BACKEND=console`). Use `smtp` (with the `DROP_SMTP_*` settings) to really send it.

## How a purchase flows

```
 sign up / log in ──► join queue ──► wait (live position over SSE)
 (password or emailed code)             │ admitter lets a few in at a time
                                        ▼
                         admitted: reserve a cart (1..N products, all or nothing)
                                        │ ◄── Idempotency-Key, per-customer quantity cap
                                        │ 2 min hold (TTL)
                                        ▼
                           create order (pending_payment)
                                        │
              Razorpay Checkout (or simulated gateway) ──signed webhook / signed
              checkout callback──► verify, dedupe, apply
                                        ▼
                  paid ─────► outbox event ─► relay ─► email + analytics consumers
```

## The guarantees, and what enforces each

| Problem | Solution | Proven by |
|---|---|---|
| Two buyers, one unit left | Single conditional `UPDATE ... WHERE available >= qty`, atomic in the DB; `CHECK (available >= 0)` as backstop | `test_no_oversell_under_concurrency` (200 buyers, 10 units) |
| Half a cart | A reservation is a cart of several lines, held all or nothing: if any line is sold out, nothing is taken | `test_cart_is_all_or_nothing_when_one_line_is_sold_out` |
| Deadlocks between overlapping carts | Lines are always processed in ascending product-id order, so every writer locks rows in the same order | `test_overlapping_carts_under_load_never_oversell_or_leak` |
| Double-click / network retry | `Idempotency-Key` header + `UNIQUE(user_id, idempotency_key)` | `test_simultaneous_double_click...` |
| One customer hoarding | Per-customer quota per product (`max_qty_per_product`, default 3) enforced by a conditional `UPDATE ... WHERE qty + :q <= :limit` on `customer_quota`, so the cap holds across carts, even when one person fires many at once with different keys | `test_cap_holds_when_one_person_fires_many_carts_at_once`, `test_customer_cap_holds_even_with_different_keys` |
| Abandoned checkouts hoard stock | Reservation TTL and a background sweeper; expiry returns every line and the quota | `test_expired_holds_return_stock_exactly_once`, `test_expiry_returns_every_line_and_the_quota` |
| Sweeper and cancel race | The status flip is itself a conditional `UPDATE`; only the caller with `rowcount == 1` returns the stock | same test, 3 concurrent sweepers |
| Stampede on the inventory | Virtual waiting room: FIFO queue, batch admission, capped concurrency, no admission means no reservation | `tests/test_queue.py` |
| Jumping the line | Queue position is the autoincrement id; missing your window sends you to the back | `test_expired_user_rejoins_at_the_back` |
| Duplicate webhook | Event id is a primary key; replays are no-ops | `test_duplicate_webhook...`, `test_same_event_delivered_concurrently...` |
| Forged webhook or checkout callback | HMAC-SHA256 signature check (simulated gateway webhook, Razorpay webhook, and Razorpay checkout signature) | `test_bad_signature_is_rejected`, `test_verify_rejects_forged_signature` |
| Paying for someone else's order, or the wrong Razorpay order | Orders are looked up by owner, and the Razorpay order id must match the one stored on the order | `test_cannot_pay_someone_elses_order`, `test_verify_rejects_a_payment_for_a_different_razorpay_order` |
| Client tampering with the price | The amount sent to Razorpay is computed server-side from the order | `test_create_order_uses_server_amount_and_is_reusable` |
| Payment arrives after the hold expired | Try to re-take the whole cart: paid if still available, **refunded** if someone else bought any of it | `test_late_payment_*` |
| Refund event before the payment event | Parked as `ignored_out_of_order` | `test_refund_event_before_payment_is_ignored` |
| Order saved but "paid" event lost (dual write) | Transactional outbox: state change and event commit together | `tests/test_outbox.py` |
| Consumer crashes mid-delivery | At-least-once delivery + `processed_events` makes consumers idempotent | `test_failing_consumer_is_retried...` |
| Code guessing / credential stuffing | One-time codes expire (10 min), die after 3 wrong guesses, have a resend cooldown and an hourly cap; one generic error for every failure; tight per-IP bucket on all sign-in endpoints | `tests/test_otp.py`, `tests/test_password_auth.py` |
| Bots and spam | Per-IP and per-user token buckets, `429` with `Retry-After` | `test_rate_limit_returns_429...` |

### Order state machine

```
pending_payment ──paid──► paid ──refund──► refunded
      │                                       ▲
      └─expired (hold lapsed)─► expired ──────┘ (late payment, item sold out)
                                   └──paid again (late payment, still available)
```

Transitions are conditional `UPDATE`s checked against an `ALLOWED` table, so an illegal move raises instead of corrupting state.

## Authentication

There are three separate sign-in paths:

- **Shop, password:** `signup` / `login` with email and password (scrypt hashed). Unknown address and wrong password give the same answer.
- **Shop, emailed code (passwordless):** `request-code` always answers `202`, whether or not the address is known. `verify-code` exchanges the 6-digit code for a JWT; the first successful verification creates the account.
- **Dashboard (operator):** one shared account (`DROP_ADMIN_USERNAME` / `DROP_ADMIN_PASSWORD`) that returns a short-lived admin token, accepted only by admin endpoints.

All three are covered by the same per-IP auth rate limit.

## Payments

- **Razorpay (real checkout).** Set `DROP_RAZORPAY_KEY_ID` and `DROP_RAZORPAY_KEY_SECRET` (plus `DROP_RAZORPAY_WEBHOOK_SECRET`, the secret you type in Razorpay Dashboard > Settings > Webhooks, pointing at `/webhooks/razorpay`). The page asks `/api/payments/config` which checkout to use. After Checkout succeeds the browser calls `/api/payments/razorpay/verify`; the webhook can also pay (and refund) an order by itself, and both paths share one idempotent state machine. Amounts are in INR paise.
- **Simulated gateway (default).** With no Razorpay keys, `/api/dev/payments/{order_id}/simulate` builds a signed event and feeds it through the same code path as the real webhook. It is disabled when `DROP_DEV_MODE=false` **or** when Razorpay is on, so the fake gateway can never mark a real order paid.

## API

| Method | Path | Notes |
|---|---|---|
| POST | `/api/auth/signup`, `/api/auth/login` | `{email, password}` (min 8 chars) -> `{token, user_id, email}` |
| POST | `/api/auth/request-code` | `{email}` -> 202, emails a 6-digit code. 429 with `Retry-After` during the resend cooldown |
| POST | `/api/auth/verify-code` | `{email, code}` -> JWT. 401 for any wrong, expired, used or locked code |
| GET | `/api/drops/current` | Products with live stock, plus the per-customer `limits` |
| POST | `/api/queue/join` · `/leave` | Join or leave the waiting room |
| GET | `/api/queue/status` | Your state and position |
| GET | `/api/queue/stream?token=` | SSE live updates (EventSource cannot send headers, so the token is a query param) |
| POST | `/api/reservations` | Header `Idempotency-Key`. Body `{items: [{product_id, qty}, ...]}`. 201 created, 200 replay, 403 not admitted, 404 unknown product, 409 sold out or per-customer limit reached, 422 invalid cart, 429 rate limited |
| DELETE | `/api/reservations/{id}` | Release a hold |
| POST | `/api/orders` | `{reservation_id}`. Create an order from a live reservation (idempotent per reservation) |
| GET | `/api/orders/{id}` | Your order, with its line items |
| GET | `/api/payments/config` | Which checkout the page should use (`razorpay` or `simulated`) |
| POST | `/api/payments/razorpay/order` | `{order_id}` -> data for Razorpay Checkout (404 if Razorpay is off) |
| POST | `/api/payments/razorpay/verify` | Checks the checkout signature and applies the payment |
| POST | `/webhooks/razorpay` | Razorpay -> us, signed with `X-Razorpay-Signature` |
| POST | `/webhooks/payments` | Generic gateway entry point, HMAC-signed with `X-Signature` |
| POST | `/api/dev/payments/{order_id}/simulate?outcome=success\|fail\|refund&duplicate=` | Fake gateway for demos (needs dev mode, off when Razorpay is on) |
| POST | `/api/admin/login` | `{username, password}` -> short-lived admin token (dashboard only) |
| GET | `/api/stats` | **Admin token required.** Dashboard data plus the **inventory audit** (`available + held + sold == total`) |
| GET | `/health` · `/metrics` | Liveness · Prometheus |

Cart rules: at most `DROP_MAX_CART_LINES` (5) different products per cart, at most `DROP_MAX_QTY_PER_PRODUCT` (3) of one product per customer across all their carts, and every item must come from the same drop. The same product listed twice in one cart is merged.

## Load test

`scripts/loadtest.py` runs the whole funnel (signup, queue, reserve a random cart, order, pay) with N shoppers released at the same instant, then audits every product on the server. It exits non-zero on any oversell, or if the units clients paid for differ from the units the server recorded as sold. It is wired into CI (100 shoppers).

```bash
DROP_EMAIL_BACKEND=memory DROP_RATE_LIMIT_ENABLED=false DROP_QUEUE_BATCH_SIZE=25 \
DROP_QUEUE_INTERVAL_SECONDS=0.5 DROP_QUEUE_MAX_ACTIVE=50 DROP_QUEUE_ADMIT_WINDOW_SECONDS=30 \
  uvicorn app.main:app --port 8000 --timeout-keep-alive 30
python scripts/loadtest.py --users 300
```

`DROP_EMAIL_BACKEND=memory` lets the load test read each emailed code from the dev mailbox endpoint, which exists only when dev mode and the in-memory mailer are both on. Run it against a fresh database so the stock numbers start clean.

One recorded run (300 shoppers, 4 products, SQLite, one uvicorn worker, load generator on the same machine):

| Result | |
|---|---|
| Paid carts / sold out / errors | 8 / 292 / 0 (all 300 accounted for) |
| Oversold | **0**; `available + held + sold == total` passed for every product |
| Paid-by-clients vs sold-on-server | Identical for every product |
| Outbox backlog after the run | 0 |
| Queue join latency | p50 1.4 s, p95 3.5 s |
| Reserve latency | p50 0.4 s, p95 4.4 s |

The correctness result is the point. The latencies are **not** a performance claim: 300 simultaneous requests against SQLite's single writer, with the load generator competing for the same CPU, is a worst case. They are not measured on Postgres.

Dropped client connections under saturation (a keep-alive race, no server errors) are handled by `--timeout-keep-alive 30` plus client-side retries, which are safe because every write is idempotent. That is the idempotency design doing its job.

## Design notes

- **Conditional `UPDATE` instead of `SELECT ... FOR UPDATE`.** The row lock is held for one statement instead of across a round trip, and no application-level locking is needed. Comparing it against `SKIP LOCKED` and a Redis Lua decrement under real Postgres load would be a good follow-up; it is not done here.
- **Why hold stock at reservation, not at payment?** Otherwise people pay for items that no longer exist. The TTL and sweeper bound the cost of abandoned holds.
- **Why a waiting room?** It turns a thundering herd on the hot inventory row into cheap queue inserts, and gives a fair, explainable order.
- **Why a quota table instead of a unique index?** Carts can hold several units of several products, so "one live reservation per product" no longer fits. A counter capped by a conditional `UPDATE` gives the same race-free guarantee for any quantity.
- **Outbox, not "publish after commit".** Publishing after commit can lose the event on a crash; publishing before can announce something that rolls back. One transaction removes both.
- **One state machine for every payment path.** Simulated webhooks, Razorpay webhooks and the browser's checkout callback all go through `orders.apply_event`, so they dedupe against each other by event id.
- **Rate limiter is a seam.** `ratelimit.RateLimiter` is a two-line protocol. The default is an in-process token bucket; assign your own Redis-backed limiter to `user_limiter` / `ip_limiter` to share limits across workers.

## Limitations (known, deliberate)

- **Scaling out:** the admitter, sweeper and outbox relay run inside the API process, and the default rate limiter is per-process, so run one worker. The queue and reservation SQL is concurrency-safe by construction (conditional updates, unique indexes), but multi-worker needs background jobs split into their own process and a shared limiter. The outbox relay already tolerates overlap (`processed_events`); `SKIP LOCKED` would remove the duplicated work.
- **Razorpay was verified in test mode only.** The signature checks, webhook translation and state transitions are covered by automated tests (against stubs), and the full checkout flow has been run by hand against Razorpay's test mode. It has not been run with live keys, so repeat the check with a small real payment before launch.
- **Tests run on SQLite.** The Postgres path (`docker-compose.yml`) is configured but was not exercised in development.
- **Auth is minimal:** no email verification for password accounts, refresh tokens or password reset (the emailed-code login is the recovery path). scrypt parameters are lowered for speed; raise them for real use. Change `DROP_JWT_SECRET`, `DROP_WEBHOOK_SECRET` and `DROP_ADMIN_PASSWORD` before deploying (with `DROP_DEV_MODE=false` the server refuses to boot while any still has its default). The dashboard login is one shared operator account, with token lifetime `DROP_ADMIN_JWT_TTL_SECONDS`. Its token is kept only in page memory, so reloading, re-opening the page or restarting the server always returns to the login screen.
- Prometheus is scraped, but no Grafana dashboard ships; `/dashboard` covers live viewing.

## Project layout

```
app/
  main.py            app wiring, seed data, background loops (sweeper, admitter, outbox relay)
  config.py          every DROP_* setting, plus the production-safety check
  models.py          all tables, including the constraints that enforce the guarantees
  services/          inventory (cart reservations, quota), queue, orders (state machine + webhooks),
                     outbox, otp, mailer, razorpay
  routers/           HTTP layer: auth, drops, queue, reservations, orders, payments, stats
  security.py        scrypt + JWT          ratelimit.py   token bucket + dependency
frontend/            index.html (shop), dashboard.html (live view), nova.css, clothes/ (product images)
scripts/loadtest.py  drop simulator and auditor
tests/               88 tests: concurrency, carts, queue, payments, Razorpay, outbox, auth, OTP, admin, rate limiting
.github/workflows/   CI: test suite + oversell smoke test
```
