"""Minimal OAuth 2.1 authorization server (authorization code + PKCE S256, refresh tokens,
dynamic client registration) - just enough for remote MCP clients such as Claude.ai connectors.

Consent requires being logged in to the web UI with the password. Issued tokens are ordinary
Forest tokens (kind "oauth") and show up - revocable - on the admin page.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import secrets
import threading
import time
from typing import Optional
from urllib.parse import urlencode, urlparse

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from . import auth
from .config import PUBLIC_URL, STATE_DIR

router = APIRouter()

ACCESS_DAYS = 7
CODE_TTL = 600
MAX_CLIENTS = 50

_lock = threading.Lock()
_codes: dict[str, dict] = {}


def base_url(request: Request) -> str:
    return PUBLIC_URL or str(request.base_url).rstrip("/")


# ── client registry ───────────────────────────────────────────────────────────

def _clients_path():
    return STATE_DIR / "oauth_clients.json"


def load_clients() -> dict:
    p = _clients_path()
    return json.loads(p.read_text()) if p.exists() else {}


def _save_clients(c: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    _clients_path().write_text(json.dumps(c, indent=2))


def delete_client(client_id: str) -> bool:
    with _lock:
        c = load_clients()
        if client_id in c:
            del c[client_id]
            _save_clients(c)
            return True
    return False


def _valid_redirect(uri: str) -> bool:
    u = urlparse(uri)
    if u.fragment:
        return False
    if u.scheme == "https" and u.netloc:
        return True
    return u.scheme == "http" and u.hostname in ("localhost", "127.0.0.1", "::1")


# ── discovery ─────────────────────────────────────────────────────────────────

@router.get("/.well-known/oauth-protected-resource")
@router.get("/.well-known/oauth-protected-resource/mcp")
def protected_resource(request: Request):
    b = base_url(request)
    return {
        "resource": f"{b}/mcp",
        "authorization_servers": [b],
        "scopes_supported": list(auth.SCOPES),
        "bearer_methods_supported": ["header"],
        "resource_name": "Forest",
    }


@router.get("/.well-known/oauth-authorization-server")
@router.get("/.well-known/oauth-authorization-server/mcp")
@router.get("/.well-known/openid-configuration")
def as_metadata(request: Request):
    b = base_url(request)
    return {
        "issuer": b,
        "authorization_endpoint": f"{b}/oauth/authorize",
        "token_endpoint": f"{b}/oauth/token",
        "registration_endpoint": f"{b}/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none", "client_secret_post", "client_secret_basic"],
        "scopes_supported": list(auth.SCOPES),
    }


# ── registration ──────────────────────────────────────────────────────────────

@router.post("/oauth/register")
async def register(request: Request):
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "invalid_client_metadata"}, 400)
    uris = body.get("redirect_uris") or []
    if not uris or not all(isinstance(u, str) and _valid_redirect(u) for u in uris):
        return JSONResponse({"error": "invalid_redirect_uri",
                             "error_description": "https (or http://localhost) redirect URIs required"}, 400)
    client_id = "fc_" + secrets.token_urlsafe(16)
    rec = {
        "client_id": client_id,
        "client_name": str(body.get("client_name") or "MCP client")[:80],
        "redirect_uris": uris,
        "created": int(time.time()),
    }
    with _lock:
        clients = load_clients()
        if len(clients) >= MAX_CLIENTS:  # drop oldest unused registrations
            for cid in sorted(clients, key=lambda k: clients[k]["created"])[: len(clients) - MAX_CLIENTS + 1]:
                del clients[cid]
        clients[client_id] = rec
        _save_clients(clients)
    auth.audit("oauth.register", client=rec["client_name"], client_id=client_id,
               ip=request.client.host if request.client else "")
    return JSONResponse({
        **rec,
        "client_id_issued_at": rec["created"],
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
    }, 201)


# ── authorize (consent) ───────────────────────────────────────────────────────

def _consent_html(client: dict, params: dict, csrf: str, vaults: list[str]) -> str:
    hidden = "".join(f'<input type="hidden" name="{html.escape(k)}" value="{html.escape(v)}">'
                     for k, v in params.items())
    host = urlparse(params["redirect_uri"]).netloc
    vault_opts = "".join(
        f'<label><input type="checkbox" name="vault" value="{html.escape(v)}" checked> {html.escape(v)}</label> '
        for v in vaults)
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>forest authorize</title><style>
body{{font-family:Menlo,Consolas,monospace;font-size:13px;max-width:420px;margin:12vh auto;padding:0 16px}}
h1{{font-size:15px}} .box{{border:1px solid #aaa;padding:14px;margin:12px 0}} label{{display:block;margin:4px 0}}
button{{font-family:inherit;font-size:13px;padding:5px 14px;border:1px solid #000;background:#fff;cursor:pointer;margin-right:6px}}
button.primary{{background:#000;color:#fff}} .dim{{color:#666}}</style></head><body>
<h1>authorize access</h1>
<div class="box"><b>{html.escape(client["client_name"])}</b> wants access to your forest vaults.<br>
<span class="dim">redirects to {html.escape(host)}</span></div>
<form method="post" action="/oauth/authorize">{hidden}<input type="hidden" name="csrf" value="{csrf}">
<div class="box"><div class="dim">access</div>
<label><input type="radio" name="grant_scope" value="read"> read only</label>
<label><input type="radio" name="grant_scope" value="write" checked> read + write</label>
<div class="dim" style="margin-top:8px">vaults</div>{vault_opts}</div>
<button class="primary" name="decision" value="allow">allow</button>
<button name="decision" value="deny">deny</button></form></body></html>"""


