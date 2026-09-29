import os
import sys
import time
import uuid
from werkzeug.security import generate_password_hash
from database import init_db, get_db_connection

# Shared password for every seeded demo persona. Override with DEMO_PASSWORD so
# a public deployment doesn't use the value printed in the README.
DEMO_PASSWORD = os.environ.get("DEMO_PASSWORD") or "CampusGo@2026"

def seed(if_empty=False):
    """Wipes all data and loads the demo personas. With if_empty=True it only
    seeds a brand-new database, so restarts and redeploys keep real data."""
    init_db()
    conn = get_db_connection()
    cur = conn.cursor()

    if if_empty and cur.execute("SELECT COUNT(*) FROM users").fetchone()[0] > 0:
        conn.close()
        print("Database already has users; skipping demo seed.")
        return

    # Clear existing demo data
    cur.execute("DELETE FROM sos_alerts;")
    cur.execute("DELETE FROM route_bookings;")
    cur.execute("DELETE FROM driver_routes;")
    cur.execute("DELETE FROM ride_reviews;")
    cur.execute("DELETE FROM rides;")
    cur.execute("DELETE FROM driver_locations;")
    cur.execute("DELETE FROM emergency_contacts;")
    cur.execute("DELETE FROM wallet_transactions;")
    cur.execute("DELETE FROM wallets;")
    cur.execute("DELETE FROM vehicles;")
    cur.execute("DELETE FROM users;")

    now = time.time()

    users = [
        # Teacher (Priority matching & badge)
        {
            "id": "usr_teacher_raman",
            "lpu_id": "FAC-10822",
            "name": "Dr. Raman Sharma",
            "email": "raman.sharma@lpu.in",
            "phone": "+91 98765 43210",
            "user_type": "teacher",
            "role": "rider",
            "is_verified": 1,
            "is_teacher_priority": 1,
            "department": "School of Computer Science & Engg",
            "avatar_url": "https://images.unsplash.com/photo-1534528741775-53994a69daeb?w=120&auto=format&fit=crop&q=80",
            "wallet_balance": 350.0
        },
        # Student Rider (Standard balance)
        {
            "id": "usr_student_aarav",
            "lpu_id": "12204592",
            "name": "Aarav Mehta",
            "email": "aarav.12204592@lpu.in",
            "phone": "+91 98123 45678",
            "user_type": "student",
            "role": "rider",
            "is_verified": 1,
            "is_teacher_priority": 0,
            "department": "B.Tech Computer Science (3rd Yr)",
            "avatar_url": "https://images.unsplash.com/photo-1539571696357-5a69c17a67c6?w=120&auto=format&fit=crop&q=80",
            "wallet_balance": 150.0
        },
        # Student Rider (Low Balance - to demonstrate prompt)
        {
            "id": "usr_student_kavya",
            "lpu_id": "12301982",
            "name": "Kavya Patel",
            "email": "kavya.12301982@lpu.in",
            "phone": "+91 98234 56789",
            "user_type": "student",
            "role": "rider",
            "is_verified": 1,
            "is_teacher_priority": 0,
            "department": "B.Des Fashion Design (2nd Yr)",
            "avatar_url": "https://images.unsplash.com/photo-1517841905240-472988babdf9?w=120&auto=format&fit=crop&q=80",
            "wallet_balance": 10.0 # Insufficient for rides
        },
        # Student Driver (Scooty)
        {
            "id": "usr_driver_simran",
            "lpu_id": "12108843",
            "name": "Simran Kaur",
            "email": "simran.12108843@lpu.in",
            "phone": "+91 98345 67890",
            "user_type": "student",
            "role": "both",
            "is_verified": 1,
            "is_teacher_priority": 0,
            "department": "B.Tech Electronics & Comm",
            "avatar_url": "https://images.unsplash.com/photo-1494790108377-be9c29b29330?w=120&auto=format&fit=crop&q=80",
            "wallet_balance": 480.0
        },
        # Student Driver (Bike)
        {
            "id": "usr_driver_vikram",
            "lpu_id": "12019934",
            "name": "Vikram Singh",
            "email": "vikram.12019934@lpu.in",
            "phone": "+91 98456 78901",
            "user_type": "student",
            "role": "driver",
            "is_verified": 1,
            "is_teacher_priority": 0,
            "department": "B.Tech Mechanical Engineering",
            "avatar_url": "https://images.unsplash.com/photo-1507003211169-0a1dd7228f2d?w=120&auto=format&fit=crop&q=80",
            "wallet_balance": 620.0
        },
        # Driver (Car)
        {
            "id": "usr_driver_harpreet",
            "lpu_id": "11904421",
            "name": "Harpreet Singh",
            "email": "harpreet.11904421@lpu.in",
            "phone": "+91 98567 89012",
            "user_type": "student",
            "role": "driver",
            "is_verified": 1,
            "is_teacher_priority": 0,
            "department": "MBA Logistics & Supply Chain",
            "avatar_url": "https://images.unsplash.com/photo-1500648767791-00dcc994a43e?w=120&auto=format&fit=crop&q=80",
            "wallet_balance": 920.0
        }
    ]

    demo_password_hash = generate_password_hash(DEMO_PASSWORD)

    for u in users:
        cur.execute("""
        INSERT INTO users (id, lpu_id, name, email, phone, user_type, role, password_hash, is_verified, is_teacher_priority, department, avatar_url, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """, (u["id"], u["lpu_id"], u["name"], u["email"], u["phone"], u["user_type"], u["role"], demo_password_hash, u["is_verified"], u["is_teacher_priority"], u["department"], u["avatar_url"], now))

        # Wallet
        cur.execute("""
        INSERT INTO wallets (user_id, balance, currency, updated_at)
        VALUES (?, ?, 'INR', ?);
        """, (u["id"], u["wallet_balance"], now))

        # Initial seed transaction
        cur.execute("""
        INSERT INTO wallet_transactions (id, user_id, amount, type, reference_id, description, balance_after, created_at)
        VALUES (?, ?, ?, 'topup', 'seed_initial', 'Initial Wallet Setup', ?, ?);
        """, (str(uuid.uuid4()), u["id"], u["wallet_balance"], u["wallet_balance"], now))

        # Mandatory emergency contacts
        cur.execute("""
        INSERT INTO emergency_contacts (id, user_id, name, relationship, phone, is_primary, created_at)
        VALUES (?, ?, ?, ?, ?, 1, ?);
        """, (str(uuid.uuid4()), u["id"], "LPU Campus Security Block-30", "Campus Security / Helpline", "+91 1824 517000", now))

        cur.execute("""
        INSERT INTO emergency_contacts (id, user_id, name, relationship, phone, is_primary, created_at)
        VALUES (?, ?, ?, ?, ?, 0, ?);
        """, (str(uuid.uuid4()), u["id"], "Guardian / Emergency Cell", "Parent / Guardian", "+91 99887 76655", now))

    # Seed Vehicles
    vehicles = [
        # Simran's Scooty
        {
            "id": "veh_simran_activa",
            "user_id": "usr_driver_simran",
            "category": "scooty",
            "model": "Honda Activa 6G",
            "plate_number": "PB08-DA-4821",
            "color": "Pearl White",
            "capacity": 1,
            "has_helmet": 1,
            "has_ac": 0
        },
        # Vikram's Bike
        {
            "id": "veh_vikram_re",
            "user_id": "usr_driver_vikram",
            "category": "bike",
            "model": "Royal Enfield Hunter 350",
            "plate_number": "PB09-AJ-7731",
            "color": "Dapper Grey",
            "capacity": 1,
            "has_helmet": 1,
            "has_ac": 0
        },
        # Harpreet's Car
        {
            "id": "veh_harpreet_dzire",
            "user_id": "usr_driver_harpreet",
            "category": "car",
            "model": "Maruti Suzuki Dzire VXi",
            "plate_number": "PB08-CR-9912",
            "color": "Silky Silver",
            "capacity": 3,
            "has_helmet": 0,
            "has_ac": 1
        }
    ]

    for v in vehicles:
        cur.execute("""
        INSERT INTO vehicles (id, user_id, category, model, plate_number, color, capacity, has_helmet, has_ac, is_active)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1);
        """, (v["id"], v["user_id"], v["category"], v["model"], v["plate_number"], v["color"], v["capacity"], v["has_helmet"], v["has_ac"]))

    # Seed Driver Locations (Live Telemetry base positions)
    locations = [
        # Simran at Uni-Mall
        {"driver_id": "usr_driver_simran", "lat": 31.2535, "lng": 75.7038, "zone": "Zone-Central"},
        # Vikram at Block 34 (CS)
        {"driver_id": "usr_driver_vikram", "lat": 31.2562, "lng": 75.7050, "zone": "Zone-North"},
        # Harpreet at Main Gate
        {"driver_id": "usr_driver_harpreet", "lat": 31.2510, "lng": 75.7080, "zone": "Zone-East"}
    ]

    for loc in locations:
        cur.execute("""
        INSERT INTO driver_locations (driver_id, is_online, lat, lng, zone, heading, updated_at)
        VALUES (?, 1, ?, ?, ?, 0.0, ?);
        """, (loc["driver_id"], loc["lat"], loc["lng"], loc["zone"], now))

    # Pre-seed Scheduled CityLink Carpools
    cur.execute("""
    INSERT INTO driver_routes (id, driver_id, origin, origin_lat, origin_lng, destination, destination_lat, destination_lng, departure_time, available_seats, total_seats, price_per_seat, notes, status, share_token, current_lat, current_lng, created_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?);
    """, (
        "route_jalandhar_today",
        "usr_driver_harpreet",
        "LPU Uni-Mall Plaza",
        31.2535,
        75.7038,
        "Jalandhar City Bus Stand",
        31.3190,
        75.5860,
        "17:15 Today (Post-Class)",
        3,
        3,
        85.0,
        "AC Carpool via GT Road. Trunk space available for luggage.",
        "share_jalandhar_pool",
        31.2535,
        75.7038,
        now
    ))

    cur.execute("""
    INSERT INTO driver_routes (id, driver_id, origin, origin_lat, origin_lng, destination, destination_lat, destination_lng, departure_time, available_seats, total_seats, price_per_seat, notes, status, share_token, current_lat, current_lng, created_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?);
    """, (
        "route_phagwara_station",
        "usr_driver_vikram",
        "Block 34 (Computer Science)",
        31.2562,
        75.7050,
        "Phagwara Railway Station",
        31.2210,
        75.7720,
        "18:00 Today",
        1,
        1,
        60.0,
        "Direct drop at Platform 1 entrance. Helmet provided.",
        "share_phagwara_pool",
        31.2562,
        75.7050,
        now
    ))

    conn.commit()
    conn.close()
    print("Database seeded with realistic LPU community test data!")
    print(f"Demo login password for every seeded persona: {DEMO_PASSWORD}")

if __name__ == "__main__":
    seed(if_empty="--if-empty" in sys.argv)
