import os
import time
import uuid
import json
from functools import wraps
from flask import Flask, request, jsonify, send_from_directory
from database import get_db_connection, init_db
from pricing_and_queue import (
    CAMPUS_LANDMARKS,
    FLAT_CAMPUS_HOP,
    CITYLINK_PRICING,
    calculate_fare,
    find_nearest_campus_landmark,
    haversine_distance_km
)
from payments import (
    generate_upi_qr,
    create_razorpay_order,
    verify_razorpay_payment,
    credit_wallet_after_payment
)
from emergency_dispatch import dispatch_emergency_alert

app = Flask(__name__, static_folder="../frontend", static_url_path="")

# In-memory rate limiter dictionary: ip_address -> [timestamps]
RATE_LIMIT_STORE = {}

def rate_limit(max_requests=15, window_seconds=60):
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            ip = request.remote_addr or "unknown_ip"
            now = time.time()
            timestamps = RATE_LIMIT_STORE.get(ip, [])
            # Filter out timestamps older than window
            timestamps = [t for t in timestamps if now - t < window_seconds]
            if len(timestamps) >= max_requests:
                return jsonify({
                    "error": "Rate limit exceeded. Please wait a moment before trying again.",
                    "code": "RATE_LIMITED"
                }), 429
            timestamps.append(now)
            RATE_LIMIT_STORE[ip] = timestamps
            return f(*args, **kwargs)
        return wrapped
    return decorator

# --- Static Frontend Serving ---
@app.route("/")
def index():
    return send_from_directory("../frontend", "index.html")

