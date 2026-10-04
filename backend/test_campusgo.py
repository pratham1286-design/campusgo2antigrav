import unittest

from test_support import ClientMixin, departure_in


class TestCampusGo(ClientMixin, unittest.TestCase):
    def test_00_protected_routes_need_a_token(self):
        self.assertEqual(self.client.get("/api/wallet").status_code, 401)

    def test_01_login_returns_teacher_badge_without_password_hash(self):
        headers = self.auth_headers("usr_teacher_raman")
        user = self.client.get("/api/auth/me", headers=headers).get_json()["user"]
        self.assertEqual(user["is_teacher_priority"], 1)
        self.assertNotIn("password_hash", user)

    def test_02_server_side_fare_quotes(self):
        headers = self.auth_headers("usr_student_aarav")
        res = self.client.post("/api/rides/quote", headers=headers, json={
            "pickup_key": "uni_mall", "drop_key": "block_34", "scope": "campus_hop"})
        quotes = res.get_json()["quotes"]
        self.assertEqual((quotes["bike"]["total_fare"], quotes["scooty"]["total_fare"], quotes["car"]["total_fare"]),
                         (15.0, 20.0, 35.0))

        city = self.client.post("/api/rides/quote", headers=headers, json={
            "pickup_key": "uni_mall", "drop_key": "chandigarh_isbt_43", "scope": "citylink"})
        self.assertEqual(city.status_code, 200)
        self.assertGreater(city.get_json()["quotes"]["car"]["total_fare"], 1000.0)

    def test_03_insufficient_balance_then_topup_then_book(self):
        headers = self.auth_headers("usr_student_kavya")  # starts with ₹10
        trip = {"pickup_key": "uni_mall", "drop_key": "block_34", "service_type": "scooty", "scope": "campus_hop"}
        res = self.client.post("/api/rides/book", headers=headers, json=trip)
        self.assertEqual(res.status_code, 402)
        self.assertEqual(res.get_json()["code"], "INSUFFICIENT_WALLET_BALANCE")

        self.assertEqual(self.topup_demo(headers, 100).get_json()["new_balance"], 110.0)

        book = self.client.post("/api/rides/book", headers=headers, json=trip)
        self.assertEqual(book.status_code, 200)
        self.assertEqual(book.get_json()["wallet_balance"], 90.0)  # fare held at booking
        cancel = self.client.post(f"/api/rides/{book.get_json()['ride_id']}/cancel", headers=headers)
        self.assertEqual(cancel.get_json()["wallet_balance"], 110.0)  # refunded in full

    def test_04_teacher_priority_flag(self):
        headers = self.auth_headers("usr_teacher_raman")
        res = self.client.post("/api/rides/book", headers=headers, json={
            "pickup_key": "uni_mall", "drop_key": "block_34", "service_type": "car", "scope": "campus_hop"})
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.get_json()["is_priority"])
        self.client.post(f"/api/rides/{res.get_json()['ride_id']}/cancel", headers=headers)

    def test_05_ride_lifecycle_pays_driver_and_rating_once(self):
        rider = self.auth_headers("usr_student_aarav")
        before = self.balance(rider)
        res = self.client.post("/api/rides/book", headers=rider, json={
            "pickup_key": "uni_mall", "drop_key": "block_34", "service_type": "bike", "scope": "campus_hop"})
        self.assertEqual(res.status_code, 200)
        ride_id = res.get_json()["ride_id"]
        self.assertEqual(self.balance(rider), before - 15.0)
        vikram = self.auth_headers("usr_driver_vikram")
        self.client.post("/api/driver/toggle-online", headers=vikram, json={"is_online": True, "lat": 31.2535, "lng": 75.7038})
        self.assertEqual(self.client.post("/api/driver/accept", headers=vikram, json={"ride_id": ride_id}).status_code, 200)

        # Can't complete before pickup, and can't rate before completion.
        self.assertEqual(self.client.post(f"/api/rides/{ride_id}/complete", headers=rider).status_code, 409)
        self.assertEqual(self.client.post(f"/api/rides/{ride_id}/rate", headers=rider, json={"rating": 5}).status_code, 409)
        step = {}
        driver = self.auth_headers(self.db("SELECT driver_id FROM rides WHERE id = ?", (ride_id,))[0]["driver_id"])
        for _ in range(20):
            step = self.client.post(f"/api/rides/{ride_id}/telemetry-step", headers=driver).get_json()
            if step["distance_remaining_km"] == 0:
                break
        self.assertEqual(step["status"], "arriving")  # at the pickup, the ride still waits for the rider's PIN
        pin = self.client.get("/api/rides/active", headers=rider).get_json()["active_ride"]["start_pin"]
        start = self.client.post(f"/api/rides/{ride_id}/start", headers=driver, json={"pin": pin})
        self.assertEqual(start.status_code, 200, start.get_json())

        comp = self.client.post(f"/api/rides/{ride_id}/complete", headers=rider)
        self.assertEqual(comp.status_code, 200)
        self.assertEqual(comp.get_json()["driver_payout"], 13.5)
        self.assertEqual(self.balance(rider), before - 15.0)  # no second charge
        self.assertEqual(self.client.post(f"/api/rides/{ride_id}/complete", headers=rider).status_code, 409)

        rate = self.client.post(f"/api/rides/{ride_id}/rate", headers=rider, json={"rating": 4, "tags": "Punctual"})
        self.assertEqual(rate.status_code, 200)
        again = self.client.post(f"/api/rides/{ride_id}/rate", headers=rider, json={"rating": 1})
        self.assertEqual(again.status_code, 409)

    def test_06_sos_reports_delivery_honestly(self):
        headers = self.auth_headers("usr_student_aarav")
        res = self.client.post("/api/sos/trigger", headers=headers, json={
            "lat": 31.2535, "lng": 75.7038, "location_name": "Uni-Mall Student Plaza"})
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertGreater(len(data["contacts_notified"]), 0)
        self.assertTrue(all(c["delivery"]["status"] == "not_sent" for c in data["contacts_notified"]))
        self.assertEqual(data["campus_security_dispatch"]["status"], "not_connected")

    def test_07_driver_posts_route_to_chandigarh(self):
        headers = self.auth_headers("usr_driver_harpreet")
        res = self.client.post("/api/routes/plan", headers=headers, json={
            "origin": "Uni-Mall & Student Plaza", "destination": "Chandigarh ISBT Sector 43",
            "departure_time": departure_in(4), "total_seats": 3, "price_per_seat": 250})
        self.assertEqual(res.status_code, 200, res.get_json())
        route_id = res.get_json()["route_id"]
        routes = self.client.get("/api/routes/scheduled", headers=headers).get_json()["routes"]
        listed = next(r for r in routes if r["id"] == route_id)
        self.assertTrue(listed["is_host"])
        self.assertNotIn("driver_phone", listed)

    def test_08_cannot_act_as_another_user(self):
        headers = self.auth_headers("usr_student_kavya")
        own = {c["phone"] for c in self.client.get("/api/user/emergency-contacts", headers=headers).get_json()["contacts"]}
        res = self.client.post("/api/sos/trigger", headers=headers,
                               json={"user_id": "usr_teacher_raman", "lat": 31.25, "lng": 75.70})
        self.assertEqual({c["contact_phone"] for c in res.get_json()["contacts_notified"]}, own)

    def test_09_carpool_join_start_complete_pays_host(self):
        host = self.auth_headers("usr_driver_harpreet")
        rider = self.auth_headers("usr_teacher_raman")
        route_id = self.client.post("/api/routes/plan", headers=host, json={
            "origin": "uni_mall", "destination": "phagwara_station", "departure_time": departure_in(5),
            "total_seats": 1, "price_per_seat": 100}).get_json()["route_id"]
        join = self.client.post(f"/api/routes/{route_id}/join", headers=rider, json={"seats": 1})
        self.assertEqual(join.status_code, 200)
        self.assertTrue(join.get_json()["is_pinned"])
        self.assertEqual(self.client.post(f"/api/routes/{route_id}/start", headers=host).status_code, 200)
        done = self.client.post(f"/api/routes/{route_id}/complete", headers=host)
        self.assertEqual(done.get_json()["driver_payout"], 90.0)
        self.assertEqual(self.client.post(f"/api/routes/{route_id}/complete", headers=host).status_code, 409)


if __name__ == "__main__":
    unittest.main()
