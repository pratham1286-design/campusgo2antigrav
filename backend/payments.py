import base64
import hashlib
import hmac
import json
import os
import time
import urllib.parse
import urllib.request
import uuid

import qrcode
import qrcode.image.svg

MIN_TOPUP = 1.0
MAX_TOPUP = 5000.0
MERCHANT_NAME = "CampusGo LPU"
DEMO_SIGNATURE = "demo_signature_valid"
# Values shipped in old example configs; never treat them as real credentials.
KNOWN_PLACEHOLDER_SECRETS = {"", "demo_secret_campusgo_2026"}


class PaymentError(Exception):
    """A payment problem that is safe to show to the user."""


def _env(name, default=""):
    return os.environ.get(name, default).strip()


def upi_vpa():
    return _env("CAMPUSGO_UPI_VPA", "campusgo.lpu@okhdfcbank")


def razorpay_live_configured():
    return bool(_env("RAZORPAY_KEY_ID")) and _env("RAZORPAY_KEY_SECRET") not in KNOWN_PLACEHOLDER_SECRETS


def payments_demo_mode():
    """Simulated payments must be switched on explicitly, and are always off
    once real Razorpay credentials are configured."""
    enabled = _env("PAYMENTS_DEMO_MODE").lower() in ("1", "true", "yes")
    return enabled and not razorpay_live_configured()


def validate_topup_amount(amount):
    if amount < MIN_TOPUP or amount > MAX_TOPUP:
        raise PaymentError(f"Top-up amount must be between ₹{MIN_TOPUP:.0f} and ₹{MAX_TOPUP:.0f}")
    return round(amount, 2)


def create_payment_order(conn, user_id, provider, amount, provider_order_id=None):
    order_id = f"pay_{uuid.uuid4().hex[:16]}"
    conn.execute("""
    INSERT INTO payment_orders (id, user_id, provider, provider_order_id, amount, status, created_at)
    VALUES (?, ?, ?, ?, ?, 'created', ?)
    """, (order_id, user_id, provider, provider_order_id, amount, time.time()))
    return order_id


def generate_upi_qr(reference_id, amount):
    """Standard NPCI UPI deep link plus an SVG QR code for it."""
    params = {
        "pa": upi_vpa(),
        "pn": MERCHANT_NAME,
        "am": f"{amount:.2f}",
        "cu": "INR",
        "tn": f"CampusGo Wallet Topup {reference_id}",
        "tr": reference_id,
    }
    upi_uri = f"upi://pay?{urllib.parse.urlencode(params, safe='@')}"

    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=2,
        image_factory=qrcode.image.svg.SvgPathImage,
    )
    qr.add_data(upi_uri)
    qr.make(fit=True)
    svg = qr.make_image().to_string()
    svg_str = svg.decode("utf-8") if isinstance(svg, bytes) else svg

    return {
        "reference_id": reference_id,
        "amount": amount,
        "vpa": upi_vpa(),
        "merchant_name": MERCHANT_NAME,
        "upi_uri": upi_uri,
        "qr_data_url": f"data:image/svg+xml;utf8,{urllib.parse.quote(svg_str)}",
    }


def create_razorpay_remote_order(amount, receipt):
    """Creates a real order through the Razorpay Orders API."""
    key_id = _env("RAZORPAY_KEY_ID")
    secret = _env("RAZORPAY_KEY_SECRET")
    body = json.dumps({"amount": int(round(amount * 100)), "currency": "INR", "receipt": receipt}).encode("utf-8")
    auth = base64.b64encode(f"{key_id}:{secret}".encode("utf-8")).decode("ascii")
    req = urllib.request.Request(
        "https://api.razorpay.com/v1/orders",
        data=body,
        headers={"Authorization": f"Basic {auth}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))["id"]
    except Exception as exc:
        raise PaymentError("Could not reach the payment gateway. Please try again.") from exc


def verify_razorpay_signature(provider_order_id, payment_id, signature):
    """HMAC-SHA256 check from Razorpay's docs. The demo signature is accepted
    only in explicit demo mode, which is impossible with live keys set."""
    if not provider_order_id or not payment_id or not signature:
        return False
    if payments_demo_mode():
        return hmac.compare_digest(signature, DEMO_SIGNATURE)
    if not razorpay_live_configured():
        return False
    expected = hmac.new(
        _env("RAZORPAY_KEY_SECRET").encode("utf-8"),
        f"{provider_order_id}|{payment_id}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def credit_order(conn, order_id, method, provider_payment_id=None):
    """Marks an order paid and credits its stored amount exactly once.
    Must run inside the caller's transaction; the caller commits."""
    now = time.time()
    order = conn.execute("SELECT * FROM payment_orders WHERE id = ?", (order_id,)).fetchone()
    if not order:
        raise PaymentError("Payment order not found")
    if provider_payment_id:
        dup = conn.execute(
            "SELECT id FROM payment_orders WHERE provider_payment_id = ?", (provider_payment_id,)
        ).fetchone()
        if dup:
            raise PaymentError("This payment has already been used")

    cur = conn.execute("""
    UPDATE payment_orders SET status = 'paid', paid_at = ?, provider_payment_id = ?
    WHERE id = ? AND status IN ('created', 'awaiting_verification')
    """, (now, provider_payment_id, order_id))
    if cur.rowcount != 1:
        raise PaymentError("This payment has already been processed")

    amount = order["amount"]
    user_id = order["user_id"]
    conn.execute("INSERT OR IGNORE INTO wallets (user_id, balance, updated_at) VALUES (?, 0, ?)", (user_id, now))
    conn.execute("UPDATE wallets SET balance = balance + ?, updated_at = ? WHERE user_id = ?", (amount, now, user_id))
    new_bal = conn.execute("SELECT balance FROM wallets WHERE user_id = ?", (user_id,)).fetchone()["balance"]
    conn.execute("""
    INSERT INTO wallet_transactions (id, user_id, amount, type, reference_id, description, balance_after, created_at)
    VALUES (?, ?, ?, 'topup', ?, ?, ?, ?)
    """, (str(uuid.uuid4()), user_id, amount, order_id, f"Wallet Top-Up via {method.upper()}", new_bal, now))

    return {"success": True, "amount_credited": amount, "new_balance": round(new_bal, 2), "reference_id": order_id, "method": method}
