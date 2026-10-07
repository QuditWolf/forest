"""Higher-level features built on the store: templates, daily notes, outlines, unlinked mentions,
link graph, attachments, review bundles, capture/web-clip, calendar feed."""

from __future__ import annotations

import ipaddress
import re
import shutil
import socket
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Optional, Tuple
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import frontmatter

from . import gitrepo, store
from .config import INBOX, JOURNAL, MAX_UPLOAD_MB, PUBLIC_URL, TEMPLATES, TIMEZONE, slugify
from .models import Page

TZ = ZoneInfo(TIMEZONE)


def now() -> datetime:
    return datetime.now(TZ)


def today() -> date:
    return now().date()


def _loc(spec: str) -> Tuple[store.Vault, str]:
    """'forest:journal' → (vault, 'journal')."""
    v, folder = store.split_ref(spec)
    return store.vault(v), folder.strip("/")


def ref_for(vault: str, path: str) -> str:
    """Canonical wikilink target: 'vault:dir/page' (no .md, folder-pages by directory)."""
    p = path[:-3] if path.endswith(".md") else path
    if p.endswith("/index"):
        p = p[:-6]
    return f"{vault}:{p}"


def page_url(vault: str, path: str) -> str:
    return f"{PUBLIC_URL}/#{vault}/{path}" if PUBLIC_URL else f"/#{vault}/{path}"


def _ensure_folder(v: store.Vault, folder: str, name: str) -> str:
    """Make sure folder/index.md exists; return its path."""
    idx = f"{folder}/index.md"
    if not (v.root / idx).exists():
        with v.lock:
            (v.root / folder).mkdir(parents=True, exist_ok=True)
            v._write_page(v.root / idx, Page(vault=v.name, path=idx, name=name, is_folder=True,
                                             created=date.today()))
    return idx


# ── Templates ─────────────────────────────────────────────────────────────────

BUILTIN_TEMPLATES = {
    "daily": """---
name: Daily
tags: [journal]
---
# {{weekday}}, {{date}}

## Plan
- [ ]

## Log

## Notes
""",
    "weekly-review": """---
name: Weekly review
tags: [review]
---
# Week review, {{date}}

## Done

## Slipped / blocked

## Stale (in progress, untouched)

## Next week - priorities
1.

## Notes
""",
    "meeting": """---
name: Meeting
tags: [meeting]
---
# {{title}}

**When:** {{date}} {{time}} · **With:**

## Agenda
-

## Notes

## Decisions

## Action items
- [ ]
""",
    "project": """---
name: Project
tags: [project]
---
# {{title}}

**Goal:**

**Done when:**

## Tasks
Tag tasks with #{{slug}} and they show up here live (you can also link single tasks with `[[tasks:category/task]]`):

```tasks
tag: {{slug}}
```

## Links

## Log
""",
    "task": """---
name: Task
state: todo
priority: medium
---
## Why

## Done when
-

## Notes
""",
}


# Earlier built-in versions: replaced on startup ONLY if the file is still exactly the old default.
OLD_BUILTINS = {
    "project": ['---\nname: Project\nstate: todo\npriority: medium\ntags: [project]\n---\n# {{title}}\n\n**Goal:**\n\n**Done when:**\n\n## Tasks\n- [ ]\n\n## Links\n\n## Log\n'],
}


def ensure_templates() -> None:
    """Seed built-in templates (never overwrites your edits; upgrades untouched old defaults)."""
    try:
        v, folder = _loc(TEMPLATES)
    except KeyError:
        return
    if not (v.root / folder / "index.md").exists():
        _ensure_folder(v, folder, "Templates")
    created = False
    for name, text in BUILTIN_TEMPLATES.items():
        f = v.root / folder / f"{name}.md"
        if not f.exists() or f.read_text(encoding="utf-8") in OLD_BUILTINS.get(name, []):
            f.write_text(text, encoding="utf-8")
            created = True
    if created:
        gitrepo.commit(v.root, "seed/upgrade templates", author="forest") if (v.root / ".git").exists() else None


