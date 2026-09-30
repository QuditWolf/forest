"""Thin wrapper around the git CLI. Every vault is a git repo; every write is a commit."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import List, Optional

_ENV = {
    "GIT_COMMITTER_NAME": "forest",
    "GIT_COMMITTER_EMAIL": "forest@localhost",
    "GIT_TERMINAL_PROMPT": "0",
}


class GitError(RuntimeError):
    pass


def _git(root: Path, *args: str, check: bool = True, input: bytes | None = None) -> str:
    env = {**os.environ, **_ENV}
    r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, env=env, input=input)
    if check and r.returncode != 0:
        raise GitError(r.stderr.decode(errors="replace").strip() or f"git {args[0]} failed")
    return r.stdout.decode(errors="replace")


def ensure_repo(root: Path) -> None:
    """Init repo if missing and set config needed for push-over-HTTP sync."""
    root.mkdir(parents=True, exist_ok=True)
    if not (root / ".git").exists():
        _git(root, "init", "-q", "-b", "main")
    for k, v in [
        ("user.name", "forest"),
        ("user.email", "forest@localhost"),
        ("receive.denyCurrentBranch", "updateInstead"),  # pushes update the working tree
        ("receive.denyNonFastForwards", "true"),         # no force-push rewriting history
        ("http.receivepack", "true"),
        ("core.quotePath", "false"),
    ]:
        _git(root, "config", k, v)
    # Pick up anything changed on disk while the server was down
    commit(root, "sync: external changes", author="forest")


def commit(root: Path, message: str, author: str = "forest") -> Optional[str]:
    """Stage everything and commit. Returns new commit hash, or None if nothing changed."""
    _git(root, "add", "-A")
    if not _git(root, "status", "--porcelain"):
        return None
    safe = "".join(c for c in author if c.isalnum() or c in "-_.:@ ") or "forest"
    _git(root, "commit", "-q", "-m", message, f"--author={safe} <{safe.replace(' ', '_')}@forest>")
    return _git(root, "rev-parse", "HEAD").strip()


_SEP = "\x1f"


def log(root: Path, path: Optional[str] = None, limit: int = 50, since: Optional[str] = None) -> List[dict]:
    args = ["log", f"-n{int(limit)}", f"--format=%H{_SEP}%an{_SEP}%aI{_SEP}%s", "--name-status"]
    if since:
        args.append(f"--since={since}")
    if path:
        # no --follow: its rename guessing mixes up small, similar pages (e.g. fresh frontmatter-only files)
        args += ["--", path]
    try:
        out = _git(root, *args)
    except GitError:
        return []  # empty repo
    entries: List[dict] = []
    for line in out.splitlines():
        if _SEP in line:
            h, an, at, s = line.split(_SEP, 3)
            entries.append({"rev": h, "short": h[:8], "author": an, "date": at, "message": s, "files": []})
        elif line.strip() and entries:
            parts = line.split("\t")
            entries[-1]["files"].append({"status": parts[0], "path": parts[-1]})
    return entries


def show_file(root: Path, rev: str, path: str) -> str:
    _check_rev(rev)
    return _git(root, "show", f"{rev}:{path}")


def diff(root: Path, rev: str, path: Optional[str] = None) -> str:
    _check_rev(rev)
    args = ["show", "--format=%H %an %aI%n%s%n", rev]
    if path:
        args += ["--", path]
    return _git(root, *args)


def _check_rev(rev: str) -> None:
    if not rev or rev.startswith("-") or not all(c.isalnum() or c in "^~" for c in rev):
        raise GitError(f"Invalid revision: {rev!r}")
