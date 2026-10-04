"""End-to-end browser test: drives the real app in Chrome as a rider and a driver.

    pip install -r ../requirements-dev.txt        (installs playwright; uses your installed Chrome)
    python e2e_browser_test.py                    (add --headed to watch it)

It starts the app on a throwaway database, fakes the GPS position, and replaces the outside
map services (place search, road routes) with canned answers, so it needs no internet and
touches no real data. Screenshots are saved to a temp folder printed at the end.
Exit code is 0 when every check passes.
"""
import json
import logging
import os
import re
import secrets
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from unittest import mock

TMP = tempfile.mkdtemp(prefix="campusgo_e2e_")
os.environ.update(DATABASE_PATH=os.path.join(TMP, "e2e.db"), SECRET_KEY=secrets.token_hex(32),
                  OTP_DEMO_MODE="1", PAYMENTS_DEMO_MODE="1", DEMO_SIMULATE_MOVEMENT="1")
for _var in ("FAST2SMS_API_KEY", "TWILIO_ACCOUNT_SID", "RAZORPAY_KEY_ID", "RAZORPAY_KEY_SECRET", "ADMIN_API_KEY"):
    os.environ.pop(_var, None)

import geo  # noqa: E402
from app import app  # noqa: E402
from pricing_and_queue import haversine_distance_km  # noqa: E402
from seed_data import seed  # noqa: E402
from werkzeug.serving import make_server  # noqa: E402

PORT = 5059
BASE = f"http://127.0.0.1:{PORT}"
HEADED = "--headed" in sys.argv
PLACES = {
    "sector": [{"lat": "30.7400", "lon": "76.7825", "name": "Sector 17 Plaza", "importance": 0.6,
                "display_name": "Sector 17 Plaza, Chandigarh, India"}],
    "elante": [{"lat": "30.7058", "lon": "76.8012", "name": "Elante Mall", "importance": 0.7,
                "display_name": "Elante Mall, Industrial Area, Chandigarh, India"}],
}


def fake_fetch(url):
    """Stands in for Nominatim and OSRM."""
    if "/search?" in url:
        query = re.search(r"[?&]q=([^&]+)", url).group(1).lower()
        return next((rows for key, rows in PLACES.items() if key in query), [])
    m = re.search(r"/driving/([\d.\-]+),([\d.\-]+);([\d.\-]+),([\d.\-]+)", url)
    a_lng, a_lat, b_lng, b_lat = (float(v) for v in m.groups())
    km = haversine_distance_km(a_lat, a_lng, b_lat, b_lng)
    mid = [(a_lng + b_lng) / 2 + 0.01, (a_lat + b_lat) / 2]
    return {"routes": [{"distance": km * 1000, "duration": km / 40 * 3600,
                        "geometry": {"coordinates": [[a_lng, a_lat], mid, [b_lng, b_lat]]}}]}


