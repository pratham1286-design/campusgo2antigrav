"""Place search and trip routes for the map, all answered on our own server.

No outside map service is needed: search reads places.db, an index of the service area's
named places built from OpenStreetMap by maintenance/build_places.py, and routes are an
estimate from the straight-line distance. The service area (200 km around LPU, as far as
Chandigarh) is enforced here rather than trusted to the client.

Optional upgrade: point OSRM_URL at a self-hosted OSRM server and routes follow real roads,
falling back to the estimate whenever it doesn't answer.
"""
import difflib
import json
import os
import re
import sqlite3
import threading
import urllib.parse
import urllib.request

import shared
from pricing_and_queue import LPU_CENTER as LPU, MAX_SERVICE_RADIUS_KM, haversine_distance_km

PLACES_DB = os.environ.get("PLACES_DB") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "places.db")
ROUTE_CACHE_TTL = 60 * 60     # roads don't change by the hour
UPSTREAM_TIMEOUT = 4
ROAD_FACTOR = 1.3             # roads are rarely straight: typical road distance / straight-line distance
# A slow map service must never tie up every request thread: at most this many calls to it run
# at once per process, and a request that can't get a slot within a moment fails fast instead.
_upstream_slots = threading.BoundedSemaphore(int(os.environ.get("MAP_UPSTREAM_CONCURRENCY", "3") or 3))


class GeoError(Exception):
    """A map-service problem that is safe to show to the user."""


def _env(name, default):
    return (os.environ.get(name) or default).strip().rstrip("/")


def _user_agent():
    contact = os.environ.get("GEOCODER_CONTACT", "").strip()
    return f"CampusGo/1.0 ({contact})" if contact else "CampusGo/1.0"


def _cache_key(parts):
    return "geo:" + ":".join(str(p) for p in parts)


