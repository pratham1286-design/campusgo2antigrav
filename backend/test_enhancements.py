import unittest

from test_support import ClientMixin
from emergency_dispatch import format_sos_message, normalize_phone
from payments import generate_upi_qr


class TestEnhancements(ClientMixin, unittest.TestCase):
    def test_01_upi_qr_generation(self):
        res = generate_upi_qr("pay_test", 150.0)
        self.assertIn("upi://pay?", res["upi_uri"])
        self.assertIn("am=150.00", res["upi_uri"])
        self.assertTrue(res["qr_data_url"].startswith("data:image/svg+xml"))

    def test_02_demo_razorpay_flow_credits_order_amount_once(self):
        headers = self.auth_headers("usr_student_aarav")
        before = self.balance(headers)
        order = self.client.post("/api/payments/razorpay/create-order", headers=headers, json={"amount": 250}).get_json()
        self.assertEqual(order["amount"], 25000)
        verify = {"order_id": order["order_id"], "payment_id": "pay_demo_1",
                  "signature": "demo_signature_valid", "amount": 5000}
        res = self.client.post("/api/payments/razorpay/verify", headers=headers, json=verify)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.balance(headers), before + 250)  # client "amount" is ignored
        replay = self.client.post("/api/payments/razorpay/verify", headers=headers, json=verify)
        self.assertEqual(replay.status_code, 400)
        self.assertEqual(self.balance(headers), before + 250)

    def test_03_sos_message_format(self):
        msg = format_sos_message("Dr. Raman Sharma", "FAC-10822", "Uni-Mall", 31.2535, 75.7038, "https://x/track/abc")
        self.assertIn("FAC-10822", msg)
        self.assertIn("https://x/track/abc", msg)

    def test_04_phone_validation(self):
        self.assertEqual(normalize_phone("+91 98765 43210"), "+919876543210")
        self.assertIsNone(normalize_phone("<script>"))
        self.assertIsNone(normalize_phone("123"))

    def test_05_new_landmarks_reach_chandigarh(self):
        landmarks = self.client.get("/api/campus/landmarks").get_json()["landmarks"]
        for key in ("ludhiana_bus_stand", "rajpura", "mohali", "chandigarh_isbt_43", "chandigarh_station"):
            self.assertIn(key, landmarks)


if __name__ == "__main__":
    unittest.main()
