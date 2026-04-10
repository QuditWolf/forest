"""Textual TUI — full interactive interface."""
from __future__ import annotations

from datetime import date
from typing import Optional

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    Select,
    Static,
    TextArea,
)

from . import store
from .config import VALID_PRIORITIES, VALID_STATES
from .models import Page as Task


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _state_icon(state: str) -> str:
    return {
        "todo": "○",
        "in-progress": "◐",
        "blocked": "✗",
        "waiting": "⏸",
        "done": "✓",
    }.get(state, "?")


def _priority_dot(priority: str) -> str:
    return {"high": "●", "medium": "◉", "low": "○"}.get(priority, "○")


def _task_label(t: Task, show_category: bool = False) -> str:
    icon = _state_icon(t.state)
    dot = _priority_dot(t.priority)
    max_name = 22 if show_category else 30
    name = t.name[:max_name] + "…" if len(t.name) > max_name else t.name
    cat_str = f"[dim]{t.category[:10]:<10}[/dim] " if show_category else ""
    due_str = f" [{t.due}]" if t.due else ""
    return f"{icon} {cat_str}{name:<32} {t.priority[:3]:3} {dot}{due_str}"


# ---------------------------------------------------------------------------
# Modal screens
# ---------------------------------------------------------------------------

class AddTaskModal(ModalScreen):
    """Modal form to add a new task."""

    def __init__(self, category: str):
        super().__init__()
        self.category = category

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-box"):
            yield Label(f"Add task to [bold]{self.category}[/bold]", id="modal-title")
            yield Label("Name:")
            yield Input(placeholder="Task name", id="task-name")
            yield Label("Priority:")
            yield Select(
                [(p, p) for p in VALID_PRIORITIES],
                value="medium",
                id="task-priority",
            )
            yield Label("Due date (YYYY-MM-DD, optional):")
            yield Input(placeholder="2026-03-20", id="task-due")
            with Horizontal(id="modal-buttons"):
                yield Button("Add", variant="primary", id="btn-add")
                yield Button("Cancel", id="btn-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-cancel":
            self.dismiss(None)
            return

        name_input = self.query_one("#task-name", Input)
        priority_select = self.query_one("#task-priority", Select)
        due_input = self.query_one("#task-due", Input)

        name = name_input.value.strip()
        if not name:
            return

        priority = str(priority_select.value) if priority_select.value else "medium"
        due_str = due_input.value.strip()
        due = None
        if due_str:
            try:
                due = date.fromisoformat(due_str)
            except ValueError:
                pass

        try:
            task = store.create_task(self.category, name, priority=priority, due=due)
            self.dismiss(task)
        except Exception as e:
            self.dismiss(None)


class StatePickerModal(ModalScreen):
    """Pick a new state for a task."""

    def __init__(self, task: Task):
        super().__init__()
        self._task = task

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-box"):
            yield Label(f"Set state for [bold]{self._task.name}[/bold]", id="modal-title")
            for s in VALID_STATES:
                icon = _state_icon(s)
                yield Button(f"{icon} {s}", id=f"state-{s}")
            yield Button("Cancel", id="btn-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-cancel":
            self.dismiss(None)
            return
        state = event.button.id.removeprefix("state-")
        if state in VALID_STATES:
            self.dismiss(state)


class CategoryPickerModal(ModalScreen):
    """Pick a category to move a task to."""

    def __init__(self, categories: list[str]):
        super().__init__()
        self.categories = categories

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-box"):
            yield Label("Move to category:", id="modal-title")
            for cat in self.categories:
                yield Button(cat, id=f"cat-{cat}")
            yield Button("Cancel", id="btn-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-cancel":
            self.dismiss(None)
            return
        cat = event.button.id.removeprefix("cat-")
        self.dismiss(cat)


class NoteModal(ModalScreen):
    """Inline note input."""

    def __init__(self, task: Task):
        super().__init__()
        self._task = task

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-box"):
            yield Label(f"Add note to [bold]{self._task.name}[/bold]", id="modal-title")
            yield Input(placeholder="Note text…", id="note-input")
            with Horizontal(id="modal-buttons"):
                yield Button("Add", variant="primary", id="btn-add")
                yield Button("Cancel", id="btn-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-cancel":
            self.dismiss(None)
            return
        text = self.query_one("#note-input", Input).value.strip()
        if text:
            self.dismiss(text)
        else:
            self.dismiss(None)


class ConfirmModal(ModalScreen):
    """Simple yes/no confirmation."""

    def __init__(self, message: str):
        super().__init__()
        self.message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-box"):
            yield Label(self.message, id="modal-title")
            with Horizontal(id="modal-buttons"):
                yield Button("Yes", variant="error", id="btn-yes")
                yield Button("No", id="btn-no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "btn-yes")


class FilterModal(ModalScreen):
    """Edit state filter and per-priority due windows."""

    def __init__(self, filter_state: str, by_priority: dict):
        super().__init__()
        self._filter_state = filter_state
        self._by_priority  = dict(by_priority)

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-box"):
            yield Label("Filters", id="modal-title")
            yield Label("Show:")
            yield Select(
                [("all", "all"), ("undone", "undone"), ("done", "done")],
                value=self._filter_state,
                id="flt-state",
            )
            yield Label("Due window per priority  (Xd / Xw / Xm / Xy / none / all):")
            for pri in ("high", "medium", "low"):
                with Horizontal():
                    yield Label(f"  {pri:<8}", markup=False)
                    yield Input(value=self._by_priority.get(pri, "all"), id=f"flt-{pri}")
            with Horizontal(id="modal-buttons"):
                yield Button("Apply", variant="primary", id="btn-apply")
                yield Button("Cancel", id="btn-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-cancel":
            self.dismiss(None)
            return
        state = str(self.query_one("#flt-state", Select).value)
        by_pri = {}
        for pri in ("high", "medium", "low"):
            by_pri[pri] = self.query_one(f"#flt-{pri}", Input).value.strip() or "all"
        self.dismiss((state, by_pri))


class HelpModal(ModalScreen):
    """Key binding reference."""

    BINDINGS = [Binding("escape", "dismiss_help", "Close", priority=True),
                Binding("ctrl+h", "dismiss_help", "Close", priority=True)]

    def action_dismiss_help(self) -> None:
        self.dismiss()

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-box"):
            yield Label("Key Bindings", id="modal-title")
            yield Static("""\
[bold]Navigation[/bold]
  ↑ / ↓        move in list
  Tab          switch panel focus

[bold]Tasks[/bold]
  a            add task  (scratchpad if in All view)
  e            edit in $EDITOR
  s            set state
  m            move to category
  n            add note
  d            delete  (→ .deleted/)

[bold]Filters[/bold]
  f            cycle state  (all → undone → done)
  F            open filter editor (due windows per priority)

[bold]General[/bold]
  r            refresh from disk
  ctrl+h / ?   show this help
  q / Escape   quit / close""")
            yield Button("Close", id="btn-close")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss()


# ---------------------------------------------------------------------------
# Main TUI App
# ---------------------------------------------------------------------------

TUI_CSS = """
Screen {
    layout: horizontal;
}

#categories-panel {
    width: 22;
    border: round $primary;
    padding: 0 1;
}

#tasks-panel {
    width: 1fr;
    border: round $primary;
    padding: 0 1;
}

#detail-panel {
    width: 36;
    border: round $primary;
    padding: 1;
}

.panel-title {
    text-align: center;
    background: $primary;
    color: $background;
    margin-bottom: 1;
}

#detail-content {
    height: 1fr;
    overflow-y: auto;
}

/* Modal */
AddTaskModal, StatePickerModal, CategoryPickerModal, NoteModal, ConfirmModal {
    align: center middle;
}

#modal-box {
    background: $surface;
    border: round $primary;
    padding: 1 2;
    width: 50;
    max-height: 30;
}

#modal-title {
    text-align: center;
    margin-bottom: 1;
    text-style: bold;
}

#modal-buttons {
    margin-top: 1;
    align: center middle;
}

#modal-buttons Button {
    margin: 0 1;
}

/* Task state colors */
.state-done { color: $text-muted; }
.state-blocked { color: $error; }
.state-in-progress { color: $accent; }
.state-waiting { color: $warning; }

#help-hint {
    dock: bottom;
    text-align: right;
    padding: 0 1;
    color: $text-muted;
}

/* Help modal — wider to fit content */
HelpModal #modal-box {
    width: 52;
    max-height: 40;
}
"""


class TasksApp(App):
    CSS = TUI_CSS
    TITLE = "Tasks"
    BINDINGS = [
        Binding("q", "quit", "Quit", priority=True),
        Binding("a", "add_task", "Add", priority=True),
        Binding("d", "delete_task", "Delete", priority=True),
        Binding("s", "set_state", "State", priority=True),
        Binding("m", "move_task", "Move", priority=True),
        Binding("n", "add_note", "Note", priority=True),
        Binding("e", "edit_task", "Edit", priority=True),
        Binding("r", "refresh", "Refresh", priority=True),
        Binding("f", "cycle_filter", "Filter", priority=True),
        Binding("F", "edit_filters", "Filters…", priority=True),
        Binding("ctrl+h", "show_help", "Help", priority=True),
        Binding("question_mark", "show_help", "Help", show=False, priority=True),
    ]

    # Filter state
    _filter_state: str = "all"          # 'all' | 'undone' | 'done'
    _filter_by_pri: dict = {"high": "all", "medium": "2w", "low": "none"}

    def on_key(self, event) -> None:
        """Belt-and-suspenders handler — fires if BINDINGS don't catch the key."""
        if len(self.screen_stack) > 1:
            return
        key_map = {
            "a":      self.action_add_task,
            "d":      self.action_delete_task,
            "s":      self.action_set_state,
            "m":      self.action_move_task,
            "n":      self.action_add_note,
            "e":      self.action_edit_task,
            "r":      self.action_refresh,
            "f":      self.action_cycle_filter,
            "F":      self.action_edit_filters,
            "ctrl+h": self.action_show_help,
            "?":      self.action_show_help,
        }
        if event.key in key_map:
            event.stop()
            key_map[event.key]()

    selected_category: reactive[Optional[str]] = reactive(None)
    selected_task: reactive[Optional[Task]] = reactive(None)
    categories: reactive[list] = reactive(list)
    tasks: reactive[list] = reactive(list)

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            with Vertical(id="categories-panel"):
                yield Label("Categories", classes="panel-title")
                yield ListView(id="cat-list")
            with Vertical(id="tasks-panel"):
                yield Label("Tasks", classes="panel-title")
                yield ListView(id="task-list")
            with Vertical(id="detail-panel"):
                yield Label("Detail", classes="panel-title")
                yield Static("", id="detail-content")
                yield Static("[dim]ctrl+h  help[/dim]", id="help-hint")
        yield Footer()

    def on_mount(self) -> None:
        self._load_categories()

    def _load_categories(self) -> None:
        self.categories = store.list_categories()
        cat_list = self.query_one("#cat-list", ListView)
        cat_list.clear()
        total = sum(c.task_count for c in self.categories)
        cat_list.append(ListItem(Label(f"[bold]All ({total})[/bold]")))
        for cat in self.categories:
            cat_list.append(ListItem(Label(f"{cat.name} ({cat.task_count})")))

        # Auto-select "All" on first load
        if self.selected_category is None and not self.tasks:
            self._load_tasks()

    def _load_tasks(self) -> None:
        all_tasks = store.list_tasks(self.selected_category)
        visible   = store.filter_tasks(
            all_tasks,
            state=self._filter_state,
            by_priority=self._filter_by_pri,
        )

        task_list = self.query_one("#task-list", ListView)
        task_list.clear()
        self._lv_map: list[Task | None] = []

        # Update panel title to show active filters
        pri_summary = "  ".join(
            f"{p[:3]}:{v}" for p, v in self._filter_by_pri.items()
        )
        tasks_panel = self.query_one("#tasks-panel", Vertical)
        title_label = tasks_panel.query_one(".panel-title", Label)
        title_label.update(
            f"Tasks  [dim]{self._filter_state}  {pri_summary}[/dim]"
        )

        if self.selected_category is not None:
            self.tasks = visible
            for t in visible:
                task_list.append(ListItem(Label(_task_label(t))))
                self._lv_map.append(t)
        else:
            self.tasks = visible
            by_cat: dict[str, list[Task]] = {}
            for t in visible:
                by_cat.setdefault(t.category, []).append(t)
            for cat, tasks in by_cat.items():
                heading = ListItem(Label(f"[bold cyan] {cat.upper()} [/bold cyan]"))
                heading.disabled = True
                task_list.append(heading)
                self._lv_map.append(None)
                for t in tasks:
                    task_list.append(ListItem(Label(_task_label(t))))
                    self._lv_map.append(t)

        self.selected_task = None
        self._update_detail()

    def _update_detail(self) -> None:
        detail = self.query_one("#detail-content", Static)
        t = self.selected_task
        if t is None:
            detail.update("")
            return

        today = date.today()
        due_info = ""
        if t.due:
            delta = (t.due - today).days
            if t.state != "done" and delta < 0:
                due_info = f" [red](overdue {-delta}d)[/red]"
            elif delta == 0:
                due_info = " [yellow](today)[/yellow]"
            elif delta <= 3:
                due_info = f" [yellow](in {delta}d)[/yellow]"

        lines = [
            f"[bold]{t.name}[/bold]",
            f"state: [{_state_style(t.state)}]{t.state}[/{_state_style(t.state)}]",
            f"priority: {_priority_dot(t.priority)} {t.priority}",
        ]
        if t.due:
            lines.append(f"due: {t.due}{due_info}")
        lines.append(f"created: {t.created}")
        if t.subtasks:
            lines.append(f"subtasks: {', '.join(t.subtasks)}")
        if t.content.strip():
            lines.append("─" * 30)
            lines.append("Notes:")
            lines.append(t.content.strip())

        detail.update("\n".join(lines))

    def _set_cat_from_index(self, idx: int) -> None:
        if idx == 0:
            self.selected_category = None
        elif idx - 1 < len(self.categories):
            self.selected_category = self.categories[idx - 1].name

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        list_id = event.list_view.id
        idx = event.list_view.index

        if list_id == "cat-list" and idx is not None:
            self._set_cat_from_index(idx)
            self._load_tasks()

        elif list_id == "task-list" and idx is not None:
            lv_map = getattr(self, "_lv_map", [])
            task = lv_map[idx] if idx < len(lv_map) else None
            if task is not None:
                self.selected_task = task
                self._update_detail()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        # Enter on category list switches panel focus to tasks
        if event.list_view.id == "cat-list":
            self.query_one("#task-list", ListView).focus()

    def action_add_task(self) -> None:
        target = self.selected_category if self.selected_category is not None else "scratchpad"

        def on_result(task):
            if task:
                self._load_tasks()
                self._load_categories()

        self.push_screen(AddTaskModal(target), on_result)

    def action_delete_task(self) -> None:
        t = self.selected_task
        if not t:
            return

        def on_confirm(confirmed):
            if confirmed:
                store.delete_task(t.id)
                self._load_tasks()
                self._load_categories()

        self.push_screen(ConfirmModal(f"Delete '{t.name}'?"), on_confirm)

    def action_set_state(self) -> None:
        t = self.selected_task
        if not t:
            return

        def on_state(new_state):
            if new_state:
                store.update_task(t.id, state=new_state)
                self._load_tasks()
                self._load_categories()

        self.push_screen(StatePickerModal(t), on_state)

    def action_move_task(self) -> None:
        t = self.selected_task
        if not t:
            return
        cats = [c.name for c in self.categories if c.name != t.category]

        def on_cat(new_cat):
            if new_cat:
                store.move_task(t.id, new_cat)
                self._load_tasks()
                self._load_categories()

        self.push_screen(CategoryPickerModal(cats), on_cat)

    def action_add_note(self) -> None:
        t = self.selected_task
        if not t:
            return

        def on_note(text):
            if text:
                store.append_note(t.id, text)
                self.selected_task = store.get_task(t.id)
                self._update_detail()

        self.push_screen(NoteModal(t), on_note)

    def action_edit_task(self) -> None:
        t = self.selected_task
        if not t:
            return
        import os, subprocess
        from .config import TASKS_ROOT
        path = TASKS_ROOT / t.id
        if path.is_dir():
            path = path / "index.md"
        editor = os.environ.get("EDITOR", "vim")
        with self.suspend():
            subprocess.run([editor, str(path)])
        self.selected_task = store.get_task(t.id)
        self._update_detail()

    def action_cycle_filter(self) -> None:
        """Cycle state filter: all → undone → done → all."""
        cycle = {"all": "undone", "undone": "done", "done": "all"}
        self._filter_state = cycle.get(self._filter_state, "all")
        self._load_tasks()

    def action_edit_filters(self) -> None:
        """Open filter editor modal."""
        def on_result(result):
            if result:
                self._filter_state, self._filter_by_pri = result
                self._load_tasks()

        self.push_screen(FilterModal(self._filter_state, self._filter_by_pri), on_result)

    def action_show_help(self) -> None:
        self.push_screen(HelpModal())

    def action_refresh(self) -> None:
        self._load_categories()
        self._load_tasks()


def _state_style(state: str) -> str:
    return {
        "todo": "white",
        "in-progress": "cyan bold",
        "blocked": "red bold",
        "waiting": "yellow",
        "done": "dim",
    }.get(state, "white")


def main():
    app = TasksApp()
    app.run()


if __name__ == "__main__":
    main()
