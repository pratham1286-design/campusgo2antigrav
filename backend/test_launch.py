"""What Google Play and the App Store require (account deletion, policy pages, an installable
app) and the privacy rules for what other users can see."""
import json
import os
import time
import unittest
from unittest import mock

from test_support import ADMIN_KEY, SMS_OUTBOX, ClientMixin, departure_in
import app as app_module

TRIP = {"pickup_key": "uni_mall", "drop_key": "block_34", "service_type": "bike", "scope": "campus_hop"}


class TestAccountDeletion(ClientMixin, unittest.TestCase):
    accounts = 0

    def setUp(self):
        super().setUp()
        TestAccountDeletion.accounts += 1  # a fresh account per test
        n = TestAccountDeletion.accounts
        self.phone, self.lpu_id, self.username = f"900004444{n}", f"1239904{n}", f"leaving.soon{n}"
        self.created = self.signup(self.phone, self.lpu_id, self.username, name="Leaving Soon")
        self.headers = {"Authorization": f"Bearer {self.created['token']}"}
        self.uid = self.created["user"]["id"]

    def test_account_with_an_active_ride_cannot_be_deleted(self):
        self.topup(self.headers, 50)
        ride_id = self.client.post("/api/rides/book", headers=self.headers, json=TRIP).get_json()["ride_id"]
        res = self.client.post("/api/user/delete-account", headers=self.headers)
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.get_json()["code"], "ACCOUNT_BUSY")
        self.client.post(f"/api/rides/{ride_id}/cancel", headers=self.headers)
        self.assertEqual(self.client.post("/api/user/delete-account", headers=self.headers).status_code, 200)

    def test_deleting_removes_personal_data_and_ends_every_session(self):
        self.client.post("/api/user/emergency-contacts", headers=self.headers, json={"name": "Mum", "phone": "9700000099"})
        self.client.post("/api/user/vehicle", headers=self.headers, json={
            "category": "bike", "model": "Pulsar", "plate_number": "PB08-ZZ-0001", "capacity": 1})
        self.client.post("/api/driver/toggle-online", headers=self.headers, json={"is_online": True, "lat": 31.2535, "lng": 75.7038})

        self.assertEqual(self.client.post("/api/user/delete-account", headers=self.headers).status_code, 200)
        self.assertEqual(self.client.get("/api/auth/me", headers=self.headers).status_code, 401)
        user = self.db("SELECT * FROM users WHERE id = ?", (self.uid,))[0]
        self.assertEqual((user["name"], user["phone"]), ("Deleted user", ""))
        self.assertIsNotNone(user["deleted_at"])
        for table, column in (("emergency_contacts", "user_id"), ("vehicles", "user_id"), ("driver_locations", "driver_id")):
            self.assertEqual(self.db(f"SELECT COUNT(*) AS n FROM {table} WHERE {column} = ?", (self.uid,))[0]["n"], 0, table)

        # The old LPU ID and number no longer lead anywhere, and are free for a new sign-up.
        SMS_OUTBOX.clear()
        login = self.client.post("/api/auth/login/start", json={"lpu_id": self.lpu_id, "phone": self.phone})
        self.assertEqual(login.status_code, 200)
        self.assertEqual(SMS_OUTBOX, [])
        self.db("DELETE FROM otp_challenges")
        again = self.signup(self.phone, self.lpu_id, self.username, name="Leaving Soon")
        self.assertNotEqual(again["user"]["id"], self.uid)

    def test_trip_records_survive_without_the_name(self):
        self.topup(self.headers, 50)
        ride_id = self.client.post("/api/rides/book", headers=self.headers, json=TRIP).get_json()["ride_id"]
        self.client.post(f"/api/rides/{ride_id}/cancel", headers=self.headers)
        self.client.post("/api/user/delete-account", headers=self.headers)
        self.assertEqual(self.db("SELECT rider_id FROM rides WHERE id = ?", (ride_id,))[0]["rider_id"], self.uid)
        self.assertGreater(len(self.db("SELECT id FROM wallet_transactions WHERE user_id = ?", (self.uid,))), 0)

    def test_deleted_teacher_leaves_the_approval_queue(self):
        teacher = self.signup("9000055555", "FAC-99055", "gone.teacher", user_type="teacher")
        self.client.post("/api/user/delete-account", headers={"Authorization": f"Bearer {teacher['token']}"})
        with mock.patch.dict(os.environ, {"ADMIN_API_KEY": ADMIN_KEY}):
            pending = self.client.get("/api/admin/faculty/pending", headers={"X-Admin-Key": ADMIN_KEY}).get_json()["pending"]
            approve = self.client.post(f"/api/admin/faculty/{teacher['user']['id']}/approve", headers={"X-Admin-Key": ADMIN_KEY})
        self.assertNotIn(teacher["user"]["id"], [p["id"] for p in pending])
        self.assertEqual(approve.status_code, 404)


