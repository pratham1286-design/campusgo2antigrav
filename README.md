# CampusGo: LPU Bike, Scooty & Carpooling Transit Platform

**CampusGo** is a dedicated transit utility built exclusively for the Lovely Professional University (LPU) community. It solves campus mobility by offering flat-rate intra-campus pooling (**Campus Hop**) and dynamic-priced commutes to nearby cities (**CityLink**), prioritizing safety, teacher priority, and peak-time class-change load bearing.

---

## 🚀 Key Features & Architectural Highlights

### 1. Onboarding & Roles
- **Closed Campus Access**: Strictly for verified LPU students and faculty (`@lpu.in` verification).
- **Teacher Priority**: Verified faculty receive a gold badge and priority matching in shared pools (`is_teacher_priority` flag).
- **Role Selection**: Flexible role selection (`Rider`, `Driver`, or `Both`) with real-time switching.
- **Mandatory Trusted Emergency Contacts**: Integrated safety contacts (parents, wardens, Block-30 Campus Security Control Room).

### 2. 100% Server-Side Validated Pricing & Wallet
- **Campus Hop (Flat Rates)**:
  - 🏍️ **Bike**: Flat ₹15.00
  - 🛵 **Scooty**: Flat ₹20.00
  - 🚗 **Car**: Flat ₹35.00
- **CityLink (Dynamic Inter-City Commute)**:
  - Base fare + Distance fare + Peak rush dynamic surge multiplier.
  - Commute hubs: Jalandhar City Bus Stand, Phagwara Railway Station, Rama Mandi Chowk, Jalandhar Cantt.
- **Server-Side Wallet Authority**:
  - Validates balances server-side *before* booking matching.
  - Returns `402 Payment Required` with deficit if balance is insufficient.
  - Instant top-up via UPI / LPU Pay.
  - Automatic deduction and driver payout ledger upon ride completion.

### 3. Peak-Time Zone Queuing
- Campus partitioned into 4 zones: `Zone-North` (Academic Blocks 30-38), `Zone-South` (Boys Hostels BH1-BH8), `Zone-Central` (Uni-Mall, Uni-Hospital), and `Zone-East` (Girls Hostels GH1-GH6, Law Gate, Main Gate).
- Heavy class-change loads are queued gracefully without timing out.
- Priority queue evaluation: `(is_teacher_priority DESC, created_at ASC)`.

### 4. Safety Infrastructure
- **Persistent SOS Beacon**: High-contrast `#FF7C00` button triggers immediate alert to campus security hotline (`+91 1824 517000`) and dispatches SMS alerts to all trusted contacts.
- **Share Live Trip**: Real-time tracking link with share token for family and friends.
- **Mutual Ratings**: Post-ride feedback and verification tags (e.g. *Clean Helmet*, *Punctual*, *Safe Ride*).

### 5. Driver Control & Scheduled Carpooling
- **Online/Offline Toggle**: Live telemetry and zone tracking.
- **CityLink Route Posting**: Drivers post departure times and seat capacities for scheduled carpools.
- **Earnings Dashboard**: Total trips, net earnings, average ratings, and transaction history.

---

## 🛠️ Tech Stack & Directory Structure

- **Backend**: Python 3.14, Flask, SQLite3, pure-Python modules.
- **Frontend**: Mobile-first responsive web app, Leaflet.js with CartoDB tiles, modern transit utility design system (`#FF7C00` orange accents, sharp typography, `#FFFFFF` canvas).

```
campus go1/
├── backend/
│   ├── app.py                # Flask REST API, rate limiting, matching & wallet
│   ├── database.py           # SQLite database schema, indexes, and connection
│   ├── pricing_and_queue.py  # Flat hop rules, CityLink pricing & zone queue logic
│   ├── seed_data.py          # Realistic LPU test personas, vehicles & landmarks
│   └── test_campusgo.py      # Automated unit & integration tests
├── frontend/
│   ├── index.html            # Single Page Mobile App container
│   ├── styles.css            # Custom transit design system (#FF7C00 palette)
│   └── app.js                # Map engine, state machine, telemetry simulation & API
└── README.md
```

---

## 🏃 Quick Start Guide

### 1. Run Automated Test Suite
```bash
python backend/test_campusgo.py
```

### 2. Start the Server
```bash
python backend/app.py
```
Open [http://127.0.0.1:5000](http://127.0.0.1:5000) in your web browser.

### 3. Demo Personas Available for Testing
- **Dr. Raman Sharma** (`usr_teacher_raman`): Teacher Rider, Faculty Priority Badge ⭐, ₹350 balance.
- **Aarav Mehta** (`usr_student_aarav`): Student Rider, B.Tech CSE, ₹150 balance.
- **Kavya Patel** (`usr_student_kavya`): Low-balance Student (₹10) to test the Top-Up prompt.
- **Simran Kaur** (`usr_driver_simran`): Student Driver, Honda Activa 6G Scooty, Uni-Mall.
- **Vikram Singh** (`usr_driver_vikram`): Student Driver, Royal Enfield Hunter 350 Bike, Block 34.
- **Harpreet Singh** (`usr_driver_harpreet`): Driver, Maruti Swift Dzire Car, Main Gate.
