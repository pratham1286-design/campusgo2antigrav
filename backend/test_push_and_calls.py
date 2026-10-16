"""Push notifications (encrypted and signed for real, checked with an independent decoder) and
in-app calls between the two sides of a ride or carpool."""
import base64
import json
import os
import unittest
from unittest import mock

from test_support import PUSH_OUTBOX, ClientMixin, departure_in
import http_ece
import push
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

TRIP = {"pickup_key": "uni_mall", "drop_key": "block_34", "service_type": "bike", "scope": "campus_hop"}
UNI_MALL = {"lat": 31.2535, "lng": 75.7038}
OFFER = "v=0\r\no=- 1 2 IN IP4 127.0.0.1\r\ns=-\r\n"
ANSWER = "v=0\r\no=- 3 4 IN IP4 127.0.0.1\r\ns=-\r\n"


class Phone:
    """A browser's push subscription: its own key pair and auth secret."""
    count = 0

    def __init__(self):
        Phone.count += 1
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.auth = os.urandom(16)
        self.endpoint = f"https://fcm.googleapis.com/fcm/send/test-{Phone.count}"

    def subscription(self):
        raw = self.key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        return {"endpoint": self.endpoint, "keys": {"p256dh": push.b64url(raw), "auth": push.b64url(self.auth)}}

    def received(self):
        """Every notification sent to this phone, decrypted with http_ece (not our own code)."""
        return [json.loads(http_ece.decrypt(body, private_key=self.key, auth_secret=self.auth, version="aes128gcm"))
                for endpoint, body, _ in PUSH_OUTBOX if endpoint == self.endpoint]

    def titles(self):
        return [m["title"] for m in self.received()]


class PushCase(ClientMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        PUSH_OUTBOX.clear()
        self.db("UPDATE rides SET status = 'cancelled' WHERE status IN ('queued', 'matched', 'arriving', 'in_progress')")
        self.db("UPDATE driver_routes SET status = 'cancelled' WHERE status IN ('open', 'pinned', 'in_progress')")
        self.db("UPDATE calls SET status = 'ended' WHERE status IN ('ringing', 'connected')")
        self.db("DELETE FROM push_subscriptions")
        self.db("UPDATE wallets SET balance = 500 WHERE user_id IN ('usr_student_aarav', 'usr_student_kavya')")
        self.rider = self.auth_headers("usr_student_aarav")
        self.driver = self.auth_headers("usr_driver_vikram")
        self.client.post("/api/driver/toggle-online", headers=self.driver, json={"is_online": True, **UNI_MALL})

    def subscribe(self, headers):
        phone = Phone()
        res = self.client.post("/api/push/subscribe", headers=headers, json=phone.subscription())
        self.assertEqual(res.status_code, 200, res.get_json())
        return phone

    def booked_and_accepted(self):
        ride_id = self.client.post("/api/rides/book", headers=self.rider, json=TRIP).get_json()["ride_id"]
        res = self.client.post("/api/driver/accept", headers=self.driver, json={"ride_id": ride_id})
        self.assertEqual(res.status_code, 200, res.get_json())
        return ride_id


class TestPushNotifications(PushCase):
    def test_messages_are_encrypted_for_the_phone_and_signed_by_the_server(self):
        phone = self.subscribe(self.rider)
        self.booked_and_accepted()
        [message] = phone.received()
        self.assertEqual(message["title"], "Your driver is on the way")
        self.assertIn("Vikram", message["body"])
        self.assertEqual(message["url"], "/?open=ride")
        [(_, _, headers)] = [p for p in PUSH_OUTBOX if p[0] == phone.endpoint]
        self.assertEqual(headers["Content-Encoding"], "aes128gcm")
        # The VAPID token is a valid ES256 signature by the key the app subscribes with.
        token, key = headers["Authorization"].removeprefix("vapid t=").split(", k=")
        self.assertEqual(key, self.client.get("/api/config").get_json()["push_public_key"])
        head, claims, signature = token.split(".")
        public = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), push.b64url_decode(key))
        raw = push.b64url_decode(signature)
        der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
        public.verify(der, f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))
        with self.assertRaises(InvalidSignature):
            public.verify(der, f"{head}.{claims}x".encode(), ec.ECDSA(hashes.SHA256()))
        self.assertEqual(json.loads(push.b64url_decode(claims))["aud"], "https://fcm.googleapis.com")

    def test_each_side_hears_about_the_ride(self):
        rider_phone, driver_phone = self.subscribe(self.rider), self.subscribe(self.driver)
        ride_id = self.client.post("/api/rides/book", headers=self.rider, json=TRIP).get_json()["ride_id"]
        self.assertEqual(driver_phone.titles(), ["New bike ride request"])
        self.assertIn("You earn ₹13", driver_phone.received()[0]["body"])
        self.client.post("/api/driver/accept", headers=self.driver, json={"ride_id": ride_id})
        self.client.post("/api/driver/location", headers=self.driver, json=UNI_MALL)   # at the pickup
        self.client.post("/api/driver/location", headers=self.driver, json=UNI_MALL)   # told only once
        pin = self.client.get("/api/rides/active", headers=self.rider).get_json()["active_ride"]["start_pin"]
        self.client.post(f"/api/rides/{ride_id}/start", headers=self.driver, json={"pin": pin})
        self.client.post(f"/api/rides/{ride_id}/complete", headers=self.rider)
        self.assertEqual(rider_phone.titles(), ["Your driver is on the way", "Your driver is here", "Your ride has started"])
        self.assertEqual(driver_phone.titles(), ["New bike ride request", "Ride completed"])

    def test_cancelling_tells_the_driver(self):
        driver_phone = self.subscribe(self.driver)
        ride_id = self.booked_and_accepted()
        self.client.post(f"/api/rides/{ride_id}/cancel", headers=self.rider)
        self.assertEqual(driver_phone.titles()[-1], "Ride cancelled")

    def test_carpool_host_and_passengers_are_told(self):
        host = self.auth_headers("usr_driver_harpreet")
        host_phone, rider_phone = self.subscribe(host), self.subscribe(self.rider)
        route_id = self.client.post("/api/routes/plan", headers=host, json={
            "origin": "uni_mall", "destination": "phagwara_station", "departure_time": departure_in(),
            "total_seats": 2, "price_per_seat": 50}).get_json()["route_id"]
        self.client.post(f"/api/routes/{route_id}/join", headers=self.rider, json={"seats": 1})
        self.assertEqual(host_phone.titles(), ["New carpool booking"])
        self.client.post(f"/api/routes/{route_id}/cancel", headers=host)
        self.assertEqual(rider_phone.titles(), ["Carpool cancelled"])
        self.assertIn("refunded", rider_phone.received()[0]["body"])

    def test_only_real_push_services_are_accepted(self):
        good = Phone().subscription()
        for endpoint in ("https://169.254.169.254/latest", "http://fcm.googleapis.com/fcm/send/x",
                         "https://fcm.googleapis.com.evil.example/x", "https://evil.example/.push.apple.com"):
            res = self.client.post("/api/push/subscribe", headers=self.rider, json={**good, "endpoint": endpoint})
            self.assertEqual(res.status_code, 400, endpoint)
        bad_keys = {**good, "keys": {"p256dh": "short", "auth": good["keys"]["auth"]}}
        self.assertEqual(self.client.post("/api/push/subscribe", headers=self.rider, json=bad_keys).status_code, 400)

    def test_a_phone_that_uninstalled_is_forgotten(self):
        self.subscribe(self.rider)
        with mock.patch.object(push, "_post", return_value=410):
            self.booked_and_accepted()
        self.assertEqual(self.db("SELECT COUNT(*) AS n FROM push_subscriptions")[0]["n"], 0)

    def test_signing_out_stops_notifications(self):
        phone = self.subscribe(self.rider)
        self.client.post("/api/auth/logout", headers=self.rider)
        self.rider = self.auth_headers("usr_student_aarav")
        self.booked_and_accepted()
        self.assertEqual(phone.received(), [])


