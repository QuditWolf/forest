"""Forest MCP server - network (Streamable HTTP at /mcp) and local stdio.

Every tool is a thin wrapper over store. Access control comes from the caller's
token (read/write scope, allowed vaults); every call is audited and every write
becomes a git commit authored by the caller.
"""

from __future__ import annotations

import functools
import json
from datetime import date
from typing import Any, List, Optional

import base64
from pathlib import Path

from mcp.server.mcpserver import Image, MCPServer
from mcp.types import ToolAnnotations

from . import auth, features, store
from .config import VALID_PRIORITIES, VALID_STATES

INSTRUCTIONS = """\
Forest is the user's personal knowledge base + task planner: plain Markdown pages with YAML
frontmatter in git-versioned vaults:
- "forest" = knowledge (notes, projects, journal, inbox, templates). Pages here are NOT tasks:
  no state/priority/due. Organise work with ordinary pages: lists/tables of [[tasks:...]] links
  (they render with live task status) or a ```tasks block (e.g. `tag: alpha`) that lists tasks live.
- "tasks" = the task planner. Flat: tasks live at the root or in a category (top-level folder).
  No subtasks - bigger work gets a forest page that links or lists its tasks.

Pages: "dir/index.md" is a folder-page, "x.md" a leaf. Refs: "vault:path" (e.g. "tasks:work/report"),
or path + vault argument; .md optional; short ids and unique names resolve too.

Conventions (follow them):
- Cross-link both ways with [[wikilinks]]: a task description links notes ([[forest:projects/alpha]]),
  notes link tasks ([[tasks:work/report]]). [[page|alias]], [[page#Heading]]. Never markdown links for internal refs.
- Tags: frontmatter `tags: [a, b]` and inline #tag / #area/sub. Shared across vaults: tag a task #alpha
  and it appears on any forest page with a ```tasks tag: alpha``` block.
- Task fields (tasks vault only): state (todo|in-progress|blocked|waiting|done), priority
  (high|medium|low), due (YYYY-MM-DD).
- Search (search/grep/find) before creating, to avoid duplicates.
- For additive notes use append; for surgical changes use edit (exact string replace);
  write replaces the whole file. Pass expected_sha (from read) to avoid clobbering concurrent edits.
- Deletes are soft (.shadow, restorable) and every change is a git commit (see history/version/restore_version).
- Pages with frontmatter `ai: readonly` (or under such a folder) cannot be changed by you; ask the user.
- Daily notes live in the journal (daily_note). Log agent work there with append.
- For the full guide call the `skill` prompt or fetch /api/skill.
"""

mcp = MCPServer("forest", instructions=INSTRUCTIONS)

RO = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
RW = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)


class ToolError(Exception):
    pass


def _j(data: Any) -> str:
    return json.dumps(data, indent=1, default=str, ensure_ascii=False)


def _page_dict(p, content: bool = True) -> dict:
    d = p.model_dump(exclude={"backlinks"} if not p.backlinks else set())
    if not content:
        d.pop("content", None)
    for k in ("due", "created", "completed"):
        if d.get(k) is not None:
            d[k] = str(d[k])
    if not d.get("extra"):
        d.pop("extra", None)
    if not d.get("children"):
        d.pop("children", None)
    return d


def _vault_for(ref_or_vault: Optional[str], vault: Optional[str], write: bool = False) -> tuple[store.Vault, str]:
    """Resolve (vault, path) from a ref, enforcing the caller's vault/scope permissions."""
    vname, path = store.split_ref(ref_or_vault or "", vault)
    return _check_vault(vname, write), path


def _check_vault(vname: Optional[str], write: bool = False) -> store.Vault:
    v = store.vault(vname)
    p = auth.principal.get()
    if not p.can_access(v.name):
        raise ToolError(f"This token has no access to vault {v.name!r}")
    if write and not p.can_write:
        raise ToolError("This token is read-only")
    return v


def _allowed_vaults(vault: Optional[str]) -> Optional[str]:
    """For cross-vault reads: restrict 'all' to the vaults the caller may see."""
    p = auth.principal.get()
    if vault and vault != "all":
        for n in vault.split(","):
            _check_vault(n.strip())
        return vault
    if "*" in p.vaults:
        return None
    return ",".join(n for n in store.VAULTS if p.can_access(n)) or "__none__"