def _fetch_json(url):
    # OSRM_URL comes from the environment; never let one open a local file.
    if not url.startswith(("https://", "http://")):
        raise GeoError("The map service is not configured correctly.")
    if not _upstream_slots.acquire(timeout=0.5):
        raise GeoError("The map service is busy right now. Please try again in a moment.")
    req = urllib.request.Request(url, headers={"User-Agent": _user_agent(), "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=UPSTREAM_TIMEOUT) as resp:
            return json.loads(resp.read(512 * 1024).decode("utf-8"))
    except Exception as exc:
        raise GeoError("The map service is not responding. Please try again.") from exc
    finally:
        _upstream_slots.release()


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


PLACES_SCHEMA = """
    CREATE TABLE places (id INTEGER PRIMARY KEY, name TEXT, alt TEXT, label TEXT, area TEXT,
                         importance REAL, lat REAL, lng REAL);
    CREATE VIRTUAL TABLE places_fts USING fts5(name, alt, area, label, content='places', content_rowid='id',
                                               tokenize='unicode61 remove_diacritics 2');
"""


def _joined(name):
    """The name written as one word, so "lawgate" finds "Law Gate" and "greenvalley" "Green Valley"."""
    words = re.findall(r"\w+", name.lower())
    return "".join(words) if len(words) > 1 else ""


def write_places(db, places):
    """Fills an empty places index; places are dicts with the PLACES_SCHEMA columns."""
    db.executescript(PLACES_SCHEMA)
    db.executemany("INSERT INTO places (name, alt, label, area, importance, lat, lng) VALUES (?,?,?,?,?,?,?)",
                   [(p["name"], " ; ".join(x for x in (p.get("alt", ""), _joined(p["name"])) if x),
                     p.get("label", ""), p.get("area", ""), p.get("importance", 0.3), p["lat"], p["lng"]) for p in places])
    db.execute("INSERT INTO places_fts (rowid, name, alt, area, label) SELECT id, name, alt, area, label FROM places")
    db.commit()


_local = threading.local()  # one read-only connection per thread


def _places_db():
    db = getattr(_local, "db", None)
    if db is None:
        if not os.path.exists(PLACES_DB):
            raise GeoError("Place search is not set up yet.")
        db = sqlite3.connect(f"file:{urllib.parse.quote(os.path.abspath(PLACES_DB))}?mode=ro", uri=True,
                             check_same_thread=False)
        _local.db = db
    return db


def close_places_db():
    db = getattr(_local, "db", None)
    if db is not None:
        _local.db = None
        db.close()


def search_places(query, near=None, limit=6):
    """Suggestions for free-text destinations inside the service area, best match first."""
    query = clean_query(query)
    words = re.findall(r"\w+", query.lower())
    if len(query) < 3 or not words:
        return []
    anchor = near if near else LPU
    # Every typed word must start a word of the name, other names, town or kind of place:
    # "jal bus" finds "Jalandhar Bus Stand", "chandigarh bus stand" finds Chandigarh's bus stands.
    match = " ".join(f'"{w}"*' for w in words[:6])
    try:
        rows = _places_db().execute(
            "SELECT p.name, p.label, p.area, p.importance, p.lat, p.lng FROM places_fts f "
            "JOIN places p ON p.id = f.rowid WHERE places_fts MATCH ? "
            "ORDER BY bm25(places_fts, 10.0, 5.0, 1.0, 1.0) LIMIT 400", (match,)).fetchall()
    except sqlite3.Error as exc:
        raise GeoError("Place search is unavailable right now.") from exc

    results, seen = [], set()
    for name, label, area, importance, lat, lng in rows:
        if haversine_distance_km(lat, lng, LPU[0], LPU[1]) > MAX_SERVICE_RADIUS_KM:
            continue
        detail = ", ".join(x for x in (label, area if area and area != name else "") if x)
        score = _score(query, name, importance, haversine_distance_km(lat, lng, anchor[0], anchor[1]))
        results.append({"name": name, "detail": detail, "lat": lat, "lng": lng,
                        "label": clean_label(f"{name}, {area}" if area and area != name else name), "_score": score})
    results.sort(key=lambda r: r["_score"], reverse=True)
    picked = []
    for r in results:
        key = (r["name"].lower(), round(r["lat"], 2), round(r["lng"], 2))  # one pin per place, not per OSM object
        if key in seen:
            continue
        seen.add(key)
        r.pop("_score")
        picked.append(r)
        if len(picked) == limit:
            break
    return picked


MAX_ROUTE_POINTS = 1500


def road_route(a_lat, a_lng, b_lat, b_lng):
    """Road geometry and timing between two points: {"points": [[lat, lng], ...], "km", "minutes"}."""
    for lat, lng in ((a_lat, a_lng), (b_lat, b_lng)):
        if not all(isinstance(v, (int, float)) and abs(v) < 1000 for v in (lat, lng)) or not (-90 <= lat <= 90 and -180 <= lng <= 180):
            raise GeoError("Invalid route points")
    for lat, lng in ((a_lat, a_lng), (b_lat, b_lng)):
        if haversine_distance_km(lat, lng, LPU[0], LPU[1]) > MAX_SERVICE_RADIUS_KM:
            raise GeoError("That point is outside our service area.")
    key = _cache_key(("route", round(a_lat, 3), round(a_lng, 3), round(b_lat, 3), round(b_lng, 3)))
    cached = shared.cache_get(key)
    if cached is not None:
        return cached

    base = _env("OSRM_URL", "")
    if not base:
        return estimated_route(a_lat, a_lng, b_lat, b_lng)
    try:
        result = _osrm_route(base, a_lat, a_lng, b_lat, b_lng)
    except GeoError:
        return estimated_route(a_lat, a_lng, b_lat, b_lng)
    shared.cache_put(key, result, ROUTE_CACHE_TTL)
    return result


def estimated_route(a_lat, a_lng, b_lat, b_lng):
    """A straight line with road-like distance and time, for when no routing server is set up."""
    km = haversine_distance_km(a_lat, a_lng, b_lat, b_lng) * ROAD_FACTOR
    speed = 20 if km < 5 else 30 if km < 30 else 50  # km/h: campus lanes, city traffic, highway
    return {"points": [[round(a_lat, 5), round(a_lng, 5)], [round(b_lat, 5), round(b_lng, 5)]],
            "km": round(km, 1), "minutes": max(1, round(km / speed * 60)), "estimated": True}


def _osrm_route(base, a_lat, a_lng, b_lat, b_lng):
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
        return {"points": points, "km": round(route["distance"] / 1000.0, 1), "minutes": max(1, round(route["duration"] / 60.0))}
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise GeoError("No road route found between those points.") from exc
