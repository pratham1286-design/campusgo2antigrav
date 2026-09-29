# CampusGo: LPU Bike, Scooty & Carpooling Transit Platform

**CampusGo** is a dedicated transit utility built exclusively for the Lovely Professional University (LPU) community. It solves campus mobility by offering flat-rate intra-campus pooling (**Campus Hop**) and dynamic-priced commutes to nearby cities (**CityLink**), prioritizing safety, teacher priority, and peak-time class-change load bearing.

---

## 🚀 Key Features & Architectural Highlights

### 1. Onboarding & Roles
- **Closed Campus Access**: For verified LPU students and faculty.
- **Teacher Priority**: Verified faculty receive a gold badge and priority matching in shared pools (`is_teacher_priority` flag).
- **Role Selection**: `Rider`, `Driver`, or `Both`. Driving requires registering a vehicle from the Profile tab.
- **Trusted Emergency Contacts**: Up to 5 validated phone numbers per user.

### 2. 100% Server-Side Validated Pricing & Wallet
- **Campus Hop (Flat Rates, on-campus trips only)**:
  - 🏍️ **Bike**: Flat ₹15.00
  - 🛵 **Scooty**: Flat ₹20.00
  - 🚗 **Car**: Flat ₹35.00
- **CityLink (Dynamic Inter-City Commute)**:
  - Base fare + distance fare + peak rush surge multiplier. At least one end must be off campus.
  - Hubs from Kapurthala, Jalandhar and Phagwara along NH-44 through Goraya, Phillaur, Ludhiana, Khanna, Sirhind and Rajpura to Zirakpur, Kharar, Mohali and Chandigarh.
- **Server-Side Wallet Authority**:
  - The fare is held from the wallet when a ride is booked and refunded in full if the rider cancels.
  - Returns `402 Payment Required` with the deficit if the balance is insufficient.
  - Every top-up is tied to a server-created payment order; the credited amount comes from that order, and each order or payment ID can be credited only once.
  - The driver is paid 90% of the fare when the ride is completed.

### 3. Peak-Time Zone Queuing
- Campus partitioned into 4 zones: `Zone-North` (Academic Blocks 30-38), `Zone-South` (Boys Hostels BH1-BH8), `Zone-Central` (Uni-Mall, Uni-Hospital), and `Zone-East` (Girls Hostels GH1-GH6, Law Gate, Main Gate).
- When no driver is free the ride is queued. Riders can cancel from the Activity tab; drivers see matching requests in their Profile tab and accept them.
- Priority queue evaluation: `(is_teacher_priority DESC, created_at ASC)`.

### 4. Safety Infrastructure
- **SOS Button**: Records the alert with the phone's GPS position and texts the user's trusted contacts **if an SMS gateway (Fast2SMS or Twilio) is configured**. The app states plainly when nobody was texted. Campus security is **not** notified automatically; the SOS screen shows a one-tap call button for the hotline (`CAMPUS_SECURITY_PHONE`).
- **Share Live Trip**: A `/track/<token>` page that shows the ride's live position with no phone numbers. The link stops working when the ride ends.
- **Ratings**: One rating per person per completed ride, with selectable stars and tags.

### 5. Driver Control & Scheduled Carpooling
- **Online/Offline Toggle** and a list of queued requests the driver's vehicle can serve.
- **CityLink Route Posting**: Hosts offer seats between listed locations. Passengers can leave before departure for a full refund; hosts can cancel and refund everyone.
- **Earnings Dashboard**: Total trips, net earnings and average rating.

---

## 🛠️ Tech Stack & Directory Structure

- **Backend**: Python 3, Flask, SQLite3.
- **Frontend**: Mobile-first web app with Leaflet.js and OpenStreetMap tiles.

