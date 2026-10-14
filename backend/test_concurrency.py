"""Many requests at the same moment must not oversell seats or overspend a wallet.
Runs on SQLite by default and on PostgreSQL with TEST_DATABASE_URL (see test_support)."""
import secrets
import threading
import time
import unittest

from test_support import ClientMixin, departure_in
from app import app, SESSION_SERIALIZER


class TestConcurrentWrites(ClientMixin, unittest.TestCase):
    RIDERS = 12

    def make_riders(self, balance):
        """Fresh riders with a known balance, signed in without the SMS flow."""
        now = time.time()
        headers = []
        for _ in range(self.RIDERS):
            tag = secrets.token_hex(6)
            uid = f"usr_conc_{tag}"
            self.db("""INSERT INTO users (id, lpu_id, name, email, phone, user_type, role, password_hash, is_verified,
                       is_teacher_priority, created_at, username, phone_key)
                       VALUES (?, ?, 'Conc Rider', '', '+916000000000', 'student', 'rider', '', 1, 0, ?, ?, ?)""",
                    (uid, f"C{tag}", now, f"conc{tag}", f"c{tag}"))
            self.db("INSERT INTO wallets (user_id, balance, currency, updated_at) VALUES (?, ?, 'INR', ?)", (uid, balance, now))
            headers.append({"Authorization": f"Bearer {SESSION_SERIALIZER.dumps({'u': uid, 'v': 0})}"})
        return headers

    def burst(self, calls):
        """Runs every call at once, each on its own client, and returns the status codes."""
        gate = threading.Barrier(len(calls))
        codes = [None] * len(calls)

        def run(i, fn):
            client = app.test_client()
            gate.wait()
            codes[i] = fn(client).status_code

        threads = [threading.Thread(target=run, args=(i, fn)) for i, fn in enumerate(calls)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return codes

    def test_seats_are_never_oversold(self):
        host = self.auth_headers("usr_driver_harpreet")
        route_id = self.client.post("/api/routes/plan", headers=host, json={
            "origin": "uni_mall", "destination": "phagwara_station", "departure_time": departure_in(),
            "total_seats": 3, "price_per_seat": 50}).get_json()["route_id"]
        self.addCleanup(self.client.post, f"/api/routes/{route_id}/cancel", headers=host)
        riders = self.make_riders(500)
        codes = self.burst([lambda c, h=h: c.post(f"/api/routes/{route_id}/join", headers=h, json={"seats": 1})
                            for h in riders])
        self.assertEqual(codes.count(200), 3, codes)
        seats = self.db("SELECT available_seats, status FROM driver_routes WHERE id = ?", (route_id,))[0]
        self.assertEqual((seats["available_seats"], seats["status"]), (0, "pinned"))
        booked = self.db("SELECT COUNT(*) AS n FROM route_bookings WHERE route_id = ? AND status = 'confirmed'", (route_id,))
        self.assertEqual(booked[0]["n"], 3)

    def test_a_wallet_is_never_overspent(self):
        host = self.auth_headers("usr_driver_harpreet")
        route_ids = [self.client.post("/api/routes/plan", headers=host, json={
            "origin": "uni_mall", "destination": "phagwara_station", "departure_time": departure_in(),
            "total_seats": 1, "price_per_seat": 40}).get_json()["route_id"] for _ in range(6)]
        for rid in route_ids:
            self.addCleanup(self.client.post, f"/api/routes/{rid}/cancel", headers=host)
        rider = self.make_riders(100)[0]   # enough for two ₹40 seats, not three
        codes = self.burst([lambda c, r=rid: c.post(f"/api/routes/{r}/join", headers=rider, json={"seats": 1})
                            for rid in route_ids])
        self.assertEqual(codes.count(200), 2, codes)
        self.assertEqual(self.balance(rider), 20.0)

    def test_one_ride_request_at_a_time(self):
        rider = self.make_riders(500)[0]
        trip = {"pickup_key": "uni_mall", "drop_key": "block_34", "service_type": "bike", "scope": "campus_hop"}
        codes = self.burst([lambda c: c.post("/api/rides/book", headers=rider, json=trip) for _ in range(8)])
        self.assertEqual(codes.count(200), 1, codes)
        self.assertEqual(self.balance(rider), 486.0)


if __name__ == "__main__":
    unittest.main()
