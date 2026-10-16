"""Database access for SQLite (local development, the default) and PostgreSQL (production).

Set DATABASE_URL=postgresql://... to use PostgreSQL. The app's SQL is written once, with `?`
placeholders; on PostgreSQL each statement is translated (`?` -> `%s`, `:name` -> `%(name)s`)
and rows come back in the same shape as sqlite3.Row (by column name or by position).

SQLite keeps everything in one file on one machine, so it suits development and small
deployments only. PostgreSQL lets several app processes and servers share one database.
"""
import atexit
import os
import re
import sqlite3
import threading

DB_PATH = os.environ.get("DATABASE_PATH") or os.path.join(os.path.dirname(__file__), "campusgo.db")
DATABASE_URL = (os.environ.get("DATABASE_URL") or "").strip()
IS_POSTGRES = DATABASE_URL.startswith(("postgres://", "postgresql://"))
print("DATABASE BACKEND:", "PostgreSQL" if IS_POSTGRES else "SQLite")

# Write transactions are serialised with this advisory lock on PostgreSQL, matching SQLite's
# single-writer behaviour that the money and seat logic relies on.
WRITE_LOCK_ID = 7_201_501
MIGRATION_LOCK_ID = 7_201_502

if IS_POSTGRES:
    import psycopg
    from psycopg_pool import ConnectionPool

    IntegrityError = (sqlite3.IntegrityError, psycopg.errors.IntegrityError)
else:
    IntegrityError = sqlite3.IntegrityError


# --- PostgreSQL -------------------------------------------------------------------------
class Row(dict):
    """A dict row that also answers row[0], like sqlite3.Row."""
    __slots__ = ()

    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return dict.__getitem__(self, key)


def _row_factory(cursor):
    names = [c.name for c in cursor.description or ()]
    return lambda values: Row(zip(names, values))


_NAMED = re.compile(r"(?<!:):([A-Za-z_]\w*)")
_sql_cache = {}


def _translate(sql, named):
    key = (sql, named)
    out = _sql_cache.get(key)
    if out is None:
        out = sql.replace("%", "%%")
        out = _NAMED.sub(r"%(\1)s", out) if named else out.replace("?", "%s")
        if len(_sql_cache) < 2000:
            _sql_cache[key] = out
    return out


class PgConnection:
    """Wraps a pooled psycopg connection with the small sqlite3 API the app uses.

    Connections run in autocommit mode, so a read-only request costs no BEGIN/ROLLBACK round
    trips. begin_write() opens a real transaction (with the write lock) for everything that
    must be all-or-nothing; commit()/rollback() end it."""

    def __init__(self, pool):
        self._pool = pool
        self._conn = pool.getconn()
        self._in_txn = False

    def begin(self):
        if not self._in_txn:
            self._conn.execute("BEGIN")
            self._in_txn = True

    def execute(self, sql, params=()):
        named = isinstance(params, dict)
        cur = self._conn.cursor(row_factory=_row_factory)
        # Always pass params (even empty) so psycopg turns the escaped %% back into %.
        cur.execute(_translate(sql, named), params if named else tuple(params or ()))
        return cur

    def cursor(self):
        return self

    def commit(self):
        if self._in_txn:
            self._in_txn = False
            self._conn.execute("COMMIT")

    def rollback(self):
        if self._in_txn:
            self._in_txn = False
            self._conn.execute("ROLLBACK")

    def close(self):
        if self._conn is not None:
            try:
                self.rollback()
            finally:
                self._pool.putconn(self._conn)
                self._conn = None


_pool = None
_pool_pid = None
_pool_lock = threading.Lock()