def api(path, method="GET", body=None, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(BASE + path, method=method, headers=headers,
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as err:
        return json.loads(err.read())


def login(lpu_id, phone):
    start = api("/api/auth/login/start", "POST", {"lpu_id": lpu_id, "phone": phone})
    return api("/api/auth/login/verify", "POST", {"challenge_id": start["challenge_id"], "otp": start["demo_otp"]})["token"]


results = []


def check(name, condition, detail=""):
    results.append(bool(condition))
    print(("PASS  " if condition else "FAIL  ") + name + (f"   [{detail}]" if detail else ""))


def main():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("playwright is not installed. Run: pip install -r ../requirements-dev.txt")

    logging.getLogger("werkzeug").setLevel(logging.ERROR)  # no request log lines
    seed()
    geo.MIN_UPSTREAM_GAP = 0
    server = make_server("127.0.0.1", PORT, app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    with mock.patch.object(geo, "_fetch_json", side_effect=fake_fetch), sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(channel="chrome", headless=not HEADED)
        except Exception:
            browser = pw.chromium.launch(channel="msedge", headless=not HEADED)

        def open_page(token, lat, lng, width=420, height=900):
            ctx = browser.new_context(viewport={"width": width, "height": height},
                                      geolocation={"latitude": lat, "longitude": lng}, permissions=["geolocation"])
            ctx.add_init_script(f"localStorage.setItem('campusgo_token', '{token}')")
            page = ctx.new_page()
            problems = []
            page.on("pageerror", lambda e: problems.append(f"page error: {e}"))
            page.on("response", lambda r: problems.append(f"HTTP {r.status} {r.request.method} {r.url.replace(BASE, '')}")
                    if r.status >= 400 and r.status not in (400, 402, 409) else None)
            return page, problems

        def shot(page, name):
            page.screenshot(path=os.path.join(TMP, name + ".png"))

        def on_view(page, view):
            return page.locator(f"#{view}").evaluate("e => e.classList.contains('active-view')")

        rider_token = login("12204592", "9812345678")
        driver_token = login("12019934", "9845678901")
        ref = api("/api/payments/upi/create-qr", "POST", {"amount": 5000}, rider_token)["reference_id"]
        api("/api/payments/upi/confirm", "POST", {"reference_id": ref}, rider_token)

        # ---- rider on campus ----
        page, errs = open_page(rider_token, 31.2535, 75.7038)
        page.goto(BASE)
        page.wait_for_selector("#pickup-select", state="attached")
        page.wait_for_timeout(2500)
        check("location gate is gone once location is shared", page.locator("#location-gate").evaluate("e => e.classList.contains('hidden')"))
        check("on campus the badge says Auto-detected", page.locator("#auto-detected-badge").inner_text() == "Auto-detected")

        page.click('.scope-tab[data-scope="citylink"]')
        page.wait_for_timeout(1200)
        page.click("#drop-search-input")
        page.wait_for_timeout(400)
        check("popular destinations are listed", page.locator("#drop-suggestions .drop-suggestion").count() >= 5)
        page.fill("#drop-search-input", "")
        page.type("#drop-search-input", "sector 17", delay=40)
        page.wait_for_timeout(2000)
        check("typed address shows place suggestions", "PLACES" in page.locator("#drop-suggestions .drop-suggestions-heading").all_inner_texts())
        page.locator("#drop-suggestions .drop-suggestion").last.click()
        page.wait_for_timeout(2500)
        check("fare quote works for the searched place", "₹" in page.locator("#selected-service-summary").inner_text())
        check("road route chip appears", page.locator("#route-info-chip").is_visible(), page.locator("#route-info-chip").inner_text())
        shot(page, "rider_route")

        page.click("#find-ride-btn")
        page.wait_for_timeout(2000)
        check("booking shows the themed popup", page.locator(".app-dialog .dialog-message").count() == 1)
        page.click(".app-dialog .dialog-btn-primary")

        # ---- driver chooses the ride ----
        dpage, derrs = open_page(driver_token, 31.2562, 75.7050)
        dpage.goto(BASE)
        dpage.wait_for_timeout(2500)
        dpage.click('.nav-tab[data-view="view-profile"]')
        dpage.wait_for_timeout(800)
        dpage.click('.role-pill-btn[data-role="both"]')
        dpage.wait_for_timeout(1200)
        if dpage.locator("#online-status-text").inner_text() != "Online":
            dpage.locator(".toggle-slider").click()
            dpage.wait_for_timeout(2000)
        check("driver goes online", dpage.locator("#online-status-text").inner_text() == "Online")
        dpage.wait_for_timeout(11000)  # the app checks for work every 10 seconds
        check("driver sees the request without refreshing", dpage.locator(".driver-request-card").count() >= 1)
        check("Profile tab shows the new-request dot", not dpage.locator("#nav-requests-dot").evaluate("e => e.classList.contains('hidden')"))
        shot(dpage, "driver_requests")
        dpage.locator('.driver-request-card [data-action="accept-ride"]').first.click()
        dpage.wait_for_timeout(2000)
        check("driver lands on the live ride screen", on_view(dpage, "view-active-ride"))

        # ---- rider is taken to live tracking ----
        page.wait_for_timeout(12000)
        check("rider is moved to the live ride screen", on_view(page, "view-active-ride"))
        page.wait_for_timeout(6000)
        text = page.locator("#cockpit-telemetry-sub").inner_text()
        check("live tracking shows the driver's distance", "km" in text, text)
        shot(page, "rider_live")

        # ---- the ride starts only with the rider's PIN ----
        pin = page.locator("#ride-pin-value").inner_text().strip()
        check("rider sees a 4-digit ride PIN", page.locator("#ride-pin-box").is_visible() and re.fullmatch(r"\d{4}", pin) is not None, pin)
        check("driver is asked for the PIN, and doesn't see it", dpage.locator("#start-ride-box").is_visible()
              and not dpage.locator("#ride-pin-box").is_visible())
        dpage.fill("#start-pin-input", pin)
        dpage.click("#start-ride-btn")
        dpage.wait_for_timeout(2500)
        check("driver's PIN entry starts the ride", "PROGRESS" in dpage.locator("#cockpit-live-status-text").inner_text()
              and not dpage.locator("#start-ride-box").is_visible())
        page.wait_for_timeout(7000)
        check("rider sees the ride in progress", "PROGRESS" in page.locator("#cockpit-live-status-text").inner_text()
              and not page.locator("#ride-pin-box").is_visible())
        shot(dpage, "driver_started")
        check("no unexpected errors (rider)", not errs, " | ".join(errs[:4]))
        check("no unexpected errors (driver)", not derrs, " | ".join(derrs[:4]))

        # ---- rider away from campus ----
        off, offerrs = open_page(login("12301982", "9823456789"), 31.40, 75.60)
        off.goto(BASE)
        off.wait_for_timeout(3000)
        selected = "e => e.options[e.selectedIndex] ? e.options[e.selectedIndex].text : ''"
        check("off campus: badge says Not in campus", off.locator("#auto-detected-badge").inner_text() == "Not in campus")
        check("off campus: Campus Hop pickup says Not in campus", "Not in campus" in off.locator("#pickup-select").evaluate(selected))
        fares = [off.locator(f"#fare-{v}").inner_text() for v in ("bike", "scooty", "car")]
        check("off campus: vehicle tiles show no price", not any(re.search(r"\d", f) for f in fares), str(fares))
        counts = [off.locator(f"#avail-{v}").inner_text() for v in ("bike", "scooty", "car")]
        check("off campus: each vehicle tile shows its own live driver count",
              all(re.fullmatch(r"\d+ drivers? online", c) for c in counts), str(counts))
        off.click('.scope-tab[data-scope="citylink"]')
        off.wait_for_timeout(1800)
        check("off campus: CityLink pickup is the current location", "current location" in off.locator("#pickup-select").evaluate(selected).lower())
        check("no unexpected errors (off campus)", not offerrs, " | ".join(offerrs[:4]))

        # ---- small phone: nothing hides under the bottom bar ----
        small, _ = open_page(login("FAC-10822", "9876543210"), 31.2535, 75.7038, 360, 640)
        small.goto(BASE)
        small.wait_for_timeout(2500)
        small.locator(".bottom-sheet").evaluate("e => e.scrollTop = e.scrollHeight")
        small.wait_for_timeout(300)
        button = small.locator("#find-ride-btn").bounding_box()
        nav = small.locator(".bottom-nav-bar").bounding_box()
        check("small phone: Find Ride sits above the bottom bar", button and nav and button["y"] + button["height"] <= nav["y"] + 1)
        browser.close()
    server.shutdown()

    passed = sum(results)
    print(f"\n{passed} of {len(results)} checks passed. Screenshots: {TMP}")
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
