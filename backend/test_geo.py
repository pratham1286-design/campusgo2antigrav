import unittest
from unittest import mock

import geo
from test_support import ClientMixin

NOMINATIM_ROWS = [
    {"lat": "30.7333", "lon": "76.7794", "name": "Sector 17 Plaza", "display_name": "Sector 17 Plaza, Chandigarh, India", "importance": 0.6},
    {"lat": "28.6139", "lon": "77.2090", "name": "Sector 17 Delhi", "display_name": "Sector 17, New Delhi, India", "importance": 0.9},  # too far
    {"lat": "bad", "lon": "x", "name": "Broken"},
    {"lat": "31.3190", "lon": "75.5860", "name": "Jalandhar Bus Stand", "display_name": "Jalandhar Bus Stand, Jalandhar, Punjab, India", "importance": 0.4},
]
CITY_TRIP = {"pickup_key": "uni_mall", "drop_key": "custom_place", "scope": "citylink", "service_type": "bike",
             "drop_lat": 30.7333, "drop_lng": 76.7794, "drop_name": "Sector 17 Plaza, Chandigarh"}


class TestPlaceSearch(ClientMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        geo._cache.clear()
        geo._last_upstream = 0.0

    def test_search_filters_to_service_area_and_ranks_by_match(self):
        with mock.patch.object(geo, "_fetch_json", return_value=NOMINATIM_ROWS):
            places = geo.search_places("jalandhar bus")
        names = [p["name"] for p in places]
        self.assertEqual(names[0], "Jalandhar Bus Stand")
        self.assertNotIn("Sector 17 Delhi", names)
        self.assertNotIn("Broken", names)

    def test_short_queries_and_repeat_queries_do_not_hit_the_service(self):
        with mock.patch.object(geo, "_fetch_json", return_value=NOMINATIM_ROWS) as fetch:
            self.assertEqual(geo.search_places("ab"), [])
            geo.search_places("sector 17")
            geo.search_places("sector 17")
        self.assertEqual(fetch.call_count, 1)

    def test_upstream_rate_limit_is_respected(self):
        with mock.patch.object(geo, "_fetch_json", return_value=NOMINATIM_ROWS):
            geo.search_places("sector 17")
            with self.assertRaises(geo.GeoError):
                geo.search_places("sector 18")

    def test_endpoint_requires_login_and_returns_places(self):
        self.assertEqual(self.client.get("/api/places/search?q=sector").status_code, 401)
        headers = self.auth_headers("usr_student_aarav")
        with mock.patch.object(geo, "_fetch_json", return_value=NOMINATIM_ROWS):
            res = self.client.get("/api/places/search?q=sector 17&lat=31.25&lng=75.7", headers=headers)
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.get_json()["places"])

    def test_map_service_failure_is_reported_not_leaked(self):
        headers = self.auth_headers("usr_student_aarav")
        with mock.patch.object(geo, "_fetch_json", side_effect=geo.GeoError("down")):
            res = self.client.get("/api/places/search?q=sector 17", headers=headers)
        self.assertEqual(res.status_code, 503)

    def test_road_route(self):
        osrm = {"routes": [{"distance": 12500, "duration": 1500, "geometry": {"coordinates": [[75.7, 31.25], [75.6, 31.3]]}}]}
        headers = self.auth_headers("usr_student_aarav")
        with mock.patch.object(geo, "_fetch_json", return_value=osrm):
            res = self.client.get("/api/route?a_lat=31.25&a_lng=75.7&b_lat=31.3&b_lng=75.6", headers=headers).get_json()
        self.assertEqual(res["km"], 12.5)
        self.assertEqual(res["minutes"], 25)
        self.assertEqual(res["points"][0], [31.25, 75.7])
        bad = self.client.get("/api/route?a_lat=31.25&a_lng=75.7&b_lat=10&b_lng=10", headers=headers)
        self.assertEqual(bad.status_code, 503)
        for junk in ("nan", "inf", "999"):
            res = self.client.get(f"/api/route?a_lat={junk}&a_lng=75.7&b_lat=31.3&b_lng=75.6", headers=headers)
            self.assertIn(res.status_code, (400, 503))


class TestCustomDestination(ClientMixin, unittest.TestCase):
    def test_any_address_can_be_booked_on_citylink(self):
        headers = self.auth_headers("usr_student_aarav")
        self.topup_demo(headers, 1000)
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

    def test_destination_name_is_sanitised(self):
        points, error = __import__("pricing_and_queue").custom_point(30.7333, 76.7794, "<b>Hi</b>\x00\n there" + "x" * 300)
        self.assertIsNone(error)
        self.assertNotIn("<", points["name"])
        self.assertNotIn("\x00", points["name"])
        self.assertLessEqual(len(points["name"]), 120)


class TestRoadFollowing(unittest.TestCase):
    L_SHAPE = [[31.0, 75.0], [31.0, 75.1], [31.1, 75.1]]  # east, then north: a road with a corner

    def test_point_along_route_stays_on_the_path(self):
        east = geo.haversine_distance_km(31.0, 75.0, 31.0, 75.1)
        north = geo.haversine_distance_km(31.0, 75.1, 31.1, 75.1)
        corner = geo.point_along_route(self.L_SHAPE, east / (east + north))
        self.assertAlmostEqual(corner[0], 31.0, places=2)
        self.assertAlmostEqual(corner[1], 75.1, places=2)
        self.assertEqual(geo.point_along_route(self.L_SHAPE, 0), [31.0, 75.0])
        self.assertEqual(geo.point_along_route(self.L_SHAPE, 1), [31.1, 75.1])

    def test_demo_vehicle_follows_road_not_straight_line(self):
        route = {"points": self.L_SHAPE, "km": 20.0, "minutes": 30}
        with mock.patch.object(geo, "road_route", return_value=route):
            lat, lng = geo.step_toward(31.0, 75.0, 31.1, 75.1, 0.25)
        self.assertAlmostEqual(lat, 31.0, places=3)  # still on the eastbound leg, not cutting the corner
        self.assertGreater(lng, 75.0)

    def test_demo_vehicle_falls_back_to_straight_line_when_routing_is_down(self):
        with mock.patch.object(geo, "road_route", side_effect=geo.GeoError("down")):
            lat, lng = geo.step_toward(31.0, 75.0, 31.1, 75.1, 0.5)
        self.assertAlmostEqual(lat, 31.05)
        self.assertAlmostEqual(lng, 75.05)


if __name__ == "__main__":
    unittest.main()
