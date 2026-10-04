"""Place search and road routes for the map, fetched server-side.

The browser never talks to the map services: the server does, so riders' IP addresses and
searches aren't shared with them, the page needs no extra CSP origins, and the service area
(200 km around LPU, as far as Chandigarh) is enforced here rather than trusted to the client.

Defaults use the public OpenStreetMap services, which are free but strictly rate limited
(Nominatim: 1 request/second, and no heavy use). For real traffic point NOMINATIM_URL and
OSRM_URL at a hosted or self-hosted instance.
"""
import difflib
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request

from pricing_and_queue import MAX_SERVICE_RADIUS_KM, haversine_distance_km

LPU = (31.2536, 75.7037)
# Bounding box that comfortably contains the service radius (about 2 degrees of latitude / 2.2 of longitude).
_LAT_SPAN = MAX_SERVICE_RADIUS_KM / 111.0
_LNG_SPAN = MAX_SERVICE_RADIUS_KM / (111.0 * 0.855)
VIEWBOX = (LPU[1] - _LNG_SPAN, LPU[0] + _LAT_SPAN, LPU[1] + _LNG_SPAN, LPU[0] - _LAT_SPAN)  # left, top, right, bottom

CACHE_TTL = 15 * 60
CACHE_MAX = 500
UPSTREAM_TIMEOUT = 6
MIN_UPSTREAM_GAP = 1.0   # Nominatim's policy: at most one request per second

_lock = threading.Lock()
_cache = {}
_last_upstream = 0.0


class GeoError(Exception):
    """A map-service problem that is safe to show to the user."""


def _env(name, default):
    return (os.environ.get(name) or default).strip().rstrip("/")


def _user_agent():
    contact = os.environ.get("GEOCODER_CONTACT", "").strip()
    return f"CampusGo/1.0 ({contact})" if contact else "CampusGo/1.0"


def _cache_get(key):
    with _lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < CACHE_TTL:
            return hit[1]
        _cache.pop(key, None)
    return None


def _cache_put(key, value):
    with _lock:
        if len(_cache) >= CACHE_MAX:
            for k in sorted(_cache, key=lambda k: _cache[k][0])[: CACHE_MAX // 5]:
                _cache.pop(k, None)
        _cache[key] = (time.time(), value)


def _fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": _user_agent(), "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=UPSTREAM_TIMEOUT) as resp:
            return json.loads(resp.read(512 * 1024).decode("utf-8"))
    except Exception as exc:
        raise GeoError("The map service is not responding. Please try again.") from exc


def clean_query(raw):
    text = re.sub(r"[\x00-\x1f\x7f]", " ", str(raw or ""))
    return " ".join(text.split())[:80]


def clean_label(raw, max_len=120):
    """A place name safe to store: no control characters, bounded length."""
    text = re.sub(r"[\x00-\x1f\x7f<>]", " ", str(raw or ""))
    return " ".join(text.split())[:max_len]


def _score(query, name, importance, near_km):
    """Higher is a better suggestion: how closely the name matches what was typed, how well
    known the place is, and how near it is to the rider (or to LPU when their position is unknown)."""
    q = query.lower()
    n = name.lower()
    similarity = difflib.SequenceMatcher(None, q, n[: max(len(q) + 10, 20)]).ratio()
    if n.startswith(q):
        similarity = max(similarity, 1.0)
    elif q in n:
        similarity = max(similarity, 0.85)
    proximity = max(0.0, 1.0 - near_km / MAX_SERVICE_RADIUS_KM)
    return 0.5 * similarity + 0.3 * min(1.0, float(importance or 0)) + 0.2 * proximity