def _get_pool():
    """One pool per process (created after gunicorn forks its workers)."""
    global _pool, _pool_pid
    if _pool is None or _pool_pid != os.getpid():
        with _pool_lock:
            if _pool is None or _pool_pid != os.getpid():
                size = int(os.environ.get("DB_POOL_SIZE", "10") or 10)
                _pool = ConnectionPool(DATABASE_URL, min_size=1, max_size=size, timeout=10,
                                       kwargs={"autocommit": True}, open=True)
                _pool_pid = os.getpid()
    return _pool


@atexit.register
def _close_pool():
    if _pool is not None and _pool_pid == os.getpid():
        _pool.close()


# --- Public API -------------------------------------------------------------------------
class SqliteConnection:
    """One SQLite connection per thread, reused across requests: opening a connection and
    setting it up costs more than a typical query. close() only ends the transaction."""

    def __init__(self, raw):
        self._conn = raw

    def execute(self, sql, params=()):
        return self._conn.execute(sql, params)

    def cursor(self):
        return self._conn.cursor()

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.rollback()


_local = threading.local()


def _sqlite_connection():
    cached = getattr(_local, "sqlite", None)
    if cached is not None and cached[0] == DB_PATH:
        return cached[1]
    # timeout: wait for a competing writer instead of failing instantly.
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    # WAL + NORMAL is crash-safe (no corruption) and avoids an fsync on every commit.
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute("PRAGMA cache_size = -16000;")  # 16 MB page cache
    conn.execute("PRAGMA temp_store = MEMORY;")
    _local.sqlite = (DB_PATH, conn)
    return conn


def get_db_connection():
    if IS_POSTGRES:
        return PgConnection(_get_pool())
    return SqliteConnection(_sqlite_connection())


def begin_write(conn):
    """Takes the write lock up front so read-check-write sequences on balances and seats
    can't interleave with another request. Released by commit or rollback."""
    if IS_POSTGRES:
        conn.begin()
        conn.execute("SELECT pg_advisory_xact_lock(?)", (WRITE_LOCK_ID,))
    else:
        conn.execute("BEGIN IMMEDIATE")


def _ddl(sql):
    # SQLite's REAL is 8 bytes; PostgreSQL's is 4, which can't hold a timestamp to the second.
    return re.sub(r"\bREAL\b", "DOUBLE PRECISION", sql) if IS_POSTGRES else sql


