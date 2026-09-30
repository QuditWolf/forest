"""Web login, tokens, access control, attack attempts, audit log."""

import httpx
import pytest

from conftest import PASSWORD, audit, bearer, login, make_token


def test_unauthenticated_is_blocked(server):
    c = httpx.Client(base_url=server.base)
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].startswith("/auth/login")
    for path in ["/tasks", "/admin", "/clip"]:
        assert c.get(path, follow_redirects=False).status_code == 302
    for path in ["/api/me", "/api/forest/tree", "/api/tree", "/api/search?q=x", "/api/backup", "/api/admin/tokens"]:
        assert c.get(path).status_code == 401, path
    r = c.post("/mcp", json={})
    assert r.status_code == 401 and "resource_metadata=" in r.headers["www-authenticate"]
    r = c.get("/git/forest.git/info/refs?service=git-upload-pack")
    assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Basic")
    # public bits
    assert c.get("/healthz").json() == {"ok": True}
    assert c.get("/lib/marked.min.js").status_code == 200
    assert c.get("/manifest.webmanifest").json()["share_target"]["action"] == "/clip"


def test_login_flow_and_cookie(server):
    c = httpx.Client(base_url=server.base)
    r = c.post("/auth/login", data={"password": "wrong", "next": "/"})
    assert r.status_code == 303 and "incorrect" in r.headers["location"]
    r = c.post("/auth/login", data={"password": PASSWORD, "next": "/tasks#tasks/x.md"})
    assert r.status_code == 303 and r.headers["location"] == "/tasks#tasks/x.md"
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie
    assert c.get("/api/me").json()["kind"] == "session"
    c.get("/auth/logout")
    assert c.get("/api/me").status_code == 401


@pytest.mark.parametrize("nxt", ["//evil.com", "https://evil.com", "/\\evil.com", "javascript:alert(1)"])
def test_no_open_redirect(server, nxt):
    c = httpx.Client(base_url=server.base)
    r = c.post("/auth/login", data={"password": PASSWORD, "next": nxt})
    assert r.headers["location"] == "/"


def test_login_page_escapes_params(server):
    r = httpx.get(server.base + "/auth/login", params={"error": "<script>x</script>", "next": '"><img src=x>'})
    assert "<script>x</script>" not in r.text and '"><img' not in r.text


def test_csrf_header_required_for_session_writes(server, web):
    c = httpx.Client(base_url=server.base, cookies=web.cookies)
    r = c.post("/api/forest/pages", json={"name": "csrf"})
    assert r.status_code == 403
    assert web.post("/api/forest/pages", json={"name": "csrf ok"}).status_code == 201


def test_security_headers(web):
    r = web.get("/")
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert "cdn" not in r.text.lower() or "jsdelivr" not in r.text  # all JS vendored


def test_token_scopes_and_limits(server, web):
    ro = bearer(server, make_token(web, "ro", "read"))
    rw_tasks = bearer(server, make_token(web, "rw-tasks", "write", ["tasks"]))
    assert ro.get("/api/forest/tree").status_code == 200
    assert ro.post("/api/forest/pages", json={"name": "x"}).status_code == 403
    assert rw_tasks.get("/api/forest/tree").status_code == 403
    assert rw_tasks.post("/api/tasks/pages", json={"name": "scoped task"}).status_code == 201
    # cross-vault reads are filtered to allowed vaults
    web.post("/api/forest/pages", json={"name": "secret note", "content": "zebra-unique-word"})
    assert rw_tasks.get("/api/search", params={"q": "zebra-unique-word"}).json() == []
    assert rw_tasks.get("/api/search", params={"q": "zebra-unique-word", "vault": "forest"}).status_code == 403
    assert ro.get("/api/search", params={"q": "zebra-unique-word"}).json()
    # tokens can't reach admin or UI
    assert ro.get("/api/admin/tokens").status_code == 403
    assert ro.get("/admin").status_code == 403
    assert ro.get("/").status_code == 403
    assert ro.get("/api/me").json()["admin"] is False


