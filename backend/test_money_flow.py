"""End-to-end Campus Hop money flow: top-up -> ₹14 held -> driver sees ₹13 -> ₹1 platform cut -> driver payout."""
import os
import unittest
from unittest import mock

from test_support import ClientMixin

UNI_MALL = {"lat": 31.2535, "lng": 75.7038}
BLOCK_34 = {"lat": 31.2562, "lng": 75.7050}
ADMIN_KEY = "a" * 40
ADMIN = {"X-Admin-Key": ADMIN_KEY}


class TestCampusHopMoneyFlow(ClientMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        for name, value in {"DEMO_SIMULATE_MOVEMENT": "0", "ADMIN_API_KEY": ADMIN_KEY}.items():
            patcher = mock.patch.dict(os.environ, {name: value})
            patcher.start()
            self.addCleanup(patcher.stop)
        self.db("UPDATE rides SET status = 'cancelled' WHERE status IN ('queued', 'matched', 'arriving', 'in_progress')")
        self.db("UPDATE driver_routes SET status = 'cancelled' WHERE status = 'in_progress'")
        self.db("UPDATE driver_payouts SET status = 'rejected' WHERE status = 'requested'")
        self.driver = self.auth_headers("usr_driver_vikram")
        res = self.client.post("/api/driver/toggle-online", headers=self.driver, json={"is_online": True, **UNI_MALL})
        self.assertEqual(res.status_code, 200, res.get_json())

    def new_rider(self, phone, lpu_id, username):
        start = self.client.post("/api/auth/signup/start", json={
            "name": "New Rider", "username": username, "user_type": "student", "lpu_id": lpu_id, "phone": phone}).get_json()
        created = self.client.post("/api/auth/signup/verify",
                                   json={"challenge_id": start["challenge_id"], "otp": start["demo_otp"]}).get_json()
        return {"Authorization": f"Bearer {created['token']}"}

    def ride_once(self, rider, service="bike"):
        trip = {"pickup_key": "uni_mall", "drop_key": "block_34", "service_type": service, "scope": "campus_hop"}
        ride_id = self.client.post("/api/rides/book", headers=rider, json=trip).get_json()["ride_id"]
        self.assertEqual(self.client.post("/api/driver/accept", headers=self.driver, json={"ride_id": ride_id}).status_code, 200)
        pin = self.client.get("/api/rides/active", headers=rider).get_json()["active_ride"]["start_pin"]
        self.assertEqual(self.client.post(f"/api/rides/{ride_id}/start", headers=self.driver, json={"pin": pin}).status_code, 200)
        self.client.post("/api/driver/location", headers=self.driver, json=BLOCK_34)
        done = self.client.post(f"/api/rides/{ride_id}/complete", headers=self.driver)
        self.assertEqual(done.status_code, 200, done.get_json())
        self.client.post("/api/driver/location", headers=self.driver, json=UNI_MALL)
        return ride_id, done.get_json()

    def test_new_account_needs_exactly_14_then_driver_gets_13_and_platform_keeps_1(self):
        rider = self.new_rider("9000022222", "12399001", "flow.rider")
        self.assertEqual(self.balance(rider), 0.0)
        trip = {"pickup_key": "uni_mall", "drop_key": "block_34", "service_type": "bike", "scope": "campus_hop"}

        quote = self.client.post("/api/rides/quote", headers=rider, json=trip).get_json()["quotes"]["bike"]
        self.assertEqual(quote["total_fare"], 14.0)
        self.assertFalse(quote["has_sufficient_balance"])
        self.assertEqual(quote["deficit"], 14.0)
        refused = self.client.post("/api/rides/book", headers=rider, json=trip)
        self.assertEqual(refused.status_code, 402)
        self.assertEqual(refused.get_json()["code"], "INSUFFICIENT_WALLET_BALANCE")

        self.assertEqual(self.topup_demo(rider, 14).get_json()["new_balance"], 14.0)
        booked = self.client.post("/api/rides/book", headers=rider, json=trip).get_json()
        self.assertEqual(booked["wallet_balance"], 0.0)

        listed = self.client.get("/api/driver/requests", headers=self.driver).get_json()["requests"]
        self.assertEqual(next(r for r in listed if r["id"] == booked["ride_id"])["driver_payout"], 13.0)
        self.client.post(f"/api/rides/{booked['ride_id']}/cancel", headers=rider)
        self.assertEqual(self.balance(rider), 14.0)  # cancel refunds the full ₹14

        before = self.client.get("/api/admin/earnings", headers=ADMIN).get_json()
        ride_id, done = self.ride_once(rider)
        self.assertEqual(done["driver_payout"], 13.0)
        self.assertEqual(self.balance(rider), 0.0)

        after = self.client.get("/api/admin/earnings", headers=ADMIN).get_json()
        self.assertEqual(round(after["platform_earned"] - before["platform_earned"], 2), 1.0)
        latest = next(e for e in after["recent"] if e["reference_id"] == ride_id)
        self.assertEqual((latest["fare"], latest["driver_payout"], latest["amount"]), (14.0, 13.0, 1.0))

    def test_driver_cashes_out_earnings_and_admin_settles(self):
        rider = self.auth_headers("usr_student_aarav")
        self.db("UPDATE wallets SET balance = 500 WHERE user_id = 'usr_student_aarav'")
        self.ride_once(rider)
        self.ride_once(rider)
        available = self.client.get("/api/driver/payouts", headers=self.driver).get_json()["withdrawable"]
        self.assertGreaterEqual(available, 26.0)

        bad = self.client.post("/api/driver/payouts", headers=self.driver, json={"upi_id": "not-a-upi", "amount": 13})
        self.assertEqual(bad.status_code, 400)
        too_much = self.client.post("/api/driver/payouts", headers=self.driver, json={"upi_id": "vikram@okaxis", "amount": available + 1})
        self.assertEqual(too_much.status_code, 402)

        wallet_before = self.balance(self.driver)
        req = self.client.post("/api/driver/payouts", headers=self.driver, json={"upi_id": "vikram@okaxis", "amount": 13})
        self.assertEqual(req.status_code, 200, req.get_json())
        self.assertEqual(self.balance(self.driver), wallet_before - 13.0)
        again = self.client.post("/api/driver/payouts", headers=self.driver, json={"upi_id": "vikram@okaxis", "amount": 1})
        self.assertEqual(again.get_json()["code"], "PAYOUT_PENDING")

        pending = self.client.get("/api/admin/payouts/pending", headers=ADMIN).get_json()["pending"]
        self.assertIn(req.get_json()["payout_id"], [p["id"] for p in pending])
        self.assertEqual(self.client.post(f"/api/admin/payouts/{req.get_json()['payout_id']}/paid", headers=ADMIN,
                                          json={"utr": "412345678901"}).status_code, 200)
        self.assertEqual(self.client.post(f"/api/admin/payouts/{req.get_json()['payout_id']}/reject",
                                          headers=ADMIN).status_code, 404)  # already paid
        self.assertEqual(self.balance(self.driver), wallet_before - 13.0)

        # A rejected request puts the money back and it can be withdrawn again.
        second = self.client.post("/api/driver/payouts", headers=self.driver, json={"upi_id": "vikram@okaxis", "amount": 1}).get_json()
        self.client.post(f"/api/admin/payouts/{second['payout_id']}/reject", headers=ADMIN, json={"note": "wrong UPI"})
        self.assertEqual(self.balance(self.driver), wallet_before - 13.0)
        self.assertEqual(self.client.get("/api/driver/payouts", headers=self.driver).get_json()["withdrawable"],
                         round(available - 13.0, 2))

    def test_topped_up_money_cannot_be_withdrawn(self):
        rider = self.new_rider("9000033333", "12399002", "topup.only")
        self.topup_demo(rider, 100)
        res = self.client.post("/api/driver/payouts", headers=rider, json={"upi_id": "me@okaxis", "amount": 50})
        self.assertEqual(res.status_code, 402)
        self.assertEqual(res.get_json()["code"], "INSUFFICIENT_EARNINGS")


if __name__ == "__main__":
    unittest.main()
