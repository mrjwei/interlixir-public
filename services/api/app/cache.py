"""Cache-aside on ElastiCache for Memcached, and the hourly quota counters.

Only this service can reach the cache: it runs in the private subnets with AppSG, which the unit's
CacheSG admits on 11211. Lambdas are outside the VPC, so the pipeline's writes (stage changes,
new documents) are never invalidated here; the short TTLs bound how stale those reads get. The API's own
writes delete the keys they affect, in the same process, after the write has committed (store.py).

The cache is an optimisation, never a dependency. With CACHE_ENDPOINT unset, or the node unreachable, every
read goes to DynamoDB. After an error the cache is skipped for BACKOFF_SECONDS (a minimal circuit breaker),
so an outage costs one short timeout per task per window instead of one per request.

Traffic to the node is TLS when CACHE_TLS is set, but that is encryption, not access control. Values are JSON,
never pickle: Memcached has no authentication and anything in AppSG can reach the node, so a value read back
must not be able to run code. Only responses that passed the ownership check are stored,
under a key that starts with the caller's userId; a miss or a 404 is never cached.
"""

import datetime
import functools
import hashlib
import json
import os
import ssl
import time

from contracts import logs
from pymemcache.client.base import PooledClient
from pymemcache.exceptions import MemcacheError

# "host:port" of the cluster's configuration endpoint (or its one node). Unset = no cache.
CACHE_ENDPOINT = os.environ.get("CACHE_ENDPOINT", "")
# The node has in-transit encryption on (the console won't create it without), so the task sets this to "true".
# Off by default for the plain Memcached the real-server tests run in Docker.
CACHE_TLS = os.environ.get("CACHE_TLS", "false").lower() == "true"
HEADER = "X-App-Cache"  # not X-Cache: CloudFront sets that one on every response it serves

# G11. Memcached TTLs are whole seconds.
JOB_TTL = 5  # polled every 3 s while a job runs; the pipeline's stage changes can't invalidate it
LIST_TTL = 10  # project list and one project's contents; synthesis adds documents without invalidating
HISTORY_TTL = 30  # written only by the history writer, which can't invalidate

# Bounded. The node is in the same VPC and answers in well under a millisecond. The TLS handshake runs
# inside connect, so it gets more room than a request.
CONNECT_TIMEOUT = 0.5
TIMEOUT = 0.2
BACKOFF_SECONDS = 10
MAX_KEY_BYTES = 250  # Memcached's limit

_down_until = 0.0


@functools.cache
def client():
    """The pooled client (thread-safe: FastAPI runs sync routes on a thread pool), or None without an
    endpoint. Connections open lazily, so a down node doesn't stop the app from starting."""
    if not CACHE_ENDPOINT:
        return None
    host, _, port = CACHE_ENDPOINT.rpartition(":")
    return PooledClient(
        (host, int(port)) if host else (CACHE_ENDPOINT, 11211),
        connect_timeout=CONNECT_TIMEOUT,
        timeout=TIMEOUT,
        no_delay=True,
        # Verifies the node's certificate and that it names the endpoint (pymemcache passes the host as SNI).
        tls_context=ssl.create_default_context() if CACHE_TLS else None,
    )


def _usable():
    c = client()
    if c is None or time.monotonic() < _down_until:
        return None
    return c


def _failed(op, error):
    global _down_until
    _down_until = time.monotonic() + BACKOFF_SECONDS
    logs.warning("cache unavailable, skipping it", op=op, error=repr(error), backoffSeconds=BACKOFF_SECONDS)


# --- Keys ------------------------------------------------------------------------------------------------
# Every key starts with the caller's userId (from the verified token), so one user's request can never read
# another user's cached response. IDs match keys.ID_PATTERN, so keys have no spaces or control characters.


def _key(*parts):
    key = ":".join(parts)
    if len(key) <= MAX_KEY_BYTES:
        return key
    return f"{parts[0]}:sha256:{hashlib.sha256(key.encode()).hexdigest()}"


def job_key(user_id, job_id):
    return _key("job", user_id, job_id)


