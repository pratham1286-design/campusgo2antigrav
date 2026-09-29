import logging
import math
import os
import re
import secrets
import sqlite3
import threading
import time
import uuid
from functools import wraps

from flask import Flask, g, jsonify, request, send_from_directory
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash

import payments
from database import get_db_connection, init_db
from emergency_dispatch import (
    CAMPUS_SECURITY_PHONE,
    dispatch_emergency_alert,
    normalize_phone,
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
ACTIVE_RIDE_STATUSES = ("queued", "matched", "arriving", "in_progress")
CANCELLABLE_RIDE_STATUSES = ("queued", "matched", "arriving")

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


def generate_session_token(user_id):
    return SESSION_SERIALIZER.dumps(user_id)


def verify_session_token(token):
    try:
        return SESSION_SERIALIZER.loads(token, max_age=SESSION_MAX_AGE_SECONDS)
    except (BadSignature, SignatureExpired):
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
    "script-src 'self' https://unpkg.com https://checkout.razorpay.com https://*.razorpay.com",
    "style-src 'self' 'unsafe-inline' https://unpkg.com https://fonts.googleapis.com",
    "font-src 'self' https://fonts.gstatic.com",
    "img-src 'self' data: https://*.tile.openstreetmap.org https://tile.openstreetmap.org "
    "https://images.unsplash.com https://unpkg.com https://*.razorpay.com",
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
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


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
    lat = get_number(data, "lat", 31.2536, lo=-90, hi=90, label="latitude")
    lng = get_number(data, "lng", 75.7037, lo=-180, hi=180, label="longitude")
    return lat, lng


# --- Rate limiting --------------------------------------------------------------
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
        user_id = verify_session_token(auth_header[7:])
        if not user_id:
            raise ApiError("Session expired or invalid, please log in again", 401, "AUTH_INVALID")
        request.auth_user_id = user_id
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
        "min_topup": payments.MIN_TOPUP,
        "max_topup": payments.MAX_TOPUP,
    })