def list_templates() -> List[dict]:
    try:
        v, folder = _loc(TEMPLATES)
    except KeyError:
        return []
    d = v.root / folder
    out = []
    if d.is_dir():
        for f in sorted(d.glob("*.md")):
            if f.name == "index.md":
                continue
            try:
                name = str(frontmatter.load(str(f)).metadata.get("name") or f.stem)
            except Exception:
                name = f.stem
            out.append({"template": f.stem, "name": name, "ref": f"{v.name}:{folder}/{f.name}"})
    return out


def render(text: str, title: str, d: Optional[datetime] = None) -> str:
    d = d or now()
    vals = {"title": title, "date": d.date().isoformat(), "time": d.strftime("%H:%M"),
            "weekday": d.strftime("%A"), "slug": slugify(title)}
    return re.sub(r"\{\{\s*(title|date|time|weekday|slug)\s*\}\}", lambda m: vals[m.group(1)], text)


def apply_template(template: str, title: str, d: Optional[datetime] = None) -> Tuple[dict, str]:
    """Returns (frontmatter defaults, rendered body) for a template name."""
    v, folder = _loc(TEMPLATES)
    f = v.root / folder / f"{slugify(template)}.md"
    if not f.exists():
        raise FileNotFoundError(f"No template {template!r}. Available: {[t['template'] for t in list_templates()]}")
    post = frontmatter.loads(render(f.read_text(encoding="utf-8"), title, d))
    meta = {k: val for k, val in post.metadata.items() if k not in ("name", "created")}
    return meta, post.content.rstrip() + "\n"


# ── Daily notes ───────────────────────────────────────────────────────────────

def daily_note(day: Optional[date] = None, create: bool = True) -> Optional[Page]:
    day = day or today()
    v, folder = _loc(JOURNAL)
    path = f"{folder}/{day.isoformat()}.md"
    if (v.root / path).exists():
        return v.get_page(path)
    if not create:
        return None
    parent = _ensure_folder(v, folder, "Journal")
    d = datetime.combine(day, now().timetz())
    try:
        meta, body = apply_template("daily", day.isoformat(), d)
    except FileNotFoundError:
        meta, body = {"tags": ["journal"]}, f"# {day.strftime('%A')}, {day.isoformat()}\n"
    return v.create_page(parent, day.isoformat(), content=body, tags=meta.get("tags"))


def journal_days() -> List[str]:
    v, folder = _loc(JOURNAL)
    d = v.root / folder
    return sorted(f.stem for f in d.glob("????-??-??.md")) if d.is_dir() else []


# ── Live task references (for [[tasks:...]] links in knowledge pages) ─────────

def summaries(refs: List[str], from_vault: str = "forest", allowed=None) -> List[dict]:
    """Resolve many [[refs]] at once → current name/state/priority/due (or exists: false)."""
    out = []
    for ref in refs[:200]:
        try:
            v, p = store.resolve_link(ref, from_vault)
            if allowed and not allowed(v):
                raise KeyError(v)
            page = store.vault(v).get_page(p)
            out.append({"ref": ref, "exists": True, **store._summary(page), "is_task": store.vault(v).is_tasks})
        except Exception:
            out.append({"ref": ref, "exists": False})
    return out


# ── Outline / sections ────────────────────────────────────────────────────────

_HEAD_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")


def heading_slug(text: str) -> str:
    return re.sub(r"[^\w\- ]", "", text.lower()).strip().replace(" ", "-")


def outline(content: str) -> List[dict]:
    out, fence = [], False
    for i, line in enumerate(content.splitlines()):
        if line.lstrip().startswith("```"):
            fence = not fence
        if fence:
            continue
        m = _HEAD_RE.match(line)
        if m:
            out.append({"level": len(m.group(1)), "text": m.group(2), "slug": heading_slug(m.group(2)), "line": i + 1})
    return out


