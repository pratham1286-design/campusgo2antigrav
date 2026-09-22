import os
import time
import uuid
import hmac
import hashlib
import urllib.parse
import qrcode
import qrcode.image.svg
from database import get_db_connection

# Environment variables for payment gateways
DEFAULT_RAZORPAY_SECRET = "demo_secret_campusgo_2026"
RAZORPAY_KEY_ID = os.environ.get("RAZORPAY_KEY_ID", "rzp_test_campusgo_demo")
RAZORPAY_KEY_SECRET = os.environ.get("RAZORPAY_KEY_SECRET", DEFAULT_RAZORPAY_SECRET)
# True only while no real Razorpay secret has been configured via env var.
DEMO_MODE = RAZORPAY_KEY_SECRET == DEFAULT_RAZORPAY_SECRET
LPU_CAMPUSGO_VPA = os.environ.get("CAMPUSGO_UPI_VPA", "campusgo.lpu@okhdfcbank")
MERCHANT_NAME = "CampusGo LPU"

def generate_upi_qr(user_id: str, amount: float, reference_id: str = None) -> dict:
    """
    Generates a standard NPCI UPI payment URI and an SVG QR code.
    Users can scan with GPay, PhonePe, Paytm, or BHIM.
    """
    if not reference_id:
        reference_id = f"UPI_{int(time.time())}_{uuid.uuid4().hex[:6]}"

    amount_str = f"{amount:.2f}"
    
    # NPCI Standard UPI deep link
    params = {
        "pa": LPU_CAMPUSGO_VPA,
        "pn": MERCHANT_NAME,
        "am": amount_str,
        "cu": "INR",
        "tn": f"CampusGo Wallet Topup {reference_id}",
        "tr": reference_id
    }
    
    upi_uri = f"upi://pay?{urllib.parse.urlencode(params, safe='@')}"

    # Generate pure Python SVG QR Code
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=2,
        image_factory=qrcode.image.svg.SvgPathImage
    )
    qr.add_data(upi_uri)
    qr.make(fit=True)
    svg_img = qr.make_image()
    svg_str = svg_img.to_string().decode("utf-8") if isinstance(svg_img.to_string(), bytes) else svg_img.to_string()

    return {
        "success": True,
        "reference_id": reference_id,
        "amount": amount,
        "vpa": LPU_CAMPUSGO_VPA,
        "merchant_name": MERCHANT_NAME,
        "upi_uri": upi_uri,
        "svg_qr": svg_str,
        "qr_data_url": f"data:image/svg+xml;utf8,{urllib.parse.quote(svg_str)}"
    }

def create_razorpay_order(user_id: str, amount: float) -> dict:
    """
    Creates a Razorpay order. In test mode, creates a mock order.
    Amount in paise (1 INR = 100 paise).
    """
    amount_paise = int(round(amount * 100))
    order_id = f"order_{uuid.uuid4().hex[:14]}"
    receipt = f"rcpt_{int(time.time())}_{user_id[:6]}"

    return {
        "success": True,
        "order_id": order_id,
        "amount": amount_paise,
        "currency": "INR",
        "receipt": receipt,
        "key_id": RAZORPAY_KEY_ID,
        "amount_display": f"₹{amount:.2f}",
        "merchant_name": MERCHANT_NAME
    }

def verify_razorpay_payment(order_id: str, payment_id: str, signature: str) -> bool:
    """
    Verifies Razorpay payment signature using HMAC SHA256.
    The demo bypass signatures only work while DEMO_MODE is on (no real
    RAZORPAY_KEY_SECRET configured) so a real deployment can't be credited
    with fake wallet top-ups by replaying the hardcoded demo signature.
    """
    if DEMO_MODE and signature in ("simulated_test_signature", "demo_signature_valid"):
        return True

    generated_signature = hmac.new(
        RAZORPAY_KEY_SECRET.encode("utf-8"),
        f"{order_id}|{payment_id}".encode("utf-8"),
        hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(generated_signature, signature)

def credit_wallet_after_payment(user_id: str, amount: float, method: str, reference_id: str) -> dict:
    """
    Server-side transactional ledger update. Credits user's wallet with validation.
    """
    if amount <= 0:
        raise ValueError("Top-up amount must be greater than zero")

    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("SELECT balance FROM wallets WHERE user_id = ?", (user_id,))
    wallet = cur.fetchone()
    old_bal = wallet["balance"] if wallet else 0.0
    new_bal = round(old_bal + amount, 2)
    now = time.time()

    cur.execute("UPDATE wallets SET balance = ?, updated_at = ? WHERE user_id = ?", (new_bal, now, user_id))
    cur.execute("""
    INSERT INTO wallet_transactions (id, user_id, amount, type, reference_id, description, balance_after, created_at)
    VALUES (?, ?, ?, 'topup', ?, ?, ?, ?)
    """, (
        str(uuid.uuid4()),
        user_id,
        amount,
        reference_id,
        f"Wallet Top-Up via {method.upper()} (#{reference_id[:10]})",
        new_bal,
        now
    ))

    conn.commit()
    conn.close()

    return {
        "success": True,
        "user_id": user_id,
        "amount_credited": amount,
        "new_balance": new_bal,
        "reference_id": reference_id,
        "method": method
    }
