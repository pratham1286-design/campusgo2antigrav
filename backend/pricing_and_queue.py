import math
import os
import re
from datetime import datetime, timedelta, timezone

# Rush hours follow campus (India) time, not the server's clock, which is UTC on most hosts.
CAMPUS_TZ = timezone(timedelta(minutes=int(os.environ.get("LOCAL_UTC_OFFSET_MINUTES", "330") or 330)))

# LPU Campus Coordinates reference center: 31.2536° N, 75.7037° E
CAMPUS_LANDMARKS = {
    # Central Zone
    "uni_mall": {"name": "Uni-Mall & Student Plaza", "lat": 31.2535, "lng": 75.7038, "zone": "Zone-Central"},
    "central_library": {"name": "Central Library & Block 29", "lat": 31.2542, "lng": 75.7042, "zone": "Zone-Central"},
    "uni_hospital": {"name": "Uni-Hospital & Medical Centre", "lat": 31.2528, "lng": 75.7045, "zone": "Zone-Central"},
    "block_13_14": {"name": "Block 13-14 (Business & Law)", "lat": 31.2520, "lng": 75.7030, "zone": "Zone-Central"},
    
    # North Zone (Academic)
    "block_34": {"name": "Block 34 (Computer Science & Engg)", "lat": 31.2562, "lng": 75.7050, "zone": "Zone-North"},
    "block_36_38": {"name": "Block 36-38 (Polytechnic & Labs)", "lat": 31.2575, "lng": 75.7055, "zone": "Zone-North"},
    "tech_park": {"name": "RoboPark & Innovation Lab", "lat": 31.2580, "lng": 75.7040, "zone": "Zone-North"},

    # South Zone (Boys Hostels & Sports)
    "bh_1_2": {"name": "BH-1 & BH-2 (Boys Hostels)", "lat": 31.2495, "lng": 75.7020, "zone": "Zone-South"},
    "bh_3_4": {"name": "BH-3 & BH-4 (Boys Hostels)", "lat": 31.2480, "lng": 75.7015, "zone": "Zone-South"},
    "bh_5_8": {"name": "BH-5 to BH-8 (Mega Boys Hostel)", "lat": 31.2465, "lng": 75.7005, "zone": "Zone-South"},
    "sports_complex": {"name": "Indoor Stadium & Olympic Pool", "lat": 31.2490, "lng": 75.7050, "zone": "Zone-South"},

    # East Zone (Girls Hostels, Gates)
    "gh_1_3": {"name": "GH-1 & GH-3 (Girls Hostels)", "lat": 31.2548, "lng": 75.7085, "zone": "Zone-East"},
    "gh_4_6": {"name": "GH-4 to GH-6 (Girls Hostels)", "lat": 31.2560, "lng": 75.7095, "zone": "Zone-East"},
    "main_gate": {"name": "LPU Main Gate (GT Road Highway)", "lat": 31.2510, "lng": 75.7080, "zone": "Zone-East"},
    "law_gate": {"name": "Law Gate (PG Hub & Food Stalls)", "lat": 31.2585, "lng": 75.7110, "zone": "Zone-East"},

    # CityLink (Inter-city hubs)
    "phagwara_station": {"name": "Phagwara Railway Station", "lat": 31.2210, "lng": 75.7720, "zone": "CityLink-Phagwara"},
    "jalandhar_bus_stand": {"name": "Jalandhar City Bus Stand", "lat": 31.3190, "lng": 75.5860, "zone": "CityLink-Jalandhar"},
    "rama_mandi": {"name": "Rama Mandi Chowk, Jalandhar", "lat": 31.3050, "lng": 75.6410, "zone": "CityLink-RamaMandi"},
    "jalandhar_cantt": {"name": "Jalandhar Cantt Railway Station", "lat": 31.2850, "lng": 75.6200, "zone": "CityLink-Jalandhar"},
    "kapurthala": {"name": "Kapurthala Bus Stand", "lat": 31.3800, "lng": 75.3800, "zone": "CityLink-Kapurthala"},

    # CityLink: NH-44 corridor from Phagwara to Chandigarh (approximate coordinates)
    "goraya": {"name": "Goraya Bus Stop", "lat": 31.1236, "lng": 75.7727, "zone": "CityLink-Goraya"},
    "phillaur": {"name": "Phillaur Railway Station", "lat": 31.0197, "lng": 75.7867, "zone": "CityLink-Phillaur"},
    "ludhiana_bus_stand": {"name": "Ludhiana Bus Stand", "lat": 30.9130, "lng": 75.8580, "zone": "CityLink-Ludhiana"},
    "ludhiana_station": {"name": "Ludhiana Railway Station", "lat": 30.9127, "lng": 75.8490, "zone": "CityLink-Ludhiana"},
    "khanna": {"name": "Khanna Bus Stand", "lat": 30.7050, "lng": 76.2222, "zone": "CityLink-Khanna"},
    "sirhind": {"name": "Sirhind (Fatehgarh Sahib) Station", "lat": 30.6436, "lng": 76.3842, "zone": "CityLink-Sirhind"},
    "rajpura": {"name": "Rajpura Bus Stand", "lat": 30.4840, "lng": 76.5940, "zone": "CityLink-Rajpura"},
    "zirakpur": {"name": "Zirakpur Flyover Chowk", "lat": 30.6425, "lng": 76.8173, "zone": "CityLink-Zirakpur"},
    "kharar": {"name": "Kharar Bus Stand", "lat": 30.7460, "lng": 76.6450, "zone": "CityLink-Mohali"},
    "mohali": {"name": "Mohali Bus Stand (Phase 6)", "lat": 30.7046, "lng": 76.7179, "zone": "CityLink-Mohali"},
    "chandigarh_isbt_43": {"name": "Chandigarh ISBT Sector 43", "lat": 30.7224, "lng": 76.7493, "zone": "CityLink-Chandigarh"},
    "chandigarh_sector_17": {"name": "Chandigarh Sector 17 Plaza", "lat": 30.7410, "lng": 76.7821, "zone": "CityLink-Chandigarh"},
    "chandigarh_station": {"name": "Chandigarh Railway Station", "lat": 30.7026, "lng": 76.8210, "zone": "CityLink-Chandigarh"}
}

