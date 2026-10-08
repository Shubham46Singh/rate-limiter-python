"""Pure rate-limiting arithmetic. No database, no HTTP, no clock.

Three algorithms are implemented so you can test the difference between them
rather than take somebody's word for it:

  fixed window    cheap, one counter per key per window, and wrong at the
                  boundary - see `boundary_burst`
  sliding log     exact: keep every timestamp, count the ones inside the
                  trailing window. Correct, and the memory grows with traffic
  sliding counter the usual compromise: weight the previous window's count by
                  how much of it is still inside the trailing window

Every function takes `now` as an argument. Nothing here reads the clock, which
is the only reason any of it can be tested.

What is worth testing
---------------------
  * `window_start(t, 60)` snaps 960.0 and 1019.9 to the same window, and
    1020.0 to the next one
  * fixed window: the limit-th request is allowed, the limit+1-th is not
  * `boundary_burst(10)` returns 20 - double the limit inside one window's
    width - and the timestamps it hands back really are within 60 seconds of
    each other
  * feed those same timestamps to `sliding_log_allows` and the 11th is refused
  * sliding log drops timestamps that have aged out: 60 requests a minute
    apart are all allowed
  * sliding counter sits between the two: it allows more than the log but
    never the full double
  * token bucket: `refill` never exceeds capacity; a bucket idle for an hour
    holds `capacity`, not an hour's worth of tokens
  * `consume` on an empty bucket returns allowed=False and a positive
    `retry_after`, and leaves the token count unchanged
  * burst: a pro key can spend its whole burst at once, then is paced by the
    refill rate
  * `retry_after_fixed` at the very start of a window is the full window
  * TIERS: an unknown tier name raises rather than silently defaulting to the
    most generous one
"""

# requests per minute, and how big a burst the bucket will hold
TIERS = {
    "free":  {"per_minute": 5,    "burst": 5,    "label": "Free"},
    "basic": {"per_minute": 60,   "burst": 90,   "label": "Basic"},
    "pro":   {"per_minute": 600,  "burst": 1200, "label": "Pro"},
}
WINDOW = 60.0


class LimitError(ValueError):
    pass


def tier(name):
    """Look up a tier. Unknown names are an error, not a default."""
    key = str(name).strip().lower()
    if key not in TIERS:
        raise LimitError(f"unknown tier {name!r}")
    return dict(TIERS[key], name=key)


def rate_per_second(per_minute):
    if per_minute <= 0:
        raise LimitError("a limit must be positive")
    return float(per_minute) / WINDOW


# --------------------------------------------------------------- fixed window

