"""Regression tests for the security review findings. Each test reproduces an
attack that used to work and checks that it is now refused."""
import os
import unittest
from unittest import mock

from test_support import ADMIN_KEY, RAZORPAY_ENV, ClientMixin, departure_in, razorpay_signature
import app as app_module
import payments

CAMPUS_TRIP = {"pickup_key": "uni_mall", "drop_key": "block_34", "service_type": "bike", "scope": "campus_hop"}


class TestMoneyHoles(ClientMixin, unittest.TestCase):
    def test_upi_confirm_does_not_credit_until_an_admin_approves(self):
        headers = self.auth_headers("usr_student_kavya")
        before = self.balance(headers)
        ref = self.client.post("/api/payments/upi/create-qr", headers=headers, json={"amount": 500}).get_json()["reference_id"]
        res = self.client.post("/api/payments/upi/confirm", headers=headers, json={"reference_id": ref, "amount": 99999})
        self.assertEqual(res.status_code, 202)
        self.assertTrue(res.get_json()["pending"])
        self.assertEqual(self.balance(headers), before)

    def test_upi_confirm_needs_own_order(self):
        kavya = self.auth_headers("usr_student_kavya")
        aarav = self.auth_headers("usr_student_aarav")
        ref = self.client.post("/api/payments/upi/create-qr", headers=kavya, json={"amount": 50}).get_json()["reference_id"]
        self.assertEqual(self.client.post("/api/payments/upi/confirm", headers=aarav, json={"reference_id": ref}).status_code, 404)
        self.assertEqual(self.client.post("/api/payments/upi/confirm", headers=kavya, json={"reference_id": "made_up"}).status_code, 404)

    def test_upi_is_off_without_a_upi_id(self):
        headers = self.auth_headers("usr_student_kavya")
        with mock.patch.dict(os.environ, {"CAMPUSGO_UPI_VPA": ""}):
            res = self.client.post("/api/payments/upi/create-qr", headers=headers, json={"amount": 50})
            self.assertFalse(self.client.get("/api/config").get_json()["upi_enabled"])
        self.assertEqual(res.status_code, 503)

    def test_direct_topup_endpoint_removed(self):
        headers = self.auth_headers("usr_student_kavya")
        self.assertIn(self.client.post("/api/wallet/topup", headers=headers, json={"amount": 5000}).status_code, (404, 405))

    def test_topup_amount_limits(self):
        headers = self.auth_headers("usr_student_kavya")
        for bad in ("nan", "inf", -5, 0, 5001, "abc", True):
            res = self.client.post("/api/payments/upi/create-qr", headers=headers, json={"amount": bad})
            self.assertEqual(res.status_code, 400, bad)

    def test_nan_does_not_lock_database(self):
        headers = self.auth_headers("usr_student_aarav")
        self.client.post("/api/payments/razorpay/create-order", headers=headers, json={"amount": "nan"})
        self.client.post("/api/rides/book", headers=headers, json={**CAMPUS_TRIP, "scope": "bogus"})
        res = self.client.post("/api/user/emergency-contacts", headers=headers,
                               json={"name": "Roommate", "phone": "+91 98765 43210"})
        self.assertEqual(res.status_code, 200)

    def test_razorpay_rejects_forged_signatures(self):
        with mock.patch.dict(os.environ, RAZORPAY_ENV):
            self.assertTrue(payments.verify_razorpay_signature("order_1", "pay_1", razorpay_signature("order_1", "pay_1")))
            for forged in ("demo_signature_valid", razorpay_signature("order_1", "pay_2"), "é" * 64):
                self.assertFalse(payments.verify_razorpay_signature("order_1", "pay_1", forged))
        self.assertFalse(payments.verify_razorpay_signature("order_1", "pay_1", razorpay_signature("order_1", "pay_1")))

    def test_razorpay_order_belongs_to_creator(self):
        aarav = self.auth_headers("usr_student_aarav")
        kavya = self.auth_headers("usr_student_kavya")
        with mock.patch.dict(os.environ, RAZORPAY_ENV), \
                mock.patch.object(payments, "_razorpay_api", return_value={"id": "order_aarav_1"}):
            order = self.client.post("/api/payments/razorpay/create-order", headers=aarav, json={"amount": 100}).get_json()
            res = self.client.post("/api/payments/razorpay/verify", headers=kavya, json={
                "order_id": order["order_id"], "payment_id": "pay_x", "signature": razorpay_signature(order["order_id"], "pay_x")})
        self.assertEqual(res.status_code, 404)

    def test_card_payments_are_off_without_razorpay_keys(self):
        headers = self.auth_headers("usr_student_aarav")
        res = self.client.post("/api/payments/razorpay/create-order", headers=headers, json={"amount": 100})
        self.assertEqual(res.status_code, 503)
        self.assertFalse(self.client.get("/api/config").get_json()["razorpay_enabled"])

    def test_negative_carpool_price_and_seats_rejected(self):
        host = self.auth_headers("usr_driver_harpreet")
        base = {"origin": "uni_mall", "destination": "jalandhar_bus_stand", "departure_time": departure_in(), "total_seats": 2}
        self.assertEqual(self.client.post("/api/routes/plan", headers=host, json={**base, "price_per_seat": -1000}).status_code, 400)
        self.assertEqual(self.client.post("/api/routes/plan", headers=host, json={**base, "total_seats": -3, "price_per_seat": 50}).status_code, 400)

        route_id = self.client.post("/api/routes/plan", headers=host, json={**base, "price_per_seat": 50}).get_json()["route_id"]
        rider = self.auth_headers("usr_student_aarav")
        before = self.balance(rider)
        for seats in (-20, 0, 1.5, "nan"):
            self.assertEqual(self.client.post(f"/api/routes/{route_id}/join", headers=rider, json={"seats": seats}).status_code, 400)
        self.assertEqual(self.balance(rider), before)
        self.assertEqual(self.client.post(f"/api/routes/{route_id}/join", headers=host, json={"seats": 1}).status_code, 400)

    def test_join_refused_without_balance_leaves_seats_untouched(self):
        host = self.auth_headers("usr_driver_harpreet")
        route_id = self.client.post("/api/routes/plan", headers=host, json={
            "origin": "uni_mall", "destination": "rama_mandi", "departure_time": departure_in(),
            "total_seats": 2, "price_per_seat": 1500}).get_json()["route_id"]
        kavya = self.auth_headers("usr_student_kavya")
        self.assertEqual(self.client.post(f"/api/routes/{route_id}/join", headers=kavya, json={"seats": 1}).status_code, 402)
        seats = self.db("SELECT available_seats FROM driver_routes WHERE id = ?", (route_id,))[0][0]
        self.assertEqual(seats, 2)