def projects_key(user_id):
    return _key("projects", user_id)


def project_key(user_id, project_id):
    return _key("project", user_id, project_id)


def history_key(user_id, limit, cursor):
    # The cursor is client-supplied and can be long, so it's hashed (history.py has already checked it's
    # this user's). A page is cached per (limit, cursor); the first page has no cursor.
    digest = hashlib.sha256((cursor or "").encode()).hexdigest()[:32]
    return _key("history", user_id, str(limit), digest)


def quota_key(user_id, window_start):
    return _key("quota", user_id, window_start.strftime("%Y%m%d%H"))


# --- Cache-aside --------------------------------------------------------------------------------------


def aside(response, route, key, ttl, load, bypass=False):
    """Return the cached value for key, or load() it and cache it for ttl seconds.

    load() returns the JSON-ready response body and raises (e.g. NotFound) for anything that mustn't be
    cached. bypass skips the read but still refreshes the entry: it's how the client's
    `Cache-Control: no-cache` is honoured; load tests use it for a no-cache baseline.
    Sets the X-App-Cache header (HIT, MISS, BYPASS, or OFF when there is no usable cache) and logs one EMF
    line, from which CloudWatch extracts CacheHit / CacheMiss per route and in total.
    """
    c = _usable()
    result = "off" if c is None else "bypass" if bypass else "miss"
    if c is not None and not bypass:
        try:
            raw = c.get(key)
        except (MemcacheError, OSError) as error:
            _failed("get", error)
            c, result = None, "off"
        else:
            if raw is not None:
                try:
                    value = json.loads(raw)
                except ValueError:
                    pass  # not ours, or truncated: treat as a miss and overwrite it
                else:
                    _record(response, route, "hit")
                    return value
    value = load()
    if c is not None:
        try:
            c.set(key, json.dumps(value, separators=(",", ":")).encode(), expire=ttl, noreply=True)
        except (MemcacheError, OSError) as error:
            _failed("set", error)
    _record(response, route, result)
    return value


def _record(response, route, result):
    response.headers[HEADER] = result.upper()
    hit = int(result == "hit")
    # bypass and off are reads from DynamoDB too, so they count as misses.
    logs.metric("INFO", "cache", {"CacheHit": (hit, "Count"), "CacheMiss": (1 - hit, "Count")}, {"Route": route},
                cache=result)


def invalidate(*keys):
    """Delete keys after a write committed, before the response goes back, so the caller's next read is a
    MISS. Waits for the replies (noreply=False): with noreply the next read, on another pooled connection,
    could reach the node first. A failure is logged and left to the TTL; it never fails the write."""
    c = _usable()
    if c is None:
        return
    try:
        c.delete_many(list(keys), noreply=False)
    except (MemcacheError, OSError) as error:
        _failed("delete", error)


# --- Quota counters (chat agent) -------------------------------------------------------------------------


def window(now=None):
    """(start, reset_at) of the fixed hourly window that contains now (UTC)."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    start = now.replace(minute=0, second=0, microsecond=0)
    return start, start + datetime.timedelta(hours=1)


def count_in_window(user_id, amount, now=None):
    """Add amount to the user's counter for the current hour: (total, reset_at), or None without a usable
    cache, so the caller decides whether to fail open or closed.

    `add` creates the key at 0 with a TTL just past the window's end (a no-op if it exists), then `incr`
    adds atomically on the node, so concurrent requests on any task can't lose an update. The reset time is
    the next hour boundary, computed here: Memcached can't report a key's remaining TTL.
    """
    c = _usable()
    if c is None:
        return None
    now = now or datetime.datetime.now(datetime.timezone.utc)
    start, reset_at = window(now)
    key = quota_key(user_id, start)
    ttl = int((reset_at - now).total_seconds()) + 60
    try:
        for _ in range(2):  # incr misses only if the key was evicted between the two calls
            c.add(key, b"0", expire=ttl, noreply=False)
            total = c.incr(key, amount, noreply=False)
            if total is not None:
                return int(total), reset_at
    except (MemcacheError, OSError) as error:
        _failed("incr", error)
    return None
