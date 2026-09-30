"""MCP over Streamable HTTP with the official SDK client, and the OAuth connector flow."""

import asyncio
import base64
import hashlib
import json
import re
import secrets
import urllib.parse as up

import httpx
import httpx2
import pytest
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from conftest import PASSWORD, audit, login, make_token

EXPECTED_TOOLS = {
    "vaults", "tree", "ls", "read", "search", "grep", "find", "links", "graph", "tags", "agenda", "review",
    "daily_note", "templates", "create", "capture", "write", "edit", "append", "update", "move", "promote",
    "delete", "trash", "restore", "history", "changes", "version", "diff", "restore_version",
    "attachments", "read_attachment", "attach", "delete_attachment",
}


def run_mcp(server, token, calls, headers=None):
    """Open one MCP session and run [(tool, args)] → list of (content_type, text_or_mime)."""
    async def go():
        h = {"Authorization": f"Bearer {token}", **(headers or {})}
        async with httpx2.AsyncClient(headers=h, timeout=60) as hc:
            async with streamable_http_client(server.base + "/mcp", http_client=hc) as st:
                async with ClientSession(st[0], st[1]) as s:
                    await s.initialize()
                    out = {"tools": {t.name: t for t in (await s.list_tools()).tools},
                           "prompts": [p.name for p in (await s.list_prompts()).prompts], "results": []}
                    for name, args in calls:
                        r = await s.call_tool(name, args)
                        c = r.content[0]
                        out["results"].append((c.type, c.text if c.type == "text" else c.mime_type))
                    if calls and calls[-1][0] == "__prompt__":
                        pass
                    return out
    return asyncio.run(go())


def j(text):
    return json.loads(text)


def test_mcp_full_tool_surface(server, wtok, web):
    web.post("/api/forest/pages", json={"name": "MCP Home", "content": "# H\n## Part\nsection body\n"})
    png = base64.b64encode(b"\x89PNG\r\n\x1a\n0000").decode()
    calls = [
        ("vaults", {}),
        ("tree", {"vault": "forest", "depth": 1}),
        ("create", {"name": "Agent Page", "vault": "forest", "content": "first", "tags": ["agent"]}),
        ("read", {"ref": "forest:agent-page"}),
        ("append", {"ref": "forest:agent-page", "text": "appended by agent"}),
        ("edit", {"ref": "forest:agent-page", "old": "first", "new": "FIRST"}),
        ("create", {"name": "Agent Task", "state": "todo", "tags": ["agent"]}),   # no vault -> tasks
        ("update", {"ref": "tasks:agent-task", "state": "in-progress", "due": "2026-12-01", "priority": "low"}),
        ("update", {"ref": "tasks:agent-task", "due": ""}),
        ("read", {"ref": "forest:mcp-home#Part"}),
        ("search", {"query": "appended agent"}),
        ("grep", {"pattern": "^state: in-progress", "context": 1}),
        ("find", {"glob": "*agent*"}),
        ("create", {"name": "Linked", "vault": "tasks", "content": "see [[forest:agent-page]]"}),
        ("links", {"ref": "forest:agent-page"}),
        ("graph", {"ref": "forest:agent-page", "depth": 1}),
        ("tags", {}),
        ("agenda", {"state": "all"}),
        ("review", {"period": "day"}),
        ("daily_note", {}),
        ("templates", {}),
        ("create", {"name": "Sync meeting", "vault": "forest", "template": "meeting"}),
        ("capture", {"text": "remember this", "url": "https://example.org/x"}),
        ("attach", {"ref": "forest:agent-page", "filename": "pic.png", "base64_data": png}),
        ("attachments", {"ref": "forest:agent-page"}),
        ("read_attachment", {"path": "forest:_assets/agent-page/pic.png"}),
        ("attach", {"ref": "forest:agent-page", "filename": "tmp.txt", "base64_data": base64.b64encode(b"bye").decode()}),
        ("delete_attachment", {"path": "forest:_assets/agent-page/tmp.txt"}),
        ("restore", {"shadow_path": "_assets/agent-page/tmp.txt", "vault": "forest"}),
        ("promote", {"ref": "forest:agent-page"}),
        ("move", {"ref": "tasks:linked", "new_parent": None}),
        ("history", {"ref": "forest:agent-page"}),
        ("changes", {"since": "1 hour ago"}),
        ("delete", {"ref": "tasks:linked"}),
        ("trash", {"vault": "tasks"}),
        ("restore", {"shadow_path": "linked.md", "vault": "tasks"}),
        ("write", {"ref": "forest:agent-written.md", "text": "---\nname: Written\n---\nv1\n"}),
        ("write", {"ref": "forest:agent-written.md", "text": "---\nname: Written\n---\nv2\n", "expected_sha": "stale"}),
        ("ls", {"vault": "forest"}),
    ]
    out = run_mcp(server, wtok, calls)
    assert EXPECTED_TOOLS <= set(out["tools"])
    assert {"daily_review", "weekly_review", "skill"} <= set(out["prompts"])
    assert out["tools"]["read"].annotations.read_only_hint is True
    assert out["tools"]["delete"].annotations.destructive_hint is True
    R = dict()
    for (name, _), res in zip(calls, out["results"]):
        R.setdefault(name, []).append(res)
        if res[0] == "text":
            assert not (res[1].startswith("error:") and name != "write"), (name, res)
    assert j(R["vaults"][0][1])["access"] == "write"
    read1 = R["read"][0][1]
    assert "sha=" in read1 and '"url"' in read1 and "[[forest:agent-page]]" in read1
    assert R["read"][1][1].rstrip().endswith("## Part\nsection body")
    assert j(R["search"][0][1])[0]["path"] == "agent-page.md"
    assert "tasks:linked.md" in j(R["links"][0][1])["backlinks"]
    assert R["read_attachment"][0] == ("image", "image/png")
    assert R["write"][1][1].startswith("error:") and "changed" in R["write"][1][1]
    assert "tasks:agent-task.md" in R["create"][1][1]
    assert "sync-meeting" in R["create"][3][1]
    # every call audited, commits authored by the token
    evs = [e for e in audit(web, "mcp.call") if e["who"] == "token:e2e-write"]
    assert len(evs) >= len(calls)
    hist = web.get("/api/forest/history", params={"path": "agent-page/index.md"}).json()
    assert all(h["author"].startswith("token:e2e-write") for h in hist)


