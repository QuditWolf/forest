"""Typer CLI — thin layer over store."""
from __future__ import annotations

import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import List, Optional

import typer
from rich.console import Console
from rich.table import Table
from rich import box

from . import store
from .config import VALID_STATES, VALID_PRIORITIES, SERVER_HOST, SERVER_PORT

app = typer.Typer(
    name="tasks",
    help="Hierarchical knowledge base & task manager.",
    add_completion=True,
    no_args_is_help=True,
)
console = Console()
err_console = Console(stderr=True)


# ── Completion helpers ─────────────────────────────────────────────────────────

def _complete_page(incomplete: str) -> List[str]:
    try:
        pages = store.list_tasks()
        return [
            p.path for p in pages if p.path.startswith(incomplete)
        ] + [
            p.short_id for p in pages if p.short_id.startswith(incomplete)
        ]
    except Exception:
        return []


def _complete_state(incomplete: str) -> List[str]:
    return [s for s in VALID_STATES if s.startswith(incomplete)]


# ── Formatting helpers ─────────────────────────────────────────────────────────

def _state_style(state: Optional[str]) -> str:
    return {
        "todo": "white",
        "in-progress": "cyan bold",
        "blocked": "red bold",
        "waiting": "yellow",
        "done": "dim",
    }.get(state or "", "dim")


def _priority_dot(priority: Optional[str]) -> str:
    return {
        "high": "[red]●[/red]",
        "medium": "[yellow]◉[/yellow]",
        "low": "[dim]○[/dim]",
    }.get(priority or "", "")


def _due_style(due: Optional[date], state: Optional[str]) -> str:
    if state == "done" or due is None:
        return "dim"
    delta = (due - date.today()).days
    if delta < 0:
        return "red bold"
    if delta <= 3:
        return "yellow"
    return "green"


def _resolve(ref: str) -> str:
    try:
        return store.resolve_task_id(ref)
    except FileNotFoundError as e:
        err_console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)


# ── Commands ───────────────────────────────────────────────────────────────────

@app.command()
def add(
    parent: str = typer.Argument(..., help="Parent page path or '.' for root"),
    name: str = typer.Argument(..., help="Page name"),
    priority: Optional[str] = typer.Option(None, "--priority", "-p", help="high|medium|low"),
    due: Optional[str] = typer.Option(None, "--due", "-d", help="Due date YYYY-MM-DD"),
    folder: bool = typer.Option(False, "--folder", "-f", help="Create as folder-page"),
):
    """Create a new page under a parent (use '.' for root)."""
    if priority and priority not in VALID_PRIORITIES:
        err_console.print(f"[red]Invalid priority. Choose: {', '.join(VALID_PRIORITIES)}[/red]")
        raise typer.Exit(1)
    due_date = date.fromisoformat(due) if due else None
    parent_path = None if parent == "." else parent
    try:
        page = store.create_page(
            parent_path, name,
            priority=priority, due=due_date, as_folder=folder,
        )
        console.print(f"[green]Created[/green] [{page.short_id}] {page.path}")
    except Exception as e:
        err_console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)


@app.command(name="list")
def list_pages(
    parent: Optional[str] = typer.Argument(None, help="Parent page path or omit for root"),
    state: str = typer.Option("all", "--state", "-s", help="all | undone | done"),
    high: str = typer.Option("all", "--high", help="Due window for high priority"),
    medium: str = typer.Option("all", "--medium", help="Due window for medium priority"),
    low: str = typer.Option("none", "--low", help="Due window for low priority"),
):
    """List pages under a parent (or all pages)."""
    pages = store.list_tasks(parent.split("/")[0] if parent else None)
    pages = store.filter_tasks(pages, state=state,
                               by_priority={"high": high, "medium": medium, "low": low})
    if not pages:
        console.print("[dim]No pages found.[/dim]")
        return

    table = Table(box=box.ROUNDED, show_header=True, header_style="bold cyan")
    table.add_column("#", style="dim", no_wrap=True, width=6)
    table.add_column("Name", min_width=24)
    table.add_column("Path", style="dim")
    table.add_column("State", no_wrap=True)
    table.add_column("Pri", no_wrap=True, justify="center")
    table.add_column("Due", no_wrap=True)

    for p in pages:
        due_str = p.due.isoformat() if p.due else ""
        table.add_row(
            p.short_id,
            f"{'[dim]' if p.state == 'done' else ''}{p.name}{'[/dim]' if p.state == 'done' else ''}",
            p.path,
            f"[{_state_style(p.state)}]{p.state or ''}[/{_state_style(p.state)}]",
            _priority_dot(p.priority),
            f"[{_due_style(p.due, p.state)}]{due_str}[/{_due_style(p.due, p.state)}]",
        )

    console.print(table)


