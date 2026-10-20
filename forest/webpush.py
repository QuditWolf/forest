"""Web Push to the installed forest apps (RFC 8030 delivery, RFC 8291 encryption, RFC 8292 VAPID).

Forest is its own push server: each device that turns notifications on hands us a subscription
(an endpoint at its browser's push service plus encryption keys). We encrypt every message for
that device and sign the request with our VAPID key; the push service (Google, Mozilla, Apple)
only relays an opaque blob. No third-party account or library.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import threading
import time
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .config import PUBLIC_URL, STATE_DIR

log = logging.getLogger("forest.webpush")
_lock = threading.Lock()
MAX_PAYLOAD = 3000          # push services accept ~4 KB encrypted; keep well under


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def unb64u(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


# ── VAPID key (identifies this server to push services) ───────────────────────

def _key() -> ec.EllipticCurvePrivateKey:
    f = STATE_DIR / "vapid.pem"
    with _lock:
        if not f.exists():
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            k = ec.generate_private_key(ec.SECP256R1())
            f.write_bytes(k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption()))
            os.chmod(f, 0o600)
        return serialization.load_pem_private_key(f.read_bytes(), None)


def public_key() -> str:
    """Application server key for pushManager.subscribe (uncompressed P-256 point, base64url)."""
    return b64u(_key().public_key().public_bytes(serialization.Encoding.X962,
                                                 serialization.PublicFormat.UncompressedPoint))


def _vapid_header(endpoint: str) -> str:
    u = urlparse(endpoint)
    sub = PUBLIC_URL if PUBLIC_URL.startswith("https://") else "mailto:forest@example.com"
    seg = lambda d: b64u(json.dumps(d, separators=(",", ":")).encode())
    signing_input = seg({"typ": "JWT", "alg": "ES256"}) + "." + \
        seg({"aud": f"{u.scheme}://{u.netloc}", "exp": int(time.time()) + 12 * 3600, "sub": sub})
    r, s = decode_dss_signature(_key().sign(signing_input.encode(), ec.ECDSA(hashes.SHA256())))
    jwt = signing_input + "." + b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
    return f"vapid t={jwt}, k={public_key()}"


# ── payload encryption (aes128gcm, RFC 8291) ──────────────────────────────────

def _hkdf(salt: bytes, ikm: bytes, info: bytes, n: int) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=n, salt=salt, info=info).derive(ikm)


def encrypt(payload: bytes, p256dh: str, auth: str) -> bytes:
    ua_pub = unb64u(p256dh)
    eph = ec.generate_private_key(ec.SECP256R1())
    eph_pub = eph.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    shared = eph.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_pub))
    ikm = _hkdf(unb64u(auth), shared, b"WebPush: info\x00" + ua_pub + eph_pub, 32)
    salt = secrets.token_bytes(16)
    cek = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    body = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)    # \x02 = last record, no padding
    return salt + (4096).to_bytes(4, "big") + bytes([len(eph_pub)]) + eph_pub + body


# ── subscribed devices ────────────────────────────────────────────────────────

def _file():
    return STATE_DIR / "push.json"


def devices() -> list:
    try:
        return json.loads(_file().read_text())
    except Exception:
        return []


def _save(ds: list) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    _file().write_text(json.dumps(ds, indent=1))
    os.chmod(_file(), 0o600)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def subscribe(sub: dict, name: str = "") -> dict:
    ep, keys = sub.get("endpoint", ""), sub.get("keys") or {}
    if not ep.startswith("https://") and not (os.environ.get("FOREST_PUSH_ALLOW_HTTP") == "1"
                                              and ep.startswith("http://127.0.0.1")):   # tests only
        raise ValueError("invalid push endpoint")
    if not keys.get("p256dh") or not keys.get("auth"):
        raise ValueError("subscription keys missing")
    unb64u(keys["p256dh"]), unb64u(keys["auth"])
    with _lock:
        ds = [d for d in devices() if d["endpoint"] != ep]
        d = {"id": secrets.token_hex(4), "name": (name or "device")[:60], "endpoint": ep,
             "keys": {"p256dh": keys["p256dh"], "auth": keys["auth"]}, "created": _now(),
             "last_ok": None, "last_error": None}
        ds.append(d)
        _save(ds)
    return public(d)


def unsubscribe(id_or_endpoint: str) -> bool:
    with _lock:
        ds = devices()
        keep = [d for d in ds if id_or_endpoint not in (d["id"], d["endpoint"])]
        _save(keep)
    return len(keep) != len(ds)


def public(d: dict) -> dict:
    """What the UI may see (no keys)."""
    return {"id": d["id"], "name": d["name"], "created": d["created"], "last_ok": d.get("last_ok"),
            "last_error": d.get("last_error"), "host": urlparse(d["endpoint"]).netloc,
            "ep": hashlib.sha256(d["endpoint"].encode()).hexdigest()[:16]}   # lets a browser spot its own row


def has_devices() -> bool:
    return bool(devices())


def send_all(title: str, body: str, url: Optional[str] = None, tag: Optional[str] = None,
             actions: Optional[list] = None, urgent: bool = False, only: Optional[str] = None) -> int:
    """Push to every device (or `only` one id). Expired subscriptions are dropped. Returns deliveries."""
    msg = {"title": title, "body": body, "url": url or "/", "tag": tag, "actions": actions or [], "urgent": urgent}
    raw = json.dumps(msg, ensure_ascii=False).encode()
    if len(raw) > MAX_PAYLOAD:
        msg["body"] = body[: max(0, len(body) - (len(raw) - MAX_PAYLOAD)) - 3] + "..."
        raw = json.dumps(msg, ensure_ascii=False).encode()
    ok, gone, status = 0, [], {}
    for d in devices():
        if only and d["id"] != only:
            continue
        try:
            r = httpx.post(d["endpoint"], content=encrypt(raw, d["keys"]["p256dh"], d["keys"]["auth"]),
                           headers={"Authorization": _vapid_header(d["endpoint"]), "TTL": str(12 * 3600),
                                    "Content-Encoding": "aes128gcm", "Content-Type": "application/octet-stream",
                                    "Urgency": "high" if urgent else "normal"}, timeout=15)
            if r.status_code in (404, 410):
                gone.append(d["id"])             # device unsubscribed / app uninstalled
            elif r.status_code >= 400:
                status[d["id"]] = (None, f"{r.status_code} {r.text[:120]}")
            else:
                ok += 1
                status[d["id"]] = (_now(), None)
        except Exception as e:
            status[d["id"]] = (None, str(e)[:120])
    if gone or status:
        with _lock:
            ds = [d for d in devices() if d["id"] not in gone]
            for d in ds:
                if d["id"] in status:
                    okt, err = status[d["id"]]
                    if okt:
                        d["last_ok"], d["last_error"] = okt, None
                    else:
                        d["last_error"] = err
            _save(ds)
        for g in gone:
            log.info("push: dropped expired device %s", g)
    return ok
