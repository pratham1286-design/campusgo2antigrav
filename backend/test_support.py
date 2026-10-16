"""Shared test setup. Imported first by every test module so the tests run against a throwaway
database and never touch backend/campusgo.db, never send a real SMS and never call Razorpay."""
import hashlib
import hmac
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from unittest import mock

# Imported after them, the tests would run against backend/campusgo.db and the developer's .env.
if "app" in sys.modules or "database" in sys.modules:
    raise RuntimeError("Import test_support before app or database.")

_TMP_DIR = tempfile.mkdtemp(prefix="campusgo_test_")
os.environ["DATABASE_PATH"] = os.path.join(_TMP_DIR, "test.db")
# TEST_DATABASE_URL=postgresql://... runs the whole suite on PostgreSQL (its tables are wiped);
# TEST_REDIS=fake runs rate limits and caches through a Redis client (fakeredis) instead of memory.
if os.environ.get("TEST_DATABASE_URL"):
    import psycopg
    os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]
    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as _pg:
        _pg.execute("DROP SCHEMA public CASCADE")
        _pg.execute("CREATE SCHEMA public")
else:
    os.environ.pop("DATABASE_URL", None)
os.environ.pop("REDIS_URL", None)
os.environ["CAMPUSGO_NO_DOTENV"] = "1"  # tests never read the developer's .env
os.environ["CAMPUSGO_UPI_VPA"] = "campusgo-test@okicici"
for _var in ("RAZORPAY_KEY_ID", "RAZORPAY_KEY_SECRET", "RAZORPAY_WEBHOOK_SECRET", "FAST2SMS_API_KEY", "TWILIO_ACCOUNT_SID",
             "SECRET_KEY", "ADMIN_API_KEY", "REVIEW_LOGIN_PHONE", "REVIEW_LOGIN_CODE"):
    os.environ.pop(_var, None)

import shared  # noqa: E402
if os.environ.get("TEST_REDIS") == "fake":
    import fakeredis
    shared.use_redis_client(fakeredis.FakeRedis(decode_responses=True))
import emergency_dispatch  # noqa: E402
from app import app  # noqa: E402
import app as _app_module  # noqa: E402
from database import get_db_connection  # noqa: E402

# Every text goes to this outbox through a fake Fast2SMS gateway, so sign-in codes and SOS
# alerts run their real code paths.
SMS_OUTBOX = []


def _fake_fast2sms(phone, message):
    SMS_OUTBOX.append((phone, message))
    return {"status": "sent", "gateway": "Fast2SMS"}


emergency_dispatch.FAST2SMS_API_KEY = "test-gateway"
mock.patch.object(emergency_dispatch, "send_fast2sms", _fake_fast2sms).start()

# Push notifications are encrypted and signed for real, then land in this outbox instead of
# Google's push service: (endpoint, encrypted body, headers).
import push  # noqa: E402
PUSH_OUTBOX = []
push.INLINE = True
mock.patch.object(push, "_post", lambda endpoint, body, headers: PUSH_OUTBOX.append((endpoint, body, headers)) or 201).start()

ADMIN_KEY = "test-admin-key-" + "x" * 32
# Razorpay test-mode credentials; tests that use them mock payments._razorpay_api.
RAZORPAY_ENV = {"RAZORPAY_KEY_ID": "rzp_test_x", "RAZORPAY_KEY_SECRET": "real_secret_value"}


def razorpay_signature(order_id, payment_id):
    """What Razorpay Checkout hands the browser for a real payment."""
    return hmac.new(RAZORPAY_ENV["RAZORPAY_KEY_SECRET"].encode(), f"{order_id}|{payment_id}".encode(),
                    hashlib.sha256).hexdigest()


LPU_IDS = {
    "usr_teacher_raman": "FAC-10822",
    "usr_student_aarav": "12204592",
    "usr_student_kavya": "12301982",
    "usr_driver_simran": "12108843",
    "usr_driver_vikram": "12019934",
    "usr_driver_harpreet": "11904421",
}
PHONES = {
    "usr_teacher_raman": "9876543210",
    "usr_student_aarav": "9812345678",
    "usr_student_kavya": "9823456789",
    "usr_driver_simran": "9834567890",
    "usr_driver_vikram": "9845678901",
    "usr_driver_harpreet": "9856789012",
}
# id: (name, user_type, role, teacher priority, wallet balance)
USERS = {
    "usr_teacher_raman": ("Dr. Raman Sharma", "teacher", "rider", 1, 350.0),
    "usr_student_aarav": ("Aarav Mehta", "student", "rider", 0, 150.0),
    "usr_student_kavya": ("Kavya Patel", "student", "rider", 0, 10.0),
    "usr_driver_simran": ("Simran Kaur", "student", "both", 0, 480.0),
    "usr_driver_vikram": ("Vikram Singh", "student", "driver", 0, 620.0),
    "usr_driver_harpreet": ("Harpreet Singh", "student", "driver", 0, 920.0),
}
# driver id: (vehicle, seats, helmet, AC, where they are online)
VEHICLES = {
    "usr_driver_simran": ("scooty", "Honda Activa 6G", "PB08-DA-4821", 1, 1, 0, (31.2535, 75.7038)),
    "usr_driver_vikram": ("bike", "Royal Enfield Hunter 350", "PB09-AJ-7731", 1, 1, 0, (31.2562, 75.7050)),
    "usr_driver_harpreet": ("car", "Maruti Suzuki Dzire VXi", "PB08-CR-9912", 3, 0, 1, (31.2510, 75.7080)),
}
TABLES = ("driver_payouts", "platform_earnings", "sos_alerts", "route_bookings", "driver_routes", "ride_reviews",
          "rides", "driver_locations", "emergency_contacts", "wallet_transactions", "wallets", "vehicles",
          "payment_orders", "otp_challenges", "calls", "push_subscriptions", "users")


