import sqlite3
import os

DB_PATH = os.environ.get("DATABASE_PATH") or os.path.join(os.path.dirname(__file__), "campusgo.db")

def get_db_connection():
    # timeout: wait for a competing writer instead of failing instantly.
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    # WAL lets readers proceed while a write is in progress.
    cursor.execute("PRAGMA journal_mode=WAL;")

    # Users table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY,
        lpu_id TEXT UNIQUE NOT NULL,
        name TEXT NOT NULL,
        email TEXT NOT NULL,
        phone TEXT NOT NULL,
        user_type TEXT NOT NULL CHECK(user_type IN ('student', 'teacher')),
        role TEXT NOT NULL CHECK(role IN ('rider', 'driver', 'both')),
        password_hash TEXT NOT NULL DEFAULT '',
        is_verified INTEGER NOT NULL DEFAULT 1,
        is_teacher_priority INTEGER NOT NULL DEFAULT 0,
        department TEXT,
        avatar_url TEXT,
        created_at REAL NOT NULL
    );
    """)

    # Upgrade databases created before password-based login was added.
    existing_user_columns = {
        row[1] for row in cursor.execute("PRAGMA table_info(users)").fetchall()
    }
    if "password_hash" not in existing_user_columns:
        cursor.execute("ALTER TABLE users ADD COLUMN password_hash TEXT NOT NULL DEFAULT ''")
    # Sign-up with a unique username and OTP-verified mobile number.
    if "username" not in existing_user_columns:
        cursor.execute("ALTER TABLE users ADD COLUMN username TEXT")
    if "phone_key" not in existing_user_columns:
        cursor.execute("ALTER TABLE users ADD COLUMN phone_key TEXT")
    # Bumped on logout so every token issued earlier stops working.
    if "token_version" not in existing_user_columns:
        cursor.execute("ALTER TABLE users ADD COLUMN token_version INTEGER NOT NULL DEFAULT 0")
    # Canonical phone (last 10 digits) and a username for accounts created before these columns existed.
    seen_keys = set()
    for row in cursor.execute("SELECT id, phone, lpu_id, username, phone_key FROM users ORDER BY created_at").fetchall():
        key = row[4] or "".join(ch for ch in (row[1] or "") if ch.isdigit())[-10:]
        # Two legacy accounts sharing a number would break the unique index and stop the app
        # booting; the later one gets a placeholder key and must be sorted out by an admin.
        if key in seen_keys:
            key = f"dup_{row[0]}"
        seen_keys.add(key)
        uname = row[3]
        if not uname:
            base = "".join(ch for ch in (row[2] or "").lower() if ch.isalnum()) or "user"
            uname, n = base, 1
            while cursor.execute("SELECT 1 FROM users WHERE lower(username) = ?", (uname,)).fetchone():
                n += 1
                uname = f"{base}{n}"
        cursor.execute("UPDATE users SET username = ?, phone_key = ? WHERE id = ?", (uname, key, row[0]))
    cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username ON users(lower(username))")
    cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_phone_key ON users(phone_key)")

    # One-time passwords sent by SMS. Only a keyed hash of the code is stored.
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS otp_challenges (
        id TEXT PRIMARY KEY,
        purpose TEXT NOT NULL CHECK(purpose IN ('signup', 'login')),
        phone_key TEXT NOT NULL,
        code_hash TEXT NOT NULL,
        payload TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        consumed INTEGER NOT NULL DEFAULT 0,
        created_at REAL NOT NULL,
        expires_at REAL NOT NULL
    );
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_otp_phone ON otp_challenges(phone_key, created_at);")

    # Vehicles table (for drivers)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS vehicles (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        category TEXT NOT NULL CHECK(category IN ('bike', 'scooty', 'car')),
        model TEXT NOT NULL,
        plate_number TEXT NOT NULL,
        color TEXT NOT NULL,
        capacity INTEGER NOT NULL DEFAULT 1,
        has_helmet INTEGER NOT NULL DEFAULT 1,
        has_ac INTEGER NOT NULL DEFAULT 0,
        is_active INTEGER NOT NULL DEFAULT 1,
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    );
    """)

    # Wallets table (server-side balance authority)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS wallets (
        user_id TEXT PRIMARY KEY,
        balance REAL NOT NULL DEFAULT 0.0,
        currency TEXT NOT NULL DEFAULT 'INR',
        updated_at REAL NOT NULL,
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    );
    """)

    # Wallet transactions ledger
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS wallet_transactions (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        amount REAL NOT NULL,
        type TEXT NOT NULL CHECK(type IN ('topup', 'fare_deduction', 'driver_payout', 'refund')),
        reference_id TEXT,
        description TEXT NOT NULL,
        balance_after REAL NOT NULL,
        created_at REAL NOT NULL,
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    );
    """)

    # Mandatory trusted emergency contacts
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS emergency_contacts (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        name TEXT NOT NULL,
        relationship TEXT NOT NULL,
        phone TEXT NOT NULL,
        is_primary INTEGER NOT NULL DEFAULT 1,
        created_at REAL NOT NULL,
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    );
    """)

    # Rides table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS rides (
        id TEXT PRIMARY KEY,
        rider_id TEXT NOT NULL,
        driver_id TEXT,
        service_type TEXT NOT NULL CHECK(service_type IN ('bike', 'scooty', 'car')),
        scope TEXT NOT NULL CHECK(scope IN ('campus_hop', 'citylink')),
        pickup_name TEXT NOT NULL,
        pickup_lat REAL NOT NULL,
        pickup_lng REAL NOT NULL,
        pickup_zone TEXT NOT NULL,
        drop_name TEXT NOT NULL,
        drop_lat REAL NOT NULL,
        drop_lng REAL NOT NULL,
        drop_zone TEXT NOT NULL,
        fare REAL NOT NULL,
        base_fare REAL NOT NULL,
        surge_multiplier REAL NOT NULL DEFAULT 1.0,
        status TEXT NOT NULL CHECK(status IN ('queued', 'matched', 'arriving', 'in_progress', 'completed', 'cancelled')),
        is_priority INTEGER NOT NULL DEFAULT 0,
        share_token TEXT UNIQUE,
        created_at REAL NOT NULL,
        matched_at REAL,
        started_at REAL,
        completed_at REAL,
        cancelled_at REAL,
        cancellation_reason TEXT,
        FOREIGN KEY(rider_id) REFERENCES users(id),
        FOREIGN KEY(driver_id) REFERENCES users(id)
    );
    """)

    # Ride start PIN: the rider tells it to the driver at pickup; nothing else can start a ride.
    existing_ride_columns = {row[1] for row in cursor.execute("PRAGMA table_info(rides)").fetchall()}
    if "start_pin" not in existing_ride_columns:
        cursor.execute("ALTER TABLE rides ADD COLUMN start_pin TEXT")
    if "pin_attempts" not in existing_ride_columns:
        cursor.execute("ALTER TABLE rides ADD COLUMN pin_attempts INTEGER NOT NULL DEFAULT 0")

    # Ride ratings & reviews
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS ride_reviews (
        id TEXT PRIMARY KEY,
        ride_id TEXT NOT NULL,
        reviewer_id TEXT NOT NULL,
        reviewee_id TEXT NOT NULL,
        rating INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5),
        tags TEXT,
        comment TEXT,
        created_at REAL NOT NULL,
        FOREIGN KEY(ride_id) REFERENCES rides(id),
        FOREIGN KEY(reviewer_id) REFERENCES users(id),
        FOREIGN KEY(reviewee_id) REFERENCES users(id)
    );
    """)

    # CityLink scheduled carpooling routes with Route Planning & Pinning
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS driver_routes (
        id TEXT PRIMARY KEY,
        driver_id TEXT NOT NULL,
        origin TEXT NOT NULL DEFAULT 'LPU Uni-Mall',
        origin_lat REAL NOT NULL DEFAULT 31.2536,
        origin_lng REAL NOT NULL DEFAULT 75.7038,
        destination TEXT NOT NULL,
        destination_lat REAL NOT NULL,
        destination_lng REAL NOT NULL,
        departure_time TEXT NOT NULL,
        available_seats INTEGER NOT NULL,
        total_seats INTEGER NOT NULL,
        price_per_seat REAL NOT NULL,
        notes TEXT,
        status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open', 'pinned', 'in_progress', 'completed', 'cancelled')),
        share_token TEXT,
        current_lat REAL,
        current_lng REAL,
        created_at REAL NOT NULL,
        started_at REAL,
        completed_at REAL,
        FOREIGN KEY(driver_id) REFERENCES users(id)
    );
    """)

    # Upgrade databases created before scheduled route telemetry was added.
    route_columns = {
        "origin": "TEXT NOT NULL DEFAULT 'LPU Uni-Mall'",
        "origin_lat": "REAL NOT NULL DEFAULT 31.2536",
        "origin_lng": "REAL NOT NULL DEFAULT 75.7038",
        "share_token": "TEXT",
        "current_lat": "REAL",
        "current_lng": "REAL",
        "started_at": "REAL",
        "completed_at": "REAL"
    }
    existing_route_columns = {
        row[1] for row in cursor.execute("PRAGMA table_info(driver_routes)").fetchall()
    }
    for column, definition in route_columns.items():
        if column not in existing_route_columns:
            cursor.execute(f"ALTER TABLE driver_routes ADD COLUMN {column} {definition}")

    # Route Bookings (riders joining driver routes)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS route_bookings (
        id TEXT PRIMARY KEY,
        route_id TEXT NOT NULL,
        rider_id TEXT NOT NULL,
        seats INTEGER NOT NULL DEFAULT 1,
        fare_paid REAL NOT NULL,
        status TEXT NOT NULL DEFAULT 'confirmed' CHECK(status IN ('confirmed', 'in_progress', 'completed', 'cancelled')),
        created_at REAL NOT NULL,
        completed_at REAL,
        FOREIGN KEY(route_id) REFERENCES driver_routes(id),
        FOREIGN KEY(rider_id) REFERENCES users(id)
    );
    """)

    # SOS Emergency alerts
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS sos_alerts (
        id TEXT PRIMARY KEY,
        ride_id TEXT,
        user_id TEXT NOT NULL,
        lat REAL NOT NULL,
        lng REAL NOT NULL,
        location_name TEXT,
        status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'acknowledged', 'resolved')),
        notified_contacts_count INTEGER NOT NULL DEFAULT 0,
        notes TEXT,
        created_at REAL NOT NULL,
        resolved_at REAL,
        FOREIGN KEY(user_id) REFERENCES users(id)
    );
    """)

    # Driver Live Telemetry & Availability
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS driver_locations (
        driver_id TEXT PRIMARY KEY,
        is_online INTEGER NOT NULL DEFAULT 1,
        lat REAL NOT NULL,
        lng REAL NOT NULL,
        zone TEXT NOT NULL,
        heading REAL NOT NULL DEFAULT 0.0,
        updated_at REAL NOT NULL,
        FOREIGN KEY(driver_id) REFERENCES users(id)
    );
    """)

    # Payment orders: every top-up must match exactly one server-created order,
    # and each order / provider payment id can only ever be credited once.
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS payment_orders (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        provider TEXT NOT NULL CHECK(provider IN ('upi', 'razorpay')),
        provider_order_id TEXT UNIQUE,
        provider_payment_id TEXT UNIQUE,
        amount REAL NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('created', 'awaiting_verification', 'paid', 'rejected')),
        created_at REAL NOT NULL,
        paid_at REAL,
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    );
    """)

    # Indexes for peak-time scaling & zone lookups
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_rides_status_zone ON rides(status, pickup_zone);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_rides_rider ON rides(rider_id);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_rides_driver ON rides(driver_id);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_driver_locations_zone ON driver_locations(zone, is_online);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_wallet_tx_user ON wallet_transactions(user_id);")
    # One review per person per ride (drop older duplicates from before this rule existed).
    cursor.execute("""
    DELETE FROM ride_reviews WHERE rowid NOT IN (
        SELECT MIN(rowid) FROM ride_reviews GROUP BY ride_id, reviewer_id
    );
    """)
    cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_reviews_once ON ride_reviews(ride_id, reviewer_id);")

    conn.commit()
    conn.close()

if __name__ == "__main__":
    init_db()
    print("Database initialized successfully at:", DB_PATH)
