import time
from uuid import uuid4

from fastapi import Body, FastAPI, Header, HTTPException, Response
from fastapi.responses import JSONResponse

from . import cache, db
from .buckets import (LimitError, TIERS, WINDOW, boundary_burst, headers,
                      retry_after_fixed, retry_after_sliding, tier, window_id)

app = FastAPI(title="api-gateway")

# What the gateway is actually protecting. In a real deployment these would be
# proxied upstream; here they are just something to put behind the limiter.
UPSTREAM = {
    "time": lambda: {"utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
    "quote": lambda: {"quote": "Make it work, make it right, make it fast."},
    "echo": lambda: {"ok": True},
}


@app.get("/health")
def health():
    out = {"status": "ok", "postgres": False, "redis": False}
    try:
        db.query("SELECT 1")
        out["postgres"] = True
    except Exception as e:
        out["pg_error"] = str(e)
    try:
        cache.client().ping()
        out["redis"] = True
    except Exception as e:
        out["redis_error"] = str(e)
    return out if out["postgres"] and out["redis"] else JSONResponse(out, status_code=503)


def _key_row(api_key):
    row = db.one("SELECT k.api_key, k.owner, k.tier, k.active, t.per_minute, t.burst"
                 " FROM api_keys k JOIN tiers t ON t.name=k.tier"
                 " WHERE k.api_key=%s", (api_key,))
    return row


def _fixed(api_key, limit, now):
    """One counter per key per window. INCR and EXPIRE together, so a counter
    can never be left without a TTL."""
    wid = window_id(now, WINDOW)
    key = f"rl:fixed:{api_key}:{wid}"
    pipe = cache.client().pipeline()
    pipe.incr(key)
    pipe.expire(key, int(WINDOW) * 2)
    used = pipe.execute()[0]
    allowed = used <= limit
    return {"algorithm": "fixed", "allowed": allowed, "used": used, "limit": limit,
            "remaining": max(0, limit - used),
            "retry_after": 0.0 if allowed else retry_after_fixed(now, WINDOW)}


def _sliding(api_key, limit, now):
    """A sorted set of request timestamps. Age the old ones out, add this one,
    count what is left - all in one MULTI so two requests cannot interleave."""
    key = f"rl:slide:{api_key}"
    member = f"{now:.6f}:{uuid4().hex[:8]}"
    pipe = cache.client().pipeline()
    pipe.zremrangebyscore(key, 0, now - WINDOW)
    pipe.zadd(key, {member: now})
    pipe.zcard(key)
    pipe.expire(key, int(WINDOW) * 2)
    used = pipe.execute()[2]
    allowed = used <= limit
    if not allowed:
        # A refused request must not count against the next one, or a client
        # that keeps retrying can never recover.
        cache.client().zrem(key, member)
        scores = cache.client().zrange(key, 0, -1, withscores=True)
        retry = retry_after_sliding([s for _, s in scores], now, limit, WINDOW)
    else:
        retry = 0.0
    return {"algorithm": "sliding", "allowed": allowed,
            "used": used if allowed else used - 1, "limit": limit,
            "remaining": max(0, limit - used), "retry_after": retry}


@app.get("/tiers")
def tiers():
    rows = db.query("SELECT name, per_minute, burst FROM tiers ORDER BY per_minute")
    return {"tiers": rows,
            "defaults_in_pure_logic": TIERS,
            "window_seconds": WINDOW}


@app.post("/keys", status_code=201)
def create_key(payload: dict = Body(...)):
    owner = str(payload.get("owner", "")).strip()

    if not owner:
        raise HTTPException(400, "owner is required")

    if len(owner) > 100:
        raise HTTPException(400, "owner must be 100 characters or fewer")
    try:
        want = tier(payload.get("tier", "free"))["name"]
    except LimitError as e:
        raise HTTPException(400, str(e))
    if not db.one("SELECT 1 FROM tiers WHERE name=%s", (want,)):
        raise HTTPException(400, f"tier {want!r} is not configured")
    api_key = "k_" + uuid4().hex[:24]
    db.query("INSERT INTO api_keys (api_key, owner, tier) VALUES (%s,%s,%s)",
             (api_key, owner, want), fetch=False)
    return {"api_key": api_key, "owner": owner, "tier": want,
            "per_minute": db.one("SELECT per_minute FROM tiers WHERE name=%s",
                                 (want,))["per_minute"]}


@app.get("/keys/{api_key}")
def show_key(api_key: str):
    row = _key_row(api_key)
    if not row:
        raise HTTPException(404, "no such api key")
    now = time.time()
    c = cache.client()
    fixed_used = c.get(f"rl:fixed:{api_key}:{window_id(now, WINDOW)}")
    return {"api_key": api_key, "owner": row["owner"], "tier": row["tier"],
            "active": row["active"], "per_minute": row["per_minute"],
            "burst": row["burst"],
            "fixed_window_used": int(fixed_used or 0),
            "sliding_window_used": c.zcount(f"rl:slide:{api_key}", now - WINDOW, now),
            "total_requests": db.one(
                "SELECT count(*) AS n FROM requests WHERE api_key=%s", (api_key,))["n"]}


@app.put("/keys/{api_key}/tier")
def set_tier(api_key: str, payload: dict = Body(...)):
    if not _key_row(api_key):
        raise HTTPException(404, "no such api key")
    try:
        want = tier(payload.get("tier"))["name"]
    except LimitError as e:
        raise HTTPException(400, str(e))
    db.query("UPDATE api_keys SET tier=%s WHERE api_key=%s", (want, api_key), fetch=False)
    cache.drop_prefix(f"rl:fixed:{api_key}")
    cache.drop(f"rl:slide:{api_key}")
    return {"api_key": api_key, "tier": want, "counters": "reset"}


@app.get("/api/{resource}")
def proxy(resource: str, response: Response,
          x_api_key: str = Header(default=""),
          algorithm: str = "sliding"):
    """The gated endpoint. Send X-API-Key; add ?algorithm=fixed to switch."""
    if resource not in UPSTREAM:
        raise HTTPException(404, f"no upstream called {resource!r}")
    if not x_api_key:
        raise HTTPException(400, "X-API-Key header is required")
    row = _key_row(x_api_key)
    if not row:
        raise HTTPException(404, "no such api key")
    if not row["active"]:
        raise HTTPException(403, "that key has been disabled")
    if algorithm not in ("fixed", "sliding"):
        raise HTTPException(400, "algorithm must be 'fixed' or 'sliding'")

    now = time.time()
    limit = int(row["per_minute"])
    verdict = (_fixed if algorithm == "fixed" else _sliding)(x_api_key, limit, now)
    status = 200 if verdict["allowed"] else 429
    db.query("INSERT INTO requests (api_key, resource, status, algorithm)"
             " VALUES (%s,%s,%s,%s)", (x_api_key, resource, status, algorithm),
             fetch=False)

    for k, v in headers(verdict).items():
        response.headers[k] = v
    if not verdict["allowed"]:
        return JSONResponse({"detail": "rate limit exceeded", **verdict},
                            status_code=429, headers=headers(verdict))
    return {"resource": resource, "data": UPSTREAM[resource](), **verdict}


@app.get("/usage/{api_key}")
def usage(api_key: str):
    if not _key_row(api_key):
        raise HTTPException(404, "no such api key")
    return {"api_key": api_key, "by_status": db.query(
        "SELECT status, algorithm, count(*) AS n FROM requests"
        " WHERE api_key=%s GROUP BY status, algorithm ORDER BY status", (api_key,)),
        "recent": db.query(
        "SELECT resource, status, algorithm, at FROM requests"
        " WHERE api_key=%s ORDER BY id DESC LIMIT 10", (api_key,))}


@app.get("/explain/boundary")
def explain_boundary(limit: int = 5):
    """Why a fixed window is not good enough, as data rather than prose."""
    if limit < 1 or limit > 1000:
        raise HTTPException(400, "limit must be between 1 and 1000")
    stamps = boundary_burst(limit)
    return {"limit_per_minute": limit,
            "requests_sent": len(stamps),
            "spread_seconds": round(stamps[-1] - stamps[0], 4),
            "fixed_window_allowed_all": len(stamps),
            "sliding_window_would_allow": limit,
            "note": "Both halves sit inside one 60s span, yet a fixed window"
                    " sees two different windows and lets all of them through."}