def window_start(now, window=WINDOW):
    """The instant the window containing `now` began."""
    if window <= 0:
        raise LimitError("a window must be positive")
    return float(int(float(now) // window) * window)


def window_id(now, window=WINDOW):
    """A stable integer naming the window - this is the Redis key suffix."""
    if window <= 0:
        raise LimitError("a window must be positive")
    return int(float(now) // window)


def retry_after_fixed(now, window=WINDOW):
    """How long until the counter resets. At the start of a window that is
    the whole window."""
    return round(window_start(now, window) + window - float(now), 3)


def fixed_window_allows(count_so_far, limit):
    """`count_so_far` is the count BEFORE this request."""
    if limit < 0:
        raise LimitError("a limit cannot be negative")
    return count_so_far < limit


def fixed_window_check(timestamps, now, limit, window=WINDOW):
    """Replay a list of request times through a fixed-window limiter."""
    start = window_start(now, window)
    count = sum(1 for t in timestamps if start <= float(t) <= float(now))
    allowed = fixed_window_allows(count, limit)
    return {"algorithm": "fixed", "allowed": allowed, "used": count,
            "limit": limit, "remaining": max(0, limit - count - (1 if allowed else 0)),
            "retry_after": 0.0 if allowed else retry_after_fixed(now, window),
            "window_id": window_id(now, window)}


def boundary_burst(limit, window=WINDOW, boundary=None):
    """Demonstrate the flaw. Returns timestamps a client can use to get
    `2 * limit` requests past a fixed-window limiter inside one window's
    width: `limit` at the end of one window, `limit` at the start of the next.
    """
    if limit <= 0:
        raise LimitError("a limit must be positive")
    edge = float(window if boundary is None else boundary)
    late = [edge - 0.001 - (i * 0.0001) for i in range(limit)]
    early = [edge + 0.001 + (i * 0.0001) for i in range(limit)]
    return sorted(late) + sorted(early)


# ------------------------------------------------------------- sliding window

def trim(timestamps, now, window=WINDOW):
    """Drop everything that has aged out of the trailing window."""
    cutoff = float(now) - float(window)
    return [float(t) for t in timestamps if float(t) > cutoff]


def sliding_log_count(timestamps, now, window=WINDOW):
    return len(trim(timestamps, now, window))


def sliding_log_allows(timestamps, now, limit, window=WINDOW):
    return sliding_log_count(timestamps, now, window) < limit


def retry_after_sliding(timestamps, now, limit, window=WINDOW):
    """When the oldest request inside the window ages out, a slot opens."""
    live = sorted(trim(timestamps, now, window))
    if len(live) < limit:
        return 0.0
    oldest = live[len(live) - limit]
    return round(max(0.0, oldest + float(window) - float(now)), 3)


def sliding_log_check(timestamps, now, limit, window=WINDOW):
    live = trim(timestamps, now, window)
    allowed = len(live) < limit
    return {"algorithm": "sliding", "allowed": allowed, "used": len(live),
            "limit": limit,
            "remaining": max(0, limit - len(live) - (1 if allowed else 0)),
            "retry_after": 0.0 if allowed else
            retry_after_sliding(timestamps, now, limit, window)}


def sliding_counter_estimate(prev_count, current_count, now, window=WINDOW):
    """The cheap approximation: weight the previous window by the fraction of
    it still inside the trailing window."""
    elapsed = float(now) - window_start(now, window)
    overlap = max(0.0, 1.0 - (elapsed / float(window)))
    return round(prev_count * overlap + current_count, 4)


def sliding_counter_allows(prev_count, current_count, now, limit, window=WINDOW):
    return sliding_counter_estimate(prev_count, current_count, now, window) < limit


# --------------------------------------------------------------- token bucket

def refill(tokens, last_seen, now, per_minute, capacity):
    """Top the bucket up for the time that has passed. Never past capacity."""
    if capacity <= 0:
        raise LimitError("a bucket must hold at least one token")
    now = float(now)
    if now < float(last_seen):
        # Clock went backwards. Do not invent tokens, do not take any away.
        return round(min(float(tokens), float(capacity)), 6), now
    gained = (now - float(last_seen)) * rate_per_second(per_minute)
    return round(min(float(capacity), float(tokens) + gained), 6), now


def consume(tokens, last_seen, now, per_minute, capacity, cost=1.0):
    """Try to spend `cost` tokens.

    Returns (allowed, tokens, last_seen, retry_after). A refused request does
    not spend anything - that is what makes a 429 cheap.
    """
    if cost <= 0:
        raise LimitError("a request must cost something")
    tokens, last_seen = refill(tokens, last_seen, now, per_minute, capacity)
    if tokens >= cost:
        return True, round(tokens - cost, 6), last_seen, 0.0
    short = cost - tokens
    return False, tokens, last_seen, round(short / rate_per_second(per_minute), 3)


def headers(result):
    """The response headers a well-behaved gateway sends back."""
    out = {
        "X-RateLimit-Limit": str(result["limit"]),
        "X-RateLimit-Remaining": str(result["remaining"]),
        "X-RateLimit-Algorithm": result["algorithm"],
    }
    if not result["allowed"]:
        out["Retry-After"] = str(max(1, int(round(result["retry_after"]))))
    return out