# --- Auth & User Persona Endpoints ---
@app.route("/api/auth/personas", methods=["GET"])
def get_personas():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
    SELECT u.*, w.balance as wallet_balance,
           (SELECT category FROM vehicles WHERE user_id = u.id AND is_active = 1 LIMIT 1) as vehicle_category,
           (SELECT model FROM vehicles WHERE user_id = u.id AND is_active = 1 LIMIT 1) as vehicle_model,
           (SELECT plate_number FROM vehicles WHERE user_id = u.id AND is_active = 1 LIMIT 1) as vehicle_plate
    FROM users u
    LEFT JOIN wallets w ON u.id = w.user_id
    ORDER BY u.is_teacher_priority DESC, u.created_at ASC
    """)
    rows = cur.fetchall()
    conn.close()
    personas = [dict(row) for row in rows]
    return jsonify({"personas": personas})

@app.route("/api/auth/login", methods=["POST"])
def login():
    data = request.json or {}
    user_id = data.get("user_id")
    lpu_id = data.get("lpu_id")

    conn = get_db_connection()
    cur = conn.cursor()

    if user_id:
        cur.execute("SELECT * FROM users WHERE id = ?", (user_id,))
    elif lpu_id:
        cur.execute("SELECT * FROM users WHERE lpu_id = ?", (lpu_id,))
    else:
        # Default to student Aarav if nothing specified
        cur.execute("SELECT * FROM users WHERE id = 'usr_student_aarav'")

    user = cur.fetchone()
    if not user:
        conn.close()
        return jsonify({"error": "User not found with provided LPU credentials"}), 404

    # Fetch wallet
    cur.execute("SELECT balance, currency FROM wallets WHERE user_id = ?", (user["id"],))
    wallet = cur.fetchone()

    # Fetch emergency contacts count
    cur.execute("SELECT COUNT(*) as count FROM emergency_contacts WHERE user_id = ?", (user["id"],))
    contacts_count = cur.fetchone()["count"]

    # Fetch active vehicle if any
    cur.execute("SELECT * FROM vehicles WHERE user_id = ? AND is_active = 1", (user["id"],))
    vehicle = cur.fetchone()

    conn.close()

    user_dict = dict(user)
    user_dict["wallet_balance"] = wallet["balance"] if wallet else 0.0
    user_dict["has_emergency_contacts"] = contacts_count > 0
    user_dict["vehicle"] = dict(vehicle) if vehicle else None

    return jsonify({"user": user_dict})

@app.route("/api/user/role", methods=["POST"])
def update_role():
    data = request.json or {}
    user_id = data.get("user_id")
    role = data.get("role")

    if role not in ("rider", "driver", "both"):
        return jsonify({"error": "Invalid role. Must be rider, driver, or both"}), 400

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))
    conn.commit()
    conn.close()
    return jsonify({"success": True, "role": role})

@app.route("/api/user/vehicle", methods=["POST"])
def save_vehicle():
    data = request.json or {}
    user_id = data.get("user_id")
    category = data.get("category")
    model = data.get("model")
    plate_number = data.get("plate_number")
    color = data.get("color", "")
    capacity = int(data.get("capacity", 1))
    has_helmet = 1 if data.get("has_helmet") else 0
    has_ac = 1 if data.get("has_ac") else 0

    if not user_id or not category or not model or not plate_number:
        return jsonify({"error": "Missing mandatory vehicle details"}), 400

    conn = get_db_connection()
    cur = conn.cursor()
    # Check if vehicle exists
    cur.execute("SELECT id FROM vehicles WHERE user_id = ?", (user_id,))
    existing = cur.fetchone()

    if existing:
        cur.execute("""
        UPDATE vehicles SET category=?, model=?, plate_number=?, color=?, capacity=?, has_helmet=?, has_ac=?, is_active=1
        WHERE user_id=?
        """, (category, model, plate_number, color, capacity, has_helmet, has_ac, user_id))
        veh_id = existing["id"]
    else:
        veh_id = f"veh_{uuid.uuid4().hex[:8]}"
        cur.execute("""
        INSERT INTO vehicles (id, user_id, category, model, plate_number, color, capacity, has_helmet, has_ac, is_active)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
        """, (veh_id, user_id, category, model, plate_number, color, capacity, has_helmet, has_ac))

    # Also make sure user role allows driving
    cur.execute("SELECT role FROM users WHERE id = ?", (user_id,))
    usr = cur.fetchone()
    if usr and usr["role"] == "rider":
        cur.execute("UPDATE users SET role = 'both' WHERE id = ?", (user_id,))

    conn.commit()
    conn.close()
    return jsonify({"success": True, "vehicle_id": veh_id})

# --- Mandatory Trusted Emergency Contacts ---
@app.route("/api/user/emergency-contacts", methods=["GET"])
def get_emergency_contacts():
    user_id = request.args.get("user_id")
    if not user_id:
        return jsonify({"error": "user_id required"}), 400

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM emergency_contacts WHERE user_id = ? ORDER BY is_primary DESC, created_at ASC", (user_id,))
    rows = cur.fetchall()
    conn.close()
    return jsonify({"contacts": [dict(r) for r in rows]})

@app.route("/api/user/emergency-contacts", methods=["POST"])
def add_emergency_contact():
    data = request.json or {}
    user_id = data.get("user_id")
    name = data.get("name")
    relationship = data.get("relationship", "Family/Friend")
    phone = data.get("phone")
    is_primary = 1 if data.get("is_primary") else 0

    if not user_id or not name or not phone:
        return jsonify({"error": "Name and valid phone number are mandatory"}), 400

    contact_id = f"emg_{uuid.uuid4().hex[:8]}"
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO emergency_contacts (id, user_id, name, relationship, phone, is_primary, created_at)
    VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (contact_id, user_id, name, relationship, phone, is_primary, time.time()))
    conn.commit()
    conn.close()
    return jsonify({"success": True, "contact_id": contact_id})

# --- Campus Landmarks & Auto-Location ---
@app.route("/api/campus/landmarks", methods=["GET"])
def list_landmarks():
    return jsonify({"landmarks": CAMPUS_LANDMARKS})

@app.route("/api/campus/locate", methods=["POST"])
def locate():
    data = request.json or {}
    lat = float(data.get("lat", 31.2536))
    lng = float(data.get("lng", 75.7037))
    key, landmark = find_nearest_campus_landmark(lat, lng)
    return jsonify({"nearest_key": key, "landmark": landmark})

# --- Ride Quotes & 100% Server-Side Fare Calculation ---
@app.route("/api/rides/quote", methods=["POST"])
def get_ride_quote():
    data = request.json or {}
    user_id = data.get("user_id")
    pickup_key = data.get("pickup_key")
    drop_key = data.get("drop_key")
    scope = data.get("scope", "campus_hop") # 'campus_hop' or 'citylink'

    pickup = CAMPUS_LANDMARKS.get(pickup_key)
    drop = CAMPUS_LANDMARKS.get(drop_key)

    if not pickup or not drop:
        return jsonify({"error": "Invalid pickup or drop location selected"}), 400

    # Retrieve user's wallet balance
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT balance FROM wallets WHERE user_id = ?", (user_id,))
    wallet_row = cur.fetchone()
    wallet_balance = wallet_row["balance"] if wallet_row else 0.0

    # Count active demand in pickup zone for peak rush surge calculation
    cur.execute("""
    SELECT COUNT(*) as demand_count FROM rides
    WHERE pickup_zone = ? AND status IN ('queued', 'matched', 'arriving')
    """, (pickup["zone"],))
    demand_count = cur.fetchone()["demand_count"]

    # Calculate quotes for all 3 vehicle services: bike, scooty, car
    services = ["bike", "scooty", "car"]
    quotes = {}

    for s in services:
        # Check active online drivers for this service in or near pickup zone
        cur.execute("""
        SELECT COUNT(*) as driver_count FROM driver_locations dl
        JOIN vehicles v ON dl.driver_id = v.user_id
        WHERE dl.is_online = 1 AND v.category = ?
        """, (s,))
        active_drivers = cur.fetchone()["driver_count"]

        fare_info = calculate_fare(
            service_type=s,
            scope=scope,
            pickup_lat=pickup["lat"],
            pickup_lng=pickup["lng"],
            drop_lat=drop["lat"],
            drop_lng=drop["lng"],
            pickup_zone=pickup["zone"],
            demand_count=demand_count
        )
        fare_info["available_drivers"] = active_drivers
        fare_info["has_sufficient_balance"] = (wallet_balance >= fare_info["total_fare"])
        fare_info["deficit"] = max(0.0, round(fare_info["total_fare"] - wallet_balance, 2))
        quotes[s] = fare_info

    conn.close()

    return jsonify({
        "pickup": pickup,
        "drop": drop,
        "wallet_balance": wallet_balance,
        "quotes": quotes
    })

# --- Ride Booking with Peak-Time Zone Scoped Matching ---
@app.route("/api/rides/book", methods=["POST"])
@rate_limit(max_requests=20, window_seconds=60)
def book_ride():
    data = request.json or {}
    rider_id = data.get("user_id")
    pickup_key = data.get("pickup_key")
    drop_key = data.get("drop_key")
    service_type = data.get("service_type")
    scope = data.get("scope", "campus_hop")

    pickup = CAMPUS_LANDMARKS.get(pickup_key)
    drop = CAMPUS_LANDMARKS.get(drop_key)

    if not rider_id or not pickup or not drop or service_type not in ("bike", "scooty", "car"):
        return jsonify({"error": "Invalid booking parameters"}), 400

    conn = get_db_connection()
    cur = conn.cursor()

    # 1. Check user validity & teacher priority flag
    cur.execute("SELECT id, name, user_type, is_teacher_priority FROM users WHERE id = ?", (rider_id,))
    rider = cur.fetchone()
    if not rider:
        conn.close()
        return jsonify({"error": "Rider account not recognized"}), 404

    # 2. Prevent concurrent active rides for the same rider
    cur.execute("""
    SELECT id, status FROM rides
    WHERE rider_id = ? AND status IN ('queued', 'matched', 'arriving', 'in_progress')
    """, (rider_id,))
    existing_ride = cur.fetchone()
    if existing_ride:
        conn.close()
        return jsonify({
            "error": "You already have an active or queued ride request.",
            "code": "CONCURRENT_RIDE_EXISTS",
            "active_ride_id": existing_ride["id"]
        }), 409

    # 3. Calculate 100% server-validated fare
    cur.execute("""
    SELECT COUNT(*) as demand_count FROM rides
    WHERE pickup_zone = ? AND status IN ('queued', 'matched', 'arriving')
    """, (pickup["zone"],))
    demand_count = cur.fetchone()["demand_count"]

    fare_info = calculate_fare(
        service_type=service_type,
        scope=scope,
        pickup_lat=pickup["lat"],
        pickup_lng=pickup["lng"],
        drop_lat=drop["lat"],
        drop_lng=drop["lng"],
        pickup_zone=pickup["zone"],
        demand_count=demand_count
    )

    total_fare = fare_info["total_fare"]
    base_fare = fare_info["base_fare"]
    surge = fare_info["surge_multiplier"]

    # 4. Strictly validate wallet balance BEFORE matching or booking
    cur.execute("SELECT balance FROM wallets WHERE user_id = ?", (rider_id,))
    wallet = cur.fetchone()
    current_balance = wallet["balance"] if wallet else 0.0

    if current_balance < total_fare:
        conn.close()
        deficit = round(total_fare - current_balance, 2)
        return jsonify({
            "error": f"Insufficient wallet balance. Total fare is ₹{total_fare:.2f}, but your wallet balance is ₹{current_balance:.2f}.",
            "code": "INSUFFICIENT_WALLET_BALANCE",
            "required_amount": total_fare,
            "current_balance": current_balance,
            "deficit": deficit
        }), 402

    # 5. Zone-Scoped Matching: Find available driver in pickup zone or adjacent zones
    # Prioritize online drivers with matching vehicle category who are not currently on a ride
    cur.execute("""
    SELECT dl.driver_id, dl.lat, dl.lng, dl.zone, u.name, u.phone, u.avatar_url,
           v.model, v.plate_number, v.color, v.has_helmet, v.has_ac
    FROM driver_locations dl
    JOIN users u ON dl.driver_id = u.id
    JOIN vehicles v ON dl.driver_id = v.user_id
    WHERE dl.is_online = 1
      AND v.category = ?
      AND v.is_active = 1
      AND dl.driver_id NOT IN (
          SELECT driver_id FROM rides
          WHERE status IN ('matched', 'arriving', 'in_progress') AND driver_id IS NOT NULL
      )
    ORDER BY (CASE WHEN dl.zone = ? THEN 0 ELSE 1 END), dl.updated_at DESC
    LIMIT 1;
    """, (service_type, pickup["zone"]))
    matched_driver = cur.fetchone()

    ride_id = f"ride_{uuid.uuid4().hex[:10]}"
    share_token = f"share_{uuid.uuid4().hex[:12]}"
    now = time.time()
    is_priority = 1 if rider["is_teacher_priority"] else 0

    if matched_driver:
        status = "arriving"
        driver_id = matched_driver["driver_id"]
        matched_at = now
    else:
        # Gracefully enqueue the ride with zone-scoped indexing
        status = "queued"
        driver_id = None
        matched_at = None

    cur.execute("""
    INSERT INTO rides (
        id, rider_id, driver_id, service_type, scope,
        pickup_name, pickup_lat, pickup_lng, pickup_zone,
        drop_name, drop_lat, drop_lng, drop_zone,
        fare, base_fare, surge_multiplier, status,
        is_priority, share_token, created_at, matched_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        ride_id, rider_id, driver_id, service_type, scope,
        pickup["name"], pickup["lat"], pickup["lng"], pickup["zone"],
        drop["name"], drop["lat"], drop["lng"], drop["zone"],
        total_fare, base_fare, surge, status,
        is_priority, share_token, now, matched_at
    ))

    conn.commit()

    # Calculate queue position if queued
    queue_position = 0
    if status == "queued":
        cur.execute("""
        SELECT COUNT(*) as pos FROM rides
        WHERE status = 'queued' AND pickup_zone = ?
          AND (is_priority > ? OR (is_priority = ? AND created_at <= ?))
        """, (pickup["zone"], is_priority, is_priority, now))
        queue_position = cur.fetchone()["pos"]

    conn.close()

    return jsonify({
        "success": True,
        "ride_id": ride_id,
        "status": status,
        "is_priority": bool(is_priority),
        "queue_position": queue_position,
        "matched_driver": dict(matched_driver) if matched_driver else None,
        "fare": total_fare,
        "share_token": share_token
    })