class TestInjectionAndAuth(ClientMixin, unittest.TestCase):
    def test_route_text_must_be_a_known_landmark(self):
        host = self.auth_headers("usr_driver_harpreet")
        res = self.client.post("/api/routes/plan", headers=host, json={
            "destination": "<img src=x onerror=alert(localStorage.campusgo_token)>",
            "departure_time": departure_in(), "total_seats": 1, "price_per_seat": 50})
        self.assertEqual(res.status_code, 400)

    def test_weak_secret_key_refused(self):
        for weak in ("dev_only_change_me_in_production", "short"):
            with mock.patch.dict(os.environ, {"SECRET_KEY": weak}):
                with self.assertRaises(RuntimeError):
                    app_module._load_secret_key()

    def test_personas_listing_removed(self):
        self.assertEqual(self.client.get("/api/auth/personas").status_code, 404)

    def test_errors_do_not_leak_internals(self):
        headers = self.auth_headers("usr_student_aarav")
        res = self.client.post("/api/rides/book", headers=headers, json={**CAMPUS_TRIP, "scope": "bogus"})
        self.assertEqual(res.status_code, 400)
        self.assertNotIn("CHECK", res.get_json()["error"])

    def test_security_headers(self):
        res = self.client.get("/")
        res.close()
        self.assertIn("default-src 'self'", res.headers["Content-Security-Policy"])
        self.assertEqual(res.headers["X-Content-Type-Options"], "nosniff")

    def test_emergency_contact_validation_and_limit(self):
        headers = self.auth_headers("usr_driver_simran")
        self.assertEqual(self.client.post("/api/user/emergency-contacts", headers=headers,
                                          json={"name": "X", "phone": "<b>hi</b>"}).status_code, 400)
        codes = [self.client.post("/api/user/emergency-contacts", headers=headers,
                                  json={"name": f"C{i}", "phone": f"+91987654321{i}"}).status_code for i in range(5)]
        self.assertIn(400, codes)  # two seeded + three new hits the limit of five

    def test_admin_routes_hidden_without_key(self):
        self.assertEqual(self.client.get("/api/admin/payments/pending").status_code, 404)

    def test_admin_credits_the_order_amount_once(self):
        kavya = self.auth_headers("usr_student_kavya")
        before = self.balance(kavya)
        ref = self.client.post("/api/payments/upi/create-qr", headers=kavya, json={"amount": 75}).get_json()["reference_id"]
        self.client.post("/api/payments/upi/confirm", headers=kavya, json={"reference_id": ref, "amount": 99999})
        with mock.patch.dict(os.environ, {"ADMIN_API_KEY": ADMIN_KEY}):
            self.assertEqual(self.client.get("/api/admin/payments/pending", headers={"X-Admin-Key": "wrong"}).status_code, 403)
            ok = self.client.post(f"/api/admin/payments/{ref}/approve", headers={"X-Admin-Key": ADMIN_KEY})
            self.assertEqual(ok.get_json()["amount_credited"], 75)
            again = self.client.post(f"/api/admin/payments/{ref}/approve", headers={"X-Admin-Key": ADMIN_KEY})
            self.assertEqual(again.status_code, 400)
        self.assertEqual(self.balance(kavya), before + 75)