_AUTH_PARAMS = ("response_type", "client_id", "redirect_uri", "code_challenge",
                "code_challenge_method", "state", "scope", "resource")


@router.get("/oauth/authorize")
def authorize(request: Request):
    from .store import VAULTS
    q = request.query_params
    client = load_clients().get(q.get("client_id", ""))
    if not client:
        return HTMLResponse("unknown client_id, re-add the connector", 400)
    redirect_uri = q.get("redirect_uri", "")
    if redirect_uri not in client["redirect_uris"]:
        return HTMLResponse("redirect_uri not registered for this client", 400)
    if q.get("response_type") != "code" or q.get("code_challenge_method") != "S256" or not q.get("code_challenge"):
        return _redirect_error(redirect_uri, q.get("state"), "invalid_request", "PKCE S256 code flow required")
    if auth.auth_enabled() and not request.session.get("authenticated"):
        nxt = "/oauth/authorize?" + urlencode({k: q[k] for k in _AUTH_PARAMS if k in q})
        return RedirectResponse("/auth/login?" + urlencode({"next": nxt}), 302)
    csrf = secrets.token_urlsafe(16)
    request.session["oauth_csrf"] = csrf
    params = {k: q[k] for k in _AUTH_PARAMS if k in q}
    return HTMLResponse(_consent_html(client, params, csrf, list(VAULTS)))


@router.post("/oauth/authorize")
async def authorize_submit(request: Request):
    form = await request.form()
    if auth.auth_enabled() and not request.session.get("authenticated"):
        return HTMLResponse("login required", 401)
    if not form.get("csrf") or form.get("csrf") != request.session.pop("oauth_csrf", None):
        return HTMLResponse("stale form, go back and retry", 400)
    client = load_clients().get(str(form.get("client_id", "")))
    redirect_uri = str(form.get("redirect_uri", ""))
    if not client or redirect_uri not in client["redirect_uris"]:
        return HTMLResponse("invalid client", 400)
    state = form.get("state")
    if form.get("decision") != "allow":
        auth.audit("oauth.denied", client=client["client_name"])
        return _redirect_error(redirect_uri, state, "access_denied", "user denied")
    scope = "read" if form.get("grant_scope") == "read" else "write"
    vaults = [str(v) for v in form.getlist("vault")] or ["*"]
    from .store import VAULTS
    if set(vaults) >= set(VAULTS):
        vaults = ["*"]
    code = secrets.token_urlsafe(32)
    with _lock:
        _codes[code] = {
            "client_id": client["client_id"], "redirect_uri": redirect_uri,
            "challenge": str(form.get("code_challenge", "")), "scope": scope, "vaults": vaults,
            "exp": time.time() + CODE_TTL,
        }
    auth.audit("oauth.approved", client=client["client_name"], scope=scope, vaults=vaults)
    sep = "&" if "?" in redirect_uri else "?"
    qs = {"code": code, **({"state": state} if state else {})}
    return RedirectResponse(redirect_uri + sep + urlencode(qs), 302)


def _redirect_error(redirect_uri: str, state: Optional[str], err: str, desc: str):
    sep = "&" if "?" in redirect_uri else "?"
    qs = {"error": err, "error_description": desc, **({"state": state} if state else {})}
    return RedirectResponse(redirect_uri + sep + urlencode(qs), 302)


# ── token endpoint ────────────────────────────────────────────────────────────

def _token_response(pub: dict, access: str, refresh: str) -> JSONResponse:
    return JSONResponse({
        "access_token": access, "token_type": "Bearer", "expires_in": ACCESS_DAYS * 86400,
        "refresh_token": refresh, "scope": pub["scope"],
    }, headers={"Cache-Control": "no-store"})


@router.post("/oauth/token")
async def token(request: Request):
    form = await request.form()
    grant = form.get("grant_type")
    if grant == "authorization_code":
        code = str(form.get("code", ""))
        with _lock:
            rec = _codes.pop(code, None)
            for k in [k for k, v in _codes.items() if v["exp"] < time.time()]:
                _codes.pop(k, None)
        if not rec or rec["exp"] < time.time():
            return JSONResponse({"error": "invalid_grant"}, 400)
        if form.get("client_id") and form.get("client_id") != rec["client_id"]:
            return JSONResponse({"error": "invalid_grant"}, 400)
        if form.get("redirect_uri") and form.get("redirect_uri") != rec["redirect_uri"]:
            return JSONResponse({"error": "invalid_grant"}, 400)
        verifier = str(form.get("code_verifier", ""))
        digest = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        if not verifier or not secrets.compare_digest(digest, rec["challenge"]):
            return JSONResponse({"error": "invalid_grant", "error_description": "PKCE mismatch"}, 400)
        client = load_clients().get(rec["client_id"], {"client_name": "oauth"})
        pub, access, refresh = auth.tokens().create(
            f"{client['client_name']}", scope=rec["scope"], vaults=rec["vaults"],
            expires_days=ACCESS_DAYS, kind="oauth", client_id=rec["client_id"], with_refresh=True)
        auth.audit("oauth.token", client=client["client_name"], token_id=pub["id"])
        return _token_response(pub, access, refresh)
    if grant == "refresh_token":
        res = auth.tokens().rotate_refresh(str(form.get("refresh_token", "")), ACCESS_DAYS)
        if not res:
            return JSONResponse({"error": "invalid_grant"}, 400)
        return _token_response(*res)
    return JSONResponse({"error": "unsupported_grant_type"}, 400)