def tool(annotations: ToolAnnotations, write: bool = False):
    """Register a tool with scope check, actor propagation, error mapping and audit."""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            p = auth.principal.get()
            if write and not p.can_write:
                return "error: this token is read-only"
            tok = store.actor.set(p.actor + " (mcp)")
            try:
                result = fn(*args, **kwargs)
                auth.audit("mcp.call", tool=fn.__name__, args=_brief(kwargs), ok=True)
                return result
            except (ToolError, FileNotFoundError, FileExistsError, ValueError, KeyError,
                    PermissionError, store.ConflictError) as e:
                auth.audit("mcp.call", tool=fn.__name__, args=_brief(kwargs), ok=False, error=str(e)[:200])
                return f"error: {e}"
            finally:
                store.actor.reset(tok)
        return mcp.tool(annotations=annotations, structured_output=False)(wrapper)
    return deco


def _brief(kwargs: dict) -> dict:
    return {k: (v[:80] + "…" if isinstance(v, str) and len(v) > 80 else v) for k, v in kwargs.items()}


def _date(s: Optional[str]) -> Optional[date]:
    return date.fromisoformat(s) if s else None


# ── discovery / navigation ────────────────────────────────────────────────────

@tool(RO)
def vaults() -> str:
    """List vaults you can access with page counts, plus valid state/priority values."""
    p = auth.principal.get()
    out = []
    for name, v in store.VAULTS.items():
        if p.can_access(name):
            out.append({"vault": name, "pages": sum(1 for _ in v.iter_md())})
    return _j({"vaults": out, "default": store.DEFAULT_VAULT, "states": VALID_STATES,
               "priorities": VALID_PRIORITIES, "access": p.scope})


@tool(RO)
def tree(vault: Optional[str] = None, path: Optional[str] = None, depth: int = 0) -> str:
    """Show the page hierarchy of a vault as an indented outline (path, name, [state]).
    path: only the subtree under this folder-page. depth: max levels (0 = unlimited)."""
    v = _check_vault(vault)
    nodes = v.get_tree()
    if path:
        target = v.get_page(path).path

        def find(ns):
            for n in ns:
                if n.path == target:
                    return n.children
                r = find(n.children)
                if r is not None:
                    return r
            return None
        nodes = find(nodes) or []
    lines: List[str] = [f"# vault: {v.name}"]

    def walk(ns, d):
        for n in ns:
            st = f" [{n.state}]" if n.state else ""
            lines.append(f"{'  ' * d}{'▸ ' if n.is_folder else '- '}{n.name}{st}  ({n.path})")
            if n.children and (not depth or d + 1 < depth):
                walk(n.children, d + 1)
    walk(nodes, 0)
    return "\n".join(lines) if len(lines) > 1 else f"# vault: {v.name}\n(empty)"


@tool(RO)
def ls(path: Optional[str] = None, vault: Optional[str] = None) -> str:
    """List the direct children of a folder-page (or the vault root) with their metadata."""
    v, p = _vault_for(path, vault)
    parent = v.get_page(p).path if p else None
    return _j([store._summary(c) for c in v.list_children(parent)])


@tool(RO)
def read(ref: str, vault: Optional[str] = None, raw: bool = False, section: Optional[str] = None,
         offset: int = 0, limit: int = 0, line_numbers: bool = False) -> str:
    """Read a page. Returns metadata (incl. sha for safe edits, outline, url) and markdown body.
    raw=True returns the exact file text (frontmatter + body) - what edit/write operate on.
    section="Heading" returns only that section (saves context; see outline in metadata).
    offset/limit select a line range (0-based offset, limit 0 = all); line_numbers prefixes lines.
    A ref may also carry a section: "page#Heading"."""
    if "#" in ref and not section:
        ref, section = ref.split("#", 1)
    v, p = _vault_for(ref, vault)
    page = v.get_page(p)
    if section:
        text = features.section(page.content, section)
    else:
        text = v.read_raw(page.path)[0] if raw else page.content
    lines = text.splitlines()
    total = len(lines)
    if offset or limit:
        lines = lines[offset: offset + limit if limit else None]
    if line_numbers:
        lines = [f"{i + offset + 1:>5}\t{l}" for i, l in enumerate(lines)]
    meta = _page_dict(page, content=False)
    meta["lines"] = total
    meta["url"] = features.page_url(v.name, page.path)
    meta["link"] = f"[[{features.ref_for(v.name, page.path)}]]"
    heads = features.outline(page.content)
    if len(heads) > 1:
        meta["outline"] = [("  " * (h["level"] - 1)) + h["text"] for h in heads]
    if v.is_ai_readonly(page.path):
        meta["ai"] = "readonly"
    head = f"<page vault={v.name} path={page.path} sha={page.sha}>\n{_j(meta)}\n</page>\n"
    return head + "\n".join(lines)


