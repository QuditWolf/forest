import os
import re
import tomllib
from pathlib import Path


def _find_toml() -> Path | None:
    # Explicit config path via env var takes highest precedence
    explicit = os.environ.get("TASKS_CONFIG")
    if explicit:
        p = Path(explicit).expanduser()
        if p.exists():
            return p
    # Auto-discovery: walk up from this file's directory
    here = Path(__file__).parent
    for candidate in [here, here.parent, Path.cwd()]:
        p = candidate / "tasks.toml"
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


def _get(section: str, key: str, env_var: str, default: str) -> str:
    return (
        str(_cfg.get(section, {}).get(key))
        if _cfg.get(section, {}).get(key) is not None
        else os.environ.get(env_var, default)
    )


TASKS_ROOT  = Path(_get("tasks",  "root", "TASKS_ROOT",  "~/org/tasks")).expanduser()
SERVER_HOST = _get("server", "host", "TASKS_HOST", "0.0.0.0")
SERVER_PORT = int(_get("server", "port", "TASKS_PORT", "7000"))

# Kept as constants for UI hints and optional validation
VALID_STATES     = ["todo", "in-progress", "blocked", "waiting", "done"]
VALID_PRIORITIES = ["high", "medium", "low"]


def slugify(name: str) -> str:
    """Lowercase, spaces→hyphens, strip non-alphanumeric-hyphen."""
    name = name.lower().strip()
    name = re.sub(r"[^\w\s-]", "", name)
    name = re.sub(r"[\s_]+", "-", name)
    name = re.sub(r"-+", "-", name)
    return name.strip("-")


def safe_resolve(rel_path: str) -> Path:
    """Join with TASKS_ROOT, reject traversal outside root."""
    resolved = (TASKS_ROOT / rel_path).resolve()
    if not str(resolved).startswith(str(TASKS_ROOT.resolve())):
        raise ValueError(f"Path traversal detected: {rel_path}")
    return resolved
