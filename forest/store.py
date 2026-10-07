"""
Filesystem data layer. Pure filesystem I/O (+ git commits), no HTTP.

A server holds several *vaults* (e.g. "forest" and "tasks"). Each vault is a
directory of .md pages with YAML frontmatter and is its own git repo.
A directory containing index.md is a "folder-page" and can have children.

Cross-vault references use a vault prefix:  [[tasks:work/report]]  #tag
"""

from __future__ import annotations

import contextvars
import fnmatch
import hashlib
import re as _re
import shutil
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional, Tuple

import frontmatter
import yaml as _yaml

from . import gitrepo
from .config import AUTOCOMMIT, TASK_VAULTS, VALID_PRIORITIES, VALID_STATES, slugify, vault_paths
from .models import Page, TreeNode


# Who is making the current change - set per request by the API/MCP layer,
# used as git commit author and in the audit log.
actor: contextvars.ContextVar[str] = contextvars.ContextVar("actor", default="local")


class ConflictError(Exception):
    """expected_sha did not match the file on disk (someone else changed it)."""


class ReadOnlyPageError(PermissionError):
    """Page (or an ancestor folder) has `ai: readonly` and the caller is a token (agent/API)."""


# ── Parsing helpers ───────────────────────────────────────────────────────────

# [[ref]]  [[vault:ref]]  [[ref|alias]]  [[ref#heading]]
_LINK_RE = _re.compile(r"\[\[([^\]\|#]+)(?:#[^\]\|]*)?(?:\|[^\]]*)?\]\]")
_TAG_RE = _re.compile(r"(?<![\w/#&\]\[(])#([A-Za-z][\w/-]*)")
_FENCE_RE = _re.compile(r"```.*?```", _re.S)
_INLINE_CODE_RE = _re.compile(r"`[^`\n]*`")
_KNOWN_META = {"name", "state", "priority", "due", "created", "completed", "tags"}


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


def _norm_tags(val) -> List[str]:
    if not val:
        return []
    if isinstance(val, str):
        val = _re.split(r"[,\s]+", val)
    out = []
    for t in val:
        t = str(t).strip().lstrip("#")
        if t and t not in out:
            out.append(t)
    return out


def inline_tags(content: str) -> List[str]:
    text = _INLINE_CODE_RE.sub("", _FENCE_RE.sub("", content or ""))
    out: List[str] = []
    for t in _TAG_RE.findall(text):
        t = t.rstrip("/-")
        if t not in out:
            out.append(t)
    return out


def link_refs(content: str) -> List[str]:
    text = _FENCE_RE.sub("", content or "")
    return [r.strip() for r in _LINK_RE.findall(text)]


