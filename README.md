# CampusGo: LPU Bike, Scooty & Carpooling Transit Platform

**CampusGo** is a transit app for the Lovely Professional University (LPU) community: flat-rate on-campus rides (**Campus Hop**), distance-priced rides to nearby cities as far as Chandigarh (**CityLink**), and scheduled carpools, with teacher priority, real-GPS trip checks and an SOS button.

---

## Features

### Accounts
- Sign up with name, username, LPU ID (or faculty ID) and an Indian mobile number verified by SMS code; log in with LPU ID + number + code.
- Roles: `Rider`, `Driver` or `Both`. Driving needs a registered vehicle.
- Teachers get queue priority only after an admin approves their faculty ID.
- Up to 5 emergency contacts. Accounts can be deleted in the app (Profile → Delete Account).

### Pricing and wallet (all server-side)
- **Campus Hop** (both ends on campus): flat ₹14 for bike, scooty or car. The driver earns ₹13, the platform keeps ₹1.
- **CityLink** (at least one end off campus): base fare + per-km rate, × up to 1.5 at class-change rush hours and busy times. The driver earns 90%.
- **Carpools:** the host sets the seat price and earns 90%.
- The fare is held from the wallet at booking and refunded in full on cancel. Top-ups go through Razorpay (credited automatically) or, optionally, a direct UPI QR (credited after an admin checks the bank statement). Drivers withdraw what they earned (never topped-up money) to a UPI ID.

### Matching and trips
- Every booking goes to a queue that online drivers with the right vehicle can see (faculty first, then oldest); drivers choose what to accept.
- Trips use the driver's real phone GPS: a ride starts only when the driver, at the pickup, enters the 4-digit PIN shown on the rider's phone, and the driver can complete it only at the drop-off (the rider can always confirm arrival). Impossible GPS jumps are rejected.
- Unmatched requests are cancelled and refunded after 30 minutes; carpools 6 hours past departure; a started trip nobody finishes is settled after 12 hours.

### Safety
- **SOS** records the alert and texts the user's emergency contacts with their location or a live trip link. It says plainly when SMS isn't available, and shows a one-tap call to campus security (it does not alert security automatically).
- **Share trip:** a `/track/<token>` page with the live position and no phone numbers, which stops working when the ride ends.
- **Privacy:** other users see available drivers only as anonymous markers placed to about 100 m, and open carpools only with first names.

---

## Project layout

```
campusgo2antigrav/
├── backend/
│   ├── app.py                 # Flask API: auth, rides, carpools, wallet, SOS, admin, static files
│   ├── database.py            # Schema; SQLite (default) or PostgreSQL (DATABASE_URL)
│   ├── shared.py              # Rate limits, caches, cluster-wide locks; memory or Redis (REDIS_URL)
│   ├── geo.py                 # Place search (local index) and trip routes (estimate, or OSRM)
│   ├── places.db              # Place-search index built from OpenStreetMap (maintenance/)
│   ├── payments.py            # UPI QR, Razorpay orders/signatures/webhook, one-time crediting
│   ├── emergency_dispatch.py  # httpSMS gateway and the SOS message
│   ├── pricing_and_queue.py   # Landmarks, trip rules, Campus Hop and CityLink pricing
│   ├── gunicorn.conf.py       # Production server settings
│   ├── test_support.py        # Test fixtures: throwaway database, fake SMS gateway
│   ├── test_*.py              # Unit and API tests
│   └── e2e_browser_test.py    # Drives the real app in Chrome as a rider and a driver
├── frontend/
│   ├── index.html, app.js, styles.css   # The single-page app
│   ├── track.html, track.js             # Public live-trip page
│   ├── privacy.html, terms.html         # Served at /privacy and /terms
│   ├── manifest.webmanifest, sw.js, offline.html, favicon.svg, icon-*.png
│   ├── leaflet.js, leaflet.css          # Leaflet 1.9.4 (map library), served locally
│   ├── protomaps-leaflet.js, campus-map.js  # Draws the map from our own files
│   └── maps/area.pmtiles, maps/streets.pmtiles  # The map of the service area (OpenStreetMap data)
├── maintenance/               # Rebuilds the map files and place index every few months
├── render.yaml, Dockerfile, docker-compose.yml, .env.example
└── requirements.txt, requirements-dev.txt
```

---

## Running locally

```bash
pip install -r requirements-dev.txt
cp .env.example .env        # then fill in what you need (see below)
cd backend
python app.py               # http://127.0.0.1:5000
```

