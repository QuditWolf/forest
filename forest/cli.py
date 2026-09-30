"""Forest CLI.

Client commands talk to a (remote) Forest server over HTTP - configure once with
`forest login --url https://tools.example.org --token fst_...`.
Server commands (serve, token, mcp, restore) act on the local data dir.

Page refs: "path/to/page", "vault:path", a 6-char short id, or a unique page name.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path
from typing import List, Optional

import typer
from rich import box
from rich.console import Console
from rich.markdown import Markdown
from rich.markup import escape
from rich.table import Table

from .client import CONFIG_PATH, ClientError, Forest, load_config, save_config

VALID_STATES = ["todo", "in-progress", "blocked", "waiting", "done"]
VALID_PRIORITIES = ["high", "medium", "low"]

app = typer.Typer(name="forest", help="Personal knowledge base & task planner.",
                  add_completion=True, no_args_is_help=True)
token_app = typer.Typer(help="Manage access tokens (server side, local data dir).", no_args_is_help=True)
app.add_typer(token_app, name="token")
console = Console()
err = Console(stderr=True)

VaultOpt = typer.Option(None, "--vault", "-V", help="Vault (default from config)")


def _client() -> Forest:
    return Forest()


def _die(msg: str):
    err.print(f"[red]{msg}[/red]")
    raise typer.Exit(1)


def _ref(f: Forest, ref: str, vault: Optional[str]) -> tuple[str, str]:
    return f.split(ref, vault)


# ── Formatting helpers ─────────────────────────────────────────────────────────

def _state_style(state: Optional[str]) -> str:
    return {"todo": "white", "in-progress": "cyan bold", "blocked": "red bold",
            "waiting": "yellow", "done": "dim"}.get(state or "", "dim")


def _priority_dot(priority: Optional[str]) -> str:
    return {"high": "[red]●[/red]", "medium": "[yellow]◉[/yellow]", "low": "[dim]○[/dim]"}.get(priority or "", "")


def _due_style(due: Optional[str], state: Optional[str]) -> str:
    if state == "done" or not due:
        return "dim"
    delta = (date.fromisoformat(due) - date.today()).days
    return "red bold" if delta < 0 else "yellow" if delta <= 3 else "green"


def _page_table(pages: list, show_vault: bool = True) -> Table:
    t = Table(box=box.ROUNDED, header_style="bold cyan")
    t.add_column("#", style="dim", no_wrap=True, width=6)
    t.add_column("Name", min_width=20)
    t.add_column("Path", style="dim")
    t.add_column("State", no_wrap=True)
    t.add_column("Pri", justify="center")
    t.add_column("Due", no_wrap=True)
    for p in pages:
        st, due = p.get("state"), p.get("due")
        name = f"[dim]{escape(p['name'])}[/dim]" if st == "done" else escape(p["name"])
        path = f"{p['vault']}:{p['path']}" if show_vault and p.get("vault") else p["path"]
        t.add_row(p.get("short_id", ""), name, path,
                  f"[{_state_style(st)}]{st or ''}[/]", _priority_dot(p.get("priority")),
                  f"[{_due_style(due, st)}]{due or ''}[/]")
    return t


# ── Setup ─────────────────────────────────────────────────────────────────────

@app.command()
def login(url: str = typer.Option(..., prompt=True, help="Server URL, e.g. https://tools.example.org"),
          token: str = typer.Option(..., prompt=True, hide_input=True, help="Access token (fst_...)"),
          vault: str = typer.Option("forest", help="Default vault")):
    """Save server URL + token to ~/.config/forest/client.toml and test them."""
    try:
        me = Forest(url, token, vault).me()
    except ClientError as e:
        _die(str(e))
    path = save_config(url, token, vault)
    console.print(f"[green]ok[/green] {me['name']} ({me['scope']}) · vaults: {', '.join(me['vaults'])} · saved {path}")


@app.command()
def vaults():
    """List vaults on the server."""
    try:
        for v in _client().vaults():
            console.print(f"{v['name']:<12} {v['pages']} pages")
    except ClientError as e:
        _die(str(e))


# ── Pages ─────────────────────────────────────────────────────────────────────

@app.command()
def add(
    parent: str = typer.Argument(..., help="Parent page ref or '.' for root"),
    name: str = typer.Argument(..., help="Page name"),
    state: Optional[str] = typer.Option(None, "--state", "-s"),
    priority: Optional[str] = typer.Option(None, "--priority", "-p", help="high|medium|low"),
    due: Optional[str] = typer.Option(None, "--due", "-d", help="YYYY-MM-DD"),
    tag: List[str] = typer.Option([], "--tag", "-t"),
    folder: bool = typer.Option(False, "--folder", "-f", help="Create as folder-page"),
    vault: Optional[str] = VaultOpt,
):
    """Create a new page under a parent (use '.' or 'vault:.' for root)."""
    f = _client()
    v, parent_path = _ref(f, parent, vault)
    try:
        p = f.create(v, name=name, parent_path=None if parent_path in (".", "") else parent_path,
                     state=state, priority=priority, due=due, tags=tag or None, as_folder=folder)
        console.print(f"[green]Created[/green] \\[{p['short_id']}] {v}:{p['path']}")
    except ClientError as e:
        _die(str(e))


@app.command(name="list")
def list_pages(
    under: Optional[str] = typer.Argument(None, help="Only pages under this path"),
    state: str = typer.Option("undone", "--state", "-s", help="all | undone | done | <state>"),
    high: str = typer.Option("all", "--high", help="Due window for high priority (e.g. all, 2w, none)"),
    medium: str = typer.Option("all", "--medium", help="Due window for medium priority"),
    low: str = typer.Option("all", "--low", help="Due window for low priority"),
    tag: Optional[str] = typer.Option(None, "--tag", "-t"),
    vault: Optional[str] = typer.Option(None, "--vault", "-V", help="Vault or 'all' (default all)"),
):
    """List task pages (anything with a state), sorted by due date then priority."""
    f = _client()
    v = vault
    if under and ":" in under:
        v, under = f.split(under)
    try:
        pages = f.agenda(vault=v, state=state, under=under, tag=tag, high=high, medium=medium, low=low)
    except ClientError as e:
        _die(str(e))
    if not pages:
        console.print("[dim]No pages found.[/dim]")
        return
    console.print(_page_table(pages))


@app.command()
def agenda(
    days: int = typer.Option(7, "--days", "-d", help="Due within N days"),
    vault: Optional[str] = typer.Option(None, "--vault", "-V"),
):
    """Overdue + due soon + in progress, across vaults."""
    f = _client()
    try:
        overdue = f.agenda(vault=vault, overdue=True)
        soon = [p for p in f.agenda(vault=vault, due_within=f"{days}d") if p not in overdue]
        doing = [p for p in f.agenda(vault=vault, state="in-progress") if p not in overdue + soon]
    except ClientError as e:
        _die(str(e))
    for title, rows in (("Overdue", overdue), (f"Due in {days}d", soon), ("In progress", doing)):
        if rows:
            console.print(f"[bold]{title}[/bold]")
            console.print(_page_table(rows))
    if not (overdue or soon or doing):
        console.print("[dim]Nothing due.[/dim]")


@app.command()
def today(append: Optional[str] = typer.Argument(None, help="Text to append to today's note")):
    """Show today's journal note (created from the daily template), or append to it."""
    f = _client()
    try:
        p = f._req("GET", "/api/daily")
        if append:
            f.append(p["vault"], p["path"], append)
            console.print(f"[green]Logged[/green] to {p['vault']}:{p['path']}")
        else:
            console.print(Markdown(p["content"]))
    except ClientError as e:
        _die(str(e))