def _sha(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()[:12]


def parse_due_window(window: str) -> Optional[date]:
    """'7d' | '2w' | '1m' | '1y' → cutoff date (inclusive). 'all'/'none'/invalid → None."""
    window = (window or "").strip().lower()
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
        import calendar
        month = today.month + n
        year = today.year + (month - 1) // 12
        month = ((month - 1) % 12) + 1
        day = min(today.day, calendar.monthrange(year, month)[1])
        return today.replace(year=year, month=month, day=day)
    try:
        return today.replace(year=today.year + n)
    except ValueError:  # Feb 29
        return today.replace(year=today.year + n, day=28)


def _validate_meta(state: Optional[str], priority: Optional[str]) -> None:
    if state is not None and state not in VALID_STATES:
        raise ValueError(f"Invalid state {state!r}. Choose: {VALID_STATES}")
    if priority is not None and priority not in VALID_PRIORITIES:
        raise ValueError(f"Invalid priority {priority!r}. Choose: {VALID_PRIORITIES}")


_TASK_FIELDS = ("state", "priority", "due", "completed")

# Attachments: binary documents get a cached "<file>.txt" sidecar with extracted text (searchable);
# text-like files are searched directly.
SIDECAR_EXTS = {".pdf", ".docx", ".html", ".htm"}
TEXT_EXTS = {".txt", ".md", ".csv", ".tsv", ".json", ".yaml", ".yml", ".log", ".xml", ".toml", ".ini",
             ".py", ".js", ".ts", ".sh", ".sql", ".rst"}


def is_sidecar(p: Path) -> bool:
    return p.suffix == ".txt" and Path(p.stem).suffix.lower() in SIDECAR_EXTS
FLAT_MSG = ("tasks are flat: put a task at the vault root or in a category (top-level folder). "
            "Organise bigger work in a forest page that links [[tasks:...]] or lists them with a ```tasks block.")


# ── Vault ─────────────────────────────────────────────────────────────────────


class Vault:
    """One page tree on disk. All reads/writes for that tree go through here."""

    def __init__(self, name: str, root: Path):
        self.name = name
        self.root = root.expanduser()
        self.lock = threading.RLock()

    @property
    def is_tasks(self) -> bool:
        """Task vault (state/priority/due, flat categories) vs knowledge vault (no task fields)."""
        return self.name in TASK_VAULTS

    def _check_task_fields(self, meta: dict) -> None:
        if self.is_tasks:
            return
        used = [k for k in _TASK_FIELDS if meta.get(k) not in (None, "")]
        if used:
            tv = TASK_VAULTS[0] if TASK_VAULTS else "tasks"
            raise ValueError(f"'{self.name}' is a knowledge vault: {', '.join(used)} only exist on tasks. "
                             f"Create the task in the '{tv}' vault and link it here with [[{tv}:category/task]].")

    def _check_flat(self, parent_path: Optional[str], as_folder: bool = False) -> None:
        """Task vaults: pages live at the root or directly in a category; categories only at the root."""
        if not self.is_tasks:
            return
        if parent_path is None:
            return
        parent = self.get_page(parent_path)
        if as_folder or not parent.is_folder or parent.path.count("/") != 1:
            raise ValueError(FLAT_MSG)

    # ── paths ──

    def safe_resolve(self, rel_path: str) -> Path:
        """Join with root, reject traversal outside root and access to .git."""
        rel_path = (rel_path or "").strip().lstrip("/")
        if any(p == ".git" for p in Path(rel_path).parts):
            raise ValueError(f"Access to .git is not allowed: {rel_path}")
        resolved = (self.root / rel_path).resolve()
        if not resolved.is_relative_to(self.root.resolve()):
            raise ValueError(f"Path traversal detected: {rel_path}")
        return resolved

    def rel(self, p: Path) -> str:
        return p.resolve().relative_to(self.root.resolve()).as_posix()

    def iter_md(self, include_hidden: bool = False) -> Iterator[Path]:
        if not self.root.exists():
            return
        for md in sorted(self.root.rglob("*.md")):
            parts = md.relative_to(self.root).parts
            if not include_hidden and any(p.startswith(".") for p in parts):
                continue
            if parts[0] == "_assets":   # attachments (even .md files) are not pages
                continue
            yield md

    def iter_searchable(self) -> Iterator[Tuple[Path, str]]:
        """(file, display_path) for pages plus attachment text: extracted-text sidecars of
        pdf/docx/html (reported under the original file's path) and text-like attachments."""
        for md in self.iter_md():
            yield md, self.rel(md)
        assets = self.root / "_assets"
        if assets.is_dir():
            for t in sorted(assets.rglob("*")):
                if not t.is_file():
                    continue
                if is_sidecar(t):
                    yield t, self.rel(t)[:-4]
                elif t.suffix.lower() in TEXT_EXTS:
                    yield t, self.rel(t)

    # ── write plumbing ──

    def is_ai_readonly(self, path: str) -> bool:
        """True if the page or any ancestor folder-page has frontmatter `ai: readonly`."""
        try:
            abs_path = self.safe_resolve(path)
        except ValueError:
            return False
        root = self.root.resolve()
        candidates = [abs_path] if abs_path.suffix == ".md" else [abs_path / "index.md"]
        d = abs_path.parent
        while d.is_relative_to(root) and d != root:
            candidates.append(d / "index.md")
            d = d.parent
        for c in candidates:
            if c.is_file():
                try:
                    if str(frontmatter.load(str(c)).metadata.get("ai", "")).lower() == "readonly":
                        return True
                except Exception:
                    pass
        return False

    def _guard(self, *paths: Optional[str]) -> None:
        """Block token (agent/API) writes to `ai: readonly` pages. Web sessions may still edit."""
        from . import auth
        p = auth.principal.get()
        if p.kind != "token":
            return
        for path in paths:
            if path and self.is_ai_readonly(path):
                auth.audit("ai.readonly_blocked", vault=self.name, path=path)
                raise ReadOnlyPageError(f"{self.name}:{path} is marked `ai: readonly`; ask the user to change it")

    def _mutate(self, message: str, fn: Callable):
        with self.lock:
            result = fn()
            if AUTOCOMMIT:
                try:
                    gitrepo.commit(self.root, message, author=actor.get())
                except gitrepo.GitError:
                    pass  # never lose a write because git hiccupped
            return result

    def _check_sha(self, md_path: Path, expected_sha: Optional[str]) -> None:
        if expected_sha and md_path.exists():
            current = _sha(md_path.read_bytes())
            if current != expected_sha:
                raise ConflictError(
                    f"Page changed since you read it (expected sha {expected_sha}, now {current}). "
                    "Re-read and retry."
                )

    def _ensure_ancestors(self, dir_path: Path) -> None:
        """Make sure every ancestor directory is a folder-page (has index.md), so pages are visible."""
        root = self.root.resolve()
        d = dir_path.resolve()
        chain = []
        while d != root and d.is_relative_to(root):
            chain.append(d)
            d = d.parent
        for d in reversed(chain):
            d.mkdir(parents=True, exist_ok=True)
            idx = d / "index.md"
            if not idx.exists():
                self._write_page(idx, Page(vault=self.name, path=self.rel(idx),
                                           name=d.name.replace("-", " ").title(),
                                           is_folder=True, created=date.today()))

    # ── parse / write ──

    def _parent_path_of(self, md_path: Path, is_folder: bool) -> Optional[str]:
        parent_dir = md_path.parent.parent if is_folder else md_path.parent
        if parent_dir.resolve() == self.root.resolve():
            return None
        parent_index = parent_dir / "index.md"
        if parent_index.exists():
            return self.rel(parent_index)
        return None

    def _children_of(self, dir_path: Path) -> List[str]:
        children = []
        if not dir_path.is_dir():
            return children
        for item in sorted(dir_path.iterdir()):
            if item.name.startswith(".") or item.name == "index.md":
                continue
            if item.is_file() and item.suffix == ".md":
                children.append(self.rel(item))
            elif item.is_dir() and (item / "index.md").exists():
                children.append(self.rel(item / "index.md"))
        return children

    def _parse_page(self, path: Path) -> Page:
        root = self.root.resolve()
        path = path.resolve()
        if path.is_dir():
            md_path = path / "index.md"
            is_folder = True
        elif path.name == "index.md":
            md_path = path
            is_folder = path.parent != root
        else:
            md_path = path
            is_folder = False

        if not md_path.exists():
            raise FileNotFoundError(f"Page not found: {self.name}:{self.rel(path)}")

        raw = md_path.read_bytes()
        post = frontmatter.loads(raw.decode("utf-8", errors="replace"))
        canonical = self.rel(md_path)
        meta = dict(post.metadata)

        if is_folder:
            default_name = md_path.parent.name.replace("-", " ").title()
        else:
            default_name = md_path.stem.replace("-", " ").title()

        content = post.content or ""
        return Page(
            vault=self.name,
            path=canonical,
            short_id=hashlib.sha1(canonical.encode()).hexdigest()[:6],
            name=str(meta.get("name") or default_name),
            parent_path=self._parent_path_of(md_path, is_folder),
            is_folder=is_folder,
            children=self._children_of(md_path.parent) if is_folder else [],
            state=meta.get("state"),
            priority=meta.get("priority"),
            due=_parse_date(meta.get("due")),
            created=_parse_date(meta.get("created")) or date.today(),
            completed=_parse_date(meta.get("completed")),
            tags=_norm_tags(meta.get("tags")),
            inline_tags=inline_tags(content),
            extra={k: v for k, v in meta.items() if k not in _KNOWN_META},
            content=content,
            sha=_sha(raw),
        )

    def _write_page(self, md_path: Path, page: Page) -> None:
        """Write a Page back to disk with canonical frontmatter (unknown keys preserved)."""
        if md_path.is_dir():
            md_path = md_path / "index.md"
        md_path.parent.mkdir(parents=True, exist_ok=True)

        meta: dict = {}
        if page.state is not None:
            meta["state"] = page.state
        if page.priority is not None:
            meta["priority"] = page.priority
        if page.due is not None:
            meta["due"] = page.due
        meta["created"] = page.created
        if page.completed is not None:
            meta["completed"] = page.completed
        meta["name"] = page.name
        if page.tags:
            meta["tags"] = list(page.tags)
        for k, v in (page.extra or {}).items():
            if k not in _KNOWN_META:
                meta[k] = v

        yaml_str = _yaml.safe_dump(meta, default_flow_style=False, sort_keys=False,
                                   allow_unicode=True).strip()
        body = page.content or ""
        if body and not body.endswith("\n"):
            body += "\n"
        md_path.write_text(f"---\n{yaml_str}\n---\n{body}", encoding="utf-8")

    # ── tree / listing ──

    def _build_tree_children(self, dir_path: Path) -> List[TreeNode]:
        nodes = []
        if not dir_path.is_dir():
            return nodes
        for item in sorted(dir_path.iterdir()):
            if item.name.startswith("."):
                continue
            try:
                if item.is_file() and item.suffix == ".md" and item.name != "index.md":
                    p = self._parse_page(item)
                    nodes.append(TreeNode(path=p.path, name=p.name, state=p.state))
                elif item.is_dir() and (item / "index.md").exists():
                    p = self._parse_page(item / "index.md")
                    nodes.append(TreeNode(path=p.path, name=p.name, is_folder=True, state=p.state,
                                          children=self._build_tree_children(item)))
            except Exception:
                pass
        return nodes

    def get_tree(self) -> List[TreeNode]:
        if not self.root.exists():
            return []
        return self._build_tree_children(self.root)

    def list_children(self, parent_path: Optional[str] = None) -> List[Page]:
        if parent_path is None:
            dir_path = self.root
        else:
            abs_path = self.safe_resolve(parent_path)
            dir_path = abs_path.parent if abs_path.name == "index.md" else abs_path
        pages = []
        if not dir_path.exists():
            return pages
        for item in sorted(dir_path.iterdir()):
            if item.name.startswith("."):
                continue
            try:
                if item.is_file() and item.suffix == ".md":
                    if parent_path is None and item.name == "index.md":
                        continue
                    if parent_path is not None and item.name == "index.md":
                        continue
                    pages.append(self._parse_page(item))
                elif item.is_dir() and (item / "index.md").exists():
                    pages.append(self._parse_page(item / "index.md"))
            except Exception:
                pass
        return pages

    def all_pages(self) -> List[Page]:
        pages = []
        for md in self.iter_md():
            try:
                pages.append(self._parse_page(md))
            except Exception:
                pass
        return pages

    # ── read ──

    def get_page(self, path: str) -> Page:
        abs_path = self.safe_resolve(path)
        if not abs_path.exists():
            if not abs_path.suffix and abs_path.with_suffix(".md").exists():
                abs_path = abs_path.with_suffix(".md")
            elif (abs_path.parent / abs_path.stem / "index.md").exists():
                abs_path = abs_path.parent / abs_path.stem / "index.md"
            else:
                # short id (6 hex) or a unique page name/slug
                try:
                    if _re.fullmatch(r"[0-9a-f]{6}", path):
                        abs_path = self.safe_resolve(self.resolve_short_id(path))
                    else:
                        abs_path = self.safe_resolve(self.resolve_ref(path))
                except FileNotFoundError:
                    raise FileNotFoundError(f"Page not found: {self.name}:{path}")
        if abs_path.is_dir():
            abs_path = abs_path / "index.md"
        return self._parse_page(abs_path)

    def read_raw(self, path: str) -> Tuple[str, str]:
        """Raw file text (frontmatter + body) and its sha."""
        p = self.get_page(path)
        data = self.safe_resolve(p.path).read_bytes()
        return data.decode("utf-8", errors="replace"), _sha(data)

    # ── create / update ──

    def create_page(
        self,
        parent_path: Optional[str],
        name: str,
        *,
        content: str = "",
        state: Optional[str] = None,
        priority: Optional[str] = None,
        due: Optional[date] = None,
        tags: Optional[List[str]] = None,
        as_folder: bool = False,
        template: Optional[str] = None,
        extra: Optional[dict] = None,
    ) -> Page:
        """Create a new page. as_folder=True creates a directory-page (dir/index.md).
        If the parent is a leaf page it is promoted to a folder first.
        template: name of a page in the templates folder; its frontmatter gives defaults, its body the content."""
        if template:
            from .features import apply_template
            t_meta, t_body = apply_template(template, name)
            content = content or t_body
            state = state or t_meta.pop("state", None)
            priority = priority or t_meta.pop("priority", None)
            tags = tags or t_meta.pop("tags", None)
            if due is None and t_meta.get("due"):
                due = _parse_date(t_meta.pop("due"))
            t_meta.pop("due", None)
            extra = {**{k: v for k, v in t_meta.items() if k not in _KNOWN_META}, **(extra or {})}
        _validate_meta(state, priority)
        self._check_task_fields({"state": state, "priority": priority, "due": due})
        self._check_flat(parent_path, as_folder)
        self._guard(parent_path)
        slug = slugify(name)

        def op():
            nonlocal parent_path
            if parent_path is None:
                parent_dir = self.root
            else:
                abs_parent = self.safe_resolve(parent_path)
                if not abs_parent.exists():
                    abs_parent = self.safe_resolve(self.get_page(parent_path).path)
                if abs_parent.name == "index.md":
                    parent_dir = abs_parent.parent
                elif abs_parent.is_dir():
                    parent_dir = abs_parent
                else:
                    parent_dir = self.safe_resolve(self._promote(self.rel(abs_parent)).path).parent
            parent_dir.mkdir(parents=True, exist_ok=True)

            if as_folder:
                new_dir = parent_dir / slug
                i = 2
                while new_dir.exists() or new_dir.with_suffix(".md").exists():
                    new_dir = parent_dir / f"{slug}-{i}"
                    i += 1
                new_dir.mkdir(parents=True)
                file_path = new_dir / "index.md"
            else:
                file_path = parent_dir / f"{slug}.md"
                i = 2
                while file_path.exists() or (parent_dir / file_path.stem).exists():
                    file_path = parent_dir / f"{slug}-{i}.md"
                    i += 1

            page = Page(vault=self.name, path=self.rel(file_path), name=name, is_folder=as_folder,
                        state=state, priority=priority, due=due, tags=_norm_tags(tags),
                        created=date.today(), content=content, extra=extra or {})
            self._write_page(file_path, page)
            return self._parse_page(file_path)

        page = self._mutate(f"create {name}", op)
        return page

    def update_page(self, path: str, expected_sha: Optional[str] = None, **fields) -> Page:
        """Update fields (name, content, state, priority, due, tags, extra) and write back."""
        _validate_meta(fields.get("state"), fields.get("priority"))
        self._check_task_fields(fields)

        def op():
            page = self.get_page(path)
            self._guard(page.path)
            abs_path = self.safe_resolve(page.path)
            self._check_sha(abs_path, expected_sha)
            for key, value in fields.items():
                if key == "tags":
                    value = _norm_tags(value)
                if key == "extra":
                    merged = dict(page.extra)
                    for k, v in (value or {}).items():
                        if v is None:
                            merged.pop(k, None)
                        else:
                            merged[k] = v
                    value = merged
                setattr(page, key, value)
            # Auto-stamp completion when marked done; clear when un-done
            if "state" in fields:
                if fields["state"] == "done" and page.completed is None:
                    page.completed = date.today()
                elif fields["state"] != "done":
                    page.completed = None
            self._write_page(abs_path, page)
            return self._parse_page(abs_path)

        return self._mutate(f"update {path} ({', '.join(fields) or 'touch'})", op)

    def write_raw(self, path: str, text: str, expected_sha: Optional[str] = None) -> Page:
        """Write a whole file (frontmatter + body). Creates it (and parent folder-pages) if missing."""
        if not path.endswith(".md"):
            raise ValueError("Page paths must end in .md")
        try:
            post = frontmatter.loads(text)
        except Exception as e:
            raise ValueError(f"Invalid frontmatter: {e}")
        _validate_meta(post.metadata.get("state"), post.metadata.get("priority"))
        self._check_task_fields(post.metadata)
        if self.is_tasks and path.strip("/").count("/") > 1:   # root task, category/task.md or category/index.md
            raise ValueError(FLAT_MSG)

        def op():
            abs_path = self.safe_resolve(path)
            self._guard(path)
            self._check_sha(abs_path, expected_sha)
            if abs_path.parent.resolve() != self.root.resolve():
                ancestor = abs_path.parent.parent if abs_path.name == "index.md" else abs_path.parent
                if ancestor.resolve() != self.root.resolve():
                    self._ensure_ancestors(ancestor)
            abs_path.parent.mkdir(parents=True, exist_ok=True)
            final = text
            if not post.metadata.get("created"):
                # stamp created so the page behaves like UI-created ones
                post.metadata["created"] = date.today()
                final = frontmatter.dumps(post)
                if not final.endswith("\n") and text.endswith("\n"):
                    final += "\n"
            abs_path.write_text(final, encoding="utf-8")
            return self._parse_page(abs_path)

        return self._mutate(f"write {path}", op)

    def edit_raw(self, path: str, old: str, new: str, replace_all: bool = False,
                 expected_sha: Optional[str] = None) -> Page:
        """Exact string replacement in the raw file, like a code editor's str_replace."""
        text, sha = self.read_raw(path)
        if expected_sha and expected_sha != sha:
            raise ConflictError(f"Page changed since you read it (expected sha {expected_sha}, now {sha}).")
        count = text.count(old)
        if count == 0:
            raise ValueError("old text not found in page")
        if count > 1 and not replace_all:
            raise ValueError(f"old text occurs {count} times - add more context or set replace_all")
        new_text = text.replace(old, new) if replace_all else text.replace(old, new, 1)
        real = self.get_page(path).path
        return self.write_raw(real, new_text, expected_sha=sha)

    def append_to_page(self, path: str, text: str, timestamp: bool = True) -> Page:
        """Append a (timestamped) note to the page body."""
        def op():
            page = self.get_page(path)
            self._guard(page.path)
            abs_path = self.safe_resolve(page.path)
            if timestamp:
                ts = datetime.now().strftime("%Y-%m-%d %H:%M")
                page.content = (page.content or "").rstrip() + f"\n\n**{ts}**: {text}"
            else:
                page.content = (page.content or "").rstrip() + f"\n\n{text}"
            self._write_page(abs_path, page)
            return self._parse_page(abs_path)

        return self._mutate(f"append {path}", op)

    # ── structure ──

    def _page_fs_node(self, page: Page) -> Path:
        """The filesystem node that *is* the page: the directory for folder-pages, else the file."""
        abs_path = self.safe_resolve(page.path)
        if page.is_folder:
            return abs_path.parent
        return abs_path

    def move_page(self, path: str, new_parent_path: Optional[str]) -> Page:
        """Move a page (and its children) under a new parent (None = root)."""
        def op():
            page = self.get_page(path)
            self._guard(page.path, new_parent_path)
            if page.is_folder and self.is_tasks:
                raise ValueError("categories can't be moved into other folders (tasks are flat)")
            self._check_flat(new_parent_path)
            src = self._page_fs_node(page)
            if new_parent_path is None:
                dest_dir = self.root
            else:
                parent = self.get_page(new_parent_path)
                # validate before any side effect (promoting the parent)
                if self.safe_resolve(parent.path).is_relative_to(src.resolve()):
                    raise ValueError("Cannot move a page into itself")
                if not parent.is_folder:
                    parent = self._promote(parent.path)
                dest_dir = self.safe_resolve(parent.path).parent
            if dest_dir.resolve().is_relative_to(src.resolve()):
                raise ValueError("Cannot move a page into itself")
            dest = dest_dir / src.name
            if dest.resolve() == src.resolve():
                return page  # already there
            if dest.exists():
                raise FileExistsError(f"A page already exists at destination: {self.rel(dest)}")
            shutil.move(str(src), str(dest))
            return self._parse_page(dest / "index.md" if dest.is_dir() else dest)

        return self._mutate(f"move {path} -> {new_parent_path or '/'}", op)

    def _promote(self, path: str) -> Page:
        abs_path = self.safe_resolve(path)
        if abs_path.name == "index.md":
            raise ValueError(f"Page is already a folder: {path}")
        if not abs_path.exists():
            raise FileNotFoundError(f"Page not found: {self.name}:{path}")
        new_dir = abs_path.parent / abs_path.stem
        if new_dir.exists():
            raise FileExistsError(f"Directory already exists: {self.rel(new_dir)}")
        new_dir.mkdir()
        new_index = new_dir / "index.md"
        shutil.move(str(abs_path), str(new_index))
        return self._parse_page(new_index)

    def promote_to_folder(self, path: str) -> Page:
        """Convert a leaf page to a folder-page (moves content into dir/index.md)."""
        path = self.get_page(path).path
        if self.is_tasks:
            raise ValueError(FLAT_MSG)
        self._guard(path)
        return self._mutate(f"promote {path}", lambda: self._promote(path))

    def delete_page(self, path: str) -> None:
        """Soft-delete a page (folder-pages take their children) into .shadow/, preserving structure."""
        def op():
            page = self.get_page(path)
            self._guard(page.path)
            node = self._page_fs_node(page)
            shadow = self.root / ".shadow"
            dest = shadow / self.rel(node)
            if dest.exists():
                dest = _unique_path(dest)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(node), str(dest))

        self._mutate(f"delete {path}", op)

    def list_shadow(self) -> List[dict]:
        """Top-level deleted items in .shadow (each restorable)."""
        shadow = self.root / ".shadow"
        if not shadow.exists():
            return []
        results = []

        def walk(d: Path):
            for item in sorted(d.iterdir()):
                if item.name.startswith("."):
                    continue
                rel = item.relative_to(shadow).as_posix()
                if rel.startswith("_assets/") and item.is_file():
                    if not is_sidecar(item):   # deleted attachment (its text sidecar travels with it)
                        results.append({"shadow_path": rel, "path": rel, "name": "attachment: " + item.name,
                                        "is_folder": False, "kind": "attachment"})
                elif item.is_dir() and (item / "index.md").exists():
                    results.append(_shadow_entry(item, item / "index.md"))
                elif item.is_dir():
                    walk(item)  # plain directory mirroring the original location
                elif item.suffix == ".md":
                    results.append(_shadow_entry(item, item))

        def _shadow_entry(item: Path, md: Path) -> dict:
            rel = item.relative_to(shadow).as_posix()
            try:
                name = str(frontmatter.load(str(md)).metadata.get("name") or item.stem)
            except Exception:
                name = item.stem.replace("-", " ").title()
            return {"shadow_path": rel, "path": rel, "name": name, "is_folder": item.is_dir(), "kind": "page"}

        walk(shadow)
        return results

    def restore_from_shadow(self, shadow_path: str) -> Page:
        """Restore a page from .shadow back to its original location (renamed if taken)."""
        def op():
            src = self.safe_resolve(f".shadow/{shadow_path}")
            if not src.exists():
                raise FileNotFoundError(f"Shadow path not found: {shadow_path}")
            dest = self.root / shadow_path
            if dest.exists():
                stem = dest.stem if dest.suffix else dest.name
                dest = dest.parent / f"{stem}-restored-{datetime.now().strftime('%H%M%S')}{dest.suffix}"
            if dest.parent.resolve() != self.root.resolve():
                self._ensure_ancestors(dest.parent)
            shutil.move(str(src), str(dest))
            return self._parse_page(dest / "index.md" if dest.is_dir() else dest)

        return self._mutate(f"restore {shadow_path}", op)

    # ── git history ──

    def history(self, path: Optional[str] = None, limit: int = 30, since: Optional[str] = None) -> List[dict]:
        if path:
            self.safe_resolve(path)
            try:
                path = self.get_page(path).path
            except FileNotFoundError:
                pass  # deleted page - history still exists under the given path
        return gitrepo.log(self.root, path, limit=limit, since=since)

    def _canonical(self, path: str) -> str:
        """Canonical path of an existing page, or the given path (e.g. for deleted pages)."""
        self.safe_resolve(path)
        try:
            return self.get_page(path).path
        except FileNotFoundError:
            return path

    def show_version(self, path: str, rev: str) -> str:
        return gitrepo.show_file(self.root, rev, self._canonical(path))

    def diff(self, rev: str, path: Optional[str] = None) -> str:
        if path:
            self.safe_resolve(path)
        return gitrepo.diff(self.root, rev, path)

    def restore_version(self, path: str, rev: str) -> Page:
        path = self._canonical(path)
        return self.write_raw(path, self.show_version(path, rev))

    # ── resolution ──

    def resolve_ref(self, ref: str, current_path: Optional[str] = None) -> str:
        """Resolve a link target inside this vault to a canonical page path.
        ./rel and ../rel are relative to current_path; otherwise root-relative;
        falls back to a unique name/slug match anywhere in the vault."""
        ref = ref.strip().strip("/")
        root = self.root.resolve()
        if (ref.startswith("./") or ref.startswith("../")) and current_path:
            cur = (root / current_path).resolve()
            cur_dir = cur.parent.parent if cur.name == "index.md" else cur.parent
            base = (cur_dir / ref).resolve()
        else:
            base = (root / ref).resolve()
        for cand in [base, Path(str(base) + ".md"), base / "index.md"]:
            if cand.is_file() and cand.is_relative_to(root) and cand.suffix == ".md":
                return self.rel(cand)
        # name / slug fallback
        want = ref.rsplit("/", 1)[-1].lower()
        want_slug = slugify(want)
        hits = []
        for md in self.iter_md():
            stem = md.parent.name if md.name == "index.md" else md.stem
            if stem.lower() == want_slug or stem.lower() == want:
                hits.append(md)
        if len(hits) == 1:
            return self.rel(hits[0])
        if not hits:
            for md in self.iter_md():
                try:
                    if str(frontmatter.load(str(md)).metadata.get("name", "")).lower() == want:
                        hits.append(md)
                except Exception:
                    pass
            if len(hits) == 1:
                return self.rel(hits[0])
        raise FileNotFoundError(f"Page reference not found: {ref!r}")

    def resolve_short_id(self, ref: str) -> str:
        """6-char short_id → canonical path."""
        for md in self.iter_md():
            rel = self.rel(md)
            if hashlib.sha1(rel.encode()).hexdigest()[:6] == ref:
                return rel
        raise FileNotFoundError(f"No page found for: {ref!r}")


