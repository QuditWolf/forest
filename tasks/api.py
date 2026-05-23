"""FastAPI app — hierarchical knowledge base API."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from typing import List, Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

from . import store
from .config import VALID_STATES, VALID_PRIORITIES, SERVER_HOST, SERVER_PORT, FOREST_PASSWORD, SESSION_SECRET

app = FastAPI(title="TaskThink API", version="0.2.0")


# ── Auth helpers ───────────────────────────────────────────────────────────────

def _auth_enabled() -> bool:
    return bool(FOREST_PASSWORD)


def _is_authenticated(request: Request) -> bool:
    if not _auth_enabled():
        return True
    return request.session.get("authenticated") is True


LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>forest — login</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      background: #ffffff;
      color: #000000;
      font-family: Menlo, Consolas, "DejaVu Sans Mono", monospace;
      font-size: 13px;
    }
    .card {
      width: 100%;
      max-width: 320px;
      padding: 2rem;
    }
    h1 {
      font-size: 13px;
      font-weight: normal;
      letter-spacing: 2px;
      text-transform: uppercase;
      color: #0066cc;
      margin-bottom: 2rem;
    }
    label {
      display: block;
      font-size: 11px;
      color: #555555;
      text-transform: uppercase;
      letter-spacing: 1px;
      margin-bottom: 0.4rem;
    }
    input[type=password] {
      width: 100%;
      padding: 0.45rem 0.6rem;
      background: #ffffff;
      border: 1px solid #aaaaaa;
      border-radius: 3px;
      color: #000000;
      font-family: inherit;
      font-size: 13px;
      outline: none;
      transition: border-color 0.2s, box-shadow 0.2s;
    }
    input[type=password]:focus {
      border-color: #0066cc;
      box-shadow: 0 0 0 2px rgba(0,102,204,0.15);
    }
    button {
      margin-top: 1rem;
      width: 100%;
      padding: 0.45rem 0.6rem;
      background: #000000;
      color: #ffffff;
      border: none;
      border-radius: 3px;
      font-family: inherit;
      font-size: 13px;
      letter-spacing: 1px;
      text-transform: uppercase;
      cursor: pointer;
      transition: background 0.15s;
    }
    button:hover { background: #0066cc; }
    .error {
      margin-top: 0.75rem;
      font-size: 11px;
      color: #cc0000;
      border-left: 2px solid #cc0000;
      padding-left: 0.5rem;
    }
  </style>
</head>
<body>
  <div class="card">
    <h1>forest</h1>
    <form method="post" action="/auth/login">
      <label for="password">password</label>
      <input type="password" id="password" name="password" autofocus required/>
      {error_block}
      <button type="submit">enter</button>
    </form>
  </div>
</body>
</html>"""


# ── Auth routes ────────────────────────────────────────────────────────────────

@app.get("/auth/login", response_class=HTMLResponse, include_in_schema=False)
def login_page(error: str = ""):
    err = '<p class="error">incorrect password</p>' if error else ""
    return LOGIN_HTML.replace("{error_block}", err)


@app.post("/auth/login", response_class=HTMLResponse, include_in_schema=False)
async def login_submit(request: Request, password: str = Form(...)):
    if FOREST_PASSWORD and password == FOREST_PASSWORD:
        request.session["authenticated"] = True
        return RedirectResponse("/", status_code=303)
    return RedirectResponse("/auth/login?error=1", status_code=303)


@app.get("/auth/logout", include_in_schema=False)
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/auth/login", status_code=303)


# ── Auth middleware (protect all /api/* and / routes) ─────────────────────────

@app.middleware("http")
async def require_auth(request: Request, call_next):
    path = request.url.path
    # Always allow auth endpoints and static login assets
    if not _auth_enabled() or path.startswith("/auth/"):
        return await call_next(request)
    if not _is_authenticated(request):
        if path.startswith("/api/"):
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        return RedirectResponse(f"/auth/login", status_code=302)
    return await call_next(request)

# SessionMiddleware must be added AFTER auth middleware so it wraps outermost
# (last add_middleware = outermost = processes request first, populating request.session
#  before the auth middleware runs)
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, session_cookie="forest_session", max_age=60 * 60 * 24 * 30)  # 30-day cookie

STATIC_DIR = Path(__file__).parent.parent / "static"


# ── Request / response schemas ─────────────────────────────────────────────────


class PageCreate(BaseModel):
    name: str
    content: str = ""
    parent_path: Optional[str] = None  # None = root level
    as_folder: bool = False
    state: Optional[str] = None
    priority: Optional[str] = None
    due: Optional[date] = None


class PageUpdate(BaseModel):
    name: Optional[str] = None
    content: Optional[str] = None
    state: Optional[str] = None
    priority: Optional[str] = None
    due: Optional[date] = None