@tool(RO)
def search(query: str, vault: Optional[str] = None, limit: int = 30) -> str:
    """Full-text search (all words must match, case-insensitive) across vaults. Name matches rank first."""
    return _j(store.search_pages(query, _allowed_vaults(vault), limit=limit))


@tool(RO)
def grep(pattern: str, vault: Optional[str] = None, regex: bool = True, ignore_case: bool = True,
         glob: Optional[str] = None, context: int = 0, max_results: int = 100) -> str:
    """grep -rn over raw page files (frontmatter included). Output lines: vault:path:line: text.
    glob filters paths (e.g. 'projects/*'). context adds N lines before/after."""
    hits = store.grep(pattern, _allowed_vaults(vault), regex=regex, ignore_case=ignore_case,
                      path_glob=glob, context=context, max_results=max_results)
    if not hits:
        return "no matches"
    out = []
    for h in hits:
        for i, b in enumerate(h.get("before", [])):
            out.append(f"{h['vault']}:{h['path']}-{h['line'] - len(h['before']) + i}- {b}")
        out.append(f"{h['vault']}:{h['path']}:{h['line']}: {h['text']}")
        for i, a in enumerate(h.get("after", [])):
            out.append(f"{h['vault']}:{h['path']}-{h['line'] + 1 + i}- {a}")
        if context:
            out.append("--")
    if len(hits) >= max_results:
        out.append(f"(stopped at {max_results} matches)")
    return "\n".join(out)


@tool(RO)
def find(glob: str, vault: Optional[str] = None) -> str:
    """Find page files by glob on their path or filename, e.g. '*meeting*', 'projects/**/index.md'."""
    return "\n".join(store.glob_pages(glob, _allowed_vaults(vault))) or "no matches"


@tool(RO)
def links(ref: str, vault: Optional[str] = None) -> str:
    """Outgoing [[links]] of a page (resolved, incl. broken ones), backlinks from all vaults, and
    unlinked_mentions: pages naming this page in plain text without linking (candidates to link)."""
    v, p = _vault_for(ref, vault)
    path = v.get_page(p).path
    pr = auth.principal.get()
    return _j({"page": f"{v.name}:{path}", "outgoing": store.outgoing_links(v.name, path),
               "backlinks": [b for b in store.get_backlinks(v.name, path) if pr.can_access(b.split(":")[0])],
               "unlinked_mentions": [m for m in features.unlinked_mentions(v.name, path) if pr.can_access(m["vault"])]})


@tool(RO)
def graph(ref: Optional[str] = None, vault: Optional[str] = None, depth: int = 2, hierarchy: bool = True) -> str:
    """Graph around a page: nodes (vault:path, name, state, is_folder) and edges (from, to, kind) where
    kind is "child" (parent folder-page -> child page) or "link" ([[wikilink]] from -> to). Follows
    links, backlinks and (hierarchy=True) parents/children `depth` hops across vaults.
    Without ref: the whole vault (can be large)."""
    v, p = _vault_for(ref, vault) if ref else (_check_vault(vault), None)
    g = features.graph(v.name, p or None, depth, hierarchy)
    pr = auth.principal.get()
    g["nodes"] = [n for n in g["nodes"] if pr.can_access(n["vault"])]
    keep = {n["id"] for n in g["nodes"]}
    g["edges"] = [e for e in g["edges"] if e["from"] in keep and e["to"] in keep]
    return _j(g)


@tool(RO)
def tags(tag: Optional[str] = None, vault: Optional[str] = None) -> str:
    """Without tag: all tags with counts. With tag: pages carrying it (hierarchical: 'proj' matches 'proj/x')."""
    vs = _allowed_vaults(vault)
    return _j(store.pages_with_tag(tag, vs) if tag else store.all_tags(vs))