def _unique_path(path: Path) -> Path:
    stem = path.stem if path.suffix else path.name
    i = 1
    while True:
        new = path.parent / f"{stem}-{i}{path.suffix}"
        if not new.exists():
            return new
        i += 1


# ── Registry & cross-vault operations ────────────────────────────────────────

VAULTS: Dict[str, Vault] = {}
DEFAULT_VAULT = "forest"


def init_vaults(git: bool = True) -> Dict[str, Vault]:
    global DEFAULT_VAULT
    VAULTS.clear()
    for name, root in vault_paths().items():
        v = Vault(name, root)
        v.root.mkdir(parents=True, exist_ok=True)
        if git:
            try:
                gitrepo.ensure_repo(v.root)
            except (gitrepo.GitError, FileNotFoundError):
                pass
        VAULTS[name] = v
    if VAULTS and DEFAULT_VAULT not in VAULTS:
        DEFAULT_VAULT = next(iter(VAULTS))
    return VAULTS


def vault(name: Optional[str] = None) -> Vault:
    if not VAULTS:
        init_vaults()
    name = name or DEFAULT_VAULT
    if name not in VAULTS:
        raise KeyError(f"Unknown vault {name!r}. Vaults: {list(VAULTS)}")
    return VAULTS[name]


def split_ref(ref: str, default_vault: Optional[str] = None) -> Tuple[str, str]:
    """'tasks:work/x.md' → ('tasks', 'work/x.md'); 'work/x.md' → (default, 'work/x.md')."""
    if not VAULTS:
        init_vaults()
    if ":" in ref:
        v, rest = ref.split(":", 1)
        if v in VAULTS:
            return v, rest.lstrip("/")
    return default_vault or DEFAULT_VAULT, ref


