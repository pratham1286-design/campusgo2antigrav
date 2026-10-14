"""Builds backend/places.db: the place-search index for the service area, from OpenStreetMap.

Run it every few months to pick up new places (needs `pip install -r requirements-dev.txt`):

    python maintenance/build_places.py

It downloads Geofabrik's northern-India extract (~210 MB, kept in maintenance/.cache so a re-run
doesn't fetch it again) and keeps every named place inside the service area. The extract holds
India only, so places across the border never appear. The app only reads the finished file;
search never calls an outside service. The data is OpenStreetMap's (ODbL): the "(c) OpenStreetMap"
credit on every map covers it.
"""
import math
import os
import re
import sqlite3
import subprocess
import sys

import osmium

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "backend"))
from geo import write_places  # noqa: E402
from pricing_and_queue import LPU_CENTER, MAX_SERVICE_RADIUS_KM, haversine_distance_km  # noqa: E402

OUT = os.path.join(HERE, "..", "backend", "places.db")
MY_PLACES = os.path.join(HERE, "my_places.txt")
EXTRACT_URL = os.environ.get("OSM_EXTRACT_URL", "https://download.geofabrik.de/asia/india/northern-zone-latest.osm.pbf")
CACHE = os.path.join(HERE, ".cache")

PLACE_KINDS = {"city", "town", "village", "suburb", "neighbourhood", "quarter", "hamlet", "locality", "city_block"}
SOME_VALUES = {"railway": {"station", "halt"}, "highway": {"bus_stop"}, "public_transport": {"station"},
               "aeroway": {"aerodrome", "terminal"}, "landuse": {"residential", "commercial", "retail", "industrial"},
               "place": PLACE_KINDS, "barrier": {"gate", "lift_gate", "entrance"}, "man_made": {"ceremonial_gate"}}
# Named roads riders type as a destination ("Law Gate Road"); one point per road per ~1 km.
ROAD_TYPES = {"motorway", "trunk", "primary", "secondary", "tertiary", "unclassified", "residential",
              "living_street", "pedestrian"}
# Keys that only describe an object. A named point with nothing else (often a society, colony or
# PG someone added in a hurry) is still a place people go to.
DESCRIPTIVE = ("name", "alt_name", "old_name", "official_name", "short_name", "int_name", "loc_name", "addr:",
               "source", "note", "description", "wikidata", "wikipedia", "website", "phone", "contact:",
               "opening_hours", "operator", "email", "fixme", "image", "ref", "check_date", "created_by")

# How well known a kind of place usually is (0..1); used to rank equally good name matches.
IMPORTANCE = {
    "place=city": 1.0, "aeroway=aerodrome": 0.9, "place=town": 0.85, "railway=station": 0.8,
    "amenity=bus_station": 0.8, "amenity=university": 0.75, "amenity=college": 0.7, "amenity=hospital": 0.7,
    "shop=mall": 0.7, "tourism=attraction": 0.65, "historic": 0.6, "place=suburb": 0.6, "amenity=place_of_worship": 0.55,
    "place=village": 0.5, "public_transport=station": 0.5, "railway=halt": 0.5, "place=neighbourhood": 0.45,
    "place=quarter": 0.45, "tourism": 0.45, "leisure": 0.4, "amenity": 0.4, "highway=bus_stop": 0.4,
    "healthcare": 0.4, "place": 0.35, "landuse=residential": 0.45, "landuse": 0.3, "office": 0.3, "shop": 0.3,
    "barrier": 0.45, "entrance": 0.45, "man_made": 0.45, "residential": 0.45, "building": 0.2,
}
KINDS = ["place", "aeroway", "railway", "amenity", "public_transport", "shop", "tourism", "historic",
         "leisure", "healthcare", "highway", "office", "landuse", "residential", "barrier", "entrance", "man_made",
         "building"]
NICE = {"amenity=bus_station": "Bus stand", "amenity=place_of_worship": "Place of worship", "amenity=fuel": "Petrol pump",
        "aeroway=aerodrome": "Airport", "railway=station": "Railway station", "railway=halt": "Railway halt",
        "highway=bus_stop": "Bus stop", "public_transport=station": "Transit station", "building=yes": "Building",
        "landuse=residential": "Residential area", "place=city_block": "Block", "barrier=gate": "Gate",
        "barrier=lift_gate": "Gate", "barrier=entrance": "Gate", "man_made=ceremonial_gate": "Gate", "residential=gated": "Gated society",
        "tourism=apartment": "Apartment / PG"}
# Words riders type for a kind of place that its label doesn't contain.
SYNONYMS = {"Bus stand": "bus station adda isbt", "Railway station": "train junction", "Petrol pump": "fuel",
            "Hospital": "aspatal", "Airport": "flight"}


def download(url):
    """The extract, fetched once and resumed if interrupted (curl ships with Windows, macOS and Linux)."""
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, url.rsplit("/", 1)[1])
    if not os.path.exists(path + ".done"):
        print(f"Downloading {url} ...", flush=True)
        subprocess.run(["curl", "-fL", "--retry", "5", "-C", "-", "-o", path, url], check=True)
        open(path + ".done", "w").close()
    return path


def classify(tags):
    for kind in KINDS:
        value = tags.get(kind)
        if value and (kind not in SOME_VALUES or value in SOME_VALUES[kind]):
            weight = IMPORTANCE.get(f"{kind}={value}", IMPORTANCE.get(kind, 0.2))
            if kind == "entrance":
                return kind, "Gate", weight
            label = NICE.get(f"{kind}={value}") or value.replace("_", " ").capitalize()
            return kind, label, weight
    if all(k.startswith(DESCRIPTIVE) for k in tags):
        return "other", "Place", 0.3
    return None, None, 0