@tool(RO)
def agenda(vault: Optional[str] = None, state: str = "undone", priority: Optional[str] = None,
           due_within: Optional[str] = None, overdue: bool = False, tag: Optional[str] = None,
           under: Optional[str] = None) -> str:
    """Tasks (tasks vault), sorted by due date then priority.
    state: undone (default) | all | done | todo | in-progress | blocked | waiting.
    due_within: '3d', '2w', '1m'. overdue: only past-due. tag: e.g. a project tag.
    under: category (e.g. "work")."""
    return _j(store.agenda(_allowed_vaults(vault), state=state, priority=priority,
                           due_within=due_within, overdue=overdue, tag=tag, under=under))


# ── writes ────────────────────────────────────────────────────────────────────

@tool(RW, write=True)
def create(name: str, vault: Optional[str] = None, parent: Optional[str] = None, content: str = "",
           state: Optional[str] = None, priority: Optional[str] = None, due: Optional[str] = None,
           tags: Optional[List[str]] = None, as_folder: bool = False, template: Optional[str] = None) -> str:
    """Create a page. Tasks (anything with state/priority/due) go to the tasks vault - used
    automatically when task fields are given without a vault - at the root or in a category
    (parent = "category" or "category/index.md"; as_folder=True at the root creates a category).
    Knowledge pages go to forest under any parent (a leaf parent becomes a folder-page).
    template: e.g. "meeting", "project", "task" (see templates). Search first to avoid duplicates."""
    if not vault and not (parent and ":" in parent) and (state or priority or due):
        tv = [n for n, x in store.VAULTS.items() if x.is_tasks]
        vault = tv[0] if tv else None
    v, par = _vault_for(parent, vault, write=True) if parent else (_check_vault(vault, True), None)
    page = v.create_page(par or None, name, content=content, state=state, priority=priority,
                         due=_date(due), tags=tags, as_folder=as_folder, template=template)
    return _j({"created": f"{v.name}:{page.path}", "sha": page.sha, "link": f"[[{features.ref_for(v.name, page.path)}]]",
               "url": features.page_url(v.name, page.path)})


@tool(RO)
def templates() -> str:
    """List page templates (editable pages in the templates folder) usable with create(template=...)."""
    return _j(features.list_templates())


@tool(RW, write=True)
def daily_note(date: Optional[str] = None) -> str:
    """Get the daily journal note for a date (YYYY-MM-DD, default today), creating it from the
    daily template if missing. Append your work log to it with append(ref, text)."""
    jv = store.split_ref(features.JOURNAL)[0]
    _check_vault(jv, write=True)
    page = features.daily_note(_date(date))
    return (f"<page vault={page.vault} path={page.path} sha={page.sha}>\n"
            f"{_j({'name': page.name, 'url': features.page_url(page.vault, page.path)})}\n</page>\n{page.content}")


@tool(RW, write=True)
def capture(text: str = "", title: Optional[str] = None, url: Optional[str] = None,
            fetch: bool = False, tags: Optional[List[str]] = None, as_task: bool = False) -> str:
    """Quick-capture a note or link into the inbox (triage later). url is stored as `source`;
    fetch=True saves a markdown copy of the article; as_task=True gives it state todo."""
    _check_vault(store.split_ref(features.INBOX)[0], write=True)
    p = features.capture(title, text, url, None, fetch, tags, as_task)
    return _j({"captured": f"{p.vault}:{p.path}", "link": f"[[{features.ref_for(p.vault, p.path)}]]"})


@tool(RO)
def review(period: str = "day", stale_days: int = 7) -> str:
    """Structured review bundle. period: "day" | "week". Returns overdue, due_soon, in_progress,
    stale (in progress with no git change for stale_days), done this period, inbox count/items,
    and recent changes split into yours vs agents'."""
    return _j(features.review(period, stale_days, _allowed_vaults(None)))


# ── attachments ───────────────────────────────────────────────────────────────

@tool(RO)
def attachments(ref: str, vault: Optional[str] = None) -> str:
    """List files attached to a page (stored in <vault>/_assets/<page>/)."""
    v, p = _vault_for(ref, vault)
    return _j(features.list_assets(v.name, p))


@tool(RO)
def read_attachment(path: str, vault: Optional[str] = None):
    """Read an attachment by its path (from attachments). Images come back as images you can see;
    PDFs as extracted text; text files as text."""
    v, p = _vault_for(path, vault)
    f = features.asset_file(v.name, p)
    suf = f.suffix.lower()
    if suf in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
        if f.stat().st_size > 8 * 1024 * 1024:
            return "error: image larger than 8 MB"
        return Image(data=f.read_bytes(), format={"jpg": "jpeg"}.get(suf[1:], suf[1:]))
    if suf == ".pdf":
        return features.pdf_text(v.name, p)
    try:
        return f.read_text(encoding="utf-8")[:200_000]
    except UnicodeDecodeError:
        return f"binary file ({f.stat().st_size} bytes): {f.name}"


