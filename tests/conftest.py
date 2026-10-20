"""E2E fixtures: a real forest server (subprocess) on a throwaway data dir, plus a fake ntfy server."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
PASSWORD = "correct horse battery"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeNtfy:
    """Records every JSON publish."""

    def __init__(self):
        self.messages: list[dict] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("content-length", 0)))
                outer.messages.append({"auth": self.headers.get("authorization"), **json.loads(body)})
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *a):
                pass

        self.port = free_port()
        self.srv = ThreadingHTTPServer(("127.0.0.1", self.port), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"


class FakeGoogle:
    """Google's OAuth token endpoint + the slice of Calendar API v3 forest uses, in memory.
    Tests change events "in Google" with edit()/delete()/add()."""

    def __init__(self):
        import base64 as b64
        self.calendars, self.events, self.seq = {}, {}, 0
        self.revoked, self.codes, self.reqs = set(), {"good"}, []
        outer = self

        def jwt(claims):
            enc = lambda d: b64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
            return enc({"alg": "none"}) + "." + enc(claims) + ".sig"

        class H(BaseHTTPRequestHandler):
            def _send(self, code, obj=None):
                data = json.dumps(obj).encode() if obj is not None else b""
                self.send_response(code)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _body(self):
                raw = self.rfile.read(int(self.headers.get("content-length", 0)))
                if "json" in (self.headers.get("content-type") or ""):
                    return json.loads(raw or b"{}")
                from urllib.parse import parse_qs
                return {k: v[0] for k, v in parse_qs(raw.decode()).items()}

            def _authed(self):
                tok = (self.headers.get("authorization") or "").removeprefix("Bearer ")
                return tok in outer.access and outer.access[tok] not in outer.revoked

            def do_POST(self):
                from urllib.parse import urlparse, unquote
                path = urlparse(self.path).path
                b = self._body()
                outer.reqs.append(("POST", path, b))
                if path == "/token":
                    if b.get("grant_type") == "authorization_code":
                        if b.get("code") not in outer.codes or not b.get("code_verifier"):
                            return self._send(400, {"error": "invalid_grant"})
                        outer.n += 1
                        rt, at = f"rt-{outer.n}", f"at-{outer.n}"
                        outer.access[at] = rt
                        return self._send(200, {"access_token": at, "refresh_token": rt, "expires_in": 3600,
                                                "id_token": jwt({"email": "me@gmail.com"})})
                    rt = b.get("refresh_token")
                    if rt in outer.revoked or rt not in outer.access.values():
                        return self._send(400, {"error": "invalid_grant"})
                    outer.n += 1
                    outer.access[f"at-{outer.n}"] = rt
                    return self._send(200, {"access_token": f"at-{outer.n}", "expires_in": 3600})
                if path == "/revoke":
                    outer.revoked.add(b.get("token"))
                    return self._send(200, {})
                if not self._authed():
                    return self._send(401, {"error": "unauthenticated"})
                if path == "/calendar/v3/calendars":
                    cid = f"cal{len(outer.calendars) + 1}@group.calendar.google.com"
                    outer.calendars[cid] = b
                    return self._send(200, {"id": cid, **b})
                parts = path.split("/")
                if len(parts) == 6 and parts[5] == "events":
                    return self._send(200, outer.add(unquote(parts[4]), b))
                self._send(404, {})

            def do_PUT(self):
                from urllib.parse import urlparse, unquote
                parts = urlparse(self.path).path.split("/")
                b = self._body()
                outer.reqs.append(("PUT", self.path, b))
                if not self._authed():
                    return self._send(401, {})
                eid = parts[6]
                if eid not in outer.events or outer.events[eid]["status"] == "cancelled":
                    return self._send(404, {})
                outer.seq += 1
                outer.events[eid] = {**b, "id": eid, "status": "confirmed", "_cal": unquote(parts[4]), "_seq": outer.seq}
                self._send(200, outer.public(eid))

            def do_PATCH(self):
                from urllib.parse import urlparse
                parts = urlparse(self.path).path.split("/")
                b = self._body()
                outer.reqs.append(("PATCH", self.path, b))
                if not self._authed():
                    return self._send(401, {})
                outer.edit(parts[6], **b)
                self._send(200, outer.public(parts[6]))

            def do_DELETE(self):
                from urllib.parse import urlparse
                parts = urlparse(self.path).path.split("/")
                outer.reqs.append(("DELETE", self.path, None))
                if not self._authed():
                    return self._send(401, {})
                if parts[6] not in outer.events:
                    return self._send(404, {})
                outer.delete(parts[6])
                self._send(204)

            def do_GET(self):
                from urllib.parse import urlparse, parse_qs, unquote
                u = urlparse(self.path)
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                if not self._authed():
                    return self._send(401, {})
                parts = u.path.split("/")
                cal = unquote(parts[4])
                since = int(q["syncToken"]) if "syncToken" in q else None
                if since is not None and since < outer.min_token:
                    return self._send(410, {"error": "fullSyncRequired"})
                items = [outer.public(e) for e, ev in outer.events.items() if ev["_cal"] == cal and
                         (ev["_seq"] > since if since is not None else ev["status"] != "cancelled")]
                self._send(200, {"items": items, "nextSyncToken": str(outer.seq)})

            def log_message(self, *a):
                pass

        self.min_token, self.n, self.access = 0, 0, {}
        self.port = free_port()
        self.srv = ThreadingHTTPServer(("127.0.0.1", self.port), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def public(self, eid):
        return {k: v for k, v in self.events[eid].items() if not k.startswith("_")}

    def add(self, cal, body):
        self.seq += 1
        eid = f"ev{self.seq}"
        self.events[eid] = {**body, "id": eid, "status": "confirmed", "_cal": cal, "_seq": self.seq}
        return self.public(eid)

    def edit(self, eid, **fields):
        self.seq += 1
        ev = self.events[eid]
        for k, v in fields.items():
            if k == "extendedProperties":
                ev.setdefault(k, {}).setdefault("private", {}).update(v.get("private", {}))
            else:
                ev[k] = v
        ev["_seq"] = self.seq

    def delete(self, eid):
        self.seq += 1
        self.events[eid].update(status="cancelled", _seq=self.seq)

    def revoke_all(self):
        """The user (or Google) ends forest's access."""
        self.revoked.update(self.access.values())

    def by_key(self, key):
        return next((self.public(e) for e, ev in self.events.items()
                     if (ev.get("extendedProperties") or {}).get("private", {}).get("forest") == key
                     and ev["status"] != "cancelled"), None)