def resolve_link(ref: str, from_vault: str, from_path: Optional[str] = None) -> Tuple[str, str]:
    """Resolve a [[link]] written in (from_vault, from_path) → (vault, canonical path)."""
    v, r = split_ref(ref, from_vault)
    return v, vault(v).resolve_ref(r, from_path if v == from_vault else None)


def outgoing_links(vname: str, path: str) -> List[dict]:
    page = vault(vname).get_page(path)
    out = []
    for ref in link_refs(page.content):
        try:
            v, p = resolve_link(ref, vname, page.path)
            out.append({"ref": ref, "vault": v, "path": p, "exists": True})
        except (FileNotFoundError, KeyError, ValueError):
            out.append({"ref": ref, "vault": None, "path": None, "exists": False})
    return out


def get_backlinks(vname: str, path: str) -> List[str]:
    """All pages (any vault) whose [[links]] resolve to vname:path. Returns 'vault:path' refs."""
    target = (vname, vault(vname).get_page(path).path)
    results = []
    for v in VAULTS.values():
        for md in v.iter_md():
            try:
                text = md.read_text(encoding="utf-8")
            except Exception:
                continue
            refs = link_refs(text)
            if not refs:
                continue
            rel = v.rel(md)
            if (v.name, rel) == target:
                continue
            for ref in refs:
                try:
                    if resolve_link(ref, v.name, rel) == target:
                        results.append(f"{v.name}:{rel}")
                        break
                except Exception:
                    pass
    return results


