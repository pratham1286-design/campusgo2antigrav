import hashlib
import hmac
import json
import logging
import math
import os
import re
import gzip
import secrets
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from functools import lru_cache, wraps

from flask import Flask, Response, g, jsonify, request, send_from_directory
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix


def _load_dotenv():
    """Local runs read settings from the project's .env file (git-ignored). Real environment
    variables always win, so hosted deployments are unaffected."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, ".env")
    if os.environ.get("CAMPUSGO_NO_DOTENV") == "1" or not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            name, sep, value = line.strip().partition("=")
            if sep and name and not name.startswith("#"):
                os.environ.setdefault(name.strip(), value.strip().strip("\"'"))


_load_dotenv()

import geo  # noqa: E402
import payments  # noqa: E402
import shared
from database import IntegrityError, begin_write, get_db_connection, init_db
from emergency_dispatch import (
    CAMPUS_SECURITY_PHONE,
    dispatch_emergency_alert,
    normalize_phone,
    send_sms,
    sms_gateway_configured,
)
from payments import PaymentError
from pricing_and_queue import (
    CAMPUS_HOP_PLATFORM_FEE,
    CAMPUS_LANDMARKS,
    LPU_CENTER,
    VALID_SERVICES,
    calculate_fare,
    find_nearest_campus_landmark,
    haversine_distance_km,
    is_city_landmark,
    validate_trip,
)

log = logging.getLogger("campusgo")

FRONTEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "frontend"))
# Static files are served by serve_static() below (from memory, compressed, cache-friendly).
app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024  # JSON bodies here are tiny

# Behind a reverse proxy (Render, nginx) request.remote_addr is the proxy's IP,
# which would put every user in one rate-limit bucket. Set TRUST_PROXY_HOPS to
# the number of proxies in front of the app so the real client IP is used.
_trust_hops = int(os.environ.get("TRUST_PROXY_HOPS", "0") or 0)
if _trust_hops > 0:
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=_trust_hops, x_proto=_trust_hops, x_host=_trust_hops)

PLATFORM_SHARE = 0.10
DRIVER_SHARE = 1 - PLATFORM_SHARE
MAX_EMERGENCY_CONTACTS = 5
LOCATION_MAX_AGE_SECONDS = 120     # a driver who hasn't reported a position this recently can't be matched
PICKUP_RADIUS_KM = 0.15            # driver must be this close to the pickup for the ride to start
START_PIN_MAX_ATTEMPTS = 5         # wrong ride PINs before the driver must release the ride
MAX_PLAUSIBLE_SPEED_KMH = 150      # a GPS report implying a faster jump than this is rejected
STALE_TRIP_SECONDS = 12 * 3600     # a started trip nobody finished is settled after this
DROP_RADIUS_KM = 0.30              # ...and this close to the drop for the driver to complete it
ROUTE_ORIGIN_RADIUS_KM = 0.5
ROUTE_DEST_RADIUS_KM = 1.0
QUEUE_TTL_SECONDS = 30 * 60        # an unmatched ride request is cancelled and refunded after this
ROUTE_GRACE_SECONDS = 6 * 3600     # a carpool is cancelled and refunded this long after its departure time
SWEEP_INTERVAL_SECONDS = 30
# Campus time is India time; datetime-local inputs send naive local times.
LOCAL_UTC_OFFSET_MINUTES = int(os.environ.get("LOCAL_UTC_OFFSET_MINUTES", "330") or 330)

# --- Session tokens ---------------------------------------------------------
def _load_secret_key():
    """gunicorn.conf.py refuses to start without SECRET_KEY, so the random fallback only ever
    applies to local `python app.py` runs (sessions then end when the server restarts)."""
    key = os.environ.get("SECRET_KEY", "").strip()
    if not key:
        print("WARNING: SECRET_KEY is not set; using a random key, so every session ends on restart.")
        return secrets.token_hex(32)
    if len(key) < 32 or key == "dev_only_change_me_in_production":
        raise RuntimeError(
            "SECRET_KEY is a known placeholder or shorter than 32 characters, so anyone could forge "
            "login tokens. Generate one with: python -c \"import secrets; print(secrets.token_hex(32))\""
        )
    return key


SESSION_SERIALIZER = URLSafeTimedSerializer(_load_secret_key(), salt="campusgo-session-v1")
SESSION_MAX_AGE_SECONDS = 60 * 60 * 24 * 7  # 7 days
OTP_HMAC_KEY = hashlib.sha256(b"campusgo-otp-v1:" + SESSION_SERIALIZER.secret_keys[-1]).digest()


def generate_session_token(conn, user_id):
    row = conn.execute("SELECT token_version FROM users WHERE id = ?", (user_id,)).fetchone()
    return SESSION_SERIALIZER.dumps({"u": user_id, "v": row["token_version"] if row else 0})


def verify_session_token(token):
    """Returns the token's {"u": user_id, "v": version} payload, or None."""
    try:
        payload = SESSION_SERIALIZER.loads(token, max_age=SESSION_MAX_AGE_SECONDS)
    except (BadSignature, SignatureExpired):
        return None
    if isinstance(payload, dict) and isinstance(payload.get("u"), str) and isinstance(payload.get("v"), int):
        return payload
    return None


# --- Errors -----------------------------------------------------------------
class ApiError(Exception):
    def __init__(self, message, status=400, code=None, **extra):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.extra = extra


@app.errorhandler(ApiError)
def _handle_api_error(err):
    payload = {"error": err.message, **err.extra}
    if err.code:
        payload["code"] = err.code
    return jsonify(payload), err.status


@app.errorhandler(geo.GeoError)
def _handle_geo_error(err):
    return jsonify({"error": str(err), "code": "MAP_SERVICE"}), 503


@app.errorhandler(PaymentError)
def _handle_payment_error(err):
    return jsonify({"error": str(err), "code": "PAYMENT_ERROR"}), 400


@app.errorhandler(HTTPException)
def _handle_http_error(err):
    return jsonify({"error": err.description or err.name}), err.code


@app.errorhandler(Exception)
def _handle_unexpected_error(err):
    # Never leak internals (SQL, stack traces) to the client.
    log.exception("Unhandled error on %s %s", request.method, request.path)
    return jsonify({"error": "Something went wrong. Please try again."}), 500


# --- Database: one connection per request, always closed --------------------
def get_db():
    if "db" not in g:
        g.db = get_db_connection()
    return g.db


@app.teardown_appcontext
def _close_db(exc):
    conn = g.pop("db", None)
    if conn is not None:
        try:
            conn.rollback()  # discards anything a failed request left uncommitted
        finally:
            conn.close()


# --- Map tiles ------------------------------------------------------------------
# The map is drawn from two files of our own (frontend/maps/, cut from the Protomaps OpenStreetMap
# build by maintenance/build_maps.sh): no outside tile server, API key or usage limit. The browser
# reads them in small pieces (HTTP range requests), never whole.
MAP_FILES = ("area.pmtiles", "streets.pmtiles")

# --- Security headers ---------------------------------------------------------
CONTENT_SECURITY_POLICY = "; ".join([
    "default-src 'self'",
    "script-src 'self' https://checkout.razorpay.com https://*.razorpay.com",
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
    "font-src 'self' https://fonts.gstatic.com",
    "img-src 'self' data: https://*.razorpay.com",
    "connect-src 'self' https://*.razorpay.com",
    "frame-src https://api.razorpay.com https://checkout.razorpay.com",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
])


@app.after_request
def _security_headers(resp):
    resp.headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    resp.headers["Permissions-Policy"] = "geolocation=(self), camera=(), microphone=()"
    if request.is_secure:
        resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    elif "Cache-Control" not in resp.headers:
        resp.headers["Cache-Control"] = "no-cache"
    return resp


_proxy_warned = False


@app.before_request
def _warn_about_proxy_misconfig():
    # Behind a proxy with TRUST_PROXY_HOPS=0 every user shares one rate-limit bucket.
    global _proxy_warned
    if not _proxy_warned and _trust_hops == 0 and request.headers.get("X-Forwarded-For"):
        _proxy_warned = True
        log.warning("X-Forwarded-For seen but TRUST_PROXY_HOPS=0: all clients share one rate-limit bucket. "
                    "Set TRUST_PROXY_HOPS to the number of proxies in front of the app.")


# --- Input helpers ------------------------------------------------------------
def body():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def get_number(data, key, default=None, *, lo, hi, integer=False, label=None):
    label = label or key.replace("_", " ")
    raw = data.get(key, default)
    if raw is None or isinstance(raw, bool):
        raise ApiError(f"A valid {label} is required")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ApiError(f"A valid {label} is required")
    if not math.isfinite(value):
        raise ApiError(f"A valid {label} is required")
    if integer:
        if value != int(value):
            raise ApiError(f"{label.capitalize()} must be a whole number")
        value = int(value)
    if value < lo or value > hi:
        raise ApiError(f"{label.capitalize()} must be between {lo} and {hi}")
    return value


def get_text(data, key, *, required=False, max_len=100, default="", label=None):
    label = label or key.replace("_", " ")
    raw = data.get(key, default)
    if raw is None:
        raw = ""
    if not isinstance(raw, str):
        raise ApiError(f"{label.capitalize()} must be text")
    raw = raw.strip()
    if required and not raw:
        raise ApiError(f"{label.capitalize()} is required")
    if len(raw) > max_len:
        raise ApiError(f"{label.capitalize()} must be at most {max_len} characters")
    return raw


def get_coordinates(data):
    """Both coordinates are required: a missing GPS fix must never be replaced by a made-up place."""
    lat = get_number(data, "lat", lo=-90, hi=90, label="latitude")
    lng = get_number(data, "lng", lo=-180, hi=180, label="longitude")
    return lat, lng


def as_bool(value):
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


# --- Rate limiting --------------------------------------------------------------
# Counters live in Redis when REDIS_URL is set (shared by every process and server), otherwise
# in this process's memory (single-worker deployments). See shared.py.
def check_rate(key, max_requests, window_seconds):
    if not shared.hit(key, max_requests, window_seconds):
        raise ApiError("Rate limit exceeded. Please wait a moment before trying again.", 429, "RATE_LIMITED")


def rate_limit(max_requests=15, window_seconds=60):
    """Buckets per endpoint, per signed-in user (or per client IP when anonymous)."""
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            who = getattr(request, "auth_user_id", None) or f"ip:{request.remote_addr or 'unknown'}"
            check_rate(f"{f.__name__}:{who}", max_requests, window_seconds)
            return f(*args, **kwargs)
        return wrapped
    return decorator


# Each user's current token version, cached briefly so polling requests skip a database
# round trip. Logging out clears it at once in the process that handled the logout; other
# processes notice within TOKEN_CHECK_CACHE_SECONDS.
TOKEN_CHECK_CACHE_SECONDS = 10
_token_versions = {}