@app.command()
def capture(text: str = typer.Argument("", help="Note text"), url: Optional[str] = typer.Option(None, "--url", "-u"),
            title: Optional[str] = typer.Option(None, "--title"), fetch: bool = typer.Option(False, "--fetch", help="Save article copy"),
            task: bool = typer.Option(False, "--task", help="Capture as todo")):
    """Quick capture into the inbox (a note, or a link with --url)."""
    try:
        p = _client()._req("POST", "/api/capture", json={"text": text, "url": url, "title": title, "fetch": fetch, "as_task": task})
        console.print(f"[green]Captured[/green] {p['vault']}:{p['path']}")
    except ClientError as e:
        _die(str(e))


@app.command()
def link(ref: str, vault: Optional[str] = VaultOpt, wiki: bool = typer.Option(False, "--wiki", "-w", help="Print [[wikilink]] instead of URL")):
    """Print the web link (or wikilink with -w) for a page."""
    f = _client()
    v, path = _ref(f, ref, vault)
    try:
        p = f.page(v, path)
    except ClientError as e:
        _die(str(e))
    if wiki:
        print(f"[[{v}:{p['path'][:-3].removesuffix('/index')}]]")
    else:
        print(f"{f.url}/#{v}/{p['path']}")


@app.command()
def tree(vault: Optional[str] = VaultOpt):
    """Show the page tree of a vault."""
    from rich.tree import Tree
    f = _client()
    v = vault or f.default_vault
    try:
        nodes = f.tree(v)
    except ClientError as e:
        _die(str(e))
    root = Tree(f"[bold]{v}[/bold]")

    def walk(ns, parent):
        for n in ns:
            st = f" [{_state_style(n.get('state'))}]{n['state']}[/]" if n.get("state") else ""
            walk(n.get("children", []), parent.add(f"{n['name']}{st} [dim]{n['path']}[/dim]"))
    walk(nodes, root)
    console.print(root)


