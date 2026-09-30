"""CLI and TUI against the running server (remote-client perspective)."""

import asyncio
import os
import subprocess
import sys

import pytest

from conftest import ROOT


@pytest.fixture(scope="module")
def cli(server, wtok, tmp_path_factory):
    cfg = tmp_path_factory.mktemp("clicfg")
    env = {**os.environ, "XDG_CONFIG_HOME": str(cfg), "FOREST_CONFIG": "/dev/null", "PYTHONPATH": str(ROOT),
           "COLUMNS": "200", "EDITOR": "true"}

    def run(*args, ok=True, input=None):
        r = subprocess.run([sys.executable, "-m", "forest.cli", *args], env=env, capture_output=True, text=True,
                           input=input, cwd=ROOT)
        if ok:
            assert r.returncode == 0, r.stdout + r.stderr
        return r.stdout + r.stderr
    run("login", "--url", server.base, "--token", wtok)
    run.env = env
    return run


def test_cli_workflow(cli, tmp_path):
    assert "forest" in cli("vaults") and "tasks" in cli("vaults")
    assert "Created" in cli("add", "tasks:.", "CLI Cat", "-f")
    out = cli("add", "tasks:cli-cat", "CLI Task", "-s", "todo", "-p", "high", "-d", "2026-12-24", "-t", "cli")
    assert "tasks:cli-cat/cli-task.md" in out
    cli("note", "tasks:cli-task", "note with [[forest:hub]] link")
    show = cli("show", "tasks:cli-task", "--plain")
    assert "[[forest:hub]]" in show and "#cli" in show     # markup not eaten
    cli("state", "tasks:cli-task", "in-progress")
    cli("due", "tasks:cli-task", "+3d")
    assert "CLI Task" in cli("list", "--state", "undone")
    assert "CLI Task" in cli("agenda")
    assert "cli-task.md" in cli("grep", "note with")
    assert "cli-task.md" in cli("search", "note with link")
    assert "#cli" in cli("tags")
    assert cli("cat", "tasks:cli-task").startswith("---")
    assert "/#tasks/cli-cat/cli-task.md" in cli("link", "tasks:cli-task")
    assert cli("link", "tasks:cli-task", "-w").strip() == "[[tasks:cli-cat/cli-task]]"
    cli("done", "tasks:cli-task")
    assert "update" in cli("history", "tasks:cli-task")
    assert "Logged" in cli("today", "cli journal line")
    assert "Captured" in cli("capture", "cli idea", "--task")
    cli("mv", "tasks:cli-task", ".")
    assert "Deleted" in cli("rm", "tasks:cli-task", "-f")
    assert "Invalid state" in cli("state", "tasks:cli-cat", "bogus", ok=False)
    assert "404" in cli("show", "tasks:nope-nope", ok=False)
    out = cli("backup", str(tmp_path))
    assert "Saved" in out and list(tmp_path.glob("forest-backup-*.tar.gz"))


def test_cli_edit_roundtrip(cli, server, tmp_path):
    script = tmp_path / "ed.sh"
    script.write_text("#!/bin/sh\nsed -i 's/^---$/---/; $ a edited-by-editor' \"$1\"\n")
    script.chmod(0o755)
    cli("add", ".", "Editable")
    env = {**cli.env, "EDITOR": str(script)}
    r = subprocess.run([sys.executable, "-m", "forest.cli", "edit", "editable"], env=env, capture_output=True, text=True, cwd=ROOT)
    assert "Saved" in r.stdout, r.stdout + r.stderr
    assert "edited-by-editor" in cli("cat", "editable")


def test_cli_clone_sync(cli, tmp_path):
    cli("clone", str(tmp_path / "vaults"))
    assert (tmp_path / "vaults" / "forest" / ".git").exists() and (tmp_path / "vaults" / "tasks" / ".git").exists()
    (tmp_path / "vaults" / "forest" / "offline-note.md").write_text("---\nname: Offline note\n---\nfrom laptop\n")
    out = cli("sync", str(tmp_path / "vaults"))
    assert "forest synced" in out
    assert "from laptop" in cli("show", "offline-note", "--plain")
    # token must not be written into the clone's config
    assert "fst_" not in (tmp_path / "vaults" / "forest" / ".git" / "config").read_text()


def test_tui(server, cli):
    os.environ["XDG_CONFIG_HOME"] = cli.env["XDG_CONFIG_HOME"]
    from forest.tui import ForestTUI
    from textual.widgets import Markdown, Static, Tree

    cli("add", "tasks:.", "TUI Task", "-s", "todo")
    cli("add", "tasks:.", "TUI Cat", "-f")

    async def go():
        app = ForestTUI()
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause(0.5)
            assert app.query_one("#tree", Tree).root.children
            await pilot.press("T"); await pilot.pause(0.5)
            assert app.page["path"].startswith("journal/")
            await pilot.press("slash"); await pilot.pause(0.2)
            await pilot.press(*"TUI Task"); await pilot.pause(0.6)
            await pilot.press("enter"); await pilot.pause(0.5)
            assert (app.page["vault"], app.page["path"]) == ("tasks", "tui-task.md")
            await pilot.press("s"); await pilot.pause(0.4)
            assert app.page["state"] == "in-progress"
            await pilot.press("x"); await pilot.pause(0.4)
            assert app.page["state"] == "done"
            await pilot.press("a"); await pilot.pause(0.2)
            await pilot.press(*"from tui [[tasks:tui-cat]]"); await pilot.press("enter"); await pilot.pause(0.5)
            assert "from tui" in app.page["content"]
            app.link_clicked(Markdown.LinkClicked(app.query_one("#body", Markdown), "wiki:tasks:tui-cat"))
            await pilot.pause(0.5)
            assert (app.page["vault"], app.page["path"]) == ("tasks", "tui-cat/index.md")
            await pilot.press("t"); await pilot.pause(0.5)
            await pilot.press("v"); await pilot.pause(0.3)
            await pilot.press("c"); await pilot.pause(0.2)
            await pilot.press(*"tui capture"); await pilot.press("enter"); await pilot.pause(0.5)
    asyncio.run(go())
