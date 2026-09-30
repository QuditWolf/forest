"""Templates, journal, capture/clip, attachments, ai:readonly, mentions, graph, outline, review, ICS, ntfy."""

import time
from datetime import date as _date, datetime, timedelta, timezone


class date(_date):
    @classmethod
    def today(cls):  # the test server runs with FOREST_TZ=UTC
        return datetime.now(timezone.utc).date()

import httpx
import pytest

from conftest import audit, bearer


def _pdf_bytes(text: str) -> bytes:
    import io
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
    w = PdfWriter()
    page = w.add_blank_page(300, 200)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): w._add_object(font)})})
    s = DecodedStreamObject()
    s.set_data(f"BT /F1 12 Tf 20 100 Td ({text}) Tj ET".encode())
    page[NameObject("/Contents")] = w._add_object(s)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def test_templates(agent, web):
    names = {t["template"] for t in agent.get("/api/templates").json()}
    assert {"daily", "weekly-review", "meeting", "project", "task"} <= names
    p = agent.post("/api/forest/pages", json={"name": "Standup", "template": "meeting"}).json()
    assert "# Standup" in p["content"] and date.today().isoformat() in p["content"] and "meeting" in p["tags"]
    t = agent.post("/api/tasks/pages", json={"name": "Tpl task", "template": "task"}).json()
    assert t["state"] == "todo" and t["priority"] == "medium" and "## Done when" in t["content"]
    # user-edited template is used
    web.put("/api/forest/raw/_templates/meeting.md", json={"text": "---\nname: Meeting\n---\nCUSTOM {{title}} {{weekday}}\n"})
    p2 = agent.post("/api/forest/pages", json={"name": "Retro", "template": "meeting"}).json()
    assert p2["content"].startswith("CUSTOM Retro " + datetime.now(timezone.utc).strftime("%A"))
    assert agent.post("/api/forest/pages", json={"name": "x", "template": "nope"}).status_code == 404


def test_daily_notes(agent):
    d = agent.get("/api/daily").json()
    assert d["path"] == f"journal/{date.today().isoformat()}.md" and "## Plan" in d["content"]
    again = agent.get("/api/daily").json()
    assert again["sha"] == d["sha"]  # not recreated
    y = agent.get("/api/daily", params={"date": "2026-01-15"}).json()
    assert y["path"] == "journal/2026-01-15.md" and "Thursday" in y["content"]
    assert agent.get("/api/daily", params={"date": "2020-01-01", "create": False}).status_code == 404
    days = agent.get("/api/daily/days").json()["days"]
    assert "2026-01-15" in days


def test_capture_and_ssrf(agent, reader):
    p = agent.post("/api/capture", json={"url": "https://example.com/a", "title": "Clip", "text": "line1\nline2",
                                        "tags": ["web"]}).json()
    assert p["path"].startswith("inbox/") and p["extra"]["source"] == "https://example.com/a"
    assert "> line1\n> line2" in p["content"]
    t = agent.post("/api/capture", json={"text": "buy stamps", "where": "tasks:", "as_task": True}).json()
    assert t["vault"] == "tasks" and t["state"] == "todo" and t["path"] == "buy-stamps.md"
    for url in ["http://127.0.0.1:1/x", "http://localhost/x", "http://169.254.169.254/latest", "file:///etc/passwd",
                "http://10.0.0.1/", "ftp://example.com"]:
        r = agent.post("/api/capture", json={"url": url, "fetch": True})
        assert r.status_code == 400, url
    assert reader.post("/api/capture", json={"text": "x"}).status_code == 403


@pytest.mark.network
def test_capture_fetch_article(agent):
    try:
        httpx.get("https://example.com", timeout=5)
    except httpx.HTTPError:
        pytest.skip("no internet")
    p = agent.post("/api/capture", json={"url": "https://example.com/", "fetch": True}).json()
    assert "Example Domain" in p["name"] and "documentation examples" in p["content"]