@tool(DESTRUCTIVE, write=True)
def delete_attachment(path: str, vault: Optional[str] = None) -> str:
    """Soft-delete an attachment (path from attachments). Restorable with restore(shadow_path).
    Links to it in pages will break - edit them if needed."""
    v, p = _vault_for(path, vault, write=True)
    return _j(features.delete_asset(v.name, p))


@tool(RW, write=True)
def attach(ref: str, filename: str, base64_data: str, vault: Optional[str] = None) -> str:
    """Attach a file (base64-encoded) to a page. Returns the markdown to embed/link it; this does
    not edit the page - append/edit the returned markdown where it belongs."""
    v, p = _vault_for(ref, vault, write=True)
    try:
        data = base64.b64decode(base64_data, validate=True)
    except Exception:
        raise ValueError("base64_data is not valid base64")
    return _j(features.save_asset(v.name, p, filename, data))


@tool(RW, write=True)
def write(ref: str, text: str, vault: Optional[str] = None, expected_sha: Optional[str] = None) -> str:
    """Write a whole page file (YAML frontmatter + markdown), creating it if missing.
    Missing parent folders become folder-pages. Replaces existing content entirely -
    prefer edit/append for changes. Pass expected_sha from read to avoid clobbering."""
    v, p = _vault_for(ref, vault, write=True)
    page = v.write_raw(p if p.endswith(".md") else p + ".md", text, expected_sha=expected_sha)
    return _j({"written": f"{v.name}:{page.path}", "sha": page.sha})


@tool(RW, write=True)
def edit(ref: str, old: str, new: str, vault: Optional[str] = None, replace_all: bool = False,
         expected_sha: Optional[str] = None) -> str:
    """Exact string replacement in the raw page file (see read raw=True). old must be unique
    unless replace_all. Works on frontmatter too."""
    v, p = _vault_for(ref, vault, write=True)
    page = v.edit_raw(p, old, new, replace_all=replace_all, expected_sha=expected_sha)
    return _j({"edited": f"{v.name}:{page.path}", "sha": page.sha})


@tool(RW, write=True)
def append(ref: str, text: str, vault: Optional[str] = None, timestamp: bool = True) -> str:
    """Append a note to the end of a page (prefixed with **YYYY-MM-DD HH:MM**: unless timestamp=False)."""
    v, p = _vault_for(ref, vault, write=True)
    page = v.append_to_page(p, text, timestamp=timestamp)
    return _j({"appended": f"{v.name}:{page.path}", "sha": page.sha})


@tool(RW, write=True)
def update(ref: str, vault: Optional[str] = None, name: Optional[str] = None, state: Optional[str] = None,
           priority: Optional[str] = None, due: Optional[str] = None, tags: Optional[List[str]] = None,
           content: Optional[str] = None, expected_sha: Optional[str] = None) -> str:
    """Update page metadata/body. Only given fields change. state/priority/due exist on tasks only;
    use "" to clear them. Marking state=done stamps 'completed'. content replaces the whole body."""
    v, p = _vault_for(ref, vault, write=True)
    fields: dict = {}
    if name is not None:
        fields["name"] = name
    if state is not None:
        fields["state"] = state or None
    if priority is not None:
        fields["priority"] = priority or None
    if due is not None:
        fields["due"] = _date(due) if due else None
    if tags is not None:
        fields["tags"] = tags
    if content is not None:
        fields["content"] = content
    page = v.update_page(p, expected_sha=expected_sha, **fields)
    return _j(_page_dict(page, content=False))


@tool(RW, write=True)
def move(ref: str, new_parent: Optional[str] = None, vault: Optional[str] = None) -> str:
    """Move a page (with its children) under new_parent (page path in the same vault; omitted = root)."""
    v, p = _vault_for(ref, vault, write=True)
    page = v.move_page(p, new_parent or None)
    return _j({"moved": f"{v.name}:{page.path}"})


@tool(RW, write=True)
def promote(ref: str, vault: Optional[str] = None) -> str:
    """Convert a leaf page into a folder-page so it can have children (knowledge vaults only;
    tasks are flat)."""
    v, p = _vault_for(ref, vault, write=True)
    return _j({"promoted": f"{v.name}:{v.promote_to_folder(p).path}"})


