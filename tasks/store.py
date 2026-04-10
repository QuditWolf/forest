"""
Filesystem data layer. Pure filesystem I/O, no HTTP.
All pages are .md files with YAML frontmatter at any depth under TASKS_ROOT.
A directory containing index.md is a "folder-page" and can have children.
"""
from __future__ import annotations

import hashlib
import re as _re
import shutil
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Optional

import frontmatter

from .config import TASKS_ROOT, safe_resolve, slugify
from .models import Page, TreeNode


# ── Internal helpers ──────────────────────────────────────────────────────────

_LINK_RE = _re.compile(r'\[\[([^\]]+)\]\]')


def _parse_date(val) -> Optional[date]:
    if val is None:
        return None
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, date):
        return val
    if isinstance(val, str):
        try:
            return date.fromisoformat(val)
        except ValueError:
            return None
    return None


def _parent_path_of(md_path: Path, is_folder: bool, root: Path) -> Optional[str]:
    """Determine canonical parent path. Returns None for root-level pages."""
    # For a folder-page (dir/index.md), its parent dir is dir.parent
    # For a leaf page (dir/page.md), its parent dir is dir
    if is_folder:
        parent_dir = md_path.parent.parent
    else:
        parent_dir = md_path.parent

    if parent_dir == root:
        return None

    parent_index = parent_dir / "index.md"
    if parent_index.exists():
        return str(parent_index.relative_to(root))

    # Parent dir exists but has no index.md — treat page as root-level
    return None


def _children_of(dir_path: Path, root: Path) -> List[str]:
    """List canonical paths of immediate children of a folder-page directory."""
    children = []
    if not dir_path.is_dir():
        return children
    for item in sorted(dir_path.iterdir()):
        if item.name.startswith(".") or item.name == "index.md":
            continue
        if item.is_file() and item.suffix == ".md":
            children.append(str(item.relative_to(root)))
        elif item.is_dir() and (item / "index.md").exists():
            children.append(str((item / "index.md").relative_to(root)))
    return children


def _parse_page(path: Path) -> Page:
    """Parse a Path into a Page. Path can be a .md file or a directory."""
    root = TASKS_ROOT
    if path.is_dir():
        md_path = path / "index.md"
        is_folder = True
    elif path.name == "index.md":
        md_path = path
        is_folder = (path.parent != root)
    else:
        md_path = path
        is_folder = False

    if not md_path.exists():
        raise FileNotFoundError(f"Page not found: {path}")

    post = frontmatter.load(str(md_path))
    canonical_path = str(md_path.relative_to(root))
    short_id = hashlib.sha1(canonical_path.encode()).hexdigest()[:6]

    meta = post.metadata
    if is_folder:
        name = meta.get("name") or md_path.parent.name.replace("-", " ").title()
    else:
        name = meta.get("name") or md_path.stem.replace("-", " ").title()

    return Page(
        path=canonical_path,
        short_id=short_id,
        name=name,
        parent_path=_parent_path_of(md_path, is_folder, root),
        is_folder=is_folder,
        children=_children_of(md_path.parent, root) if is_folder else [],
        state=meta.get("state"),
        priority=meta.get("priority"),
        due=_parse_date(meta.get("due")),
        created=_parse_date(meta.get("created")) or date.today(),
        completed=_parse_date(meta.get("completed")),
        content=post.content or "",
    )


def _write_page(md_path: Path, page: Page) -> None:
    """Write a Page back to disk with canonical frontmatter."""
    import yaml as _yaml

    if md_path.is_dir():
        md_path = md_path / "index.md"

    md_path.parent.mkdir(parents=True, exist_ok=True)

    meta: dict = {}
    if page.state is not None:
        meta["state"] = page.state
    if page.priority is not None:
        meta["priority"] = page.priority
    if page.due is not None:
        meta["due"] = page.due.isoformat()
    meta["created"] = page.created.isoformat()
    if page.completed is not None:
        meta["completed"] = page.completed.isoformat()
    meta["name"] = page.name

    yaml_str = _yaml.dump(meta, default_flow_style=False, sort_keys=False).strip()
    text = f"---\n{yaml_str}\n---\n{page.content or ''}"
    md_path.write_text(text, encoding="utf-8")


