"""Authentication, access tokens, and audit log.

Principals:
  session  - browser logged in with the password (full access + admin pages)
  token    - bearer token (API, MCP, git). scope "read" or "write", optionally limited to some vaults.
  local    - auth disabled (no FOREST_PASSWORD) - only sensible when bound to localhost.

Tokens are stored hashed (sha256) in STATE_DIR/tokens.json; the plaintext is shown once at creation.
"""

from __future__ import annotations

import contextvars
import hashlib
import hmac
import json
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from .config import FOREST_PASSWORD, STATE_DIR

TOKEN_PREFIX = "fst_"
SCOPES = ("read", "write")


@dataclass
class Principal:
    kind: str                       # session | token | local | anonymous
    name: str
    scope: str = "write"            # read | write
    vaults: List[str] = field(default_factory=lambda: ["*"])
    token_id: Optional[str] = None

    @property
    def can_write(self) -> bool:
        return self.scope == "write"

    @property
    def is_admin(self) -> bool:
        return self.kind in ("session", "local")

    def can_access(self, vault: str) -> bool:
        return "*" in self.vaults or vault in self.vaults

    @property
    def actor(self) -> str:
        return self.name if self.kind != "token" else f"token:{self.name}"


ANONYMOUS = Principal("anonymous", "anonymous", scope="none", vaults=[])
principal: contextvars.ContextVar[Principal] = contextvars.ContextVar("principal", default=ANONYMOUS)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat(timespec="seconds") if dt else None


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ── Password + login throttling ───────────────────────────────────────────────

def auth_enabled() -> bool:
    return bool(FOREST_PASSWORD)


def check_password(pw: str) -> bool:
    if not FOREST_PASSWORD:
        return False
    return hmac.compare_digest(_hash(pw or ""), _hash(FOREST_PASSWORD))


_fail_lock = threading.Lock()
_failures: dict[str, list[float]] = {}
MAX_FAILS, FAIL_WINDOW = 5, 15 * 60


def login_blocked(ip: str) -> bool:
    with _fail_lock:
        recent = [t for t in _failures.get(ip, []) if time.time() - t < FAIL_WINDOW]
        _failures[ip] = recent
        return len(recent) >= MAX_FAILS


def record_failure(ip: str) -> None:
    with _fail_lock:
        _failures.setdefault(ip, []).append(time.time())


def clear_failures(ip: str) -> None:
    with _fail_lock:
        _failures.pop(ip, None)


# ── Token store ───────────────────────────────────────────────────────────────

