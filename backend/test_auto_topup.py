"""Top-ups paid through Razorpay are credited without anyone approving them: by the signed
webhook, or by asking Razorpay about the user's unpaid orders."""
import hashlib
import hmac
import json
import os
import unittest
from unittest import mock

from test_support import ClientMixin
import payments

WEBHOOK_SECRET = "whsec_test_value"
LIVE_ENV = {"RAZORPAY_KEY_ID": "rzp_test_x", "RAZORPAY_KEY_SECRET": "real_secret_value",
            "RAZORPAY_WEBHOOK_SECRET": WEBHOOK_SECRET}


class TestAutoTopup(ClientMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, LIVE_ENV)
        patcher.start()
        self.addCleanup(patcher.stop)

    def create_order(self, headers, amount):
        remote_id = f"order_{self.id().rsplit('.', 1)[-1]}_{amount}"
        with mock.patch.object(payments, "_razorpay_api", return_value={"id": remote_id}) as api:
            res = self.client.post("/api/payments/razorpay/create-order", headers=headers, json={"amount": amount})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(api.call_args[0][1]["payment_capture"], 1)
        return remote_id

    def webhook(self, order_id, payment_id, paise, event="payment.captured", status="captured", secret=WEBHOOK_SECRET):
        raw = json.dumps({"event": event, "payload": {"payment": {"entity": {
            "id": payment_id, "order_id": order_id, "amount": paise, "status": status}}}}).encode("utf-8")
        signature = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
        return self.client.post("/api/payments/razorpay/webhook", data=raw,
                                headers={"Content-Type": "application/json", "X-Razorpay-Signature": signature})

    def test_webhook_credits_order_amount_once(self):
        headers = self.auth_headers("usr_student_aarav")
        before = self.balance(headers)
        order_id = self.create_order(headers, 120)
        self.assertEqual(self.webhook(order_id, "pay_wh_1", 12000).status_code, 200)
        self.assertEqual(self.balance(headers), before + 120)
        # Razorpay retries deliveries and also sends order.paid for the same payment.
        self.assertEqual(self.webhook(order_id, "pay_wh_1", 12000, event="order.paid").status_code, 200)
        self.assertEqual(self.webhook(order_id, "pay_wh_other", 12000).status_code, 200)
        self.assertEqual(self.balance(headers), before + 120)

    def test_webhook_refuses_forged_or_mismatched_payments(self):
        headers = self.auth_headers("usr_student_aarav")
        before = self.balance(headers)
        order_id = self.create_order(headers, 130)
        self.assertEqual(self.webhook(order_id, "pay_forged", 13000, secret="guess").status_code, 400)
        self.assertEqual(self.webhook(order_id, "pay_short", 100).status_code, 200)  # paid less than the order
        self.assertEqual(self.webhook(order_id, "pay_failed", 13000, status="failed").status_code, 200)
        self.assertEqual(self.webhook("order_unknown", "pay_x", 13000).status_code, 200)
        self.assertEqual(self.balance(headers), before)

    def test_webhook_is_off_without_a_secret(self):
        with mock.patch.dict(os.environ, {"RAZORPAY_WEBHOOK_SECRET": ""}):
            self.assertEqual(self.webhook("order_any", "pay_any", 100, secret="").status_code, 404)

    def test_sync_credits_paid_orders_only(self):
        headers = self.auth_headers("usr_student_kavya")
        before = self.balance(headers)
        paid = self.create_order(headers, 140)
        self.create_order(headers, 150)  # never paid

        def razorpay(path, payload=None):
            if path == f"orders/{paid}/payments":
                return {"items": [{"id": "pay_failed_try", "status": "failed", "amount": 14000},
                                  {"id": "pay_sync_1", "status": "captured", "amount": 14000}]}
            return {"items": []}

        with mock.patch.object(payments, "_razorpay_api", side_effect=razorpay):
            first = self.client.post("/api/payments/razorpay/sync", headers=headers).get_json()
            second = self.client.post("/api/payments/razorpay/sync", headers=headers).get_json()
        self.assertEqual(first["amount_credited"], 140)
        self.assertEqual(first["new_balance"], before + 140)
        self.assertEqual(second["amount_credited"], 0)
        self.assertEqual(self.balance(headers), before + 140)

    def test_sync_survives_gateway_outage(self):
        headers = self.auth_headers("usr_student_kavya")
        before = self.balance(headers)
        self.create_order(headers, 160)
        with mock.patch.object(payments, "_razorpay_api", side_effect=payments.PaymentError("down")):
            res = self.client.post("/api/payments/razorpay/sync", headers=headers)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["new_balance"], before)


if __name__ == "__main__":
    unittest.main()
