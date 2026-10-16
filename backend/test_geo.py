import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

from test_support import ClientMixin
import geo
import shared

PLACES = [
    {"name": "Sector 17 Plaza", "label": "Marketplace", "area": "Chandigarh", "importance": 0.6, "lat": 30.7333, "lng": 76.7794},
    {"name": "Sector 17 Delhi", "label": "Suburb", "area": "New Delhi", "importance": 0.9, "lat": 28.6139, "lng": 77.2090},  # too far
    {"name": "Jalandhar Bus Stand", "label": "Bus stand", "area": "Jalandhar", "importance": 0.8, "lat": 31.3190, "lng": 75.5860},
    {"name": "Jalandhar Bus Stand", "label": "Building", "area": "Jalandhar", "importance": 0.2, "lat": 31.3191, "lng": 75.5861},  # same place twice
    {"name": "Bus Stop Jalandhar Bypass", "label": "Bus stop", "area": "Jalandhar", "importance": 0.4, "lat": 31.29, "lng": 75.62},
]
CITY_TRIP = {"pickup_key": "uni_mall", "drop_key": "custom_place", "scope": "citylink", "service_type": "bike",
             "drop_lat": 30.7333, "drop_lng": 76.7794, "drop_name": "Sector 17 Plaza, Chandigarh"}


def use_places(test, places):
    """Points place search at a small index for the test's duration."""
    tmp = tempfile.TemporaryDirectory()
    test.addCleanup(tmp.cleanup)
    path = os.path.join(tmp.name, "places.db")
    db = sqlite3.connect(path)
    geo.write_places(db, places)
    db.close()
    patcher = mock.patch.object(geo, "PLACES_DB", path)
    patcher.start()
    test.addCleanup(patcher.stop)
    geo.close_places_db()  # connections to the previous index
    test.addCleanup(geo.close_places_db)  # runs first: Windows can't delete an open file


class TestPlaceSearch(ClientMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        shared.reset_memory()
        use_places(self, PLACES)

    def test_search_filters_to_service_area_and_ranks_by_match(self):
        places = geo.search_places("jalandhar bus")
        names = [p["name"] for p in places]
        self.assertEqual(names[0], "Jalandhar Bus Stand")
        self.assertEqual(names.count("Jalandhar Bus Stand"), 1)
        self.assertEqual(places[0]["detail"], "Bus stand, Jalandhar")
        self.assertNotIn("Sector 17 Delhi", [p["name"] for p in geo.search_places("sector 17")])

    def test_words_match_by_prefix_in_any_order(self):
        self.assertEqual(geo.search_places("bus jal")[0]["name"], "Jalandhar Bus Stand")
        self.assertEqual(geo.search_places("sect 17")[0]["name"], "Sector 17 Plaza")

    def test_names_typed_without_spaces(self):
        self.assertEqual(geo.search_places("jalandharbus")[0]["name"], "Jalandhar Bus Stand")

    def test_short_or_odd_queries_return_nothing(self):
        self.assertEqual(geo.search_places("ab"), [])
        self.assertEqual(geo.search_places('""*'), [])
        self.assertEqual(geo.search_places("zzzzqqq"), [])

    def test_search_never_calls_an_outside_service(self):
        with mock.patch.object(geo, "_fetch_json", side_effect=AssertionError("network used")):
            self.assertTrue(geo.search_places("sector 17"))
            self.assertTrue(geo.search_places("sector 18") is not None)

    def test_endpoint_requires_login_and_returns_places(self):
        self.assertEqual(self.client.get("/api/places/search?q=sector").status_code, 401)
        headers = self.auth_headers("usr_student_aarav")
        res = self.client.get("/api/places/search?q=sector 17&lat=31.25&lng=75.7", headers=headers)
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.get_json()["places"])

    def test_missing_index_is_reported_not_leaked(self):
        headers = self.auth_headers("usr_student_aarav")
        with mock.patch.object(geo, "PLACES_DB", os.path.join(tempfile.gettempdir(), "no-such-places.db")):
            geo.close_places_db()
            res = self.client.get("/api/places/search?q=sector 17", headers=headers)
        self.assertEqual(res.status_code, 503)


