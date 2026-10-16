import json
import logging
import os
import re
import urllib.error
import urllib.request

HTTPSMS_API_KEY = os.environ.get("HTTPSMS_API_KEY")
HTTPSMS_PHONE_NUMBER = os.environ.get("HTTPSMS_PHONE_NUMBER")
# Single source of truth for the helpline shown in the app and in SMS alerts.
CAMPUS_SECURITY_PHONE = os.environ.get("CAMPUS_SECURITY_PHONE", "+91 1824 517000")

log = logging.getLogger("campusgo")

PHONE_PATTERN = re.compile(r"^\+?\d{10,15}$")


def normalize_phone(phone):
    """Returns a cleaned phone number, or None if it isn't a plausible number."""
    cleaned = re.sub(r"[\s\-()]", "", str(phone or ""))
    return cleaned if PHONE_PATTERN.match(cleaned) else None


def sms_gateway_configured():
    return bool(HTTPSMS_API_KEY and normalize_phone(HTTPSMS_PHONE_NUMBER))


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


def _http_error_text(exc):
    """The gateway's own error message (bad key, no balance, unverified number...) for the logs."""
    if isinstance(exc, urllib.error.HTTPError):
        try:
            return f"HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:500]}"
        except Exception:
            return f"HTTP {exc.code}"
    return repr(exc)


def send_httpsms(phone, message):
    payload = {"from": to_e164(HTTPSMS_PHONE_NUMBER), "to": to_e164(phone), "content": message}
    try:
        req = urllib.request.Request(
            "https://api.httpsms.com/v1/messages/send",
            data=json.dumps(payload).encode("utf-8"),
            headers={"x-api-key": HTTPSMS_API_KEY, "Accept": "application/json", "Content-Type": "application/json"},
        )
        # httpSMS queues the send and may take longer than a direct SMS API.
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        if not isinstance(body, dict) or body.get("status") != "success":
            log.warning("httpSMS rejected the SMS: %s", str(body)[:500])
            return {"status": "failed", "gateway": "httpSMS"}
        return {"status": "sent", "gateway": "httpSMS"}
    except Exception as exc:
        log.warning("httpSMS request failed: %s", _http_error_text(exc))
        return {"status": "failed", "gateway": "httpSMS"}


def send_sms(phone, message):
    """Sends one text through whichever gateway is configured.
    Returns {'status': 'sent' | 'failed' | 'not_sent' | 'invalid_number', ...}."""
    number = normalize_phone(phone)
    if not number:
        return {"status": "invalid_number"}
    if sms_gateway_configured():
        return send_httpsms(number, message)
    return {"status": "not_sent", "reason": "No SMS gateway configured"}


def dispatch_emergency_alert(user_name, lpu_id, location_name, lat, lng, contacts, tracking_url=None,
                             approximate=False, blocked_phones=()):
    """Sends the SOS text to the user's own trusted contacts through a real SMS
    gateway when one is configured. Delivery statuses are reported honestly:
    'not_sent' means no gateway is configured and nobody was texted."""
    message_body = format_sos_message(user_name, lpu_id, location_name, lat, lng, tracking_url, approximate)
    dispatched_results = []

    for c in contacts:
        if normalize_phone(c.get("phone")) in blocked_phones:
            # Per-number cap so nobody's phone can be flooded through many accounts.
            delivery = {"status": "rate_limited"}
        else:
            delivery = send_sms(c.get("phone"), message_body)
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
    }

    return {
        "sos_message": message_body,
        "contacts_dispatched": dispatched_results,
        "contacts_sent": sum(1 for r in dispatched_results if r["delivery"]["status"] == "sent"),
        "campus_security_dispatch": security,
    }