def test_mcp_read_only_and_vault_limits(server, web):
    ro = make_token(web, "mcp-ro", "read")
    out = run_mcp(server, ro, [("create", {"name": "nope"}), ("append", {"ref": "forest:mcp-home", "text": "x"}),
                               ("read", {"ref": "forest:mcp-home"})])
    assert out["results"][0][1].startswith("error: this token is read-only")
    assert out["results"][1][1].startswith("error:")
    assert not out["results"][2][1].startswith("error")
    tasks_only = make_token(web, "mcp-tasks", "write", ["tasks"])
    out = run_mcp(server, tasks_only, [("read", {"ref": "forest:mcp-home"}), ("search", {"query": "section body"}),
                                       ("vaults", {}), ("create", {"name": "ok", "vault": "tasks"})])
    assert "no access" in out["results"][0][1]
    assert j(out["results"][1][1]) == []
    assert [v["vault"] for v in j(out["results"][2][1])["vaults"]] == ["tasks"]
    assert "created" in out["results"][3][1]


def test_mcp_readonly_page(server, web, wtok):
    web.post("/api/forest/pages", json={"name": "Frozen", "content": "keep"})
    web.patch("/api/forest/page/frozen.md", json={"extra": {"ai": "readonly"}})
    out = run_mcp(server, wtok, [("append", {"ref": "forest:frozen", "text": "x"}), ("read", {"ref": "forest:frozen"})])
    assert "readonly" in out["results"][0][1]
    assert '"ai": "readonly"' in out["results"][1][1]


def test_mcp_transport_security(server, wtok):
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}
    h = {"Authorization": f"Bearer {wtok}", "Accept": "application/json, text/event-stream"}
    assert httpx.post(server.base + "/mcp", json=init, headers=h).status_code == 200
    assert httpx.post(server.base + "/mcp", json=init, headers={**h, "Origin": "https://evil.example"}).status_code == 403
    assert httpx.post(server.base + "/mcp", json=init, headers={**h, "Authorization": "Bearer fst_bad"}).status_code == 401
    # as it arrives through nginx: public Host header, forwarded proto, claude.ai origin
    proxied = {**h, "Host": "tools.example.com", "X-Forwarded-Proto": "https",
               "X-Forwarded-For": "203.0.113.9", "Origin": "https://claude.ai"}
    assert httpx.post(server.base + "/mcp", json=init, headers=proxied).status_code == 200