class TestMyPlaces(unittest.TestCase):
    """maintenance/my_places.txt: hand-added societies the OpenStreetMap data doesn't have."""

    def test_lines_are_read_and_bad_ones_skipped(self):
        try:
            sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "maintenance"))
            import build_places
        except ImportError:
            self.skipTest("osmium not installed (requirements-dev.txt)")
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "my_places.txt")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("\n".join([
                    "# a comment",
                    "# Switched Off | Mohali |",
                    "Green Valley | Phagwara | 31.25170, 75.69760",
                    "TDI City | Mohali | 30.7329,76.6753",
                    "Swapped | Mohali | 76.6753, 30.7329",
                    "No Numbers | Mohali |",
                    "just a name",
                ]))
            with mock.patch("builtins.print"):
                places = build_places.read_my_places(path)
        self.assertEqual([p["name"] for p in places], ["Green Valley", "TDI City"])
        self.assertEqual((places[1]["lat"], places[1]["lng"], places[1]["area"]), (30.7329, 76.6753, "Mohali"))


class TestRoutes(ClientMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        shared.reset_memory()

    def test_estimated_route_without_a_routing_server(self):
        headers = self.auth_headers("usr_student_aarav")
        with mock.patch.dict(os.environ, {"OSRM_URL": ""}),                 mock.patch.object(geo, "_fetch_json", side_effect=AssertionError("network used")):
            res = self.client.get("/api/route?a_lat=31.25&a_lng=75.7&b_lat=31.3&b_lng=75.6", headers=headers).get_json()
        self.assertTrue(res["estimated"])
        self.assertEqual(res["points"], [[31.25, 75.7], [31.3, 75.6]])
        straight = geo.haversine_distance_km(31.25, 75.7, 31.3, 75.6)
        self.assertAlmostEqual(res["km"], round(straight * geo.ROAD_FACTOR, 1))
        self.assertGreaterEqual(res["minutes"], 1)

    def test_road_route_from_a_routing_server(self):
        osrm = {"routes": [{"distance": 12500, "duration": 1500, "geometry": {"coordinates": [[75.7, 31.25], [75.6, 31.3]]}}]}
        headers = self.auth_headers("usr_student_aarav")
        with mock.patch.dict(os.environ, {"OSRM_URL": "https://osrm.example"}),                 mock.patch.object(geo, "_fetch_json", return_value=osrm):
            res = self.client.get("/api/route?a_lat=31.25&a_lng=75.7&b_lat=31.3&b_lng=75.6", headers=headers).get_json()
        self.assertEqual(res["km"], 12.5)
        self.assertEqual(res["minutes"], 25)
        self.assertEqual(res["points"][0], [31.25, 75.7])
        self.assertNotIn("estimated", res)

    def test_routing_server_failure_falls_back_to_estimate(self):
        headers = self.auth_headers("usr_student_aarav")
        with mock.patch.dict(os.environ, {"OSRM_URL": "https://osrm.example"}),                 mock.patch.object(geo, "_fetch_json", side_effect=geo.GeoError("down")):
            res = self.client.get("/api/route?a_lat=31.25&a_lng=75.7&b_lat=31.3&b_lng=75.6", headers=headers)
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.get_json()["estimated"])

    def test_bad_route_points(self):
        headers = self.auth_headers("usr_student_aarav")
        bad = self.client.get("/api/route?a_lat=31.25&a_lng=75.7&b_lat=10&b_lng=10", headers=headers)
        self.assertEqual(bad.status_code, 503)
        for junk in ("nan", "inf", "999"):
            res = self.client.get(f"/api/route?a_lat={junk}&a_lng=75.7&b_lat=31.3&b_lng=75.6", headers=headers)
            self.assertIn(res.status_code, (400, 503))


class TestMapFiles(ClientMixin, unittest.TestCase):
    def test_map_files_are_served_in_pieces_and_nothing_else(self):
        res = self.client.get("/maps/area.pmtiles", headers={"Range": "bytes=0-126"})
        self.assertEqual(res.status_code, 206)
        self.assertEqual(len(res.data), 127)
        self.assertTrue(res.data.startswith(b"PMTiles"))
        res.close()
        self.assertEqual(self.client.get("/maps/../app.py").status_code, 404)
        self.assertEqual(self.client.get("/maps/other.pmtiles").status_code, 404)


