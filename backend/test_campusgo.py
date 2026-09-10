import unittest
import json
import time
from app import app
from seed_data import seed
from database import get_db_connection

class TestCampusGo(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        seed()
        cls.client = app.test_client()

    def test_01_personas_and_teacher_badge(self):
        """Verify LPU personas and teacher verification badge & priority flag."""
        res = self.client.get("/api/auth/personas")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        personas = data["personas"]
        self.assertGreater(len(personas), 0)

        teacher = next(p for p in personas if p["id"] == "usr_teacher_raman")
        self.assertEqual(teacher["user_type"], "teacher")
        self.assertEqual(teacher["is_teacher_priority"], 1)
        self.assertEqual(teacher["is_verified"], 1)

    def test_02_server_side_fare_quotes(self):
        """Verify 100% server-side fare calculation: Flat rates for Campus Hop and dynamic for CityLink."""
        # Campus Hop: Uni-Mall to Block 34
        res = self.client.post("/api/rides/quote", json={
            "user_id": "usr_student_aarav",
            "pickup_key": "uni_mall",
            "drop_key": "block_34",
            "scope": "campus_hop"
        })
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        quotes = data["quotes"]

        # Strict flat rates
        self.assertEqual(quotes["bike"]["total_fare"], 15.0)
        self.assertEqual(quotes["scooty"]["total_fare"], 20.0)
        self.assertEqual(quotes["car"]["total_fare"], 35.0)

        # CityLink: Uni-Mall to Jalandhar City Bus Stand
        res_city = self.client.post("/api/rides/quote", json={
            "user_id": "usr_student_aarav",
            "pickup_key": "uni_mall",
            "drop_key": "jalandhar_bus_stand",
            "scope": "citylink"
        })
        self.assertEqual(res_city.status_code, 200)
        data_city = res_city.get_json()
        quotes_city = data_city["quotes"]
        self.assertGreater(quotes_city["car"]["total_fare"], 100.0)

    def test_03_wallet_validation_blocks_insufficient_funds(self):
        """Verify server strictly blocks booking if wallet balance is below fare."""
        # Kavya Patel has initial balance of ₹10.0
        # Attempt to book a ₹20 scooty ride
        res = self.client.post("/api/rides/book", json={
            "user_id": "usr_student_kavya",
            "pickup_key": "uni_mall",
            "drop_key": "block_34",
            "service_type": "scooty",
            "scope": "campus_hop"
        })
        self.assertEqual(res.status_code, 402)
        err = res.get_json()
        self.assertEqual(err["code"], "INSUFFICIENT_WALLET_BALANCE")
        self.assertGreater(err["deficit"], 0)

        # Top-up Kavya's wallet by ₹100
        topup_res = self.client.post("/api/wallet/topup", json={
            "user_id": "usr_student_kavya",
            "amount": 100.0
        })
        self.assertEqual(topup_res.status_code, 200)
        topup_data = topup_res.get_json()
        self.assertEqual(topup_data["new_balance"], 110.0)

        # Re-attempt booking -> Should now succeed!
        book_res = self.client.post("/api/rides/book", json={
            "user_id": "usr_student_kavya",
            "pickup_key": "uni_mall",
            "drop_key": "block_34",
            "service_type": "scooty",
            "scope": "campus_hop"
        })
        self.assertEqual(book_res.status_code, 200)
        book_data = book_res.get_json()
        self.assertIn(book_data["status"], ("matched", "arriving", "queued"))
        ride_id = book_data["ride_id"]

        # Clean up ride for subsequent tests
        conn = get_db_connection()
        conn.execute("UPDATE rides SET status = 'cancelled' WHERE id = ?", (ride_id,))
        conn.commit()
        conn.close()

    def test_04_teacher_priority_matching(self):
        """Verify that teacher rides receive priority matching flag."""
        res = self.client.post("/api/rides/book", json={
            "user_id": "usr_teacher_raman",
            "pickup_key": "uni_mall",
            "drop_key": "block_34",
            "service_type": "car",
            "scope": "campus_hop"
        })
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["is_priority"])

        # Clean up ride
        ride_id = data["ride_id"]
        conn = get_db_connection()
        conn.execute("UPDATE rides SET status = 'cancelled' WHERE id = ?", (ride_id,))
        conn.commit()
        conn.close()

    def test_05_active_ride_completion_and_wallet_deduction(self):
        """Verify ride lifecycle from booking -> telemetry step -> auto-deduction on completion."""
        # 1. Aarav books bike ride
        res = self.client.post("/api/rides/book", json={
            "user_id": "usr_student_aarav",
            "pickup_key": "uni_mall",
            "drop_key": "block_34",
            "service_type": "bike",
            "scope": "campus_hop"
        })
        self.assertEqual(res.status_code, 200)
        ride_data = res.get_json()
        ride_id = ride_data["ride_id"]

        # Fetch Aarav wallet before completion
        wallet_res = self.client.get("/api/wallet?user_id=usr_student_aarav")
        bal_before = wallet_res.get_json()["balance"]

        # 2. Advance telemetry
        step_res = self.client.post(f"/api/rides/{ride_id}/telemetry-step")
        self.assertEqual(step_res.status_code, 200)

        # 3. Conclude ride
        comp_res = self.client.post(f"/api/rides/{ride_id}/complete")
        self.assertEqual(comp_res.status_code, 200)
        comp_data = comp_res.get_json()
        self.assertTrue(comp_data["success"])

        # Check that rider wallet was auto-deducted
        wallet_res_after = self.client.get("/api/wallet?user_id=usr_student_aarav")
        bal_after = wallet_res_after.get_json()["balance"]
        self.assertEqual(bal_after, bal_before - 15.0)

        # Rate the ride
        rate_res = self.client.post(f"/api/rides/{ride_id}/rate", json={
            "reviewer_id": "usr_student_aarav",
            "rating": 5,
            "tags": "Punctual,Safe Riding,Clean Helmet",
            "comment": "Super smooth ride from Uni-Mall to Block 34!"
        })
        self.assertEqual(rate_res.status_code, 200)

    def test_06_sos_emergency_dispatch(self):
        """Verify persistent SOS button triggers emergency dispatch to trusted contacts and campus security."""
        res = self.client.post("/api/sos/trigger", json={
            "user_id": "usr_student_aarav",
            "lat": 31.2535,
            "lng": 75.7038,
            "location_name": "Uni-Mall Student Plaza"
        })
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data["status"], "DISPATCHED")
        self.assertGreater(len(data["contacts_notified"]), 0)
        self.assertIn("Block 30", data["campus_security_hotline"])

    def test_07_driver_actions_and_scheduled_routes(self):
        """Verify driver toggle, earnings dashboard, and posting CityLink scheduled carpool."""
        # Driver earnings
        res = self.client.get("/api/driver/earnings?driver_id=usr_driver_vikram")
        self.assertEqual(res.status_code, 200)
        earnings = res.get_json()
        self.assertIn("total_earnings", earnings)

        # Post CityLink route
        route_res = self.client.post("/api/driver/routes", json={
            "driver_id": "usr_driver_vikram",
            "destination": "Rama Mandi Chowk",
            "departure_time": "19:00 Today",
            "available_seats": 1,
            "price_per_seat": 45.0,
            "notes": "Direct commute via GT Road"
        })
        self.assertEqual(route_res.status_code, 200)
        route_id = route_res.get_json()["route_id"]

        # List routes
        list_res = self.client.get("/api/driver/routes")
        routes = list_res.get_json()["routes"]
        found = any(r["id"] == route_id for r in routes)
        self.assertTrue(found)

if __name__ == "__main__":
    unittest.main()
