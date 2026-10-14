"""State that every app process must agree on: rate limits, the map-service cache, the
map-search throttle and "run this job once every N seconds".

With REDIS_URL set, it lives in Redis, so any number of processes and servers share it.
Without it, it lives in this process's memory, which is only correct for a single process
(gunicorn.conf.py runs one worker in that case).

Every operation is O(1): no request ever scans all stored keys.
"""
import json
import os
import threading
import time

REDIS_URL = (os.environ.get("REDIS_URL") or "").strip()
_PREFIX = "campusgo:"

_redis = None
_lock = threading.Lock()


class _RateStore(dict):
    """key -> [window_start, count, window]. clear() (used by tests) also resets Redis counters."""

    def clear(self):
        super().clear()
        if _redis is not None:
            _delete_prefix("rl:")


RATE_LIMIT_STORE = _RateStore()
_cache = {}           # key -> (expires_at, value)
_flags = {}           # key -> expires_at
_last_cleanup = [0.0]
CLEANUP_EVERY = 60.0
MAX_MEMORY_KEYS = 200_000


def use_redis_client(client):
    """Lets tests (or a custom setup) supply the Redis client."""
    global _redis
    _redis = client


def _client():
    global _redis
    if _redis is None and REDIS_URL:
        import redis
        _redis = redis.Redis.from_url(REDIS_URL, socket_timeout=2, socket_connect_timeout=2,
                                      health_check_interval=30, decode_responses=True)
    return _redis


def backend_name():
    return "redis" if _client() is not None else "memory"


def _cleanup(now):
    """Drops expired entries at most once a minute, so no single request pays for a scan."""
    if now - _last_cleanup[0] < CLEANUP_EVERY and len(RATE_LIMIT_STORE) + len(_cache) < MAX_MEMORY_KEYS:
        return
    _last_cleanup[0] = now
    for store, expired in ((RATE_LIMIT_STORE, lambda v: now - v[0] > v[2]),
                           (_cache, lambda v: v[0] < now),
                           (_flags, lambda v: v < now)):
        for k in [k for k, v in store.items() if expired(v)]:
            store.pop(k, None)


def hit(key, limit, window_seconds):
    """Counts one use of `key`. Returns False when the limit for the current window is
    already used up. The window starts at the first use and lasts window_seconds."""
    r = _client()
    if r is not None:
        k = _PREFIX + "rl:" + key
        pipe = r.pipeline()
        pipe.set(k, 0, ex=int(window_seconds), nx=True)
        pipe.incr(k)
        count = pipe.execute()[1]
        return count <= limit
    now = time.time()
    with _lock:
        _cleanup(now)
        entry = RATE_LIMIT_STORE.get(key)
        if entry is None or now - entry[0] >= window_seconds:
            entry = [now, 0, window_seconds]
            RATE_LIMIT_STORE[key] = entry
        if entry[1] >= limit:
            return False
        entry[1] += 1
        return True


def cache_get(key):
    r = _client()
    if r is not None:
        raw = r.get(_PREFIX + "c:" + key)
        return json.loads(raw) if raw is not None else None
    hit_ = _cache.get(key)
    if hit_ and hit_[0] > time.time():
        return hit_[1]
    return None


def cache_put(key, value, ttl_seconds):
    r = _client()
    if r is not None:
        r.set(_PREFIX + "c:" + key, json.dumps(value), ex=int(ttl_seconds))
        return
    now = time.time()
    with _lock:
        _cleanup(now)
        _cache[key] = (now + ttl_seconds, value)


def acquire(key, ttl_seconds):
    """True for the first caller in each ttl window, False for everyone else, across all
    processes. Used for the 1-request-per-second map-search rule and the cleanup job."""
    r = _client()
    if r is not None:
        return bool(r.set(_PREFIX + "f:" + key, "1", px=max(1, int(ttl_seconds * 1000)), nx=True))
    now = time.time()
    with _lock:
        if _flags.get(key, 0) > now:
            return False
        _flags[key] = now + ttl_seconds
        return True


def _delete_prefix(kind):
    r = _client()
    keys = list(r.scan_iter(match=_PREFIX + kind + "*", count=1000))
    if keys:
        r.delete(*keys)


def reset_memory():
    """Clears all limits, cache entries and flags (tests)."""
    with _lock:
        RATE_LIMIT_STORE.clear()
        _cache.clear()
        _flags.clear()
    if _client() is not None:
        _delete_prefix("")
