"""Real-GPS trip rules, stale-request cleanup, faculty approval, logout and SOS safety."""
import os
import time
import unittest
from unittest import mock

from test_support import ClientMixin, departure_in
import app as app_module

UNI_MALL = {"lat": 31.2535, "lng": 75.7038}
BLOCK_34 = {"lat": 31.2562, "lng": 75.7050}
TRIP = {"pickup_key": "uni_mall", "drop_key": "block_34", "service_type": "bike", "scope": "campus_hop"}


class TestRealGpsTrips(ClientMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, {"DEMO_SIMULATE_MOVEMENT": "0"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.db("UPDATE rides SET status = 'cancelled' WHERE status IN ('queued', 'matched', 'arriving', 'in_progress')")
        self.rider = self.auth_headers("usr_student_aarav")
        self.driver = self.auth_headers("usr_driver_vikram")
        self.go_online()

    def go_online(self, **pos):
        res = self.client.post("/api/driver/toggle-online", headers=self.driver, json={"is_online": True, **(pos or UNI_MALL)})
        self.assertEqual(res.status_code, 200, res.get_json())

    def report(self, pos):
        res = self.client.post("/api/driver/location", headers=self.driver, json=pos)
        self.assertEqual(res.status_code, 200, res.get_json())
        return res.get_json()

    def book(self):
        res = self.client.post("/api/rides/book", headers=self.rider, json=TRIP)
        self.assertEqual(res.status_code, 200, res.get_json())
        return res.get_json()

    def accept(self, ride_id):
        res = self.client.post("/api/driver/accept", headers=self.driver, json={"ride_id": ride_id})
        self.assertEqual(res.status_code, 200, res.get_json())

    def test_booking_waits_for_a_driver_to_choose_it(self):
        ride = self.book()
        self.assertEqual(ride["status"], "queued")
        self.assertGreaterEqual(ride["drivers_notified"], 1)
        listed = self.client.get("/api/driver/requests", headers=self.driver).get_json()["requests"]
        item = next(r for r in listed if r["id"] == ride["ride_id"])
        self.assertIsNotNone(item["distance_to_pickup_km"])
        self.assertGreater(item["trip_km"], 0)
        self.client.post(f"/api/rides/{ride['ride_id']}/cancel", headers=self.rider)  # refund the held fare

    def test_offline_driver_cannot_accept(self):
        ride_id = self.book()["ride_id"]
        self.client.post("/api/driver/toggle-online", headers=self.driver, json={"is_online": False})
        res = self.client.post("/api/driver/accept", headers=self.driver, json={"ride_id": ride_id})
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.get_json()["code"], "DRIVER_OFFLINE")
        self.client.post(f"/api/rides/{ride_id}/cancel", headers=self.rider)  # refund the held fare

    def test_simulation_is_refused_when_disabled(self):
        ride_id = self.book()["ride_id"]
        for headers in (self.rider, self.driver):
            res = self.client.post(f"/api/rides/{ride_id}/telemetry-step", headers=headers)
            self.assertEqual(res.status_code, 403)
            self.assertEqual(res.get_json()["code"], "SIMULATION_DISABLED")

    def test_driver_cannot_collect_fare_without_driving(self):
        ride = self.book()
        ride_id = ride["ride_id"]
        self.assertEqual(ride["status"], "queued")
        self.accept(ride_id)
        before = self.balance(self.driver)

        # Not started, so completing is refused; arriving at the pickup starts it.
        self.assertEqual(self.client.post(f"/api/rides/{ride_id}/complete", headers=self.driver).status_code, 409)
        self.assertEqual(self.report(UNI_MALL)["ride_status"], "in_progress")

        # Still at the pickup: the driver can't finish the trip, and money hasn't moved.
        res = self.client.post(f"/api/rides/{ride_id}/complete", headers=self.driver)
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.get_json()["code"], "NOT_AT_DESTINATION")
        self.assertEqual(self.balance(self.driver), before)

        self.report(BLOCK_34)
        done = self.client.post(f"/api/rides/{ride_id}/complete", headers=self.driver)
        self.assertEqual(done.status_code, 200, done.get_json())
        self.assertEqual(self.balance(self.driver), before + 13.5)

    def test_rider_can_confirm_arrival_even_if_driver_phone_is_silent(self):
        ride_id = self.book()["ride_id"]
        self.accept(ride_id)
        self.report(UNI_MALL)
        self.assertEqual(self.client.post(f"/api/rides/{ride_id}/complete", headers=self.rider).status_code, 200)

    def test_location_needs_both_coordinates(self):
        res = self.client.post("/api/driver/location", headers=self.driver, json={"lat": 31.25})
        self.assertEqual(res.status_code, 400)

    def test_silent_driver_is_not_matched(self):
        self.db("UPDATE driver_locations SET updated_at = ? WHERE driver_id = 'usr_driver_vikram'", (time.time() - 600,))
        self.assertEqual(self.book()["status"], "queued")
        nearby = self.client.get("/api/drivers/nearby", headers=self.rider).get_json()["drivers"]
        self.assertFalse([d for d in nearby if d["first_name"] == "Vikram"])

    def test_unmatched_request_expires_with_refund(self):
        self.db("UPDATE driver_locations SET updated_at = ? WHERE driver_id = 'usr_driver_vikram'", (time.time() - 600,))
        before = self.balance(self.rider)
        ride_id = self.book()["ride_id"]
        self.assertEqual(self.balance(self.rider), before - 15.0)
        self.db("UPDATE rides SET created_at = ? WHERE id = ?", (time.time() - 3600, ride_id))
        conn = app_module.get_db_connection()
        try:
            app_module.sweep_stale(conn, force=True)
        finally:
            conn.close()
        self.assertEqual(self.db("SELECT status FROM rides WHERE id = ?", (ride_id,))[0]["status"], "cancelled")
        self.assertEqual(self.balance(self.rider), before)

    def test_carpool_needs_host_at_origin_and_destination(self):
        host = self.auth_headers("usr_driver_harpreet")
        route_id = self.client.post("/api/routes/plan", headers=host, json={
            "origin": "uni_mall", "destination": "phagwara_station", "departure_time": departure_in(),
            "total_seats": 2, "price_per_seat": 50}).get_json()["route_id"]
        self.assertEqual(self.client.post(f"/api/routes/{route_id}/join", headers=self.rider, json={"seats": 1}).status_code, 200)
        self.client.post("/api/driver/location", headers=host, json={"lat": 31.3190, "lng": 75.5860})  # Jalandhar
        res = self.client.post(f"/api/routes/{route_id}/start", headers=host)
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.get_json()["code"], "NOT_AT_ORIGIN")
        self.client.post("/api/driver/location", headers=host, json=UNI_MALL)
        self.assertEqual(self.client.post(f"/api/routes/{route_id}/start", headers=host).status_code, 200)
        res = self.client.post(f"/api/routes/{route_id}/complete", headers=host)
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.get_json()["code"], "NOT_AT_DESTINATION")
        self.client.post("/api/driver/location", headers=host, json={"lat": 31.2210, "lng": 75.7720})  # Phagwara station
        self.assertEqual(self.client.post(f"/api/routes/{route_id}/complete", headers=host).status_code, 200)

    def test_carpool_rejects_free_text_and_past_departure(self):
        host = self.auth_headers("usr_driver_harpreet")
        base = {"origin": "uni_mall", "destination": "phagwara_station", "total_seats": 1, "price_per_seat": 50}
        self.assertEqual(self.client.post("/api/routes/plan", headers=host, json={**base, "departure_time": "soon"}).status_code, 400)
        self.assertEqual(self.client.post("/api/routes/plan", headers=host, json={**base, "departure_time": departure_in(-48)}).status_code, 400)

    def test_passengers_do_not_see_each_others_phone(self):
        host = self.auth_headers("usr_driver_harpreet")
        route_id = self.client.post("/api/routes/plan", headers=host, json={
            "origin": "uni_mall", "destination": "phagwara_station", "departure_time": departure_in(),
            "total_seats": 2, "price_per_seat": 50}).get_json()["route_id"]
        self.client.post(f"/api/routes/{route_id}/join", headers=self.rider, json={"seats": 1})
        mine = self.client.get(f"/api/routes/{route_id}/live", headers=self.rider).get_json()["cockpit"]
        theirs = self.client.get(f"/api/routes/{route_id}/live", headers=host).get_json()["cockpit"]
        self.assertTrue(all("passenger_phone" not in p for p in mine["passengers"]))
        self.assertTrue(all("passenger_phone" in p for p in theirs["passengers"]))
        self.client.post(f"/api/routes/{route_id}/cancel", headers=host)