def _selected(vaults: Optional[str | List[str]]) -> List[Vault]:
    if not VAULTS:
        init_vaults()
    if not vaults or vaults == "all":
        return list(VAULTS.values())
    names = vaults if isinstance(vaults, list) else [n.strip() for n in vaults.split(",")]
    return [vault(n) for n in names]


def search_pages(query: str, vaults: Optional[str] = None, limit: int = 100) -> List[dict]:
    """Case-insensitive full-text search; every whitespace-separated term must match.
    Name matches rank first. Returns [{vault, path, name, snippet}]."""
    terms = [t for t in (query or "").lower().split() if t]
    if not terms:
        return []
    hits = []
    for v in _selected(vaults):
        for md, rel in v.iter_searchable():
            try:
                text = md.read_text(encoding="utf-8")
            except Exception:
                continue
            low = text.lower()
            if not all(t in low for t in terms):
                continue
            body_start = low.find("\n---", 3) + 4 if low.startswith("---") else 0
            idx = low.find(terms[0], body_start)
            if idx < 0:
                idx = low.index(terms[0])
            snippet = text[max(body_start, idx - 60): idx + len(terms[0]) + 60].replace("\n", " ").strip()
            if md.suffix != ".md":
                name = "attachment: " + Path(rel).name
            else:
                try:
                    name = v._parse_page(md).name
                except Exception:
                    name = md.stem.replace("-", " ").title()
            score = sum(3 for t in terms if t in name.lower())
            hits.append((score, {"vault": v.name, "path": rel, "name": name,
                                 "snippet": f"...{snippet}..."}))
    hits.sort(key=lambda h: -h[0])
    return [h for _, h in hits[:limit]]


