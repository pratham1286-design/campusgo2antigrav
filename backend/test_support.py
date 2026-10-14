"""Shared test setup. Imported first by every test module so the tests run
against a throwaway database and never touch backend/campusgo.db."""
import os
import tempfile
from datetime import datetime, timedelta, timezone

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
os.environ["PAYMENTS_DEMO_MODE"] = "1"
os.environ["CAMPUSGO_NO_DOTENV"] = "1"  # tests never read the developer's .env
os.environ["OTP_DEMO_MODE"] = "1"
os.environ["DEMO_SIMULATE_MOVEMENT"] = "1"
for _var in ("RAZORPAY_KEY_ID", "RAZORPAY_KEY_SECRET", "RAZORPAY_WEBHOOK_SECRET", "FAST2SMS_API_KEY", "TWILIO_ACCOUNT_SID",
             "SECRET_KEY", "ADMIN_API_KEY"):
    os.environ.pop(_var, None)

import shared  # noqa: E402
if os.environ.get("TEST_REDIS") == "fake":
    import fakeredis
    shared.use_redis_client(fakeredis.FakeRedis(decode_responses=True))
from app import app, RATE_LIMIT_STORE  # noqa: E402
import app as _app_module  # noqa: E402
from database import get_db_connection  # noqa: E402
from seed_data import seed  # noqa: E402

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


def departure_in(hours=3):
    """A campus-local (IST) departure time in the format the carpool form sends."""
    return (datetime.now(timezone.utc) + timedelta(minutes=330, hours=hours)).strftime("%Y-%m-%dT%H:%M")


class ClientMixin:
    @classmethod
    def setUpClass(cls):
        seed()
        cls.client = app.test_client()

    def setUp(self):
        shared.reset_memory()
        _app_module._nearby_cache.clear()
        _app_module._token_versions.clear()
        self.db("DELETE FROM otp_challenges")

    def auth_headers(self, user_id):
        RATE_LIMIT_STORE.clear()
        self.db("DELETE FROM otp_challenges")
        start = self.client.post("/api/auth/login/start", json={"lpu_id": LPU_IDS[user_id], "phone": PHONES[user_id]}).get_json()
        res = self.client.post("/api/auth/login/verify", json={"challenge_id": start["challenge_id"], "otp": start["demo_otp"]})
        RATE_LIMIT_STORE.clear()
        return {"Authorization": f"Bearer {res.get_json()['token']}"}

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

    def topup_demo(self, headers, amount):
        ref = self.client.post("/api/payments/upi/create-qr", headers=headers,
                               json={"amount": amount}).get_json()["reference_id"]
        return self.client.post("/api/payments/upi/confirm", headers=headers, json={"reference_id": ref})