def _pkce():
    v = secrets.token_urlsafe(40)
    return v, base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()


def test_oauth_connector_flow(server, web):
    c = httpx.Client(base_url=server.base)
    prm = c.get("/.well-known/oauth-protected-resource").json()
    meta = c.get("/.well-known/oauth-authorization-server").json()
    assert prm["authorization_servers"] == [server.base] and meta["code_challenge_methods_supported"] == ["S256"]
    assert c.post("/oauth/register", json={"redirect_uris": ["http://evil.example/cb"]}).status_code == 400
    reg = c.post("/oauth/register", json={"client_name": "Claude", "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"]}).json()
    ver, ch = _pkce()
    q = {"response_type": "code", "client_id": reg["client_id"], "redirect_uri": reg["redirect_uris"][0],
         "code_challenge": ch, "code_challenge_method": "S256", "state": "st8"}
    assert c.get("/oauth/authorize", params=q).status_code == 302  # login first
    assert c.get("/oauth/authorize", params={**q, "redirect_uri": "https://evil.example/cb"}).status_code == 400
    assert c.get("/oauth/authorize", params={**q, "code_challenge_method": "plain"}).status_code == 302
    b = login(server)

    def consent(**over):
        page = b.get("/oauth/authorize", params=q)
        csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
        form = {**q, "csrf": csrf, "decision": "allow", "grant_scope": "write", "vault": ["forest", "tasks"], **over}
        return b.post("/oauth/authorize", data=form)

    denied = consent(decision="deny")
    assert "error=access_denied" in denied.headers["location"]
    stale = b.post("/oauth/authorize", data={**q, "csrf": "wrong", "decision": "allow"})
    assert stale.status_code == 400
    loc = consent().headers["location"]
    code = up.parse_qs(up.urlparse(loc).query)["code"][0]
    assert up.parse_qs(up.urlparse(loc).query)["state"] == ["st8"]
    tokurl = server.base + "/oauth/token"
    good = {"grant_type": "authorization_code", "code": code, "code_verifier": ver, "client_id": reg["client_id"],
            "redirect_uri": reg["redirect_uris"][0]}
    tok = httpx.post(tokurl, data=good).json()
    assert tok["token_type"] == "Bearer" and tok["refresh_token"]
    assert httpx.post(tokurl, data=good).status_code == 400  # code is single-use
    out = run_mcp(server, tok["access_token"], [("vaults", {})], headers={"Origin": "https://claude.ai"})
    assert j(out["results"][0][1])["access"] == "write"
    new = httpx.post(tokurl, data={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"]}).json()
    assert httpx.get(server.base + "/api/me", headers={"Authorization": "Bearer " + tok["access_token"]}).status_code == 401
    assert httpx.post(tokurl, data={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"]}).status_code == 400
    me = httpx.get(server.base + "/api/me", headers={"Authorization": "Bearer " + new["access_token"]}).json()
    assert me["name"] == "Claude"
    # removing the client revokes its tokens
    web.delete(f"/api/admin/oauth-clients/{reg['client_id']}")
    assert httpx.get(server.base + "/api/me", headers={"Authorization": "Bearer " + new["access_token"]}).status_code == 401
    # PKCE mismatch
    ver2, ch2 = _pkce()
    reg2 = c.post("/oauth/register", json={"client_name": "X", "redirect_uris": ["http://localhost:9/cb"]}).json()
    q.update(client_id=reg2["client_id"], redirect_uri="http://localhost:9/cb", code_challenge=ch2)
    code2 = up.parse_qs(up.urlparse(consent().headers["location"]).query)["code"][0]
    bad = httpx.post(tokurl, data={**good, "code": code2, "client_id": reg2["client_id"], "redirect_uri": "http://localhost:9/cb",
                                   "code_verifier": "wrong"})
    assert bad.status_code == 400
    assert {"oauth.register", "oauth.approved", "oauth.token", "oauth.denied"} <= {e["event"] for e in audit(web, "oauth")}