# --- Auth -----------------------------------------------------------------------------
USER_PUBLIC_FIELDS = ("id, lpu_id, name, email, phone, user_type, role, is_verified, "
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


@app.route("/api/auth/login", methods=["POST"])
@rate_limit(max_requests=10, window_seconds=60)
def login():
    data = body()
    lpu_id = get_text(data, "lpu_id", required=True, max_len=32, label="LPU ID")
    password = data.get("password")
    if not isinstance(password, str) or not password or len(password) > 256:
        raise ApiError("LPU ID and password are required")
    # Second limit per account, so rotating IPs doesn't allow password guessing.
    check_rate(f"login-account:{lpu_id.lower()}", 10, 300)

    conn = get_db()
    user = conn.execute("SELECT id, password_hash FROM users WHERE lpu_id = ?", (lpu_id,)).fetchone()
    if not user or not user["password_hash"] or not check_password_hash(user["password_hash"], password):
        raise ApiError("Invalid LPU ID or password", 401)

    return jsonify({"user": _build_user_dict(conn, user["id"]), "token": generate_session_token(user["id"])})


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
    has_helmet = 1 if data.get("has_helmet") else 0
    has_ac = 1 if data.get("has_ac") and category == "car" else 0

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

    conn = get_db()
    begin_write(conn)
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
    SELECT dl.lat, dl.lng, v.category, u.name
    FROM driver_locations dl
    JOIN users u ON u.id = dl.driver_id
    JOIN vehicles v ON v.user_id = dl.driver_id AND v.is_active = 1
    WHERE dl.is_online = 1
      AND dl.driver_id != ?
      AND dl.driver_id NOT IN (SELECT driver_id FROM rides
                               WHERE status IN ('matched', 'arriving', 'in_progress') AND driver_id IS NOT NULL)
    """, (request.auth_user_id,)).fetchall()
    drivers = [{"lat": r["lat"], "lng": r["lng"], "category": r["category"],
                "first_name": (r["name"] or "Driver").split()[0]} for r in rows]
    return jsonify({"drivers": drivers})


# --- Rides ----------------------------------------------------------------------------------
def _trip_from_request(data):
    scope = data.get("scope", "campus_hop")
    pickup, drop, error = validate_trip(scope, data.get("pickup_key"), data.get("drop_key"))
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
    balance = wallet_balance(conn, request.auth_user_id)
    demand = _demand(conn, pickup["zone"])

    quotes = {}
    for service in VALID_SERVICES:
        drivers = conn.execute("""
        SELECT COUNT(*) AS n FROM driver_locations dl
        JOIN vehicles v ON dl.driver_id = v.user_id AND v.is_active = 1
        WHERE dl.is_online = 1 AND v.category = ?
        """, (service,)).fetchone()["n"]
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

    matched = conn.execute("""
    SELECT dl.driver_id, u.name, u.avatar_url, v.model, v.plate_number, v.color, v.has_helmet, v.has_ac
    FROM driver_locations dl
    JOIN users u ON dl.driver_id = u.id
    JOIN vehicles v ON dl.driver_id = v.user_id
    WHERE dl.is_online = 1 AND v.category = ? AND v.is_active = 1 AND dl.driver_id != ?
      AND dl.driver_id NOT IN (SELECT driver_id FROM rides
                               WHERE status IN ('matched', 'arriving', 'in_progress') AND driver_id IS NOT NULL)
    ORDER BY (CASE WHEN dl.zone = ? THEN 0 ELSE 1 END), dl.updated_at DESC
    LIMIT 1
    """, (service_type, rider_id, pickup["zone"])).fetchone()

    now = time.time()
    is_priority = 1 if rider["is_teacher_priority"] else 0
    status = "arriving" if matched else "queued"
    conn.execute("""
    INSERT INTO rides (
        id, rider_id, driver_id, service_type, scope,
        pickup_name, pickup_lat, pickup_lng, pickup_zone,
        drop_name, drop_lat, drop_lng, drop_zone,
        fare, base_fare, surge_multiplier, status,
        is_priority, share_token, created_at, matched_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        ride_id, rider_id, matched["driver_id"] if matched else None, service_type, scope,
        pickup["name"], pickup["lat"], pickup["lng"], pickup["zone"],
        drop["name"], drop["lat"], drop["lng"], drop["zone"],
        total_fare, fare_info["base_fare"], fare_info["surge_multiplier"], status,
        is_priority, f"share_{secrets.token_urlsafe(16)}", now, now if matched else None,
    ))
    position = queue_position(conn, pickup["zone"], is_priority, now) if status == "queued" else 0
    conn.commit()

    matched_info = None
    if matched:
        matched_info = {k: matched[k] for k in ("name", "avatar_url", "model", "plate_number", "color", "has_helmet", "has_ac")}
    return jsonify({
        "success": True,
        "ride_id": ride_id,
        "status": status,
        "is_priority": bool(is_priority),
        "queue_position": position,
        "matched_driver": matched_info,
        "fare": total_fare,
        "wallet_balance": round(new_bal, 2),
    })


@app.route("/api/rides/active", methods=["GET"])
@require_auth
def get_active_ride():
    user_id = request.auth_user_id
    conn = get_db()
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


@app.route("/api/rides/<ride_id>/telemetry-step", methods=["POST"])
@require_auth
@rate_limit(max_requests=120, window_seconds=60)
def telemetry_step(ride_id):
    """Simulates the driver moving: towards pickup while 'arriving', then
    towards the drop while 'in_progress'."""
    conn = get_db()
    ride = load_ride_for_participant(conn, ride_id)
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
    now = time.time()

    if request.auth_user_id == ride["rider_id"]:
        cur = conn.execute(
            "UPDATE rides SET status = 'cancelled', cancelled_at = ?, cancellation_reason = ? "
            "WHERE id = ? AND status IN ('queued', 'matched', 'arriving')",
            (now, reason or "Cancelled by rider", ride_id),
        )
        if cur.rowcount != 1:
            raise ApiError("This ride can no longer be cancelled", 409, "NOT_CANCELLABLE")
        refunded = 0.0
        if fare_was_held(conn, ride):
            refunded = ride["fare"]
            credit_wallet(conn, ride["rider_id"], refunded, "refund", ride_id, f"Refund for cancelled Ride #{ride_id[5:13]}")
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
@app.route("/api/sos/trigger", methods=["POST"])
@require_auth
@rate_limit(max_requests=3, window_seconds=60)
def trigger_sos():
    data = body()
    user_id = request.auth_user_id
    lat, lng = get_coordinates(data)
    location_name = get_text(data, "location_name", max_len=100) or "Near LPU Campus"
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

    result = dispatch_emergency_alert(user["name"], user["lpu_id"], location_name, lat, lng, contacts, share_url)

    alert_id = f"sos_{uuid.uuid4().hex[:8]}"
    conn.execute("""
    INSERT INTO sos_alerts (id, ride_id, user_id, lat, lng, location_name, status, notified_contacts_count, notes, created_at)
    VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)
    """, (alert_id, linked_ride_id, user_id, lat, lng, location_name, result["contacts_sent"],
          "SOS triggered from app", time.time()))
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
    now = time.time()
    conn.execute("""
    INSERT INTO driver_locations (driver_id, is_online, lat, lng, zone, heading, updated_at)
    VALUES (?, ?, 31.2536, 75.7038, ?, 0.0, ?)
    ON CONFLICT(driver_id) DO UPDATE SET is_online = excluded.is_online, zone = excluded.zone,
                                         updated_at = excluded.updated_at
    """, (driver_id, is_online, zone, now))
    conn.commit()
    return jsonify({"success": True, "is_online": bool(is_online)})