class TestRideRules(ClientMixin, unittest.TestCase):
    def tearDown(self):
        self.db("UPDATE rides SET status = 'cancelled' WHERE status IN ('queued', 'matched', 'arriving', 'in_progress')")
        self.db("UPDATE driver_locations SET is_online = 1")

    def test_campus_hop_cannot_go_off_campus(self):
        headers = self.auth_headers("usr_student_aarav")
        for trip in ({"pickup_key": "uni_mall", "drop_key": "jalandhar_bus_stand", "scope": "campus_hop"},
                     {"pickup_key": "uni_mall", "drop_key": "block_34", "scope": "citylink"},
                     {"pickup_key": "uni_mall", "drop_key": "uni_mall", "scope": "campus_hop"}):
            self.assertEqual(self.client.post("/api/rides/quote", headers=headers, json=trip).status_code, 400, trip)

    def test_queued_ride_can_be_cancelled_and_refunded(self):
        self.db("UPDATE driver_locations SET is_online = 0")
        headers = self.auth_headers("usr_student_aarav")
        before = self.balance(headers)
        ride = self.client.post("/api/rides/book", headers=headers, json=CAMPUS_TRIP).get_json()
        self.assertEqual(ride["status"], "queued")
        self.assertEqual(self.client.post(f"/api/rides/{ride['ride_id']}/complete", headers=headers).status_code, 409)
        self.assertEqual(self.client.post(f"/api/rides/{ride['ride_id']}/cancel", headers=headers).status_code, 200)
        self.assertEqual(self.balance(headers), before)
        self.assertEqual(self.client.post("/api/rides/book", headers=headers, json=CAMPUS_TRIP).status_code, 200)

    def test_driver_accept_rules(self):
        self.db("UPDATE driver_locations SET is_online = 0")
        rider = self.auth_headers("usr_student_aarav")
        ride_id = self.client.post("/api/rides/book", headers=rider, json=CAMPUS_TRIP).get_json()["ride_id"]

        kavya = self.auth_headers("usr_student_kavya")  # no vehicle
        self.assertEqual(self.client.get("/api/driver/requests", headers=kavya).status_code, 403)
        self.assertEqual(self.client.post("/api/driver/accept", headers=kavya, json={"ride_id": ride_id}).status_code, 403)
        self.assertEqual(self.client.post("/api/driver/toggle-online", headers=kavya, json={"is_online": True}).status_code, 403)

        simran = self.auth_headers("usr_driver_simran")  # scooty can't take a bike request
        self.assertEqual(self.client.post("/api/driver/accept", headers=simran, json={"ride_id": ride_id}).status_code, 409)

        vikram = self.auth_headers("usr_driver_vikram")  # bike
        offline = self.client.get("/api/driver/requests", headers=vikram).get_json()
        self.assertEqual(offline["requests"], [])  # an offline driver is not shown requests
        self.assertTrue(offline["offline"])
        self.client.post("/api/driver/toggle-online", headers=vikram,
                         json={"is_online": True, "lat": 31.2535, "lng": 75.7038})
        reqs = self.client.get("/api/driver/requests", headers=vikram).get_json()["requests"]
        self.assertIn(ride_id, [r["id"] for r in reqs])
        self.assertNotIn("share_token", reqs[0])
        self.assertEqual(self.client.post("/api/driver/accept", headers=vikram, json={"ride_id": ride_id}).status_code, 200)
        self.assertEqual(self.client.post("/api/driver/accept", headers=vikram, json={"ride_id": ride_id}).status_code, 409)

    def test_share_link_hides_phone_and_expires(self):
        headers = self.auth_headers("usr_student_aarav")
        ride = self.client.post("/api/rides/book", headers=headers, json=CAMPUS_TRIP).get_json()
        token = self.client.get("/api/rides/active", headers=headers).get_json()["active_ride"]["share_token"]
        shared = self.client.get(f"/api/rides/share/{token}").get_json()["ride"]
        self.assertNotIn("driver_phone", shared)
        self.client.post(f"/api/rides/{ride['ride_id']}/cancel", headers=headers)
        self.assertEqual(self.client.get(f"/api/rides/share/{token}").status_code, 404)

    def test_odd_pin_is_refused_not_a_server_error(self):
        rider = self.auth_headers("usr_student_aarav")
        ride_id = self.client.post("/api/rides/book", headers=rider, json=CAMPUS_TRIP).get_json()["ride_id"]
        vikram = self.auth_headers("usr_driver_vikram")
        self.assertEqual(self.client.post("/api/driver/accept", headers=vikram, json={"ride_id": ride_id}).status_code, 200)
        self.client.post("/api/driver/location", headers=vikram, json={"lat": 31.2535, "lng": 75.7038})
        for pin in ("१२३४", "12a4", "12345"):  # Devanagari digits used to crash compare_digest
            res = self.client.post(f"/api/rides/{ride_id}/start", headers=vikram, json={"pin": pin})
            self.assertEqual(res.status_code, 400, pin)
        pin = self.client.get("/api/rides/active", headers=rider).get_json()["active_ride"]["start_pin"]
        self.assertEqual(self.client.post(f"/api/rides/{ride_id}/start", headers=vikram, json={"pin": pin}).status_code, 200)

    def test_sos_ignores_rides_the_user_is_not_in(self):
        aarav = self.auth_headers("usr_student_aarav")
        ride_id = self.client.post("/api/rides/book", headers=aarav, json=CAMPUS_TRIP).get_json()["ride_id"]
        kavya = self.auth_headers("usr_student_kavya")
        res = self.client.post("/api/sos/trigger", headers=kavya, json={"ride_id": ride_id, "lat": 31.25, "lng": 75.70})
        self.assertIsNone(res.get_json()["tracking_url"])
        mine = self.client.post("/api/sos/trigger", headers=aarav, json={"ride_id": ride_id, "lat": 31.25, "lng": 75.70})
        self.assertIn("/track/share_", mine.get_json()["tracking_url"])


if __name__ == "__main__":
    unittest.main()