```
campusgo2antigrav/
├── backend/
│   ├── app.py                 # Flask REST API, auth, rate limiting, matching & wallet
│   ├── database.py            # SQLite schema, indexes and connection
│   ├── payments.py            # UPI QR, Razorpay orders/signatures, one-time crediting
│   ├── emergency_dispatch.py  # SOS message and SMS gateways
│   ├── pricing_and_queue.py   # Landmarks, flat hop rules, CityLink pricing
│   ├── seed_data.py           # Demo personas, vehicles & routes
│   ├── test_support.py        # Test setup (temporary database)
│   ├── test_campusgo.py       # Core flow tests
│   ├── test_enhancements.py   # Payments / SOS / landmark tests
│   └── test_security.py       # Regression tests for security fixes
├── frontend/
│   ├── index.html             # Single-page mobile app
│   ├── styles.css
│   ├── app.js                 # Map, state, API calls
│   ├── track.html / track.js  # Public live-tracking page
└── README.md
```

---

## 🏃 Quick Start Guide

### 1. Run the test suite
The tests create their own temporary database, so they never touch `backend/campusgo.db`.
```bash
cd backend
python -m unittest test_campusgo test_enhancements test_security
```

### 2. Start the server locally
```bash
cd backend
python seed_data.py          # wipes and loads demo data (use --if-empty to keep existing data)
PAYMENTS_DEMO_MODE=1 python app.py
```
Open [http://127.0.0.1:5000](http://127.0.0.1:5000). `PAYMENTS_DEMO_MODE=1` lets you try top-ups with fake money.

### 3. Demo personas
Every seeded account uses the password in `DEMO_PASSWORD`, which defaults to **`CampusGo@2026`** for local use.

- **Dr. Raman Sharma** (`FAC-10822`): Teacher rider, Faculty Priority badge ⭐, ₹350 balance.
- **Aarav Mehta** (`12204592`): Student rider, B.Tech CSE, ₹150 balance.
- **Kavya Patel** (`12301982`): Low-balance student (₹10) to test the top-up prompt.
- **Simran Kaur** (`12108843`): Student driver, Honda Activa 6G scooty, Uni-Mall.
- **Vikram Singh** (`12019934`): Student driver, Royal Enfield Hunter 350 bike, Block 34.
- **Harpreet Singh** (`11904421`): Driver, Maruti Dzire car, Main Gate.

---

## 🔐 Security & Deployment Notes

- **Login is real.** Every API call except login, landmarks, config and the public share link needs a signed `Authorization: Bearer <token>` header. The acting user always comes from the token, never from the request body.
- **`SECRET_KEY` is required** in any deployment. The app refuses to start with a known placeholder or a key shorter than 32 characters. `render.yaml` generates one. For Docker, `docker compose up` fails until you set it.
- **Demo data is opt-in.** `SEED_DEMO_DATA=1` creates the demo personas only when the database is empty. Set `DEMO_PASSWORD` on any public deployment so the README password doesn't work there.
- **Payments.**
  - `PAYMENTS_DEMO_MODE=1` enables fake-money top-ups for demos. It is ignored whenever real Razorpay keys are configured.
  - With `RAZORPAY_KEY_ID` and `RAZORPAY_KEY_SECRET` set, card top-ups use Razorpay Checkout and are credited only after the HMAC signature is verified.
  - UPI has no automatic payment confirmation. When a user taps "I've Paid", the order waits for verification. With `ADMIN_API_KEY` set, list pending orders and approve or reject them after checking your bank statement:
    ```bash
    curl -H "X-Admin-Key: $ADMIN_API_KEY" https://your-app/api/admin/payments/pending
    curl -X POST -H "X-Admin-Key: $ADMIN_API_KEY" https://your-app/api/admin/payments/<id>/approve
    ```
- **SMS.** Without `FAST2SMS_API_KEY` or Twilio credentials, SOS alerts are recorded but no one is texted, and the app tells the user so.
- **Reverse proxies.** Set `TRUST_PROXY_HOPS` to the number of proxies in front of the app (1 on Render) so rate limits use the real client IP. Set `PUBLIC_BASE_URL` so SOS tracking links use your public domain.
- **SQLite persistence.** On Render's free plan the database is lost on every deploy or restart. For real persistence use a paid plan with a disk and set `DATABASE_PATH` (see `render.yaml`). Docker Compose persists only the `/app/data` directory, so rebuilt images always run the new code.
- **Headers.** Responses carry a strict Content-Security-Policy, `X-Frame-Options: DENY`, `nosniff` and `no-referrer`. The frontend never inserts server data as raw HTML.
- See `.env.example` for every setting.