class TestIdentityAndSos(ClientMixin, unittest.TestCase):
    def signup(self, phone, lpu_id, username, user_type="teacher"):
        start = self.client.post("/api/auth/signup/start", json={
            "name": "Test Person", "username": username, "user_type": user_type, "lpu_id": lpu_id, "phone": phone}).get_json()
        return self.client.post("/api/auth/signup/verify",
                                json={"challenge_id": start["challenge_id"], "otp": start["demo_otp"]}).get_json()

    def test_new_teacher_gets_no_priority_until_admin_approves(self):
        created = self.signup("9000011111", "FAC-99001", "new.teacher")
        self.assertEqual(created["user"]["user_type"], "teacher")
        self.assertFalse(created["user"]["is_teacher_priority"])

        key = "k" * 40
        with mock.patch.dict(os.environ, {"ADMIN_API_KEY": key}):
            admin = {"X-Admin-Key": key}
            pending = self.client.get("/api/admin/faculty/pending", headers=admin).get_json()["pending"]
            self.assertIn(created["user"]["id"], [p["id"] for p in pending])
            res = self.client.post(f"/api/admin/faculty/{created['user']['id']}/approve", headers=admin)
            self.assertEqual(res.status_code, 200)
        headers = {"Authorization": f"Bearer {created['token']}"}
        self.assertTrue(self.client.get("/api/auth/me", headers=headers).get_json()["user"]["is_teacher_priority"])

    def test_logout_revokes_every_token(self):
        one = self.auth_headers("usr_student_kavya")
        two = self.auth_headers("usr_student_kavya")
        self.assertEqual(self.client.get("/api/auth/me", headers=one).status_code, 200)
        self.assertEqual(self.client.post("/api/auth/logout", headers=one).status_code, 200)
        self.assertEqual(self.client.get("/api/auth/me", headers=one).status_code, 401)
        self.assertEqual(self.client.get("/api/auth/me", headers=two).status_code, 401)

    def test_login_for_unknown_account_looks_like_a_real_request(self):
        with mock.patch.dict(os.environ, {"OTP_DEMO_MODE": "0"}):
            res = self.client.post("/api/auth/login/start", json={"lpu_id": "99999999", "phone": "9111111111"})
            self.assertEqual(res.status_code, 200)
            body = res.get_json()
            self.assertIn("challenge_id", body)
            self.assertNotIn("demo_otp", body)
            verify = self.client.post("/api/auth/login/verify", json={"challenge_id": body["challenge_id"], "otp": "123456"})
            self.assertEqual(verify.status_code, 400)

    def test_strangers_cannot_exhaust_a_victims_login_attempts(self):
        for _ in range(12):
            self.client.post("/api/auth/login/start", json={"lpu_id": "12204592", "phone": "9000000000"},
                             environ_overrides={"REMOTE_ADDR": "10.0.0.66"})
        start = self.client.post("/api/auth/login/start", json={"lpu_id": "12204592", "phone": "9812345678"},
                                 environ_overrides={"REMOTE_ADDR": "10.0.0.7"})
        self.assertEqual(start.status_code, 200)

    def test_sos_without_location_says_so(self):
        headers = self.auth_headers("usr_student_aarav")
        res = self.client.post("/api/sos/trigger", headers=headers, json={"location_name": "Location unavailable"})
        self.assertEqual(res.status_code, 200, res.get_json())
        body = res.get_json()
        self.assertIn("unavailable", body["sos_message"])
        self.assertNotIn("31.25", body["sos_message"])
        self.assertEqual(self.client.post(f"/api/sos/{body['alert_id']}/resolve", headers=headers).status_code, 200)

    def test_sos_message_keeps_link_and_hotline_short(self):
        msg = app_module.dispatch_emergency_alert(
            "Aarav Mehta", "12204592", "Near Uni-Mall & Student Plaza", 31.2535, 75.7038, [],
            "https://campusgo.example.com/track/share_abcdefghijklmnop")["sos_message"]
        self.assertIn("https://campusgo.example.com/track/", msg)
        self.assertIn(app_module.CAMPUS_SECURITY_PHONE, msg)
        self.assertLess(len(msg), 260)

    def test_contact_cannot_be_own_number_or_duplicate(self):
        headers = self.auth_headers("usr_student_kavya")
        own = self.client.post("/api/user/emergency-contacts", headers=headers, json={"name": "Me", "phone": "9823456789"})
        self.assertEqual(own.status_code, 400)
        first = self.client.post("/api/user/emergency-contacts", headers=headers, json={"name": "Mum", "phone": "+919700000001"})
        self.assertEqual(first.status_code, 200)
        dup = self.client.post("/api/user/emergency-contacts", headers=headers, json={"name": "Mum2", "phone": "9700000001"})
        self.assertEqual(dup.status_code, 409)

    def test_one_number_cannot_be_flooded_with_sos_texts(self):
        cap = app_module.SOS_CONTACT_TEXTS_PER_HOUR
        for _ in range(cap):
            app_module.check_rate("sos-dest:9888800000", cap, 3600)
        with self.assertRaises(app_module.ApiError):
            app_module.check_rate("sos-dest:9888800000", cap, 3600)


if __name__ == "__main__":
    unittest.main()