def read_my_places(path=MY_PLACES):
    """Our own places from my_places.txt ("Name | Town | lat, lng"); bad lines are reported, not fatal."""
    places = []
    if not os.path.exists(path):
        return places
    with open(path, encoding="utf-8") as fh:
        for number, line in enumerate(fh, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in line.split("|")]
            numbers = re.findall(r"-?\d+\.\d+", parts[2]) if len(parts) == 3 else []
            if len(parts) != 3 or not parts[0] or len(numbers) != 2:
                print(f"  my_places.txt line {number} skipped: write it as  Name | Town | 30.12345, 76.12345")
                continue
            lat, lng = float(numbers[0]), float(numbers[1])
            if haversine_distance_km(lat, lng, *LPU_CENTER) > MAX_SERVICE_RADIUS_KM:
                print(f"  my_places.txt line {number} skipped: {parts[0]} is outside the service area "
                      "(check the numbers aren't swapped)")
                continue
            places.append({"name": parts[0][:80], "alt": "", "kind": "mine", "label": "Society",
                           "importance": 0.7, "lat": round(lat, 6), "lng": round(lng, 6), "area": parts[1][:40]})
    return places


def road_point(way):
    """The middle of a named road, or None for anything that isn't a road riders would type."""
    if way.tags.get("highway") not in ROAD_TYPES:
        return None
    try:
        node = way.nodes[len(way.nodes) // 2]
        return (node.lat, node.lon) if node.location.valid() else None
    except osmium.InvalidLocationError:
        return None


def centre(obj):
    """A point for the object: the node itself, or the average of an area's outer ring."""
    if obj.is_node():
        return (obj.location.lat, obj.location.lon) if obj.location.valid() else None
    if obj.is_area():
        pts = [(n.lat, n.lon) for ring in obj.outer_rings() for n in ring if n.location.valid()]
        if pts:
            return sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)
    return None  # open ways and other relations: the same place is nearly always a node or area too


def main():
    source = sys.argv[1] if len(sys.argv) > 1 else download(EXTRACT_URL)
    lat_span = MAX_SERVICE_RADIUS_KM / 111.0
    lng_span = MAX_SERVICE_RADIUS_KM / (111.0 * math.cos(math.radians(LPU_CENTER[0])))
    south, north = LPU_CENTER[0] - lat_span, LPU_CENTER[0] + lat_span
    west, east = LPU_CENTER[1] - lng_span, LPU_CENTER[1] + lng_span

    print("Reading places (a few minutes) ...", flush=True)
    places, roads_seen = [], set()
    for obj in osmium.FileProcessor(source).with_areas().with_filter(osmium.filter.KeyFilter("name")):
        tags = dict(obj.tags)
        if obj.is_way():
            point = road_point(obj)
            kind, label, weight = "road", "Road", 0.25
            if point and (tags["name"].lower(), round(point[0], 2), round(point[1], 2)) in roads_seen:
                continue
        elif obj.is_node() or obj.is_area():
            if obj.is_area() and "highway" in tags:
                continue  # a closed road loop: already counted as a road
            kind, label, weight = classify(tags)
            if not kind:
                continue
            point = centre(obj)
        else:
            continue
        if not point or not (south <= point[0] <= north and west <= point[1] <= east):
            continue
        lat, lng = point
        if haversine_distance_km(lat, lng, *LPU_CENTER) > MAX_SERVICE_RADIUS_KM:
            continue
        if kind == "road":
            roads_seen.add((tags["name"].lower(), round(lat, 2), round(lng, 2)))
        name = (tags.get("name:en") or tags.get("name") or "").strip()[:80]
        # Other names people type: the local name, "Harmandir Sahib" for the Golden Temple, etc.
        others = {tags.get(k, "").strip() for k in ("name", "alt_name", "official_name", "old_name", "short_name",
                                                    "alt_name:en", "official_name:en")} - {"", name}
        if name:
            others.add(SYNONYMS.get(label, ""))
            places.append({"name": name, "alt": " ; ".join(sorted(others - {""}))[:200],
                           "kind": kind, "label": label, "importance": weight, "lat": round(lat, 6), "lng": round(lng, 6)})

    mine = read_my_places()
    print(f"{len(mine)} places from my_places.txt", flush=True)

    # Each place's subtitle names the nearest town or city, so "Bus stand" reads "Bus stand, Phagwara".
    towns = [p for p in places if p["kind"] == "place" and p["label"] in ("City", "Town")]
    cell = 0.25
    grid = {}
    for t in towns:
        grid.setdefault((int(t["lat"] / cell), int(t["lng"] / cell)), []).append(t)
    for p in places:
        ci, cj = int(p["lat"] / cell), int(p["lng"] / cell)
        near = [t for di in (-1, 0, 1) for dj in (-1, 0, 1) for t in grid.get((ci + di, cj + dj), []) if t is not p]
        best = min(near, key=lambda t: haversine_distance_km(p["lat"], p["lng"], t["lat"], t["lng"]), default=None)
        p["area"] = best["name"] if best and p not in towns else ""  # a town is its own area

    tmp = OUT + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    db = sqlite3.connect(tmp)
    write_places(db, places + mine)
    db.execute("INSERT INTO places_fts (places_fts) VALUES ('optimize')")
    db.commit()
    db.execute("VACUUM")
    db.close()
    os.replace(tmp, OUT)
    print(f"Wrote {len(places) + len(mine)} places to {os.path.normpath(OUT)} ({os.path.getsize(OUT) // 1024} KB)")


if __name__ == "__main__":
    main()