@app.route("/api/driver/requests", methods=["GET"])
@require_auth
def get_driver_requests():
    """Queued requests this driver's vehicle can serve, teachers first."""
    conn = get_db()
    vehicle = require_vehicle(conn, request.auth_user_id)
    rows = conn.execute("""
    SELECT r.id, r.service_type, r.scope, r.pickup_name, r.pickup_zone, r.drop_name, r.fare,
           r.is_priority, r.created_at, u.name AS rider_name, u.user_type AS rider_type
    FROM rides r
    JOIN users u ON r.rider_id = u.id
    WHERE r.status = 'queued' AND r.service_type = ? AND r.rider_id != ?
    ORDER BY r.is_priority DESC, r.created_at ASC
    LIMIT 25
    """, (vehicle["category"], request.auth_user_id)).fetchall()
    requests_out = []
    for r in rows:
        item = dict(r)
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
    if driver_is_busy(conn, driver_id):
        raise ApiError("Finish your current ride before accepting another", 409, "DRIVER_BUSY")

    cur = conn.execute("""
    UPDATE rides SET driver_id = ?, status = 'arriving', matched_at = ?
    WHERE id = ? AND status = 'queued' AND service_type = ? AND rider_id != ?
    """, (driver_id, time.time(), ride_id, vehicle["category"], driver_id))
    if cur.rowcount != 1:
        raise ApiError("This request is no longer available for your vehicle", 409, "NOT_AVAILABLE")

    conn.execute("""
    INSERT INTO driver_locations (driver_id, is_online, lat, lng, zone, heading, updated_at)
    VALUES (?, 1, 31.2536, 75.7038, 'Zone-Central', 0.0, ?)
    ON CONFLICT(driver_id) DO UPDATE SET is_online = 1, updated_at = excluded.updated_at
    """, (driver_id, time.time()))
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
    cur = conn.execute("UPDATE driver_routes SET status = 'cancelled' WHERE id = ? AND status IN ('open', 'pinned')",
                       (route_id,))
    if cur.rowcount != 1:
        raise ApiError("This trip has already started or ended", 409)

    bookings = conn.execute("SELECT * FROM route_bookings WHERE route_id = ? AND status = 'confirmed'", (route_id,)).fetchall()
    for b in bookings:
        conn.execute("UPDATE route_bookings SET status = 'cancelled' WHERE id = ?", (b["id"],))
        credit_wallet(conn, b["rider_id"], b["fare_paid"], "refund", b["id"],
                      f"Refund: host cancelled trip to {route['destination']}")
    conn.commit()
    return jsonify({"success": True, "status": "cancelled", "refunded_bookings": len(bookings)})


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
    route_dict["passengers"] = [dict(p) for p in conn.execute("""
    SELECT rb.id AS booking_id, rb.seats, rb.fare_paid, rb.status AS booking_status,
           u.name AS passenger_name, u.phone AS passenger_phone, u.avatar_url AS passenger_avatar
    FROM route_bookings rb JOIN users u ON rb.rider_id = u.id
    WHERE rb.route_id = ? AND rb.status IN ('confirmed', 'in_progress', 'completed')
    """, (route_id,)).fetchall()]

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
    conn = get_db()
    route = conn.execute("SELECT * FROM driver_routes WHERE id = ?", (route_id,)).fetchone()
    if not route or route["status"] != "in_progress":
        raise ApiError("Route is not active")
    route_participant_check(conn, route, request.auth_user_id)

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


# Create tables on startup (idempotent). Demo data is seeded separately.
init_db()

if __name__ == "__main__":
    print("Starting CampusGo API Server on http://127.0.0.1:5000")
    app.run(host="127.0.0.1", port=5000, debug=False)