class TokenStore:
    def __init__(self):
        self.path = STATE_DIR / "tokens.json"
        self.lock = threading.RLock()
        self._tokens: list[dict] = []
        self._by_hash: dict[str, dict] = {}
        self._dirty_at: float = 0
        self._load()

    def _load(self):
        if self.path.exists():
            self._tokens = json.loads(self.path.read_text()).get("tokens", [])
        self._reindex()

    def _reindex(self):
        self._by_hash = {}
        for t in self._tokens:
            self._by_hash[t["hash"]] = t
            if t.get("refresh_hash"):
                self._by_hash["r:" + t["refresh_hash"]] = t

    def _save(self):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"tokens": self._tokens}, indent=2))
        tmp.chmod(0o600)
        tmp.replace(self.path)
        self._dirty_at = 0

    def create(self, name: str, scope: str = "read", vaults: Optional[List[str]] = None,
               expires_days: Optional[int] = None, kind: str = "manual",
               client_id: Optional[str] = None, with_refresh: bool = False) -> tuple[dict, str, Optional[str]]:
        if scope not in SCOPES:
            raise ValueError(f"scope must be one of {SCOPES}")
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        refresh = ("fsr_" + secrets.token_urlsafe(32)) if with_refresh else None
        rec = {
            "id": secrets.token_hex(6),
            "name": name.strip()[:64] or "token",
            "scope": scope,
            "vaults": vaults or ["*"],
            "kind": kind,
            "client_id": client_id,
            "hash": _hash(token),
            "hint": token[:8] + "…" + token[-4:],
            "created": _iso(_now()),
            "expires": _iso(_now() + timedelta(days=expires_days)) if expires_days else None,
            "last_used": None,
            "last_ip": None,
        }
        if refresh:
            rec["refresh_hash"] = _hash(refresh)
            rec["refresh_expires"] = _iso(_now() + timedelta(days=90))
        with self.lock:
            self._tokens.append(rec)
            self._reindex()
            self._save()
        return self.public(rec), token, refresh

    def verify(self, token: str, ip: str = "") -> Optional[dict]:
        if not token or not token.startswith(TOKEN_PREFIX):
            return None
        with self.lock:
            rec = self._by_hash.get(_hash(token))
            if not rec:
                return None
            if rec.get("expires") and datetime.fromisoformat(rec["expires"]) < _now():
                return None
            rec["last_used"] = _iso(_now())
            rec["last_ip"] = ip
            # flush usage stats at most once a minute
            if not self._dirty_at:
                self._dirty_at = time.time()
            elif time.time() - self._dirty_at > 60:
                self._save()
            return rec

    def rotate_refresh(self, refresh: str, access_days: int = 7) -> Optional[tuple[dict, str, str]]:
        """OAuth refresh: swap in a new access + refresh token for the same record."""
        with self.lock:
            rec = self._by_hash.get("r:" + _hash(refresh or ""))
            if not rec:
                return None
            if rec.get("refresh_expires") and datetime.fromisoformat(rec["refresh_expires"]) < _now():
                return None
            token = TOKEN_PREFIX + secrets.token_urlsafe(32)
            new_refresh = "fsr_" + secrets.token_urlsafe(32)
            rec["hash"] = _hash(token)
            rec["hint"] = token[:8] + "…" + token[-4:]
            rec["expires"] = _iso(_now() + timedelta(days=access_days))
            rec["refresh_hash"] = _hash(new_refresh)
            rec["refresh_expires"] = _iso(_now() + timedelta(days=90))
            self._reindex()
            self._save()
            return self.public(rec), token, new_refresh

    def revoke(self, token_id: str) -> bool:
        with self.lock:
            before = len(self._tokens)
            self._tokens = [t for t in self._tokens if t["id"] != token_id]
            self._reindex()
            self._save()
            return len(self._tokens) < before

    def list(self) -> List[dict]:
        with self.lock:
            return [self.public(t) for t in self._tokens]

    @staticmethod
    def public(rec: dict) -> dict:
        return {k: v for k, v in rec.items() if k not in ("hash", "refresh_hash")}

    def flush(self):
        with self.lock:
            if self._dirty_at:
                self._save()


_store: Optional[TokenStore] = None


def tokens() -> TokenStore:
    global _store
    if _store is None:
        _store = TokenStore()
    return _store


def principal_from_token(token: str, ip: str = "") -> Optional[Principal]:
    rec = tokens().verify(token, ip)
    if not rec:
        return None
    return Principal("token", rec["name"], scope=rec["scope"], vaults=rec["vaults"], token_id=rec["id"])


# ── Audit log (JSON lines) ────────────────────────────────────────────────────

_audit_lock = threading.Lock()
AUDIT_MAX_BYTES = 5 * 1024 * 1024


def audit(event: str, **fields) -> None:
    p = principal.get()
    rec = {"ts": _iso(_now()), "event": event, "who": p.actor, "kind": p.kind, **fields}
    line = json.dumps(rec, default=str)
    path = STATE_DIR / "audit.log"
    with _audit_lock:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > AUDIT_MAX_BYTES:
            path.replace(path.with_suffix(".log.1"))
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def read_audit(limit: int = 200, event: Optional[str] = None) -> List[dict]:
    path = STATE_DIR / "audit.log"
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    out = []
    for line in reversed(lines):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if event and not rec.get("event", "").startswith(event):
            continue
        out.append(rec)
        if len(out) >= limit:
            break
    return out