def current_token_version(user_id):
    now = time.time()
    hit = _token_versions.get(user_id)
    if hit and hit[0] > now:
        return hit[1]
    row = get_db().execute("SELECT token_version FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        return None
    if len(_token_versions) > 100_000:
        _token_versions.clear()
    _token_versions[user_id] = (now + TOKEN_CHECK_CACHE_SECONDS, row["token_version"])
    return row["token_version"]


def require_auth(f):
    """Requires 'Authorization: Bearer <token>'. The acting user always comes
    from the token (request.auth_user_id), never from the request body."""
    @wraps(f)
    def wrapped(*args, **kwargs):
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            raise ApiError("Authentication required", 401, "AUTH_REQUIRED")
        payload = verify_session_token(auth_header[7:])
        if not payload or current_token_version(payload["u"]) != payload["v"]:
            raise ApiError("Session expired or invalid, please log in again", 401, "AUTH_INVALID")
        request.auth_user_id = payload["u"]
        return f(*args, **kwargs)
    return wrapped


# --- Shared domain helpers --------------------------------------------------------
def public_base_url():
    base = os.environ.get("PUBLIC_BASE_URL", "").strip()
    return (base or request.host_url).rstrip("/")


def tracking_url(share_token):
    return f"{public_base_url()}/track/{share_token}"


HONORIFICS = {"dr", "mr", "mrs", "ms", "prof"}


def first_name(name, fallback="Rider"):
    """What other users see of someone's name outside a trip they share ("Dr. Raman Sharma" -> "Raman")."""
    parts = [p for p in (name or "").split() if p.lower().rstrip(".") not in HONORIFICS]
    return parts[0] if parts else fallback


def wallet_balance(conn, user_id):
    row = conn.execute("SELECT balance FROM wallets WHERE user_id = ?", (user_id,)).fetchone()
    return row["balance"] if row else 0.0


def debit_wallet(conn, user_id, amount, reference_id, description, tx_type="fare_deduction"):
    """Atomically debits only if the balance covers it. Caller holds the write lock."""
    now = time.time()
    cur = conn.execute(
        "UPDATE wallets SET balance = balance - ?, updated_at = ? WHERE user_id = ? AND balance >= ?",
        (amount, now, user_id, amount),
    )
    if cur.rowcount != 1:
        balance = wallet_balance(conn, user_id)
        raise ApiError(
            f"Insufficient wallet balance. You need ₹{amount:.2f} but have ₹{balance:.2f}.",
            402, "INSUFFICIENT_WALLET_BALANCE",
            required_amount=amount, current_balance=balance, deficit=round(amount - balance, 2),
        )
    new_bal = wallet_balance(conn, user_id)
    conn.execute("""
    INSERT INTO wallet_transactions (id, user_id, amount, type, reference_id, description, balance_after, created_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (str(uuid.uuid4()), user_id, -amount, tx_type, reference_id, description, new_bal, now))
    return new_bal


def credit_wallet(conn, user_id, amount, tx_type, reference_id, description):
    now = time.time()
    conn.execute("INSERT INTO wallets (user_id, balance, updated_at) VALUES (?, 0, ?) ON CONFLICT (user_id) DO NOTHING",
                 (user_id, now))
    conn.execute("UPDATE wallets SET balance = balance + ?, updated_at = ? WHERE user_id = ?", (amount, now, user_id))
    new_bal = wallet_balance(conn, user_id)
    conn.execute("""
    INSERT INTO wallet_transactions (id, user_id, amount, type, reference_id, description, balance_after, created_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (str(uuid.uuid4()), user_id, amount, tx_type, reference_id, description, new_bal, now))
    return new_bal


def active_vehicle(conn, user_id):
    return conn.execute("SELECT * FROM vehicles WHERE user_id = ? AND is_active = 1", (user_id,)).fetchone()


def require_vehicle(conn, user_id):
    vehicle = active_vehicle(conn, user_id)
    if not vehicle:
        raise ApiError("Register a vehicle in your profile before driving.", 403, "VEHICLE_REQUIRED")
    return vehicle


def driver_is_busy(conn, driver_id):
    """True while the user is driving someone: an accepted ride or a carpool they host that has started."""
    return conn.execute("""
    SELECT 1 FROM rides WHERE driver_id = ? AND status IN ('matched', 'arriving', 'in_progress')
    UNION ALL
    SELECT 1 FROM driver_routes WHERE driver_id = ? AND status = 'in_progress'
    """, (driver_id, driver_id)).fetchone() is not None


def find_landmark(value):
    """Exact match on a landmark key or full name - no fuzzy guessing."""
    value = value.strip() if isinstance(value, str) else ""
    if value in CAMPUS_LANDMARKS:
        return value, CAMPUS_LANDMARKS[value]
    for key, landmark in CAMPUS_LANDMARKS.items():
        if landmark["name"].lower() == value.lower():
            return key, landmark
    return None, None


def load_ride_for_participant(conn, ride_id):
    ride = conn.execute("SELECT * FROM rides WHERE id = ?", (ride_id,)).fetchone()
    if not ride:
        raise ApiError("Ride not found", 404)
    if request.auth_user_id not in (ride["rider_id"], ride["driver_id"]):
        raise ApiError("You are not part of this ride", 403)
    return ride


def route_participant_check(conn, route, user_id):
    if route["driver_id"] == user_id:
        return
    booking = conn.execute("""
    SELECT id FROM route_bookings
    WHERE route_id = ? AND rider_id = ? AND status IN ('confirmed', 'in_progress', 'completed')
    """, (route["id"], user_id)).fetchone()
    if not booking:
        raise ApiError("You are not part of this route", 403)


def queue_position(conn, zone, is_priority, created_at):
    return conn.execute("""
    SELECT COUNT(*) AS pos FROM rides
    WHERE status = 'queued' AND pickup_zone = ?
      AND (is_priority > ? OR (is_priority = ? AND created_at <= ?))
    """, (zone, is_priority, is_priority, created_at)).fetchone()["pos"]


def driver_near(conn, driver_id, lat, lng, radius_km):
    """True when the driver's own phone recently reported a position within radius_km of the point."""
    loc = conn.execute("SELECT lat, lng, updated_at FROM driver_locations WHERE driver_id = ?", (driver_id,)).fetchone()
    if not loc or time.time() - loc["updated_at"] > LOCATION_MAX_AGE_SECONDS:
        return False
    return haversine_distance_km(loc["lat"], loc["lng"], lat, lng) <= radius_km


def parse_departure(text):
    """Epoch seconds for a 'YYYY-MM-DDTHH:MM' campus-local time, or None if it isn't one."""
    try:
        naive = datetime.strptime((text or "").strip(), "%Y-%m-%dT%H:%M")
    except ValueError:
        return None
    return (naive - timedelta(minutes=LOCAL_UTC_OFFSET_MINUTES)).replace(tzinfo=timezone.utc).timestamp()


def cancel_ride_with_refund(conn, ride, reason):
    """Cancels a not-yet-started ride and returns the held fare. Returns the refunded
    amount, or None if the ride had already moved on. Caller holds the write lock."""
    cur = conn.execute(
        "UPDATE rides SET status = 'cancelled', cancelled_at = ?, cancellation_reason = ? "
        "WHERE id = ? AND status IN ('queued', 'matched', 'arriving')",
        (time.time(), reason, ride["id"]),
    )
    if cur.rowcount != 1:
        return None
    credit_wallet(conn, ride["rider_id"], ride["fare"], "refund", ride["id"], f"Refund for cancelled Ride #{ride['id'][5:13]}")
    return ride["fare"]


def cancel_route_with_refunds(conn, route, why):
    """Cancels an unstarted carpool and refunds every confirmed seat. Returns bookings refunded, or None."""
    cur = conn.execute("UPDATE driver_routes SET status = 'cancelled' WHERE id = ? AND status IN ('open', 'pinned')",
                       (route["id"],))
    if cur.rowcount != 1:
        return None
    bookings = conn.execute("SELECT * FROM route_bookings WHERE route_id = ? AND status = 'confirmed'", (route["id"],)).fetchall()
    for b in bookings:
        conn.execute("UPDATE route_bookings SET status = 'cancelled' WHERE id = ?", (b["id"],))
        credit_wallet(conn, b["rider_id"], b["fare_paid"], "refund", b["id"], f"Refund: {why} to {route['destination']}")
    return len(bookings)


def driver_payout_for(scope, fare):
    """Campus Hop keeps a flat platform fee; CityLink and carpools keep PLATFORM_SHARE."""
    if scope == "campus_hop":
        return round(max(0.0, fare - CAMPUS_HOP_PLATFORM_FEE), 2)
    return round(fare * DRIVER_SHARE, 2)


def record_platform_earning(conn, source, reference_id, scope, fare, payout, now):
    conn.execute("""
    INSERT INTO platform_earnings (id, source, reference_id, scope, fare, driver_payout, amount, created_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (str(uuid.uuid4()), source, reference_id, scope, fare, payout, round(fare - payout, 2), now))


def withdrawable_balance(conn, user_id):
    """Drivers can cash out what they earned, never money they topped up themselves."""
    earned = conn.execute("""
    SELECT COALESCE(SUM(amount), 0.0) AS total FROM wallet_transactions
    WHERE user_id = ? AND type IN ('driver_payout', 'withdrawal')
    """, (user_id,)).fetchone()["total"]
    return round(max(0.0, min(earned, wallet_balance(conn, user_id))), 2)


def settle_ride(conn, ride, now):
    """Completes a started ride and pays the driver. Returns the payout, or None if it had
    already moved on. Caller holds the write lock."""
    cur = conn.execute(
        "UPDATE rides SET status = 'completed', completed_at = ? "
        "WHERE id = ? AND status = 'in_progress' AND driver_id IS NOT NULL",
        (now, ride["id"]),
    )
    if cur.rowcount != 1:
        return None
    # The fare was held when the ride was booked, so only the driver is paid here.
    payout = driver_payout_for(ride["scope"], ride["fare"])
    credit_wallet(conn, ride["driver_id"], payout, "driver_payout", ride["id"], f"Payout for Ride #{ride['id'][5:13]}")
    record_platform_earning(conn, "ride", ride["id"], ride["scope"], ride["fare"], payout, now)
    return payout


def settle_route(conn, route, now):
    """Completes a started carpool and pays the host. Returns (revenue, payout), or None."""
    cur = conn.execute(
        "UPDATE driver_routes SET status = 'completed', completed_at = ? WHERE id = ? AND status = 'in_progress'",
        (now, route["id"]),
    )
    if cur.rowcount != 1:
        return None
    revenue = conn.execute(
        "SELECT COALESCE(SUM(fare_paid), 0.0) AS total FROM route_bookings WHERE route_id = ? AND status = 'in_progress'",
        (route["id"],),
    ).fetchone()["total"]
    payout = round(revenue * DRIVER_SHARE, 2)
    if payout > 0:
        credit_wallet(conn, route["driver_id"], payout, "driver_payout", route["id"], f"Carpool Payout: {route['destination']}")
        record_platform_earning(conn, "carpool", route["id"], "citylink", revenue, payout, now)
    conn.execute(
        "UPDATE route_bookings SET status = 'completed', completed_at = ? WHERE route_id = ? AND status = 'in_progress'",
        (now, route["id"]),
    )
    return revenue, payout


def sweep_stale(conn, force=False):
    """Frees money and seats stuck in requests nobody served: queued rides older than
    QUEUE_TTL and carpools long past their departure. Must run outside a transaction.
    Runs at most once per SWEEP_INTERVAL_SECONDS across all app processes."""
    if not force and not shared.acquire("sweep", SWEEP_INTERVAL_SECONDS):
        return
    now = time.time()
    ride_ids = [r["id"] for r in conn.execute(
        "SELECT id FROM rides WHERE status = 'queued' AND created_at < ?", (now - QUEUE_TTL_SECONDS,)).fetchall()]
    route_ids = [r["id"] for r in conn.execute(
        "SELECT id, departure_time FROM driver_routes WHERE status IN ('open', 'pinned')").fetchall()
        if now > parse_departure(r["departure_time"]) + ROUTE_GRACE_SECONDS]
    # Started trips nobody marked complete. A ride only starts once the rider gives the driver
    # their PIN, and a carpool once the host is at the pickup, so the trip happened: settle it.
    stale_ride_ids = [r["id"] for r in conn.execute(
        "SELECT id FROM rides WHERE status = 'in_progress' AND started_at < ?", (now - STALE_TRIP_SECONDS,)).fetchall()]
    stale_route_ids = [r["id"] for r in conn.execute(
        "SELECT id FROM driver_routes WHERE status = 'in_progress' AND started_at < ?", (now - STALE_TRIP_SECONDS,)).fetchall()]
    if not (ride_ids or route_ids or stale_ride_ids or stale_route_ids):
        return
    begin_write(conn)
    for ride_id in ride_ids:
        ride = conn.execute("SELECT * FROM rides WHERE id = ? AND status = 'queued'", (ride_id,)).fetchone()
        if ride:
            cancel_ride_with_refund(conn, ride, "No driver accepted in time")
    for route_id in route_ids:
        route = conn.execute("SELECT * FROM driver_routes WHERE id = ?", (route_id,)).fetchone()
        if route:
            cancel_route_with_refunds(conn, route, "trip expired")
    for ride_id in stale_ride_ids:
        ride = conn.execute("SELECT * FROM rides WHERE id = ? AND status = 'in_progress'", (ride_id,)).fetchone()
        if ride:
            settle_ride(conn, ride, now)
    for route_id in stale_route_ids:
        route = conn.execute("SELECT * FROM driver_routes WHERE id = ? AND status = 'in_progress'", (route_id,)).fetchone()
        if route:
            settle_route(conn, route, now)
    conn.commit()


# --- Static pages -------------------------------------------------------------------
# The few frontend files are read once and kept in memory with a gzip copy. Pages link the
# JS/CSS with ?v=<content hash>, so browsers and CDNs cache those for a year and still pick
# up a new release immediately; the pages themselves are revalidated with an ETag.
STATIC_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png",
                ".webmanifest": "application/manifest+json"}
VERSIONED_ASSETS = ("app.js", "styles.css", "track.js", "leaflet.js", "leaflet.css", "protomaps-leaflet.js", "campus-map.js")
PUBLIC_FILES = VERSIONED_ASSETS + ("sw.js", "manifest.webmanifest", "offline.html", "favicon.svg", "avatar-default.svg",
                                   "icon-192.png", "icon-512.png", "apple-touch-icon.png")
_static_files = {}
_static_lock = threading.RLock()  # re-entered: a page loads its JS/CSS to version them


def _load_static(name):
    path = os.path.join(FRONTEND_DIR, name)
    mtime = os.path.getmtime(path)
    cached = _static_files.get(name)
    if cached and cached["mtime"] == mtime:
        return cached
    with _static_lock:
        with open(path, "rb") as fh:
            data = fh.read()
        if name.endswith(".html"):
            text = data.decode("utf-8")
            for asset in VERSIONED_ASSETS:
                version = _load_static(asset)["etag"].strip('"')
                text = (text.replace(f'"{asset}"', f'"/{asset}?v={version}"')
                            .replace(f'"/{asset}"', f'"/{asset}?v={version}"'))
            data = text.encode("utf-8")
        content_type = STATIC_TYPES[os.path.splitext(name)[1]]
        entry = {"mtime": mtime, "data": data, "type": content_type,
                 "gzip": gzip.compress(data, 6) if content_type != "image/png" else None,
                 "etag": '"' + hashlib.sha256(data).hexdigest()[:16] + '"'}
        _static_files[name] = entry
        return entry


def _static_response(name, versioned=False):
    entry = _load_static(name)
    headers = {"ETag": entry["etag"], "Vary": "Accept-Encoding",
               "Cache-Control": "public, max-age=31536000, immutable" if versioned else "no-cache"}
    if request.headers.get("If-None-Match") == entry["etag"]:
        return Response(status=304, headers=headers)
    body = entry["data"]
    if entry["gzip"] and "gzip" in request.headers.get("Accept-Encoding", "") and len(body) > 1024:
        body = entry["gzip"]
        headers["Content-Encoding"] = "gzip"
    return Response(body, headers=headers, content_type=entry["type"])


@app.route("/")
def index():
    return _static_response("index.html")


@app.route("/track/<share_token>")
def track_page(share_token):
    return _static_response("track.html")


@app.route("/privacy")
def privacy_page():
    return _static_response("privacy.html")


@app.route("/terms")
def terms_page():
    return _static_response("terms.html")


@app.route("/<name>")
def serve_static(name):
    if name not in PUBLIC_FILES:
        raise ApiError("Not found", 404)
    return _static_response(name, versioned=bool(request.args.get("v")))


@app.route("/maps/<name>")
def map_file(name):
    if name not in MAP_FILES:
        raise ApiError("Not found", 404)
    # conditional=True answers the Range requests the map reader makes; the file is refreshed
    # only every few months, and the reader notices a new copy by its ETag.
    return send_from_directory(os.path.join(FRONTEND_DIR, "maps"), name, mimetype="application/octet-stream",
                               conditional=True, max_age=86400)


@app.route("/.well-known/assetlinks.json")
def android_asset_links():
    """Proves to Android that the Play Store app (a Trusted Web Activity) and this site have the same
    owner, so the app opens full-screen without a browser bar. Values come from the Play Console."""
    package = os.environ.get("ANDROID_PACKAGE_NAME", "").strip()
    fingerprints = [f.strip() for f in os.environ.get("ANDROID_CERT_SHA256", "").split(",") if f.strip()]
    if not package or not fingerprints:
        raise ApiError("Not found", 404)
    return jsonify([{
        "relation": ["delegate_permission/common.handle_all_urls"],
        "target": {"namespace": "android_app", "package_name": package, "sha256_cert_fingerprints": fingerprints},
    }])


@app.route("/healthz")
def healthz():
    """For load balancers: the app is up and can reach its database."""
    get_db().execute("SELECT 1").fetchone()
    return jsonify({"status": "ok"})


# --- Public config -------------------------------------------------------------------
@app.route("/api/config", methods=["GET"])
def public_config():
    return jsonify({
        "security_hotline": CAMPUS_SECURITY_PHONE,
        "razorpay_enabled": payments.razorpay_configured(),
        "upi_enabled": bool(payments.upi_vpa()),
    })


# --- Auth -----------------------------------------------------------------------------
def _build_user_dict(conn, user_id):
    user = conn.execute("""
    SELECT id, lpu_id, username, name, phone, user_type, role, is_teacher_priority, created_at
    FROM users WHERE id = ? AND deleted_at IS NULL
    """, (user_id,)).fetchone()
    if not user:
        return None
    user_dict = dict(user)
    contacts = conn.execute("SELECT COUNT(*) AS c FROM emergency_contacts WHERE user_id = ?", (user_id,)).fetchone()["c"]
    vehicle = active_vehicle(conn, user_id)
    user_dict["wallet_balance"] = round(wallet_balance(conn, user_id), 2)
    user_dict["has_emergency_contacts"] = contacts > 0
    user_dict["vehicle"] = dict(vehicle) if vehicle else None
    return user_dict


# --- Sign up / log in with a one-time password sent by SMS ---------------------------
OTP_LENGTH = 6
OTP_TTL_SECONDS = 300
OTP_MAX_ATTEMPTS = 5
OTP_RESEND_SECONDS = 30
USERNAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._]{2,19}$")
STUDENT_ID_PATTERN = re.compile(r"^\d{8}$")
TEACHER_ID_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9\-]{3,19}$")
NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z .'\-]{1,59}$")


def phone_key(raw):
    """Canonical form of an Indian mobile number (its last 10 digits), or None."""
    cleaned = normalize_phone(raw)
    if not cleaned:
        return None
    digits = re.sub(r"\D", "", cleaned)
    if len(digits) == 10:
        key = digits
    elif len(digits) == 12 and digits.startswith("91"):
        key = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        key = digits[1:]
    else:
        return None
    return key if key[0] in "6789" else None


def _otp_hash(challenge_id, code):
    return hmac.new(OTP_HMAC_KEY, f"{challenge_id}:{code}".encode(), hashlib.sha256).hexdigest()


def review_login_code(key):
    """App Store and Play reviewers can't receive our SMS, so REVIEW_LOGIN_PHONE signs in with the
    fixed REVIEW_LOGIN_CODE instead of a texted one. Only that one number; every other limit applies."""
    review_key = phone_key(os.environ.get("REVIEW_LOGIN_PHONE", ""))
    code = os.environ.get("REVIEW_LOGIN_CODE", "").strip()
    if review_key and key == review_key and re.fullmatch(r"\d{6}", code):
        return code
    return None


def require_phone(data):
    key = phone_key(data.get("phone"))
    if not key:
        raise ApiError("Enter a valid 10-digit Indian mobile number", 400, "BAD_PHONE")
    return key


def normalize_lpu_id(value, user_type):
    value = value.strip().upper()
    if user_type == "student" and not STUDENT_ID_PATTERN.match(value):
        raise ApiError("A student LPU ID is 8 digits, e.g. 12204592", 400, "BAD_LPU_ID")
    if user_type == "teacher" and not TEACHER_ID_PATTERN.match(value):
        raise ApiError("Enter your faculty ID, e.g. FAC-10822", 400, "BAD_LPU_ID")
    return value


def issue_otp(conn, purpose, key, payload):
    """Creates a challenge and texts the code. Returns the JSON reply for the client."""
    review_code = review_login_code(key)
    if not review_code and not sms_gateway_configured():
        raise ApiError("SMS sign-in is not available right now. Please try again later.", 503, "SMS_UNAVAILABLE")

    now = time.time()
    recent = conn.execute(
        "SELECT created_at FROM otp_challenges WHERE phone_key = ? ORDER BY created_at DESC LIMIT 1", (key,)
    ).fetchone()
    if recent and now - recent["created_at"] < OTP_RESEND_SECONDS:
        wait = int(OTP_RESEND_SECONDS - (now - recent["created_at"])) + 1
        raise ApiError(f"Please wait {wait}s before asking for another code", 429, "OTP_COOLDOWN", retry_after=wait)
    # Caps texts per number: SMS costs money, and this stops spamming someone else's phone.
    check_rate(f"otp-phone:{key}", 5, 3600)

    code = review_code or "".join(str(secrets.randbelow(10)) for _ in range(OTP_LENGTH))
    challenge_id = f"otp_{secrets.token_urlsafe(16)}"
    if not review_code:
        delivery = send_sms(f"+91{key}", f"{code} is your CampusGo verification code. It expires in 5 minutes. Do not share it with anyone.")
        if delivery.get("status") != "sent":
            raise ApiError("We couldn't send the SMS. Check the number and try again.", 502, "SMS_FAILED")

    conn.execute("DELETE FROM otp_challenges WHERE expires_at < ?", (now - 3600,))
    conn.execute(
        "INSERT INTO otp_challenges (id, purpose, phone_key, code_hash, payload, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (challenge_id, purpose, key, _otp_hash(challenge_id, code), json.dumps(payload), now, now + OTP_TTL_SECONDS),
    )
    conn.commit()
    return {
        "challenge_id": challenge_id,
        "phone_hint": f"+91 {key[:2]}XXXXXX{key[-2:]}",
        "expires_in": OTP_TTL_SECONDS,
        "resend_after": OTP_RESEND_SECONDS,
    }


def consume_otp(conn, purpose, data):
    """Checks the submitted code once. Returns (payload, phone_key) stored when it was issued."""
    challenge_id = get_text(data, "challenge_id", required=True, max_len=64, label="challenge")
    code = get_text(data, "otp", required=True, max_len=12, label="code")
    row = conn.execute("SELECT * FROM otp_challenges WHERE id = ? AND purpose = ?", (challenge_id, purpose)).fetchone()
    if not row or row["consumed"] or time.time() > row["expires_at"]:
        raise ApiError("This code has expired. Request a new one.", 400, "OTP_EXPIRED")
    # Count the attempt before comparing, so guessing is capped even with parallel requests.
    begin_write(conn)
    cur = conn.execute("UPDATE otp_challenges SET attempts = attempts + 1 WHERE id = ? AND attempts < ? AND consumed = 0",
                       (challenge_id, OTP_MAX_ATTEMPTS))
    conn.commit()
    if cur.rowcount == 0:
        raise ApiError("Too many wrong codes. Request a new one.", 429, "OTP_LOCKED")
    if not hmac.compare_digest(row["code_hash"], _otp_hash(challenge_id, code)):
        left = OTP_MAX_ATTEMPTS - row["attempts"] - 1
        raise ApiError("That code is not correct" + (f" ({left} tries left)" if left > 0 else ""), 400, "OTP_WRONG")
    begin_write(conn)
    claimed = conn.execute("UPDATE otp_challenges SET consumed = 1 WHERE id = ? AND consumed = 0", (challenge_id,))
    conn.commit()
    if claimed.rowcount == 0:
        raise ApiError("This code has already been used.", 400, "OTP_EXPIRED")
    return json.loads(row["payload"]), row["phone_key"]


def username_taken(conn, username):
    return conn.execute("SELECT 1 FROM users WHERE lower(username) = ?", (username.lower(),)).fetchone() is not None


def suggest_usernames(conn, name, count=5):
    parts = [re.sub(r"[^a-z0-9]", "", p) for p in name.lower().split()]
    parts = [p for p in parts if p]
    if not parts:
        return []
    first, last = parts[0], parts[-1]
    if first == last:
        candidates = [first, f"{first}_official", f"the{first}"]
    else:
        candidates = [f"{first}.{last}", f"{first}{last}", f"{first}_{last}", f"{first}{last[:1]}", f"{first[:1]}{last}", first]
    suggestions = []
    for candidate in candidates:
        if USERNAME_PATTERN.match(candidate) and candidate not in suggestions and not username_taken(conn, candidate):
            suggestions.append(candidate)
    # Fill the rest with numbered variants that are free right now.
    base = candidates[0][:16]
    attempts = 0
    while len(suggestions) < count and attempts < 50:
        attempts += 1
        candidate = f"{base}{secrets.randbelow(900) + 100}"
        if candidate not in suggestions and USERNAME_PATTERN.match(candidate) and not username_taken(conn, candidate):
            suggestions.append(candidate)
    return suggestions[:count]


@app.route("/api/auth/username-suggestions", methods=["POST"])
@rate_limit(max_requests=30, window_seconds=60)
def username_suggestions():
    name = get_text(body(), "name", required=True, max_len=60, label="name")
    if not NAME_PATTERN.match(name):
        raise ApiError("Enter your full name using letters only", 400, "BAD_NAME")
    return jsonify({"suggestions": suggest_usernames(get_db(), name)})


@app.route("/api/auth/username-available", methods=["POST"])
@rate_limit(max_requests=60, window_seconds=60)
def username_available():
    username = get_text(body(), "username", required=True, max_len=30, label="username").lower()
    if not USERNAME_PATTERN.match(username):
        return jsonify({"available": False, "reason": "Use 3-20 lowercase letters, numbers, dots or underscores"})
    taken = username_taken(get_db(), username)
    return jsonify({"available": not taken, "reason": "That username is taken" if taken else ""})


@app.route("/api/auth/signup/start", methods=["POST"])
@rate_limit(max_requests=10, window_seconds=60)
def signup_start():
    data = body()
    name = get_text(data, "name", required=True, max_len=60, label="name")
    if not NAME_PATTERN.match(name):
        raise ApiError("Enter your full name using letters only", 400, "BAD_NAME")
    username = get_text(data, "username", required=True, max_len=30, label="username").lower()
    if not USERNAME_PATTERN.match(username):
        raise ApiError("Username must be 3-20 lowercase letters, numbers, dots or underscores", 400, "BAD_USERNAME")
    user_type = data.get("user_type")
    if user_type not in ("student", "teacher"):
        raise ApiError("Choose whether you are a student or a teacher")
    lpu_id = normalize_lpu_id(get_text(data, "lpu_id", required=True, max_len=32, label="LPU ID"), user_type)
    key = require_phone(data)

    conn = get_db()
    if username_taken(conn, username):
        raise ApiError("That username is taken. Pick another.", 409, "USERNAME_TAKEN")
    if conn.execute("SELECT 1 FROM users WHERE upper(lpu_id) = ?", (lpu_id,)).fetchone():
        raise ApiError("An account with this LPU ID already exists. Please log in instead.", 409, "LPU_ID_EXISTS")
    if conn.execute("SELECT 1 FROM users WHERE phone_key = ?", (key,)).fetchone():
        raise ApiError("An account with this mobile number already exists. Please log in instead.", 409, "PHONE_EXISTS")

    payload = {"name": " ".join(name.split()), "username": username, "user_type": user_type, "lpu_id": lpu_id}
    return jsonify(issue_otp(conn, "signup", key, payload))


@app.route("/api/auth/signup/verify", methods=["POST"])
@rate_limit(max_requests=15, window_seconds=60)
def signup_verify():
    conn = get_db()
    payload, key = consume_otp(conn, "signup", body())

    user_id = f"usr_{uuid.uuid4().hex[:12]}"
    now = time.time()
    begin_write(conn)
    try:
        conn.execute("""
        INSERT INTO users (id, lpu_id, username, name, phone, phone_key, user_type, role, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'rider', ?)
        """, (user_id, payload["lpu_id"], payload["username"], payload["name"], f"+91{key}", key,
              payload["user_type"], now))
    except IntegrityError:
        conn.rollback()
        raise ApiError("Someone just took that username, ID or number. Please start again.", 409, "SIGNUP_CONFLICT")
    conn.execute("INSERT INTO wallets (user_id, balance, updated_at) VALUES (?, 0, ?)", (user_id, now))
    conn.commit()
    return jsonify({"user": _build_user_dict(conn, user_id), "token": generate_session_token(conn, user_id), "new_account": True})


@app.route("/api/auth/login/start", methods=["POST"])
@rate_limit(max_requests=10, window_seconds=60)
def login_start():
    data = body()
    lpu_id = get_text(data, "lpu_id", required=True, max_len=32, label="LPU ID").upper()
    key = require_phone(data)
    # Keyed by caller too, so a stranger can't use up a victim's attempts and lock them out.
    check_rate(f"login-account:{lpu_id}:{request.remote_addr}", 10, 300)
    conn = get_db()
    user = conn.execute("SELECT id FROM users WHERE upper(lpu_id) = ? AND phone_key = ?", (lpu_id, key)).fetchone()
    if not user:
        # Same reply (and the same resend cooldown) as a real code request, so this endpoint
        # can't be used to find out who has an account.
        try:
            check_rate(f"otp-decoy:{key}", 1, OTP_RESEND_SECONDS)
        except ApiError:
            raise ApiError(f"Please wait {OTP_RESEND_SECONDS}s before asking for another code", 429, "OTP_COOLDOWN",
                           retry_after=OTP_RESEND_SECONDS)
        return jsonify({
            "challenge_id": f"otp_{secrets.token_urlsafe(16)}",
            "phone_hint": f"+91 {key[:2]}XXXXXX{key[-2:]}",
            "expires_in": OTP_TTL_SECONDS,
            "resend_after": OTP_RESEND_SECONDS,
        })
    return jsonify(issue_otp(conn, "login", key, {"user_id": user["id"]}))


@app.route("/api/auth/login/verify", methods=["POST"])
@rate_limit(max_requests=15, window_seconds=60)
def login_verify():
    conn = get_db()
    payload, _ = consume_otp(conn, "login", body())
    return jsonify({"user": _build_user_dict(conn, payload["user_id"]), "token": generate_session_token(conn, payload["user_id"])})


@app.route("/api/auth/logout", methods=["POST"])
@require_auth
def logout():
    """Signs the user out everywhere: every token issued so far stops working."""
    conn = get_db()
    conn.execute("UPDATE users SET token_version = token_version + 1 WHERE id = ?", (request.auth_user_id,))
    conn.commit()
    _token_versions.pop(request.auth_user_id, None)
    return jsonify({"success": True})


@app.route("/api/auth/me", methods=["GET"])
@require_auth
def get_current_user():
    user = _build_user_dict(get_db(), request.auth_user_id)
    if not user:
        raise ApiError("User not found", 404)
    return jsonify({"user": user})


# --- Profile ------------------------------------------------------------------------
@app.route("/api/user/role", methods=["POST"])
@require_auth
def update_role():
    role = body().get("role")
    if role not in ("rider", "driver", "both"):
        raise ApiError("Invalid role. Must be rider, driver, or both")
    conn = get_db()
    if role == "rider" and driver_is_busy(conn, request.auth_user_id):
        raise ApiError("Finish the trip you are driving before switching to rider only.", 409, "DRIVER_BUSY")
    conn.execute("UPDATE users SET role = ? WHERE id = ?", (role, request.auth_user_id))
    if role == "rider":
        conn.execute("UPDATE driver_locations SET is_online = 0 WHERE driver_id = ?", (request.auth_user_id,))
    conn.commit()
    return jsonify({"success": True, "role": role})


PLATE_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9 \-]{3,14}$")
MAX_CAPACITY = {"bike": 1, "scooty": 1, "car": 6}


@app.route("/api/user/vehicle", methods=["POST"])
@require_auth
@rate_limit(max_requests=10, window_seconds=60)
def save_vehicle():
    data = body()
    user_id = request.auth_user_id
    category = data.get("category")
    if category not in VALID_SERVICES:
        raise ApiError("Vehicle type must be bike, scooty or car")
    model = get_text(data, "model", required=True, max_len=40, label="vehicle model")
    plate_number = get_text(data, "plate_number", required=True, max_len=15, label="plate number").upper()
    if not PLATE_PATTERN.match(plate_number):
        raise ApiError("Plate number should look like PB08-AB-1234")
    color = get_text(data, "color", max_len=20)
    capacity = get_number(data, "capacity", 1, lo=1, hi=MAX_CAPACITY[category], integer=True, label="seat capacity")
    has_helmet = 1 if as_bool(data.get("has_helmet")) else 0
    has_ac = 1 if as_bool(data.get("has_ac")) and category == "car" else 0

    conn = get_db()
    begin_write(conn)
    if driver_is_busy(conn, user_id):
        raise ApiError("You can change your vehicle after the current trip.", 409, "DRIVER_BUSY")
    existing = conn.execute("SELECT id FROM vehicles WHERE user_id = ?", (user_id,)).fetchone()
    if existing:
        conn.execute("""
        UPDATE vehicles SET category=?, model=?, plate_number=?, color=?, capacity=?, has_helmet=?, has_ac=?, is_active=1
        WHERE user_id=?
        """, (category, model, plate_number, color, capacity, has_helmet, has_ac, user_id))
        veh_id = existing["id"]
    else:
        veh_id = f"veh_{uuid.uuid4().hex[:8]}"
        conn.execute("""
        INSERT INTO vehicles (id, user_id, category, model, plate_number, color, capacity, has_helmet, has_ac, is_active)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
        """, (veh_id, user_id, category, model, plate_number, color, capacity, has_helmet, has_ac))
    conn.execute("UPDATE users SET role = 'both' WHERE id = ? AND role = 'rider'", (user_id,))
    conn.commit()
    return jsonify({"success": True, "vehicle_id": veh_id})


# --- Emergency contacts ---------------------------------------------------------------
@app.route("/api/user/emergency-contacts", methods=["GET"])
@require_auth
def get_emergency_contacts():
    rows = get_db().execute(
        "SELECT id, name, relationship, phone, is_primary, created_at FROM emergency_contacts "
        "WHERE user_id = ? ORDER BY is_primary DESC, created_at ASC",
        (request.auth_user_id,),
    ).fetchall()
    return jsonify({"contacts": [dict(r) for r in rows]})


@app.route("/api/user/emergency-contacts", methods=["POST"])
@require_auth
@rate_limit(max_requests=10, window_seconds=60)
def add_emergency_contact():
    data = body()
    user_id = request.auth_user_id
    name = get_text(data, "name", required=True, max_len=60)
    relationship = get_text(data, "relationship", max_len=40) or "Family/Friend"
    phone = normalize_phone(data.get("phone"))
    if not phone:
        raise ApiError("Enter a valid phone number with 10 to 15 digits")
    check_rate(f"contact-add-hour:{user_id}", 10, 3600)

    conn = get_db()
    own = conn.execute("SELECT phone_key FROM users WHERE id = ?", (user_id,)).fetchone()
    new_key = phone_key(phone)
    if own and new_key and own["phone_key"] == new_key:
        raise ApiError("Add someone else's number. Your own phone can't be your emergency contact.")
    begin_write(conn)
    for existing in conn.execute("SELECT phone FROM emergency_contacts WHERE user_id = ?", (user_id,)).fetchall():
        if phone_key(existing["phone"]) == new_key if new_key else existing["phone"] == phone:
            raise ApiError("That number is already one of your emergency contacts.", 409)
    count = conn.execute("SELECT COUNT(*) AS c FROM emergency_contacts WHERE user_id = ?", (user_id,)).fetchone()["c"]
    if count >= MAX_EMERGENCY_CONTACTS:
        raise ApiError(f"You can save up to {MAX_EMERGENCY_CONTACTS} emergency contacts. Remove one first.")
    contact_id = f"emg_{uuid.uuid4().hex[:8]}"
    conn.execute("""
    INSERT INTO emergency_contacts (id, user_id, name, relationship, phone, is_primary, created_at)
    VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (contact_id, user_id, name, relationship, phone, 1 if count == 0 else 0, time.time()))
    conn.commit()
    return jsonify({"success": True, "contact_id": contact_id})


@app.route("/api/user/emergency-contacts/<contact_id>", methods=["DELETE"])
@require_auth
def delete_emergency_contact(contact_id):
    conn = get_db()
    cur = conn.execute("DELETE FROM emergency_contacts WHERE id = ? AND user_id = ?", (contact_id, request.auth_user_id))
    if cur.rowcount != 1:
        raise ApiError("Contact not found", 404)
    # If the primary contact was removed, the oldest remaining one becomes primary.
    conn.execute("""
    UPDATE emergency_contacts SET is_primary = 1 WHERE id = (
        SELECT id FROM emergency_contacts WHERE user_id = ? ORDER BY created_at ASC LIMIT 1)
      AND NOT EXISTS (SELECT 1 FROM emergency_contacts WHERE user_id = ? AND is_primary = 1)
    """, (request.auth_user_id, request.auth_user_id))
    conn.commit()
    return jsonify({"success": True})


# --- Account deletion (required in-app by Google Play and the App Store) ----------------------
@app.route("/api/user/delete-account", methods=["POST"])
@require_auth
@rate_limit(max_requests=5, window_seconds=60)
def delete_account():
    """Emergency contacts, vehicle, live location and pending sign-in codes are deleted. Rides,
    payments and payouts are financial records that reference the user, so the user row stays,
    with every field that identified the person replaced. Any wallet balance is forfeited (the
    app says so first), and every session ends."""
    user_id = request.auth_user_id
    conn = get_db()
    begin_write(conn)
    busy = conn.execute("""
    SELECT 1 FROM rides WHERE (rider_id = ? OR driver_id = ?) AND status IN ('queued', 'matched', 'arriving', 'in_progress')
    UNION ALL SELECT 1 FROM driver_routes WHERE driver_id = ? AND status IN ('open', 'pinned', 'in_progress')
    UNION ALL SELECT 1 FROM route_bookings WHERE rider_id = ? AND status IN ('confirmed', 'in_progress')
    UNION ALL SELECT 1 FROM driver_payouts WHERE driver_id = ? AND status = 'requested'
    """, (user_id,) * 5).fetchone()
    if busy:
        raise ApiError("Finish or cancel your rides, carpools and payout requests before deleting your account.",
                       409, "ACCOUNT_BUSY")
    phone = conn.execute("SELECT phone_key FROM users WHERE id = ?", (user_id,)).fetchone()["phone_key"]
    conn.execute("DELETE FROM emergency_contacts WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM vehicles WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM driver_locations WHERE driver_id = ?", (user_id,))
    conn.execute("DELETE FROM otp_challenges WHERE phone_key = ?", (phone,))
    # ':' can't appear in a real LPU ID, username or phone, so these never collide with a new sign-up.
    tombstone = f"deleted:{user_id}"
    conn.execute("""
    UPDATE users SET lpu_id = ?, username = ?, phone_key = ?, name = 'Deleted user', phone = '', role = 'rider',
                     is_teacher_priority = 0, token_version = token_version + 1, deleted_at = ?
    WHERE id = ?
    """, (tombstone.upper(), tombstone, tombstone, time.time(), user_id))
    conn.commit()
    _token_versions.pop(user_id, None)
    return jsonify({"success": True})


# --- Landmarks & location --------------------------------------------------------------
@app.route("/api/campus/landmarks", methods=["GET"])
def list_landmarks():
    return jsonify({"landmarks": CAMPUS_LANDMARKS})


@app.route("/api/campus/locate", methods=["POST"])
@require_auth
@rate_limit(max_requests=30, window_seconds=60)
def locate():
    lat, lng = get_coordinates(body())
    key, landmark = find_nearest_campus_landmark(lat, lng)
    distance = haversine_distance_km(lat, lng, landmark["lat"], landmark["lng"])
    return jsonify({"nearest_key": key, "landmark": landmark, "distance_km": round(distance, 2)})


NEARBY_RADIUS_KM = 5.0       # drivers further than this from the rider aren't shown or counted
NEARBY_LIMIT = 40            # at most this many markers per map
NEARBY_CACHE_SECONDS = 4     # riders in the same ~1 km cell share one database query
_nearby_cache = {}
_nearby_lock = threading.Lock()


@lru_cache(maxsize=20000)
def driver_public_key(driver_id):
    """Lets the map move a driver's marker instead of redrawing it, without revealing the account id."""
    return hmac.new(OTP_HMAC_KEY, driver_id.encode(), hashlib.sha256).hexdigest()[:10]


def _drivers_in_cell(conn, lat, lng):
    """Free, online drivers around a ~1 km map cell, cached for a few seconds and shared by
    every rider in that cell, so polling cost doesn't grow with the number of riders."""
    cell = (round(lat, 2), round(lng, 2))
    now = time.time()
    hit = _nearby_cache.get(cell)
    if hit and hit[0] > now:
        return hit[1]
    reach = NEARBY_RADIUS_KM + 1.0  # covers anyone inside the cell
    dlat = reach / 111.0
    dlng = reach / (111.0 * max(0.2, math.cos(math.radians(cell[0]))))
    rows = conn.execute("""
    SELECT dl.driver_id, dl.lat, dl.lng, v.category
    FROM driver_locations dl
    JOIN vehicles v ON v.user_id = dl.driver_id AND v.is_active = 1
    WHERE dl.is_online = 1 AND dl.lat BETWEEN ? AND ? AND dl.lng BETWEEN ? AND ? AND dl.updated_at >= ?
      AND NOT EXISTS (SELECT 1 FROM rides r WHERE r.driver_id = dl.driver_id
                      AND r.status IN ('matched', 'arriving', 'in_progress'))
      AND NOT EXISTS (SELECT 1 FROM driver_routes dr WHERE dr.driver_id = dl.driver_id AND dr.status = 'in_progress')
    """, (cell[0] - dlat, cell[0] + dlat, cell[1] - dlng, cell[1] + dlng,
          time.time() - LOCATION_MAX_AGE_SECONDS)).fetchall()
    drivers = [{"id": r["driver_id"], "lat": r["lat"], "lng": r["lng"], "category": r["category"]} for r in rows]
    with _nearby_lock:
        if len(_nearby_cache) > 5000:
            for k in [k for k, v in _nearby_cache.items() if v[0] <= now]:
                _nearby_cache.pop(k, None)
        _nearby_cache[cell] = (now + NEARBY_CACHE_SECONDS, drivers)
    return drivers


def drivers_near(conn, lat, lng, exclude_id=None, limit=NEARBY_LIMIT):
    """Free online drivers within NEARBY_RADIUS_KM of a point, nearest first."""
    found = []
    for d in _drivers_in_cell(conn, lat, lng):
        if d["id"] == exclude_id:
            continue
        km = haversine_distance_km(lat, lng, d["lat"], d["lng"])
        if km <= NEARBY_RADIUS_KM:
            found.append((km, d))
    found.sort(key=lambda pair: pair[0])
    return [d for _, d in found[:limit]] if limit else [d for _, d in found]


@app.route("/api/drivers/nearby", methods=["GET"])
@require_auth
def nearby_drivers():
    """Online, free drivers near the rider for the home map. Anyone with an account can call
    this, so it carries no names and positions only to about 100 m: enough to show that cars
    are around, not enough to follow a particular driver."""
    try:
        lat = float(request.args.get("lat", LPU_CENTER[0]))
        lng = float(request.args.get("lng", LPU_CENTER[1]))
    except ValueError:
        raise ApiError("Invalid position")
    if not (math.isfinite(lat) and math.isfinite(lng) and -90 <= lat <= 90 and -180 <= lng <= 180):
        raise ApiError("Invalid position")
    drivers = [{"key": driver_public_key(d["id"]), "lat": round(d["lat"], 3), "lng": round(d["lng"], 3),
                "category": d["category"]}
               for d in drivers_near(get_db(), lat, lng, exclude_id=request.auth_user_id)]
    return jsonify({"drivers": drivers, "radius_km": NEARBY_RADIUS_KM})


# --- Map services -----------------------------------------------------------------------------
@app.route("/api/places/search", methods=["GET"])
@require_auth
@rate_limit(max_requests=40, window_seconds=60)
def search_places():
    """Address suggestions for a CityLink destination, best match first, inside the service area."""
    query = geo.clean_query(request.args.get("q"))
    near = None
    if request.args.get("lat") and request.args.get("lng"):
        try:
            near = (float(request.args["lat"]), float(request.args["lng"]))
        except ValueError:
            near = None
        if near and not (-90 <= near[0] <= 90 and -180 <= near[1] <= 180):
            near = None
    return jsonify({"places": geo.search_places(query, near)})


@app.route("/api/route", methods=["GET"])
@require_auth
@rate_limit(max_requests=40, window_seconds=60)
def road_route():
    """Road geometry, distance and time between two points, for drawing the trip on the map."""
    try:
        a_lat, a_lng = float(request.args["a_lat"]), float(request.args["a_lng"])
        b_lat, b_lng = float(request.args["b_lat"]), float(request.args["b_lng"])
    except (KeyError, ValueError):
        raise ApiError("Route needs a_lat, a_lng, b_lat and b_lng")
    return jsonify(geo.road_route(a_lat, a_lng, b_lat, b_lng))


# --- Rides ----------------------------------------------------------------------------------
def _trip_from_request(data):
    scope = data.get("scope", "campus_hop")
    pickup, drop, error = validate_trip(
        scope, data.get("pickup_key"), data.get("drop_key"), data.get("pickup_lat"), data.get("pickup_lng"),
        data.get("drop_lat"), data.get("drop_lng"), data.get("drop_name"),
    )
    if error:
        raise ApiError(error)
    return scope, pickup, drop


def _demand(conn, zone):
    return conn.execute("""
    SELECT COUNT(*) AS n FROM rides WHERE pickup_zone = ? AND status IN ('queued', 'matched', 'arriving')
    """, (zone,)).fetchone()["n"]


@app.route("/api/rides/quote", methods=["POST"])
@require_auth
def get_ride_quote():
    scope, pickup, drop = _trip_from_request(body())
    conn = get_db()
    sweep_stale(conn)
    balance = wallet_balance(conn, request.auth_user_id)
    demand = _demand(conn, pickup["zone"])

    nearby = drivers_near(conn, pickup["lat"], pickup["lng"], exclude_id=request.auth_user_id, limit=None)
    quotes = {}
    for service in VALID_SERVICES:
        drivers = sum(1 for d in nearby if d["category"] == service)
        fare = calculate_fare(service, scope, pickup["lat"], pickup["lng"], drop["lat"], drop["lng"], demand)
        fare["available_drivers"] = drivers
        fare["has_sufficient_balance"] = balance >= fare["total_fare"]
        fare["deficit"] = max(0.0, round(fare["total_fare"] - balance, 2))
        quotes[service] = fare

    return jsonify({"pickup": pickup, "drop": drop, "wallet_balance": round(balance, 2), "quotes": quotes})


@app.route("/api/rides/book", methods=["POST"])
@require_auth
@rate_limit(max_requests=20, window_seconds=60)
def book_ride():
    data = body()
    rider_id = request.auth_user_id
    service_type = data.get("service_type")
    if service_type not in VALID_SERVICES:
        raise ApiError("Invalid vehicle type")
    scope, pickup, drop = _trip_from_request(data)

    conn = get_db()
    sweep_stale(conn)
    begin_write(conn)
    rider = conn.execute("SELECT id, is_teacher_priority FROM users WHERE id = ?", (rider_id,)).fetchone()
    if not rider:
        raise ApiError("Rider account not recognized", 404)

    existing = conn.execute(
        "SELECT id FROM rides WHERE rider_id = ? AND status IN ('queued', 'matched', 'arriving', 'in_progress')",
        (rider_id,),
    ).fetchone()
    if existing:
        raise ApiError("You already have an active or queued ride request.", 409, "CONCURRENT_RIDE_EXISTS",
                       active_ride_id=existing["id"])

    fare_info = calculate_fare(service_type, scope, pickup["lat"], pickup["lng"], drop["lat"], drop["lng"],
                               _demand(conn, pickup["zone"]))
    total_fare = fare_info["total_fare"]
    ride_id = f"ride_{uuid.uuid4().hex[:12]}"

    # The fare is held up front, so a rider can't spend it elsewhere before the
    # ride ends. It is refunded in full if the rider cancels.
    new_bal = debit_wallet(conn, rider_id, total_fare, ride_id, f"Fare held for Ride #{ride_id[5:13]}")

    # Drivers choose which request to take, so a booking always starts in the queue.
    now = time.time()
    is_priority = 1 if rider["is_teacher_priority"] else 0
    status = "queued"
    conn.execute("""
    INSERT INTO rides (
        id, rider_id, driver_id, service_type, scope,
        pickup_name, pickup_lat, pickup_lng, pickup_zone,
        drop_name, drop_lat, drop_lng, drop_zone,
        fare, base_fare, surge_multiplier, status,
        is_priority, share_token, created_at, matched_at, start_pin
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        ride_id, rider_id, None, service_type, scope,
        pickup["name"], pickup["lat"], pickup["lng"], pickup["zone"],
        drop["name"], drop["lat"], drop["lng"], drop["zone"],
        total_fare, fare_info["base_fare"], fare_info["surge_multiplier"], status,
        is_priority, f"share_{secrets.token_urlsafe(16)}", now, None, f"{secrets.randbelow(10000):04d}",
    ))
    position = queue_position(conn, pickup["zone"], is_priority, now)
    conn.commit()
    waiting_drivers = sum(1 for d in drivers_near(conn, pickup["lat"], pickup["lng"], exclude_id=rider_id, limit=None)
                          if d["category"] == service_type)
    return jsonify({
        "success": True,
        "ride_id": ride_id,
        "status": status,
        "is_priority": bool(is_priority),
        "queue_position": position,
        "drivers_notified": waiting_drivers,
        "fare": total_fare,
        "wallet_balance": round(new_bal, 2),
    })


@app.route("/api/rides/active", methods=["GET"])
@require_auth
def get_active_ride():
    user_id = request.auth_user_id
    conn = get_db()
    sweep_stale(conn)
    ride = conn.execute("""
    SELECT r.*,
           ru.name AS rider_name, ru.phone AS rider_phone,
           ru.is_teacher_priority AS rider_is_teacher, ru.user_type AS rider_type,
           du.name AS driver_name, du.phone AS driver_phone,
           v.model AS vehicle_model, v.plate_number AS vehicle_plate, v.color AS vehicle_color,
           v.category AS vehicle_category, v.has_helmet, v.has_ac,
           dl.lat AS driver_live_lat, dl.lng AS driver_live_lng
    FROM rides r
    JOIN users ru ON r.rider_id = ru.id
    LEFT JOIN users du ON r.driver_id = du.id
    LEFT JOIN vehicles v ON r.driver_id = v.user_id AND v.is_active = 1
    LEFT JOIN driver_locations dl ON r.driver_id = dl.driver_id
    WHERE (r.rider_id = ? OR r.driver_id = ?)
      AND r.status IN ('queued', 'matched', 'arriving', 'in_progress')
    ORDER BY r.created_at DESC LIMIT 1
    """, (user_id, user_id)).fetchone()
    if not ride:
        return jsonify({"active_ride": None})

    ride_dict = dict(ride)
    ride_dict["is_rider"] = ride["rider_id"] == user_id
    ride_dict.pop("pin_attempts", None)
    if not ride_dict["is_rider"]:
        ride_dict.pop("start_pin", None)  # the driver gets it from the rider in person
    if ride_dict["status"] == "queued":
        ride_dict["queue_position"] = queue_position(conn, ride["pickup_zone"], ride["is_priority"], ride["created_at"])
    return jsonify({"active_ride": ride_dict})


@app.route("/api/driver/location", methods=["POST"])
@require_auth
@rate_limit(max_requests=30, window_seconds=60)
def report_driver_location():
    """The driver's phone reports its real GPS position. This is the only thing that moves a
    driver on the map, and what the pickup and drop-off checks rely on. Two database round
    trips in the usual case: drivers send this every 15 seconds."""
    lat, lng = get_coordinates(body())
    driver_id = request.auth_user_id
    conn = get_db()
    now = time.time()
    state = conn.execute("""
    SELECT dl.lat, dl.lng, dl.updated_at,
           (SELECT status FROM rides WHERE driver_id = ? AND status IN ('matched', 'arriving', 'in_progress') LIMIT 1)
               AS ride_status,
           (SELECT 1 FROM driver_routes WHERE driver_id = ? AND status = 'in_progress' LIMIT 1) AS hosting
    FROM (SELECT 1) AS one LEFT JOIN driver_locations dl ON dl.driver_id = ?
    """, (driver_id, driver_id, driver_id)).fetchone()
    busy = bool(state["ride_status"] or state["hosting"])
    # While driving someone, a position that jumps further than any vehicle could travel is a
    # spoofed or broken fix: it must not be able to reach a pickup or drop-off point.
    if busy and state["updated_at"] and now - state["updated_at"] < 600:
        jump_km = haversine_distance_km(state["lat"], state["lng"], lat, lng)
        hours = max(now - state["updated_at"], 1.0) / 3600
        if jump_km > 1.0 and jump_km / hours > MAX_PLAUSIBLE_SPEED_KMH:
            raise ApiError("Your GPS position jumped too far. Waiting for a steady location fix.", 409, "GPS_JUMP")
    conn.execute("""
    INSERT INTO driver_locations (driver_id, is_online, lat, lng, updated_at) VALUES (?, 0, ?, ?, ?)
    ON CONFLICT(driver_id) DO UPDATE SET lat = excluded.lat, lng = excluded.lng, updated_at = excluded.updated_at
    """, (driver_id, lat, lng, now))
    if state["hosting"]:
        conn.execute("UPDATE driver_routes SET current_lat = ?, current_lng = ? WHERE driver_id = ? AND status = 'in_progress'",
                     (lat, lng, driver_id))
    conn.commit()
    # Reaching the pickup does not start the ride: the rider's PIN does (see /start).
    return jsonify({"success": True, "ride_status": state["ride_status"]})


@app.route("/api/rides/<ride_id>/start", methods=["POST"])
@require_auth
@rate_limit(max_requests=10, window_seconds=60)
def start_ride(ride_id):
    """The driver starts the trip with the 4-digit PIN the rider tells them at pickup, so a
    ride (and its fare) can't begin without the rider actually being there."""
    pin = get_text(body(), "pin", required=True, max_len=8, label="ride PIN")
    if not re.fullmatch(r"[0-9]{4}", pin):
        raise ApiError("Enter the rider's 4-digit PIN", 400, "PIN_WRONG")
    conn = get_db()
    ride = load_ride_for_participant(conn, ride_id)
    if request.auth_user_id != ride["driver_id"]:
        raise ApiError("Only the driver can start the ride", 403)
    begin_write(conn)
    ride = conn.execute("SELECT * FROM rides WHERE id = ?", (ride_id,)).fetchone()
    if ride["status"] not in ("matched", "arriving") or ride["driver_id"] != request.auth_user_id:
        raise ApiError("This ride can't be started now", 409, "NOT_STARTABLE")
    if not driver_near(conn, ride["driver_id"], ride["pickup_lat"], ride["pickup_lng"], PICKUP_RADIUS_KM):
        raise ApiError("Start the ride at the pickup point: your phone's live location must be there.", 409, "NOT_AT_PICKUP")
    if ride["pin_attempts"] >= START_PIN_MAX_ATTEMPTS:
        raise ApiError("Too many wrong PINs. Release the ride so the rider can be picked up again.", 429, "PIN_LOCKED")
    if not hmac.compare_digest(ride["start_pin"], pin):
        conn.execute("UPDATE rides SET pin_attempts = pin_attempts + 1 WHERE id = ?", (ride_id,))
        conn.commit()
        left = START_PIN_MAX_ATTEMPTS - ride["pin_attempts"] - 1
        raise ApiError("That PIN is not correct" + (f" ({left} tries left)" if left > 0 else ""), 400, "PIN_WRONG")
    conn.execute("UPDATE rides SET status = 'in_progress', started_at = ? WHERE id = ?", (time.time(), ride_id))
    conn.commit()
    return jsonify({"success": True, "status": "in_progress"})


@app.route("/api/rides/<ride_id>/cancel", methods=["POST"])
@require_auth
def cancel_ride(ride_id):
    """Rider cancel: ride ends and the held fare is refunded in full.
    Driver cancel: the ride goes back into the queue for another driver."""
    conn = get_db()
    ride = load_ride_for_participant(conn, ride_id)
    reason = get_text(body(), "reason", max_len=120)
    begin_write(conn)

    if request.auth_user_id == ride["rider_id"]:
        refunded = cancel_ride_with_refund(conn, ride, reason or "Cancelled by rider")
        if refunded is None:
            raise ApiError("This ride can no longer be cancelled", 409, "NOT_CANCELLABLE")
        conn.commit()
        return jsonify({"success": True, "status": "cancelled", "refunded": refunded,
                        "wallet_balance": round(wallet_balance(conn, ride["rider_id"]), 2)})

    cur = conn.execute(
        "UPDATE rides SET status = 'queued', driver_id = NULL, matched_at = NULL, pin_attempts = 0, created_at = ? "
        "WHERE id = ? AND driver_id = ? AND status IN ('matched', 'arriving')",
        (time.time(), ride_id, request.auth_user_id),
    )
    if cur.rowcount != 1:
        raise ApiError("This ride can no longer be released", 409, "NOT_CANCELLABLE")
    conn.commit()
    return jsonify({"success": True, "status": "queued"})


@app.route("/api/rides/<ride_id>/complete", methods=["POST"])
@require_auth
def complete_ride(ride_id):
    conn = get_db()
    ride = load_ride_for_participant(conn, ride_id)
    if ride["status"] == "completed":
        raise ApiError("Ride already completed", 409, "ALREADY_COMPLETED")

    begin_write(conn)
    now = time.time()
    # The rider can always confirm arrival. The driver can only finish the trip once their own
    # phone has reported a position at the drop-off, so a fare can't be collected without driving.
    if request.auth_user_id == ride["driver_id"] and not driver_near(conn, ride["driver_id"], ride["drop_lat"], ride["drop_lng"], DROP_RADIUS_KM):
        raise ApiError("You can complete the ride once your phone shows you at the drop-off point "
                       "(or the rider confirms arrival).", 409, "NOT_AT_DESTINATION")
    payout = settle_ride(conn, ride, now)
    if payout is None:
        raise ApiError("A ride can only be completed after the driver has picked you up.", 409, "NOT_IN_PROGRESS")
    conn.commit()

    return jsonify({
        "success": True,
        "ride_id": ride_id,
        "fare": ride["fare"],
        "driver_payout": payout,
        "wallet_balance": round(wallet_balance(conn, request.auth_user_id), 2),
    })


@app.route("/api/rides/<ride_id>/rate", methods=["POST"])
@require_auth
@rate_limit(max_requests=20, window_seconds=60)
def rate_ride(ride_id):
    data = body()
    rating = get_number(data, "rating", lo=1, hi=5, integer=True)
    tags = get_text(data, "tags", max_len=120)
    comment = get_text(data, "comment", max_len=300)

    conn = get_db()
    ride = load_ride_for_participant(conn, ride_id)
    if ride["status"] != "completed" or not ride["driver_id"]:
        raise ApiError("You can rate a ride only after it is completed", 409, "NOT_COMPLETED")

    reviewer_id = request.auth_user_id
    reviewee_id = ride["driver_id"] if reviewer_id == ride["rider_id"] else ride["rider_id"]
    try:
        conn.execute("""
        INSERT INTO ride_reviews (id, ride_id, reviewer_id, reviewee_id, rating, tags, comment, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (str(uuid.uuid4()), ride_id, reviewer_id, reviewee_id, rating, tags, comment, time.time()))
    except IntegrityError:
        raise ApiError("You have already rated this ride", 409, "ALREADY_RATED")
    conn.commit()
    return jsonify({"success": True})


# --- SOS -------------------------------------------------------------------------------------
SOS_CONTACT_TEXTS_PER_HOUR = 3     # per destination number, across all accounts


@app.route("/api/sos/trigger", methods=["POST"])
@require_auth
@rate_limit(max_requests=3, window_seconds=60)
def trigger_sos():
    data = body()
    user_id = request.auth_user_id
    # Hourly and daily caps stop the button being used to text someone's contacts over and over.
    check_rate(f"sos-hour:{user_id}", 6, 3600)
    check_rate(f"sos-day:{user_id}", 15, 86400)

    # A missing GPS fix is reported as "location unavailable", never replaced by a made-up place.
    if data.get("lat") is not None and data.get("lng") is not None:
        lat, lng = get_coordinates(data)
    else:
        lat = lng = None
    approximate = as_bool(data.get("approximate")) or lat is None
    location_name = get_text(data, "location_name", max_len=100) or "Location unavailable"
    ride_id = data.get("ride_id")

    conn = get_db()
    user = conn.execute("SELECT name, lpu_id FROM users WHERE id = ?", (user_id,)).fetchone()
    if not user:
        raise ApiError("User not found", 404)
    contacts = [dict(c) for c in conn.execute(
        "SELECT name, phone, relationship FROM emergency_contacts WHERE user_id = ?", (user_id,)
    ).fetchall()]

    # Only attach a tracking link for an active ride this user is actually in.
    share_url = None
    linked_ride_id = None
    if isinstance(ride_id, str) and ride_id:
        ride = conn.execute(
            "SELECT id, share_token FROM rides WHERE id = ? AND (rider_id = ? OR driver_id = ?) "
            "AND status IN ('queued', 'matched', 'arriving', 'in_progress')",
            (ride_id, user_id, user_id),
        ).fetchone()
        if ride:
            linked_ride_id = ride["id"]
            share_url = tracking_url(ride["share_token"])

    blocked = set()
    for c in contacts:
        number = normalize_phone(c.get("phone"))
        if not number:
            continue
        try:
            check_rate(f"sos-dest:{phone_key(number) or number}", SOS_CONTACT_TEXTS_PER_HOUR, 3600)
        except ApiError:
            blocked.add(number)

    result = dispatch_emergency_alert(user["name"], user["lpu_id"], location_name, lat, lng, contacts, share_url,
                                      approximate=approximate, blocked_phones=blocked)

    alert_id = f"sos_{uuid.uuid4().hex[:8]}"
    note = "SOS triggered from app" + (" (approximate location)" if approximate and lat is not None else "")
    if lat is None:
        note = "SOS triggered from app (no location available)"
    conn.execute("""
    INSERT INTO sos_alerts (id, ride_id, user_id, lat, lng, location_name, status, notified_contacts_count, notes, created_at)
    VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)
    """, (alert_id, linked_ride_id, user_id, lat if lat is not None else 0.0, lng if lng is not None else 0.0,
          location_name, result["contacts_sent"], note, time.time()))
    conn.commit()

    return jsonify({
        "success": True,
        "alert_id": alert_id,
        "status": "RECORDED",
        "sos_message": result["sos_message"],
        "contacts_notified": result["contacts_dispatched"],
        "contacts_sent": result["contacts_sent"],
        "sms_gateway_configured": sms_gateway_configured(),
        "campus_security_dispatch": result["campus_security_dispatch"],
        "campus_security_hotline": CAMPUS_SECURITY_PHONE,
        "tracking_url": share_url,
    })


@app.route("/api/sos/<alert_id>/resolve", methods=["POST"])
@require_auth
def resolve_own_sos(alert_id):
    """The user marks their own alert as resolved ("I'm safe")."""
    conn = get_db()
    cur = conn.execute(
        "UPDATE sos_alerts SET status = 'resolved', resolved_at = ? WHERE id = ? AND user_id = ? AND status != 'resolved'",
        (time.time(), alert_id, request.auth_user_id),
    )
    if cur.rowcount != 1:
        raise ApiError("Alert not found or already resolved", 404)
    conn.commit()
    return jsonify({"success": True, "status": "resolved"})


# --- Public live-tracking link -----------------------------------------------------------
@app.route("/api/rides/share/<share_token>", methods=["GET"])
@rate_limit(max_requests=60, window_seconds=60)
def get_shared_ride(share_token):
    """Works only while the ride is active; exposes no phone numbers or IDs."""
    ride = get_db().execute("""
    SELECT r.service_type, r.pickup_name, r.pickup_lat, r.pickup_lng, r.drop_name, r.drop_lat, r.drop_lng,
           r.status, r.created_at, ru.name AS rider_name, du.name AS driver_name,
           v.model AS vehicle_model, v.plate_number AS vehicle_plate, v.color AS vehicle_color,
           dl.lat AS driver_lat, dl.lng AS driver_lng
    FROM rides r
    JOIN users ru ON r.rider_id = ru.id
    LEFT JOIN users du ON r.driver_id = du.id
    LEFT JOIN vehicles v ON r.driver_id = v.user_id AND v.is_active = 1
    LEFT JOIN driver_locations dl ON r.driver_id = dl.driver_id
    WHERE r.share_token = ? AND r.status IN ('queued', 'matched', 'arriving', 'in_progress')
    """, (share_token,)).fetchone()
    if not ride:
        raise ApiError("This tracking link has expired or is invalid", 404)
    ride_dict = dict(ride)
    ride_dict["rider_name"] = first_name(ride_dict["rider_name"])
    ride_dict["driver_name"] = first_name(ride_dict["driver_name"], None)
    try:  # the road path, so the shared page draws the real route instead of two dots
        ride_dict["route_points"] = geo.road_route(ride_dict["pickup_lat"], ride_dict["pickup_lng"],
                                                   ride_dict["drop_lat"], ride_dict["drop_lng"])["points"]
    except geo.GeoError:
        ride_dict["route_points"] = []
    return jsonify({"ride": ride_dict})


# --- Driver ----------------------------------------------------------------------------------
@app.route("/api/driver/toggle-online", methods=["POST"])
@require_auth
def toggle_driver_online():
    data = body()
    driver_id = request.auth_user_id
    is_online = 1 if data.get("is_online", True) else 0
    conn = get_db()
    if is_online:
        require_vehicle(conn, driver_id)

    has_fix = data.get("lat") is not None and data.get("lng") is not None
    lat, lng = get_coordinates(data) if has_fix else LPU_CENTER
    # Only a real position report refreshes the location timestamp; going online alone doesn't,
    # so a driver whose phone stops reporting drops out of matching after a couple of minutes.
    conn.execute("""
    INSERT INTO driver_locations (driver_id, is_online, lat, lng, updated_at)
    VALUES (:driver, :online, :lat, :lng, :now)
    ON CONFLICT(driver_id) DO UPDATE SET
        is_online = excluded.is_online,
        lat = CASE WHEN :fix = 1 THEN excluded.lat ELSE driver_locations.lat END,
        lng = CASE WHEN :fix = 1 THEN excluded.lng ELSE driver_locations.lng END,
        updated_at = CASE WHEN :fix = 1 THEN excluded.updated_at ELSE driver_locations.updated_at END
    """, {"driver": driver_id, "online": is_online, "lat": lat, "lng": lng,
          "now": time.time() if has_fix else 0.0, "fix": 1 if has_fix else 0})
    conn.commit()
    return jsonify({"success": True, "is_online": bool(is_online)})


@app.route("/api/driver/requests", methods=["GET"])
@require_auth
def get_driver_requests():
    """Queued requests this driver's vehicle can serve, teachers first."""
    conn = get_db()
    sweep_stale(conn)
    # Vehicle, online status and last position in one round trip.
    me = conn.execute("""
    SELECT v.category, dl.is_online, dl.lat, dl.lng, dl.updated_at
    FROM vehicles v LEFT JOIN driver_locations dl ON dl.driver_id = v.user_id
    WHERE v.user_id = ? AND v.is_active = 1
    """, (request.auth_user_id,)).fetchone()
    if not me:
        raise ApiError("Register a vehicle in your profile before driving.", 403, "VEHICLE_REQUIRED")
    if not me["is_online"]:
        return jsonify({"requests": [], "offline": True})
    rows = conn.execute("""
    SELECT r.id, r.service_type, r.scope, r.pickup_name, r.pickup_zone, r.drop_name, r.fare,
           r.pickup_lat, r.pickup_lng, r.drop_lat, r.drop_lng,
           r.is_priority, r.created_at, u.name AS rider_name, u.user_type AS rider_type
    FROM rides r
    JOIN users u ON r.rider_id = u.id
    WHERE r.status = 'queued' AND r.service_type = ? AND r.rider_id != ?
    ORDER BY r.is_priority DESC, r.created_at ASC
    LIMIT 25
    """, (me["category"], request.auth_user_id)).fetchall()
    has_fix = time.time() - me["updated_at"] <= LOCATION_MAX_AGE_SECONDS
    requests_out = []
    for r in rows:
        item = dict(r)
        item["trip_km"] = round(haversine_distance_km(item["pickup_lat"], item["pickup_lng"], item["drop_lat"], item["drop_lng"]), 1)
        item["distance_to_pickup_km"] = round(haversine_distance_km(me["lat"], me["lng"], item.pop("pickup_lat"), item.pop("pickup_lng")), 1) if has_fix else None
        item.pop("drop_lat"), item.pop("drop_lng")
        item["rider_name"] = first_name(item["rider_name"])
        item["driver_payout"] = driver_payout_for(item["scope"], item["fare"])
        requests_out.append(item)
    return jsonify({"requests": requests_out})


@app.route("/api/driver/accept", methods=["POST"])
@require_auth
@rate_limit(max_requests=20, window_seconds=60)
def driver_accept_request():
    driver_id = request.auth_user_id
    ride_id = body().get("ride_id")
    if not isinstance(ride_id, str) or not ride_id:
        raise ApiError("ride_id is required")

    conn = get_db()
    vehicle = require_vehicle(conn, driver_id)
    begin_write(conn)
    online = conn.execute("SELECT is_online FROM driver_locations WHERE driver_id = ?", (driver_id,)).fetchone()
    if not (online and online["is_online"]):
        raise ApiError("Go online before accepting rides.", 409, "DRIVER_OFFLINE")
    if driver_is_busy(conn, driver_id):
        raise ApiError("Finish your current ride before accepting another", 409, "DRIVER_BUSY")

    cur = conn.execute("""
    UPDATE rides SET driver_id = ?, status = 'arriving', matched_at = ?
    WHERE id = ? AND status = 'queued' AND service_type = ? AND rider_id != ?
    """, (driver_id, time.time(), ride_id, vehicle["category"], driver_id))
    if cur.rowcount != 1:
        raise ApiError("This request is no longer available for your vehicle", 409, "NOT_AVAILABLE")

    conn.commit()
    return jsonify({"success": True, "ride_id": ride_id, "status": "arriving"})


@app.route("/api/driver/earnings", methods=["GET"])
@require_auth
def get_driver_earnings():
    driver_id = request.auth_user_id
    conn = get_db()
    summary = conn.execute("""
    SELECT COUNT(*) AS total_trips, COALESCE(SUM(amount), 0.0) AS total_earnings
    FROM wallet_transactions WHERE user_id = ? AND type = 'driver_payout'
    """, (driver_id,)).fetchone()
    rating = conn.execute("""
    SELECT COALESCE(AVG(rating), 5.0) AS avg_rating, COUNT(*) AS review_count
    FROM ride_reviews WHERE reviewee_id = ?
    """, (driver_id,)).fetchone()
    loc = conn.execute("SELECT is_online FROM driver_locations WHERE driver_id = ?", (driver_id,)).fetchone()
    return jsonify({
        "total_trips": summary["total_trips"],
        "total_earnings": round(summary["total_earnings"], 2),
        "avg_rating": round(rating["avg_rating"], 1),
        "review_count": rating["review_count"],
        "is_online": bool(loc and loc["is_online"]),
        "has_vehicle": active_vehicle(conn, driver_id) is not None,
        "withdrawable": withdrawable_balance(conn, driver_id),
    })


UPI_ID_RE = re.compile(r"^[A-Za-z0-9._-]{2,64}@[A-Za-z][A-Za-z0-9.-]{1,32}$")
MIN_PAYOUT = 1.0


@app.route("/api/driver/payouts", methods=["GET"])
@require_auth
def list_driver_payouts():
    conn = get_db()
    rows = conn.execute("""
    SELECT id, amount, upi_id, status, utr, note, created_at, settled_at FROM driver_payouts
    WHERE driver_id = ? ORDER BY created_at DESC LIMIT 20
    """, (request.auth_user_id,)).fetchall()
    return jsonify({"withdrawable": withdrawable_balance(conn, request.auth_user_id), "payouts": [dict(r) for r in rows]})


@app.route("/api/driver/payouts", methods=["POST"])
@require_auth
@rate_limit(max_requests=5, window_seconds=60)
def request_driver_payout():
    """Moves earnings out of the wallet into a payout request. The admin sends the money by UPI
    and marks it paid; a rejected request returns the money to the wallet."""
    data = body()
    upi_id = get_text(data, "upi_id", required=True, max_len=100, label="UPI ID")
    if not UPI_ID_RE.match(upi_id):
        raise ApiError("Enter a valid UPI ID, like name@okaxis")
    conn = get_db()
    begin_write(conn)
    driver_id = request.auth_user_id
    if conn.execute("SELECT 1 FROM driver_payouts WHERE driver_id = ? AND status = 'requested'", (driver_id,)).fetchone():
        raise ApiError("You already have a payout request waiting to be paid.", 409, "PAYOUT_PENDING")
    available = withdrawable_balance(conn, driver_id)
    if available < MIN_PAYOUT:
        raise ApiError("You have no ride earnings to withdraw yet.", 402, "INSUFFICIENT_EARNINGS", withdrawable=available)
    amount = get_number(data, "amount", default=available, lo=MIN_PAYOUT, hi=100000, label="amount")
    amount = round(amount, 2)
    if amount > available:
        raise ApiError(f"You can withdraw up to ₹{available:.2f} of your ride earnings.", 402, "INSUFFICIENT_EARNINGS",
                       withdrawable=available)
    payout_id = f"wd_{uuid.uuid4().hex[:12]}"
    now = time.time()
    new_bal = debit_wallet(conn, driver_id, amount, payout_id, f"Payout to {upi_id}", tx_type="withdrawal")
    conn.execute("""
    INSERT INTO driver_payouts (id, driver_id, amount, upi_id, status, created_at) VALUES (?, ?, ?, ?, 'requested', ?)
    """, (payout_id, driver_id, amount, upi_id, now))
    conn.commit()
    return jsonify({"success": True, "payout_id": payout_id, "amount": amount, "status": "requested",
                    "wallet_balance": round(new_bal, 2)})


# --- CityLink scheduled carpools -------------------------------------------------------------
@app.route("/api/routes/scheduled", methods=["GET"])
@require_auth
def get_scheduled_routes():
    """Every open carpool, visible to every user: first names only, no phone numbers or IDs."""
    user_id = request.auth_user_id
    conn = get_db()
    sweep_stale(conn)
    routes = [dict(r) for r in conn.execute("""
    SELECT dr.id, dr.driver_id, dr.origin, dr.destination, dr.departure_time, dr.available_seats, dr.total_seats,
           dr.price_per_seat, dr.notes, dr.status, dr.created_at, dr.started_at, u.name AS driver_name,
           v.model AS vehicle_model, v.category AS vehicle_category, v.plate_number AS vehicle_plate
    FROM driver_routes dr
    JOIN users u ON dr.driver_id = u.id
    LEFT JOIN vehicles v ON dr.driver_id = v.user_id AND v.is_active = 1
    WHERE dr.status IN ('open', 'pinned', 'in_progress')
    ORDER BY (CASE dr.status WHEN 'pinned' THEN 0 WHEN 'in_progress' THEN 1 ELSE 2 END), dr.created_at DESC
    """).fetchall()]

    passengers = {r["id"]: [] for r in routes}
    for p in conn.execute("""
    SELECT rb.route_id, rb.seats, rb.rider_id, u.name FROM route_bookings rb
    JOIN users u ON rb.rider_id = u.id
    JOIN driver_routes dr ON dr.id = rb.route_id AND dr.status IN ('open', 'pinned', 'in_progress')
    WHERE rb.status IN ('confirmed', 'in_progress')
    """).fetchall():
        if p["route_id"] in passengers:
            passengers[p["route_id"]].append(p)
    for r in routes:
        booked = passengers[r["id"]]
        r["is_host"] = r.pop("driver_id") == user_id
        r["has_joined"] = any(p["rider_id"] == user_id for p in booked)
        r["driver_name"] = first_name(r["driver_name"])
        r["passengers"] = [{"passenger_name": first_name(p["name"]), "seats": p["seats"]} for p in booked]
    return jsonify({"routes": routes})


@app.route("/api/routes/plan", methods=["POST"])
@require_auth
@rate_limit(max_requests=10, window_seconds=60)
def plan_future_route():
    data = body()
    driver_id = request.auth_user_id
    conn = get_db()
    vehicle = require_vehicle(conn, driver_id)

    origin_key, origin = find_landmark(data.get("origin") or "uni_mall")
    dest_key, dest = find_landmark(data.get("destination"))
    if not origin or not dest:
        raise ApiError("Choose the origin and destination from the list of locations")
    if origin_key == dest_key:
        raise ApiError("Origin and destination must be different")
    if not (is_city_landmark(origin) or is_city_landmark(dest)):
        raise ApiError("A CityLink route needs an off-campus origin or destination")

    departure_time = get_text(data, "departure_time", required=True, max_len=40, label="departure time")
    departs = parse_departure(departure_time)
    if departs is None:
        raise ApiError("Pick the departure date and time")
    if departs < time.time() - 300:
        raise ApiError("That departure time has already passed")
    if departs > time.time() + 30 * 24 * 3600:
        raise ApiError("Departure can be at most 30 days ahead")
    max_seats = max(1, vehicle["capacity"])
    total_seats = get_number(data, "total_seats", data.get("available_seats", 1), lo=1, hi=max_seats,
                             integer=True, label="seats")
    price_per_seat = get_number(data, "price_per_seat", 80, lo=10, hi=2000, label="price per seat")
    notes = get_text(data, "notes", max_len=200)

    route_id = f"route_{uuid.uuid4().hex[:10]}"
    conn.execute("""
    INSERT INTO driver_routes (
        id, driver_id, origin, origin_lat, origin_lng, destination, destination_lat, destination_lng,
        departure_time, available_seats, total_seats, price_per_seat, notes, status,
        current_lat, current_lng, created_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)
    """, (
        route_id, driver_id, origin["name"], origin["lat"], origin["lng"], dest["name"], dest["lat"], dest["lng"],
        departure_time, total_seats, total_seats, round(price_per_seat, 2), notes,
        origin["lat"], origin["lng"], time.time(),
    ))
    conn.commit()
    return jsonify({"success": True, "route_id": route_id, "status": "open", "total_seats": total_seats})


@app.route("/api/routes/<route_id>/join", methods=["POST"])
@require_auth
@rate_limit(max_requests=20, window_seconds=60)
def join_route(route_id):
    rider_id = request.auth_user_id
    seats = get_number(body(), "seats", 1, lo=1, hi=6, integer=True)

    conn = get_db()
    begin_write(conn)
    route = conn.execute("SELECT * FROM driver_routes WHERE id = ?", (route_id,)).fetchone()
    if not route or route["status"] not in ("open", "pinned"):
        raise ApiError("Route is no longer available", 404)
    if route["driver_id"] == rider_id:
        raise ApiError("You can't book a seat on your own route")
    if conn.execute("SELECT 1 FROM route_bookings WHERE route_id = ? AND rider_id = ? AND status = 'confirmed'",
                    (route_id, rider_id)).fetchone():
        raise ApiError("You have already booked this route", 409, "ALREADY_BOOKED")

    cur = conn.execute("""
    UPDATE driver_routes
    SET available_seats = available_seats - ?,
        status = CASE WHEN available_seats - ? = 0 THEN 'pinned' ELSE status END
    WHERE id = ? AND status = 'open' AND available_seats >= ?
    """, (seats, seats, route_id, seats))
    if cur.rowcount != 1:
        if route["status"] == "pinned" or route["available_seats"] == 0:
            raise ApiError("This trip is full", 409, "NOT_ENOUGH_SEATS")
        raise ApiError(f"Only {route['available_seats']} seat(s) remaining", 409, "NOT_ENOUGH_SEATS")

    fare = round(route["price_per_seat"] * seats, 2)
    booking_id = f"bk_{uuid.uuid4().hex[:10]}"
    new_bal = debit_wallet(conn, rider_id, fare, booking_id, f"Booked {seats} seat(s) to {route['destination']}")
    conn.execute("""
    INSERT INTO route_bookings (id, route_id, rider_id, seats, fare_paid, status, created_at)
    VALUES (?, ?, ?, ?, ?, 'confirmed', ?)
    """, (booking_id, route_id, rider_id, seats, fare, time.time()))
    updated = conn.execute("SELECT available_seats, status FROM driver_routes WHERE id = ?", (route_id,)).fetchone()
    conn.commit()

    return jsonify({
        "success": True,
        "booking_id": booking_id,
        "fare_paid": fare,
        "seats_booked": seats,
        "remaining_seats": updated["available_seats"],
        "status": updated["status"],
        "is_pinned": updated["status"] == "pinned",
        "wallet_balance": round(new_bal, 2),
    })


@app.route("/api/routes/<route_id>/leave", methods=["POST"])
@require_auth
def leave_route(route_id):
    """A passenger cancels their booking before departure and is refunded."""
    rider_id = request.auth_user_id
    conn = get_db()
    begin_write(conn)
    route = conn.execute("SELECT id, status FROM driver_routes WHERE id = ?", (route_id,)).fetchone()
    if not route or route["status"] not in ("open", "pinned"):
        raise ApiError("This trip has already started or ended", 409)
    booking = conn.execute(
        "SELECT * FROM route_bookings WHERE route_id = ? AND rider_id = ? AND status = 'confirmed'", (route_id, rider_id)
    ).fetchone()
    if not booking:
        raise ApiError("You have no booking on this route", 404)

    conn.execute("UPDATE route_bookings SET status = 'cancelled' WHERE id = ?", (booking["id"],))
    conn.execute("UPDATE driver_routes SET available_seats = available_seats + ?, status = 'open' WHERE id = ?",
                 (booking["seats"], route_id))
    new_bal = credit_wallet(conn, rider_id, booking["fare_paid"], "refund", booking["id"], "Refund for cancelled carpool seat")
    conn.commit()
    return jsonify({"success": True, "refunded": booking["fare_paid"], "wallet_balance": round(new_bal, 2)})


@app.route("/api/routes/<route_id>/cancel", methods=["POST"])
@require_auth
def cancel_route(route_id):
    """The host cancels before departure; every passenger is refunded."""
    conn = get_db()
    begin_write(conn)
    route = conn.execute("SELECT * FROM driver_routes WHERE id = ?", (route_id,)).fetchone()
    if not route:
        raise ApiError("Route not found", 404)
    if route["driver_id"] != request.auth_user_id:
        raise ApiError("Only the route host can cancel this trip", 403)
    refunded = cancel_route_with_refunds(conn, route, "host cancelled trip")
    if refunded is None:
        raise ApiError("This trip has already started or ended", 409)
    conn.commit()
    return jsonify({"success": True, "status": "cancelled", "refunded_bookings": refunded})


@app.route("/api/routes/<route_id>/start", methods=["POST"])
@require_auth
def start_scheduled_route(route_id):
    conn = get_db()
    begin_write(conn)
    route = conn.execute("SELECT * FROM driver_routes WHERE id = ?", (route_id,)).fetchone()
    if not route:
        raise ApiError("Route not found", 404)
    if route["driver_id"] != request.auth_user_id:
        raise ApiError("Only the route host can start this trip", 403)
    if not conn.execute("SELECT 1 FROM route_bookings WHERE route_id = ? AND status = 'confirmed'", (route_id,)).fetchone():
        raise ApiError("No passengers have booked this route yet")
    if driver_is_busy(conn, route["driver_id"]):
        raise ApiError("Finish the ride you are driving before starting this trip.", 409, "DRIVER_BUSY")
    if not driver_near(conn, route["driver_id"], route["origin_lat"], route["origin_lng"], ROUTE_ORIGIN_RADIUS_KM):
        raise ApiError("Start the trip from the pickup point: your phone's live location must be near it.",
                       409, "NOT_AT_ORIGIN")

    now = time.time()
    cur = conn.execute(
        "UPDATE driver_routes SET status = 'in_progress', started_at = ? WHERE id = ? AND status IN ('open', 'pinned')",
        (now, route_id),
    )
    if cur.rowcount != 1:
        raise ApiError("This trip has already started or ended", 409)
    conn.execute("UPDATE route_bookings SET status = 'in_progress' WHERE route_id = ? AND status = 'confirmed'", (route_id,))
    conn.commit()
    return jsonify({"success": True, "route_id": route_id, "status": "in_progress"})


@app.route("/api/routes/<route_id>/live", methods=["GET"])
@require_auth
def get_live_cockpit(route_id):
    conn = get_db()
    route = conn.execute("""
    SELECT dr.*, u.name AS driver_name, u.phone AS driver_phone,
           v.model AS vehicle_model, v.plate_number AS vehicle_plate, v.category AS vehicle_category
    FROM driver_routes dr
    JOIN users u ON dr.driver_id = u.id
    LEFT JOIN vehicles v ON dr.driver_id = v.user_id AND v.is_active = 1
    WHERE dr.id = ?
    """, (route_id,)).fetchone()
    if not route:
        raise ApiError("Route not found", 404)
    route_participant_check(conn, route, request.auth_user_id)

    route_dict = dict(route)
    is_host = route_dict.pop("driver_id") == request.auth_user_id
    route_dict["is_host"] = is_host
    route_dict["passengers"] = [dict(p) for p in conn.execute("""
    SELECT rb.id AS booking_id, rb.seats, rb.fare_paid, rb.status AS booking_status,
           u.name AS passenger_name, u.phone AS passenger_phone
    FROM route_bookings rb JOIN users u ON rb.rider_id = u.id
    WHERE rb.route_id = ? AND rb.status IN ('confirmed', 'in_progress', 'completed')
    """, (route_id,)).fetchall()]

    if not is_host:
        # Fellow passengers get names only; contact numbers are for the host.
        for p in route_dict["passengers"]:
            p.pop("passenger_phone", None)
    c_lat = route_dict["current_lat"] or route_dict["origin_lat"]
    c_lng = route_dict["current_lng"] or route_dict["origin_lng"]
    dist = haversine_distance_km(c_lat, c_lng, route_dict["destination_lat"], route_dict["destination_lng"])
    route_dict["distance_remaining_km"] = round(dist, 2)
    route_dict["eta_minutes"] = max(3, int(dist * 2.5) + 2)
    return jsonify({"cockpit": route_dict})


@app.route("/api/routes/<route_id>/complete", methods=["POST"])
@require_auth
def complete_scheduled_route(route_id):
    conn = get_db()
    begin_write(conn)
    route = conn.execute("SELECT * FROM driver_routes WHERE id = ?", (route_id,)).fetchone()
    if not route:
        raise ApiError("Route not found", 404)
    if route["driver_id"] != request.auth_user_id:
        raise ApiError("Only the route host can complete this trip", 403)
    if not driver_near(conn, route["driver_id"], route["destination_lat"], route["destination_lng"], ROUTE_DEST_RADIUS_KM):
        raise ApiError("Complete the trip once your phone shows you at the destination.", 409, "NOT_AT_DESTINATION")

    settled = settle_route(conn, route, time.time())
    if settled is None:
        raise ApiError("Only a trip that is in progress can be completed", 409, "NOT_IN_PROGRESS")
    total_revenue, payout = settled
    conn.commit()
    return jsonify({"success": True, "status": "completed", "total_revenue": total_revenue, "driver_payout": payout})


@app.route("/api/routes/active", methods=["GET"])
@require_auth
def get_active_route():
    """The carpool this user is on right now, as host or passenger, so the app can reopen its live view."""
    user_id = request.auth_user_id
    route = get_db().execute("""
    SELECT id FROM driver_routes
    WHERE status = 'in_progress' AND (driver_id = ? OR id IN (
        SELECT route_id FROM route_bookings WHERE rider_id = ? AND status = 'in_progress'))
    ORDER BY started_at DESC LIMIT 1
    """, (user_id, user_id)).fetchone()
    return jsonify({"route_id": route["id"] if route else None})


# --- Wallet & payments -----------------------------------------------------------------------
@app.route("/api/wallet", methods=["GET"])
@require_auth
def get_wallet():
    conn = get_db()
    txs = conn.execute("""
    SELECT id, amount, type, description, balance_after, created_at FROM wallet_transactions
    WHERE user_id = ? ORDER BY created_at DESC LIMIT 20
    """, (request.auth_user_id,)).fetchall()
    return jsonify({
        "balance": round(wallet_balance(conn, request.auth_user_id), 2),
        "currency": "INR",
        "transactions": [dict(t) for t in txs],
    })


def _topup_amount(data):
    return round(get_number(data, "amount", lo=payments.MIN_TOPUP, hi=payments.MAX_TOPUP, label="amount"), 2)


@app.route("/api/payments/upi/create-qr", methods=["POST"])
@require_auth
@rate_limit(max_requests=20, window_seconds=60)
def upi_create_qr():
    amount = _topup_amount(body())
    if not payments.upi_vpa():
        raise ApiError("UPI QR top-ups are not set up on this server", 503, "PAYMENTS_DISABLED")
    conn = get_db()
    order_id = payments.create_payment_order(conn, request.auth_user_id, "upi", amount)
    conn.commit()
    return jsonify({"success": True, **payments.generate_upi_qr(order_id, amount)})


@app.route("/api/payments/upi/confirm", methods=["POST"])
@require_auth
@rate_limit(max_requests=10, window_seconds=60)
def upi_confirm():
    """The user says they paid. UPI gives no client-side proof of payment, so the order is
    credited only after an admin checks the bank statement. The amount always comes from the
    stored order, never the request."""
    reference_id = body().get("reference_id")
    if not isinstance(reference_id, str) or not reference_id:
        raise ApiError("reference_id is required")

    conn = get_db()
    begin_write(conn)
    order = conn.execute(
        "SELECT * FROM payment_orders WHERE id = ? AND user_id = ? AND provider = 'upi'",
        (reference_id, request.auth_user_id),
    ).fetchone()
    if not order:
        raise ApiError("Payment reference not found", 404)
    if order["status"] not in ("created", "awaiting_verification"):
        raise ApiError("This payment has already been processed", 409)
    conn.execute("UPDATE payment_orders SET status = 'awaiting_verification' WHERE id = ?", (order["id"],))
    conn.commit()
    return jsonify({
        "success": True,
        "pending": True,
        "reference_id": order["id"],
        "amount": order["amount"],
        "message": "Thanks! Your top-up will be added once the payment is verified.",
    }), 202


@app.route("/api/payments/razorpay/create-order", methods=["POST"])
@require_auth
@rate_limit(max_requests=15, window_seconds=60)
def razorpay_create():
    amount = _topup_amount(body())
    if not payments.razorpay_configured():
        raise ApiError("Card payments are not configured on this server", 503, "PAYMENTS_DISABLED")
    provider_order_id = payments.create_razorpay_remote_order(amount, f"rcpt_{uuid.uuid4().hex[:12]}")
    conn = get_db()
    payments.create_payment_order(conn, request.auth_user_id, "razorpay", amount, provider_order_id)
    conn.commit()
    return jsonify({
        "success": True,
        "order_id": provider_order_id,
        "amount": payments.to_paise(amount),
        "currency": "INR",
        "key_id": os.environ.get("RAZORPAY_KEY_ID", "").strip(),
        "merchant_name": payments.MERCHANT_NAME,
    })


@app.route("/api/payments/razorpay/verify", methods=["POST"])
@require_auth
@rate_limit(max_requests=15, window_seconds=60)
def razorpay_verify():
    data = body()
    provider_order_id = data.get("order_id")
    payment_id = data.get("payment_id")
    signature = data.get("signature")
    if not all(isinstance(v, str) and v and len(v) <= 128 for v in (provider_order_id, payment_id, signature)):
        raise ApiError("Invalid payment verification parameters")

    conn = get_db()
    begin_write(conn)
    order = conn.execute(
        "SELECT * FROM payment_orders WHERE provider_order_id = ? AND user_id = ? AND provider = 'razorpay'",
        (provider_order_id, request.auth_user_id),
    ).fetchone()
    if not order:
        raise ApiError("Payment order not found", 404)
    if not payments.verify_razorpay_signature(provider_order_id, payment_id, signature):
        raise ApiError("Payment signature verification failed", 400, "INVALID_SIGNATURE")

    result = payments.settle_razorpay_order(conn, order, payment_id)
    conn.commit()
    return jsonify(result)


RAZORPAY_SYNC_WINDOW_SECONDS = 24 * 3600
RAZORPAY_SYNC_MAX_ORDERS = 3


@app.route("/api/payments/razorpay/sync", methods=["POST"])
@require_auth
@rate_limit(max_requests=20, window_seconds=60)
def razorpay_sync():
    """Asks Razorpay about this user's recent unpaid orders and credits the ones that were
    paid. Covers a payer whose browser never came back from the payment (closed tab, UPI app
    switch) on servers where no webhook is set up."""
    conn = get_db()
    credited = []
    if payments.razorpay_configured():
        pending = conn.execute("""
        SELECT id, provider_order_id, amount FROM payment_orders
        WHERE user_id = ? AND provider = 'razorpay' AND status = 'created' AND created_at > ?
        ORDER BY created_at DESC LIMIT ?
        """, (request.auth_user_id, time.time() - RAZORPAY_SYNC_WINDOW_SECONDS, RAZORPAY_SYNC_MAX_ORDERS)).fetchall()
        conn.rollback()  # no transaction is held open while talking to Razorpay
        for row in pending:
            try:
                payment_id = payments.fetch_captured_payment(row["provider_order_id"], row["amount"])
            except PaymentError:
                break  # gateway unreachable: the next sync or the webhook picks it up
            if not payment_id:
                continue
            begin_write(conn)
            order = conn.execute("SELECT * FROM payment_orders WHERE id = ?", (row["id"],)).fetchone()
            try:
                result = payments.settle_razorpay_order(conn, order, payment_id)
            except PaymentError:
                conn.rollback()
                continue
            conn.commit()
            if not result.get("already_credited"):
                credited.append({"reference_id": order["id"], "amount": order["amount"]})
    return jsonify({
        "success": True,
        "credited": credited,
        "amount_credited": round(sum(c["amount"] for c in credited), 2),
        "new_balance": round(wallet_balance(conn, request.auth_user_id), 2),
    })


@app.route("/api/payments/razorpay/webhook", methods=["POST"])
def razorpay_webhook():
    """Razorpay calls this when a payment is captured, so the wallet is credited even if the
    payer's browser never reports back. There is no login: the caller is proven by the HMAC of
    the raw body, and the amount credited is always the stored order's."""
    if not payments.webhook_configured():
        raise ApiError("Not found", 404)
    raw = request.get_data()
    if not payments.verify_webhook_signature(raw, request.headers.get("X-Razorpay-Signature", "")):
        raise ApiError("Invalid webhook signature", 400, "INVALID_SIGNATURE")
    try:
        event = json.loads(raw)
        payment = event["payload"]["payment"]["entity"] if event.get("event") in ("payment.captured", "order.paid") else None
    except (ValueError, KeyError, TypeError, AttributeError):
        raise ApiError("Malformed webhook body")
    # Anything else Razorpay sends is acknowledged so it is not retried.
    if not isinstance(payment, dict) or payment.get("status") != "captured":
        return jsonify({"success": True, "ignored": True})
    provider_order_id, payment_id = payment.get("order_id"), payment.get("id")
    if not isinstance(provider_order_id, str) or not isinstance(payment_id, str):
        return jsonify({"success": True, "ignored": True})

    conn = get_db()
    begin_write(conn)
    order = conn.execute(
        "SELECT * FROM payment_orders WHERE provider_order_id = ? AND provider = 'razorpay'", (provider_order_id,)
    ).fetchone()
    if not order:
        return jsonify({"success": True, "ignored": True})
    if payment.get("amount") != payments.to_paise(order["amount"]):
        log.error("Razorpay webhook amount mismatch for order %s (payment %s)", order["id"], payment_id)
        return jsonify({"success": True, "ignored": True})
    try:
        payments.settle_razorpay_order(conn, order, payment_id)
    except PaymentError:
        # Already settled by the browser callback or an earlier delivery.
        return jsonify({"success": True, "ignored": True})
    conn.commit()
    return jsonify({"success": True})


# --- Admin: manual UPI reconciliation ---------------------------------------------------------
def require_admin(f):
    """Enabled only when ADMIN_API_KEY (32+ chars) is set; otherwise these
    routes behave as if they don't exist."""
    @wraps(f)
    def wrapped(*args, **kwargs):
        expected = os.environ.get("ADMIN_API_KEY", "")
        if len(expected) < 32:
            raise ApiError("Not found", 404)
        check_rate(f"admin:{request.remote_addr}", 30, 60)
        supplied = request.headers.get("X-Admin-Key", "")
        if not secrets.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8")):
            raise ApiError("Forbidden", 403)
        return f(*args, **kwargs)
    return wrapped


@app.route("/api/admin/payments/pending", methods=["GET"])
@require_admin
def admin_pending_payments():
    rows = get_db().execute("""
    SELECT p.id, p.amount, p.created_at, u.lpu_id, u.name FROM payment_orders p
    JOIN users u ON u.id = p.user_id
    WHERE p.provider = 'upi' AND p.status = 'awaiting_verification' ORDER BY p.created_at ASC
    """).fetchall()
    return jsonify({"pending": [dict(r) for r in rows]})


@app.route("/api/admin/payments/<order_id>/approve", methods=["POST"])
@require_admin
def admin_approve_payment(order_id):
    """Call after matching the UPI reference against the bank statement."""
    conn = get_db()
    begin_write(conn)
    order = conn.execute("SELECT id FROM payment_orders WHERE id = ? AND provider = 'upi'", (order_id,)).fetchone()
    if not order:
        raise ApiError("Payment order not found", 404)
    result = payments.credit_order(conn, order_id, "upi")
    conn.commit()
    return jsonify(result)


@app.route("/api/admin/earnings", methods=["GET"])
@require_admin
def admin_platform_earnings():
    """The platform's cut (fare minus driver payout) from every completed trip."""
    conn = get_db()
    total = conn.execute("""
    SELECT COUNT(*) AS trips, COALESCE(SUM(amount), 0.0) AS earned, COALESCE(SUM(fare), 0.0) AS collected,
           COALESCE(SUM(driver_payout), 0.0) AS paid_to_drivers
    FROM platform_earnings
    """).fetchone()
    by_scope = conn.execute("""
    SELECT scope, COUNT(*) AS trips, COALESCE(SUM(amount), 0.0) AS earned FROM platform_earnings GROUP BY scope
    """).fetchall()
    recent = conn.execute("""
    SELECT source, reference_id, scope, fare, driver_payout, amount, created_at FROM platform_earnings
    ORDER BY created_at DESC LIMIT 50
    """).fetchall()
    owed = conn.execute("SELECT COALESCE(SUM(amount), 0.0) AS total FROM driver_payouts WHERE status = 'requested'").fetchone()
    return jsonify({
        "trips": total["trips"],
        "platform_earned": round(total["earned"], 2),
        "fares_collected": round(total["collected"], 2),
        "credited_to_drivers": round(total["paid_to_drivers"], 2),
        "payout_requests_owed": round(owed["total"], 2),
        "by_scope": {r["scope"]: {"trips": r["trips"], "earned": round(r["earned"], 2)} for r in by_scope},
        "recent": [dict(r) for r in recent],
    })


@app.route("/api/admin/payouts/pending", methods=["GET"])
@require_admin
def admin_pending_payouts():
    rows = get_db().execute("""
    SELECT p.id, p.amount, p.upi_id, p.created_at, u.lpu_id, u.name, u.phone FROM driver_payouts p
    JOIN users u ON u.id = p.driver_id
    WHERE p.status = 'requested' ORDER BY p.created_at ASC
    """).fetchall()
    return jsonify({"pending": [dict(r) for r in rows]})


@app.route("/api/admin/payouts/<payout_id>/paid", methods=["POST"])
@require_admin
def admin_mark_payout_paid(payout_id):
    """Call after sending the money to the driver's UPI ID; pass the bank's UTR as proof."""
    utr = get_text(body(), "utr", required=True, max_len=64, label="UTR")
    conn = get_db()
    cur = conn.execute(
        "UPDATE driver_payouts SET status = 'paid', utr = ?, settled_at = ? WHERE id = ? AND status = 'requested'",
        (utr, time.time(), payout_id),
    )
    if cur.rowcount != 1:
        raise ApiError("Payout request not found or already settled", 404)
    conn.commit()
    return jsonify({"success": True, "status": "paid"})


@app.route("/api/admin/payouts/<payout_id>/reject", methods=["POST"])
@require_admin
def admin_reject_payout(payout_id):
    note = get_text(body(), "note", max_len=200)
    conn = get_db()
    begin_write(conn)
    payout = conn.execute("SELECT * FROM driver_payouts WHERE id = ? AND status = 'requested'", (payout_id,)).fetchone()
    if not payout:
        raise ApiError("Payout request not found or already settled", 404)
    conn.execute("UPDATE driver_payouts SET status = 'rejected', note = ?, settled_at = ? WHERE id = ?",
                 (note or None, time.time(), payout_id))
    # Booked as a reversed withdrawal so the amount counts as withdrawable earnings again.
    credit_wallet(conn, payout["driver_id"], payout["amount"], "withdrawal", payout_id, "Payout request returned to wallet")
    conn.commit()
    return jsonify({"success": True, "status": "rejected"})


@app.route("/api/admin/payments/<order_id>/reject", methods=["POST"])
@require_admin
def admin_reject_payment(order_id):
    conn = get_db()
    cur = conn.execute(
        "UPDATE payment_orders SET status = 'rejected' "
        "WHERE id = ? AND provider = 'upi' AND status IN ('created', 'awaiting_verification')",
        (order_id,),
    )
    if cur.rowcount != 1:
        raise ApiError("Payment order not found or already processed", 404)
    conn.commit()
    return jsonify({"success": True, "status": "rejected"})


@app.route("/api/admin/faculty/pending", methods=["GET"])
@require_admin
def admin_pending_faculty():
    """Accounts that signed up as teachers and are waiting for priority-queue approval."""
    rows = get_db().execute("""
    SELECT id, lpu_id, name, phone, created_at FROM users
    WHERE user_type = 'teacher' AND is_teacher_priority = 0 AND deleted_at IS NULL ORDER BY created_at ASC
    """).fetchall()
    return jsonify({"pending": [dict(r) for r in rows]})


@app.route("/api/admin/faculty/<user_id>/approve", methods=["POST"])
@require_admin
def admin_approve_faculty(user_id):
    """Call only after checking the faculty ID against the staff records."""
    conn = get_db()
    cur = conn.execute("UPDATE users SET is_teacher_priority = 1 WHERE id = ? AND user_type = 'teacher' AND deleted_at IS NULL",
                       (user_id,))
    if cur.rowcount != 1:
        raise ApiError("Teacher account not found", 404)
    conn.commit()
    return jsonify({"success": True})


@app.route("/api/admin/sos", methods=["GET"])
@require_admin
def admin_list_sos():
    rows = get_db().execute("""
    SELECT s.id, s.ride_id, s.lat, s.lng, s.location_name, s.status, s.notes, s.created_at,
           u.name, u.lpu_id, u.phone
    FROM sos_alerts s JOIN users u ON u.id = s.user_id
    WHERE s.status != 'resolved' ORDER BY s.created_at DESC LIMIT 100
    """).fetchall()
    return jsonify({"alerts": [dict(r) for r in rows]})


@app.route("/api/admin/sos/<alert_id>/<action>", methods=["POST"])
@require_admin
def admin_update_sos(alert_id, action):
    if action not in ("acknowledge", "resolve"):
        raise ApiError("Not found", 404)
    status = "acknowledged" if action == "acknowledge" else "resolved"
    conn = get_db()
    cur = conn.execute(
        "UPDATE sos_alerts SET status = ?, resolved_at = CASE WHEN ? = 'resolved' THEN ? ELSE resolved_at END "
        "WHERE id = ? AND status != 'resolved'",
        (status, status, time.time(), alert_id),
    )
    if cur.rowcount != 1:
        raise ApiError("Alert not found or already resolved", 404)
    conn.commit()
    return jsonify({"success": True, "status": status})


# Create tables on startup (idempotent). Demo data is seeded separately.
init_db()

if __name__ == "__main__":
    # HTTPS_DEV=1 serves a self-signed certificate (needs the `cryptography`
    # package) so a phone on the same Wi-Fi can use GPS, which browsers only
    # allow on https:// or localhost. HOST=0.0.0.0 exposes it to the LAN.
    use_https = os.environ.get("HTTPS_DEV") == "1"
    host = os.environ.get("HOST", "127.0.0.1")
    print(f"Starting CampusGo API Server on {'https' if use_https else 'http'}://{host}:5000")
    app.run(host=host, port=5000, debug=False, ssl_context="adhoc" if use_https else None)
