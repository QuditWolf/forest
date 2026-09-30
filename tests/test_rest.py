"""REST API: page lifecycle, both vaults, links/tags/agenda/search, history, conflicts, concurrency."""

import concurrent.futures as cf
import subprocess
from datetime import date as _date, datetime, timedelta, timezone


class date(_date):
    @classmethod
    def today(cls):
        return datetime.now(timezone.utc).date()

from conftest import audit


def test_page_lifecycle(agent, server):
    r = agent.post("/api/forest/pages", json={"name": "Proj A", "as_folder": True})
    assert r.status_code == 201 and r.json()["path"] == "proj-a/index.md"
    p = agent.post("/api/forest/pages", json={"name": "Design Doc", "parent_path": "proj-a", "content": "hello",
                                             "state": "todo", "priority": "high", "tags": ["x", "#y"]}).json()
    assert p["path"] == "proj-a/design-doc.md" and p["tags"] == ["x", "y"]
    # leaf as parent auto-promotes
    c = agent.post("/api/forest/pages", json={"name": "Child", "parent_path": p["path"]}).json()
    assert c["path"] == "proj-a/design-doc/child.md"
    parent = agent.get("/api/forest/page/proj-a/design-doc").json()
    assert parent["is_folder"] and parent["children"] == ["proj-a/design-doc/child.md"]
    # lookups: no .md, short id, unique name
    sid = parent["short_id"]
    assert agent.get(f"/api/forest/page/{sid}").json()["path"] == parent["path"]
    assert agent.get("/api/forest/page/Child").json()["path"] == c["path"]
    # update + done stamps completed; unknown frontmatter kept
    u = agent.patch(f"/api/forest/page/{c['path']}", json={"state": "done", "extra": {"custom": 7}}).json()
    assert u["completed"] == date.today().isoformat() and u["extra"]["custom"] == 7
    u = agent.patch(f"/api/forest/page/{c['path']}", json={"name": "Child renamed"}).json()
    assert u["extra"]["custom"] == 7 and u["completed"]
    # invalid values
    assert agent.patch(f"/api/forest/page/{c['path']}", json={"state": "bogus"}).status_code in (400, 422)
    # append, raw, edit
    agent.post(f"/api/forest/page/{c['path']}/append", json={"text": "note one"})
    raw = agent.get(f"/api/forest/page/{c['path']}", params={"raw": True}).json()
    assert "note one" in raw["text"] and raw["text"].startswith("---")
    e = agent.post(f"/api/forest/page/{c['path']}/edit", json={"old": "note one", "new": "note ONE", "expected_sha": raw["sha"]})
    assert e.status_code == 200
    assert agent.post(f"/api/forest/page/{c['path']}/edit", json={"old": "absent", "new": "x"}).status_code == 400
    # move folder with children
    m = agent.post(f"/api/forest/page/{parent['path']}/move", json={"new_parent_path": None}).json()
    assert m["path"] == "design-doc/index.md" and m["children"] == ["design-doc/child.md"]
    assert agent.post("/api/forest/page/design-doc/index.md/move", json={"new_parent_path": "design-doc/child.md"}).status_code == 400
    # delete folder -> shadow -> restore with children
    assert agent.delete("/api/forest/page/design-doc/index.md").status_code == 204
    assert agent.get("/api/forest/page/design-doc/child.md").status_code == 404
    sh = agent.get("/api/forest/shadow").json()
    assert any(s["shadow_path"] == "design-doc" for s in sh)
    rs = agent.post("/api/forest/shadow/restore", json={"shadow_path": "design-doc"}).json()
    assert rs["children"] == ["design-doc/child.md"]
    # raw write creates missing ancestor folder-pages
    w = agent.put("/api/forest/raw/deep/a/b.md", json={"text": "---\nname: B\n---\nbody\n"})
    assert w.status_code == 200
    tree = agent.get("/api/forest/tree").json()
    assert any(n["path"] == "deep/index.md" for n in tree)
    assert agent.put("/api/forest/raw/x.txt", json={"text": "x"}).status_code == 400
    assert agent.put("/api/forest/raw/bad.md", json={"text": "---\nstate: nope\n---\n"}).status_code == 400