# --- Active Ride Telemetry & Status ---
@app.route("/api/rides/active", methods=["GET"])
def get_active_ride():
    user_id = request.args.get("user_id")
    if not user_id:
        return jsonify({"error": "user_id required"}), 400

    conn = get_db_connection()
    cur = conn.cursor()

    # Check if user is rider or driver in an active ride
    cur.execute("""
    SELECT r.*,
           ru.name as rider_name, ru.phone as rider_phone, ru.avatar_url as rider_avatar,
           ru.is_teacher_priority as rider_is_teacher, ru.user_type as rider_type,
           du.name as driver_name, du.phone as driver_phone, du.avatar_url as driver_avatar,
           v.model as vehicle_model, v.plate_number as vehicle_plate, v.color as vehicle_color,
           v.category as vehicle_category, v.has_helmet, v.has_ac,
           dl.lat as driver_live_lat, dl.lng as driver_live_lng
    FROM rides r
    JOIN users ru ON r.rider_id = ru.id
    LEFT JOIN users du ON r.driver_id = du.id
    LEFT JOIN vehicles v ON r.driver_id = v.user_id AND v.is_active = 1
    LEFT JOIN driver_locations dl ON r.driver_id = dl.driver_id
    WHERE (r.rider_id = ? OR r.driver_id = ?)
      AND r.status IN ('queued', 'matched', 'arriving', 'in_progress')
    ORDER BY r.created_at DESC LIMIT 1;
    """, (user_id, user_id))

    ride = cur.fetchone()
    if not ride:
        conn.close()
        return jsonify({"active_ride": None})

    ride_dict = dict(ride)

    # If queued, calculate priority queue position
    if ride_dict["status"] == "queued":
        cur.execute("""
        SELECT COUNT(*) as pos FROM rides
        WHERE status = 'queued' AND pickup_zone = ?
          AND (is_priority > ? OR (is_priority = ? AND created_at <= ?))
        """, (ride_dict["pickup_zone"], ride_dict["is_priority"], ride_dict["is_priority"], ride_dict["created_at"]))
        ride_dict["queue_position"] = cur.fetchone()["pos"]

    conn.close()
    return jsonify({"active_ride": ride_dict})

