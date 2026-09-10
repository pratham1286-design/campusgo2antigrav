import math
import time
from datetime import datetime

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
    "jalandhar_cantt": {"name": "Jalandhar Cantt Railway Station", "lat": 31.2850, "lng": 75.6200, "zone": "CityLink-Jalandhar"}
}

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
    now = datetime.now()
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
    if service_type not in ('bike', 'scooty', 'car'):
        raise ValueError(f"Invalid service type: {service_type}")

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
