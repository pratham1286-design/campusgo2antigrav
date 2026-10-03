import hashlib
import hmac
import json
import logging
import math
import os
import re
import secrets
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import Flask, g, jsonify, request, send_from_directory
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

import geo
import payments
from database import get_db_connection, init_db
from emergency_dispatch import (
    CAMPUS_SECURITY_PHONE,
    dispatch_emergency_alert,
    normalize_phone,
    send_sms,
    sms_gateway_configured,
)
from payments import PaymentError
from pricing_and_queue import (
    CAMPUS_LANDMARKS,
    VALID_SERVICES,
    calculate_fare,
    find_nearest_campus_landmark,
    haversine_distance_km,
    is_city_landmark,
    validate_trip,
)

log = logging.getLogger("campusgo")

FRONTEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "frontend"))
app = Flask(__name__, static_folder=FRONTEND_DIR, static_url_path="")
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
DROP_RADIUS_KM = 0.30              # ...and this close to the drop for the driver to complete it
ROUTE_ORIGIN_RADIUS_KM = 0.5
ROUTE_DEST_RADIUS_KM = 1.0
QUEUE_TTL_SECONDS = 30 * 60        # an unmatched ride request is cancelled and refunded after this
ROUTE_GRACE_SECONDS = 6 * 3600     # a carpool is cancelled and refunded this long after its departure time
SWEEP_INTERVAL_SECONDS = 30
# Campus time is India time; datetime-local inputs send naive local times.
LOCAL_UTC_OFFSET_MINUTES = int(os.environ.get("LOCAL_UTC_OFFSET_MINUTES", "330") or 330)

# --- Session tokens ---------------------------------------------------------
WEAK_SECRET_KEYS = {"dev_only_change_me_in_production", "changeme", "change_me", "secret", "dev", "test"}


def _load_secret_key():
    key = os.environ.get("SECRET_KEY", "").strip()
    if not key:
        print("WARNING: SECRET_KEY env var not set. Using a random ephemeral key - "
              "all sessions will be invalidated on restart. Set SECRET_KEY in production.")
        return secrets.token_hex(32)
    if key in WEAK_SECRET_KEYS or len(key) < 32:
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


def begin_write(conn):
    """Takes SQLite's write lock up front so read-check-write sequences on
    balances and seats can't interleave with another request."""
    conn.execute("BEGIN IMMEDIATE")


@app.teardown_appcontext
def _close_db(exc):
    conn = g.pop("db", None)
    if conn is not None:
        try:
            conn.rollback()  # discards anything a failed request left uncommitted
        finally:
            conn.close()