def load_fixtures():
    """Empties every table and loads the test riders and drivers."""
    conn = get_db_connection()
    try:
        for table in TABLES:
            conn.execute(f"DELETE FROM {table}")
        now = time.time()
        for uid, (name, user_type, role, priority, balance) in USERS.items():
            conn.execute("""
            INSERT INTO users (id, lpu_id, username, name, phone, phone_key, user_type, role, is_teacher_priority, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (uid, LPU_IDS[uid], name.lower().replace("dr. ", "").replace(" ", "."), name, f"+91{PHONES[uid]}",
                  PHONES[uid], user_type, role, priority, now))
            conn.execute("INSERT INTO wallets (user_id, balance, updated_at) VALUES (?, ?, ?)", (uid, balance, now))
            for i, (contact, phone) in enumerate((("Hostel Warden", "+91 95555 00001"), ("Parent", "+91 95555 00002"))):
                conn.execute("""
                INSERT INTO emergency_contacts (id, user_id, name, relationship, phone, is_primary, created_at)
                VALUES (?, ?, ?, 'Family', ?, ?, ?)
                """, (f"emg_{uid}_{i}", uid, contact, phone, 1 if i == 0 else 0, now + i))
        for uid, (category, model, plate, seats, helmet, ac, (lat, lng)) in VEHICLES.items():
            conn.execute("""
            INSERT INTO vehicles (id, user_id, category, model, plate_number, color, capacity, has_helmet, has_ac, is_active)
            VALUES (?, ?, ?, ?, ?, 'White', ?, ?, ?, 1)
            """, (f"veh_{uid}", uid, category, model, plate, seats, helmet, ac))
            conn.execute("INSERT INTO driver_locations (driver_id, is_online, lat, lng, updated_at) VALUES (?, 1, ?, ?, ?)",
                         (uid, lat, lng, now))
        conn.commit()
    finally:
        conn.close()


def departure_in(hours=3):
    """A campus-local (IST) departure time in the format the carpool form sends."""
    return (datetime.now(timezone.utc) + timedelta(minutes=330, hours=hours)).strftime("%Y-%m-%dT%H:%M")


def last_otp(to=None):
    """The code in the most recent sign-in text (optionally: the most recent one sent to `to`)."""
    for phone, message in reversed(SMS_OUTBOX):
        found = re.match(r"(\d{6}) is your CampusGo verification code", message)
        if found and (to is None or phone.endswith(to[-10:])):
            return found.group(1)
    raise AssertionError(f"no sign-in code was texted{f' to {to}' if to else ''}")


class ClientMixin:
    @classmethod
    def setUpClass(cls):
        load_fixtures()
        cls.client = app.test_client()

    def setUp(self):
        shared.reset_memory()
        _app_module._nearby_cache.clear()
        _app_module._token_versions.clear()
        SMS_OUTBOX.clear()
        self.db("DELETE FROM otp_challenges")

    def auth_headers(self, user_id):
        shared.clear_rate_limits()
        self.db("DELETE FROM otp_challenges")
        start = self.client.post("/api/auth/login/start", json={"lpu_id": LPU_IDS[user_id], "phone": PHONES[user_id]}).get_json()
        res = self.client.post("/api/auth/login/verify",
                               json={"challenge_id": start["challenge_id"], "otp": last_otp(PHONES[user_id])})
        shared.clear_rate_limits()
        return {"Authorization": f"Bearer {res.get_json()['token']}"}

    def signup(self, phone, lpu_id, username, user_type="student", name="Test Person"):
        """Creates an account through the real sign-up flow; returns the verify reply."""
        start = self.client.post("/api/auth/signup/start", json={
            "name": name, "username": username, "user_type": user_type, "lpu_id": lpu_id, "phone": phone}).get_json()
        return self.client.post("/api/auth/signup/verify",
                                json={"challenge_id": start["challenge_id"], "otp": last_otp(phone)}).get_json()

    def balance(self, headers):
        return self.client.get("/api/wallet", headers=headers).get_json()["balance"]

    def db(self, sql, params=()):
        conn = get_db_connection()
        try:
            cur = conn.execute(sql, params)
            rows = cur.fetchall() if sql.lstrip().upper().startswith("SELECT") else []
            conn.commit()
            _app_module._nearby_cache.clear()  # direct edits must not be hidden by the few-second cache
            return rows
        finally:
            conn.close()

    def topup(self, headers, amount):
        """A UPI top-up as it happens for real: QR, "I've paid", then an admin approves it."""
        ref = self.client.post("/api/payments/upi/create-qr", headers=headers,
                               json={"amount": amount}).get_json()["reference_id"]
        self.client.post("/api/payments/upi/confirm", headers=headers, json={"reference_id": ref})
        with mock.patch.dict(os.environ, {"ADMIN_API_KEY": ADMIN_KEY}):
            return self.client.post(f"/api/admin/payments/{ref}/approve", headers={"X-Admin-Key": ADMIN_KEY})
