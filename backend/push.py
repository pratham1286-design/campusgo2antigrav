"""Web Push: notifications that reach a phone even while CampusGo is closed.

Each message is encrypted for the subscribed browser (RFC 8291, aes128gcm) and signed with the
server's VAPID key (RFC 8292); the browser's own push service (Google's, for Chrome on Android)
delivers it. No account, API key or fee. The VAPID key is VAPID_PRIVATE_KEY if set, otherwise
one made on first use and kept in the database, so it survives restarts and is shared by every
server process (a new key would silently break every existing subscription).

Sending never blocks or fails the request that caused it: messages go out on a small thread pool.
"""
import base64
import json
import logging
import os
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives import hmac as crypto_hmac
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from database import get_db_connection

log = logging.getLogger("campusgo.push")

RECORD_SIZE = 4096
TTL_SECONDS = 3600            # a message the phone can't receive within an hour is stale: drop it
MAX_PAYLOAD_BYTES = 2048      # well inside one 4096-byte record
VAPID_SUBJECT = os.environ.get("VAPID_SUBJECT", "mailto:support@campusgo.app")
# The server POSTs to subscription URLs, so only real browser push services are accepted:
# anything else would let a user make the server call an address of their choosing.
PUSH_HOSTS = ("fcm.googleapis.com", ".push.services.mozilla.com", ".notify.windows.com", ".push.apple.com")

INLINE = False                # tests: send on the calling thread
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="push")
_vapid = None
_vapid_lock = threading.Lock()


def b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64url_decode(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _hmac(key, data):
    mac = crypto_hmac.HMAC(key, hashes.SHA256())
    mac.update(data)
    return mac.finalize()


def _raw_public(public_key):
    return public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def encrypt(payload, p256dh, auth):
    """The aes128gcm body for one browser subscription (RFC 8291 section 3)."""
    ua_public = b64url_decode(p256dh)
    auth_secret = b64url_decode(auth)
    ua_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)
    as_private = ec.generate_private_key(ec.SECP256R1())
    as_public = _raw_public(as_private.public_key())
    ecdh_secret = as_private.exchange(ec.ECDH(), ua_key)
    ikm = _hmac(_hmac(auth_secret, ecdh_secret), b"WebPush: info\x00" + ua_public + as_public + b"\x01")
    salt = os.urandom(16)
    prk = _hmac(salt, ikm)
    cek = _hmac(prk, b"Content-Encoding: aes128gcm\x00\x01")[:16]
    nonce = _hmac(prk, b"Content-Encoding: nonce\x00\x01")[:12]
    ciphertext = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)  # \x02: the last (only) record
    return salt + struct.pack("!IB", RECORD_SIZE, len(as_public)) + as_public + ciphertext


class Vapid:
    def __init__(self, private_key):
        self.key = private_key
        self.public_key = b64url(_raw_public(private_key.public_key()))

    def authorization(self, endpoint):
        """The VAPID header proving these pushes come from this server (an ES256 JWT)."""
        parts = urllib.parse.urlsplit(endpoint)
        claims = {"aud": f"{parts.scheme}://{parts.netloc}", "exp": int(time.time()) + 12 * 3600, "sub": VAPID_SUBJECT}
        compact = lambda obj: b64url(json.dumps(obj, separators=(",", ":")).encode())
        signing_input = f"{compact({'typ': 'JWT', 'alg': 'ES256'})}.{compact(claims)}"
        r, s = decode_dss_signature(self.key.sign(signing_input.encode(), ec.ECDSA(hashes.SHA256())))
        signature = b64url(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
        return f"vapid t={signing_input}.{signature}, k={self.public_key}"


def _new_private_key_text():
    return b64url(ec.generate_private_key(ec.SECP256R1()).private_numbers().private_value.to_bytes(32, "big"))


def vapid(conn=None):
    """The server's VAPID key pair, loaded (or made) once per process."""
    global _vapid
    if _vapid:
        return _vapid
    with _vapid_lock:
        if _vapid:
            return _vapid
        text = os.environ.get("VAPID_PRIVATE_KEY", "").strip()
        if not text:
            own = conn is None
            conn = conn or get_db_connection()
            try:
                read = lambda: conn.execute("SELECT value FROM app_settings WHERE name = 'vapid_private_key'").fetchone()
                row = read()
                if not row:
                    # Several processes may start at once: the first insert wins, everyone reads it back.
                    conn.execute("INSERT INTO app_settings (name, value) VALUES ('vapid_private_key', ?) ON CONFLICT (name) DO NOTHING",
                                 (_new_private_key_text(),))
                    conn.commit()
                    row = read()
                text = row["value"]
            finally:
                if own:
                    conn.close()
        _vapid = Vapid(ec.derive_private_key(int.from_bytes(b64url_decode(text), "big"), ec.SECP256R1()))
        return _vapid


def valid_endpoint(endpoint):
    parts = urllib.parse.urlsplit(endpoint or "")
    host = (parts.hostname or "").lower()
    return (parts.scheme == "https" and len(endpoint) <= 1000
            and any(host == h or (h.startswith(".") and host.endswith(h)) for h in PUSH_HOSTS))


def valid_keys(p256dh, auth):
    try:
        return len(b64url_decode(p256dh)) == 65 and len(b64url_decode(auth)) == 16
    except (ValueError, TypeError):
        return False


def _post(endpoint, body, headers):
    """Returns the push service's HTTP status."""
    req = urllib.request.Request(endpoint, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status
    except urllib.error.HTTPError as err:
        return err.code


def _deliver(user_ids, message):
    payload = json.dumps(message, separators=(",", ":")).encode()[:MAX_PAYLOAD_BYTES]
    conn = get_db_connection()
    try:
        marks = ",".join("?" * len(user_ids))
        subs = conn.execute(f"SELECT endpoint, p256dh, auth FROM push_subscriptions WHERE user_id IN ({marks})",
                            tuple(user_ids)).fetchall()
        keys = vapid(conn)
        gone = []
        for sub in subs:
            try:
                status = _post(sub["endpoint"], encrypt(payload, sub["p256dh"], sub["auth"]), {
                    "Authorization": keys.authorization(sub["endpoint"]),
                    "Content-Encoding": "aes128gcm",
                    "Content-Type": "application/octet-stream",
                    "TTL": str(TTL_SECONDS),
                    "Urgency": "high",
                })
            except (OSError, ValueError) as err:
                log.warning("push to %s failed: %s", urllib.parse.urlsplit(sub["endpoint"]).hostname, err)
                continue
            if status in (404, 410):  # the browser unsubscribed or the app was uninstalled
                gone.append(sub["endpoint"])
            elif status >= 400:
                log.warning("push service answered %s", status)
        for endpoint in gone:
            conn.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))
        if gone:
            conn.commit()
    finally:
        conn.close()


def notify(user_ids, title, body, url="/", tag=None):
    """Sends a notification to every phone these users turned notifications on for.
    `tag` replaces an earlier notification with the same tag instead of stacking a new one."""
    user_ids = [u for u in dict.fromkeys(user_ids if isinstance(user_ids, (list, tuple, set)) else [user_ids]) if u]
    if not user_ids:
        return
    message = {"title": title, "body": body, "url": url, "tag": tag or ""}

    def run():
        try:
            _deliver(user_ids, message)
        except Exception:  # a failed notification must never surface anywhere else
            log.exception("push delivery failed")

    if INLINE:
        run()
    else:
        _executor.submit(run)