@app.command()
def show(ref: str = typer.Argument(..., help="Page ref"), vault: Optional[str] = VaultOpt,
         plain: bool = typer.Option(False, "--plain", help="Print markdown without rendering")):
    """Show page details and rendered content."""
    f = _client()
    v, path = _ref(f, ref, vault)
    try:
        p = f.page(v, path)
        bl = f.backlinks(v, p["path"])
    except ClientError as e:
        _die(str(e))
    console.print(f"[bold]{escape(p['name'])}[/bold]  [dim]\\[{p['short_id']}] {v}:{p['path']}[/dim]")
    if p.get("state"):
        console.print(f"  State:    [{_state_style(p['state'])}]{p['state']}[/]")
    if p.get("priority"):
        console.print(f"  Priority: {_priority_dot(p['priority'])} {p['priority']}")
    if p.get("due"):
        console.print(f"  Due:      [{_due_style(p['due'], p.get('state'))}]{p['due']}[/]")
    tags = sorted(set(p.get("tags", [])) | set(p.get("inline_tags", [])))
    if tags:
        console.print(f"  Tags:     {' '.join('#' + t for t in tags)}")
    console.print(f"  Created:  {p['created']}")
    if p.get("children"):
        console.print(f"  Children: {', '.join(c.rsplit('/', 1)[-1] for c in p['children'])}")
    if bl:
        console.print(f"  Backlinks: {', '.join(bl)}")
    if p["content"].strip():
        console.print()
        console.print(p["content"], markup=False, highlight=False) if plain else console.print(Markdown(p["content"]))


@app.command()
def cat(ref: str, vault: Optional[str] = VaultOpt):
    """Print the raw page file (frontmatter + markdown)."""
    f = _client()
    v, path = _ref(f, ref, vault)
    try:
        sys.stdout.write(f.raw(v, path)["text"])
    except ClientError as e:
        _die(str(e))


def _set(ref: str, vault: Optional[str], **fields):
    f = _client()
    v, path = _ref(f, ref, vault)
    try:
        return f.update(v, path, **fields)
    except ClientError as e:
        _die(str(e))


@app.command()
def state(ref: str, new_state: str = typer.Argument(..., help="|".join(VALID_STATES)),
          vault: Optional[str] = VaultOpt):
    """Update page state."""
    if new_state not in VALID_STATES:
        _die(f"Invalid state. Choose: {', '.join(VALID_STATES)}")
    p = _set(ref, vault, state=new_state)
    console.print(f"[green]Updated[/green] \\[{p['short_id']}] → {new_state}")