def test_attachments(agent, server, web):
    agent.post("/api/forest/pages", json={"name": "With Files"})
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 20
    a = agent.post("/api/forest/assets", data={"page": "with-files.md"}, files={"file": ("my shot!.png", png, "image/png")}).json()
    assert a["path"] == "_assets/with-files/my-shot.png" and a["markdown"].startswith("![")
    r = agent.get(a["url"])
    assert r.status_code == 200 and r.content == png and "sandbox" in r.headers["content-security-policy"]
    # same name -> no overwrite
    a2 = agent.post("/api/forest/assets", data={"page": "forest:with-files"}, files={"file": ("my shot!.png", png)}).json()
    assert a2["path"] == "_assets/with-files/my-shot-2.png"
    # html can't render inline (stored XSS), svg is sandboxed
    h = agent.post("/api/forest/assets", data={"page": "with-files.md"}, files={"file": ("x.html", b"<script>alert(1)</script>")}).json()
    assert agent.get(h["url"]).headers["content-disposition"].startswith("attachment")
    s = agent.post("/api/forest/assets", data={"page": "with-files.md"}, files={"file": ("x.svg", b"<svg onload=alert(1)/>")}).json()
    assert "sandbox" in agent.get(s["url"]).headers["content-security-policy"]
    # pdf text searchable
    pdf = agent.post("/api/forest/assets", data={"page": "with-files.md"}, files={"file": ("inv.pdf", _pdf_bytes("walrus ledger 42"))}).json()
    assert any(h["path"] == pdf["path"] for h in agent.get("/api/search", params={"q": "walrus ledger"}).json())
    assert any(h["path"] == pdf["path"] for h in agent.get("/api/grep", params={"pattern": "walrus"}).json())
    files = agent.get("/api/forest/assets/with-files.md").json()
    assert {f["path"].split("/")[-1] for f in files} == {"my-shot.png", "my-shot-2.png", "x.html", "x.svg", "inv.pdf"}
    # size limit (1 MB in tests)
    big = agent.post("/api/forest/assets", data={"page": "with-files.md"}, files={"file": ("big.bin", b"0" * (1024 * 1024 + 10))})
    assert big.status_code == 400
    # delete (soft) -> gone, in trash with its pdf text, restorable; search follows
    d = agent.delete(pdf["url"]).json()
    assert d["shadow_path"] == "_assets/with-files/inv.pdf"
    assert agent.get(pdf["url"]).status_code == 404
    assert not agent.get("/api/search", params={"q": "walrus ledger"}).json()
    trash = agent.get("/api/forest/shadow").json()
    assert any(t["shadow_path"] == "_assets/with-files/inv.pdf" and t["kind"] == "attachment" for t in trash)
    assert not any(t["shadow_path"].endswith(".pdf.txt") for t in trash)
    r = agent.post("/api/forest/shadow/restore", json={"shadow_path": "_assets/with-files/inv.pdf"}).json()
    assert r["path"] == "_assets/with-files/inv.pdf" and agent.get(pdf["url"]).status_code == 200
    assert agent.get("/api/search", params={"q": "walrus ledger"}).json()
    assert agent.delete("/api/forest/asset/_assets/with-files/nope.png").status_code == 404
    assert agent.delete("/api/forest/asset/with-files.md").status_code == 400      # pages aren't attachments
    assert httpx.delete(server.base + pdf["url"], headers={"Authorization": "Bearer " + web.post(
        "/api/admin/tokens", json={"name": "ro-asset", "scope": "read"}).json()["token"]}).status_code == 403
    hist = agent.get("/api/forest/history", params={"limit": 5}).json()
    assert any(h["message"].startswith("delete attachment") for h in hist)
    # assets can't be read outside _assets, and need auth
    assert agent.get("/api/forest/asset/with-files.md").status_code == 400
    assert httpx.get(server.base + a["url"]).status_code == 401


def test_ai_readonly(agent, web):
    web.post("/api/forest/pages", json={"name": "Vault Rules", "as_folder": True})
    web.patch("/api/forest/page/vault-rules/index.md", json={"extra": {"ai": "readonly"}})
    web.post("/api/forest/pages", json={"name": "Rule One", "parent_path": "vault-rules/index.md"})
    for r in [agent.post("/api/forest/page/vault-rules/rule-one.md/append", json={"text": "x"}),
              agent.patch("/api/forest/page/vault-rules/rule-one.md", json={"state": "done"}),
              agent.put("/api/forest/raw/vault-rules/rule-two.md", json={"text": "x"}),
              agent.post("/api/forest/pages", json={"name": "sneaky", "parent_path": "vault-rules/index.md"}),
              agent.delete("/api/forest/page/vault-rules/rule-one.md"),
              agent.post("/api/forest/page/vault-rules/rule-one.md/move", json={"new_parent_path": None}),
              agent.post("/api/forest/assets", data={"page": "vault-rules/rule-one.md"}, files={"file": ("a.txt", b"x")}),
              agent.delete(web.post("/api/forest/assets", data={"page": "vault-rules/rule-one.md"},
                                    files={"file": ("mine.txt", b"x")}).json()["url"])]:
        assert r.status_code == 403, r.text
    assert web.post("/api/forest/page/vault-rules/rule-one.md/append", json={"text": "me"}).status_code == 200
    assert any(e["event"] == "ai.readonly_blocked" for e in audit(web))
    # a page can't be moved INTO a readonly folder either
    agent.post("/api/forest/pages", json={"name": "Loose"})
    assert agent.post("/api/forest/page/loose.md/move", json={"new_parent_path": "vault-rules/index.md"}).status_code == 403


def test_mentions_and_link(agent):
    agent.post("/api/forest/pages", json={"name": "Quantum Garden"})
    agent.post("/api/tasks/pages", json={"name": "Water plants", "content": "Visit the quantum garden today. `Quantum Garden` in code."})
    agent.post("/api/forest/pages", json={"name": "Already", "content": "see [[quantum-garden]] Quantum Garden"})
    m = agent.get("/api/forest/mentions/quantum-garden.md").json()
    assert [(x["vault"], x["path"]) for x in m] == [("tasks", "water-plants.md")]
    r = agent.post("/api/forest/page/quantum-garden.md/link-mention", json={"source_vault": "tasks", "source_path": "water-plants.md"}).json()
    assert "[[forest:quantum-garden|quantum garden]]" in r["content"] and "`Quantum Garden`" in r["content"]
    assert agent.get("/api/forest/mentions/quantum-garden.md").json() == []
    assert "tasks:water-plants.md" in agent.get("/api/forest/backlinks/quantum-garden.md").json()


