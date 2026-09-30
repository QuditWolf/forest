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


@pytest.fixture(scope="session")
def ntfy():
    f = FakeNtfy()
    yield f
    f.srv.shutdown()


@pytest.fixture(scope="session")
def server(tmp_path_factory, ntfy):
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