def section(content: str, heading: str) -> str:
    """Text of one section (heading line to the next heading of the same or higher level)."""
    lines = content.splitlines()
    heads = outline(content)
    want = heading.lstrip("#").strip().lower()
    for i, h in enumerate(heads):
        if h["text"].lower() == want or h["slug"] == heading_slug(want):
            end = len(lines)
            for h2 in heads[i + 1:]:
                if h2["level"] <= h["level"]:
                    end = h2["line"] - 1
                    break
            return "\n".join(lines[h["line"] - 1:end]).rstrip() + "\n"
    raise ValueError(f"No heading {heading!r}. Headings: {[h['text'] for h in heads]}")


# ── Unlinked mentions ─────────────────────────────────────────────────────────

_MASK_RE = re.compile(r"```.*?```|`[^`\n]*`|\[\[[^\]]*\]\]|\[[^\]]*\]\([^)]*\)", re.S)


def _masked(text: str) -> str:
    """Blank out code, wikilinks and markdown links (same length, so offsets stay valid)."""
    return _MASK_RE.sub(lambda m: " " * len(m.group(0)), text)


def _mention_re(name: str) -> re.Pattern:
    return re.compile(r"(?<![\w/])" + re.escape(name) + r"(?![\w/])", re.IGNORECASE)


def unlinked_mentions(vname: str, path: str, limit: int = 50) -> List[dict]:
    """Pages that mention this page's name in plain text without linking to it."""
    target_page = store.vault(vname).get_page(path)
    name = target_page.name.strip()
    if len(name) < 3:
        return []
    rx = _mention_re(name)
    linked = set(store.get_backlinks(vname, target_page.path))
    out = []
    for v in store.VAULTS.values():
        for md in v.iter_md():
            rel = v.rel(md)
            if (v.name, rel) == (vname, target_page.path) or f"{v.name}:{rel}" in linked:
                continue
            try:
                body = frontmatter.loads(md.read_text(encoding="utf-8")).content
            except Exception:
                continue
            masked = _masked(body)
            hits = list(rx.finditer(masked))
            if not hits:
                continue
            m = hits[0]
            line_start = body.rfind("\n", 0, m.start()) + 1
            line_end = body.find("\n", m.end())
            line = body[line_start: line_end if line_end >= 0 else len(body)]
            out.append({"vault": v.name, "path": rel, "count": len(hits), "text": body[m.start():m.end()],
                        "snippet": line.strip()[:200]})
            if len(out) >= limit:
                return out
    return out


def link_mention(src_vault: str, src_path: str, target_vault: str, target_path: str) -> Page:
    """Turn the first plain-text mention of the target's name in src into [[target|text]]."""
    target = store.vault(target_vault).get_page(target_path)
    v = store.vault(src_vault)
    page = v.get_page(src_path)
    m = _mention_re(target.name.strip()).search(_masked(page.content))
    if not m:
        raise ValueError("No unlinked mention found")
    text = page.content[m.start():m.end()]
    new = page.content[:m.start()] + f"[[{ref_for(target_vault, target.path)}|{text}]]" + page.content[m.end():]
    return v.update_page(page.path, expected_sha=page.sha, content=new)


# ── Link graph ────────────────────────────────────────────────────────────────

def _parent_key(v: store.Vault, md: Path) -> Optional[str]:
    """'vault:path' of the folder-page that contains this page, if any."""
    root = v.root.resolve()
    d = md.resolve().parent
    if md.name == "index.md":
        d = d.parent
    if d == root or not d.is_relative_to(root):
        return None
    idx = d / "index.md"
    return f"{v.name}:{v.rel(idx)}" if idx.exists() else None