VALID_SCOPES = ("campus_hop", "citylink")
VALID_SERVICES = ("bike", "scooty", "car")

def is_city_landmark(landmark: dict) -> bool:
    return landmark["zone"].startswith("CityLink")

CURRENT_LOCATION_KEY = "current_location"
CUSTOM_PLACE_KEY = "custom_place"
MIN_TRIP_KM = 0.15
CAMPUS_RADIUS_KM = 2.0
MAX_SERVICE_RADIUS_KM = 200.0


def custom_point(lat, lng, name):
    """Builds a pickup or drop from real coordinates (GPS fix or a searched address).
    Inside the campus it takes the nearest landmark's zone; anywhere else it is a CityLink
    point. Returns (point, error)."""
    if lat is None or lng is None:
        return None, "That location is not available. Allow location access or choose a place."
    try:
        lat, lng = float(lat), float(lng)
    except (TypeError, ValueError):
        return None, "Invalid location"
    if lat != lat or lng != lng or not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return None, "Invalid location"
    if haversine_distance_km(lat, lng, 31.2536, 75.7037) > MAX_SERVICE_RADIUS_KM:
        return None, "That location is outside our service area."
    key, landmark = find_nearest_campus_landmark(lat, lng)
    if haversine_distance_km(lat, lng, landmark["lat"], landmark["lng"]) <= CAMPUS_RADIUS_KM and not is_city_landmark(landmark):
        zone = landmark["zone"]
    else:
        zone = "CityLink-Current"
    label = " ".join(re.sub(r"[\x00-\x1f\x7f<>]", " ", str(name or "")).split())[:120] or "Selected location"
    return {"name": label, "lat": lat, "lng": lng, "zone": zone}, None


def validate_trip(scope: str, pickup_key: str, drop_key: str, pickup_lat=None, pickup_lng=None,
                  drop_lat=None, drop_lng=None, drop_name=None):
    """Returns (pickup, drop, error). Campus Hop must stay on campus; CityLink
    must have at least one off-campus end, so flat campus pricing can't be
    used for inter-city trips."""
    if scope not in VALID_SCOPES:
        return None, None, "Invalid ride type"
    if pickup_key == CURRENT_LOCATION_KEY:
        pickup, error = custom_point(pickup_lat, pickup_lng, "My current location")
        if error:
            return None, None, error
    else:
        pickup = CAMPUS_LANDMARKS.get(pickup_key)
    if drop_key == CUSTOM_PLACE_KEY:
        if scope != "citylink":
            return None, None, "Searching for any address is available on CityLink only."
        drop, error = custom_point(drop_lat, drop_lng, drop_name)
        if error:
            return None, None, error
    else:
        drop = CAMPUS_LANDMARKS.get(drop_key)
    if not pickup or not drop:
        return None, None, "Invalid pickup or drop location selected"
    if pickup_key == drop_key and drop_key != CUSTOM_PLACE_KEY:
        return None, None, "Pickup and drop must be different locations"
    if haversine_distance_km(pickup["lat"], pickup["lng"], drop["lat"], drop["lng"]) < MIN_TRIP_KM:
        return None, None, "Pickup and drop are too close together"
    if scope == "campus_hop" and (is_city_landmark(pickup) or is_city_landmark(drop)):
        return None, None, "Campus Hop is for on-campus trips only. Use CityLink for off-campus destinations."
    if scope == "citylink" and not (is_city_landmark(pickup) or is_city_landmark(drop)):
        return None, None, "CityLink needs an off-campus pickup or drop. Use Campus Hop for on-campus trips."
    return pickup, drop, None