def _build_tree_children(dir_path: Path) -> List[TreeNode]:
    nodes = []
    if not dir_path.is_dir():
        return nodes
    for item in sorted(dir_path.iterdir()):
        if item.name.startswith("."):
            continue
        if item.is_file() and item.suffix == ".md" and item.name != "index.md":
            try:
                page = _parse_page(item)
                nodes.append(TreeNode(path=page.path, name=page.name, is_folder=False))
            except Exception:
                pass
        elif item.is_dir() and (item / "index.md").exists():
            try:
                page = _parse_page(item / "index.md")
                nodes.append(TreeNode(
                    path=page.path,
                    name=page.name,
                    is_folder=True,
                    children=_build_tree_children(item),
                ))
            except Exception:
                pass
    return nodes


# ── Public API ────────────────────────────────────────────────────────────────

def get_tree() -> List[TreeNode]:
    """Return the full navigation tree from root."""
    if not TASKS_ROOT.exists():
        return []
    return _build_tree_children(TASKS_ROOT)


def list_children(parent_path: Optional[str] = None) -> List[Page]:
    """List immediate child pages of a folder-page, or root-level pages."""
    if parent_path is None:
        dir_path = TASKS_ROOT
    else:
        abs_path = safe_resolve(parent_path)
        dir_path = abs_path.parent if abs_path.name == "index.md" else abs_path

    pages = []
    if not dir_path.exists():
        return pages

    for item in sorted(dir_path.iterdir()):
        if item.name.startswith("."):
            continue
        if item.is_file() and item.suffix == ".md":
            if parent_path is None and item.name == "index.md":
                continue
            try:
                pages.append(_parse_page(item))
            except Exception:
                pass
        elif item.is_dir() and (item / "index.md").exists():
            try:
                pages.append(_parse_page(item / "index.md"))
            except Exception:
                pass

    return pages


def get_page(path: str) -> Page:
    """Load a page by its canonical path. Backlinks not included (use get_backlinks)."""
    abs_path = safe_resolve(path)
    if not abs_path.exists():
        # Try resolving as directory
        if (abs_path.parent / abs_path.stem / "index.md").exists():
            abs_path = abs_path.parent / abs_path.stem / "index.md"
        else:
            raise FileNotFoundError(f"Page not found: {path}")
    return _parse_page(abs_path)


def create_page(
    parent_path: Optional[str],
    name: str,
    *,
    content: str = "",
    state: Optional[str] = None,
    priority: Optional[str] = None,
    due: Optional[date] = None,
    as_folder: bool = False,
) -> Page:
    """Create a new page. as_folder=True creates a directory-page (dir/index.md)."""
    slug = slugify(name)

    if parent_path is None:
        parent_dir = TASKS_ROOT
    else:
        abs_parent = safe_resolve(parent_path)
        if abs_parent.name == "index.md":
            parent_dir = abs_parent.parent
        elif abs_parent.is_dir():
            parent_dir = abs_parent
        else:
            # Leaf page as parent: create sibling in the same directory
            parent_dir = abs_parent.parent

    parent_dir.mkdir(parents=True, exist_ok=True)

    if as_folder:
        new_dir = parent_dir / slug
        i = 2
        while new_dir.exists():
            new_dir = parent_dir / f"{slug}-{i}"
            i += 1
        new_dir.mkdir(parents=True)
        file_path = new_dir / "index.md"
    else:
        file_path = parent_dir / f"{slug}.md"
        i = 2
        while file_path.exists():
            file_path = parent_dir / f"{slug}-{i}.md"
            i += 1

    page = Page(
        path=str(file_path.relative_to(TASKS_ROOT)),
        name=name,
        parent_path=parent_path,
        is_folder=as_folder,
        state=state,
        priority=priority,
        due=due,
        created=date.today(),
        content=content,
    )
    _write_page(file_path, page)
    return _parse_page(file_path)


def update_page(path: str, **fields) -> Page:
    """Update arbitrary fields on a page and write it back to disk."""
    page = get_page(path)
    abs_path = safe_resolve(path)

    for key, value in fields.items():
        setattr(page, key, value)

    # Auto-stamp completion when marked done; clear when un-done
    if "state" in fields:
        if fields["state"] == "done" and page.completed is None:
            page.completed = date.today()
        elif fields["state"] != "done":
            page.completed = None

    _write_page(abs_path, page)
    return _parse_page(abs_path)


def move_page(path: str, new_parent_path: Optional[str]) -> Page:
    """Move a page (and its children) under a new parent."""
    src = safe_resolve(path)
    if not src.exists():
        raise FileNotFoundError(f"Page not found: {path}")

    if new_parent_path is None:
        dest_dir = TASKS_ROOT
    else:
        abs_parent = safe_resolve(new_parent_path)
        dest_dir = abs_parent.parent if abs_parent.name == "index.md" else abs_parent
    dest_dir.mkdir(parents=True, exist_ok=True)

    dest = dest_dir / src.name
    if dest.exists():
        raise FileExistsError(f"A page already exists at destination: {dest}")

    shutil.move(str(src), str(dest))

    new_path = str(dest.relative_to(TASKS_ROOT))
    if dest.is_dir():
        new_path = str((dest / "index.md").relative_to(TASKS_ROOT))
    return get_page(new_path)