# --- Security headers ---------------------------------------------------------
CONTENT_SECURITY_POLICY = "; ".join([
    "default-src 'self'",
    # unpkg is limited to the one pinned Leaflet folder (and the pages carry SRI hashes), not the whole CDN.
    "script-src 'self' https://unpkg.com/leaflet@1.9.4/dist/ https://checkout.razorpay.com https://*.razorpay.com",
    "style-src 'self' 'unsafe-inline' https://unpkg.com/leaflet@1.9.4/dist/ https://fonts.googleapis.com",
    "font-src 'self' https://fonts.gstatic.com",
    "img-src 'self' data: https://*.tile.openstreetmap.org https://tile.openstreetmap.org "
    "https://images.unsplash.com https://unpkg.com/leaflet@1.9.4/dist/ https://*.razorpay.com",
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
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Permissions-Policy"] = "geolocation=(self), camera=(), microphone=()"
    if request.is_secure:
        resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    elif request.path.endswith((".js", ".css", ".html")) or request.path == "/":
        # Always revalidate, so a browser never keeps running an old app.js.
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
# In-memory and per process: counters reset on restart, and the app must run a single
# worker (see Procfile/Dockerfile) or each worker would keep its own count.
RATE_LIMIT_STORE = {}
_RATE_LOCK = threading.Lock()


def check_rate(key, max_requests, window_seconds):
    now = time.time()
    with _RATE_LOCK:
        timestamps = [t for t in RATE_LIMIT_STORE.get(key, []) if now - t < window_seconds]
        if len(timestamps) >= max_requests:
            RATE_LIMIT_STORE[key] = timestamps
            raise ApiError("Rate limit exceeded. Please wait a moment before trying again.", 429, "RATE_LIMITED")
        timestamps.append(now)
        RATE_LIMIT_STORE[key] = timestamps
        if len(RATE_LIMIT_STORE) > 5000:
            for k in [k for k, v in RATE_LIMIT_STORE.items() if not v or now - v[-1] > 3600]:
                RATE_LIMIT_STORE.pop(k, None)


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


def require_auth(f):
    """Requires 'Authorization: Bearer <token>'. The acting user always comes
    from the token (request.auth_user_id), never from the request body."""
    @wraps(f)
    def wrapped(*args, **kwargs):
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            raise ApiError("Authentication required", 401, "AUTH_REQUIRED")
        payload = verify_session_token(auth_header[7:])
        row = get_db().execute("SELECT token_version FROM users WHERE id = ?", (payload["u"],)).fetchone() if payload else None
        if not row or row["token_version"] != payload["v"]:
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


def wallet_balance(conn, user_id):
    row = conn.execute("SELECT balance FROM wallets WHERE user_id = ?", (user_id,)).fetchone()
    return row["balance"] if row else 0.0


def debit_wallet(conn, user_id, amount, reference_id, description):
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
    VALUES (?, ?, ?, 'fare_deduction', ?, ?, ?, ?)
    """, (str(uuid.uuid4()), user_id, -amount, reference_id, description, new_bal, now))
    return new_bal


def credit_wallet(conn, user_id, amount, tx_type, reference_id, description):
    now = time.time()
    conn.execute("INSERT OR IGNORE INTO wallets (user_id, balance, updated_at) VALUES (?, 0, ?)", (user_id, now))
    conn.execute("UPDATE wallets SET balance = balance + ?, updated_at = ? WHERE user_id = ?", (amount, now, user_id))
    new_bal = wallet_balance(conn, user_id)
    conn.execute("""
    INSERT INTO wallet_transactions (id, user_id, amount, type, reference_id, description, balance_after, created_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (str(uuid.uuid4()), user_id, amount, tx_type, reference_id, description, new_bal, now))
    return new_bal


def fare_was_held(conn, ride):
    """Rides booked before fares were held up front have no hold transaction;
    they must not be refunded on cancel and must be charged on completion."""
    return conn.execute(
        "SELECT 1 FROM wallet_transactions WHERE user_id = ? AND reference_id = ? AND type = 'fare_deduction'",
        (ride["rider_id"], ride["id"]),
    ).fetchone() is not None


def active_vehicle(conn, user_id):
    return conn.execute("SELECT * FROM vehicles WHERE user_id = ? AND is_active = 1", (user_id,)).fetchone()


def require_vehicle(conn, user_id):
    vehicle = active_vehicle(conn, user_id)
    if not vehicle:
        raise ApiError("Register a vehicle in your profile before driving.", 403, "VEHICLE_REQUIRED")
    return vehicle


def driver_is_busy(conn, driver_id):
    return conn.execute(
        "SELECT 1 FROM rides WHERE driver_id = ? AND status IN ('matched', 'arriving', 'in_progress')", (driver_id,)
    ).fetchone() is not None


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


def movement_simulation_enabled():
    """Demo-only: lets the driver advance a fake position from the UI. Real deployments
    leave this off and trust only the GPS the driver's phone reports."""
    return os.environ.get("DEMO_SIMULATE_MOVEMENT", "") == "1"


def fresh_cutoff():
    """Drivers whose last position report is older than this are treated as offline."""
    return 0.0 if movement_simulation_enabled() else time.time() - LOCATION_MAX_AGE_SECONDS


def driver_near(conn, driver_id, lat, lng, radius_km):
    """True when the driver's own phone recently reported a position within radius_km of the point."""
    if movement_simulation_enabled():
        return True
    loc = conn.execute("SELECT lat, lng, updated_at FROM driver_locations WHERE driver_id = ?", (driver_id,)).fetchone()
    if not loc or time.time() - loc["updated_at"] > LOCATION_MAX_AGE_SECONDS:
        return False
    return haversine_distance_km(loc["lat"], loc["lng"], lat, lng) <= radius_km


def parse_departure(text):
    """Epoch seconds for a 'YYYY-MM-DDTHH:MM' campus-local time, or None for legacy free text."""
    try:
        naive = datetime.strptime((text or "").strip(), "%Y-%m-%dT%H:%M")
    except ValueError:
        return None
    return (naive - timedelta(minutes=LOCAL_UTC_OFFSET_MINUTES)).replace(tzinfo=timezone.utc).timestamp()


def route_is_expired(route, now):
    departs = parse_departure(route["departure_time"])
    if departs is None:
        return now - route["created_at"] > 24 * 3600
    return now > departs + ROUTE_GRACE_SECONDS


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
    refunded = 0.0
    if fare_was_held(conn, ride):
        refunded = ride["fare"]
        credit_wallet(conn, ride["rider_id"], refunded, "refund", ride["id"], f"Refund for cancelled Ride #{ride['id'][5:13]}")
    return refunded


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


_last_sweep = 0.0


def sweep_stale(conn, force=False):
    """Frees money and seats stuck in requests nobody served: queued rides older than
    QUEUE_TTL and carpools long past their departure. Must run outside a transaction."""
    global _last_sweep
    now = time.time()
    if not force and now - _last_sweep < SWEEP_INTERVAL_SECONDS:
        return
    _last_sweep = now
    ride_ids = [r["id"] for r in conn.execute(
        "SELECT id FROM rides WHERE status = 'queued' AND created_at < ?", (now - QUEUE_TTL_SECONDS,)).fetchall()]
    route_ids = [r["id"] for r in conn.execute(
        "SELECT id, departure_time, created_at FROM driver_routes WHERE status IN ('open', 'pinned')").fetchall()
        if route_is_expired(r, now)]
    if not ride_ids and not route_ids:
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
    conn.commit()


# --- Static pages -------------------------------------------------------------------
@app.route("/")
def index():
    return send_from_directory(FRONTEND_DIR, "index.html")


@app.route("/track/<share_token>")
def track_page(share_token):
    return send_from_directory(FRONTEND_DIR, "track.html")


# --- Public config -------------------------------------------------------------------
@app.route("/api/config", methods=["GET"])
def public_config():
    live = payments.razorpay_live_configured()
    demo = payments.payments_demo_mode()
    return jsonify({
        "security_hotline": CAMPUS_SECURITY_PHONE,
        "sms_gateway_configured": sms_gateway_configured(),
        "payments_demo_mode": demo,
        "razorpay_enabled": live or demo,
        "upi_manual_verification": not demo,
        "demo_simulation": movement_simulation_enabled(),
        "min_topup": payments.MIN_TOPUP,
        "max_topup": payments.MAX_TOPUP,
    })


# --- Auth -----------------------------------------------------------------------------
USER_PUBLIC_FIELDS = ("id, lpu_id, username, name, email, phone, user_type, role, is_verified, "
                      "is_teacher_priority, department, avatar_url, created_at")


def _build_user_dict(conn, user_id):
    user = conn.execute(f"SELECT {USER_PUBLIC_FIELDS} FROM users WHERE id = ?", (user_id,)).fetchone()
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


def otp_demo_mode():
    """Shows the code in the API reply instead of texting it. Only works while no
    SMS gateway is configured, so it can never leak codes in a real deployment."""
    return os.environ.get("OTP_DEMO_MODE", "") == "1" and not sms_gateway_configured()


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
    demo = otp_demo_mode()
    if not demo and not sms_gateway_configured():
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

    code = "".join(str(secrets.randbelow(10)) for _ in range(OTP_LENGTH))
    challenge_id = f"otp_{secrets.token_urlsafe(16)}"
    if not demo:
        delivery = send_sms(f"+91{key}", f"{code} is your CampusGo verification code. It expires in 5 minutes. Do not share it with anyone.")
        if delivery.get("status") != "sent":
            raise ApiError("We couldn't send the SMS. Check the number and try again.", 502, "SMS_FAILED")

    conn.execute("DELETE FROM otp_challenges WHERE expires_at < ?", (now - 3600,))
    conn.execute(
        "INSERT INTO otp_challenges (id, purpose, phone_key, code_hash, payload, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (challenge_id, purpose, key, _otp_hash(challenge_id, code), json.dumps(payload), now, now + OTP_TTL_SECONDS),
    )
    conn.commit()
    reply = {
        "challenge_id": challenge_id,
        "phone_hint": f"+91 {key[:2]}XXXXXX{key[-2:]}",
        "expires_in": OTP_TTL_SECONDS,
        "resend_after": OTP_RESEND_SECONDS,
    }
    if demo:
        reply["demo_otp"] = code
    return reply


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
        INSERT INTO users (id, lpu_id, name, email, phone, user_type, role, password_hash, is_verified,
                           is_teacher_priority, department, avatar_url, created_at, username, phone_key)
        VALUES (?, ?, ?, '', ?, ?, 'rider', '', 1, 0, NULL, NULL, ?, ?, ?)
        """, (user_id, payload["lpu_id"], payload["name"], f"+91{key}", payload["user_type"],
              now, payload["username"], key))
    except sqlite3.IntegrityError:
        conn.rollback()
        raise ApiError("Someone just took that username, ID or number. Please start again.", 409, "SIGNUP_CONFLICT")
    conn.execute("INSERT INTO wallets (user_id, balance, currency, updated_at) VALUES (?, 0, 'INR', ?)", (user_id, now))
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
        if otp_demo_mode():
            raise ApiError("No account matches this LPU ID and mobile number. New here? Sign up instead.", 404, "NO_ACCOUNT")
        # Same reply as a real code request, so this endpoint can't be used to find out who has an account.
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
    conn.commit()
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


@app.route("/api/drivers/nearby", methods=["GET"])
@require_auth
def nearby_drivers():
    """Online, free drivers for the home map (no contact details)."""
    rows = get_db().execute("""
    SELECT dl.driver_id, dl.lat, dl.lng, v.category, u.name
    FROM driver_locations dl
    JOIN users u ON u.id = dl.driver_id
    JOIN vehicles v ON v.user_id = dl.driver_id AND v.is_active = 1
    WHERE dl.is_online = 1 AND dl.updated_at >= ?
      AND dl.driver_id != ?
      AND dl.driver_id NOT IN (SELECT driver_id FROM rides
                               WHERE status IN ('matched', 'arriving', 'in_progress') AND driver_id IS NOT NULL)
    """, (fresh_cutoff(), request.auth_user_id)).fetchall()
    # "key" lets the map move a driver's marker instead of redrawing it, without revealing the account id.
    drivers = [{"key": hmac.new(OTP_HMAC_KEY, r["driver_id"].encode(), hashlib.sha256).hexdigest()[:10],
                "lat": r["lat"], "lng": r["lng"], "category": r["category"],
                "first_name": (r["name"] or "Driver").split()[0]} for r in rows]
    return jsonify({"drivers": drivers})


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

    quotes = {}
    for service in VALID_SERVICES:
        drivers = conn.execute("""
        SELECT COUNT(*) AS n FROM driver_locations dl
        JOIN vehicles v ON dl.driver_id = v.user_id AND v.is_active = 1
        WHERE dl.is_online = 1 AND dl.updated_at >= ? AND v.category = ? AND dl.driver_id != ?
          AND dl.driver_id NOT IN (SELECT driver_id FROM rides
                                   WHERE status IN ('matched', 'arriving', 'in_progress') AND driver_id IS NOT NULL)
        """, (fresh_cutoff(), service, request.auth_user_id)).fetchone()["n"]
        fare = calculate_fare(service, scope, pickup["lat"], pickup["lng"], drop["lat"], drop["lng"], pickup["zone"], demand)
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
                               pickup["zone"], _demand(conn, pickup["zone"]))
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
        is_priority, share_token, created_at, matched_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        ride_id, rider_id, None, service_type, scope,
        pickup["name"], pickup["lat"], pickup["lng"], pickup["zone"],
        drop["name"], drop["lat"], drop["lng"], drop["zone"],
        total_fare, fare_info["base_fare"], fare_info["surge_multiplier"], status,
        is_priority, f"share_{secrets.token_urlsafe(16)}", now, None,
    ))
    position = queue_position(conn, pickup["zone"], is_priority, now)
    waiting_drivers = conn.execute("""
    SELECT COUNT(*) AS n FROM driver_locations dl JOIN vehicles v ON v.user_id = dl.driver_id AND v.is_active = 1
    WHERE dl.is_online = 1 AND dl.updated_at >= ? AND v.category = ? AND dl.driver_id != ?
    """, (fresh_cutoff(), service_type, rider_id)).fetchone()["n"]
    conn.commit()
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
           ru.name AS rider_name, ru.phone AS rider_phone, ru.avatar_url AS rider_avatar,
           ru.is_teacher_priority AS rider_is_teacher, ru.user_type AS rider_type,
           du.name AS driver_name, du.phone AS driver_phone, du.avatar_url AS driver_avatar,
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
    if ride_dict["status"] == "queued":
        ride_dict["queue_position"] = queue_position(conn, ride["pickup_zone"], ride["is_priority"], ride["created_at"])
    return jsonify({"active_ride": ride_dict})


@app.route("/api/driver/location", methods=["POST"])
@require_auth
@rate_limit(max_requests=30, window_seconds=60)
def report_driver_location():
    """The driver's phone reports its real GPS position. This is the only thing that
    moves a driver on the map, starts a pickup, or lets a trip be completed."""
    lat, lng = get_coordinates(body())
    driver_id = request.auth_user_id
    conn = get_db()
    begin_write(conn)
    now = time.time()
    conn.execute("""
    INSERT INTO driver_locations (driver_id, is_online, lat, lng, zone, heading, updated_at)
    VALUES (?, 0, ?, ?, 'Zone-Central', 0.0, ?)
    ON CONFLICT(driver_id) DO UPDATE SET lat = excluded.lat, lng = excluded.lng, updated_at = excluded.updated_at
    """, (driver_id, lat, lng, now))

    ride = conn.execute(
        "SELECT * FROM rides WHERE driver_id = ? AND status IN ('matched', 'arriving', 'in_progress') LIMIT 1",
        (driver_id,),
    ).fetchone()
    status = ride["status"] if ride else None
    if ride and status in ("matched", "arriving") and \
            haversine_distance_km(lat, lng, ride["pickup_lat"], ride["pickup_lng"]) <= PICKUP_RADIUS_KM:
        conn.execute("UPDATE rides SET status = 'in_progress', started_at = ? WHERE id = ? AND status IN ('matched', 'arriving')",
                     (now, ride["id"]))
        status = "in_progress"
    conn.execute("UPDATE driver_routes SET current_lat = ?, current_lng = ? WHERE driver_id = ? AND status = 'in_progress'",
                 (lat, lng, driver_id))
    conn.commit()
    return jsonify({"success": True, "ride_status": status})


@app.route("/api/rides/<ride_id>/telemetry-step", methods=["POST"])
@require_auth
@rate_limit(max_requests=120, window_seconds=60)
def telemetry_step(ride_id):
    """Demo only (DEMO_SIMULATE_MOVEMENT=1): the driver advances a fake position. With
    it off, positions come from /api/driver/location and this endpoint refuses."""
    if not movement_simulation_enabled():
        raise ApiError("Movement simulation is disabled. Positions come from the driver's phone.", 403, "SIMULATION_DISABLED")
    conn = get_db()
    ride = load_ride_for_participant(conn, ride_id)
    if request.auth_user_id != ride["driver_id"]:
        raise ApiError("Only the driver can move the simulated vehicle", 403)
    status = ride["status"]
    driver_id = ride["driver_id"]
    if status not in ("matched", "arriving", "in_progress") or not driver_id:
        return jsonify({"status": status})

    begin_write(conn)
    loc = conn.execute("SELECT lat, lng FROM driver_locations WHERE driver_id = ?", (driver_id,)).fetchone()
    current_lat = loc["lat"] if loc else ride["pickup_lat"]
    current_lng = loc["lng"] if loc else ride["pickup_lng"]

    if status in ("matched", "arriving"):
        target_lat, target_lng, step = ride["pickup_lat"], ride["pickup_lng"], 0.35
    else:
        target_lat, target_lng, step = ride["drop_lat"], ride["drop_lng"], 0.25

    dist_remaining = haversine_distance_km(current_lat, current_lng, target_lat, target_lng)
    new_lat = current_lat + (target_lat - current_lat) * step
    new_lng = current_lng + (target_lng - current_lng) * step

    next_status = status
    if status in ("matched", "arriving") and dist_remaining < 0.08:
        next_status = "in_progress"
        new_lat, new_lng = ride["pickup_lat"], ride["pickup_lng"]
        conn.execute("UPDATE rides SET status = 'in_progress', started_at = ? WHERE id = ?", (time.time(), ride_id))
        dist_remaining = haversine_distance_km(new_lat, new_lng, ride["drop_lat"], ride["drop_lng"])

    conn.execute("UPDATE driver_locations SET lat = ?, lng = ?, updated_at = ? WHERE driver_id = ?",
                 (new_lat, new_lng, time.time(), driver_id))
    conn.commit()
    return jsonify({
        "status": next_status,
        "current_lat": new_lat,
        "current_lng": new_lng,
        "distance_remaining_km": round(dist_remaining, 2),
    })


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
        "UPDATE rides SET status = 'queued', driver_id = NULL, matched_at = NULL "
        "WHERE id = ? AND driver_id = ? AND status IN ('matched', 'arriving')",
        (ride_id, request.auth_user_id),
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
    cur = conn.execute(
        "UPDATE rides SET status = 'completed', completed_at = ? "
        "WHERE id = ? AND status = 'in_progress' AND driver_id IS NOT NULL",
        (now, ride_id),
    )
    if cur.rowcount != 1:
        raise ApiError("A ride can only be completed after the driver has picked you up.", 409, "NOT_IN_PROGRESS")

    # Normally the fare was held at booking, so only the driver is paid here.
    # Rides booked before that rule existed are charged now instead.
    if not fare_was_held(conn, ride):
        debit_wallet(conn, ride["rider_id"], ride["fare"], ride_id, f"CampusGo Ride #{ride_id[5:13]}")
    payout = round(ride["fare"] * DRIVER_SHARE, 2)
    credit_wallet(conn, ride["driver_id"], payout, "driver_payout", ride_id, f"Payout for Ride #{ride_id[5:13]}")
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
    except sqlite3.IntegrityError:
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
    ride_dict["rider_name"] = (ride_dict["rider_name"] or "Rider").split()[0]
    if ride_dict["driver_name"]:
        ride_dict["driver_name"] = ride_dict["driver_name"].split()[0]
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

    zones = {l["zone"] for l in CAMPUS_LANDMARKS.values()}
    zone = data.get("zone") if data.get("zone") in zones else "Zone-Central"
    has_fix = data.get("lat") is not None and data.get("lng") is not None
    lat, lng = get_coordinates(data) if has_fix else (31.2536, 75.7038)
    # Only a real position report refreshes the location timestamp; going online alone doesn't,
    # so a driver whose phone stops reporting drops out of matching after a couple of minutes.
    conn.execute("""
    INSERT INTO driver_locations (driver_id, is_online, lat, lng, zone, heading, updated_at)
    VALUES (:driver, :online, :lat, :lng, :zone, 0.0, :now)
    ON CONFLICT(driver_id) DO UPDATE SET
        is_online = excluded.is_online, zone = excluded.zone,
        lat = CASE WHEN :fix THEN excluded.lat ELSE driver_locations.lat END,
        lng = CASE WHEN :fix THEN excluded.lng ELSE driver_locations.lng END,
        updated_at = CASE WHEN :fix THEN excluded.updated_at ELSE driver_locations.updated_at END
    """, {"driver": driver_id, "online": is_online, "lat": lat, "lng": lng, "zone": zone,
          "now": time.time() if has_fix else 0.0, "fix": 1 if has_fix else 0})
    conn.commit()
    return jsonify({"success": True, "is_online": bool(is_online)})


@app.route("/api/driver/requests", methods=["GET"])
@require_auth
def get_driver_requests():
    """Queued requests this driver's vehicle can serve, teachers first."""
    conn = get_db()
    sweep_stale(conn)
    vehicle = require_vehicle(conn, request.auth_user_id)
    online = conn.execute("SELECT is_online FROM driver_locations WHERE driver_id = ?", (request.auth_user_id,)).fetchone()
    if not (online and online["is_online"]):
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
    """, (vehicle["category"], request.auth_user_id)).fetchall()
    me = conn.execute("SELECT lat, lng, updated_at FROM driver_locations WHERE driver_id = ?", (request.auth_user_id,)).fetchone()
    has_fix = bool(me and time.time() - me["updated_at"] <= LOCATION_MAX_AGE_SECONDS)
    requests_out = []
    for r in rows:
        item = dict(r)
        item["trip_km"] = round(haversine_distance_km(item["pickup_lat"], item["pickup_lng"], item["drop_lat"], item["drop_lng"]), 1)
        item["distance_to_pickup_km"] = round(haversine_distance_km(me["lat"], me["lng"], item.pop("pickup_lat"), item.pop("pickup_lng")), 1) if has_fix else None
        item.pop("drop_lat"), item.pop("drop_lng")
        item["rider_name"] = (item["rider_name"] or "Rider").split()[0]
        item["driver_payout"] = round(item["fare"] * DRIVER_SHARE, 2)
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
    })


# --- CityLink scheduled carpools -------------------------------------------------------------
ROUTE_LIST_FIELDS = """
dr.id, dr.driver_id, dr.origin, dr.destination, dr.departure_time, dr.available_seats, dr.total_seats,
dr.price_per_seat, dr.notes, dr.status, dr.created_at, dr.started_at,
u.name AS driver_name, u.avatar_url AS driver_avatar,
v.model AS vehicle_model, v.category AS vehicle_category, v.plate_number AS vehicle_plate
"""


@app.route("/api/driver/routes", methods=["GET"])
@app.route("/api/routes/scheduled", methods=["GET"])
@require_auth
def get_scheduled_routes():
    user_id = request.auth_user_id
    conn = get_db()
    sweep_stale(conn)
    routes = [dict(r) for r in conn.execute(f"""
    SELECT {ROUTE_LIST_FIELDS}
    FROM driver_routes dr
    JOIN users u ON dr.driver_id = u.id
    LEFT JOIN vehicles v ON dr.driver_id = v.user_id AND v.is_active = 1
    WHERE dr.status IN ('open', 'pinned', 'in_progress')
    ORDER BY (CASE dr.status WHEN 'pinned' THEN 0 WHEN 'in_progress' THEN 1 ELSE 2 END), dr.created_at DESC
    """).fetchall()]

    for r in routes:
        # Campus-wide listing: names only, no phone numbers or LPU IDs.
        passengers = [dict(p) for p in conn.execute("""
        SELECT rb.seats, rb.status AS booking_status, u.id AS passenger_id,
               u.name AS passenger_name, u.avatar_url AS passenger_avatar
        FROM route_bookings rb JOIN users u ON rb.rider_id = u.id
        WHERE rb.route_id = ? AND rb.status IN ('confirmed', 'in_progress')
        """, (r["id"],)).fetchall()]
        r["is_host"] = r.pop("driver_id") == user_id
        r["has_joined"] = any(p["passenger_id"] == user_id for p in passengers)
        for p in passengers:
            p.pop("passenger_id")
        r["passengers"] = passengers

    return jsonify({"routes": routes})


@app.route("/api/routes/plan", methods=["POST"])
@app.route("/api/driver/routes", methods=["POST"])
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
        departure_time, available_seats, total_seats, price_per_seat, notes, status, share_token,
        current_lat, current_lng, created_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?)
    """, (
        route_id, driver_id, origin["name"], origin["lat"], origin["lng"], dest["name"], dest["lat"], dest["lng"],
        departure_time, total_seats, total_seats, round(price_per_seat, 2), notes,
        f"share_route_{secrets.token_urlsafe(12)}", origin["lat"], origin["lng"], time.time(),
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
    SELECT dr.*, u.name AS driver_name, u.phone AS driver_phone, u.avatar_url AS driver_avatar,
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
    route_dict.pop("share_token", None)
    route_dict["is_host"] = route["driver_id"] == request.auth_user_id
    is_host = route["driver_id"] == request.auth_user_id
    route_dict["passengers"] = [dict(p) for p in conn.execute("""
    SELECT rb.id AS booking_id, rb.seats, rb.fare_paid, rb.status AS booking_status,
           u.name AS passenger_name, u.phone AS passenger_phone, u.avatar_url AS passenger_avatar
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


@app.route("/api/routes/<route_id>/telemetry-step", methods=["POST"])
@require_auth
@rate_limit(max_requests=120, window_seconds=60)
def route_telemetry_step(route_id):
    """Demo only (DEMO_SIMULATE_MOVEMENT=1). Real trips are moved by the host's phone GPS."""
    if not movement_simulation_enabled():
        raise ApiError("Movement simulation is disabled. Positions come from the host's phone.", 403, "SIMULATION_DISABLED")
    conn = get_db()
    route = conn.execute("SELECT * FROM driver_routes WHERE id = ?", (route_id,)).fetchone()
    if not route or route["status"] != "in_progress":
        raise ApiError("Route is not active")
    if route["driver_id"] != request.auth_user_id:
        raise ApiError("Only the host can move the simulated vehicle", 403)

    curr_lat = route["current_lat"] or route["origin_lat"]
    curr_lng = route["current_lng"] or route["origin_lng"]
    new_lat = curr_lat + (route["destination_lat"] - curr_lat) * 0.30
    new_lng = curr_lng + (route["destination_lng"] - curr_lng) * 0.30
    dist = haversine_distance_km(new_lat, new_lng, route["destination_lat"], route["destination_lng"])

    conn.execute("UPDATE driver_routes SET current_lat = ?, current_lng = ? WHERE id = ?", (new_lat, new_lng, route_id))
    conn.commit()
    return jsonify({"status": "in_progress", "current_lat": new_lat, "current_lng": new_lng,
                    "distance_remaining_km": round(dist, 2)})


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

    now = time.time()
    cur = conn.execute(
        "UPDATE driver_routes SET status = 'completed', completed_at = ? WHERE id = ? AND status = 'in_progress'",
        (now, route_id),
    )
    if cur.rowcount != 1:
        raise ApiError("Only a trip that is in progress can be completed", 409, "NOT_IN_PROGRESS")

    total_revenue = conn.execute(
        "SELECT COALESCE(SUM(fare_paid), 0.0) AS total FROM route_bookings WHERE route_id = ? AND status = 'in_progress'",
        (route_id,),
    ).fetchone()["total"]
    payout = round(total_revenue * DRIVER_SHARE, 2)
    if payout > 0:
        credit_wallet(conn, route["driver_id"], payout, "driver_payout", route_id, f"Carpool Payout: {route['destination']}")
    conn.execute(
        "UPDATE route_bookings SET status = 'completed', completed_at = ? WHERE route_id = ? AND status = 'in_progress'",
        (now, route_id),
    )
    conn.commit()
    return jsonify({"success": True, "status": "completed", "total_revenue": total_revenue, "driver_payout": payout})


@app.route("/api/user/active-session", methods=["GET"])
@require_auth
def get_user_active_session():
    user_id = request.auth_user_id
    conn = get_db()
    active_route = conn.execute("""
    SELECT dr.id AS route_id, dr.origin, dr.destination, dr.departure_time, dr.status,
           dr.price_per_seat, dr.available_seats, dr.total_seats,
           (CASE WHEN dr.driver_id = ? THEN 1 ELSE 0 END) AS is_host
    FROM driver_routes dr
    WHERE (dr.driver_id = ? OR dr.id IN (
        SELECT route_id FROM route_bookings WHERE rider_id = ? AND status IN ('confirmed', 'in_progress')
    ))
    AND dr.status IN ('pinned', 'in_progress')
    ORDER BY dr.started_at DESC LIMIT 1
    """, (user_id, user_id, user_id)).fetchone()
    if active_route:
        return jsonify({"active_session": dict(active_route), "session_type": "scheduled_route"})

    active_ride = conn.execute("""
    SELECT id AS ride_id, pickup_name, drop_name, status, fare,
           (CASE WHEN driver_id = ? THEN 1 ELSE 0 END) AS is_driver
    FROM rides
    WHERE (rider_id = ? OR driver_id = ?) AND status IN ('matched', 'arriving', 'in_progress')
    ORDER BY created_at DESC LIMIT 1
    """, (user_id, user_id, user_id)).fetchone()
    if active_ride:
        return jsonify({"active_session": dict(active_ride), "session_type": "on_demand_ride"})
    return jsonify({"active_session": None, "session_type": None})


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
    amount = get_number(data, "amount", lo=payments.MIN_TOPUP, hi=payments.MAX_TOPUP, label="amount")
    return payments.validate_topup_amount(amount)


@app.route("/api/payments/upi/create-qr", methods=["POST"])
@require_auth
@rate_limit(max_requests=20, window_seconds=60)
def upi_create_qr():
    amount = _topup_amount(body())
    conn = get_db()
    order_id = payments.create_payment_order(conn, request.auth_user_id, "upi", amount)
    conn.commit()
    result = payments.generate_upi_qr(order_id, amount)
    result.update({"success": True, "demo_mode": payments.payments_demo_mode()})
    return jsonify(result)


@app.route("/api/payments/upi/confirm", methods=["POST"])
@require_auth
@rate_limit(max_requests=10, window_seconds=60)
def upi_confirm():
    """The user says they paid. UPI gives no client-side proof of payment, so
    the order is credited only after verification - except in explicit demo
    mode. The amount always comes from the stored order, never the request."""
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

    if payments.payments_demo_mode():
        result = payments.credit_order(conn, order["id"], "upi (demo)")
        conn.commit()
        return jsonify(result)

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
    live = payments.razorpay_live_configured()
    demo = payments.payments_demo_mode()
    if not live and not demo:
        raise ApiError("Card payments are not configured on this server", 503, "PAYMENTS_DISABLED")

    receipt = f"rcpt_{uuid.uuid4().hex[:12]}"
    if live:
        provider_order_id = payments.create_razorpay_remote_order(amount, receipt)
    else:
        provider_order_id = f"order_demo_{uuid.uuid4().hex[:14]}"
    conn = get_db()
    payments.create_payment_order(conn, request.auth_user_id, "razorpay", amount, provider_order_id)
    conn.commit()
    return jsonify({
        "success": True,
        "order_id": provider_order_id,
        "amount": int(round(amount * 100)),
        "currency": "INR",
        "key_id": os.environ.get("RAZORPAY_KEY_ID", "") if live else None,
        "demo_mode": demo,
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

    result = payments.credit_order(conn, order["id"], "razorpay", payment_id)
    conn.commit()
    return jsonify(result)


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
    WHERE user_type = 'teacher' AND is_teacher_priority = 0 ORDER BY created_at ASC
    """).fetchall()
    return jsonify({"pending": [dict(r) for r in rows]})


@app.route("/api/admin/faculty/<user_id>/approve", methods=["POST"])
@require_admin
def admin_approve_faculty(user_id):
    """Call only after checking the faculty ID against the staff records."""
    conn = get_db()
    cur = conn.execute("UPDATE users SET is_teacher_priority = 1 WHERE id = ? AND user_type = 'teacher'", (user_id,))
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
