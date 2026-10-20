"""In-app notifications (Web Push): subscribe, encrypted delivery, VAPID, expiry, digests, actions."""

import base64
import json
import threading
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from conftest import free_port


def b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def unb64u(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class Browser:
    """A fake browser subscription: its own P-256 key + auth secret, like pushManager.subscribe()."""

    def __init__(self, endpoint):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.pub = self.key.public_key().public_bytes(serialization.Encoding.X962,
                                                      serialization.PublicFormat.UncompressedPoint)
        self.auth = b"0123456789abcdef"
        self.sub = {"endpoint": endpoint, "keys": {"p256dh": b64u(self.pub), "auth": b64u(self.auth)}}

    def decrypt(self, body: bytes) -> dict:
        salt, rs, idlen = body[:16], int.from_bytes(body[16:20], "big"), body[20]
        as_pub = body[21:21 + idlen]
        ct = body[21 + idlen:]
        shared = self.key.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_pub))
        hk = lambda salt, ikm, info, n: HKDF(algorithm=hashes.SHA256(), length=n, salt=salt, info=info).derive(ikm)
        ikm = hk(self.auth, shared, b"WebPush: info\x00" + self.pub + as_pub, 32)
        cek = hk(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
        nonce = hk(salt, ikm, b"Content-Encoding: nonce\x00", 12)
        pt = AESGCM(cek).decrypt(nonce, ct, None)
        assert rs == 4096 and pt.endswith(b"\x02")
        return json.loads(pt[:-1])


class FakePushService:
    """Plays Google's/Mozilla's push service: records requests; /gone/* answers 410."""

    def __init__(self):
        self.reqs = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("content-length", 0)))
                outer.reqs.append({"path": self.path, "headers": dict(self.headers), "body": body})
                self.send_response(410 if self.path.startswith("/gone") else 201)
                self.end_headers()

            def log_message(self, *a):
                pass

        self.port = free_port()
        self.srv = ThreadingHTTPServer(("127.0.0.1", self.port), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"


@pytest.fixture(scope="module")
def push():
    p = FakePushService()
    yield p
    p.srv.shutdown()


def verify_vapid(headers, server_key_b64, aud):
    auth = headers["Authorization"]
    assert auth.startswith("vapid t=")
    jwt, k = auth[len("vapid t="):].split(", k=")
    assert k == server_key_b64
    h, c, sig = jwt.split(".")
    claims = json.loads(unb64u(c))
    assert json.loads(unb64u(h))["alg"] == "ES256" and claims["aud"] == aud and claims["exp"] > datetime.now().timestamp()
    pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), unb64u(k))
    raw = unb64u(sig)
    pub.verify(encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")),
               f"{h}.{c}".encode(), ec.ECDSA(hashes.SHA256()))       # raises if forged


def test_push_devices_delivery_and_expiry(server, web, agent, push):
    key = web.get("/api/push/key").json()["key"]
    assert len(unb64u(key)) == 65 and web.get("/api/push/key").json()["key"] == key     # stable
    phone, old = Browser(push.url("/sub/phone")), Browser(push.url("/gone/old-laptop"))
    d = web.post("/api/push/devices", json={"subscription": phone.sub, "name": "Android Chrome"})
    assert d.status_code == 201 and d.json()["name"] == "Android Chrome" and "keys" not in d.json()
    web.post("/api/push/devices", json={"subscription": old.sub, "name": "old laptop"})
    # re-subscribing the same endpoint replaces, doesn't duplicate
    web.post("/api/push/devices", json={"subscription": phone.sub, "name": "Android Chrome"})
    devs = web.get("/api/push/devices").json()
    assert sorted(x["name"] for x in devs) == ["Android Chrome", "old laptop"]
    assert all("keys" not in x and "endpoint" not in x for x in devs)
    # bad subscriptions and tokens are refused
    assert web.post("/api/push/devices", json={"subscription": {"endpoint": "ftp://x", "keys": phone.sub["keys"]}}).status_code == 400
    assert web.post("/api/push/devices", json={"subscription": {"endpoint": push.url("/x")}}).status_code == 400
    assert agent.get("/api/push/devices").status_code == 403
    assert agent.post("/api/push/devices", json={"subscription": phone.sub}).status_code == 403

    # test push: encrypted for the device, signed with our VAPID key; the expired device is dropped
    push.reqs.clear()
    r = web.post("/api/push/test")
    assert r.status_code == 200 and r.json()["delivered"] == 1
    got = [x for x in push.reqs if x["path"] == "/sub/phone"][0]
    assert got["headers"]["Content-Encoding"] == "aes128gcm" and int(got["headers"]["TTL"]) > 0
    assert b"Notifications work" not in got["body"]                     # opaque to the push service
    msg = phone.decrypt(got["body"])
    assert msg["title"] == "forest: test" and "work on this device" in msg["body"]
    verify_vapid(got["headers"], key, f"http://127.0.0.1:{push.port}")
    assert [x["name"] for x in web.get("/api/push/devices").json()] == ["Android Chrome"]
    assert web.get("/api/push/devices").json()[0]["last_ok"]

    # remove
    dev = web.get("/api/push/devices").json()[0]["id"]
    assert web.delete(f"/api/push/devices/{dev}").status_code == 200
    assert web.get("/api/push/devices").json() == []
    assert web.delete(f"/api/push/devices/{dev}").status_code == 404
    web.post("/api/push/devices", json={"subscription": phone.sub, "name": "Android Chrome"})


