"""Chat quota: messages per user per fixed hourly window, counted in Memcached. The counter is the API's `count_in_window` (services/api/app/cache.py), copied because the agent is
another deployable; the key format is the same, `quota:<userId>:<yyyymmddhh>`.

The agent task runs in the private subnets with AppSG, which the unit's CacheSG admits on 11211, so it
reaches the same node as the API. Every agent task increments the same key with Memcached's atomic `incr`, so the
limit holds however many tasks run.

**Fails open.** Without a usable cache (CACHE_ENDPOINT unset, the node down) a message is allowed and the
quota is reported as unknown. Chat stays up through a cache outage, and the cost of one is still bounded per
message by the agent's token and tool-round limits (chat.py). A warning line names every message that went
through unmetered.
"""

import datetime
import functools
import math
import os
import ssl
import time

from contracts import logs
from pymemcache.client.base import PooledClient
from pymemcache.exceptions import MemcacheError

CACHE_ENDPOINT = os.environ.get("CACHE_ENDPOINT", "")
CACHE_TLS = os.environ.get("CACHE_TLS", "false").lower() == "true"
LIMIT = int(os.environ.get("CHAT_MESSAGES_PER_HOUR", "30"))
WARN_FRACTION = 0.8

CONNECT_TIMEOUT = 0.5
TIMEOUT = 0.2
BACKOFF_SECONDS = 10

_down_until = 0.0


@functools.cache
def client():
    if not CACHE_ENDPOINT:
        return None
    host, _, port = CACHE_ENDPOINT.rpartition(":")
    return PooledClient(
        (host, int(port)) if host else (CACHE_ENDPOINT, 11211),
        connect_timeout=CONNECT_TIMEOUT,
        timeout=TIMEOUT,
        no_delay=True,
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


def window(now=None):
    """(start, reset_at) of the fixed hourly window that contains now (UTC)."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    start = now.replace(minute=0, second=0, microsecond=0)
    return start, start + datetime.timedelta(hours=1)


def key(user_id, window_start):
    return f"quota:{user_id}:{window_start.strftime('%Y%m%d%H')}"


def status(used, reset_at, limit=LIMIT):
    """The quota as the client sees it. used=None: unknown (no cache). warning from 80% of the limit; blocked
    once the limit is used up, so the next message would be refused."""
    if used is None:
        return {"used": None, "limit": limit, "resetAt": None, "warning": False, "blocked": False}
    used = min(used, limit)
    return {
        "used": used,
        "limit": limit,
        "resetAt": reset_at.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "warning": used >= math.ceil(WARN_FRACTION * limit),
        "blocked": used >= limit,
    }


def peek(user_id, now=None):
    """The current window's count without adding to it (the panel's "18 of 30 messages this hour")."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    start, reset_at = window(now)
    c = _usable()
    if c is None:
        return status(None, reset_at)
    try:
        raw = c.get(key(user_id, start))
    except (MemcacheError, OSError) as error:
        _failed("get", error)
        return status(None, reset_at)
    try:
        return status(int(raw or 0), reset_at)
    except ValueError:
        return status(0, reset_at)


def count_in_window(user_id, amount, now=None):
    """Add amount to the user's counter for the current hour: (total, reset_at), or None without a usable cache.

    `add` creates the key at 0 with a TTL just past the window's end (a no-op if it exists), then `incr` adds
    atomically on the node. The reset time is the next hour boundary, computed here: Memcached can't report a
    key's remaining TTL.
    """
    c = _usable()
    if c is None:
        return None
    now = now or datetime.datetime.now(datetime.timezone.utc)
    start, reset_at = window(now)
    k = key(user_id, start)
    ttl = int((reset_at - now).total_seconds()) + 60
    try:
        for _ in range(2):  # incr misses only if the key was evicted between the two calls
            c.add(k, b"0", expire=ttl, noreply=False)
            total = c.incr(k, amount, noreply=False)
            if total is not None:
                return int(total), reset_at
    except (MemcacheError, OSError) as error:
        _failed("incr", error)
    return None


def take(user_id, now=None):
    """Count one message. (allowed, status). Over the limit: not allowed, and the status says when it resets."""
    counted = count_in_window(user_id, 1, now)
    if counted is None:
        logs.warning("quota unavailable, message not metered", userId=user_id)
        return True, status(None, None)
    total, reset_at = counted
    return total <= LIMIT, status(total, reset_at)
