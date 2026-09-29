"""Shared test setup. Imported first by every test module so the tests run
against a throwaway database and never touch backend/campusgo.db."""
import os
import tempfile

_TMP_DIR = tempfile.mkdtemp(prefix="campusgo_test_")
os.environ["DATABASE_PATH"] = os.path.join(_TMP_DIR, "test.db")
os.environ["PAYMENTS_DEMO_MODE"] = "1"
for _var in ("RAZORPAY_KEY_ID", "RAZORPAY_KEY_SECRET", "FAST2SMS_API_KEY", "TWILIO_ACCOUNT_SID",
             "SECRET_KEY", "ADMIN_API_KEY", "DEMO_PASSWORD"):
    os.environ.pop(_var, None)

from app import app, RATE_LIMIT_STORE  # noqa: E402
from database import get_db_connection  # noqa: E402
from seed_data import seed, DEMO_PASSWORD  # noqa: E402

LPU_IDS = {
    "usr_teacher_raman": "FAC-10822",
    "usr_student_aarav": "12204592",
    "usr_student_kavya": "12301982",
    "usr_driver_simran": "12108843",
    "usr_driver_vikram": "12019934",
    "usr_driver_harpreet": "11904421",
}


class ClientMixin:
    @classmethod
    def setUpClass(cls):
        seed()
        cls.client = app.test_client()

    def setUp(self):
        RATE_LIMIT_STORE.clear()

    def auth_headers(self, user_id):
        RATE_LIMIT_STORE.clear()
        res = self.client.post("/api/auth/login", json={"lpu_id": LPU_IDS[user_id], "password": DEMO_PASSWORD})
        return {"Authorization": f"Bearer {res.get_json()['token']}"}

    def balance(self, headers):
        return self.client.get("/api/wallet", headers=headers).get_json()["balance"]

    def db(self, sql, params=()):
        conn = get_db_connection()
        try:
            rows = conn.execute(sql, params).fetchall()
            conn.commit()
            return rows
        finally:
            conn.close()

    def topup_demo(self, headers, amount):
        ref = self.client.post("/api/payments/upi/create-qr", headers=headers,
                               json={"amount": amount}).get_json()["reference_id"]
        return self.client.post("/api/payments/upi/confirm", headers=headers, json={"reference_id": ref})