def promote_to_folder(path: str) -> Page:
    """Convert a leaf page to a folder-page (moves content into dir/index.md)."""
    abs_path = safe_resolve(path)
    if abs_path.name == "index.md":
        raise ValueError(f"Page is already a folder: {path}")
    if not abs_path.exists():
        raise FileNotFoundError(f"Page not found: {path}")

    new_dir = abs_path.parent / abs_path.stem
    if new_dir.exists():
        raise FileExistsError(f"Directory already exists: {new_dir}")

    new_dir.mkdir()
    new_index = new_dir / "index.md"
    shutil.move(str(abs_path), str(new_index))
    return _parse_page(new_index)


def delete_page(path: str) -> None:
    """Soft-delete a page (moved to .deleted/ inside TASKS_ROOT)."""
    abs_path = safe_resolve(path)
    if not abs_path.exists():
        raise FileNotFoundError(f"Page not found: {path}")

    trash = TASKS_ROOT / ".deleted"
    trash.mkdir(exist_ok=True)

    dest = trash / abs_path.name
    if dest.exists():
        stem = abs_path.stem if abs_path.is_file() else abs_path.name
        suffix = abs_path.suffix if abs_path.is_file() else ""
        ts = datetime.now().strftime("%Y%m%d%H%M%S")
        dest = trash / f"{stem}-{ts}{suffix}"

    shutil.move(str(abs_path), str(dest))


def append_to_page(path: str, text: str) -> Page:
    """Append a timestamped note to the page body."""
    page = get_page(path)
    abs_path = safe_resolve(path)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    page.content = (page.content or "").rstrip() + f"\n\n**{timestamp}**: {text}"
    _write_page(abs_path, page)
    return _parse_page(abs_path)


def search_pages(query: str) -> List[dict]:
    """Full-text search across all pages. Returns [{path, name, snippet}]."""
    results = []
    if not query or not TASKS_ROOT.exists():
        return results

    query_lower = query.lower()
    for md_path in sorted(TASKS_ROOT.rglob("*.md")):
        parts = md_path.relative_to(TASKS_ROOT).parts
        if any(p.startswith(".") for p in parts):
            continue
        try:
            text = md_path.read_text(encoding="utf-8")
            if query_lower not in text.lower():
                continue
            idx = text.lower().index(query_lower)
            start = max(0, idx - 60)
            end = min(len(text), idx + len(query) + 60)
            snippet = text[start:end].replace("\n", " ").strip()
            rel = str(md_path.relative_to(TASKS_ROOT))
            try:
                page = _parse_page(md_path)
                name = page.name
            except Exception:
                name = md_path.stem.replace("-", " ").title()
            results.append({"path": rel, "name": name, "snippet": f"...{snippet}..."})
        except Exception:
            pass

    return results


def resolve_page_ref(ref: str, current_path: str) -> str:
    """Resolve a [[ref]] link to a canonical path.
    ref can be:
      ./relative or ../relative  → relative to current page's directory
      absolute/path              → root-relative
    """
    ref = ref.strip()
    root = TASKS_ROOT

    if ref.startswith("./") or ref.startswith("../"):
        current_abs = (root / current_path).resolve()
        # For folder-pages, "current dir" is the directory itself
        if current_abs.name == "index.md":
            current_dir = current_abs.parent.parent
        else:
            current_dir = current_abs.parent
        resolved_base = (current_dir / ref).resolve()
    else:
        resolved_base = (root / ref).resolve()

    root_str = str(root.resolve())
    for candidate in [
        resolved_base,
        Path(str(resolved_base) + ".md"),
        resolved_base / "index.md",
    ]:
        if candidate.exists() and str(candidate).startswith(root_str):
            return str(candidate.relative_to(root))

    raise FileNotFoundError(f"Page reference not found: {ref!r}")


def get_backlinks(path: str) -> List[str]:
    """Find all pages that [[link]] to the given page."""
    results = []
    if not TASKS_ROOT.exists():
        return results

    for md_path in TASKS_ROOT.rglob("*.md"):
        parts = md_path.relative_to(TASKS_ROOT).parts
        if any(p.startswith(".") for p in parts):
            continue
        try:
            text = md_path.read_text(encoding="utf-8")
            refs = _LINK_RE.findall(text)
            if not refs:
                continue
            rel = str(md_path.relative_to(TASKS_ROOT))
            if rel == path:
                continue
            for ref in refs:
                try:
                    if resolve_page_ref(ref.strip(), rel) == path:
                        results.append(rel)
                        break
                except Exception:
                    pass
        except Exception:
            pass

    return results