def link_index(hierarchy: bool = True) -> Tuple[List[Tuple[str, str, str]], dict]:
    """All edges as (from, to, kind) with 'vault:path' ids. kind: 'child' (parent -> child page)
    or 'link' (a [[wikilink]] from -> to). Plus node info."""
    edges, nodes = [], {}
    for v in store.VAULTS.values():
        for md in v.iter_md():
            rel = v.rel(md)
            key = f"{v.name}:{rel}"
            try:
                post = frontmatter.loads(md.read_text(encoding="utf-8"))
            except Exception:
                continue
            nodes[key] = _node_info(v, md, rel, post.metadata)
            if hierarchy:
                parent = _parent_key(v, md)
                if parent:
                    edges.append((parent, key, "child"))
            for ref in store.link_refs(post.content):
                try:
                    tv, tp = store.resolve_link(ref, v.name, rel)
                    if (tv, tp) != (v.name, rel):
                        edges.append((key, f"{tv}:{tp}", "link"))
                except Exception:
                    pass
    return edges, nodes


def _node_info(v: store.Vault, md: Path, rel: str, meta: dict) -> dict:
    is_folder = md.name == "index.md" and md.parent.resolve() != v.root.resolve()
    return {"id": f"{v.name}:{rel}", "vault": v.name, "path": rel, "is_folder": is_folder,
            "name": str(meta.get("name") or (md.parent.name if md.name == "index.md" else md.stem)),
            "state": meta.get("state")}


def graph(vname: str, path: Optional[str] = None, depth: int = 2, hierarchy: bool = True) -> dict:
    """Graph around a page: `depth` hops over links, backlinks and (if hierarchy) parent/children,
    across vaults. Without path: the whole vault. Edges carry kind 'child' or 'link'."""
    edges, nodes = link_index(hierarchy)
    center = None
    if path is None:
        keep = {k for k in nodes if k.startswith(vname + ":")}
    else:
        center = f"{vname}:{store.vault(vname).get_page(path).path}"
        adj: dict = {}
        for a, b, _ in edges:
            adj.setdefault(a, set()).add(b)
            adj.setdefault(b, set()).add(a)
        keep, frontier = {center}, {center}
        for _ in range(max(1, min(depth, 4))):
            frontier = {n for f in frontier for n in adj.get(f, ())} - keep
            keep |= frontier
    e = sorted({(a, b, k) for a, b, k in edges if a in keep and b in keep})
    return {"center": center, "nodes": [nodes[k] for k in sorted(keep) if k in nodes],
            "edges": [{"from": a, "to": b, "kind": k} for a, b, k in e]}


def neighbors(vname: str, path: str) -> dict:
    """One page plus its direct neighbourhood: parent, children, outgoing links, backlinks.
    Used by the web UI to expand a node in the graph explorer."""
    v = store.vault(vname)
    page = v.get_page(path)
    key = f"{vname}:{page.path}"
    nodes: dict = {}
    edges: list = []

    def add(vn: str, p: str) -> Optional[str]:
        try:
            vv = store.vault(vn)
            md = vv.safe_resolve(p)
            post = frontmatter.loads(md.read_text(encoding="utf-8"))
        except Exception:
            return None
        k = f"{vn}:{p}"
        nodes[k] = _node_info(vv, md, p, post.metadata)
        return k

    add(vname, page.path)
    if page.parent_path:
        if add(vname, page.parent_path):
            edges.append((f"{vname}:{page.parent_path}", key, "child"))
    for c in page.children:
        if add(vname, c):
            edges.append((key, f"{vname}:{c}", "child"))
    for link in store.outgoing_links(vname, page.path):
        if link["exists"] and add(link["vault"], link["path"]):
            edges.append((key, f"{link['vault']}:{link['path']}", "link"))
    for ref in store.get_backlinks(vname, page.path):
        bv, bp = ref.split(":", 1)
        if add(bv, bp):
            edges.append((ref, key, "link"))
    return {"center": key, "nodes": list(nodes.values()),
            "edges": [{"from": a, "to": b, "kind": k} for a, b, k in dict.fromkeys(edges)]}