# Campus Hop Flat Pricing Rules
FLAT_CAMPUS_HOP = {
    "bike": 15.0,
    "scooty": 20.0,
    "car": 35.0
}

# CityLink Dynamic Base Rates
CITYLINK_PRICING = {
    "bike": {"base": 40.0, "per_km": 6.5},
    "scooty": {"base": 45.0, "per_km": 7.0},
    "car": {"base": 80.0, "per_km": 14.0}
}

def haversine_distance_km(lat1, lon1, lat2, lon2):
    """Calculate the great circle distance between two points in km."""
    R = 6371.0 # Earth radius in km
    dLat = math.radians(lat2 - lat1)
    dLon = math.radians(lon2 - lon1)
    a = math.sin(dLat / 2) * math.sin(dLat / 2) + \
        math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * \
        math.sin(dLon / 2) * math.sin(dLon / 2)
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c

def get_current_surge_multiplier(pickup_zone: str, demand_count: int = 0) -> float:
    """
    Calculates dynamic surge multiplier based on class timetable peak rush
    and real-time demand in the zone.
    """
    now = datetime.now(CAMPUS_TZ)
    hour = now.hour
    minute = now.minute
    total_minutes = hour * 60 + minute

    # Peak campus rush windows:
    # 08:30 - 09:15 (Morning classes): 510 to 555
    # 12:45 - 13:30 (Lunch / Afternoon block swap): 765 to 810
    # 16:45 - 17:45 (Evening exodus): 1005 to 1065
    is_time_rush = (510 <= total_minutes <= 555) or \
                   (765 <= total_minutes <= 810) or \
                   (1005 <= total_minutes <= 1065)

    base_surge = 1.25 if is_time_rush else 1.0

    # Demand load addition
    if demand_count > 5:
        base_surge += 0.20
    elif demand_count > 2:
        base_surge += 0.10

    return min(1.50, round(base_surge, 2))

def calculate_fare(service_type: str, scope: str, pickup_lat: float, pickup_lng: float, drop_lat: float, drop_lng: float, pickup_zone: str, demand_count: int = 0):
    """
    100% Server-side validated fare calculation.
    - Campus Hop: strictly flat rate.
    - CityLink: base + distance + dynamic surge.
    """
    if service_type not in VALID_SERVICES:
        raise ValueError(f"Invalid service type: {service_type}")
    if scope not in VALID_SCOPES:
        raise ValueError(f"Invalid scope: {scope}")

    dist_km = haversine_distance_km(pickup_lat, pickup_lng, drop_lat, drop_lng)

    if scope == "campus_hop":
        # Intra-campus is flat rate
        base_fare = FLAT_CAMPUS_HOP[service_type]
        surge = 1.0 # Flat rate for campus mobility
        total_fare = base_fare
        est_minutes = max(3, int(dist_km * 4) + 2)
    else:
        # CityLink dynamic commute
        pricing = CITYLINK_PRICING[service_type]
        surge = get_current_surge_multiplier(pickup_zone, demand_count)
        base_fare = pricing["base"] + (pricing["per_km"] * dist_km)
        total_fare = round(base_fare * surge, 2)
        est_minutes = max(10, int(dist_km * 2.5) + 5)

    return {
        "service_type": service_type,
        "scope": scope,
        "distance_km": round(dist_km, 2),
        "base_fare": base_fare,
        "surge_multiplier": surge,
        "total_fare": total_fare,
        "estimated_minutes": est_minutes
    }

def find_nearest_campus_landmark(lat: float, lng: float):
    """Auto-detects the closest LPU campus landmark given coordinates."""
    nearest_key = "uni_mall"
    min_dist = float("inf")
    for key, data in CAMPUS_LANDMARKS.items():
        dist = haversine_distance_km(lat, lng, data["lat"], data["lng"])
        if dist < min_dist:
            min_dist = dist
            nearest_key = key
    return nearest_key, CAMPUS_LANDMARKS[nearest_key]