# --- Live Ride Telemetry Step Simulation ---
@app.route("/api/rides/<ride_id>/telemetry-step", methods=["POST"])
def telemetry_step(ride_id):
    """
    Advances driver simulation smoothly:
    1. 'arriving': moves towards pickup point
    2. 'in_progress': moves from pickup point towards drop point
    3. auto-transitions when close enough
    """
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM rides WHERE id = ?", (ride_id,))
    ride = cur.fetchone()

    if not ride:
        conn.close()
        return jsonify({"error": "Ride not found"}), 404

    status = ride["status"]
    driver_id = ride["driver_id"]

    if status in ("completed", "cancelled") or not driver_id:
        conn.close()
        return jsonify({"status": status})

    # Fetch driver current location
    cur.execute("SELECT lat, lng FROM driver_locations WHERE driver_id = ?", (driver_id,))
    loc = cur.fetchone()
    current_lat = loc["lat"] if loc else ride["pickup_lat"]
    current_lng = loc["lng"] if loc else ride["pickup_lng"]

    # Target depending on state
    if status == "arriving":
        target_lat = ride["pickup_lat"]
        target_lng = ride["pickup_lng"]
        step_factor = 0.35 # Quick arrival
    else: # in_progress
        target_lat = ride["drop_lat"]
        target_lng = ride["drop_lng"]
        step_factor = 0.25 # Steady ride

    # Interpolate towards target
    d_lat = target_lat - current_lat
    d_lng = target_lng - current_lng
    dist_remaining = haversine_distance_km(current_lat, current_lng, target_lat, target_lng)

    new_lat = current_lat + (d_lat * step_factor)
    new_lng = current_lng + (d_lng * step_factor)

    next_status = status
    if status == "arriving" and dist_remaining < 0.08: # Within ~80m of pickup
        next_status = "in_progress"
        cur.execute("UPDATE rides SET status = 'in_progress', started_at = ? WHERE id = ?", (time.time(), ride_id))
    elif status == "in_progress" and dist_remaining < 0.08: # Arrived at destination
        # Ready to complete
        pass

    cur.execute("""
    UPDATE driver_locations SET lat = ?, lng = ?, updated_at = ?
    WHERE driver_id = ?
    """, (new_lat, new_lng, time.time(), driver_id))

    conn.commit()
    conn.close()

    return jsonify({
        "status": next_status,
        "current_lat": new_lat,
        "current_lng": new_lng,
        "distance_remaining_km": round(dist_remaining, 2)
    })

# --- Complete Ride & Server-Side Wallet Auto-Deduction ---
@app.route("/api/rides/<ride_id>/complete", methods=["POST"])
def complete_ride(ride_id):
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("SELECT * FROM rides WHERE id = ?", (ride_id,))
    ride = cur.fetchone()
    if not ride:
        conn.close()
        return jsonify({"error": "Ride not found"}), 404

    if ride["status"] == "completed":
        conn.close()
        return jsonify({"success": True, "message": "Ride already completed"})

    fare = ride["fare"]
    rider_id = ride["rider_id"]
    driver_id = ride["driver_id"]
    now = time.time()

    # 1. Deduct from rider's wallet
    cur.execute("SELECT balance FROM wallets WHERE user_id = ?", (rider_id,))
    rider_wallet = cur.fetchone()
    rider_bal = rider_wallet["balance"] if rider_wallet else 0.0
    new_rider_bal = rider_bal - fare

    cur.execute("UPDATE wallets SET balance = ?, updated_at = ? WHERE user_id = ?", (new_rider_bal, now, rider_id))
    cur.execute("""
    INSERT INTO wallet_transactions (id, user_id, amount, type, reference_id, description, balance_after, created_at)
    VALUES (?, ?, ?, 'fare_deduction', ?, ?, ?, ?)
    """, (str(uuid.uuid4()), rider_id, -fare, ride_id, f"CampusGo Ride #{ride_id[:8]}", new_rider_bal, now))

    # 2. Credit driver's wallet (Driver payout: 90% after platform contribution)
    if driver_id:
        driver_payout = round(fare * 0.90, 2)
        cur.execute("SELECT balance FROM wallets WHERE user_id = ?", (driver_id,))
        driver_wallet = cur.fetchone()
        driver_bal = driver_wallet["balance"] if driver_wallet else 0.0
        new_driver_bal = driver_bal + driver_payout

        cur.execute("UPDATE wallets SET balance = ?, updated_at = ? WHERE user_id = ?", (new_driver_bal, now, driver_id))
        cur.execute("""
        INSERT INTO wallet_transactions (id, user_id, amount, type, reference_id, description, balance_after, created_at)
        VALUES (?, ?, ?, 'driver_payout', ?, ?, ?, ?)
        """, (str(uuid.uuid4()), driver_id, driver_payout, ride_id, f"Payout for Ride #{ride_id[:8]}", new_driver_bal, now))

    # 3. Mark ride as completed
    cur.execute("UPDATE rides SET status = 'completed', completed_at = ? WHERE id = ?", (now, ride_id))

    conn.commit()
    conn.close()

    return jsonify({
        "success": True,
        "fare": fare,
        "rider_balance_after": new_rider_bal,
        "driver_payout": driver_payout if driver_id else 0.0
    })

