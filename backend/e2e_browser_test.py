"""End-to-end browser test: drives the real app in Chrome as a rider and a driver.

    pip install -r ../requirements-dev.txt        (installs playwright; uses your installed Chrome)
    python e2e_browser_test.py                    (add --headed to watch it)

It starts the app on the test suite's throwaway database and fake SMS gateway (test_support),
fakes the GPS position, and replaces the outside map services (place search, road routes) with
canned answers, so it needs no internet and touches no real data. Screenshots are saved to a
temp folder printed at the end. Exit code is 0 when every check passes.
"""
import json
import logging
import os
import re
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from unittest import mock

from test_support import ADMIN_KEY, LPU_IDS, PHONES, last_otp, load_fixtures
import geo  # noqa: E402
from app import app  # noqa: E402
from pricing_and_queue import haversine_distance_km  # noqa: E402
from werkzeug.serving import make_server  # noqa: E402

TMP = tempfile.mkdtemp(prefix="campusgo_e2e_")
os.environ["ADMIN_API_KEY"] = ADMIN_KEY
UNI_MALL = {"lat": 31.2535, "lng": 75.7038}

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


def login(user_id):
    start = api("/api/auth/login/start", "POST", {"lpu_id": LPU_IDS[user_id], "phone": PHONES[user_id]})
    return api("/api/auth/login/verify", "POST",
               {"challenge_id": start["challenge_id"], "otp": last_otp(PHONES[user_id])})["token"]


def topup(token, amount):
    """QR, "I've paid", then the admin approves it after checking the bank statement."""
    ref = api("/api/payments/upi/create-qr", "POST", {"amount": amount}, token)["reference_id"]
    api("/api/payments/upi/confirm", "POST", {"reference_id": ref}, token)
    req = urllib.request.Request(f"{BASE}/api/admin/payments/{ref}/approve", method="POST", headers={"X-Admin-Key": ADMIN_KEY})
    urllib.request.urlopen(req).close()


results = []


def check(name, condition, detail=""):
    results.append(bool(condition))
    print(("PASS  " if condition else "FAIL  ") + name + (f"   [{detail}]" if detail else ""))


def main():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("playwright is not installed. Run: pip install -r ../requirements-dev.txt")
    sys.stdout.reconfigure(errors="replace")  # a Windows console can't print every character (e.g. ₹)

    logging.getLogger("werkzeug").setLevel(logging.ERROR)  # no request log lines
    load_fixtures()
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

        rider_token = login("usr_student_aarav")
        driver_token = login("usr_driver_vikram")
        topup(rider_token, 5000)

        # ---- rider on campus ----
        page, errs = open_page(rider_token, 31.2535, 75.7038)
        page.goto(BASE)
        page.wait_for_selector("#pickup-select", state="attached")
        page.wait_for_timeout(2500)
        check("location gate is gone once location is shared", page.locator("#location-gate").evaluate("e => e.classList.contains('hidden')"))
        check("on campus the badge says Auto-detected", page.locator("#auto-detected-badge").inner_text() == "Auto-detected")
        check("the service worker installs (offline page, Play Store packaging)",
              page.evaluate("navigator.serviceWorker.ready.then(() => true)"))
        page.click("#header-wallet-btn")
        page.wait_for_timeout(800)
        check("top-up offers only the methods the server has set up (UPI here, no Razorpay keys)",
              page.locator("#pay-panel-upi").is_visible()
              and not page.locator('.pay-method-tab[data-method="razorpay"]').is_visible()
              and page.locator("#upi-qr-container img").count() == 1)
        page.click('#modal-topup .modal-close-btn')

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
        dpage.wait_for_timeout(1500)
        check("the PIN alone can't start the ride away from the pickup",
              "pickup point" in dpage.locator(".app-dialog .dialog-message").inner_text())
        dpage.click(".app-dialog .dialog-btn-primary")
        api("/api/driver/location", "POST", UNI_MALL, driver_token)  # the driver's phone reaches the pickup
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
        kavya_token = login("usr_student_kavya")
        off, offerrs = open_page(kavya_token, 31.40, 75.60)
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

        # ---- account screen and deleting the account (required by both app stores) ----
        off.click('.nav-tab[data-view="view-profile"]')
        off.wait_for_timeout(800)
        account = off.locator("#profile-account-lpu").inner_text()
        check("profile shows the username and the signed-in number", off.locator("#profile-card-username").inner_text() == "@kavya.patel"
              and account == "Signed in as 12301982 · +919823456789", account)
        off.click("#delete-account-btn")
        off.wait_for_timeout(500)
        warning = off.locator(".app-dialog .dialog-message").inner_text()
        check("deleting asks first and warns about the wallet balance", "₹10.00" in warning, warning)
        off.click(".app-dialog .dialog-btn-primary")
        off.wait_for_timeout(1500)
        check("a deleted account is signed out", off.locator("#login-screen").is_visible())
        check("the deleted account's sessions stop working", api("/api/auth/me", token=kavya_token).get("code") == "AUTH_INVALID")

        # ---- small phone: nothing hides under the bottom bar ----
        small, _ = open_page(login("usr_teacher_raman"), 31.2535, 75.7038, 360, 640)
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