@app.command()
def done(ref: str, vault: Optional[str] = VaultOpt):
    """Mark a page as done."""
    p = _set(ref, vault, state="done")
    console.print(f"[green]Done[/green] \\[{p['short_id']}] {escape(p['name'])}")


@app.command()
def due(ref: str, when: str = typer.Argument(..., help="YYYY-MM-DD, +3d, +2w, or 'none'"),
        vault: Optional[str] = VaultOpt):
    """Set or clear a due date."""
    from .store import parse_due_window
    if when == "none":
        val = None
    elif when.startswith("+"):
        d = parse_due_window(when[1:])
        if not d:
            _die("Use +Nd, +Nw, +Nm or YYYY-MM-DD")
        val = d.isoformat()
    else:
        val = when
    p = _set(ref, vault, due=val)
    console.print(f"[green]Due[/green] \\[{p['short_id']}] {escape(p['name'])} → {val or 'none'}")


@app.command()
def mv(ref: str, new_parent: str = typer.Argument(..., help="New parent ref or '.' for root"),
       vault: Optional[str] = VaultOpt):
    """Move a page (with children) under a new parent."""
    f = _client()
    v, path = _ref(f, ref, vault)
    try:
        p = f.move(v, path, None if new_parent == "." else new_parent)
        console.print(f"[green]Moved[/green] → {v}:{p['path']}")
    except ClientError as e:
        _die(str(e))


@app.command()
def note(ref: str, text: str = typer.Argument(..., help="Note text to append"),
         vault: Optional[str] = VaultOpt):
    """Append a timestamped note to a page."""
    f = _client()
    v, path = _ref(f, ref, vault)
    try:
        p = f.append(v, path, text)
        console.print(f"[green]Note added[/green] to {v}:{p['path']}")
    except ClientError as e:
        _die(str(e))


@app.command()
def search(query: str, vault: Optional[str] = typer.Option(None, "--vault", "-V")):
    """Full-text search across vaults."""
    try:
        results = _client().search(query, vault)
    except ClientError as e:
        _die(str(e))
    if not results:
        console.print("[dim]No results found.[/dim]")
    for r in results:
        console.print(f"[cyan]{r['vault']}:{r['path']}[/cyan]  [bold]{escape(r['name'])}[/bold]")
        console.print(f"  [dim]{escape(r['snippet'])}[/dim]")


@app.command()
def grep(pattern: str, vault: Optional[str] = typer.Option(None, "--vault", "-V"),
         fixed: bool = typer.Option(False, "-F", help="Fixed string, not regex"),
         case: bool = typer.Option(False, "-s", help="Case sensitive"),
         glob: Optional[str] = typer.Option(None, "--glob", "-g"),
         context: int = typer.Option(0, "-C")):
    """grep -rn across page files on the server."""
    try:
        hits = _client().grep(pattern, vault=vault, regex=not fixed, ignore_case=not case,
                              glob=glob, context=context)
    except ClientError as e:
        _die(str(e))
    for h in hits:
        loc = f"[magenta]{h['vault']}:{h['path']}[/magenta]:[green]{h['line']}[/green]"
        for b in h.get("before", []):
            console.print(f"[dim]{h['vault']}:{h['path']}- {escape(b)}[/dim]", highlight=False)
        console.print(f"{loc}: {escape(h['text'])}", highlight=False)
        for a in h.get("after", []):
            console.print(f"[dim]{h['vault']}:{h['path']}- {a}[/dim]", highlight=False)


@app.command()
def tags(tag: Optional[str] = typer.Argument(None), vault: Optional[str] = typer.Option(None, "--vault", "-V")):
    """List tags, or pages with a tag."""
    f = _client()
    try:
        if tag:
            console.print(_page_table(f.tagged(tag, vault)))
        else:
            for t in f.tags(vault):
                console.print(f"#{t['tag']:<30} {t['count']}")
    except ClientError as e:
        _die(str(e))