def grep(pattern: str, vaults: Optional[str] = None, *, regex: bool = True, ignore_case: bool = True,
         path_glob: Optional[str] = None, context: int = 0, max_results: int = 200) -> List[dict]:
    """Line-oriented search over raw files (frontmatter included), like grep -rn."""
    if len(pattern) > 300:
        raise ValueError("pattern too long (max 300 characters)")
    flags = _re.IGNORECASE if ignore_case else 0
    rx = _re.compile(pattern if regex else _re.escape(pattern), flags)
    out = []
    for v in _selected(vaults):
        for md, rel in v.iter_searchable():
            if path_glob and not fnmatch.fnmatch(rel, path_glob):
                continue
            try:
                lines = md.read_text(encoding="utf-8").splitlines()
            except Exception:
                continue
            for i, line in enumerate(lines):
                if rx.search(line):
                    hit = {"vault": v.name, "path": rel, "line": i + 1, "text": line}
                    if context:
                        hit["before"] = lines[max(0, i - context): i]
                        hit["after"] = lines[i + 1: i + 1 + context]
                    out.append(hit)
                    if len(out) >= max_results:
                        return out
    return out


def glob_pages(pattern: str, vaults: Optional[str] = None) -> List[str]:
    out = []
    for v in _selected(vaults):
        for md in v.iter_md():
            rel = v.rel(md)
            if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(md.name, pattern):
                out.append(f"{v.name}:{rel}")
    return out


