"""Configuration.

Precedence (highest first): environment variables → forest.toml → defaults.
Environment-first so the same image works in Docker and on a laptop.

Server env vars:
  FOREST_CONFIG        explicit path to forest.toml
  FOREST_DATA          data dir (vaults + state), default ~/org/forest-data
  FOREST_VAULTS        comma list of vault names, default "forest,tasks"
  FOREST_VAULT_<NAME>  explicit path for a vault (else $FOREST_DATA/vaults/<name>)
  FOREST_HOST / FOREST_PORT
  FOREST_PASSWORD      web login password (unset = no login, only OK on localhost)
  FOREST_SECRET        session signing key (auto-generated + persisted if unset)
  FOREST_PUBLIC_URL    e.g. https://tools.example.com (OAuth + origin checks)
  FOREST_COOKIE_SECURE auto/1/0 - Secure session cookie (auto: on HTTPS requests only)
  FOREST_AUTOCOMMIT    1/0 - git commit after every write (default 1)
"""

from __future__ import annotations

import os
import re
import secrets
import tomllib
from pathlib import Path


def _find_toml() -> Path | None:
    explicit = os.environ.get("FOREST_CONFIG")
    if explicit:
        p = Path(explicit).expanduser()
        return p if p.exists() else None
    here = Path(__file__).parent
    for candidate in [Path.cwd(), here.parent]:
        p = candidate / "forest.toml"
        if p.exists():
            return p
    return None


def _load_toml() -> dict:
    path = _find_toml()
    if path is None:
        return {}
    with open(path, "rb") as f:
        return tomllib.load(f)


_cfg = _load_toml()


def _get(section: str, key: str, env_var: str, default):
    if os.environ.get(env_var) not in (None, ""):
        return os.environ[env_var]
    val = _cfg.get(section, {}).get(key)
    return default if val is None else val


def _bool(v) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


DATA_DIR = Path(_get("forest", "data", "FOREST_DATA", "~/org/forest-data")).expanduser()
STATE_DIR = DATA_DIR / ".forest"          # tokens, audit log, secret, oauth state

SERVER_HOST = str(_get("server", "host", "FOREST_HOST", "127.0.0.1"))
SERVER_PORT = int(_get("server", "port", "FOREST_PORT", 7000))

PUBLIC_URL = str(_get("server", "public_url", "FOREST_PUBLIC_URL", "")).rstrip("/")

FOREST_PASSWORD: str | None = _get("auth", "password", "FOREST_PASSWORD", None) or None

# "auto" (default): Secure cookie on HTTPS requests (via nginx), plain on http:// (e.g. direct VPN IP).
# "1": always Secure. "0": never.
_cs = str(_get("auth", "cookie_secure", "FOREST_COOKIE_SECURE", "auto")).strip().lower()
COOKIE_SECURE: bool | None = None if _cs == "auto" else _bool(_cs)

AUTOCOMMIT = _bool(_get("forest", "autocommit", "FOREST_AUTOCOMMIT", "1"))

# Timezone for daily notes, reminders and digests (IANA name, e.g. "Europe/Berlin")
TIMEZONE = str(_get("forest", "timezone", "FOREST_TZ", os.environ.get("TZ") or "UTC")).strip()
try:
    __import__("zoneinfo").ZoneInfo(TIMEZONE)
except Exception:
    raise SystemExit(f"FOREST_TZ must be an IANA timezone name like Asia/Kolkata, Europe/Berlin or UTC "
                     f"(got {TIMEZONE!r}; abbreviations like IST/EST are ambiguous and not accepted).")
# Make date.today() / datetime.now() (created/completed stamps, git dates) follow the same timezone
if TIMEZONE and os.environ.get("TZ") != TIMEZONE and hasattr(__import__("time"), "tzset"):
    os.environ["TZ"] = TIMEZONE
    __import__("time").tzset()

# Where things live (vault:folder)
JOURNAL = str(_get("forest", "journal", "FOREST_JOURNAL", "forest:journal"))       # daily notes
TEMPLATES = str(_get("forest", "templates", "FOREST_TEMPLATES", "forest:_templates"))
INBOX = str(_get("forest", "inbox", "FOREST_INBOX", "forest:inbox"))               # captures / web clips

MAX_UPLOAD_MB = int(_get("forest", "max_upload_mb", "FOREST_MAX_UPLOAD_MB", 25))

# ntfy push reminders (optional). Leave URL empty to disable.
NTFY_URL = str(_get("ntfy", "url", "FOREST_NTFY_URL", "")).rstrip("/")            # e.g. https://ntfy.sh
NTFY_TOPIC = str(_get("ntfy", "topic", "FOREST_NTFY_TOPIC", ""))
NTFY_TOKEN = str(_get("ntfy", "token", "FOREST_NTFY_TOKEN", ""))
DIGEST_TIME = str(_get("ntfy", "digest_time", "FOREST_DIGEST_TIME", "08:00"))      # daily digest, "" = off
WEEKLY_TIME = str(_get("ntfy", "weekly_time", "FOREST_WEEKLY_TIME", "sun 19:00"))  # weekly overview, "" = off

VALID_STATES     = ["todo", "in-progress", "blocked", "waiting", "done"]
VALID_PRIORITIES = ["high", "medium", "low"]

VAULT_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
# Names that would collide with top-level API routes
RESERVED_NAMES = {"vaults", "search", "grep", "find", "tags", "agenda", "admin", "auth",
                  "backup", "restore", "me", "history", "changes", "resolve", "index", "daily",
                  "templates", "review", "capture", "skill", "calendar.ics", "remind", "docs",
                  "openapi.json"}


def vault_paths() -> dict[str, Path]:
    """name → root path for every configured vault."""
    names_raw = _get("forest", "vaults", "FOREST_VAULTS", "forest,tasks")
    names = names_raw if isinstance(names_raw, list) else [n.strip() for n in str(names_raw).split(",")]
    explicit = _cfg.get("vaults", {})
    # Back-compat: old forest.toml had [forest] root = "..."
    legacy_root = _cfg.get("forest", {}).get("root")
    out: dict[str, Path] = {}
    for n in names:
        if not n:
            continue
        if not VAULT_NAME_RE.match(n) or n in RESERVED_NAMES:
            raise ValueError(f"Invalid vault name: {n!r}")
        p = os.environ.get(f"FOREST_VAULT_{n.upper()}") or explicit.get(n)
        if not p and n == "forest" and legacy_root:
            p = legacy_root
        out[n] = Path(p).expanduser() if p else DATA_DIR / "vaults" / n
    return out


def session_secret() -> str:
    """FOREST_SECRET, else a random secret persisted in STATE_DIR."""
    s = _get("auth", "session_secret", "FOREST_SECRET", None)
    if s:
        return str(s)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    f = STATE_DIR / "secret"
    if not f.exists():
        f.write_text(secrets.token_urlsafe(48))
        f.chmod(0o600)
    return f.read_text().strip()


def slugify(name: str) -> str:
    """Lowercase, spaces→hyphens, strip non-alphanumeric-hyphen."""
    name = name.lower().strip()
    name = re.sub(r"[^\w\s-]", "", name)
    name = re.sub(r"[\s_]+", "-", name)
    name = re.sub(r"-+", "-", name)
    return name.strip("-") or "untitled"