@app.command()
def edit(ref: str, vault: Optional[str] = VaultOpt):
    """Open a page in $EDITOR (raw file); saves back with conflict detection."""
    f = _client()
    v, path = _ref(f, ref, vault)
    try:
        r = f.raw(v, path)
    except ClientError as e:
        _die(str(e))
    with tempfile.NamedTemporaryFile("w+", suffix=".md", prefix="forest-", delete=False) as tmp:
        tmp.write(r["text"])
    editor = os.environ.get("EDITOR", "vim")
    subprocess.run([*editor.split(), tmp.name])
    new = Path(tmp.name).read_text()
    if new == r["text"]:
        console.print("[dim]No changes.[/dim]")
        Path(tmp.name).unlink()
        return
    try:
        f.write_raw(v, r["path"], new, expected_sha=r["sha"])
        console.print(f"[green]Saved[/green] {v}:{r['path']}")
        Path(tmp.name).unlink()
    except ClientError as e:
        _die(f"{e}\nYour edit is kept at {tmp.name}")


@app.command()
def rm(ref: str, force: bool = typer.Option(False, "--force", "-f"), vault: Optional[str] = VaultOpt):
    """Soft-delete a page (moved to .shadow, restorable)."""
    f = _client()
    v, path = _ref(f, ref, vault)
    try:
        p = f.page(v, path)
        if not force and not typer.confirm(f"Delete '{v}:{p['path']}'?"):
            raise typer.Exit(0)
        f.delete(v, p["path"])
        console.print(f"[red]Deleted[/red] {v}:{p['path']}")
    except ClientError as e:
        _die(str(e))


@app.command()
def history(ref: Optional[str] = typer.Argument(None), vault: Optional[str] = VaultOpt,
            limit: int = typer.Option(20, "-n")):
    """Git history of a page (or the whole vault)."""
    f = _client()
    v, path = _ref(f, ref, vault) if ref else (vault or f.default_vault, None)
    try:
        for e in f.history(v, path, limit):
            console.print(f"[yellow]{e['short']}[/yellow] {e['date'][:16]} [cyan]{e['author']}[/cyan] {e['message']}")
    except ClientError as e:
        _die(str(e))


@app.command()
def tui():
    """Launch the interactive TUI (connects to the configured server)."""
    from .tui import main as tui_main
    tui_main()


# ── Backup / offline / sync ───────────────────────────────────────────────────

@app.command()
def backup(dest: Path = typer.Argument(Path("."), help="Directory or file to write")):
    """Download a full backup (all vaults + git history) from the server."""
    try:
        out = _client().download_backup(dest)
        console.print(f"[green]Saved[/green] {out}")
    except ClientError as e:
        _die(str(e))


@app.command()
def restore(archive: Path, data: Optional[Path] = typer.Option(None, "--data", help="Data dir (default FOREST_DATA)"),
            yes: bool = typer.Option(False, "--yes", "-y")):
    """Restore a backup into a LOCAL data dir (e.g. to run offline or rebuild a server).
    Existing vault contents are moved aside, not deleted."""
    if data:
        os.environ["FOREST_DATA"] = str(data.expanduser())
    from . import backup as bk
    from . import store
    store.init_vaults()
    if not yes and not typer.confirm(f"Restore into {[str(v.root) for v in store.VAULTS.values()]}?"):
        raise typer.Exit(0)
    res = bk.restore_backup(archive)
    console.print(f"[green]Restored[/green] {', '.join(res['restored'])}; previous contents in {res['moved_aside_to']}")


def _git_url(url: str, vault: str) -> str:
    return f"{url.rstrip('/')}/git/{vault}.git"


def _git_auth_args(token: str) -> list[str]:
    import base64
    b = base64.b64encode(f"forest:{token}".encode()).decode()
    return ["-c", f"http.extraHeader=Authorization: Basic {b}"]


