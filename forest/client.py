"""HTTP client for a (remote) Forest server - used by the CLI and TUI.

Config: ~/.config/forest/client.toml   (or env FOREST_URL / FOREST_TOKEN / FOREST_DEFAULT_VAULT)
    url   = "https://tools.example.com"
    token = "fst_..."
    vault = "forest"
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

import httpx

CONFIG_PATH = Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser() / "forest" / "client.toml"


class ClientError(Exception):
    pass


def load_config() -> dict:
    cfg: dict = {}
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "rb") as f:
            cfg = tomllib.load(f)
    cfg["url"] = os.environ.get("FOREST_URL") or cfg.get("url") or "http://127.0.0.1:7000"
    cfg["token"] = os.environ.get("FOREST_TOKEN") or cfg.get("token") or ""
    cfg["vault"] = os.environ.get("FOREST_DEFAULT_VAULT") or cfg.get("vault") or "forest"
    return cfg


def save_config(url: str, token: str, vault: str = "forest") -> Path:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(f'url = "{url.rstrip("/")}"\ntoken = "{token}"\nvault = "{vault}"\n')
    CONFIG_PATH.chmod(0o600)
    return CONFIG_PATH


class Forest:
    def __init__(self, url: Optional[str] = None, token: Optional[str] = None, vault: Optional[str] = None):
        cfg = load_config()
        self.url = (url or cfg["url"]).rstrip("/")
        self.token = token or cfg["token"]
        self.default_vault = vault or cfg["vault"]
        headers = {"X-Requested-With": "forest"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        self.http = httpx.Client(base_url=self.url, headers=headers, timeout=30)

    # ── plumbing ──

    def _req(self, method: str, path: str, **kw) -> Any:
        try:
            r = self.http.request(method, path, **kw)
        except httpx.HTTPError as e:
            raise ClientError(f"cannot reach {self.url}: {e}")
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail", r.text)
            except ValueError:
                detail = r.text
            raise ClientError(f"{r.status_code}: {detail}")
        if r.status_code == 204 or not r.content:
            return None
        return r.json()

    def split(self, ref: str, vault: Optional[str] = None) -> tuple[str, str]:
        """'tasks:work/x' → ('tasks','work/x'); plain refs use the given/default vault."""
        if ":" in ref:
            v, rest = ref.split(":", 1)
            if v and "/" not in v:
                return v, rest
        return vault or self.default_vault, ref

    @staticmethod
    def q(path: str) -> str:
        return quote(path, safe="/")

    # ── global ──

    def me(self): return self._req("GET", "/api/me")
    def vaults(self): return self._req("GET", "/api/vaults")
    def search(self, q: str, vault: Optional[str] = None): return self._req("GET", "/api/search", params={"q": q, "vault": vault})
    def grep(self, pattern: str, **kw): return self._req("GET", "/api/grep", params={"pattern": pattern, **{k: v for k, v in kw.items() if v is not None}})
    def tags(self, vault: Optional[str] = None): return self._req("GET", "/api/tags", params={"vault": vault})
    def tagged(self, tag: str, vault: Optional[str] = None): return self._req("GET", f"/api/tags/{self.q(tag)}", params={"vault": vault})
    def agenda(self, **kw): return self._req("GET", "/api/agenda", params={k: v for k, v in kw.items() if v is not None})
    def changes(self, since: str = "7 days ago", vault: Optional[str] = None): return self._req("GET", "/api/changes", params={"since": since, "vault": vault})
    def resolve(self, ref: str, from_vault: str, from_path: Optional[str] = None):
        return self._req("GET", "/api/resolve", params={"ref": ref, "from_vault": from_vault, "from_path": from_path})

    # ── per vault ──

    def tree(self, vault: str): return self._req("GET", f"/api/{vault}/tree")
    def pages(self, vault: str): return self._req("GET", f"/api/{vault}/pages")
    def page(self, vault: str, path: str): return self._req("GET", f"/api/{vault}/page/{self.q(path)}")
    def raw(self, vault: str, path: str): return self._req("GET", f"/api/{vault}/page/{self.q(path)}", params={"raw": "true"})
    def create(self, vault: str, **body): return self._req("POST", f"/api/{vault}/pages", json=body)
    def update(self, vault: str, path: str, **fields): return self._req("PATCH", f"/api/{vault}/page/{self.q(path)}", json=fields)
    def write_raw(self, vault: str, path: str, text: str, expected_sha: Optional[str] = None):
        return self._req("PUT", f"/api/{vault}/raw/{self.q(path)}", json={"text": text, "expected_sha": expected_sha})
    def append(self, vault: str, path: str, text: str): return self._req("POST", f"/api/{vault}/page/{self.q(path)}/append", json={"text": text})
    def move(self, vault: str, path: str, new_parent: Optional[str]): return self._req("POST", f"/api/{vault}/page/{self.q(path)}/move", json={"new_parent_path": new_parent})
    def promote(self, vault: str, path: str): return self._req("POST", f"/api/{vault}/page/{self.q(path)}/promote")
    def delete(self, vault: str, path: str): return self._req("DELETE", f"/api/{vault}/page/{self.q(path)}")
    def backlinks(self, vault: str, path: str): return self._req("GET", f"/api/{vault}/backlinks/{self.q(path)}")
    def links(self, vault: str, path: str): return self._req("GET", f"/api/{vault}/links/{self.q(path)}")
    def history(self, vault: str, path: Optional[str] = None, limit: int = 30):
        return self._req("GET", f"/api/{vault}/history", params={"path": path, "limit": limit})
    def shadow(self, vault: str): return self._req("GET", f"/api/{vault}/shadow")
    def restore_shadow(self, vault: str, shadow_path: str): return self._req("POST", f"/api/{vault}/shadow/restore", json={"shadow_path": shadow_path})

    def download_backup(self, dest: Path) -> Path:
        with self.http.stream("GET", "/api/backup", timeout=300) as r:
            if r.status_code >= 400:
                r.read()
                raise ClientError(f"{r.status_code}: {r.text}")
            cd = r.headers.get("content-disposition", "")
            name = cd.split("filename=")[-1].strip('"') if "filename=" in cd else "forest-backup.tar.gz"
            out = dest / name if dest.is_dir() else dest
            with open(out, "wb") as f:
                for chunk in r.iter_bytes():
                    f.write(chunk)
        return out
