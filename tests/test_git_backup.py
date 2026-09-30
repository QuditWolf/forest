"""git smart-HTTP sync (clone/pull/push, permissions, non-fast-forward) and backup/restore."""

import io
import subprocess
import tarfile

import httpx

from conftest import audit, make_token


def git(*args, cwd=None, check=True):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                       env={"GIT_TERMINAL_PROMPT": "0", "PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": "/tmp",
                            "GIT_AUTHOR_NAME": "laptop", "GIT_AUTHOR_EMAIL": "l@x", "GIT_COMMITTER_NAME": "laptop",
                            "GIT_COMMITTER_EMAIL": "l@x"})
    if check:
        assert r.returncode == 0, r.stderr
    return r


def url(server, token, vault="tasks"):
    return f"http://u:{token}@127.0.0.1:{server.port}/git/{vault}.git"


def test_git_clone_push_pull(server, agent, rtok, wtok, tmp_path, web):
    agent.post("/api/tasks/pages", json={"name": "From server", "state": "todo"})
    git("clone", "-q", url(server, rtok), str(tmp_path / "ro"))
    assert (tmp_path / "ro" / "from-server.md").exists()
    # read token can't push
    (tmp_path / "ro" / "offline.md").write_text("---\nname: Offline\nstate: todo\n---\nwritten offline\n")
    git("add", "-A", cwd=tmp_path / "ro"); git("commit", "-qm", "offline", cwd=tmp_path / "ro")
    assert git("push", "-q", "origin", "main", cwd=tmp_path / "ro", check=False).returncode != 0
    # write token can; server working tree updates and API sees it
    git("push", "-q", url(server, wtok), "main", cwd=tmp_path / "ro")
    p = agent.get("/api/tasks/page/offline.md").json()
    assert "written offline" in p["content"]
    # server-side edit, then pull
    agent.post("/api/tasks/page/offline.md/append", json={"text": "server added"})
    git("pull", "-q", "--no-rebase", url(server, rtok), "main", cwd=tmp_path / "ro")
    assert "server added" in (tmp_path / "ro" / "offline.md").read_text()
    # non-fast-forward push is rejected (no history rewriting)
    git("commit", "-q", "--amend", "-m", "rewritten", cwd=tmp_path / "ro")
    assert git("push", "-q", "-f", url(server, wtok), "main", cwd=tmp_path / "ro", check=False).returncode != 0
    # vault-limited token can't clone other vaults; wrong password fails
    tok_forest = make_token(web, "git-forest-only", "read", ["forest"])
    assert git("clone", "-q", url(server, tok_forest, "tasks"), str(tmp_path / "x"), check=False).returncode != 0
    assert git("clone", "-q", url(server, "fst_wrong"), str(tmp_path / "y"), check=False).returncode != 0
    assert any(e["event"] == "git.push" for e in audit(web))


def test_backup_and_restore(server, web, agent, reader, tmp_path):
    agent.post("/api/forest/pages", json={"name": "Before backup", "content": "keep me"})
    r = reader.get("/api/backup")
    assert r.status_code == 200 and r.headers["content-type"] == "application/gzip"
    tf = tarfile.open(fileobj=io.BytesIO(r.content))
    names = tf.getnames()
    assert "manifest.json" in names and "vaults/forest/before-backup.md" in names
    assert any(n.startswith("vaults/forest/.git/") for n in names)
    assert not any(".forest" in n or "tokens" in n for n in names)  # no secrets in backups
    # vault-limited token can't pull a full backup
    lim = httpx.get(server.base + "/api/backup", headers={"Authorization": "Bearer " + make_token(web, "bk", "read", ["tasks"])})
    assert lim.status_code == 403
    # change something, restore, check it's back and the old state was moved aside
    agent.delete("/api/forest/page/before-backup.md")
    assert agent.get("/api/forest/page/before-backup.md").status_code == 404
    res = web.post("/api/admin/restore", files={"file": ("b.tar.gz", r.content, "application/gzip")}).json()
    assert set(res["restored"]) == {"forest", "tasks"}
    assert agent.get("/api/forest/page/before-backup.md").json()["content"].strip() == "keep me"
    assert (server.data / ".forest" / "pre-restore").exists()
    # history survived
    assert web.get("/api/forest/history", params={"limit": 5}).json()
    # restore needs admin; malicious archives rejected
    assert agent.post("/api/admin/restore", files={"file": ("b.tar.gz", r.content)}).status_code == 403
    evil = io.BytesIO()
    with tarfile.open(fileobj=evil, mode="w:gz") as t:
        data = b'{"vaults": ["forest"]}'
        ti = tarfile.TarInfo("manifest.json"); ti.size = len(data); t.addfile(ti, io.BytesIO(data))
        ti = tarfile.TarInfo("../../escape.txt"); ti.size = 1; t.addfile(ti, io.BytesIO(b"x"))
    assert web.post("/api/admin/restore", files={"file": ("e.tar.gz", evil.getvalue())}).status_code == 400
    assert not (server.data.parent / "escape.txt").exists()
    assert web.post("/api/admin/restore", files={"file": ("n.tar.gz", b"not a tar")}).status_code in (400, 500)