class TestInAppCalls(PushCase):
    def call(self, headers, **body):
        return self.client.post("/api/calls", headers=headers, json={"offer": OFFER, **body})

    def test_rider_and_driver_can_call_without_seeing_numbers(self):
        driver_phone = self.subscribe(self.driver)
        ride_id = self.booked_and_accepted()
        active = self.client.get("/api/rides/active", headers=self.rider).get_json()["active_ride"]
        self.assertNotIn("driver_phone", active)
        self.assertNotIn("rider_phone", self.client.get("/api/rides/active", headers=self.driver).get_json()["active_ride"])

        res = self.call(self.rider, context_type="ride", context_id=ride_id)
        self.assertEqual(res.status_code, 200, res.get_json())
        call_id = res.get_json()["call_id"]
        self.assertEqual(res.get_json()["callee_name"], "Vikram")
        self.assertEqual(driver_phone.titles()[-1], "Incoming call")
        self.assertEqual(driver_phone.received()[-1]["url"], "/?open=call")

        incoming = self.client.get("/api/calls/incoming", headers=self.driver).get_json()["call"]
        self.assertEqual((incoming["id"], incoming["offer"], incoming["caller_role"]), (call_id, OFFER, "your rider"))
        self.assertIsNone(self.client.get("/api/calls/incoming", headers=self.rider).get_json()["call"])

        self.assertEqual(self.client.post(f"/api/calls/{call_id}/answer", headers=self.driver, json={"answer": ANSWER}).status_code, 200)
        status = self.client.get(f"/api/calls/{call_id}", headers=self.rider).get_json()
        self.assertEqual((status["status"], status["answer"]), ("connected", ANSWER))
        self.client.post(f"/api/calls/{call_id}/end", headers=self.driver)
        self.assertEqual(self.client.get(f"/api/calls/{call_id}", headers=self.rider).get_json()["status"], "ended")

    def test_who_can_call(self):
        ride_id = self.client.post("/api/rides/book", headers=self.rider, json=TRIP).get_json()["ride_id"]
        self.assertEqual(self.call(self.rider, context_type="ride", context_id=ride_id).get_json()["code"], "CALL_NOT_AVAILABLE")
        self.client.post("/api/driver/accept", headers=self.driver, json={"ride_id": ride_id})
        stranger = self.auth_headers("usr_student_kavya")
        self.assertEqual(self.call(stranger, context_type="ride", context_id=ride_id).status_code, 403)
        self.assertEqual(self.call(self.rider, context_type="ride", context_id=ride_id, offer="hello").status_code, 400)
        call_id = self.call(self.rider, context_type="ride", context_id=ride_id).get_json()["call_id"]
        self.assertEqual(self.client.get(f"/api/calls/{call_id}", headers=stranger).status_code, 404)
        self.assertEqual(self.client.post(f"/api/calls/{call_id}/answer", headers=self.rider, json={"answer": ANSWER}).status_code, 403)

    def test_unanswered_declined_and_cancelled_calls(self):
        driver_phone = self.subscribe(self.driver)
        ride_id = self.booked_and_accepted()
        # Nobody answers within 45 seconds: a missed call, announced once.
        call_id = self.call(self.rider, context_type="ride", context_id=ride_id).get_json()["call_id"]
        self.db("UPDATE calls SET created_at = created_at - 60 WHERE id = ?", (call_id,))
        self.assertIsNone(self.client.get("/api/calls/incoming", headers=self.driver).get_json()["call"])
        self.assertEqual(self.client.get(f"/api/calls/{call_id}", headers=self.rider).get_json()["status"], "missed")
        self.assertEqual(driver_phone.titles().count("Missed call"), 1)
        # The callee declines.
        call_id = self.call(self.rider, context_type="ride", context_id=ride_id).get_json()["call_id"]
        self.assertEqual(self.client.post(f"/api/calls/{call_id}/end", headers=self.driver).get_json()["status"], "declined")
        # The caller gives up before an answer: the callee sees a missed call.
        call_id = self.call(self.rider, context_type="ride", context_id=ride_id).get_json()["call_id"]
        self.assertEqual(self.client.post(f"/api/calls/{call_id}/end", headers=self.rider).get_json()["status"], "missed")
        self.assertEqual(driver_phone.titles().count("Missed call"), 2)
        self.assertEqual(self.client.post(f"/api/calls/{call_id}/answer", headers=self.driver,
                                          json={"answer": ANSWER}).get_json()["code"], "CALL_ENDED")

    def test_busy_person_cannot_be_called_by_someone_else(self):
        ride_id = self.booked_and_accepted()
        call_id = self.call(self.rider, context_type="ride", context_id=ride_id).get_json()["call_id"]
        self.client.post(f"/api/calls/{call_id}/answer", headers=self.driver, json={"answer": ANSWER})
        # Mid-call, the rider can't be rung by the host of a carpool they also booked.
        host = self.auth_headers("usr_driver_harpreet")
        route_id = self.client.post("/api/routes/plan", headers=host, json={
            "origin": "uni_mall", "destination": "phagwara_station", "departure_time": departure_in(),
            "total_seats": 2, "price_per_seat": 50}).get_json()["route_id"]
        booking_id = self.client.post(f"/api/routes/{route_id}/join", headers=self.rider, json={"seats": 1}).get_json()["booking_id"]
        busy = self.call(host, context_type="route", context_id=route_id, booking_id=booking_id)
        self.assertEqual(busy.get_json()["code"], "CALLEE_BUSY")

    def test_carpool_host_and_passenger_can_call_each_other(self):
        host = self.auth_headers("usr_driver_harpreet")
        route_id = self.client.post("/api/routes/plan", headers=host, json={
            "origin": "uni_mall", "destination": "phagwara_station", "departure_time": departure_in(),
            "total_seats": 3, "price_per_seat": 50}).get_json()["route_id"]
        booking_id = self.client.post(f"/api/routes/{route_id}/join", headers=self.rider, json={"seats": 1}).get_json()["booking_id"]
        other = self.auth_headers("usr_student_kavya")
        self.client.post(f"/api/routes/{route_id}/join", headers=other, json={"seats": 1})

        to_host = self.call(self.rider, context_type="route", context_id=route_id)
        self.assertEqual(to_host.status_code, 200, to_host.get_json())
        self.client.post(f"/api/calls/{to_host.get_json()['call_id']}/end", headers=self.rider)
        incoming = self.client.get("/api/calls/incoming", headers=host).get_json()["call"]
        self.assertIsNone(incoming)  # already hung up

        to_passenger = self.call(host, context_type="route", context_id=route_id, booking_id=booking_id)
        self.assertEqual(to_passenger.status_code, 200, to_passenger.get_json())
        incoming = self.client.get("/api/calls/incoming", headers=self.rider).get_json()["call"]
        self.assertEqual(incoming["caller_role"], "your carpool host")
        # A host can only ring people booked on their own trip; outsiders can't ring the host.
        self.assertEqual(self.call(host, context_type="route", context_id=route_id, booking_id="bk_nope").status_code, 404)
        self.assertEqual(self.call(self.driver, context_type="route", context_id=route_id).status_code, 403)


if __name__ == "__main__":
    unittest.main()