SCHEMA = [
    # token_version is bumped on logout, so every earlier token stops working. A deleted account
    # keeps its row (rides and payments reference it) with every personal field replaced.
    """
    CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY,
        lpu_id TEXT UNIQUE NOT NULL,
        username TEXT NOT NULL,
        name TEXT NOT NULL,
        phone TEXT NOT NULL,
        phone_key TEXT NOT NULL,
        user_type TEXT NOT NULL CHECK(user_type IN ('student', 'teacher')),
        role TEXT NOT NULL CHECK(role IN ('rider', 'driver', 'both')),
        is_teacher_priority INTEGER NOT NULL DEFAULT 0,
        token_version INTEGER NOT NULL DEFAULT 0,
        created_at REAL NOT NULL,
        deleted_at REAL
    )""",
    # One-time passwords sent by SMS. Only a keyed hash of the code is stored.
    """
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
    )""",
    """
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
    )""",
    # Server-side balance authority
    """
    CREATE TABLE IF NOT EXISTS wallets (
        user_id TEXT PRIMARY KEY,
        balance REAL NOT NULL DEFAULT 0.0,
        updated_at REAL NOT NULL,
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    )""",
    """
    CREATE TABLE IF NOT EXISTS wallet_transactions (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        amount REAL NOT NULL,
        type TEXT NOT NULL CHECK(type IN ('topup', 'fare_deduction', 'driver_payout', 'refund', 'withdrawal')),
        reference_id TEXT,
        description TEXT NOT NULL,
        balance_after REAL NOT NULL,
        created_at REAL NOT NULL,
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    )""",
    """
    CREATE TABLE IF NOT EXISTS emergency_contacts (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        name TEXT NOT NULL,
        relationship TEXT NOT NULL,
        phone TEXT NOT NULL,
        is_primary INTEGER NOT NULL DEFAULT 1,
        created_at REAL NOT NULL,
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    )""",
    """
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
        start_pin TEXT NOT NULL,
        pin_attempts INTEGER NOT NULL DEFAULT 0,
        created_at REAL NOT NULL,
        matched_at REAL,
        started_at REAL,
        completed_at REAL,
        cancelled_at REAL,
        cancellation_reason TEXT,
        FOREIGN KEY(rider_id) REFERENCES users(id),
        FOREIGN KEY(driver_id) REFERENCES users(id)
    )""",
    """
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
    )""",
    # CityLink scheduled carpools
    """
    CREATE TABLE IF NOT EXISTS driver_routes (
        id TEXT PRIMARY KEY,
        driver_id TEXT NOT NULL,
        origin TEXT NOT NULL,
        origin_lat REAL NOT NULL,
        origin_lng REAL NOT NULL,
        destination TEXT NOT NULL,
        destination_lat REAL NOT NULL,
        destination_lng REAL NOT NULL,
        departure_time TEXT NOT NULL,
        available_seats INTEGER NOT NULL,
        total_seats INTEGER NOT NULL,
        price_per_seat REAL NOT NULL,
        notes TEXT,
        status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open', 'pinned', 'in_progress', 'completed', 'cancelled')),
        current_lat REAL,
        current_lng REAL,
        created_at REAL NOT NULL,
        started_at REAL,
        completed_at REAL,
        FOREIGN KEY(driver_id) REFERENCES users(id)
    )""",
    """
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
    )""",
    """
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
    )""",
    # Driver live position and availability
    """
    CREATE TABLE IF NOT EXISTS driver_locations (
        driver_id TEXT PRIMARY KEY,
        is_online INTEGER NOT NULL DEFAULT 1,
        lat REAL NOT NULL,
        lng REAL NOT NULL,
        updated_at REAL NOT NULL,
        FOREIGN KEY(driver_id) REFERENCES users(id)
    )""",
    # Every top-up must match exactly one server-created order, and each order /
    # provider payment id can only ever be credited once.
    """
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
    )""",
    # The platform's cut of every completed trip: fare paid minus what the driver was credited.
    """
    CREATE TABLE IF NOT EXISTS platform_earnings (
        id TEXT PRIMARY KEY,
        source TEXT NOT NULL CHECK(source IN ('ride', 'carpool')),
        reference_id TEXT NOT NULL UNIQUE,
        scope TEXT,
        fare REAL NOT NULL,
        driver_payout REAL NOT NULL,
        amount REAL NOT NULL,
        created_at REAL NOT NULL
    )""",
    # Drivers cash out wallet earnings here; the admin sends the money by UPI and marks it paid.
    """
    CREATE TABLE IF NOT EXISTS driver_payouts (
        id TEXT PRIMARY KEY,
        driver_id TEXT NOT NULL,
        amount REAL NOT NULL,
        upi_id TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('requested', 'paid', 'rejected')),
        utr TEXT,
        note TEXT,
        created_at REAL NOT NULL,
        settled_at REAL,
        FOREIGN KEY(driver_id) REFERENCES users(id)
    )""",
    # Phones that turned notifications on (Web Push). One row per browser install.
    """
    CREATE TABLE IF NOT EXISTS push_subscriptions (
        endpoint TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        p256dh TEXT NOT NULL,
        auth TEXT NOT NULL,
        created_at REAL NOT NULL,
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    )""",
    # Server-wide values made once and kept (e.g. the push signing key).
    """
    CREATE TABLE IF NOT EXISTS app_settings (
        name TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )""",
    # In-app voice calls between the two sides of a ride or carpool. The server only passes the
    # call setup (WebRTC offer/answer) between the phones: no numbers are shared, no audio stored.
    """
    CREATE TABLE IF NOT EXISTS calls (
        id TEXT PRIMARY KEY,
        caller_id TEXT NOT NULL,
        callee_id TEXT NOT NULL,
        context_type TEXT NOT NULL CHECK(context_type IN ('ride', 'route')),
        context_id TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('ringing', 'connected', 'ended', 'declined', 'missed')),
        offer TEXT NOT NULL,
        answer TEXT,
        created_at REAL NOT NULL,
        answered_at REAL,
        ended_at REAL,
        FOREIGN KEY(caller_id) REFERENCES users(id) ON DELETE CASCADE,
        FOREIGN KEY(callee_id) REFERENCES users(id) ON DELETE CASCADE
    )""",
]