# --- Mutual Ratings & Reviews ---
@app.route("/api/rides/<ride_id>/rate", methods=["POST"])
def rate_ride(ride_id):
    data = request.json or {}
    reviewer_id = data.get("reviewer_id")
    rating = int(data.get("rating", 5))
    tags = data.get("tags", "")
    comment = data.get("comment", "")

    if not reviewer_id or not (1 <= rating <= 5):
        return jsonify({"error": "Valid rating between 1 and 5 is required"}), 400

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT rider_id, driver_id FROM rides WHERE id = ?", (ride_id,))
    ride = cur.fetchone()

    if not ride:
        conn.close()
        return jsonify({"error": "Ride not found"}), 404

    reviewee_id = ride["driver_id"] if reviewer_id == ride["rider_id"] else ride["rider_id"]

    cur.execute("""
    INSERT INTO ride_reviews (id, ride_id, reviewer_id, reviewee_id, rating, tags, comment, created_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (str(uuid.uuid4()), ride_id, reviewer_id, reviewee_id, rating, tags, comment, time.time()))

    conn.commit()
    conn.close()
    return jsonify({"success": True})

# --- SOS Emergency Alert System ---
@app.route("/api/sos/trigger", methods=["POST"])
@rate_limit(max_requests=10, window_seconds=60)
def trigger_sos():
    data = request.json or {}
    user_id = data.get("user_id")
    ride_id = data.get("ride_id")
    lat = float(data.get("lat", 31.2536))
    lng = float(data.get("lng", 75.7037))
    location_name = data.get("location_name", "Near LPU Campus")

    if not user_id:
        return jsonify({"error": "user_id is required"}), 400

    conn = get_db_connection()
    cur = conn.cursor()

    # Fetch user info
    cur.execute("SELECT name, lpu_id FROM users WHERE id = ?", (user_id,))
    user = cur.fetchone()
    user_name = user["name"] if user else "CampusGo User"
    lpu_id = user["lpu_id"] if user else "Unknown ID"

    # Get emergency contacts to notify
    cur.execute("SELECT name, phone, relationship FROM emergency_contacts WHERE user_id = ?", (user_id,))
    contacts = [dict(c) for c in cur.fetchall()]

    # Fetch share token if ride exists
    share_token = None
    if ride_id:
        cur.execute("SELECT share_token FROM rides WHERE id = ?", (ride_id,))
        r = cur.fetchone()
        if r:
            share_token = r["share_token"]

    alert_id = f"sos_{uuid.uuid4().hex[:8]}"
    cur.execute("""
    INSERT INTO sos_alerts (id, ride_id, user_id, lat, lng, location_name, status, notified_contacts_count, notes, created_at)
    VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)
    """, (alert_id, ride_id, user_id, lat, lng, location_name, len(contacts), "SOS Beacon Triggered via Mobile App", time.time()))

    conn.commit()
    conn.close()

    # Dispatch via multi-channel SMS / WhatsApp / Hotline
    dispatch_res = dispatch_emergency_alert(
        user_name=user_name,
        lpu_id=lpu_id,
        location_name=location_name,
        lat=lat,
        lng=lng,
        contacts=contacts,
        share_token=share_token
    )

    return jsonify({
        "success": True,
        "alert_id": alert_id,
        "status": "DISPATCHED",
        "sos_message": dispatch_res["sos_message"],
        "contacts_notified": dispatch_res["contacts_dispatched"],
        "campus_security_dispatch": dispatch_res["campus_security_dispatch"],
        "campus_security_hotline": "+91 1824 517000 (Block 30 Control Room)"
    })

# --- Public Live Tracking Share Link ---
@app.route("/api/rides/share/<share_token>", methods=["GET"])
def get_shared_ride(share_token):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
    SELECT r.id, r.service_type, r.scope, r.pickup_name, r.drop_name, r.status, r.created_at,
           ru.name as rider_name, ru.is_teacher_priority,
           du.name as driver_name, du.phone as driver_phone,
           v.model as vehicle_model, v.plate_number as vehicle_plate, v.color as vehicle_color,
           dl.lat as driver_lat, dl.lng as driver_lng
    FROM rides r
    JOIN users ru ON r.rider_id = ru.id
    LEFT JOIN users du ON r.driver_id = du.id
    LEFT JOIN vehicles v ON r.driver_id = v.user_id AND v.is_active = 1
    LEFT JOIN driver_locations dl ON r.driver_id = dl.driver_id
    WHERE r.share_token = ?;
    """, (share_token,))
    ride = cur.fetchone()
    conn.close()

    if not ride:
        return jsonify({"error": "Invalid or expired tracking link"}), 404

    return jsonify({"ride": dict(ride)})

# --- Driver Actions: Queue Acceptance & Future CityLink Routes ---
@app.route("/api/driver/toggle-online", methods=["POST"])
def toggle_driver_online():
    data = request.json or {}
    driver_id = data.get("driver_id")
    is_online = 1 if data.get("is_online", True) else 0
    zone = data.get("zone", "Zone-Central")

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO driver_locations (driver_id, is_online, lat, lng, zone, heading, updated_at)
    VALUES (?, ?, 31.2536, 75.7038, ?, 0.0, ?)
    ON CONFLICT(driver_id) DO UPDATE SET is_online = ?, zone = ?, updated_at = ?;
    """, (driver_id, is_online, zone, time.time(), is_online, zone, time.time()))
    conn.commit()
    conn.close()
    return jsonify({"success": True, "is_online": bool(is_online)})

@app.route("/api/driver/requests", methods=["GET"])
def get_driver_requests():
    """Lists queued ride requests in the driver's zone, prioritizing teachers."""
    driver_id = request.args.get("driver_id")
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("""
    SELECT r.*, u.name as rider_name, u.user_type as rider_type, u.is_teacher_priority
    FROM rides r
    JOIN users u ON r.rider_id = u.id
    WHERE r.status = 'queued'
    ORDER BY r.is_priority DESC, r.created_at ASC;
    """)
    rows = cur.fetchall()
    conn.close()
    return jsonify({"requests": [dict(r) for r in rows]})

@app.route("/api/driver/accept", methods=["POST"])
def driver_accept_request():
    data = request.json or {}
    driver_id = data.get("driver_id")
    ride_id = data.get("ride_id")

    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("SELECT status FROM rides WHERE id = ?", (ride_id,))
    ride = cur.fetchone()
    if not ride or ride["status"] != "queued":
        conn.close()
        return jsonify({"error": "Ride request is no longer waiting in queue"}), 400

    cur.execute("""
    UPDATE rides SET driver_id = ?, status = 'arriving', matched_at = ?
    WHERE id = ?
    """, (driver_id, time.time(), ride_id))

    conn.commit()
    conn.close()
    return jsonify({"success": True, "ride_id": ride_id, "status": "arriving"})

@app.route("/api/driver/earnings", methods=["GET"])
def get_driver_earnings():
    driver_id = request.args.get("driver_id")
    if not driver_id:
        return jsonify({"error": "driver_id required"}), 400

    conn = get_db_connection()
    cur = conn.cursor()

    # Completed rides count & total earned
    cur.execute("""
    SELECT COUNT(*) as total_trips, COALESCE(SUM(fare * 0.90), 0.0) as total_earnings
    FROM rides WHERE driver_id = ? AND status = 'completed';
    """, (driver_id,))
    summary = cur.fetchone()

    # Average rating
    cur.execute("""
    SELECT COALESCE(AVG(rating), 5.0) as avg_rating, COUNT(*) as review_count
    FROM ride_reviews WHERE reviewee_id = ?;
    """, (driver_id,))
    rating_row = cur.fetchone()

    # Recent transactions
    cur.execute("""
    SELECT * FROM wallet_transactions WHERE user_id = ?
    ORDER BY created_at DESC LIMIT 10;
    """, (driver_id,))
    txs = cur.fetchall()

    conn.close()

    return jsonify({
        "total_trips": summary["total_trips"],
        "total_earnings": round(summary["total_earnings"], 2),
        "avg_rating": round(rating_row["avg_rating"], 1),
        "review_count": rating_row["review_count"],
        "recent_transactions": [dict(t) for t in txs]
    })