@pytest.fixture(scope="session")
def google():
    g = FakeGoogle()
    yield g
    g.srv.shutdown()


@pytest.fixture(scope="session")
def ntfy():
    f = FakeNtfy()
    yield f
    f.srv.shutdown()


@pytest.fixture(scope="session")
def server(tmp_path_factory, ntfy, google):
    data = tmp_path_factory.mktemp("data")
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    env = {
        **os.environ,
        "FOREST_CONFIG": "/dev/null",
        "FOREST_DATA": str(data),
        "FOREST_PASSWORD": PASSWORD,
        "FOREST_SECRET": "test-secret-" + "x" * 40,
        "FOREST_PUBLIC_URL": base,
        "FOREST_TZ": "UTC",
        "FOREST_MAX_UPLOAD_MB": "1",
        "FOREST_NTFY_URL": ntfy.url,
        "FOREST_NTFY_TOPIC": "forest-test",
        "FOREST_NTFY_TOKEN": "tk_ntfy",
        "FOREST_DIGEST_TIME": "",
        "FOREST_WEEKLY_TIME": "",
        "FOREST_EVENING_TIME": "",
        "FOREST_PUSH_ALLOW_HTTP": "1",
        "FOREST_GOOGLE_CLIENT_ID": "cid.apps.googleusercontent.com",
        "FOREST_GOOGLE_CLIENT_SECRET": "csecret",
        "FOREST_GOOGLE_TEST_BASE": google.url,
        "XDG_CONFIG_HOME": str(data / "clientcfg"),
        "PYTHONPATH": str(ROOT),
    }
    log = open(data / "server.log", "w")
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "forest.api:app", "--host", "127.0.0.1",
                             "--port", str(port)], cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    for _ in range(100):
        try:
            if httpx.get(base + "/healthz", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.1)
    else:
        proc.kill()
        raise RuntimeError((data / "server.log").read_text())

    class S:
        pass
    s = S()
    s.base, s.data, s.env, s.proc, s.port = base, data, env, proc, port
    yield s
    proc.terminate()
    proc.wait(10)


def login(server) -> httpx.Client:
    c = httpx.Client(base_url=server.base, headers={"X-Requested-With": "forest"}, timeout=30)
    r = c.post("/auth/login", data={"password": PASSWORD, "next": "/"})
    assert r.status_code == 303, r.text
    return c


@pytest.fixture(scope="session")
def web(server):
    """Logged-in browser-like session (admin)."""
    c = login(server)
    yield c
    c.close()


def make_token(web, name, scope="write", vaults=None, expires_days=None) -> str:
    r = web.post("/api/admin/tokens", json={"name": name, "scope": scope, "vaults": vaults or ["*"],
                                            "expires_days": expires_days})
    assert r.status_code == 201, r.text
    return r.json()["token"]


def bearer(server, token) -> httpx.Client:
    return httpx.Client(base_url=server.base, headers={"Authorization": f"Bearer {token}"}, timeout=30)


@pytest.fixture(scope="session")
def wtok(web):
    return make_token(web, "e2e-write", "write")


@pytest.fixture(scope="session")
def rtok(web):
    return make_token(web, "e2e-read", "read")


@pytest.fixture(scope="session")
def agent(server, wtok):
    c = bearer(server, wtok)
    yield c
    c.close()


@pytest.fixture(scope="session")
def reader(server, rtok):
    c = bearer(server, rtok)
    yield c
    c.close()


def audit(web, event=None):
    return web.get("/api/admin/audit", params={"limit": 500, **({"event": event} if event else {})}).json()
