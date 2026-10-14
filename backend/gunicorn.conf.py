"""Gunicorn settings: `gunicorn --config backend/gunicorn.conf.py`.

Several worker processes need shared state: PostgreSQL (DATABASE_URL) for data and Redis
(REDIS_URL) for rate limits and caches. Without both, the app runs one worker, because
SQLite and in-memory limits only work inside a single process.
"""
import multiprocessing
import os

# Each worker would otherwise make up its own random key, and a login or SMS code issued by one
# worker would be rejected by the next.
if not os.environ.get("SECRET_KEY", "").strip():
    raise SystemExit("SECRET_KEY is not set. Generate one with: "
                     "python -c \"import secrets; print(secrets.token_hex(32))\"")

chdir = os.path.dirname(os.path.abspath(__file__))
wsgi_app = "app:app"
bind = f"0.0.0.0:{os.environ.get('PORT', '5000')}"

_shared_state = bool(os.environ.get("REDIS_URL", "").strip()) and \
    os.environ.get("DATABASE_URL", "").strip().startswith(("postgres://", "postgresql://"))
_default_workers = min(2 * multiprocessing.cpu_count() + 1, 9) if _shared_state else 1
workers = int(os.environ.get("WEB_CONCURRENCY") or _default_workers)
if workers > 1 and not _shared_state:
    print("WARNING: WEB_CONCURRENCY > 1 needs DATABASE_URL (PostgreSQL) and REDIS_URL; running 1 worker.")
    workers = 1

# Threads let one worker overlap requests that wait on the database or a map service.
worker_class = "gthread"
threads = int(os.environ.get("GUNICORN_THREADS", "8"))

timeout = 30            # a request stuck longer than this gets its worker restarted
graceful_timeout = 20
keepalive = 5           # reuse connections from the load balancer
max_requests = 5000     # recycle workers now and then so slow leaks can't build up
max_requests_jitter = 500
accesslog = None        # per-request logs cost CPU at peak; the platform's router logs requests
errorlog = "-"
loglevel = os.environ.get("LOG_LEVEL", "info")