# --- CityLink Scheduled Future Routes (Plan, Pin, Cockpit & Reset) ---
@app.route("/api/driver/routes", methods=["GET"])
@app.route("/api/routes/scheduled", methods=["GET"])
def get_scheduled_routes():
    user_id = request.args.get("user_id")
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("""
    SELECT dr.*, u.name as driver_name, u.phone as driver_phone, u.avatar_url as driver_avatar,
           v.model as vehicle_model, v.category as vehicle_category, v.plate_number as vehicle_plate
    FROM driver_routes dr
    JOIN users u ON dr.driver_id = u.id
    LEFT JOIN vehicles v ON dr.driver_id = v.user_id AND v.is_active = 1
    WHERE dr.status IN ('open', 'pinned', 'in_progress')
    ORDER BY (CASE WHEN dr.status = 'pinned' THEN 0 WHEN dr.status = 'in_progress' THEN 1 ELSE 2 END), dr.created_at DESC;
    """)
    routes = [dict(r) for r in cur.fetchall()]

    for r in routes:
        # Fetch passenger manifest for each route
        cur.execute("""
        SELECT rb.id as booking_id, rb.seats, rb.fare_paid, rb.status as booking_status,
               u.id as passenger_id, u.name as passenger_name, u.phone as passenger_phone,
               u.avatar_url as passenger_avatar, u.lpu_id as passenger_lpu_id
        FROM route_bookings rb
        JOIN users u ON rb.rider_id = u.id
        WHERE rb.route_id = ? AND rb.status IN ('confirmed', 'in_progress')
        """, (r["id"],))
        r["passengers"] = [dict(p) for p in cur.fetchall()]
        r["is_host"] = (user_id and r["driver_id"] == user_id)
        r["has_joined"] = any(p["passenger_id"] == user_id for p in r["passengers"]) if user_id else False

    conn.close()
    return jsonify({"routes": routes})

