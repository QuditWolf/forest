#!/usr/bin/env python3
"""
Forest MCP Server — Model Context Protocol interface to the Forest knowledge base.

Exposes Forest's store layer as native MCP tools, bypassing the HTTP API entirely.
Designed for OpenCode (or any MCP-compatible client) via stdio transport.

Usage (stdio, default):
    /workspace/task_think/.venv/bin/python /workspace/task_think/forest_mcp.py
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

# Ensure the forest package is importable when run as a script
_project_root = Path(__file__).parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from tasks import store
from tasks.config import VALID_STATES, VALID_PRIORITIES
from tasks.models import Page, TreeNode

# ── Server setup ──────────────────────────────────────────────────────────────

mcp = FastMCP(
    "Forest",
    instructions=(
        "Forest is a hierarchical markdown knowledge base stored as plain .md files. "
        "Pages live in a tree structure with optional task metadata (state, priority, due). "
        "Use [[wikilinks]] for internal links — never markdown [text](url) for internal refs. "
        "Always search with forest_search before creating to avoid duplicates. "
        "Use forest_append for additive writes — don't use forest_update to add notes "
        "as it replaces the entire content body."
    ),
)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _page_to_dict(page: Page) -> dict:
    """Convert Page to a clean dict, serialising dates to ISO strings."""
    d = page.model_dump()
    for key in ("due", "created", "completed"):
        if d.get(key) is not None:
            d[key] = str(d[key])
    return d


def _tree_to_dict(node: TreeNode) -> dict:
    return {
        "path": node.path,
        "name": node.name,
        "is_folder": node.is_folder,
        "children": [_tree_to_dict(c) for c in node.children],
    }


def _ok(data: Any) -> str:
    return json.dumps(data, indent=2, default=str)


def _err(msg: str) -> str:
    return json.dumps({"error": msg})


# ── Tools ─────────────────────────────────────────────────────────────────────


@mcp.tool()
def forest_tree() -> str:
    """Browse the full Forest page hierarchy as a nested tree.

    Returns the complete navigation tree showing all pages and their nesting.
    Use this to understand the knowledge base structure before navigating.
    Folder pages have is_folder=true and may have children.
    """
    tree = store.get_tree()
    return _ok([_tree_to_dict(n) for n in tree])


@mcp.tool()
def forest_children(path: Optional[str] = None) -> str:
    """List immediate children of a folder page, or root-level pages.

    Args:
        path: Canonical path of a folder page (e.g. "projects/index.md").
              Omit or pass null to list root-level pages.

    Returns full Page objects for each child including metadata.
    """
    try:
        pages = store.list_children(path)
        return _ok([_page_to_dict(p) for p in pages])
    except Exception as e:
        return _err(str(e))


@mcp.tool()
def forest_read(path: str) -> str:
    """Read a Forest page by its canonical path or 6-char short_id.

    Args:
        path: Canonical path (e.g. "projects/alpha.md" or "projects/index.md")
              or 6-char short_id (e.g. "a1b2c3").

    Returns the full page including markdown content, metadata, and children list.
    Backlinks are NOT included — use forest_backlinks for those.
    """
    # Try direct path first, then short_id resolution
    try:
        page = store.get_page(path)
    except FileNotFoundError:
        try:
            resolved = store.resolve_task_id(path)
            page = store.get_page(resolved)
        except FileNotFoundError:
            return _err(f"Page not found: {path}")
        except Exception as e:
            return _err(str(e))
    except Exception as e:
        return _err(str(e))
    return _ok(_page_to_dict(page))


@mcp.tool()
def forest_search(query: str) -> str:
    """Full-text search across all Forest pages. Case-insensitive substring match.

    Args:
        query: Search terms to find in page content and metadata.

    Returns matching pages with path, name, and a ~120-char snippet showing
    context around the first match. Always search before creating new pages
    to avoid duplicates.
    """
    results = store.search_pages(query)
    if not results:
        return _ok({"message": f"No pages found matching '{query}'", "results": []})
    return _ok({"count": len(results), "results": results})


@mcp.tool()
def forest_create(
    name: str,
    content: str = "",
    parent_path: Optional[str] = None,
    as_folder: bool = False,
    state: Optional[str] = None,
    priority: Optional[str] = None,
    due: Optional[str] = None,
) -> str:
    """Create a new page in Forest.

    Args:
        name: Display name for the page (also used to generate the filename slug).
        content: Markdown body. Use [[wikilinks]] for internal links, never [text](url).
        parent_path: Path of parent folder-page to nest under (e.g. "projects/index.md").
                     Omit or null for a root-level page.
        as_folder: If true, creates a folder-page (directory/index.md) that can have children.
        state: Task state — one of: todo, in-progress, blocked, waiting, done.
        priority: Priority level — one of: high, medium, low.
        due: Due date in YYYY-MM-DD format.

    Always run forest_search first to avoid creating duplicate pages.
    """
    if state and state not in VALID_STATES:
        return _err(f"Invalid state '{state}'. Valid: {VALID_STATES}")
    if priority and priority not in VALID_PRIORITIES:
        return _err(f"Invalid priority '{priority}'. Valid: {VALID_PRIORITIES}")

    due_date: Optional[date] = None
    if due:
        try:
            due_date = date.fromisoformat(due)
        except ValueError:
            return _err(f"Invalid date '{due}'. Use YYYY-MM-DD.")

    try:
        page = store.create_page(
            parent_path,
            name,
            content=content,
            state=state,
            priority=priority,
            due=due_date,
            as_folder=as_folder,
        )
        return _ok({"created": _page_to_dict(page)})
    except FileExistsError as e:
        return _err(f"Page already exists: {e}")
    except Exception as e:
        return _err(str(e))


@mcp.tool()
def forest_update(
    path: str,
    name: Optional[str] = None,
    content: Optional[str] = None,
    state: Optional[str] = None,
    priority: Optional[str] = None,
    due: Optional[str] = None,
) -> str:
    """Update fields on an existing Forest page. Only provided fields are changed.

    Args:
        path: Canonical path of the page to update.
        name: New display name.
        content: New markdown body — REPLACES the entire existing content.
                 Use forest_append instead if you only want to add a note.
        state: New task state — one of: todo, in-progress, blocked, waiting, done.
               Setting to 'done' auto-stamps the completed date.
        priority: New priority — one of: high, medium, low.
        due: New due date in YYYY-MM-DD format.

    WARNING: content replaces the full body. Use forest_append to add without losing content.
    """
    if state and state not in VALID_STATES:
        return _err(f"Invalid state '{state}'. Valid: {VALID_STATES}")
    if priority and priority not in VALID_PRIORITIES:
        return _err(f"Invalid priority '{priority}'. Valid: {VALID_PRIORITIES}")

    fields: dict[str, Any] = {}
    if name is not None:
        fields["name"] = name
    if content is not None:
        fields["content"] = content
    if state is not None:
        fields["state"] = state
    if priority is not None:
        fields["priority"] = priority
    if due is not None:
        try:
            fields["due"] = date.fromisoformat(due)
        except ValueError:
            return _err(f"Invalid date '{due}'. Use YYYY-MM-DD.")

    if not fields:
        return _err("No fields to update. Provide at least one of: name, content, state, priority, due.")

    try:
        page = store.update_page(path, **fields)
        return _ok({"updated": _page_to_dict(page)})
    except FileNotFoundError:
        return _err(f"Page not found: {path}")
    except Exception as e:
        return _err(str(e))


@mcp.tool()
def forest_append(path: str, text: str) -> str:
    """Append a timestamped note to a Forest page. Non-destructive — preserves all existing content.

    Args:
        path: Canonical path of the page to append to.
        text: The note text to append. Auto-timestamped as: **YYYY-MM-DD HH:MM**: <text>

    This is the preferred way to add information to existing pages.
    Never use forest_update(content=...) just to add a note — use this instead.
    """
    try:
        page = store.append_to_page(path, text)
        return _ok({"appended_to": path, "page": _page_to_dict(page)})
    except FileNotFoundError:
        return _err(f"Page not found: {path}")
    except Exception as e:
        return _err(str(e))


@mcp.tool()
def forest_delete(path: str) -> str:
    """Soft-delete a Forest page. Moved to .shadow/ — fully recoverable via forest_restore.

    Args:
        path: Canonical path of the page to delete.

    The page and all its children (if a folder) are moved to .shadow/ preserving structure.
    Use forest_shadow to list deleted pages and forest_restore to recover them.
    """
    try:
        store.delete_page(path)
        return _ok({"deleted": path, "recoverable": True, "recover_with": "forest_restore"})
    except FileNotFoundError:
        return _err(f"Page not found: {path}")
    except Exception as e:
        return _err(str(e))


@mcp.tool()
def forest_move(path: str, new_parent_path: Optional[str] = None) -> str:
    """Move a Forest page (and its children if a folder) under a new parent.

    Args:
        path: Canonical path of the page to move.
        new_parent_path: Path of the new parent folder-page (e.g. "archive/index.md").
                         Omit or null to move to root level.
    """
    try:
        page = store.move_page(path, new_parent_path)
        return _ok({"moved": _page_to_dict(page)})
    except FileNotFoundError:
        return _err(f"Page not found: {path}")
    except FileExistsError as e:
        return _err(f"Destination conflict: {e}")
    except Exception as e:
        return _err(str(e))


@mcp.tool()
def forest_promote(path: str) -> str:
    """Convert a leaf page to a folder-page so it can have children.

    Args:
        path: Canonical path of the leaf page to promote (must NOT be index.md already).

    The .md file is moved into a new directory as index.md.
    Example: projects/alpha.md → projects/alpha/index.md
    """
    try:
        page = store.promote_to_folder(path)
        return _ok({"promoted": _page_to_dict(page)})
    except (FileNotFoundError, FileExistsError, ValueError) as e:
        return _err(str(e))


@mcp.tool()
def forest_backlinks(path: str) -> str:
    """Find all pages that contain [[wikilinks]] pointing to the given page.

    Args:
        path: Canonical path of the target page.

    Returns a list of page paths that link to this page.
    Computed live by scanning all pages — no stored index.
    """
    try:
        links = store.get_backlinks(path)
        return _ok({"target": path, "count": len(links), "backlinks": links})
    except Exception as e:
        return _err(str(e))


@mcp.tool()
def forest_shadow() -> str:
    """List all soft-deleted pages in .shadow/ that can be restored.

    Use the shadow_path values from results with forest_restore to recover pages.
    """
    try:
        items = store.list_shadow()
        return _ok({"count": len(items), "deleted_pages": items})
    except Exception as e:
        return _err(str(e))


@mcp.tool()
def forest_restore(shadow_path: str) -> str:
    """Restore a soft-deleted page from .shadow/ back to its original location.

    Args:
        shadow_path: Relative path within .shadow/ as returned by forest_shadow.
                     Example: "projects/alpha.md"

    If the original location is occupied, the page is restored with a -restored-HHMMSS suffix.
    """
    try:
        page = store.restore_from_shadow(shadow_path)
        return _ok({"restored": _page_to_dict(page)})
    except FileNotFoundError as e:
        return _err(str(e))
    except Exception as e:
        return _err(str(e))


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run(transport="stdio")