def search_places(query, near=None, limit=6):
    """Suggestions for free-text destinations inside the service area, best match first."""
    query = clean_query(query)
    if len(query) < 3:
        return []
    anchor = near if near else LPU
    key = ("search", query.lower(), round(anchor[0], 1), round(anchor[1], 1))
    cached = _cache_get(key)
    if cached is not None:
        return cached

    global _last_upstream
    with _lock:
        now = time.time()
        if now - _last_upstream < MIN_UPSTREAM_GAP:
            raise GeoError("Searching too fast. Keep typing and suggestions will appear.")
        _last_upstream = now

    params = urllib.parse.urlencode({
        "q": query, "format": "jsonv2", "countrycodes": "in", "limit": 10, "dedupe": 1,
        "viewbox": ",".join(f"{v:.4f}" for v in VIEWBOX), "bounded": 1, "addressdetails": 0,
    })
    rows = _fetch_json(f"{_env('NOMINATIM_URL', 'https://nominatim.openstreetmap.org')}/search?{params}")
    if not isinstance(rows, list):
        raise GeoError("The map service returned an unexpected answer.")

    results = []
    for row in rows:
        try:
            lat, lng = float(row["lat"]), float(row["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        if haversine_distance_km(lat, lng, LPU[0], LPU[1]) > MAX_SERVICE_RADIUS_KM:
            continue
        full = clean_label(row.get("display_name"), 160)
        title = clean_label(row.get("name") or full.split(",")[0], 80)
        subtitle = clean_label(", ".join(full.split(",")[1:4]).strip(), 100)
        score = _score(query, title or full, row.get("importance"), haversine_distance_km(lat, lng, anchor[0], anchor[1]))
        results.append({"name": title or full, "detail": subtitle, "lat": round(lat, 6), "lng": round(lng, 6),
                        "label": full[:120], "_score": score})
    results.sort(key=lambda r: r["_score"], reverse=True)
    for r in results:
        r.pop("_score")
    results = results[:limit]
    _cache_put(key, results)
    return results


MAX_ROUTE_POINTS = 1500


def road_route(a_lat, a_lng, b_lat, b_lng):
    """Road geometry and timing between two points: {"points": [[lat, lng], ...], "km", "minutes"}."""
    for lat, lng in ((a_lat, a_lng), (b_lat, b_lng)):
        if not all(isinstance(v, (int, float)) and abs(v) < 1000 for v in (lat, lng)) or not (-90 <= lat <= 90 and -180 <= lng <= 180):
            raise GeoError("Invalid route points")
    for lat, lng in ((a_lat, a_lng), (b_lat, b_lng)):
        if haversine_distance_km(lat, lng, LPU[0], LPU[1]) > MAX_SERVICE_RADIUS_KM:
            raise GeoError("That point is outside our service area.")
    key = ("route", round(a_lat, 3), round(a_lng, 3), round(b_lat, 3), round(b_lng, 3))
    cached = _cache_get(key)
    if cached is not None:
        return cached

    base = _env("OSRM_URL", "https://router.project-osrm.org")
    url = (f"{base}/route/v1/driving/{a_lng:.5f},{a_lat:.5f};{b_lng:.5f},{b_lat:.5f}"
           "?overview=full&geometries=geojson")
    data = _fetch_json(url)
    try:
        route = data["routes"][0]
        coords = route["geometry"]["coordinates"]
        if len(coords) > MAX_ROUTE_POINTS:  # thin out evenly, keeping both ends, so the whole road stays drawn
            step = (len(coords) - 1) / (MAX_ROUTE_POINTS - 1)
            coords = [coords[round(i * step)] for i in range(MAX_ROUTE_POINTS)]
        points = [[round(lat, 5), round(lng, 5)] for lng, lat in coords]
        result = {"points": points, "km": round(route["distance"] / 1000.0, 1), "minutes": max(1, round(route["duration"] / 60.0))}
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise GeoError("No road route found between those points.") from exc
    _cache_put(key, result)
    return result


def point_along_route(points, fraction):
    """The [lat, lng] lying `fraction` (0..1) of the way along the road path, measured by distance."""
    if len(points) < 2 or fraction <= 0:
        return list(points[0])
    if fraction >= 1:
        return list(points[-1])
    legs = [haversine_distance_km(a[0], a[1], b[0], b[1]) for a, b in zip(points, points[1:])]
    goal = sum(legs) * fraction
    for (a, b), leg in zip(zip(points, points[1:]), legs):
        if goal <= leg and leg > 0:
            t = goal / leg
            return [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t]
        goal -= leg
    return list(points[-1])


def step_toward(cur_lat, cur_lng, target_lat, target_lng, fraction):
    """Demo vehicle movement: advance `fraction` of the remaining way along the real road path,
    falling back to a straight line only if the routing service is unavailable."""
    try:
        points = road_route(cur_lat, cur_lng, target_lat, target_lng)["points"]
    except GeoError:
        points = []
    if len(points) >= 2:
        return point_along_route(points, fraction)
    return [cur_lat + (target_lat - cur_lat) * fraction, cur_lng + (target_lng - cur_lng) * fraction]