@app.route("/api/routes/plan", methods=["POST"])
@app.route("/api/driver/routes", methods=["POST"])
def plan_future_route():
    data = request.json or {}
    driver_id = data.get("driver_id")
    origin = data.get("origin", "LPU Uni-Mall Plaza")
    destination = data.get("destination")
    departure_time = data.get("departure_time")
    total_seats = int(data.get("total_seats", data.get("available_seats", 3)))
    price_per_seat = float(data.get("price_per_seat", 80.0))
    notes = data.get("notes", "")

    if not driver_id or not destination or not departure_time:
        return jsonify({"error": "Destination and departure time are required"}), 400

    # Look up landmark coords for origin
    orig_lat, orig_lng = 31.2535, 75.7038 # Default Uni-Mall
    for key, l in CAMPUS_LANDMARKS.items():
        if l["name"].lower() in origin.lower() or key in origin.lower():
            orig_lat = l["lat"]
            orig_lng = l["lng"]
            break

    # Look up landmark coords for destination
    dest_lat, dest_lng = 31.3190, 75.5860 # Default Jalandhar
    for key, l in CAMPUS_LANDMARKS.items():
        if l["name"].lower() in destination.lower() or key in destination.lower():
            dest_lat = l["lat"]
            dest_lng = l["lng"]
            break

    route_id = f"route_{uuid.uuid4().hex[:8]}"
    share_token = f"share_route_{uuid.uuid4().hex[:10]}"
    now = time.time()

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO driver_routes (
        id, driver_id, origin, origin_lat, origin_lng,
        destination, destination_lat, destination_lng,
        departure_time, available_seats, total_seats, price_per_seat,
        notes, status, share_token, current_lat, current_lng, created_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?)
    """, (
        route_id, driver_id, origin, orig_lat, orig_lng,
        destination, dest_lat, dest_lng,
        departure_time, total_seats, total_seats, price_per_seat,
        notes, share_token, orig_lat, orig_lng, now
    ))
    conn.commit()
    conn.close()

    return jsonify({"success": True, "route_id": route_id, "status": "open", "total_seats": total_seats})

@app.route("/api/routes/<route_id>/join", methods=["POST"])
def join_route(route_id):
    data = request.json or {}
    rider_id = data.get("rider_id")
    seats = int(data.get("seats", 1))

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM driver_routes WHERE id = ? AND status IN ('open', 'pinned')", (route_id,))
    route = cur.fetchone()

    if not route:
        conn.close()
        return jsonify({"error": "Route is no longer available"}), 404

    if route["available_seats"] < seats:
        conn.close()
        return jsonify({"error": f"Only {route['available_seats']} seat(s) remaining"}), 400

    fare = route["price_per_seat"] * seats

    # Check rider wallet
    cur.execute("SELECT balance FROM wallets WHERE user_id = ?", (rider_id,))
    w = cur.fetchone()
    bal = w["balance"] if w else 0.0

    if bal < fare:
        conn.close()
        return jsonify({
            "error": "Insufficient wallet balance",
            "required": fare,
            "current_balance": bal,
            "deficit": round(fare - bal, 2)
        }), 402

    now = time.time()
    # Deduct wallet
    cur.execute("UPDATE wallets SET balance = balance - ?, updated_at = ? WHERE user_id = ?", (fare, now, rider_id))
    cur.execute("""
    INSERT INTO wallet_transactions (id, user_id, amount, type, reference_id, description, balance_after, created_at)
    VALUES (?, ?, ?, 'fare_deduction', ?, ?, ?, ?)
    """, (str(uuid.uuid4()), rider_id, -fare, route_id, f"Booked {seats} Seat(s) to {route['destination']}", bal - fare, now))

    # Calculate remaining seats & auto-pin if full
    remaining_seats = route["available_seats"] - seats
    new_status = 'pinned' if remaining_seats == 0 else 'open'

    cur.execute("UPDATE driver_routes SET available_seats = ?, status = ? WHERE id = ?", (remaining_seats, new_status, route_id))

    # Booking entry
    booking_id = f"bk_{uuid.uuid4().hex[:8]}"
    cur.execute("""
    INSERT INTO route_bookings (id, route_id, rider_id, seats, fare_paid, status, created_at)
    VALUES (?, ?, ?, ?, ?, 'confirmed', ?)
    """, (booking_id, route_id, rider_id, seats, fare, now))

    conn.commit()
    conn.close()

    return jsonify({
        "success": True,
        "booking_id": booking_id,
        "fare_paid": fare,
        "seats_booked": seats,
        "remaining_seats": remaining_seats,
        "status": new_status,
        "is_pinned": (new_status == 'pinned')
    })

# --- During-Ride Cockpit Activation & Lifecycle ---
@app.route("/api/routes/<route_id>/start", methods=["POST"])
def start_scheduled_route(route_id):
    """
    Activates the During-Ride Cockpit Dashboard when confirmed by host/passenger.
    """
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM driver_routes WHERE id = ?", (route_id,))
    route = cur.fetchone()

    if not route:
        conn.close()
        return jsonify({"error": "Route not found"}), 404

    now = time.time()
    cur.execute("""
    UPDATE driver_routes SET status = 'in_progress', started_at = ?
    WHERE id = ?
    """, (now, route_id))

    cur.execute("""
    UPDATE route_bookings SET status = 'in_progress'
    WHERE route_id = ? AND status = 'confirmed'
    """, (route_id,))

    conn.commit()
    conn.close()

    return jsonify({"success": True, "route_id": route_id, "status": "in_progress"})

@app.route("/api/routes/<route_id>/live", methods=["GET"])
def get_live_cockpit(route_id):
    """
    Returns full telemetry, passenger manifest, and progress for During-Ride Dashboard.
    """
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("""
    SELECT dr.*, u.name as driver_name, u.phone as driver_phone, u.avatar_url as driver_avatar,
           v.model as vehicle_model, v.plate_number as vehicle_plate, v.category as vehicle_category
    FROM driver_routes dr
    JOIN users u ON dr.driver_id = u.id
    LEFT JOIN vehicles v ON dr.driver_id = v.user_id AND v.is_active = 1
    WHERE dr.id = ?
    """, (route_id,))
    route = cur.fetchone()

    if not route:
        conn.close()
        return jsonify({"error": "Route not found"}), 404

    route_dict = dict(route)

    # Passengers Manifest
    cur.execute("""
    SELECT rb.id as booking_id, rb.seats, rb.fare_paid, rb.status as booking_status,
           u.id as passenger_id, u.name as passenger_name, u.phone as passenger_phone,
           u.avatar_url as passenger_avatar, u.lpu_id as passenger_lpu_id
    FROM route_bookings rb
    JOIN users u ON rb.rider_id = u.id
    WHERE rb.route_id = ? AND rb.status IN ('confirmed', 'in_progress', 'completed')
    """, (route_id,))
    route_dict["passengers"] = [dict(p) for p in cur.fetchall()]

    # Distance remaining
    c_lat = route_dict["current_lat"] or route_dict["origin_lat"]
    c_lng = route_dict["current_lng"] or route_dict["origin_lng"]
    d_lat = route_dict["destination_lat"]
    d_lng = route_dict["destination_lng"]
    dist_rem = haversine_distance_km(c_lat, c_lng, d_lat, d_lng)
    route_dict["distance_remaining_km"] = round(dist_rem, 2)
    route_dict["eta_minutes"] = max(3, int(dist_rem * 2.5) + 2)

    conn.close()
    return jsonify({"cockpit": route_dict})

@app.route("/api/routes/<route_id>/telemetry-step", methods=["POST"])
def route_telemetry_step(route_id):
    """Advances vehicle along scheduled route for the live cockpit."""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM driver_routes WHERE id = ?", (route_id,))
    route = cur.fetchone()

    if not route or route["status"] != "in_progress":
        conn.close()
        return jsonify({"error": "Route is not active"}), 400

    curr_lat = route["current_lat"] or route["origin_lat"]
    curr_lng = route["current_lng"] or route["origin_lng"]
    target_lat = route["destination_lat"]
    target_lng = route["destination_lng"]

    d_lat = target_lat - curr_lat
    d_lng = target_lng - curr_lng
    new_lat = curr_lat + (d_lat * 0.30)
    new_lng = curr_lng + (d_lng * 0.30)

    dist_rem = haversine_distance_km(new_lat, new_lng, target_lat, target_lng)

    cur.execute("""
    UPDATE driver_routes SET current_lat = ?, current_lng = ?
    WHERE id = ?
    """, (new_lat, new_lng, route_id))

    conn.commit()
    conn.close()

    return jsonify({
        "status": "in_progress",
        "current_lat": new_lat,
        "current_lng": new_lng,
        "distance_remaining_km": round(dist_rem, 2)
    })

@app.route("/api/routes/<route_id>/complete", methods=["POST"])
def complete_scheduled_route(route_id):
    """
    Concludes scheduled ride, disburses earnings to host, and resets dashboard.
    """
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM driver_routes WHERE id = ?", (route_id,))
    route = cur.fetchone()

    if not route:
        conn.close()
        return jsonify({"error": "Route not found"}), 404

    now = time.time()
    driver_id = route["driver_id"]

    # Calculate total revenue collected from all passenger bookings
    cur.execute("SELECT COALESCE(SUM(fare_paid), 0.0) as total_rev FROM route_bookings WHERE route_id = ?", (route_id,))
    total_revenue = cur.fetchone()["total_rev"]
    driver_payout = round(total_revenue * 0.90, 2)

    if driver_payout > 0:
        cur.execute("UPDATE wallets SET balance = balance + ?, updated_at = ? WHERE user_id = ?", (driver_payout, now, driver_id))
        cur.execute("""
        INSERT INTO wallet_transactions (id, user_id, amount, type, reference_id, description, balance_after, created_at)
        VALUES (?, ?, ?, 'driver_payout', ?, ?, (SELECT balance FROM wallets WHERE user_id = ?), ?)
        """, (str(uuid.uuid4()), driver_id, driver_payout, route_id, f"Carpool Payout: {route['destination']}", driver_id, now))

    # Mark route as completed
    cur.execute("UPDATE driver_routes SET status = 'completed', completed_at = ? WHERE id = ?", (now, route_id))
    cur.execute("UPDATE route_bookings SET status = 'completed', completed_at = ? WHERE route_id = ?", (now, route_id))

    conn.commit()
    conn.close()

    return jsonify({
        "success": True,
        "status": "completed",
        "reset_dashboard": True,
        "total_revenue": total_revenue,
        "driver_payout": driver_payout
    })

@app.route("/api/user/active-session", methods=["GET"])
def get_user_active_session():
    """
    Detects if user is currently inside a during-ride cockpit (as host or passenger).
    Used for instant real-time sync and clean reset.
    """
    user_id = request.args.get("user_id")
    if not user_id:
        return jsonify({"active_session": None})

    conn = get_db_connection()
    cur = conn.cursor()

    # 1. Check if user is in an active scheduled carpool (in_progress or pinned)
    cur.execute("""
    SELECT dr.id as route_id, dr.origin, dr.destination, dr.departure_time, dr.status,
           dr.price_per_seat, dr.available_seats, dr.total_seats,
           (CASE WHEN dr.driver_id = ? THEN 1 ELSE 0 END) as is_host
    FROM driver_routes dr
    WHERE (dr.driver_id = ? OR dr.id IN (
        SELECT route_id FROM route_bookings WHERE rider_id = ? AND status IN ('confirmed', 'in_progress')
    ))
    AND dr.status IN ('pinned', 'in_progress')
    ORDER BY dr.started_at DESC LIMIT 1;
    """, (user_id, user_id, user_id))
    active_route = cur.fetchone()

    if active_route:
        conn.close()
        return jsonify({"active_session": dict(active_route), "session_type": "scheduled_route"})

    # 2. Check regular on-demand active ride
    cur.execute("""
    SELECT id as ride_id, pickup_name, drop_name, status, fare,
           (CASE WHEN driver_id = ? THEN 1 ELSE 0 END) as is_driver
    FROM rides
    WHERE (rider_id = ? OR driver_id = ?)
      AND status IN ('matched', 'arriving', 'in_progress')
    ORDER BY created_at DESC LIMIT 1;
    """, (user_id, user_id, user_id))
    active_ride = cur.fetchone()

    conn.close()
    if active_ride:
        return jsonify({"active_session": dict(active_ride), "session_type": "on_demand_ride"})

    return jsonify({"active_session": None, "session_type": None})

# --- Server-Side Wallet Management ---
@app.route("/api/wallet", methods=["GET"])
def get_wallet():
    user_id = request.args.get("user_id")
    if not user_id:
        return jsonify({"error": "user_id required"}), 400

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT balance, currency FROM wallets WHERE user_id = ?", (user_id,))
    wallet = cur.fetchone()

    cur.execute("""
    SELECT * FROM wallet_transactions WHERE user_id = ?
    ORDER BY created_at DESC LIMIT 20;
    """, (user_id,))
    txs = cur.fetchall()
    conn.close()

    return jsonify({
        "balance": wallet["balance"] if wallet else 0.0,
        "currency": "INR",
        "transactions": [dict(t) for t in txs]
    })

@app.route("/api/wallet/topup", methods=["POST"])
@rate_limit(max_requests=10, window_seconds=60)
def topup_wallet():
    data = request.json or {}
    user_id = data.get("user_id")
    amount = float(data.get("amount", 0.0))

    if not user_id or amount <= 0:
        return jsonify({"error": "Invalid topup amount. Must be greater than 0"}), 400

    if amount > 5000:
        return jsonify({"error": "Maximum single topup allowed is ₹5000"}), 400

    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("SELECT balance FROM wallets WHERE user_id = ?", (user_id,))
    wallet = cur.fetchone()
    old_bal = wallet["balance"] if wallet else 0.0
    new_bal = old_bal + amount
    now = time.time()

    cur.execute("UPDATE wallets SET balance = ?, updated_at = ? WHERE user_id = ?", (new_bal, now, user_id))
    cur.execute("""
    INSERT INTO wallet_transactions (id, user_id, amount, type, reference_id, description, balance_after, created_at)
    VALUES (?, ?, ?, 'topup', ?, 'CampusGo Wallet Top-up', ?, ?)
    """, (str(uuid.uuid4()), user_id, amount, f"topup_{int(now)}", new_bal, now))

    conn.commit()
    conn.close()

    return jsonify({"success": True, "new_balance": new_bal, "amount_added": amount})

# --- Enhanced Payment Gateway Endpoints: UPI QR & Razorpay ---
@app.route("/api/payments/upi/create-qr", methods=["POST"])
@rate_limit(max_requests=15, window_seconds=60)
def upi_create_qr():
    data = request.json or {}
    user_id = data.get("user_id")
    amount = float(data.get("amount", 100.0))
    if not user_id or amount <= 0:
        return jsonify({"error": "Invalid user_id or amount"}), 400
    res = generate_upi_qr(user_id, amount)
    return jsonify(res)

@app.route("/api/payments/upi/confirm", methods=["POST"])
@rate_limit(max_requests=15, window_seconds=60)
def upi_confirm():
    data = request.json or {}
    user_id = data.get("user_id")
    amount = float(data.get("amount", 0.0))
    reference_id = data.get("reference_id", f"UPI_{int(time.time())}")
    if not user_id or amount <= 0:
        return jsonify({"error": "Invalid user_id or amount"}), 400
    try:
        result = credit_wallet_after_payment(user_id, amount, "upi", reference_id)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/payments/razorpay/create-order", methods=["POST"])
@rate_limit(max_requests=15, window_seconds=60)
def razorpay_create():
    data = request.json or {}
    user_id = data.get("user_id")
    amount = float(data.get("amount", 100.0))
    if not user_id or amount <= 0:
        return jsonify({"error": "Invalid user_id or amount"}), 400
    res = create_razorpay_order(user_id, amount)
    return jsonify(res)

@app.route("/api/payments/razorpay/verify", methods=["POST"])
@rate_limit(max_requests=15, window_seconds=60)
def razorpay_verify():
    data = request.json or {}
    user_id = data.get("user_id")
    order_id = data.get("order_id")
    payment_id = data.get("payment_id", f"pay_{uuid.uuid4().hex[:10]}")
    signature = data.get("signature", "demo_signature_valid")
    amount = float(data.get("amount", 0.0))

    if not user_id or not order_id or amount <= 0:
        return jsonify({"error": "Invalid payment verification parameters"}), 400

    is_valid = verify_razorpay_payment(order_id, payment_id, signature)
    if not is_valid:
        return jsonify({"error": "Payment signature verification failed", "code": "INVALID_SIGNATURE"}), 400

    try:
        result = credit_wallet_after_payment(user_id, amount, "razorpay", payment_id)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 400

if __name__ == "__main__":
    init_db()
    print("Starting CampusGo API Server on http://127.0.0.1:5000")
    app.run(host="127.0.0.1", port=5000, debug=False)