class TestCustomDestination(ClientMixin, unittest.TestCase):
    def test_any_address_can_be_booked_on_citylink(self):
        headers = self.auth_headers("usr_student_aarav")
        self.topup(headers, 1000)
        quote = self.client.post("/api/rides/quote", headers=headers, json=CITY_TRIP)
        self.assertEqual(quote.status_code, 200, quote.get_json())
        res = self.client.post("/api/rides/book", headers=headers, json=CITY_TRIP)
        self.assertEqual(res.status_code, 200, res.get_json())
        ride = self.client.get("/api/rides/active", headers=headers).get_json()["active_ride"]
        self.assertEqual(ride["drop_name"], "Sector 17 Plaza, Chandigarh")
        self.client.post(f"/api/rides/{ride['id']}/cancel", headers=headers)

    def test_custom_destination_rules(self):
        headers = self.auth_headers("usr_student_aarav")
        post = lambda **kw: self.client.post("/api/rides/quote", headers=headers, json={**CITY_TRIP, **kw})
        self.assertEqual(post(scope="campus_hop").status_code, 400)             # CityLink only
        self.assertEqual(post(drop_lat=28.6, drop_lng=77.2).status_code, 400)  # outside the service area
        self.assertEqual(post(drop_lat=None).status_code, 400)                  # coordinates required
        self.assertEqual(post(drop_lat="x").status_code, 400)
        # Both ends on campus is not a CityLink trip.
        self.assertEqual(post(drop_lat=31.2562, drop_lng=75.7050).status_code, 400)

    def test_moved_pins_on_campus_keep_the_flat_fare(self):
        headers = self.auth_headers("usr_student_aarav")
        trip = {"pickup_key": "main_gate", "drop_key": "block_34", "scope": "campus_hop",
                "pickup_lat": 31.2514, "pickup_lng": 75.7071, "drop_lat": 31.2566, "drop_lng": 75.7046}
        res = self.client.post("/api/rides/quote", headers=headers, json=trip)
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertEqual(res.get_json()["quotes"]["bike"]["total_fare"], 14.0)
        # A Campus Hop pin dragged off campus would be a long trip at the flat fare.
        off_campus = self.client.post("/api/rides/quote", headers=headers, json={**trip, "drop_lat": 31.3190, "drop_lng": 75.5860})
        self.assertEqual(off_campus.status_code, 400)

    def test_moved_citylink_pin_is_priced_by_its_new_distance(self):
        headers = self.auth_headers("usr_student_aarav")
        trip = {"pickup_key": "uni_mall", "drop_key": "jalandhar_bus_stand", "scope": "citylink"}
        fare = lambda **kw: self.client.post("/api/rides/quote", headers=headers, json={**trip, **kw}).get_json()["quotes"]["car"]
        listed = fare()
        moved = fare(drop_lat=31.3400, drop_lng=75.5600, drop_name="Near Jalandhar City Bus Stand")
        self.assertGreater(moved["distance_km"], listed["distance_km"])
        self.assertGreater(moved["base_fare"], listed["base_fare"])
        # Only known places can be moved: a made-up key is rejected.
        self.assertEqual(self.client.post("/api/rides/quote", headers=headers,
                                          json={**trip, "pickup_key": "nowhere", "pickup_lat": 31.25, "pickup_lng": 75.70}).status_code, 400)

    def test_destination_name_is_sanitised(self):
        points, error = __import__("pricing_and_queue").custom_point(30.7333, 76.7794, "<b>Hi</b>\x00\n there" + "x" * 300)
        self.assertIsNone(error)
        self.assertNotIn("<", points["name"])
        self.assertNotIn("\x00", points["name"])
        self.assertLessEqual(len(points["name"]), 120)

    def test_map_service_url_must_be_http(self):
        with mock.patch.object(geo.urllib.request, "urlopen", side_effect=AssertionError("file opened")):
            with self.assertRaises(geo.GeoError):
                geo._fetch_json("file:///etc/passwd")
            with mock.patch.dict("os.environ", {"OSRM_URL": "file:///etc/passwd"}):
                self.assertTrue(geo.road_route(31.25, 75.70, 31.30, 75.60)["estimated"])


if __name__ == "__main__":
    unittest.main()