# ── Backward-compat shims for CLI ─────────────────────────────────────────────

def parse_due_window(window: str) -> Optional[date]:
    window = window.strip().lower()
    if window in ("all", "none", ""):
        return None
    m = _re.match(r"^(\d+)([dwmy])$", window)
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2)
    today = date.today()
    if unit == "d":
        return today + timedelta(days=n)
    if unit == "w":
        return today + timedelta(weeks=n)
    if unit == "m":
        month = today.month + n
        year  = today.year + (month - 1) // 12
        month = ((month - 1) % 12) + 1
        import calendar
        day = min(today.day, calendar.monthrange(year, month)[1])
        return today.replace(year=year, month=month, day=day)
    if unit == "y":
        try:
            return today.replace(year=today.year + n)
        except ValueError:
            return today.replace(year=today.year + n, day=28)
    return None


def filter_tasks(tasks: list, state: str = "all", by_priority: Optional[dict] = None) -> list:
    result = tasks
    if state == "undone":
        result = [t for t in result if t.state != "done"]
    elif state == "done":
        result = [t for t in result if t.state == "done"]
    if by_priority:
        filtered = []
        for t in result:
            window = (by_priority.get(t.priority or "medium") or "all").strip().lower()
            if window == "all":
                filtered.append(t)
            elif window == "none":
                pass
            else:
                cutoff = parse_due_window(window)
                if cutoff is None:
                    filtered.append(t)
                elif t.due is None:
                    pass
                elif t.due <= cutoff:
                    filtered.append(t)
        result = filtered
    return result


def resolve_task_id(ref: str) -> str:
    """Accept full page path or 6-char short_id. Returns the canonical path."""
    path = TASKS_ROOT / ref
    if path.exists():
        if path.is_dir() and (path / "index.md").exists():
            return str((path / "index.md").relative_to(TASKS_ROOT))
        return ref
    # Short ID scan
    for md_path in TASKS_ROOT.rglob("*.md"):
        parts = md_path.relative_to(TASKS_ROOT).parts
        if any(p.startswith(".") for p in parts):
            continue
        rel = str(md_path.relative_to(TASKS_ROOT))
        sid = hashlib.sha1(rel.encode()).hexdigest()[:6]
        if sid == ref:
            return rel
    raise FileNotFoundError(f"No page found for: {ref!r}")


def list_tasks(category: Optional[str] = None) -> list:
    """List pages, optionally under a top-level directory. Compat shim."""
    if category:
        return list_children(f"{category}/index.md") + [
            p for p in list_children(None)
            if not p.is_folder and p.path.startswith(f"{category}/")
        ]
    # Flat list of all pages
    pages = []
    if not TASKS_ROOT.exists():
        return pages
    for md_path in sorted(TASKS_ROOT.rglob("*.md")):
        parts = md_path.relative_to(TASKS_ROOT).parts
        if any(p.startswith(".") for p in parts):
            continue
        try:
            pages.append(_parse_page(md_path))
        except Exception:
            pass
    return pages


def list_categories() -> list:
    """List top-level directories as categories. Compat shim."""
    from .models import Page
    result = []
    if not TASKS_ROOT.exists():
        return result
    for item in sorted(TASKS_ROOT.iterdir()):
        if item.is_dir() and not item.name.startswith("."):
            count = sum(1 for p in list_tasks(item.name) if p.state not in (None, "done"))
            # Return a simple namespace-compatible dict
            result.append({"name": item.name, "task_count": count})
    return result


# Compat aliases
def get_task(path: str) -> Page:
    return get_page(path)


def create_task(category: str, name: str, *, priority: str = "medium",
                due: Optional[date] = None, content: str = "") -> Page:
    return create_page(f"{category}/index.md" if (TASKS_ROOT / category / "index.md").exists()
                       else None, name, priority=priority, due=due, content=content)


def update_task(path: str, **fields) -> Page:
    return update_page(path, **fields)


def move_task(path: str, new_category: str) -> Page:
    parent = f"{new_category}/index.md" if (TASKS_ROOT / new_category / "index.md").exists() else None
    return move_page(path, parent)


def append_note(path: str, text: str) -> None:
    append_to_page(path, text)


def delete_task(path: str) -> None:
    delete_page(path)