def all_tags(vaults: Optional[str] = None) -> List[dict]:
    counts: Dict[str, int] = {}
    for v in _selected(vaults):
        for p in v.all_pages():
            for t in set(p.tags) | set(p.inline_tags):
                counts[t] = counts.get(t, 0) + 1
    return [{"tag": t, "count": c} for t, c in sorted(counts.items(), key=lambda x: (-x[1], x[0]))]


def pages_with_tag(tag: str, vaults: Optional[str] = None) -> List[dict]:
    tag = tag.lstrip("#").lower()
    out = []
    for v in _selected(vaults):
        for p in v.all_pages():
            tags = [t.lower() for t in p.tags + p.inline_tags]
            # hierarchical: #project matches #project/alpha
            if any(t == tag or t.startswith(tag + "/") for t in tags):
                out.append(_summary(p))
    return out


_PRI_RANK = {"high": 0, "medium": 1, "low": 2, None: 3}


def _summary(p: Page) -> dict:
    return {"vault": p.vault, "path": p.path, "name": p.name, "state": p.state,
            "priority": p.priority, "due": p.due.isoformat() if p.due else None,
            "tags": sorted(set(p.tags) | set(p.inline_tags)), "short_id": p.short_id,
            "is_folder": p.is_folder, "parent_path": p.parent_path,
            "created": p.created.isoformat(),
            "completed": p.completed.isoformat() if p.completed else None}