class MoveRequest(BaseModel):
    new_parent_path: Optional[str] = None  # None = move to root


class AppendRequest(BaseModel):
    text: str


class PromoteRequest(BaseModel):
    pass


# ── Helpers ────────────────────────────────────────────────────────────────────


def _validate_meta(state: Optional[str], priority: Optional[str]) -> None:
    if state is not None and state not in VALID_STATES:
        raise HTTPException(400, f"Invalid state. Choose: {VALID_STATES}")
    if priority is not None and priority not in VALID_PRIORITIES:
        raise HTTPException(400, f"Invalid priority. Choose: {VALID_PRIORITIES}")


# ── Tree & navigation ──────────────────────────────────────────────────────────


@app.get("/api/tree")
def get_tree():
    """Full navigation tree for sidebar."""
    return store.get_tree()


@app.get("/api/children")
def get_root_children():
    """List root-level pages."""
    return store.list_children(None)


@app.get("/api/children/{parent_path:path}")
def get_children(parent_path: str):
    """List immediate children of a folder-page."""
    try:
        return store.list_children(parent_path)
    except Exception as e:
        raise HTTPException(404, str(e))


# ── Page CRUD ──────────────────────────────────────────────────────────────────


@app.get("/api/page/{path:path}")
def get_page(path: str):
    """Get a page by its canonical path."""
    try:
        return store.get_page(path)
    except FileNotFoundError:
        raise HTTPException(404, f"Page not found: {path}")


@app.post("/api/pages", status_code=201)
def create_page(body: PageCreate):
    """Create a new page. Set body.parent_path to nest under an existing page."""
    _validate_meta(body.state, body.priority)
    try:
        return store.create_page(
            body.parent_path,
            body.name,
            content=body.content,
            state=body.state,
            priority=body.priority,
            due=body.due,
            as_folder=body.as_folder,
        )
    except FileExistsError as e:
        raise HTTPException(409, str(e))
    except Exception as e:
        raise HTTPException(500, str(e))


@app.patch("/api/page/{path:path}")
def update_page(path: str, body: PageUpdate):
    """Update page fields. Only fields present in the body are changed."""
    updates = body.model_dump(exclude_unset=True)
    _validate_meta(updates.get("state"), updates.get("priority"))
    try:
        return store.update_page(path, **updates)
    except FileNotFoundError:
        raise HTTPException(404, f"Page not found: {path}")
    except Exception as e:
        raise HTTPException(500, str(e))


@app.delete("/api/page/{path:path}", status_code=204)
def delete_page(path: str):
    """Soft-delete a page (moved to .deleted/)."""
    try:
        store.delete_page(path)
    except FileNotFoundError:
        raise HTTPException(404, f"Page not found: {path}")


@app.post("/api/page/{path:path}/move")
def move_page(path: str, body: MoveRequest):
    """Move a page under a new parent (or to root if new_parent_path is null)."""
    try:
        return store.move_page(path, body.new_parent_path)
    except FileNotFoundError:
        raise HTTPException(404, f"Page not found: {path}")
    except FileExistsError as e:
        raise HTTPException(409, str(e))


@app.post("/api/page/{path:path}/promote")
def promote_page(path: str):
    """Convert a leaf page to a folder-page (so it can have children)."""
    try:
        return store.promote_to_folder(path)
    except (FileNotFoundError, FileExistsError, ValueError) as e:
        raise HTTPException(400, str(e))


@app.post("/api/page/{path:path}/append")
def append_to_page(path: str, body: AppendRequest):
    """Append a timestamped note to a page. Agent-friendly write endpoint."""
    try:
        return store.append_to_page(path, body.text)
    except FileNotFoundError:
        raise HTTPException(404, f"Page not found: {path}")


# ── Discovery ──────────────────────────────────────────────────────────────────


@app.get("/api/search")
def search_pages(q: str = ""):
    """Full-text search across all pages."""
    return store.search_pages(q)


@app.get("/api/backlinks/{path:path}")
def get_backlinks(path: str):
    """Get paths of all pages that [[link]] to this page."""
    return store.get_backlinks(path)


@app.get("/api/shadow")
def list_shadow():
    """List all deleted pages in .shadow."""
    return store.list_shadow()


@app.post("/api/shadow/restore")
def restore_shadow(body: dict):
    """Restore a page from .shadow. body = {"shadow_path": "relative/path/in/shadow"}"""
    try:
        return store.restore_from_shadow(body.get("shadow_path", ""))
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(500, str(e))


# ── Static files (catch-all last) ─────────────────────────────────────────────

if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")


def main():
    uvicorn.run(app, host=SERVER_HOST, port=SERVER_PORT)