@tool(DESTRUCTIVE, write=True)
def delete(ref: str, vault: Optional[str] = None) -> str:
    """Soft-delete a page (and its children) into the vault's .shadow; restorable with restore."""
    v, p = _vault_for(ref, vault, write=True)
    v.delete_page(p)
    return _j({"deleted": f"{v.name}:{p}"})


@tool(RO)
def trash(vault: Optional[str] = None) -> str:
    """List soft-deleted pages and attachments in a vault's .shadow."""
    return _j(_check_vault(vault).list_shadow())


@tool(RW, write=True)
def restore(shadow_path: str, vault: Optional[str] = None) -> str:
    """Restore a soft-deleted page or attachment (shadow_path from trash) to its original location."""
    v = _check_vault(vault, write=True)
    if shadow_path.startswith("_assets/"):
        return _j({"restored": features.restore_asset(v.name, shadow_path)})
    return _j({"restored": f"{v.name}:{v.restore_from_shadow(shadow_path).path}"})


# ── history (git) ─────────────────────────────────────────────────────────────

@tool(RO)
def history(ref: Optional[str] = None, vault: Optional[str] = None, limit: int = 20,
            since: Optional[str] = None) -> str:
    """Git history of a page (or the whole vault if ref omitted): rev, author, date, message, files.
    since: git date like '3 days ago' or '2026-01-01'."""
    v, p = _vault_for(ref, vault) if ref else (_check_vault(vault), None)
    return _j(v.history(p or None, limit=limit, since=since))


@tool(RO)
def changes(since: str = "7 days ago", vault: Optional[str] = None, limit: int = 30) -> str:
    """Recent commits across vaults (what changed lately, by whom - web, tokens, agents)."""
    return _j(store.recent_changes(since, _allowed_vaults(vault), limit=limit))


@tool(RO)
def version(ref: str, rev: str, vault: Optional[str] = None) -> str:
    """Raw content of a page at a past revision (rev from history)."""
    v, p = _vault_for(ref, vault)
    return v.show_version(p, rev)


@tool(RO)
def diff(rev: str, vault: Optional[str] = None, ref: Optional[str] = None) -> str:
    """The patch introduced by a commit, optionally limited to one page."""
    v, p = _vault_for(ref, vault) if ref else (_check_vault(vault), None)
    return v.diff(rev, p)


@tool(RW, write=True)
def restore_version(ref: str, rev: str, vault: Optional[str] = None) -> str:
    """Restore a page's content to a past revision (creates a new commit; history is kept)."""
    v, p = _vault_for(ref, vault, write=True)
    page = v.restore_version(p, rev)
    return _j({"restored": f"{v.name}:{page.path}", "rev": rev, "sha": page.sha})


# ── prompts ───────────────────────────────────────────────────────────────────

@mcp.prompt()
def daily_review() -> str:
    """Plan today from the task vaults."""
    return ("Use the forest tools. 1) Call review(period='day') and daily_note(). "
            "2) Summarise briefly: overdue, due soon, in progress, stale, inbox count. "
            "3) Propose a short prioritised plan for today (max 5 items, as [[links]]). "
            "4) Ask me to confirm; only then update task states and append the plan to today's daily note.")


@mcp.prompt()
def weekly_review() -> str:
    """Review the past week and plan the next."""
    from datetime import date as _d
    y, w, _ = features.today().isocalendar()
    return ("Use the forest tools. 1) Call review(period='week'). 2) Draft a weekly review using "
            "create(name='week-%d-W%02d', vault='forest', parent='journal/index.md', template='weekly-review') "
            "(or read it if it exists) and fill the sections: done, slipped/blocked, stale, next week's "
            "priorities (as [[links]]), agent activity. 3) Show me the draft summary. "
            "4) Ask before changing any task (states, due dates) - propose changes as a list first." % (y, w))


@mcp.prompt()
def skill() -> str:
    """The full Forest usage guide for agents (mental model, tools, recipes, rules)."""
    return (Path(__file__).parent / "skill" / "SKILL.md").read_text(encoding="utf-8")


def run_stdio():
    """Local, offline MCP over stdio (full access - it's your machine)."""
    store.init_vaults()
    auth.principal.set(auth.Principal("local", "local-stdio"))
    mcp.run("stdio")