def task_vaults(vaults: Optional[str] = None) -> Optional[str]:
    """Restrict a vault selection to task vaults (agenda, review, digest, calendar only look at tasks)."""
    names = [v.name for v in _selected(vaults) if v.is_tasks]
    return ",".join(names) if names else "__none__"


def agenda(
    vaults: Optional[str] = None,
    state: str = "undone",
    priority: Optional[str] = None,
    due_within: Optional[str] = None,
    overdue: bool = False,
    tag: Optional[str] = None,
    under: Optional[str] = None,
    by_priority: Optional[dict] = None,
) -> List[dict]:
    """Pages that carry task metadata, filtered and sorted by due date then priority.
    state: all | undone | done | <specific state>.
    by_priority: per-priority due windows, e.g. {"high": "all", "medium": "2w", "low": "none"}."""
    today = date.today()
    cutoff = parse_due_window(due_within) if due_within else None
    out = []
    vaults = task_vaults(vaults)
    for v in [] if vaults == "__none__" else _selected(vaults):
        for p in v.all_pages():
            if p.state is None:
                continue
            if state == "undone" and p.state == "done":
                continue
            if state == "done" and p.state != "done":
                continue
            if state not in ("all", "undone", "done") and p.state != state:
                continue
            if priority and p.priority != priority:
                continue
            if under and not p.path.startswith(under.rstrip("/") + "/") and p.path != under:
                continue
            if tag:
                tl = tag.lstrip("#").lower()
                if not any(t.lower() == tl or t.lower().startswith(tl + "/") for t in p.tags + p.inline_tags):
                    continue
            if overdue and not (p.due and p.due < today and p.state != "done"):
                continue
            if cutoff and not (p.due and p.due <= cutoff):
                continue
            if by_priority:
                window = (by_priority.get(p.priority or "medium") or "all").strip().lower()
                if window == "none":
                    continue
                if window != "all":
                    c = parse_due_window(window)
                    if c is not None and (p.due is None or p.due > c):
                        continue
            out.append(_summary(p))
    out.sort(key=lambda s: (s["due"] or "9999-12-31", _PRI_RANK.get(s["priority"], 3), s["name"].lower()))
    return out


def recent_changes(since: str = "7 days ago", vaults: Optional[str] = None, limit: int = 50) -> List[dict]:
    out = []
    for v in _selected(vaults):
        for e in gitrepo.log(v.root, None, limit=limit, since=since):
            e["vault"] = v.name
            out.append(e)
    out.sort(key=lambda e: e["date"], reverse=True)
    return out[:limit]