def test_bad_expired_and_revoked_tokens(server, web):
    assert bearer(server, "fst_nope").get("/api/me").status_code == 401
    assert bearer(server, "garbage").get("/api/me").status_code == 401
    tok = make_token(web, "temp")
    c = bearer(server, tok)
    assert c.get("/api/me").status_code == 200
    tid = [t for t in web.get("/api/admin/tokens").json() if t["name"] == "temp"][0]["id"]
    assert web.delete(f"/api/admin/tokens/{tid}").status_code == 204
    assert c.get("/api/me").status_code == 401
    listed = web.get("/api/admin/tokens").json()
    assert all("hash" not in t and "token" not in t for t in listed)  # never leak secrets


def test_path_traversal_and_git_dir_blocked(agent):
    bad = ["../../etc/passwd", "..%2F..%2Fetc%2Fpasswd", ".git/config", "a/../../../x.md", "/etc/passwd"]
    for p in bad:
        for r in [agent.get(f"/api/forest/page/{p}"),
                  agent.put(f"/api/forest/raw/{p}", json={"text": "x"}),
                  agent.get(f"/api/forest/asset/{p}")]:
            assert r.status_code in (400, 403, 404, 422), (p, r.status_code, r.text)
    assert agent.get("/api/forest/asset/_assets/../.git/config").status_code in (400, 404)
    assert agent.get("/api/forest/version/HEAD/../../x").status_code in (400, 404)
    assert agent.get("/api/forest/diff/--output=x").status_code == 400
    assert agent.get("/api/nosuchvault/tree").status_code == 404


def test_login_rate_limit(server):
    c = httpx.Client(base_url=server.base, headers={"X-Forwarded-For": "1.2.3.4"})
    for _ in range(5):
        c.post("/auth/login", data={"password": "nope", "next": "/"})
    r = c.post("/auth/login", data={"password": PASSWORD, "next": "/"})
    assert "too+many+attempts" in r.headers["location"]


def test_audit_log(web, reader):
    reader.get("/api/forest/tree")
    evs = audit(web)
    kinds = {e["event"] for e in evs}
    assert {"login.ok", "login.fail", "token.create", "token.revoke", "http", "auth.bad_token"} <= kinds
    assert any(e["event"] == "http" and e["who"] == "token:e2e-read" for e in evs)


def test_refuses_to_start_without_password(tmp_path):
    import os, subprocess, sys
    from conftest import ROOT
    env = {**os.environ, "FOREST_CONFIG": "/dev/null", "FOREST_DATA": str(tmp_path), "FOREST_PUBLIC_URL": "https://x.example",
           "PYTHONPATH": str(ROOT)}
    env.pop("FOREST_PASSWORD", None)
    r = subprocess.run([sys.executable, "-m", "uvicorn", "forest.api:app", "--port", "1"], env=env, cwd=ROOT,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode != 0 and "FOREST_PASSWORD is not set" in r.stderr


def test_regex_length_capped(agent):
    assert agent.get("/api/grep", params={"pattern": "(a+)+" * 100}).status_code == 400


def test_cookie_secure_follows_scheme(server):
    """Direct http:// (VPN IP) gets a plain cookie so login works; HTTPS via nginx gets Secure."""
    c = httpx.Client(base_url=server.base)
    plain = c.post("/auth/login", data={"password": PASSWORD, "next": "/"}).headers["set-cookie"].lower()
    assert "secure" not in plain
    via_nginx = httpx.Client(base_url=server.base, headers={"X-Forwarded-Proto": "https", "X-Forwarded-For": "5.6.7.8"})
    sec = via_nginx.post("/auth/login", data={"password": PASSWORD, "next": "/"}).headers["set-cookie"].lower()
    assert "; secure" in sec and "httponly" in sec
    # and the plain-http session actually works for the UI + API
    assert c.get("/api/me").json()["kind"] == "session"
    assert c.get("/", follow_redirects=False).status_code == 200


def test_bad_timezone_gives_clear_error(tmp_path):
    import os, subprocess, sys
    from conftest import ROOT
    env = {**os.environ, "FOREST_CONFIG": "/dev/null", "FOREST_DATA": str(tmp_path), "FOREST_PASSWORD": "x",
           "FOREST_TZ": "IST", "PYTHONPATH": str(ROOT)}
    r = subprocess.run([sys.executable, "-m", "uvicorn", "forest.api:app", "--port", "1"], env=env, cwd=ROOT,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode != 0 and "Asia/Kolkata" in r.stderr and "Traceback" not in r.stderr.split("FOREST_TZ")[0][-300:]
