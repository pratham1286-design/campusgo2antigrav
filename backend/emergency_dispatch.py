import base64
import json
import os
import re
import time
import urllib.parse
import urllib.request

FAST2SMS_API_KEY = os.environ.get("FAST2SMS_API_KEY")
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.environ.get("TWILIO_PHONE_NUMBER")
# Single source of truth for the helpline shown in the app and in SMS alerts.
CAMPUS_SECURITY_PHONE = os.environ.get("CAMPUS_SECURITY_PHONE", "+91 1824 517000")

PHONE_PATTERN = re.compile(r"^\+?\d{10,15}$")


def normalize_phone(phone):
    """Returns a cleaned phone number, or None if it isn't a plausible number."""
    cleaned = re.sub(r"[\s\-()]", "", str(phone or ""))
    return cleaned if PHONE_PATTERN.match(cleaned) else None


def sms_gateway_configured():
    return bool(FAST2SMS_API_KEY or (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_PHONE_NUMBER))


def to_e164(phone):
    """+91XXXXXXXXXX for Indian mobiles written with or without the country code."""
    digits = re.sub(r"\D", "", str(phone or ""))
    if len(digits) == 10:
        return f"+91{digits}"
    if len(digits) == 11 and digits.startswith("0"):
        return f"+91{digits[1:]}"
    return f"+{digits}"


def format_sos_message(user_name, lpu_id, location_name, lat, lng, tracking_url=None, approximate=False):
    """Short emergency SMS. The link and hotline come first/last on purpose:
    they are the parts that must survive any length limit."""
    where = f"{location_name} (approx.)" if approximate and location_name else location_name
    if tracking_url:
        link = f"Live track: {tracking_url}"
    elif lat is not None and lng is not None:
        link = f"Map: https://maps.google.com/?q={lat:.5f},{lng:.5f}"
    else:
        link = "Location unavailable - call them now"
    return (
        f"CampusGo SOS: {user_name} (ID {lpu_id}) needs help.\n"
        f"{link}\n"
        f"Near: {where}\n"
        f"LPU Security: {CAMPUS_SECURITY_PHONE}"
    )


def _fast2sms_ok(body):
    # Fast2SMS answers 200 with {"return": false, ...} for rejected messages.
    return isinstance(body, dict) and body.get("return") is True


def send_fast2sms(phone, message):
    clean_phone = re.sub(r"\D", "", phone)[-10:]
    payload = {"route": "q", "message": message, "language": "english", "numbers": clean_phone}
    try:
        req = urllib.request.Request(
            "https://www.fast2sms.com/dev/bulkV2",
            data=json.dumps(payload).encode("utf-8"),
            headers={"authorization": FAST2SMS_API_KEY, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        return {"status": "sent" if _fast2sms_ok(body) else "failed", "gateway": "Fast2SMS"}
    except Exception:
        return {"status": "failed", "gateway": "Fast2SMS"}


def send_twilio_sms(phone, message):
    url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Messages.json"
    data = urllib.parse.urlencode({"From": TWILIO_PHONE_NUMBER, "To": to_e164(phone), "Body": message}).encode("utf-8")
    b64_auth = base64.b64encode(f"{TWILIO_ACCOUNT_SID}:{TWILIO_AUTH_TOKEN}".encode("utf-8")).decode("ascii")
    try:
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Authorization": f"Basic {b64_auth}", "Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            json.loads(resp.read().decode("utf-8"))
            return {"status": "sent", "gateway": "Twilio"}
    except Exception:
        return {"status": "failed", "gateway": "Twilio"}


def dispatch_emergency_alert(user_name, lpu_id, location_name, lat, lng, contacts, tracking_url=None,
                             approximate=False, blocked_phones=()):
    """Sends the SOS text to the user's own trusted contacts through a real SMS
    gateway when one is configured. Delivery statuses are reported honestly:
    'not_sent' means no gateway is configured and nobody was texted."""
    message_body = format_sos_message(user_name, lpu_id, location_name, lat, lng, tracking_url, approximate)
    dispatched_results = []

    for c in contacts:
        phone = normalize_phone(c.get("phone"))
        if not phone:
            delivery = {"status": "invalid_number"}
        elif phone in blocked_phones:
            # Per-number cap so nobody's phone can be flooded through many accounts.
            delivery = {"status": "rate_limited"}
        elif FAST2SMS_API_KEY:
            delivery = send_fast2sms(phone, message_body)
        elif TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_PHONE_NUMBER:
            delivery = send_twilio_sms(phone, message_body)
        else:
            delivery = {"status": "not_sent", "reason": "No SMS gateway configured"}

        dispatched_results.append({
            "contact_name": c.get("name", "Contact"),
            "contact_phone": c.get("phone", ""),
            "relationship": c.get("relationship", "Trusted Contact"),
            "delivery": delivery,
        })

    # There is no integration with the campus control room, so say so plainly
    # and point the user at the hotline instead of claiming a patrol was sent.
    security = {
        "status": "not_connected",
        "hotline": CAMPUS_SECURITY_PHONE,
        "message": "Campus security is not notified automatically. Call the hotline now.",
        "timestamp": time.time(),
    }

    return {
        "success": True,
        "sos_message": message_body,
        "contacts_dispatched": dispatched_results,
        "contacts_sent": sum(1 for r in dispatched_results if r["delivery"]["status"] == "sent"),
        "campus_security_dispatch": security,
    }


def send_sms(phone, message):
    """Sends one text through whichever gateway is configured.
    Returns {'status': 'sent' | 'failed' | 'not_sent', ...}."""
    number = normalize_phone(phone)
    if not number:
        return {"status": "invalid_number"}
    if FAST2SMS_API_KEY:
        return send_fast2sms(number, message)
    if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_PHONE_NUMBER:
        return send_twilio_sms(number, message)
    return {"status": "not_sent", "reason": "No SMS gateway configured"}