class TestStoreListingPages(ClientMixin, unittest.TestCase):
    def get(self, path):
        res = self.client.get(path)
        res.close()
        return res

    def test_policy_pages_are_public(self):
        for path, heading in (("/privacy", b"Privacy Policy"), ("/terms", b"Terms of Service")):
            res = self.client.get(path)
            self.assertEqual(res.status_code, 200, path)
            self.assertIn(heading, res.data)
            self.assertIn(b"Delete Account", res.data)

    def test_app_is_installable(self):
        manifest = json.loads(self.client.get("/manifest.webmanifest").data)
        self.assertEqual(manifest["start_url"], "/")
        self.assertEqual(manifest["display"], "standalone")
        self.assertIn("maskable", [i["purpose"] for i in manifest["icons"]])
        for icon in manifest["icons"]:
            res = self.client.get(icon["src"], headers={"Accept-Encoding": "gzip"})
            res.close()
            self.assertEqual((res.status_code, res.content_type), (200, "image/png"), icon["src"])
            self.assertNotIn("Content-Encoding", res.headers)  # PNGs are already compressed
        self.assertEqual(self.get("/manifest.webmanifest").content_type, "application/manifest+json")
        for path in ("/sw.js", "/offline.html", "/favicon.svg", "/apple-touch-icon.png"):
            self.assertEqual(self.get(path).status_code, 200, path)

    def test_pages_load_no_third_party_scripts(self):
        page = self.client.get("/").data.decode()
        self.assertIn('src="/leaflet.js?v=', page)
        self.assertNotIn("unpkg.com", page)
        csp = self.get("/").headers["Content-Security-Policy"]
        self.assertNotIn("unpkg", csp)
        self.assertNotIn("unsplash", csp)

    def test_no_demo_content_ships(self):
        page = self.client.get("/").data.decode()
        for leftover in ("pratham1286", "Harpreet", "PB08-CR-9912", "Jalandhar City Bus Stand", "Advance Simulation", "demo"):
            self.assertNotIn(leftover, page)
        self.assertEqual(set(self.client.get("/api/config").get_json()),
                         {"security_hotline", "razorpay_enabled", "upi_enabled", "tile_url", "tile_attribution"})

    def test_android_app_links_follow_the_play_console_values(self):
        self.assertEqual(self.get("/.well-known/assetlinks.json").status_code, 404)
        fingerprint = "AB:CD:" + ":".join(["00"] * 30)
        with mock.patch.dict(os.environ, {"ANDROID_PACKAGE_NAME": "app.campusgo.twa", "ANDROID_CERT_SHA256": fingerprint}):
            links = self.client.get("/.well-known/assetlinks.json").get_json()
        self.assertEqual(links[0]["target"], {"namespace": "android_app", "package_name": "app.campusgo.twa",
                                              "sha256_cert_fingerprints": [fingerprint]})

    def test_unknown_files_are_not_served(self):
        for path in ("/app.py", "/.env", "/campusgo.db", "/privacy.html"):
            self.assertEqual(self.get(path).status_code, 404, path)


class TestWhatOtherUsersSee(ClientMixin, unittest.TestCase):
    def test_nearby_drivers_are_anonymous_and_approximate(self):
        self.db("UPDATE driver_locations SET lat = 31.25351, lng = 75.70386, updated_at = ? WHERE driver_id = 'usr_driver_simran'",
                (time.time(),))
        rider = self.auth_headers("usr_student_aarav")
        drivers = self.client.get("/api/drivers/nearby", headers=rider).get_json()["drivers"]
        simran = next(d for d in drivers if d["key"] == app_module.driver_public_key("usr_driver_simran"))
        self.assertEqual(set(simran), {"key", "lat", "lng", "category"})
        self.assertEqual((simran["lat"], simran["lng"]), (31.254, 75.704))

    def test_carpool_listing_shows_first_names_only(self):
        host = self.auth_headers("usr_driver_harpreet")
        route_id = self.client.post("/api/routes/plan", headers=host, json={
            "origin": "uni_mall", "destination": "phagwara_station", "departure_time": departure_in(),
            "total_seats": 2, "price_per_seat": 50}).get_json()["route_id"]
        self.client.post(f"/api/routes/{route_id}/join", headers=self.auth_headers("usr_teacher_raman"), json={"seats": 1})
        stranger = self.auth_headers("usr_student_kavya")
        listed = next(r for r in self.client.get("/api/routes/scheduled", headers=stranger).get_json()["routes"]
                      if r["id"] == route_id)
        self.assertEqual(listed["driver_name"], "Harpreet")
        self.assertEqual(listed["passengers"], [{"passenger_name": "Raman", "seats": 1}])
        self.assertFalse(listed["is_host"] or listed["has_joined"])
        self.client.post(f"/api/routes/{route_id}/cancel", headers=host)


if __name__ == "__main__":
    unittest.main()