def test_conflict_detection(agent, web):
    p = agent.post("/api/forest/pages", json={"name": "Shared", "content": "v1"}).json()
    web.patch(f"/api/forest/page/{p['path']}", json={"content": "web edit"})
    r = agent.patch(f"/api/forest/page/{p['path']}", json={"content": "agent edit", "expected_sha": p["sha"]})
    assert r.status_code == 409
    assert agent.get(f"/api/forest/page/{p['path']}").json()["content"].strip() == "web edit"


def test_cross_vault_links_tags_agenda(agent):
    t = agent.post("/api/tasks/pages", json={"name": "Ship It", "state": "in-progress", "priority": "high",
                                            "due": (date.today() - timedelta(days=1)).isoformat()}).json()
    n = agent.post("/api/forest/pages", json={"name": "Launch Notes",
                                             "content": "Plan for [[tasks:ship-it|shipping]] #launch/q4 and `#notatag`\n```\n#nottag [[nolink]]\n```"}).json()
    assert n["inline_tags"] == ["launch/q4"]
    links = agent.get(f"/api/forest/links/{n['path']}").json()
    assert links == [{"ref": "tasks:ship-it", "vault": "tasks", "path": "ship-it.md", "exists": True}]
    assert agent.get(f"/api/tasks/backlinks/{t['path']}").json() == ["forest:launch-notes.md"]
    assert agent.get("/api/resolve", params={"ref": "tasks:ship-it"}).json() == {"vault": "tasks", "path": "ship-it.md"}
    assert any(x["tag"] == "launch/q4" for x in agent.get("/api/tags").json())
    assert [p["path"] for p in agent.get("/api/tags/launch").json()] == ["launch-notes.md"]
    over = agent.get("/api/agenda", params={"overdue": True}).json()
    assert any(p["path"] == "ship-it.md" for p in over)
    assert agent.get("/api/agenda", params={"vault": "forest", "overdue": True}).json() == [] or \
        all(p["vault"] == "forest" for p in agent.get("/api/agenda", params={"vault": "forest"}).json())
    s = agent.get("/api/search", params={"q": "shipping plan"}).json()
    assert s and s[0]["path"] == "launch-notes.md"
    g = agent.get("/api/grep", params={"pattern": "^state: in-progress"}).json()
    assert any(h["path"] == "ship-it.md" for h in g)
    assert agent.get("/api/grep", params={"pattern": "("}).status_code == 400
    assert "tasks:ship-it.md" in agent.get("/api/find", params={"glob": "*ship*"}).json()
    # legacy single-vault paths still work
    assert agent.get("/api/tree").status_code == 200


def test_history_diff_restore(agent):
    p = agent.post("/api/forest/pages", json={"name": "Versioned", "content": "one"}).json()
    agent.patch(f"/api/forest/page/{p['path']}", json={"content": "two"})
    h = agent.get("/api/forest/history", params={"path": p["path"]}).json()
    assert len(h) >= 2 and h[0]["author"] == "token:e2e-write"
    old = h[-1]["rev"]
    assert "one" in agent.get(f"/api/forest/version/{old}/{p['path']}").json()["text"]
    assert "two" in agent.get(f"/api/forest/diff/{h[0]['rev']}", params={"path": p["path"]}).json()["diff"]
    r = agent.post(f"/api/forest/page/{p['path']}/restore-version", json={"rev": old}).json()
    assert r["content"].strip() == "one"
    ch = agent.get("/api/changes", params={"since": "1 hour ago"}).json()
    assert any(c["vault"] == "forest" for c in ch)


def test_concurrent_writes_lose_nothing(agent, server):
    p = agent.post("/api/forest/pages", json={"name": "Busy"}).json()
    with cf.ThreadPoolExecutor(8) as ex:
        list(ex.map(lambda i: agent.post(f"/api/forest/page/{p['path']}/append", json={"text": f"line-{i}"}), range(20)))
    content = agent.get(f"/api/forest/page/{p['path']}").json()["content"]
    assert all(f"line-{i}" in content for i in range(20))
    root = server.data / "vaults" / "forest"
    assert subprocess.run(["git", "-C", str(root), "status", "--porcelain"], capture_output=True, text=True).stdout == ""


def test_writes_are_audited(web, agent):
    agent.post("/api/forest/pages", json={"name": "audited"})
    assert any(e["event"] == "http" and e["method"] == "POST" and e["who"] == "token:e2e-write" for e in audit(web))