# ── Attachments ───────────────────────────────────────────────────────────────

MAX_BYTES = MAX_UPLOAD_MB * 1024 * 1024
_FNAME_RE = re.compile(r"[^\w.\-]+")


def asset_folder(page_path: str) -> str:
    stem = page_path[:-3] if page_path.endswith(".md") else page_path
    if stem.endswith("/index"):
        stem = stem[:-6]
    return f"_assets/{stem}"


def save_asset(vname: str, page_path: str, filename: str, data: bytes) -> dict:
    if len(data) > MAX_BYTES:
        raise ValueError(f"File too large ({len(data) // 1024 // 1024} MB > {MAX_UPLOAD_MB} MB)")
    v = store.vault(vname)
    page = v.get_page(page_path.removeprefix(f"{vname}:"))
    v._guard(page.path)
    raw = Path(filename or "file").name
    stem = _FNAME_RE.sub("-", Path(raw).stem).strip("-.") or "file"
    suffix = _FNAME_RE.sub("", Path(raw).suffix.lower())[:12]
    name = stem + suffix
    folder = asset_folder(page.path)

    def op():
        d = v.safe_resolve(folder)
        d.mkdir(parents=True, exist_ok=True)
        target = d / name
        i = 2
        while target.exists():
            target = d / f"{Path(name).stem}-{i}{Path(name).suffix}"
            i += 1
        target.write_bytes(data)
        if target.suffix.lower() in store.SIDECAR_EXTS:
            _sidecar(target)
        return v.rel(target)

    rel = v._mutate(f"attach {name} to {page.path}", op)
    url = f"/api/{v.name}/asset/{rel}"
    is_img = asset_kind(rel) == "image"
    return {"vault": v.name, "path": rel, "url": url, "size": len(data), "kind": asset_kind(rel),
            "markdown": f"![{Path(rel).stem}]({url})" if is_img else f"[{Path(rel).name}]({url})"}


def list_assets(vname: str, page_path: str) -> List[dict]:
    v = store.vault(vname)
    page = v.get_page(page_path)
    d = v.root / asset_folder(page.path)
    if not d.is_dir():
        return []
    return [{"path": v.rel(f), "url": f"/api/{v.name}/asset/{v.rel(f)}", "size": f.stat().st_size,
             "kind": asset_kind(f.name)}
            for f in sorted(d.iterdir()) if f.is_file() and not store.is_sidecar(f)]


def _asset_page(v: store.Vault, path: str) -> Optional[str]:
    """Page an attachment belongs to (_assets/a/b/x.png → a/b.md or a/b/index.md), if it exists."""
    stem = str(Path(path).parent)[len("_assets/"):]
    for cand in (f"{stem}.md", f"{stem}/index.md"):
        if (v.root / cand).is_file():
            return cand
    return None


def delete_asset(vname: str, path: str) -> dict:
    """Soft-delete an attachment into .shadow/ (restorable, like pages). Its PDF text sidecar goes along."""
    v = store.vault(vname)
    f = asset_file(vname, path)
    v._guard(_asset_page(v, path))

    def op():
        dest = v.root / ".shadow" / path
        if dest.exists():
            dest = store._unique_path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        side = f.with_name(f.name + ".txt")
        shutil.move(str(f), str(dest))
        if side.exists():
            shutil.move(str(side), str(dest.with_name(dest.name + ".txt")))
        try:
            f.parent.rmdir()  # remove empty _assets/<page>/ folder
        except OSError:
            pass
        return v.rel(dest)[len(".shadow/"):]

    shadow_path = v._mutate(f"delete attachment {path}", op)
    return {"deleted": f"{vname}:{path}", "shadow_path": shadow_path}