@app.command()
def clone(dest: Path = typer.Argument(Path("~/forest"), help="Where to put the local vault clones")):
    """git-clone every vault for offline use (token is not stored in the repo URL)."""
    f = _client()
    dest = dest.expanduser()
    dest.mkdir(parents=True, exist_ok=True)
    for v in f.me()["vaults"]:
        target = dest / v
        if target.exists():
            console.print(f"[dim]{target} exists - skipping[/dim]")
            continue
        r = subprocess.run(["git", *_git_auth_args(f.token), "clone", _git_url(f.url, v), str(target)])
        if r.returncode == 0:
            console.print(f"[green]Cloned[/green] {v} → {target}")
    console.print(f"Run offline:  FOREST_DATA={dest.parent} FOREST_VAULT_FOREST={dest}/forest "
                  f"FOREST_VAULT_TASKS={dest}/tasks forest serve")


@app.command()
def sync(dest: Path = typer.Argument(Path("~/forest"), help="Directory holding the vault clones")):
    """Pull then push every local vault clone (commits local changes first)."""
    f = _client()
    for d in sorted(dest.expanduser().iterdir()):
        if not (d / ".git").exists():
            continue
        g = ["git", "-C", str(d)]
        a = _git_auth_args(f.token)
        subprocess.run([*g, "add", "-A"])
        subprocess.run([*g, "commit", "-qm", "local edits"], capture_output=True)
        pull = subprocess.run([*g, *a, "pull", "--no-rebase", "--no-edit", _git_url(f.url, d.name), "main"])
        if pull.returncode != 0:
            err.print(f"[red]{d.name}: pull failed - resolve conflicts in {d} and re-run[/red]")
            continue
        push = subprocess.run([*g, *a, "push", _git_url(f.url, d.name), "HEAD:main"])
        console.print(f"[green]{d.name} synced[/green]" if push.returncode == 0 else f"[red]{d.name}: push failed[/red]")


# ── Server side ───────────────────────────────────────────────────────────────

@app.command()
def serve(host: Optional[str] = typer.Option(None, "--host"), port: Optional[int] = typer.Option(None, "--port", "-p"),
          data: Optional[Path] = typer.Option(None, "--data", help="Data dir"),
          reload: bool = typer.Option(False, "--reload", help="Hot-reload (dev)")):
    """Run the server (web UIs + API + MCP + git sync)."""
    if data:
        os.environ["FOREST_DATA"] = str(data.expanduser())
    from .config import SERVER_HOST, SERVER_PORT
    import uvicorn
    h, p = host or SERVER_HOST, port or SERVER_PORT
    console.print(f"forest serving on http://{h}:{p}")
    uvicorn.run("forest.api:app", host=h, port=p, reload=reload, proxy_headers=True)


@app.command()
def mcp(data: Optional[Path] = typer.Option(None, "--data", help="Data dir")):
    """Local MCP server over stdio (offline use with local agents)."""
    if data:
        os.environ["FOREST_DATA"] = str(data.expanduser())
    from .mcp_server import run_stdio
    run_stdio()


@token_app.command("create")
def token_create(name: str, scope: str = typer.Option("read", help="read | write"),
                 vault: List[str] = typer.Option(["*"], "--vault", "-V", help="Limit to vault(s)"),
                 days: Optional[int] = typer.Option(None, "--days", help="Expire after N days")):
    """Create a token (printed once)."""
    from . import auth
    pub, tok, _ = auth.tokens().create(name, scope, vault, days)
    auth.audit("token.create", token_id=pub["id"], name=name, scope=scope, vaults=vault, via="cli")
    console.print(f"[green]{pub['name']}[/green] ({scope}, vaults={','.join(vault)}) id={pub['id']}")
    console.print(tok)


@token_app.command("list")
def token_list():
    from . import auth
    for t in auth.tokens().list():
        console.print(f"{t['id']}  {t['name']:<24} {t['scope']:<5} {','.join(t['vaults']):<12} "
                      f"{t['kind']:<6} last used {t['last_used'] or 'never'}")


@token_app.command("revoke")
def token_revoke(token_id: str):
    from . import auth
    if auth.tokens().revoke(token_id):
        auth.audit("token.revoke", token_id=token_id, via="cli")
        console.print("[green]revoked[/green]")
    else:
        _die("no such token")


if __name__ == "__main__":
    app()