@app.command()
def show(page_ref: str = typer.Argument(..., help="Page path or short hex",
                                        autocompletion=_complete_page)):
    """Show full page details."""
    path = _resolve(page_ref)
    try:
        p = store.get_page(path)
    except FileNotFoundError as e:
        err_console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)

    console.print(f"[bold]{p.name}[/bold]  [dim][{p.short_id}][/dim]")
    console.print(f"  Path:     {p.path}")
    if p.parent_path:
        console.print(f"  Parent:   {p.parent_path}")
    if p.state:
        console.print(f"  State:    [{_state_style(p.state)}]{p.state}[/{_state_style(p.state)}]")
    if p.priority:
        console.print(f"  Priority: {_priority_dot(p.priority)} {p.priority}")
    if p.due:
        console.print(f"  Due:      [{_due_style(p.due, p.state)}]{p.due}[/{_due_style(p.due, p.state)}]")
    console.print(f"  Created:  {p.created}")
    if p.children:
        console.print(f"  Children: {len(p.children)}")
    if p.content.strip():
        console.print("\n[bold]Content:[/bold]")
        console.print(p.content)


@app.command()
def state(
    page_ref: str = typer.Argument(..., help="Page path or short hex",
                                   autocompletion=_complete_page),
    new_state: str = typer.Argument(..., help="New state", autocompletion=_complete_state),
):
    """Update page state."""
    path = _resolve(page_ref)
    if new_state not in VALID_STATES:
        err_console.print(f"[red]Invalid state. Choose: {', '.join(VALID_STATES)}[/red]")
        raise typer.Exit(1)
    try:
        p = store.update_page(path, state=new_state)
        console.print(f"[green]Updated[/green] [{p.short_id}] → {new_state}")
    except FileNotFoundError as e:
        err_console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)


@app.command()
def done(page_ref: str = typer.Argument(..., help="Page path or short hex",
                                        autocompletion=_complete_page)):
    """Mark a page as done."""
    path = _resolve(page_ref)
    try:
        p = store.update_page(path, state="done")
        console.print(f"[green]Done[/green] [{p.short_id}] {p.name}")
    except FileNotFoundError as e:
        err_console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)


@app.command()
def mv(
    page_ref: str = typer.Argument(..., help="Page path or short hex",
                                   autocompletion=_complete_page),
    new_parent: str = typer.Argument(..., help="New parent path or '.' for root"),
):
    """Move a page under a new parent."""
    path = _resolve(page_ref)
    parent = None if new_parent == "." else new_parent
    try:
        p = store.move_page(path, parent)
        console.print(f"[green]Moved[/green] [{p.short_id}] → {p.path}")
    except (FileNotFoundError, FileExistsError) as e:
        err_console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)


@app.command()
def note(
    page_ref: str = typer.Argument(..., help="Page path or short hex",
                                   autocompletion=_complete_page),
    text: str = typer.Argument(..., help="Note text to append"),
):
    """Append a timestamped note to a page."""
    path = _resolve(page_ref)
    try:
        store.append_to_page(path, text)
        console.print(f"[green]Note added[/green] to {path}")
    except FileNotFoundError as e:
        err_console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)


@app.command()
def search(query: str = typer.Argument(..., help="Search query")):
    """Full-text search across all pages."""
    results = store.search_pages(query)
    if not results:
        console.print("[dim]No results found.[/dim]")
        return
    for r in results:
        console.print(f"[cyan]{r['path']}[/cyan]  [bold]{r['name']}[/bold]")
        console.print(f"  [dim]{r['snippet']}[/dim]")
        console.print()


@app.command()
def edit(page_ref: str = typer.Argument(..., help="Page path or short hex",
                                        autocompletion=_complete_page)):
    """Open a page in $EDITOR."""
    import os
    path = _resolve(page_ref)
    try:
        store.get_page(path)
    except FileNotFoundError as e:
        err_console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    from .config import TASKS_ROOT
    abs_path = TASKS_ROOT / path
    if abs_path.is_dir():
        abs_path = abs_path / "index.md"
    editor = os.environ.get("EDITOR", "vim")
    subprocess.run([editor, str(abs_path)])


@app.command()
def rm(
    page_ref: str = typer.Argument(..., help="Page path or short hex",
                                   autocompletion=_complete_page),
    force: bool = typer.Option(False, "--force", "-f", help="Skip confirmation"),
):
    """Delete a page (soft-delete to .deleted/)."""
    path = _resolve(page_ref)
    if not force:
        confirmed = typer.confirm(f"Delete page '{path}'?")
        if not confirmed:
            raise typer.Exit(0)
    try:
        store.delete_page(path)
        console.print(f"[red]Deleted[/red] {path}")
    except FileNotFoundError as e:
        err_console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)


@app.command()
def tui():
    """Launch the interactive TUI."""
    from .tui import main as tui_main
    tui_main()


@app.command()
def serve(
    config: Optional[str] = typer.Option(None, "--config", "-c",
                                         help="Path to config file (overrides auto-discovery)"),
    host: Optional[str] = typer.Option(None, "--host", help="Bind address"),
    port: Optional[int] = typer.Option(None, "--port", "-p", help="Port"),
    reload: bool = typer.Option(False, "--reload", help="Enable hot-reload (dev mode)"),
):
    """Start the API + web server."""
    import os
    env = os.environ.copy()
    if config:
        env["TASKS_CONFIG"] = str(Path(config).resolve())

    h = host or SERVER_HOST
    p = port or SERVER_PORT

    console.print(f"Serving on http://{h}:{p}")

    cmd = [
        sys.executable, "-m", "uvicorn", "tasks.api:app",
        "--host", h, "--port", str(p),
    ]
    if reload:
        cmd += ["--reload", "--reload-dir", "tasks"]

    subprocess.run(cmd, env=env)
