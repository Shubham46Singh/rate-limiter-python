# API Gateway with Rate Limiting

**Language:** Python (FastAPI) &nbsp;|&nbsp; **Needs:** Postgres + Redis

This is a **starter**. The application already works. Your job is everything
that gets it building, tested and running in CI.

---

## You do not need Python installed

You will build this into a container, and the container brings its own
Python 3.12. You are not being asked to extend the app — you are being asked
to ship it.

---

## 1. What this app needs

| | |
|---|---|
| **Runtime** | Python 3.12 |
| **Install dependencies** | `pip install -r requirements.txt` |
| **Start the app** | `uvicorn app.main:app --host 0.0.0.0 --port 8080` |
| **Listens on** | port 8080, bound to `0.0.0.0` |
| **Environment variables** | `DATABASE_URL`, `REDIS_URL` |
| **Needs running first** | Postgres, Redis, and the migrations applied |

### What it does

Sits in front of a small API. Every client gets an API key on a tier - free, basic or pro - and each tier has its own requests-per-minute allowance. Go over it and you get a 429 with a Retry-After. Both a fixed-window and a sliding-window limiter are implemented, and you choose between them per request, so the difference is something you can measure rather than argue about.

### Endpoints

```
GET  /health
GET  /tiers                          the configured allowances
POST /keys                           {"owner":"asha","tier":"free"}
GET  /keys/{api_key}                 tier, and what both limiters currently think
PUT  /keys/{api_key}/tier            {"tier":"pro"} - also resets the counters
GET  /api/{resource}                 the gated call. Header: X-API-Key
                                     ?algorithm=fixed|sliding   (default sliding)
                                     resources: time, quote, echo
GET  /usage/{api_key}                counts by status, and the last ten calls
GET  /explain/boundary?limit=5       the fixed-window flaw, as numbers

Try it:  for i in $(seq 1 8); do curl -s -o /dev/null -w "%{http_code} " \
           -H 'X-API-Key: k_demo_free' localhost:8080/api/time; done; echo
```

`/health` reports Postgres and Redis **separately**. If it says
`postgres: false` the app started fine and your compose wiring is wrong —
do not go looking in the application code.

### Migrations

`migrations/` holds `.sql` files applied **in filename order** before the app
starts. They create the tables and insert sample data. A container running
`psql` over them in order is enough; you do not need a migration tool.

---

## 2. What you must write

| File | What it has to do |
|---|---|
| `Dockerfile` | Install dependencies **before** copying source, pin the base image, do not run as root. |
| `docker-compose.yml` | App + Postgres + Redis + a migration step, one `docker compose up`. |
| `.circleci/config.yml` | lint → unit tests → integration tests → secret scan → image build |
| Unit tests | For `app/buckets.py`. No database, no network. |
| Integration tests | Against a real Postgres and Redis as CircleCI service containers. |

Then push your image to **your own Docker Hub account**, tagged `:1.0`.

### When it works

```bash
docker compose up --build
curl localhost:8080/health
```

```json
{"status":"ok","postgres":true,"redis":true}
```

---

## Where the marks are

`app/buckets.py` is **pure logic** — plain functions over plain data, no
database and no HTTP. Start your tests there. Use pytest:
`pytest --cov=app --cov-report=term-missing`. Minimum 70%.

`boundary_burst(limit)` hands you the timestamps of an attack. Feed them to `fixed_window_check` and to `sliding_log_check` in the same test and assert that the two disagree - that one assertion is the whole point of this project.

## Why Redis is here

This is the one project where Redis is not a cache - it is the feature. The fixed-window limiter is a single INCR with an EXPIRE, issued in one pipeline so a counter can never be created without a TTL. The sliding window is a sorted set scored by timestamp: ZREMRANGEBYSCORE ages the old entries out, ZADD records this one, ZCARD counts what is left, all in one round trip. Doing either of these in Postgres would mean a write on every single request.

## The hard part

A sliding window is correct but harder than a fixed window. Fixed windows let someone send double the limit across a boundary.

Do not describe this - demonstrate it. `GET /explain/boundary` and the pure function `boundary_burst` give you the shape; write the unit test that proves a client sending 2 x limit requests inside a single 60-second span is fully allowed by `fixed_window_check` and partly refused by `sliding_log_check`. Then go the other way and say honestly what the sliding window costs: one sorted set per key that grows with every allowed request, a multi-command round trip instead of one INCR, and memory proportional to traffic rather than to the number of keys. Say which you would ship, and at what request rate you would change your mind. `sliding_counter_estimate` is the compromise most real gateways actually run - work out what it gets wrong.

Write your answer in your README. It is worth more marks than the feature.

---

## Getting unstuck

| Symptom | Almost always |
|---|---|
| `/health` says `postgres: false` | Wrong hostname. In compose the host is the **service name**, not `localhost`. |
| Page will not load, logs fine | No `ports:` mapping, or bound to `127.0.0.1` not `0.0.0.0`. |
| `relation "..." does not exist` | Migrations did not run, or the app started before they finished. |
| Build takes minutes each time | `COPY . .` is above your dependency install. |
| CI cannot reach the database | In CircleCI service containers the host **is** `localhost` — opposite of compose. |