- **Signing in** needs httpSMS (`HTTPSMS_API_KEY` and `HTTPSMS_PHONE_NUMBER`). The sender number must be registered in the httpSMS Android app, which needs to be online to send texts. Without a gateway, set `REVIEW_LOGIN_PHONE` and `REVIEW_LOGIN_CODE` for a fixed-code account.
- **Payments:** use Razorpay **test** keys (`rzp_test_...`) to try top-ups with fake money.
- **GPS on a phone:** browsers only share location over HTTPS. Run `HTTPS_DEV=1 HOST=0.0.0.0 python app.py` and open the `https://` LAN address. On a computer, Chrome DevTools → Sensors can fake a location.
- The local database is `backend/campusgo.db`, created on first start. Delete it to start over.

## Tests

```bash
cd backend
python -m unittest discover -p "test_*.py"
```

Tests use a throwaway database and a fake SMS gateway; they never touch `campusgo.db` or your `.env`. To run them on PostgreSQL and through a Redis client (as in production):

```bash
TEST_DATABASE_URL=postgresql://user:pass@localhost/campusgo_test TEST_REDIS=fake python -m unittest discover -p "test_*.py"
```

`TEST_DATABASE_URL` must point at a throwaway database: its tables are wiped.

**Browser test:** `python e2e_browser_test.py` (add `--headed` to watch). It opens Chrome as a rider, a driver and an off-campus rider with faked GPS, and checks booking, the driver accepting, live tracking, the PIN start, top-ups, the account screen and account deletion. It exits non-zero if anything fails.

---

## Deploying

### Render (recommended)
`render.yaml` creates the web service (1–4 autoscaling instances), PostgreSQL and a Redis-compatible Key Value store (paid plans). It generates `SECRET_KEY` and `ADMIN_API_KEY`; set the rest in the dashboard: `PUBLIC_BASE_URL`, an SMS gateway, Razorpay live keys + `RAZORPAY_WEBHOOK_SECRET`. Maps need no settings or API keys.

### Docker
`SECRET_KEY=... POSTGRES_PASSWORD=... docker compose up -d` runs the app with PostgreSQL and Redis. `.dockerignore` keeps tests, local databases and `.env` out of the image.

### Production notes
- **Launch on an empty production database.** The schema is created automatically on first start.
- `SECRET_KEY` is required: gunicorn refuses to start without it, and the app refuses keys under 32 characters.
- Set `TRUST_PROXY_HOPS` to the number of proxies in front of the app (1 on Render) so rate limits see real client IPs.
- With `DATABASE_URL` (PostgreSQL) and `REDIS_URL` set, gunicorn runs several workers and instances can share state. Without them it falls back to SQLite and one worker. Keep `WEB_CONCURRENCY × DB_POOL_SIZE × instances` under the database's connection limit.
- Razorpay webhook: in the Razorpay Dashboard (Account & Settings → Webhooks) add `https://your-domain/api/payments/razorpay/webhook` with the `payment.captured` event, and put its secret in `RAZORPAY_WEBHOOK_SECRET`.
- Maps use no outside service: the map, place search and route estimates ship with the app (OpenStreetMap data, credited on every map). Refresh them every few months with `maintenance/` (see its README).
- Every setting is documented in `.env.example`.

## Admin operations

All admin routes need the `X-Admin-Key: $ADMIN_API_KEY` header and return 404 when `ADMIN_API_KEY` is unset.

```bash
A="X-Admin-Key: $ADMIN_API_KEY"; H="Content-Type: application/json"
curl -H "$A" https://your-domain/api/admin/payments/pending                        # UPI top-ups to check
curl -X POST -H "$A" https://your-domain/api/admin/payments/<id>/approve            # ...or /reject
curl -H "$A" https://your-domain/api/admin/payouts/pending                         # driver withdrawals
curl -X POST -H "$A" -H "$H" -d '{"utr":"412345678901"}' https://your-domain/api/admin/payouts/<id>/paid
curl -X POST -H "$A" -H "$H" -d '{"note":"wrong UPI ID"}' https://your-domain/api/admin/payouts/<id>/reject
curl -H "$A" https://your-domain/api/admin/faculty/pending                         # teachers awaiting priority
curl -X POST -H "$A" https://your-domain/api/admin/faculty/<user_id>/approve
curl -H "$A" https://your-domain/api/admin/sos                                     # open SOS alerts
curl -X POST -H "$A" https://your-domain/api/admin/sos/<id>/acknowledge            # ...or /resolve
curl -H "$A" https://your-domain/api/admin/earnings                                # platform's cut
```

## Security