def restore_asset(vname: str, shadow_path: str) -> dict:
    v = store.vault(vname)
    if not shadow_path.startswith("_assets/"):
        raise ValueError("Not an attachment")
    src = v.safe_resolve(f".shadow/{shadow_path}")
    if not src.is_file():
        raise FileNotFoundError(f"Shadow path not found: {shadow_path}")
    v._guard(_asset_page(v, shadow_path))

    def op():
        dest = v.safe_resolve(shadow_path)
        if dest.exists():
            dest = store._unique_path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        side = src.with_name(src.name + ".txt")
        shutil.move(str(src), str(dest))
        if side.exists():
            shutil.move(str(side), str(dest.with_name(dest.name + ".txt")))
        return v.rel(dest)

    rel = v._mutate(f"restore attachment {shadow_path}", op)
    return {"vault": v.name, "path": rel, "url": f"/api/{v.name}/asset/{rel}"}


def asset_file(vname: str, path: str) -> Path:
    v = store.vault(vname)
    if not path.startswith("_assets/"):
        raise ValueError("Attachments live under _assets/")
    f = v.safe_resolve(path)
    if not f.is_file():
        raise FileNotFoundError(f"No attachment {vname}:{path}")
    return f


IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}


def asset_kind(name: str) -> str:
    """image | pdf | document (docx/html) | text | binary"""
    suf = Path(name).suffix.lower()
    if suf in IMAGE_EXTS:
        return "image"
    if suf == ".pdf":
        return "pdf"
    if suf in store.SIDECAR_EXTS:
        return "document"
    if suf in store.TEXT_EXTS:
        return "text"
    return "binary"


def _extract(f: Path) -> str:
    suf = f.suffix.lower()
    if suf == ".pdf":
        from pypdf import PdfReader
        return "\n\n".join((pg.extract_text() or "") for pg in PdfReader(str(f)).pages)
    if suf == ".docx":
        import html as _html
        import zipfile
        with zipfile.ZipFile(f) as z:
            xml = z.read("word/document.xml").decode("utf-8", errors="replace")
        xml = re.sub(r"</w:p>", "\n", xml)
        xml = re.sub(r"<w:tab/>", "\t", xml)
        return _html.unescape(re.sub(r"<[^>]+>", "", xml)).strip()
    if suf in (".html", ".htm"):
        from markdownify import markdownify
        raw = f.read_bytes().decode("utf-8", errors="replace")
        raw = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", "", raw)
        return re.sub(r"\n{3,}", "\n\n", markdownify(raw, heading_style="ATX")).strip()
    return f.read_bytes().decode("utf-8", errors="replace")


def _sidecar(f: Path) -> str:
    """Extracted text of a pdf/docx/html attachment, cached as '<file>.txt' next to it (searchable)."""
    side = f.with_name(f.name + ".txt")
    if side.exists() and side.stat().st_mtime >= f.stat().st_mtime:
        return side.read_text(encoding="utf-8")
    try:
        text = _extract(f)
    except Exception as e:
        text = f"(could not extract text: {e})"
    side.write_text(text, encoding="utf-8")
    return text


def asset_text(vname: str, path: str) -> Optional[str]:
    """Text of an attachment (pdf, docx, html, text formats); None for images/binaries."""
    f = asset_file(vname, path)
    kind = asset_kind(f.name)
    if kind in ("pdf", "document"):
        return _sidecar(f)
    if kind == "text":
        return f.read_bytes().decode("utf-8", errors="replace")
    return None


def pdf_text(vname: str, path: str) -> str:   # kept for callers of the old name
    return asset_text(vname, path) or ""


