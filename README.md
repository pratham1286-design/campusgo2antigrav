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
- Every booking goes to a waiting list that online drivers with the right vehicle can see (faculty first, then oldest). Each driver chooses which request to accept, so nothing is assigned automatically. Riders can cancel from the Activity tab for a full refund until a driver accepts.
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
PAYMENTS_DEMO_MODE=1 OTP_DEMO_MODE=1 python app.py
```
Open [http://127.0.0.1:5000](http://127.0.0.1:5000). `PAYMENTS_DEMO_MODE=1` lets you try top-ups with fake money. `OTP_DEMO_MODE=1` shows the SMS code on screen instead of texting it (ignored once an SMS gateway is configured).

### 3. Demo personas
Log in with the LPU ID and mobile number below, then enter the code shown on screen (`OTP_DEMO_MODE=1`). The numbers are the ones in `seed_data.py`: Dr. Raman `9876543210`, Aarav `9812345678`, Kavya `9823456789`, Simran `9834567890`, Vikram `9845678901`, Harpreet `9856789012`.

- **Dr. Raman Sharma** (`FAC-10822`): Teacher rider, Faculty Priority badge ⭐, ₹350 balance.
- **Aarav Mehta** (`12204592`): Student rider, B.Tech CSE, ₹150 balance.
- **Kavya Patel** (`12301982`): Low-balance student (₹10) to test the top-up prompt.
- **Simran Kaur** (`12108843`): Student driver, Honda Activa 6G scooty, Uni-Mall.
- **Vikram Singh** (`12019934`): Student driver, Royal Enfield Hunter 350 bike, Block 34.
- **Harpreet Singh** (`11904421`): Driver, Maruti Dzire car, Main Gate.

---

## 🔐 Security & Deployment Notes

- **Sign up and log in use SMS one-time passwords.** Sign up collects name, a unique username (with suggestions), LPU ID and an OTP-verified mobile number; log in asks for LPU ID + mobile number + OTP. Codes are 6 digits, expire after 5 minutes, allow 5 tries, are stored only as a keyed hash, and can be resent every 30 seconds (5 texts per number per hour). Real delivery needs `FAST2SMS_API_KEY` or the Twilio settings; with neither, and no `OTP_DEMO_MODE`, the API answers 503 instead of issuing codes.
- **Sessions are real.** Every API call except login, landmarks, config and the public share link needs a signed `Authorization: Bearer <token>` header. The acting user always comes from the token, never from the request body.
- **Trips use real GPS.** Drivers' phones report their position (`/api/driver/location`). A ride starts when the driver is at the pickup, and the driver can complete it only once their phone shows them at the drop-off (the rider can always confirm arrival). Drivers who stop reporting for 2 minutes drop out of the available list and stop seeing requests. `DEMO_SIMULATE_MOVEMENT=1` turns on a demo-only fake-movement button and skips these checks; keep it off in real use.
- **Teacher priority needs approval.** Anyone can sign up as a teacher, but queue priority is granted only after an admin approves the account (`/api/admin/faculty/pending`, `/api/admin/faculty/<id>/approve`, same `X-Admin-Key` as payments). SOS alerts can be reviewed and resolved at `/api/admin/sos`.
- **Stale requests are cleaned up.** Ride requests unmatched for 30 minutes and carpools 6 hours past departure are cancelled and refunded automatically. `POST /api/auth/logout` signs the user out everywhere.
- **`SECRET_KEY` is required** in any deployment. The app refuses to start with a known placeholder or a key shorter than 32 characters. `render.yaml` generates one. For Docker, `docker compose up` fails until you set it.
- **Demo data is opt-in.** `SEED_DEMO_DATA=1` creates the demo personas only when the database is empty. Demo personas sign in with an SMS code like everyone else (set `OTP_DEMO_MODE=1` locally to see the code in the reply).
- **Payments.**
  - `PAYMENTS_DEMO_MODE=1` enables fake-money top-ups for demos. It is ignored whenever real Razorpay keys are configured.
  - With `RAZORPAY_KEY_ID` and `RAZORPAY_KEY_SECRET` set, card top-ups use Razorpay Checkout and are credited only after the HMAC signature is verified.
  - UPI has no automatic payment confirmation. When a user taps "I've Paid", the order waits for verification. With `ADMIN_API_KEY` set, list pending orders and approve or reject them after checking your bank statement:
    ```bash
    curl -H "X-Admin-Key: $ADMIN_API_KEY" https://your-app/api/admin/payments/pending
    curl -X POST -H "X-Admin-Key: $ADMIN_API_KEY" https://your-app/api/admin/payments/<id>/approve
    ```
- **Maps.** The home map draws the road route (distance and time) and moves nearby drivers as they drive. On CityLink the destination box searches any place or address inside the 200 km service area (as far as Chandigarh), ranking suggestions by how well they match what was typed, how well known the place is, and how close it is to the rider; the popular destinations are always listed and matched first. While a ride is active, the ride screen shows the driver's live position, the road to the pickup or drop and an ETA, refreshed every 5 seconds. The browser never calls the map services: the server does (`backend/geo.py`), so searches stay private and the service area is enforced server-side. The defaults are the free public OpenStreetMap servers (about one search per second); set `NOMINATIM_URL`, `OSRM_URL` and `GEOCODER_CONTACT` to use your own or a paid provider.
- **Single worker.** Rate-limit counters live in memory, so run one gunicorn worker (as the Procfile and Dockerfile do); they reset on restart.
- **Local HTTPS for phone GPS.** Browsers only share location on `https://` or `localhost`. `pip install -r requirements-dev.txt`, then `HTTPS_DEV=1 HOST=0.0.0.0 python app.py`.
- **SMS.** Without `FAST2SMS_API_KEY` or Twilio credentials, SOS alerts are recorded but no one is texted, and the app tells the user so.
- **Reverse proxies.** Set `TRUST_PROXY_HOPS` to the number of proxies in front of the app (1 on Render) so rate limits use the real client IP. Set `PUBLIC_BASE_URL` so SOS tracking links use your public domain.
- **SQLite persistence.** On Render's free plan the database is lost on every deploy or restart. For real persistence use a paid plan with a disk and set `DATABASE_PATH` (see `render.yaml`). Docker Compose persists only the `/app/data` directory, so rebuilt images always run the new code.
- **Headers.** Responses carry a strict Content-Security-Policy, `X-Frame-Options: DENY`, `nosniff` and `no-referrer`. The frontend never inserts server data as raw HTML.
- See `.env.example` for every setting.
