"""Textual TUI - remote client for a Forest server (same server the web UI and MCP use).

Configure once:  forest login --url https://tools.example.org --token fst_...
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from rich.markup import escape
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (Button, Checkbox, ContentSwitcher, Footer, Header, Input, Label, Markdown,
                             OptionList, Select, Static, Tree)
from textual.widgets.option_list import Option

from .client import ClientError, Forest

STATES = ["todo", "in-progress", "blocked", "waiting", "done"]
PRIORITIES = ["high", "medium", "low"]
SYM = {"todo": "○", "in-progress": "◐", "blocked": "✗", "waiting": "⏸", "done": "✓"}
PSYM = {"high": "●", "medium": "◉", "low": "○"}
WIKI_RE = re.compile(r"\[\[([^\]\|#]+)(?:#[^\]\|]*)?(?:\|([^\]]*))?\]\]")


def _wikify(md: str) -> str:
    """[[ref|alias]] → [alias](wiki:ref) so links are clickable in the Markdown widget."""
    def rep(m):
        ref = m.group(1).strip()
        label = (m.group(2) or ref.split(":")[-1].split("/")[-1]).replace("-", " ")
        return f"[{label}](wiki:{ref.replace(' ', '%20')})"
    return WIKI_RE.sub(rep, md)


# ── Modals ────────────────────────────────────────────────────────────────────

class InputModal(ModalScreen[Optional[str]]):
    BINDINGS = [("escape", "cancel", "cancel")]

    def __init__(self, prompt: str, value: str = "", placeholder: str = ""):
        super().__init__()
        self.prompt, self.value, self.placeholder = prompt, value, placeholder

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal"):
            yield Label(self.prompt)
            yield Input(value=self.value, placeholder=self.placeholder, id="inp")

    @on(Input.Submitted)
    def submit(self, e: Input.Submitted):
        self.dismiss(e.value)

    def action_cancel(self):
        self.dismiss(None)


class ConfirmModal(ModalScreen[bool]):
    BINDINGS = [("escape", "no", "no"), ("y", "yes", "yes"), ("n", "no", "no")]

    def __init__(self, prompt: str):
        super().__init__()
        self.prompt = prompt

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal"):
            yield Label(self.prompt)
            with Horizontal(classes="buttons"):
                yield Button("yes (y)", id="yes", variant="error")
                yield Button("no (n)", id="no")

    @on(Button.Pressed)
    def pressed(self, e: Button.Pressed):
        self.dismiss(e.button.id == "yes")

    def action_yes(self):
        self.dismiss(True)

    def action_no(self):
        self.dismiss(False)


class NewPageModal(ModalScreen[Optional[dict]]):
    BINDINGS = [("escape", "cancel", "cancel")]

    def __init__(self, title: str, task_defaults: bool = False):
        super().__init__()
        self.title_text, self.task_defaults = title, task_defaults

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal"):
            yield Label(self.title_text)
            yield Input(placeholder="name", id="name")
            with Horizontal(classes="row"):
                yield Select([(s, s) for s in STATES], prompt="state", id="state",
                             value="todo" if self.task_defaults else Select.BLANK)
                yield Select([(p, p) for p in PRIORITIES], prompt="priority", id="priority",
                             value="medium" if self.task_defaults else Select.BLANK)
            yield Input(placeholder="due YYYY-MM-DD (optional)", id="due")
            yield Input(placeholder="tags, comma separated (optional)", id="tags")
            yield Checkbox("folder (can hold sub-pages)", id="folder")
            with Horizontal(classes="buttons"):
                yield Button("create", id="create", variant="primary")
                yield Button("cancel", id="cancel")

    def _collect(self):
        name = self.query_one("#name", Input).value.strip()
        if not name:
            self.query_one("#name", Input).focus()
            return
        st = self.query_one("#state", Select).value
        pr = self.query_one("#priority", Select).value
        tags = [t.strip() for t in self.query_one("#tags", Input).value.split(",") if t.strip()]
        self.dismiss({
            "name": name,
            "state": None if st == Select.BLANK else st,
            "priority": None if pr == Select.BLANK else pr,
            "due": self.query_one("#due", Input).value.strip() or None,
            "tags": tags or None,
            "as_folder": self.query_one("#folder", Checkbox).value,
        })

    @on(Input.Submitted)
    def submitted(self):
        self._collect()

    @on(Button.Pressed)
    def pressed(self, e: Button.Pressed):
        if e.button.id == "create":
            self._collect()
        else:
            self.dismiss(None)

    def action_cancel(self):
        self.dismiss(None)


class SearchModal(ModalScreen[Optional[tuple]]):
    """Live full-text search across all vaults. Enter opens the highlighted result."""
    BINDINGS = [("escape", "cancel", "cancel"), ("down", "down", "down"), ("up", "up", "up")]

    def __init__(self, client: Forest):
        super().__init__()
        self.client = client
        self.results: list = []

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal wide"):
            yield Label("search all vaults")
            yield Input(placeholder="query…", id="q")
            yield OptionList(id="res")

    @on(Input.Changed)
    def changed(self, e: Input.Changed):
        ol = self.query_one("#res", OptionList)
        ol.clear_options()
        if len(e.value.strip()) < 2:
            return
        try:
            self.results = self.client.search(e.value)[:40]
        except ClientError as ex:
            self.notify(str(ex), severity="error")
            return
        for r in self.results:
            ol.add_option(Option(f"{escape(r['name'])}  [dim]{r['vault']}:{r['path']}[/dim]\n  [dim]{escape(r['snippet'][:90])}[/dim]"))
        if self.results:
            ol.highlighted = 0

    @on(Input.Submitted)
    def submitted(self):
        ol = self.query_one("#res", OptionList)
        if self.results and ol.highlighted is not None:
            r = self.results[ol.highlighted]
            self.dismiss((r["vault"], r["path"]))

    @on(OptionList.OptionSelected)
    def selected(self, e: OptionList.OptionSelected):
        r = self.results[e.option_index]
        self.dismiss((r["vault"], r["path"]))

    def action_down(self):
        self.query_one("#res", OptionList).action_cursor_down()

    def action_up(self):
        self.query_one("#res", OptionList).action_cursor_up()

    def action_cancel(self):
        self.dismiss(None)


# ── App ───────────────────────────────────────────────────────────────────────

class ForestTUI(App):
    TITLE = "forest"
    CSS = """
    #left { width: 38; border-right: solid $panel-lighten-2; }
    #tree, #agenda { height: 1fr; }
    #meta { padding: 0 1; height: auto; background: $boost; }
    #links { padding: 0 1; height: auto; color: $text-muted; border-top: solid $panel-lighten-2; }
    #bodyscroll { height: 1fr; padding: 0 1; }
    .modal { width: 64; height: auto; max-height: 80%; padding: 1 2; border: thick $accent; background: $surface; }
    .modal.wide { width: 90; }
    ModalScreen { align: center middle; }
    .row { height: auto; }
    .row Select { width: 1fr; }
    .buttons { height: auto; margin-top: 1; }
    .buttons Button { margin-right: 1; }
    #res { height: auto; max-height: 24; }
    """
    BINDINGS = [
        Binding("q", "quit", "quit"),
        Binding("slash", "search", "search"),
        Binding("v", "switch_vault", "vault"),
        Binding("t", "toggle_agenda", "agenda"),
        Binding("n", "new_child", "new sub"),
        Binding("N", "new_root", "new root", show=False),
        Binding("e", "edit", "edit"),
        Binding("a", "append", "note"),
        Binding("s", "cycle_state", "state"),
        Binding("p", "cycle_priority", "prio"),
        Binding("x", "toggle_done", "done"),
        Binding("d", "set_due", "due"),
        Binding("m", "move", "move", show=False),
        Binding("D", "delete", "delete", show=False),
        Binding("H", "history", "history", show=False),
        Binding("r", "refresh", "refresh"),
        Binding("T", "today", "today"),
        Binding("y", "copy_link", "copy link", show=False),
        Binding("c", "capture", "capture", show=False),
    ]

    def __init__(self):
        super().__init__()
        self.client = Forest()
        self.vaults: list[str] = []
        self.vault = self.client.default_vault
        self.page: Optional[dict] = None
        self.agenda_rows: list = []

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            with Vertical(id="left"):
                with ContentSwitcher(initial="tree"):
                    yield Tree("pages", id="tree")
                    yield OptionList(id="agenda")
            with Vertical():
                yield Static("select a page (/ to search, t for agenda)", id="meta")
                with VerticalScroll(id="bodyscroll"):
                    yield Markdown("", id="body")
                yield Static("", id="links")
        yield Footer()

    def on_mount(self):
        try:
            me = self.client.me()
        except ClientError as e:
            self.exit(message=f"forest: {e}\nConfigure with: forest login --url URL --token TOKEN")
            return
        self.vaults = me["vaults"]
        if self.vault not in self.vaults:
            self.vault = self.vaults[0]
        self.sub_title = f"{self.client.url} · {me['name']} ({me['scope']})"
        self.load_tree()
        self.query_one("#tree", Tree).focus()

    # ── loading ──

    def _err(self, e: Exception):
        self.notify(str(e), severity="error", timeout=6)

    def load_tree(self, select: Optional[str] = None):
        tree = self.query_one("#tree", Tree)
        tree.clear()
        tree.root.label = f"[b]{self.vault}[/b]"
        tree.root.expand()
        try:
            nodes = self.client.tree(self.vault)
        except ClientError as e:
            return self._err(e)
        target = select or (self.page["path"] if self.page and self.page.get("vault") == self.vault else None)

        def add(parent, ns):
            for n in ns:
                st = SYM.get(n.get("state") or "", "")
                label = f"{st + ' ' if st else ''}{escape(n['name'])}"
                if n.get("is_folder"):
                    node = parent.add(label, data=n["path"],
                                      expand=bool(target and target.startswith(n["path"].rsplit("/", 1)[0] + "/")))
                    add(node, n.get("children", []))
                else:
                    parent.add_leaf(label, data=n["path"])
        add(tree.root, nodes)

    def open_page(self, vault: str, path: str):
        try:
            p = self.client.page(vault, path)
            bl = self.client.backlinks(vault, p["path"])
        except ClientError as e:
            return self._err(e)
        if vault != self.vault:
            self.vault = vault
            self.load_tree(select=p["path"])
        self.page = p
        self.render_page(bl)

    def render_page(self, backlinks: Optional[list] = None):
        p = self.page
        if not p:
            return
        bits = [f"[b]{escape(p['name'])}[/b]  [dim]{p['vault']}:{p['path']}  \\[{p['short_id']}][/dim]"]
        meta = []
        if p.get("state"):
            meta.append(f"{SYM.get(p['state'], '')} {p['state']}")
        if p.get("priority"):
            meta.append(f"{PSYM.get(p['priority'], '')} {p['priority']}")
        if p.get("due"):
            meta.append(f"due {p['due']}")
        tags = sorted(set(p.get("tags", [])) | set(p.get("inline_tags", [])))
        if tags:
            meta.append(escape(" ".join("#" + t for t in tags)))
        if meta:
            bits.append("  ·  ".join(meta))
        self.query_one("#meta", Static).update("\n".join(bits))
        self.query_one("#body", Markdown).update(_wikify(p["content"]) or "_empty page, press e to edit_")
        children = ", ".join(c.rsplit("/", 2)[-2] if c.endswith("index.md") else c.rsplit("/", 1)[-1][:-3]
                             for c in p.get("children", []))
        links = f"sub-pages: {children or 'none'}"
        if backlinks is not None:
            links += f"   │   backlinks: {', '.join(backlinks) or 'none'}"
        self.query_one("#links", Static).update(escape(links))

    def reload_page(self):
        if self.page:
            self.open_page(self.page["vault"], self.page["path"])

    # ── events ──

    @on(Tree.NodeSelected, "#tree")
    def tree_selected(self, e: Tree.NodeSelected):
        if e.node.data:
            self.open_page(self.vault, e.node.data)

    @on(OptionList.OptionSelected, "#agenda")
    def agenda_selected(self, e: OptionList.OptionSelected):
        r = self.agenda_rows[e.option_index]
        self.open_page(r["vault"], r["path"])

    @on(Markdown.LinkClicked)
    def link_clicked(self, e: Markdown.LinkClicked):
        href = e.href
        if href.startswith("wiki:") and self.page:
            ref = href[5:].replace("%20", " ")
            try:
                r = self.client.resolve(ref, self.page["vault"], self.page["path"])
                self.open_page(r["vault"], r["path"])
            except ClientError:
                self.notify(f"page not found: {ref}", severity="warning")
        elif href.startswith("http"):
            self.open_url(href)

    # ── actions ──

    def action_search(self):
        def done(res):
            if res:
                self.open_page(*res)
        self.push_screen(SearchModal(self.client), done)

    def action_switch_vault(self):
        if not self.vaults:
            return
        self.vault = self.vaults[(self.vaults.index(self.vault) + 1) % len(self.vaults)]
        self.load_tree()
        self.notify(f"vault: {self.vault}")

    def action_toggle_agenda(self):
        sw = self.query_one(ContentSwitcher)
        if sw.current == "agenda":
            sw.current = "tree"
            self.query_one("#tree", Tree).focus()
            return
        try:
            rows = self.client.agenda(state="undone")
        except ClientError as e:
            return self._err(e)
        self.agenda_rows = rows
        ol = self.query_one("#agenda", OptionList)
        ol.clear_options()
        from datetime import date
        today = date.today().isoformat()
        for r in rows:
            due = r.get("due") or ""
            style = "red" if due and due < today else "yellow" if due and due <= today else "dim"
            ol.add_option(Option(f"{SYM.get(r['state'], '?')} {PSYM.get(r.get('priority') or '', ' ')} "
                                 f"{escape(r['name'][:22]):<22} [{style}]{due}[/{style}]\n   [dim]{r['vault']}:{r['path']}[/dim]"))
        sw.current = "agenda"
        ol.focus()

    def _need_page(self) -> bool:
        if not self.page:
            self.notify("open a page first", severity="warning")
            return False
        return True

    def _update(self, **fields):
        try:
            self.page = self.client.update(self.page["vault"], self.page["path"], **fields)
            self.render_page()
            self.load_tree()
        except ClientError as e:
            self._err(e)

    def action_cycle_state(self):
        if self._need_page():
            cur = self.page.get("state")
            nxt = STATES[(STATES.index(cur) + 1) % len(STATES)] if cur in STATES else STATES[0]
            self._update(state=nxt)

    def action_cycle_priority(self):
        if self._need_page():
            cur = self.page.get("priority")
            order = PRIORITIES + [None]
            self._update(priority=order[(order.index(cur) + 1) % len(order)] if cur in order else "high")

    def action_toggle_done(self):
        if self._need_page():
            self._update(state="todo" if self.page.get("state") == "done" else "done")

    def action_set_due(self):
        if not self._need_page():
            return

        def done(v):
            if v is None:
                return
            from .store import parse_due_window
            v = v.strip()
            if v.startswith("+"):
                d = parse_due_window(v[1:])
                v = d.isoformat() if d else ""
            self._update(due=v or None)
        self.push_screen(InputModal("due date (YYYY-MM-DD, +3d, +2w, empty = clear)", self.page.get("due") or ""), done)

    def action_append(self):
        if not self._need_page():
            return

        def done(text):
            if text:
                try:
                    self.client.append(self.page["vault"], self.page["path"], text)
                    self.reload_page()
                except ClientError as e:
                    self._err(e)
        self.push_screen(InputModal(f"append note to {self.page['name']}"), done)

    def _new(self, parent: Optional[str]):
        def done(data):
            if not data:
                return
            try:
                p = self.client.create(self.vault, parent_path=parent, **data)
                self.load_tree(select=p["path"])
                self.open_page(self.vault, p["path"])
            except ClientError as e:
                self._err(e)
        where = f"under {self.page['name']}" if parent and self.page else f"in {self.vault} root"
        self.push_screen(NewPageModal(f"new page {where}", task_defaults=self.vault == "tasks"), done)

    def action_new_child(self):
        self._new(self.page["path"] if self.page and self.page["vault"] == self.vault else None)

    def action_new_root(self):
        self._new(None)

    def action_edit(self):
        if not self._need_page():
            return
        try:
            r = self.client.raw(self.page["vault"], self.page["path"])
        except ClientError as e:
            return self._err(e)
        with tempfile.NamedTemporaryFile("w", suffix=".md", prefix="forest-", delete=False) as tmp:
            tmp.write(r["text"])
        editor = os.environ.get("EDITOR", "vim")
        with self.suspend():
            subprocess.run([*editor.split(), tmp.name])
        new = Path(tmp.name).read_text()
        if new != r["text"]:
            try:
                self.client.write_raw(self.page["vault"], r["path"], new, expected_sha=r["sha"])
                Path(tmp.name).unlink()
                self.notify("saved")
            except ClientError as e:
                self.notify(f"{e}. Your edit is kept at {tmp.name}", severity="error", timeout=15)
        else:
            Path(tmp.name).unlink()
        self.reload_page()
        self.load_tree()

    def action_move(self):
        if not self._need_page():
            return

        def done(v):
            if v is None:
                return
            try:
                p = self.client.move(self.page["vault"], self.page["path"], v.strip() or None)
                self.load_tree(select=p["path"])
                self.open_page(self.page["vault"], p["path"])
            except ClientError as e:
                self._err(e)
        self.push_screen(InputModal("move under page path (empty = root)", placeholder="projects/index.md"), done)

    def action_delete(self):
        if not self._need_page():
            return

        def done(ok):
            if ok:
                try:
                    self.client.delete(self.page["vault"], self.page["path"])
                    self.page = None
                    self.query_one("#body", Markdown).update("")
                    self.query_one("#meta", Static).update("deleted (restorable from .shadow in the web UI)")
                    self.load_tree()
                except ClientError as e:
                    self._err(e)
        self.push_screen(ConfirmModal(f"delete {self.page['name']}? (soft-delete to .shadow)"), done)

    def action_history(self):
        if not self._need_page():
            return
        try:
            h = self.client.history(self.page["vault"], self.page["path"], 30)
        except ClientError as e:
            return self._err(e)
        md = "\n".join(f"- `{e['short']}` {e['date'][:16]} **{e['author']}** - {e['message']}" for e in h)
        self.query_one("#body", Markdown).update(f"## history\n\n{md or '_no history_'}\n\n_press r to go back_")

    def action_today(self):
        try:
            p = self.client._req("GET", "/api/daily")
        except ClientError as e:
            return self._err(e)
        self.open_page(p["vault"], p["path"])

    def action_copy_link(self):
        """Copy the page's web link (OSC 52 clipboard; works over SSH in most terminals)."""
        if self._need_page():
            url = f"{self.client.url}/#{self.page['vault']}/{self.page['path']}"
            self.copy_to_clipboard(url)
            self.notify(f"copied {url}")

    def action_capture(self):
        def done(text):
            if text:
                try:
                    p = self.client._req("POST", "/api/capture", json={"text": text})
                    self.notify(f"captured to {p['vault']}:{p['path']}")
                except ClientError as e:
                    self._err(e)
        self.push_screen(InputModal("capture to inbox"), done)

    def action_refresh(self):
        self.load_tree()
        self.reload_page()


def main():
    ForestTUI().run()


if __name__ == "__main__":
    main()