def _safe_get(url: str, max_bytes: int):
    """GET a public http(s) URL, validating every redirect hop against private/internal addresses."""
    import httpx
    for _ in range(4):
        u = urlparse(url)
        if u.scheme not in ("http", "https") or not u.hostname or not _public_host(u.hostname):
            raise ValueError("Only public http(s) URLs can be fetched")
        with httpx.stream("GET", url, follow_redirects=False, timeout=30,
                          headers={"User-Agent": "Mozilla/5.0 (forest)"}) as r:
            if r.is_redirect:
                url = urljoin(url, r.headers.get("location", ""))
                continue
            r.raise_for_status()
            data = b""
            for chunk in r.iter_bytes():
                data += chunk
                if len(data) > max_bytes:
                    raise ValueError(f"Download larger than {max_bytes // 1024 // 1024} MB")
            return r, data, url
    raise ValueError("Too many redirects")


def attach_from_url(vname: str, page_path: str, url: str, filename: Optional[str] = None) -> dict:
    """Download a public file and attach it to a page."""
    import mimetypes
    r, data, final = _safe_get(url, MAX_BYTES)
    if not filename:
        cd = r.headers.get("content-disposition", "")
        m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', cd)
        filename = m.group(1) if m else (Path(urlparse(final).path).name or "download")
    if not Path(filename).suffix:
        ext = mimetypes.guess_extension((r.headers.get("content-type") or "").split(";")[0].strip()) or ""
        filename += ext
    res = save_asset(vname, page_path, filename, data)
    res["source"] = final
    return res


# ── Review bundle ─────────────────────────────────────────────────────────────

def _last_change(v: store.Vault, path: str) -> Optional[datetime]:
    log = gitrepo.log(v.root, path, limit=1)
    return datetime.fromisoformat(log[0]["date"]) if log else None


def inbox_items() -> List[dict]:
    items = []
    try:
        v, folder = _loc(INBOX)
        d = v.root / folder
        if d.is_dir():
            items += [store._summary(p) for p in v.list_children(f"{folder}/index.md")
                      if (d / "index.md").exists()]
    except KeyError:
        pass
    if "tasks" in store.VAULTS:  # loose root-level tasks = unsorted
        items += [store._summary(p) for p in store.vault("tasks").list_children(None)
                  if not p.is_folder and p.state != "done"]
    return items


def review(period: str = "day", stale_days: int = 7, vaults: Optional[str] = None) -> dict:
    days = 7 if period == "week" else 1
    t = today()
    start = t - timedelta(days=days)
    overdue = store.agenda(vaults, overdue=True)
    over_keys = {(p["vault"], p["path"]) for p in overdue}
    soon = [p for p in store.agenda(vaults, due_within=f"{3 if days == 1 else 7}d")
            if (p["vault"], p["path"]) not in over_keys]
    doing = store.agenda(vaults, state="in-progress")
    stale = []
    for p in doing:
        last = _last_change(store.vault(p["vault"]), p["path"])
        if last and (now() - last).days >= stale_days:
            stale.append({**p, "last_change": last.isoformat(timespec="minutes"), "idle_days": (now() - last).days})
    done = [p for p in store.agenda(vaults, state="done") if p.get("completed") and p["completed"] >= start.isoformat()]
    changes = store.recent_changes(f"{days} days ago", vaults, limit=300)
    by_agents = [c for c in changes if c["author"].startswith("token")]
    by_you = [c for c in changes if not c["author"].startswith("token") and c["author"] != "forest"]
    inbox = inbox_items()
    return {
        "period": period, "today": t.isoformat(), "since": start.isoformat(),
        "overdue": overdue, "due_soon": soon, "in_progress": doing, "stale": stale,
        "done": done, "inbox_count": len(inbox), "inbox": inbox[:20],
        "changes": {"by_you": len(by_you), "by_agents": len(by_agents),
                    "agent_changes": [{"vault": c["vault"], "author": c["author"], "message": c["message"],
                                       "date": c["date"]} for c in by_agents[:30]],
                    "your_changes": [{"vault": c["vault"], "message": c["message"], "date": c["date"]}
                                     for c in by_you[:30]]},
    }


# ── Capture / web clip ────────────────────────────────────────────────────────