def test_graph_outline_section(agent, server, web):
    agent.post("/api/forest/pages", json={"name": "Hub", "content": "# Top\n## Alpha\nA text [[spoke]]\n## Beta\nB text\n### Beta sub\nC\n```\n# not a heading\n```"})
    agent.post("/api/forest/pages", json={"name": "Spoke", "content": "[[tasks:far]]"})
    agent.post("/api/tasks/pages", json={"name": "Far"})
    g = agent.get("/api/forest/graph/hub.md", params={"depth": 2}).json()
    ids = {n["id"] for n in g["nodes"]}
    assert {"forest:hub.md", "forest:spoke.md", "tasks:far.md"} <= ids and g["center"] == "forest:hub.md"
    g1 = agent.get("/api/forest/graph/hub.md", params={"depth": 1}).json()
    assert "tasks:far.md" not in {n["id"] for n in g1["nodes"]}
    # vault-limited token doesn't see other vault nodes
    tok = bearer(server, web.post("/api/admin/tokens", json={"name": "fo", "vaults": ["forest"]}).json()["token"])
    assert "tasks:far.md" not in {n["id"] for n in tok.get("/api/forest/graph/hub.md").json()["nodes"]}
    out = agent.get("/api/forest/outline/hub.md").json()
    assert [h["text"] for h in out] == ["Top", "Alpha", "Beta", "Beta sub"]
    sec = agent.get("/api/forest/section/hub.md", params={"heading": "Beta"}).json()["text"]
    assert "B text" in sec and "Beta sub" in sec and "Alpha" not in sec


def test_review(agent):
    r = agent.get("/api/review", params={"period": "week"}).json()
    assert {"overdue", "due_soon", "in_progress", "stale", "done", "inbox_count", "changes"} <= set(r)
    assert r["inbox_count"] >= 1 and r["changes"]["by_agents"] >= 1


def test_calendar_ics(server, agent, rtok):
    agent.post("/api/tasks/pages", json={"name": "Dentist, 9am; bring card", "state": "todo", "due": "2026-11-03"})
    r = httpx.get(server.base + "/api/calendar.ics", params={"token": rtok})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/calendar")
    assert "DTSTART;VALUE=DATE:20261103" in r.text and "Dentist\\, 9am\\; bring card" in r.text
    assert httpx.get(server.base + "/api/calendar.ics").status_code == 401
    # query token only accepted on the calendar path
    assert httpx.get(server.base + "/api/forest/tree", params={"token": rtok}).status_code == 401


def test_skill_served(reader):
    r = reader.get("/api/skill")
    assert r.status_code == 200 and "## Tools" in r.text and "ai: readonly" in r.text


def test_ntfy(web, agent, ntfy, server):
    ntfy.messages.clear()
    assert web.post("/api/admin/ntfy-test", params={"kind": "test"}).status_code == 200
    assert ntfy.messages[-1]["topic"] == "forest-test" and ntfy.messages[-1]["auth"] == "Bearer tk_ntfy"
    web.post("/api/admin/ntfy-test", params={"kind": "digest"})
    assert "Inbox:" in ntfy.messages[-1]["message"]
    web.post("/api/admin/ntfy-test", params={"kind": "weekly"})
    assert "Done this week" in ntfy.messages[-1]["message"]
    # per-page reminder with action buttons
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M")
    agent.post("/api/tasks/pages", json={"name": "Remind me", "state": "todo"})
    agent.patch("/api/tasks/page/remind-me.md", json={"extra": {"remind": past}})
    web.post("/api/admin/ntfy-test", params={"kind": "reminders"})
    msg = [m for m in ntfy.messages if m["title"] == "reminder: Remind me"]
    assert len(msg) == 1 and [a["label"] for a in msg[0]["actions"]] == ["done", "snooze 1d"]
    web.post("/api/admin/ntfy-test", params={"kind": "reminders"})  # not sent twice
    assert len([m for m in ntfy.messages if m["title"] == "reminder: Remind me"]) == 1
    # action buttons work without login; tampered links don't
    done_url = msg[0]["actions"][0]["url"]
    assert httpx.post(done_url).json()["ok"]
    assert agent.get("/api/tasks/page/remind-me.md").json()["state"] == "done"
    assert httpx.post(done_url[:-3] + "abc").status_code == 400
    snooze = msg[0]["actions"][1]["url"]
    httpx.post(snooze)
    assert agent.get("/api/tasks/page/remind-me.md").json()["extra"]["remind"] > past
    # non-admin can't trigger
    assert agent.post("/api/admin/ntfy-test").status_code == 403