# Every hot query has an index: these are the lookups the app runs on each poll.
INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_otp_phone ON otp_challenges(phone_key, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_rides_status_zone ON rides(status, pickup_zone)",
    "CREATE INDEX IF NOT EXISTS idx_rides_queue ON rides(status, service_type, is_priority, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_rides_rider ON rides(rider_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_rides_driver ON rides(driver_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_driver_locations_online ON driver_locations(is_online, lat, lng)",
    "CREATE INDEX IF NOT EXISTS idx_wallet_tx_user ON wallet_transactions(user_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_wallet_tx_ref ON wallet_transactions(reference_id, user_id, type)",
    "CREATE INDEX IF NOT EXISTS idx_vehicles_user ON vehicles(user_id, is_active)",
    "CREATE INDEX IF NOT EXISTS idx_contacts_user ON emergency_contacts(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_routes_status ON driver_routes(status, driver_id)",
    "CREATE INDEX IF NOT EXISTS idx_bookings_route ON route_bookings(route_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_bookings_rider ON route_bookings(rider_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_payment_orders_user ON payment_orders(user_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_sos_status ON sos_alerts(status, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_reviews_reviewee ON ride_reviews(reviewee_id)",
    "CREATE INDEX IF NOT EXISTS idx_platform_earnings_time ON platform_earnings(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_driver_payouts_status ON driver_payouts(status, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_driver_payouts_driver ON driver_payouts(driver_id, created_at)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username ON users(lower(username))",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_phone_key ON users(phone_key)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_reviews_once ON ride_reviews(ride_id, reviewer_id)",
    "CREATE INDEX IF NOT EXISTS idx_push_user ON push_subscriptions(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_calls_callee ON calls(callee_id, status)",
]


# Columns added after launch. CREATE TABLE IF NOT EXISTS leaves an existing table as it was,
# so these are added to it here (a fresh database gets them the same way).
ADDED_COLUMNS = [
    # How far off the driver's reported position may be (phones on "approximate location" are ~2 km).
    ("driver_locations", "accuracy_km", "REAL NOT NULL DEFAULT 0"),
]


def _add_columns(cursor):
    for table, column, kind in ADDED_COLUMNS:
        if IS_POSTGRES:
            cursor.execute(_ddl(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {kind}"))
        elif column not in [row["name"] for row in cursor.execute(f"PRAGMA table_info({table})").fetchall()]:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")


def init_db():
    """Creates the schema if it doesn't exist yet. Safe to run from many processes at once."""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        if IS_POSTGRES:
            # Workers starting together take turns; the first one does the work.
            cursor.execute("SELECT pg_advisory_lock(?)", (MIGRATION_LOCK_ID,))
        else:
            cursor.execute("PRAGMA journal_mode=WAL;")  # readers proceed while a write is in progress
        for statement in SCHEMA:
            cursor.execute(_ddl(statement))
        _add_columns(cursor)
        for statement in INDEXES:
            cursor.execute(statement)
        conn.commit()
    finally:
        if IS_POSTGRES:
            conn.rollback()
            cursor.execute("SELECT pg_advisory_unlock(?)", (MIGRATION_LOCK_ID,))
            conn.commit()
        conn.close()
