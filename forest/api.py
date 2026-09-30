"""FastAPI app - web UIs, REST API, MCP (/mcp), git sync (/git), OAuth, admin."""

from __future__ import annotations

import contextlib
import html
import tempfile
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

import asyncio
import base64

from . import auth, backup, features, githttp, oauth, reminders, store
from .auth import Principal
from .config import (COOKIE_SECURE, PUBLIC_URL, SERVER_HOST, SERVER_PORT, STATE_DIR,
                     VALID_PRIORITIES, VALID_STATES, session_secret)
from .mcp_server import mcp

STATIC_DIR = Path(__file__).parent.parent / "static"

from mcp.server.transport_security import TransportSecuritySettings

# The SDK's own Host/Origin check defaults to localhost-only and would reject requests arriving via
# nginx (Host: your domain) or from claude.ai. Auth + mcp_origin_guard below handle this instead.
mcp_app = mcp.streamable_http_app(
    streamable_http_path="/mcp", stateless_http=True, json_response=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    _refuse_insecure_start()
    store.init_vaults()
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    features.ensure_templates()
    task = asyncio.create_task(reminders.loop())
    async with mcp.session_manager.run():
        yield
    task.cancel()
    auth.tokens().flush()


def _refuse_insecure_start():
    """Never run without a password when reachable from the network (e.g. .env typo behind nginx)."""
    import os
    if auth.auth_enabled() or os.environ.get("FOREST_ALLOW_NO_AUTH") == "1":
        return
    if PUBLIC_URL or SERVER_HOST not in ("127.0.0.1", "localhost", "::1") or os.path.exists("/.dockerenv"):
        raise RuntimeError("FOREST_PASSWORD is not set. Refusing to start without login on a network-reachable "
                           "server. Set FOREST_PASSWORD (or FOREST_ALLOW_NO_AUTH=1 for a local-only instance).")


app = FastAPI(title="Forest API", version="0.3.0", lifespan=lifespan,
              docs_url="/api/docs", openapi_url="/api/openapi.json", redoc_url=None)


# ── Login page ────────────────────────────────────────────────────────────────

LOGIN_HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"/><meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>forest login</title>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{min-height:100vh;display:flex;align-items:center;justify-content:center;background:#fff;color:#000;
font-family:Menlo,Consolas,"DejaVu Sans Mono",monospace;font-size:13px}
@media (prefers-color-scheme:dark){body{background:#0f0f0f;color:#e8e8e8}input{background:#1a1a1a!important;color:#e8e8e8!important}
button{background:#e8e8e8!important;color:#000!important}}
.card{width:100%;max-width:320px;padding:2rem}
h1{font-size:13px;font-weight:normal;margin-bottom:1.5rem;letter-spacing:.05em}
label{display:block;margin-bottom:.4rem;color:#666}
input[type=password]{width:100%;font-family:inherit;font-size:13px;padding:6px 8px;border:1px solid #aaa;background:#fff;color:#000;outline:none}
input[type=password]:focus{border-color:#000}
button{margin-top:1rem;width:100%;font-family:inherit;font-size:13px;padding:6px;background:#000;color:#fff;border:none;cursor:pointer}
.error{margin-top:.75rem;color:#c00}
</style></head><body><div class="card"><h1>forest</h1>
<form method="post" action="/auth/login"><input type="hidden" name="next" value="__NEXT__"/>
<label for="password">password</label><input type="password" id="password" name="password" autofocus required/>
<button type="submit">enter</button>__ERROR__</form></div>
<script>/* keep #vault/path deep links through login (the server never sees the #fragment) */
if (location.hash) { const n = document.querySelector('input[name=next]'); if (!n.value.includes('#')) n.value += location.hash; }
</script></body></html>"""


def _safe_next(nxt: str) -> str:
    return nxt if nxt.startswith("/") and not nxt.startswith("//") and "\\" not in nxt else "/"


def _ip(request: Request) -> str:
    return request.client.host if request.client else ""


@app.get("/auth/login", response_class=HTMLResponse, include_in_schema=False)
def login_page(error: str = "", next: str = "/"):
    err = '<p class="error">' + html.escape(error) + "</p>" if error else ""
    return LOGIN_HTML.replace("__ERROR__", err).replace("__NEXT__", html.escape(_safe_next(next)))


@app.post("/auth/login", include_in_schema=False)
async def login_submit(request: Request, password: str = Form(...), next: str = Form("/")):
    ip = _ip(request)
    if auth.login_blocked(ip):
        auth.audit("login.blocked", ip=ip)
        return RedirectResponse("/auth/login?" + urlencode({"error": "too many attempts, wait 15 min",
                                                            "next": next}), 303)
    if auth.check_password(password):
        auth.clear_failures(ip)
        request.session.clear()
        request.session["authenticated"] = True
        auth.principal.set(Principal("session", "web"))
        auth.audit("login.ok", ip=ip, ua=request.headers.get("user-agent", "")[:120])
        return RedirectResponse(_safe_next(next), 303)
    auth.record_failure(ip)
    auth.audit("login.fail", ip=ip)
    return RedirectResponse("/auth/login?" + urlencode({"error": "incorrect password", "next": next}), 303)


@app.get("/auth/logout", include_in_schema=False)
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/auth/login", 302)


# ── Auth middleware ───────────────────────────────────────────────────────────

PUBLIC_PREFIXES = ("/auth/", "/.well-known/", "/oauth/", "/lib/", "/healthz", "/favicon",
                   "/api/remind/action/", "/manifest.webmanifest", "/sw.js", "/icon.svg")
TOKEN_PREFIXES = ("/api/", "/mcp", "/git/")
UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}
LEGACY_API = {"tree", "children", "page", "pages", "backlinks", "shadow"}

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
}
CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data: https:; connect-src 'self'; font-src 'self' data:; "
       "frame-ancestors 'none'; base-uri 'self'; form-action 'self' https: http://localhost:* http://127.0.0.1:*")


def _bearer(request: Request) -> Optional[str]:
    h = request.headers.get("authorization", "")
    if h.lower().startswith("bearer "):
        return h[7:].strip()
    if h.lower().startswith("basic "):  # git over https: any username, token as password
        import base64
        try:
            _, _, pw = base64.b64decode(h[6:]).decode().partition(":")
            return pw
        except Exception:
            return None
    return None


def _unauthorized(request: Request, path: str, msg: str = "Unauthorized"):
    if path.startswith("/mcp"):
        rm = f'{oauth.base_url(request)}/.well-known/oauth-protected-resource'
        return JSONResponse({"error": "invalid_token", "error_description": msg}, 401,
                            headers={"WWW-Authenticate": f'Bearer resource_metadata="{rm}"'})
    if path.startswith("/git/"):
        return Response(msg, 401, headers={"WWW-Authenticate": 'Basic realm="forest"'})
    return JSONResponse({"detail": msg}, 401)


@app.middleware("http")
async def access_control(request: Request, call_next):
    path = request.url.path

    # Legacy single-vault API paths (/api/tree, /api/page/...) → default vault
    parts = path.split("/")
    if len(parts) > 2 and parts[1] == "api" and parts[2] in LEGACY_API:
        request.scope["path"] = "/api/" + store.DEFAULT_VAULT + path[4:]
        path = request.scope["path"]

    ip = _ip(request)
    token = _bearer(request)
    if token is None and path == "/api/calendar.ics" and request.query_params.get("token"):
        token = request.query_params["token"]  # calendar apps can't send headers
    p: Optional[Principal] = None

    if path.startswith(PUBLIC_PREFIXES) or path == "/login":
        p = Principal("session", "web") if request.session.get("authenticated") else auth.ANONYMOUS
    elif token is not None:
        p = auth.principal_from_token(token, ip)
        if p is None:
            auth.principal.set(auth.ANONYMOUS)
            auth.audit("auth.bad_token", ip=ip, path=path)
            return _unauthorized(request, path, "invalid or expired token")
        if not path.startswith(TOKEN_PREFIXES) or path.startswith("/api/admin"):
            return JSONResponse({"detail": "tokens cannot access this endpoint"}, 403)
        if request.method in UNSAFE and not p.can_write and path.startswith("/api/"):
            auth.principal.set(p)
            auth.audit("auth.denied", ip=ip, method=request.method, path=path, reason="read-only token")
            return JSONResponse({"detail": "read-only token"}, 403)
    elif not auth.auth_enabled():
        p = Principal("local", "local")
    elif request.session.get("authenticated"):
        p = Principal("session", "web")
        # CSRF defence in depth (SameSite=Lax already blocks cross-site POST cookies)
        if request.method in UNSAFE and path.startswith("/api/") and \
                request.headers.get("x-requested-with") != "forest":
            return JSONResponse({"detail": "missing X-Requested-With header"}, 403)
    else:
        if path.startswith(TOKEN_PREFIXES):
            return _unauthorized(request, path)
        return RedirectResponse("/auth/login?" + urlencode({"next": path}), 302)

    auth.principal.set(p)
    store.actor.set(p.actor)
    response = await call_next(request)

    for k, v in SECURITY_HEADERS.items():
        response.headers.setdefault(k, v)
    if response.headers.get("content-type", "").startswith("text/html"):
        response.headers.setdefault("Content-Security-Policy", CSP)

    # Audit: every token-authenticated request (agents), every write, every denial
    if p.kind == "token" and not path.startswith("/mcp") or \
            (request.method in UNSAFE and not path.startswith(("/mcp", "/auth/", "/oauth/", "/git/"))) or \
            response.status_code in (401, 403):
        auth.audit("http", ip=ip, method=request.method, path=path, status=response.status_code)
    return response


# SessionMiddleware must be outermost so request.session exists in the auth middleware
app.add_middleware(SessionMiddleware, secret_key=session_secret(), session_cookie="forest_session",
                   max_age=60 * 60 * 24 * 30, same_site="lax", https_only=bool(COOKIE_SECURE))


class SecureCookieOnHttps:
    """COOKIE_SECURE=auto: add `Secure` to the session cookie on HTTPS requests (nginx sets
    X-Forwarded-Proto, uvicorn --proxy-headers turns it into scope["scheme"]), and leave it off on
    plain http:// so direct VPN access (http://10.x.x.x:7700) can log in too."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("scheme") != "https":
            return await self.app(scope, receive, send)

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = []
                for k, v in message.get("headers", []):
                    if k.lower() == b"set-cookie" and v.startswith(b"forest_session=") and b"secure" not in v.lower():
                        v += b"; secure"
                    headers.append((k, v))
                message = {**message, "headers": headers}
            await send(message)

        return await self.app(scope, receive, send_wrapper)


if COOKIE_SECURE is None:
    app.add_middleware(SecureCookieOnHttps)
app.include_router(oauth.router)


# ── Helpers ───────────────────────────────────────────────────────────────────

def V(name: str, write: bool = False) -> store.Vault:
    """Resolve a vault from the URL and enforce the caller's permissions."""
    try:
        v = store.vault(name)
    except KeyError:
        raise HTTPException(404, f"Unknown vault {name!r}")
    p = auth.principal.get()
    if not p.can_access(v.name):
        raise HTTPException(403, f"No access to vault {name!r}")
    if write and not p.can_write:
        raise HTTPException(403, "read-only token")
    return v


def _allowed(vault: Optional[str]) -> Optional[str]:
    p = auth.principal.get()
    if vault and vault != "all":
        for n in vault.split(","):
            V(n.strip())
        return vault
    if "*" in p.vaults:
        return None
    return ",".join(n for n in store.VAULTS if p.can_access(n)) or "__none__"


def _admin():
    if not auth.principal.get().is_admin:
        raise HTTPException(403, "admin (web login) only")


@contextlib.contextmanager
def _errors():
    try:
        yield
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except FileExistsError as e:
        raise HTTPException(409, str(e))
    except store.ConflictError as e:
        raise HTTPException(409, str(e))
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except (ValueError, KeyError) as e:
        raise HTTPException(400, str(e))
    except store.gitrepo.GitError as e:
        raise HTTPException(400, str(e))


# ── Schemas ───────────────────────────────────────────────────────────────────

class PageCreate(BaseModel):
    name: str
    content: str = ""
    parent_path: Optional[str] = None
    as_folder: bool = False
    state: Optional[str] = None
    priority: Optional[str] = None
    due: Optional[date] = None
    tags: Optional[List[str]] = None
    template: Optional[str] = None


class PageUpdate(BaseModel):
    name: Optional[str] = None
    content: Optional[str] = None
    state: Optional[str] = None
    priority: Optional[str] = None
    due: Optional[date] = None
    tags: Optional[List[str]] = None
    extra: Optional[Dict[str, Any]] = None
    expected_sha: Optional[str] = None


class RawWrite(BaseModel):
    text: str
    expected_sha: Optional[str] = None


class EditRequest(BaseModel):
    old: str
    new: str
    replace_all: bool = False
    expected_sha: Optional[str] = None


class MoveRequest(BaseModel):
    new_parent_path: Optional[str] = None


class AppendRequest(BaseModel):
    text: str
    timestamp: bool = True


class RestoreShadow(BaseModel):
    shadow_path: str


class RevRequest(BaseModel):
    rev: str


class CaptureRequest(BaseModel):
    title: Optional[str] = None
    text: str = ""
    url: Optional[str] = None
    where: Optional[str] = None        # 'vault:folder', default inbox
    fetch: bool = False                # save a markdown copy of the article
    tags: Optional[List[str]] = None
    as_task: bool = False


class LinkMention(BaseModel):
    source_vault: str
    source_path: str


class TokenCreate(BaseModel):
    name: str
    scope: str = "read"
    vaults: List[str] = ["*"]
    expires_days: Optional[int] = None


# ── Global endpoints ──────────────────────────────────────────────────────────

@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"ok": True}


@app.get("/api/me")
def me():
    p = auth.principal.get()
    return {"kind": p.kind, "name": p.name, "scope": p.scope, "admin": p.is_admin,
            "vaults": [n for n in store.VAULTS if p.can_access(n)], "default_vault": store.DEFAULT_VAULT,
            "states": VALID_STATES, "priorities": VALID_PRIORITIES}


@app.get("/api/vaults")
def list_vaults():
    p = auth.principal.get()
    return [{"name": n, "pages": sum(1 for _ in v.iter_md())} for n, v in store.VAULTS.items() if p.can_access(n)]


@app.get("/api/search")
def search(q: str = "", vault: Optional[str] = None, limit: int = 100):
    """Full-text search (all words must match). vault: name, comma list, or all."""
    return store.search_pages(q, _allowed(vault), limit=limit)


@app.get("/api/grep")
def grep(pattern: str, vault: Optional[str] = None, regex: bool = True, ignore_case: bool = True,
         glob: Optional[str] = None, context: int = 0, max_results: int = 200):
    with _errors():
        try:
            return store.grep(pattern, _allowed(vault), regex=regex, ignore_case=ignore_case,
                              path_glob=glob, context=context, max_results=max_results)
        except Exception as e:  # bad regex
            if isinstance(e, HTTPException):
                raise
            raise ValueError(str(e))


@app.get("/api/find")
def find(glob: str, vault: Optional[str] = None):
    return store.glob_pages(glob, _allowed(vault))


@app.get("/api/tags")
def tags(vault: Optional[str] = None):
    return store.all_tags(_allowed(vault))


@app.get("/api/tags/{tag:path}")
def tagged(tag: str, vault: Optional[str] = None):
    return store.pages_with_tag(tag, _allowed(vault))


@app.get("/api/agenda")
def agenda(vault: Optional[str] = None, state: str = "undone", priority: Optional[str] = None,
           due_within: Optional[str] = None, overdue: bool = False, tag: Optional[str] = None,
           under: Optional[str] = None, high: Optional[str] = None, medium: Optional[str] = None,
           low: Optional[str] = None):
    """Task pages across vaults. high/medium/low = per-priority due windows (e.g. 'all', '2w', 'none')."""
    by_pri = {"high": high or "all", "medium": medium or "all", "low": low or "all"} \
        if any((high, medium, low)) else None
    return store.agenda(_allowed(vault), state=state, priority=priority, due_within=due_within,
                        overdue=overdue, tag=tag, under=under, by_priority=by_pri)


@app.get("/api/changes")
def changes(since: str = "7 days ago", vault: Optional[str] = None, limit: int = 50):
    return store.recent_changes(since, _allowed(vault), limit=limit)


@app.get("/api/resolve")
def resolve(ref: str, from_vault: Optional[str] = None, from_path: Optional[str] = None):
    """Resolve a [[link]] target → {vault, path}."""
    with _errors():
        v, p = store.resolve_link(ref, from_vault or store.DEFAULT_VAULT, from_path)
        V(v)
        return {"vault": v, "path": p}


@app.get("/api/index")
def index(vault: Optional[str] = None):
    """Light list of every page (vault, path, name, state) + tags - for autocomplete."""
    vs = store._selected(_allowed(vault))
    pages = [{"vault": v.name, "path": p.path, "name": p.name, "state": p.state, "is_folder": p.is_folder}
             for v in vs for p in v.all_pages()]
    return {"pages": pages, "tags": [t["tag"] for t in store.all_tags(_allowed(vault))]}


@app.get("/api/templates")
def templates():
    return features.list_templates()


@app.get("/api/daily")
def daily(date: Optional[date] = None, create: bool = True):
    """Daily note for a date (default today), created from the daily template if missing."""
    jv = store.split_ref(features.JOURNAL)[0]
    V(jv, write=create)
    with _errors():
        p = features.daily_note(date, create=create)
    if p is None:
        raise HTTPException(404, "no note for that day")
    return p


@app.get("/api/daily/days")
def daily_days():
    V(store.split_ref(features.JOURNAL)[0])
    return {"today": features.today().isoformat(), "days": features.journal_days()}


@app.get("/api/review")
def review(period: str = "day", stale_days: int = 7, vault: Optional[str] = None):
    """Structured review bundle: overdue, due soon, in progress, stale, done, changes, inbox."""
    return features.review(period, stale_days, _allowed(vault))


@app.post("/api/capture", status_code=201)
def capture(body: CaptureRequest):
    """Quick capture / web clip into the inbox (or body.where = 'vault:folder')."""
    target = store.split_ref(body.where or features.INBOX)[0]
    V(target, write=True)
    with _errors():
        p = features.capture(body.title, body.text, body.url, body.where, body.fetch, body.tags, body.as_task)
    auth.audit("capture", vault=p.vault, path=p.path, url=body.url)
    return p


@app.get("/api/calendar.ics", include_in_schema=False)
def calendar_ics(vault: Optional[str] = None):
    """iCalendar feed of due dates. Calendar apps: /api/calendar.ics?token=<read token>."""
    return Response(features.calendar_ics(_allowed(vault)), media_type="text/calendar",
                    headers={"Content-Disposition": 'inline; filename="forest.ics"'})


@app.get("/api/skill", include_in_schema=False)
def skill():
    """The Forest agent skill (SKILL.md) - load into any agent / assistant project."""
    f = Path(__file__).parent / "skill" / "SKILL.md"
    return Response(f.read_text(encoding="utf-8"), media_type="text/markdown; charset=utf-8")


@app.post("/api/remind/action/{token}", include_in_schema=False)
def remind_action(token: str):
    """ntfy action button callback (signed link, no login)."""
    with _errors():
        return reminders.run_action(token)


# ── Admin: tokens, audit, oauth clients, backup ──────────────────────────────

@app.post("/api/admin/ntfy-test")
def admin_ntfy_test(kind: str = "test"):
    _admin()
    try:
        if kind == "digest":
            reminders.daily_digest()
        elif kind == "weekly":
            reminders.weekly_overview()
        elif kind == "reminders":
            reminders.due_reminders()   # send any due `remind:` pushes now
        else:
            reminders.send("forest: test", "If you can read this, reminders work.", tags=["white_check_mark"])
    except Exception as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.get("/api/admin/settings")
def admin_settings():
    _admin()
    from . import config as c
    return {"public_url": c.PUBLIC_URL, "timezone": c.TIMEZONE, "journal": c.JOURNAL, "inbox": c.INBOX,
            "templates": c.TEMPLATES, "max_upload_mb": c.MAX_UPLOAD_MB, "ntfy": reminders.enabled(),
            "ntfy_url": c.NTFY_URL, "ntfy_topic": (c.NTFY_TOPIC[:3] + "...") if c.NTFY_TOPIC else "",
            "digest_time": c.DIGEST_TIME, "weekly_time": c.WEEKLY_TIME}


@app.get("/api/admin/tokens")
def admin_tokens():
    _admin()
    return auth.tokens().list()


@app.post("/api/admin/tokens", status_code=201)
def admin_create_token(body: TokenCreate):
    _admin()
    with _errors():
        vaults = body.vaults or ["*"]
        for n in vaults:
            if n != "*" and n not in store.VAULTS:
                raise ValueError(f"unknown vault {n}")
        pub, token, _ = auth.tokens().create(body.name, body.scope, vaults, body.expires_days)
    auth.audit("token.create", token_id=pub["id"], name=pub["name"], scope=pub["scope"], vaults=vaults)
    return {**pub, "token": token}


@app.delete("/api/admin/tokens/{token_id}", status_code=204)
def admin_revoke_token(token_id: str):
    _admin()
    if not auth.tokens().revoke(token_id):
        raise HTTPException(404, "no such token")
    auth.audit("token.revoke", token_id=token_id)


@app.get("/api/admin/audit")
def admin_audit(limit: int = 200, event: Optional[str] = None):
    _admin()
    return auth.read_audit(min(limit, 2000), event)


@app.get("/api/admin/oauth-clients")
def admin_oauth_clients():
    _admin()
    return list(oauth.load_clients().values())


@app.delete("/api/admin/oauth-clients/{client_id}", status_code=204)
def admin_delete_client(client_id: str):
    _admin()
    oauth.delete_client(client_id)
    for t in auth.tokens().list():
        if t.get("client_id") == client_id:
            auth.tokens().revoke(t["id"])
    auth.audit("oauth.client_delete", client_id=client_id)


@app.get("/api/backup")
def download_backup(vault: Optional[str] = None):
    """tar.gz of vaults (with full git history). Tokens need access to every requested vault."""
    names = [n.strip() for n in vault.split(",")] if vault else list(store.VAULTS)
    vs = [V(n) for n in names]
    tmp = tempfile.NamedTemporaryFile(prefix="forest-backup-", suffix=".tar.gz", delete=False)
    with tmp:
        backup.create_backup(tmp, vs)
    auth.audit("backup.download", vaults=names)
    from starlette.background import BackgroundTask
    return FileResponse(tmp.name, media_type="application/gzip", filename=backup.backup_filename(),
                        background=BackgroundTask(lambda: Path(tmp.name).unlink(missing_ok=True)))


@app.post("/api/admin/restore")
async def admin_restore(file: UploadFile = File(...), vault: Optional[str] = Form(None)):
    """Restore vaults from a backup archive. Current contents are moved aside, not deleted."""
    _admin()
    with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
        while chunk := await file.read(1 << 20):
            tmp.write(chunk)
    try:
        with _errors():
            only = [n.strip() for n in vault.split(",")] if vault else None
            res = backup.restore_backup(Path(tmp.name), only)
    finally:
        Path(tmp.name).unlink(missing_ok=True)
    auth.audit("backup.restore", **res)
    return res


# ── Per-vault endpoints ───────────────────────────────────────────────────────

@app.get("/api/{vault}/tree")
def get_tree(vault: str):
    return V(vault).get_tree()


@app.get("/api/{vault}/pages")
def list_pages(vault: str):
    """Flat list of every page (metadata only, no content)."""
    return [store._summary(p) for p in V(vault).all_pages()]


@app.get("/api/{vault}/children")
def get_root_children(vault: str):
    return V(vault).list_children(None)


@app.get("/api/{vault}/children/{parent_path:path}")
def get_children(vault: str, parent_path: str):
    with _errors():
        return V(vault).list_children(parent_path)


@app.get("/api/{vault}/page/{path:path}")
def get_page(vault: str, path: str, raw: bool = False):
    """Get a page. raw=true → {text, sha} of the exact file."""
    v = V(vault)
    with _errors():
        if raw:
            text, sha = v.read_raw(path)
            return {"vault": v.name, "path": v.get_page(path).path, "text": text, "sha": sha}
        return v.get_page(path)


@app.post("/api/{vault}/pages", status_code=201)
def create_page(vault: str, body: PageCreate):
    with _errors():
        return V(vault, True).create_page(body.parent_path, body.name, content=body.content,
                                          state=body.state, priority=body.priority, due=body.due,
                                          tags=body.tags, as_folder=body.as_folder, template=body.template)


@app.patch("/api/{vault}/page/{path:path}")
def update_page(vault: str, path: str, body: PageUpdate):
    """Update fields. Only fields present in the body change; pass expected_sha to detect conflicts."""
    updates = body.model_dump(exclude_unset=True)
    sha = updates.pop("expected_sha", None)
    with _errors():
        return V(vault, True).update_page(path, expected_sha=sha, **updates)


@app.put("/api/{vault}/raw/{path:path}")
def write_raw(vault: str, path: str, body: RawWrite):
    """Write the exact file text (frontmatter + body). Creates the page if missing."""
    with _errors():
        return V(vault, True).write_raw(path, body.text, expected_sha=body.expected_sha)


@app.post("/api/{vault}/page/{path:path}/edit")
def edit_page(vault: str, path: str, body: EditRequest):
    """Exact string replacement in the raw file."""
    with _errors():
        return V(vault, True).edit_raw(path, body.old, body.new, body.replace_all, body.expected_sha)


@app.delete("/api/{vault}/page/{path:path}", status_code=204)
def delete_page(vault: str, path: str):
    with _errors():
        V(vault, True).delete_page(path)


@app.post("/api/{vault}/page/{path:path}/move")
def move_page(vault: str, path: str, body: MoveRequest):
    with _errors():
        return V(vault, True).move_page(path, body.new_parent_path)


@app.post("/api/{vault}/page/{path:path}/promote")
def promote_page(vault: str, path: str):
    with _errors():
        return V(vault, True).promote_to_folder(path)


@app.post("/api/{vault}/page/{path:path}/append")
def append_to_page(vault: str, path: str, body: AppendRequest):
    with _errors():
        return V(vault, True).append_to_page(path, body.text, body.timestamp)


@app.post("/api/{vault}/page/{path:path}/restore-version")
def restore_version(vault: str, path: str, body: RevRequest):
    with _errors():
        return V(vault, True).restore_version(path, body.rev)


@app.get("/api/{vault}/backlinks/{path:path}")
def get_backlinks(vault: str, path: str):
    """Pages (any vault) linking here, as 'vault:path' refs."""
    with _errors():
        return store.get_backlinks(V(vault).name, path)


@app.get("/api/{vault}/links/{path:path}")
def get_links(vault: str, path: str):
    with _errors():
        return store.outgoing_links(V(vault).name, path)


@app.get("/api/{vault}/shadow")
def list_shadow(vault: str):
    return V(vault).list_shadow()


@app.post("/api/{vault}/shadow/restore")
def restore_shadow(vault: str, body: RestoreShadow):
    """Restore a deleted page or attachment (shadow_path from GET shadow)."""
    with _errors():
        v = V(vault, True)
        if body.shadow_path.startswith("_assets/"):
            return features.restore_asset(v.name, body.shadow_path)
        return v.restore_from_shadow(body.shadow_path)


@app.get("/api/{vault}/history")
def history(vault: str, path: Optional[str] = None, limit: int = 30, since: Optional[str] = None):
    with _errors():
        return V(vault).history(path, limit=limit, since=since)


@app.get("/api/{vault}/version/{rev}/{path:path}")
def version(vault: str, rev: str, path: str):
    with _errors():
        return {"rev": rev, "path": path, "text": V(vault).show_version(path, rev)}


@app.get("/api/{vault}/diff/{rev}")
def diff(vault: str, rev: str, path: Optional[str] = None):
    with _errors():
        return {"rev": rev, "diff": V(vault).diff(rev, path)}


@app.get("/api/{vault}/outline/{path:path}")
def get_outline(vault: str, path: str):
    with _errors():
        return features.outline(V(vault).get_page(path).content)


@app.get("/api/{vault}/section/{path:path}")
def get_section(vault: str, path: str, heading: str):
    with _errors():
        return {"heading": heading, "text": features.section(V(vault).get_page(path).content, heading)}


@app.get("/api/{vault}/mentions/{path:path}")
def get_mentions(vault: str, path: str):
    """Pages that mention this page's name without linking to it."""
    with _errors():
        return features.unlinked_mentions(V(vault).name, path)


@app.post("/api/{vault}/page/{path:path}/link-mention")
def link_mention(vault: str, path: str, body: LinkMention):
    """Rewrite the first plain mention of this page's name in source into a [[link]]."""
    V(vault)
    V(body.source_vault, True)
    with _errors():
        return features.link_mention(body.source_vault, body.source_path, vault, path)


@app.get("/api/{vault}/neighbors/{path:path}")
def get_neighbors(vault: str, path: str):
    """A page and its direct neighbours (parent, children, links, backlinks) - for graph expand."""
    V(vault)
    with _errors():
        g = features.neighbors(vault, path)
    p = auth.principal.get()
    g["nodes"] = [n for n in g["nodes"] if p.can_access(n["vault"])]
    keep = {n["id"] for n in g["nodes"]}
    g["edges"] = [e for e in g["edges"] if e["from"] in keep and e["to"] in keep]
    return g


@app.get("/api/{vault}/graph")
@app.get("/api/{vault}/graph/{path:path}")
def get_graph(vault: str, path: Optional[str] = None, depth: int = 2, hierarchy: bool = True):
    """Graph around a page (or the whole vault without path): links, backlinks and parent/children."""
    V(vault)
    with _errors():
        g = features.graph(vault, path or None, depth, hierarchy)
    p = auth.principal.get()
    g["nodes"] = [n for n in g["nodes"] if p.can_access(n["vault"])]
    keep = {n["id"] for n in g["nodes"]}
    g["edges"] = [e for e in g["edges"] if e["from"] in keep and e["to"] in keep]
    return g


@app.post("/api/{vault}/assets", status_code=201)
async def upload_asset(vault: str, page: str = Form(...), file: UploadFile = File(...)):
    """Attach a file to a page → stored in <vault>/_assets/<page>/, returns markdown to insert."""
    V(vault, True)
    data = await file.read(features.MAX_BYTES + 1)
    with _errors():
        return features.save_asset(vault, page, file.filename or "file", data)


@app.get("/api/{vault}/assets/{path:path}")
def list_assets(vault: str, path: str):
    with _errors():
        return features.list_assets(V(vault).name, path)


@app.get("/api/{vault}/asset/{path:path}")
def get_asset(vault: str, path: str):
    """Serve an attachment. Images/PDF inline; anything that could run script is sandboxed/downloaded."""
    with _errors():
        f = features.asset_file(V(vault).name, path)
    import mimetypes
    mt = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
    inline = mt.startswith(("image/", "audio/", "video/")) or mt in ("application/pdf", "text/plain")
    if mt in ("image/svg+xml", "text/html", "application/xhtml+xml"):
        inline = mt == "image/svg+xml"
    headers = {"Content-Security-Policy": "sandbox; default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'",
               "Cache-Control": "private, max-age=3600"}
    return FileResponse(f, media_type=mt, headers=headers,
                        content_disposition_type="inline" if inline else "attachment", filename=f.name)


@app.delete("/api/{vault}/asset/{path:path}")
def delete_asset(vault: str, path: str):
    """Soft-delete an attachment (moved to .shadow, restorable via shadow/restore)."""
    with _errors():
        return features.delete_asset(V(vault, True).name, path)


# ── Git smart HTTP ────────────────────────────────────────────────────────────

@app.api_route("/git/{vault}/{rest:path}", methods=["GET", "POST"], include_in_schema=False)
async def git_http(vault: str, rest: str, request: Request):
    return await githttp.handle(request, vault, rest)


# ── MCP (Streamable HTTP) ─────────────────────────────────────────────────────

for _r in mcp_app.routes:
    app.router.routes.append(_r)


@app.middleware("http")
async def mcp_origin_guard(request: Request, call_next):
    """DNS-rebinding guard for /mcp: browsers send Origin; only allow our own (or none, e.g. CLI clients)."""
    if request.url.path.startswith("/mcp"):
        origin = request.headers.get("origin")
        allowed = {PUBLIC_URL, str(request.base_url).rstrip("/"), "https://claude.ai", "https://claude.com"}
        if origin and origin not in allowed:
            return JSONResponse({"detail": "origin not allowed"}, 403)
    return await call_next(request)


# ── Web UIs ───────────────────────────────────────────────────────────────────

def _html(name: str):
    return FileResponse(STATIC_DIR / name, media_type="text/html",
                        headers={"Cache-Control": "no-cache"})


@app.get("/", include_in_schema=False)
def ui_forest():
    return _html("index.html")


@app.get("/tasks", include_in_schema=False)
@app.get("/tasks/", include_in_schema=False)
def ui_tasks():
    return _html("tasks.html")


@app.get("/admin", include_in_schema=False)
def ui_admin():
    _admin()
    return _html("admin.html")


@app.get("/clip", include_in_schema=False)
def ui_clip():
    """Web clipper / share target page (bookmarklet + PWA share)."""
    return _html("clip.html")


@app.get("/manifest.webmanifest", include_in_schema=False)
def manifest():
    return FileResponse(STATIC_DIR / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/sw.js", include_in_schema=False)
def service_worker():
    return FileResponse(STATIC_DIR / "sw.js", media_type="text/javascript", headers={"Cache-Control": "no-cache"})


@app.get("/icon.svg", include_in_schema=False)
@app.get("/favicon.ico", include_in_schema=False)
def icon():
    return FileResponse(STATIC_DIR / "icon.svg", media_type="image/svg+xml")


if (STATIC_DIR / "lib").exists():
    app.mount("/lib", StaticFiles(directory=str(STATIC_DIR / "lib")), name="lib")


def main():
    uvicorn.run(app, host=SERVER_HOST, port=SERVER_PORT, proxy_headers=True)
