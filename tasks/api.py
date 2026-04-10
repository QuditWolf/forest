"""FastAPI app — hierarchical knowledge base API."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from typing import List, Optional

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import store
from .config import VALID_STATES, VALID_PRIORITIES, SERVER_HOST, SERVER_PORT

app = FastAPI(title="TaskThink API", version="0.2.0")

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