- Every API call except sign-in, config, landmarks and the public trip link needs a signed `Authorization: Bearer` token; the acting user always comes from the token. Logging out (or deleting the account) ends every session.
- SMS codes: 6 digits, 5 minutes, 5 tries, stored only as a keyed hash, resendable every 30 s, at most 5 texts per number per hour. Login replies look the same whether or not an account exists.
- Money: every top-up is tied to a server-created order and credited once; Razorpay payments are verified by HMAC signature, webhook signature or a server-side status check; wallet and seat changes are serialised so they can't be overspent or oversold.
- Strict Content-Security-Policy with no third-party scripts except Razorpay Checkout, `X-Frame-Options: DENY`, `nosniff`, HSTS on HTTPS, rate limits on every write, no internal errors in replies, and no server data inserted into the page as raw HTML.

---

## Launching on Google Play and the App Store

The app is an installable web app (PWA). Both stores get a thin native shell around the live site, so app updates ship by deploying the server; no store update is needed.

### 1. Before submitting to either store
- [ ] Deploy to production with an empty database and every setting above (live Razorpay keys, SMS gateway, `PUBLIC_BASE_URL` on HTTPS).
- [ ] Fill in every `[BRACKETED]` value in `frontend/privacy.html` and `frontend/terms.html` (operator name, contact email, address, grievance officer, hosting provider, city) and have both reviewed by a lawyer.
- [ ] **Reviewer account.** Set `REVIEW_LOGIN_PHONE` (a number you own) and `REVIEW_LOGIN_CODE` (6 digits), then sign up in the app with that number. To give it wallet money without paying: set `CAMPUSGO_UPI_VPA`, start a UPI top-up from that account, tap "I've Paid", and approve the order with the admin API.
- [ ] Record a short video of a full ride (book → driver accepts → PIN → complete): a ride needs a second person at the pickup, so reviewers can't do it alone.
- [ ] Decide on the legal points below.

### 2. Google Play (Trusted Web Activity)
- [ ] Create a Play Console developer account. New personal accounts must run a closed test with at least 12 testers for 14 days before they can publish.
- [ ] Generate the Android app at [pwabuilder.com](https://www.pwabuilder.com) from your site URL (or `npx @bubblewrap/cli init --manifest https://your-domain/manifest.webmanifest`). Pick a package name (e.g. `in.campusgo.app`) and turn on **location delegation**.
- [ ] Upload the `.aab` to a testing track. In Play Console → Setup → App signing, copy the **SHA-256** certificate fingerprint and set `ANDROID_PACKAGE_NAME` and `ANDROID_CERT_SHA256` on the server. Check `https://your-domain/.well-known/assetlinks.json`; the app then opens full-screen without a browser bar.
- [ ] Store listing: icon `frontend/icon-512.png`, a 1024×500 feature graphic, phone screenshots, descriptions; category Maps & Navigation.
- [ ] App content: privacy policy `https://your-domain/privacy`; account deletion URL `https://your-domain/privacy` (it explains in-app and email deletion); target audience 18+; content rating questionnaire; no ads.
- [ ] Data safety form. CampusGo collects: name, phone number and user IDs (LPU ID); precise location (app functionality; drivers' positions are shown to their riders); purchase history (wallet); user-generated content (ratings, trip notes); other info (emergency contacts the user types in, vehicle details, a driver's UPI ID). Card and bank details go to Razorpay. Data is encrypted in transit; users can delete their account in the app. No data is used for ads or sold.

### 3. Apple App Store
- [ ] Join the Apple Developer Program and use a Mac with Xcode (or a cloud Mac build service).
- [ ] Generate the iOS project at [pwabuilder.com](https://www.pwabuilder.com) (a WKWebView shell). Set the bundle ID and name, and in `Info.plist` add `NSLocationWhenInUseUsageDescription`: *"CampusGo uses your location to set your pickup point, show nearby rides, and share your position with your driver or rider during a trip."*
- [ ] App Store Connect: privacy policy and support URLs, the App Privacy answers (same data as above), screenshots, and in App Review Information the reviewer number + code and the video link.
- [ ] Account deletion is in the app (Guideline 5.1.1(v)). Sign in with Apple is not required, because sign-in is by phone number only.
- [ ] Risk: Apple can reject apps that are mainly a website in a wrapper (Guideline 4.2). If that happens, the next step is a Capacitor build with the frontend bundled in the app and native location and push notifications.

### 4. Legal points to settle before a public launch (not code)
- Carrying paying passengers in private bikes and cars is regulated in India (Motor Vehicles Act, state aggregator and bike-taxi rules, commercial insurance). Get advice on how CampusGo and its drivers comply.
- The wallet must stay closed-loop (spendable only on CampusGo) to avoid RBI prepaid-instrument licensing. The code already blocks withdrawing topped-up money.
- India's DPDP Act requires verifiable parental consent for users under 18. The terms limit CampusGo to users aged 18 and over.
- SMS sign-in codes in India should use a DLT-registered template (ask Fast2SMS about its OTP route).
