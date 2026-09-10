import unittest
import json
import time
from app import app
from seed_data import seed
from payments import generate_upi_qr, create_razorpay_order, verify_razorpay_payment, credit_wallet_after_payment
from emergency_dispatch import format_sos_message, dispatch_emergency_alert

class TestEnhancements(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        seed()
        cls.client = app.test_client()

    def test_01_upi_qr_generation(self):
        """Test NPCI UPI URI and SVG QR code generation."""
        res = generate_upi_qr(user_id="usr_student_aarav", amount=150.0)
        self.assertTrue(res["success"])
        self.assertIn("upi://pay?", res["upi_uri"])
        self.assertIn("campusgo.lpu@okhdfcbank", res["upi_uri"])
        self.assertIn("<svg", res["svg_qr"])
        self.assertIn("data:image/svg+xml", res["qr_data_url"])

    def test_02_razorpay_order_and_signature_verification(self):
        """Test Razorpay order creation and HMAC SHA256 signature verification."""
        order = create_razorpay_order(user_id="usr_student_aarav", amount=250.0)
        self.assertTrue(order["success"])
        self.assertEqual(order["amount"], 25000) # in paise
        self.assertIn("order_", order["order_id"])

        # Test verification with demo signature
        is_valid = verify_razorpay_payment(order["order_id"], "pay_test123", "demo_signature_valid")
        self.assertTrue(is_valid)

    def test_03_payment_api_endpoints(self):
        """Test /api/payments/upi/create-qr and /api/payments/upi/confirm."""
        # 1. Create QR
        res_qr = self.client.post("/api/payments/upi/create-qr", json={
            "user_id": "usr_student_kavya",
            "amount": 75.0
        })
        self.assertEqual(res_qr.status_code, 200)
        qr_data = res_qr.get_json()
        self.assertIn("svg_qr", qr_data)
        ref_id = qr_data["reference_id"]

        # 2. Confirm UPI payment
        res_confirm = self.client.post("/api/payments/upi/confirm", json={
            "user_id": "usr_student_kavya",
            "amount": 75.0,
            "reference_id": ref_id
        })
        self.assertEqual(res_confirm.status_code, 200)
        conf_data = res_confirm.get_json()
        self.assertTrue(conf_data["success"])
        # Kavya had 10 + 75 = 85
        self.assertEqual(conf_data["new_balance"], 85.0)

    def test_04_emergency_sms_dispatch_formatting(self):
        """Test format_sos_message contains required LPU ID, coordinates, and Block 30 hotline."""
        msg = format_sos_message(
            user_name="Dr. Raman Sharma",
            lpu_id="FAC-10822",
            location_name="Uni-Mall Student Plaza",
            lat=31.2535,
            lng=75.7038,
            share_token="share_test_token"
        )
        self.assertIn("FAC-10822", msg)
        self.assertIn("Uni-Mall", msg)
        self.assertIn("+91 1824 517000", msg)
        self.assertIn("share_test_token", msg)

    def test_05_sos_trigger_with_multi_channel_dispatch(self):
        """Test /api/sos/trigger returns SMS message payload and delivery records."""
        res = self.client.post("/api/sos/trigger", json={
            "user_id": "usr_student_aarav",
            "lat": 31.2536,
            "lng": 75.7037,
            "location_name": "Block 34 (Computer Science)"
        })
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data["status"], "DISPATCHED")
        self.assertIn("sos_message", data)
        self.assertIn("Block 34", data["sos_message"])
        self.assertGreater(len(data["contacts_notified"]), 0)
        for contact in data["contacts_notified"]:
            self.assertIn("delivery", contact)
            self.assertIn(contact["delivery"]["status"], ("DELIVERED_SIMULATED", "sent"))

if __name__ == "__main__":
    unittest.main()