def test_push_digests_and_reminders(server, web, agent, push):
    phone = Browser(push.url("/sub/phone2"))
    for x in web.get("/api/push/devices").json():
        web.delete(f"/api/push/devices/{x['id']}")
    web.post("/api/push/devices", json={"subscription": phone.sub, "name": "phone"})
    today, tomorrow = date.today(), date.today() + timedelta(days=1)
    agent.post("/api/tasks/pages", json={"name": "Dentist", "state": "todo", "due": today.isoformat(), "at": "16:00"})
    agent.post("/api/tasks/pages", json={"name": "Buy milk", "state": "todo", "due": today.isoformat()})
    agent.post("/api/tasks/pages", json={"name": "Flight", "state": "todo", "due": tomorrow.isoformat(), "at": "07:10"})

    def last():
        return phone.decrypt([x for x in push.reqs if x["path"] == "/sub/phone2"][-1]["body"])

    web.post("/api/admin/ntfy-test", params={"kind": "digest"})
    m = last()
    assert m["title"].startswith("today: ") and "16:00 Dentist" in m["body"] and "- Buy milk" in m["body"]
    assert m["body"].index("16:00 Dentist") < m["body"].index("- Buy milk")       # timed first
    assert m["url"].endswith("/tasks#today") and m["tag"] == "digest"
    web.post("/api/admin/ntfy-test", params={"kind": "evening"})
    m = last()
    assert m["title"].startswith("tomorrow: ") and "07:10 Flight" in m["body"] and m["url"].endswith("/tasks#tomorrow")

    # a task reminder: urgent, with a working "done" button
    soon = datetime.now(timezone.utc) + timedelta(minutes=3)
    agent.post("/api/tasks/pages", json={"name": "Standup call", "state": "todo", "due": soon.date().isoformat(),
                                         "at": soon.strftime("%H:%M"), "remind": ["5m"]})
    web.post("/api/admin/ntfy-test", params={"kind": "reminders"})
    req = [x for x in push.reqs if x["path"] == "/sub/phone2"][-1]
    m = phone.decrypt(req["body"])
    assert m["title"] == "reminder: Standup call" and req["headers"]["Urgency"] == "high" and m["urgent"] is True
    assert "/tasks#tasks/standup-call.md" in m["url"]
    done = [a for a in m["actions"] if a["title"] == "done"][0]
    assert httpx.post(done["url"]).json()["ok"]
    assert agent.get("/api/tasks/page/standup-call.md").json()["state"] == "done"


def test_notify_settings(web, agent):
    st = web.get("/api/admin/notify").json()
    assert {"morning", "evening", "weekly", "devices"} <= set(st)
    r = web.put("/api/admin/notify", json={"morning": "7:30", "evening": "", "weekly": "Sunday 18:00"})
    assert r.json() == {"morning": "07:30", "evening": "", "weekly": "sun 18:00"}
    assert web.get("/api/admin/notify").json()["morning"] == "07:30"
    assert web.put("/api/admin/notify", json={"morning": "25:00"}).status_code == 400
    assert web.put("/api/admin/notify", json={"weekly": "someday"}).status_code == 400
    assert agent.put("/api/admin/notify", json={"morning": "06:00"}).status_code == 403
    web.put("/api/admin/notify", json={"morning": "", "evening": "", "weekly": ""})