def _public_host(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return False
    return True


def fetch_article(url: str) -> Tuple[str, str]:
    """Fetch a public web page and convert the main content to markdown (SSRF-guarded)."""
    import httpx
    from markdownify import markdownify
    from readability import Document

    for _ in range(4):  # follow up to 3 redirects, validating each hop
        u = urlparse(url)
        if u.scheme not in ("http", "https") or not u.hostname or not _public_host(u.hostname):
            raise ValueError("Only public http(s) URLs can be fetched")
        r = httpx.get(url, follow_redirects=False, timeout=15,
                      headers={"User-Agent": "Mozilla/5.0 (forest web clipper)"})
        if r.is_redirect:
            url = urljoin(url, r.headers.get("location", ""))
            continue
        break
    r.raise_for_status()
    if len(r.content) > 5 * 1024 * 1024:
        raise ValueError("Page too large")
    doc = Document(r.text)
    md = markdownify(doc.summary(html_partial=True), heading_style="ATX", strip=["script", "style"])
    md = re.sub(r"\n{3,}", "\n\n", md).strip()
    return doc.short_title() or url, md


def capture(title: Optional[str] = None, text: str = "", url: Optional[str] = None,
            where: Optional[str] = None, fetch: bool = False, tags: Optional[List[str]] = None,
            as_task: bool = False) -> Page:
    """Save a note / web clip into the inbox (or `where` = 'vault:folder').
    as_task: goes to the tasks vault (root = unsorted) with state todo, unless `where` says otherwise."""
    if as_task and not where:
        tv = [n for n, x in store.VAULTS.items() if x.is_tasks]
        where = f"{tv[0]}:" if tv else None
    v, folder = _loc(where or INBOX)
    parent = _ensure_folder(v, folder, folder.rsplit("/", 1)[-1].replace("-", " ").title() or "Inbox") \
        if folder else None
    article_title, article = (None, "")
    if fetch and url:
        article_title, article = fetch_article(url)
    title = (title or article_title or (text.strip().splitlines() or [url or "capture"])[0])[:100].strip()
    parts = []
    if text.strip():
        parts.append("\n".join("> " + l if l.strip() else ">" for l in text.strip().splitlines()))
    if url:
        parts.append(f"Source: <{url}>")
    if article:
        parts.append("---\n\n" + article)
    extra = {"captured": now().isoformat(timespec="minutes")}
    if url:
        extra["source"] = url
    return v.create_page(parent, title, content="\n\n".join(parts) + "\n", tags=tags,
                         state="todo" if as_task else None, extra=extra)


# ── Calendar feed ─────────────────────────────────────────────────────────────

def calendar_ics(vaults: Optional[str] = None) -> str:
    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")

    stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//forest//tasks//EN", "X-WR-CALNAME:forest tasks",
             "CALSCALE:GREGORIAN"]
    icons = {"todo": "○", "in-progress": "◐", "blocked": "✗", "waiting": "⏸", "done": "✓"}
    for p in store.agenda(vaults, state="all"):
        if not p.get("due"):
            continue
        d = date.fromisoformat(p["due"])
        lines += ["BEGIN:VEVENT", f"UID:{esc(p['vault'] + ':' + p['path'])}@forest", f"DTSTAMP:{stamp}",
                  f"DTSTART;VALUE=DATE:{d.strftime('%Y%m%d')}",
                  f"DTEND;VALUE=DATE:{(d + timedelta(days=1)).strftime('%Y%m%d')}",
                  f"SUMMARY:{esc(icons.get(p['state'], '') + ' ' + p['name'])}",
                  f"DESCRIPTION:{esc(p['vault'] + ':' + p['path'] + ' · ' + (p.get('priority') or ''))}",
                  f"URL:{page_url(p['vault'], p['path'])}",
                  "STATUS:" + ("COMPLETED" if p["state"] == "done" else "CONFIRMED"),
                  "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"
