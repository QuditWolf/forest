"""Real browser (Chromium via Playwright): forest UI, tasks UI, admin, clip, XSS resistance."""

import glob
import os
import re

import pytest

from conftest import PASSWORD

pw = pytest.importorskip("playwright.sync_api")


def _chromium():
    for pat in [os.environ.get("FOREST_TEST_CHROME", ""), os.path.expanduser("~/.cache/ms-playwright/chromium-*/chrome-linux*/chrome")]:
        for p in sorted(glob.glob(pat), reverse=True) if pat else []:
            return p
    return None


@pytest.fixture(scope="module")
def page(server, web):
    exe = _chromium()
    with pw.sync_playwright() as p:
        try:
            b = p.chromium.launch(executable_path=exe, args=["--no-sandbox"]) if exe else p.chromium.launch(args=["--no-sandbox"])
        except Exception as e:
            pytest.skip(f"no chromium: {e}")
        ctx = b.new_context(viewport={"width": 1400, "height": 900})
        ctx.grant_permissions(["clipboard-read", "clipboard-write"])
        pg = ctx.new_page()
        pg.errors = []
        pg.on("pageerror", lambda e: pg.errors.append(str(e)))
        # confirm() dialogs: accept, except the save-conflict one (keep the other side's edit)
        pg.on("dialog", lambda d: d.dismiss() if "changed elsewhere" in d.message else d.accept())
        yield pg
        b.close()


def settle(pg, ms=600):
    pg.wait_for_timeout(ms)


def test_deep_link_through_login(page, server, web):
    web.post("/api/forest/pages", json={"name": "Deep Target", "content": "you made it"})
    page.goto(server.base + "/#forest/deep-target.md")
    assert "/auth/login" in page.url
    page.fill("#password", PASSWORD)
    page.click("button[type=submit]")
    settle(page, 1500)
    assert page.url.endswith("/#forest/deep-target.md")
    assert "you made it" in page.inner_text("#view-pane")
    assert page.is_visible("#page-head") and page.is_visible("#edit-btn")


def test_xss_resistance(page, server, web):
    created = web.post("/api/forest/pages", json={
        "name": "<img src=x onerror=window.__xss1=1>",
        "content": ('<script>window.__xss2=1</script>\n<img src=x onerror="window.__xss3=1">\n'
                    '[[x" onmouseover="window.__xss4=1|click]] <a href="javascript:window.__xss5=1">j</a>\n'
                    '```mermaid\ngraph LR\n A["<img src=x onerror=window.__xss6=1>"] --> B\n```\n'),
        "tags": ["<b>t</b>"]}).json()
    page.goto(server.base + "/#forest/" + created["path"])
    settle(page, 2500)
    assert "<img src=x" in page.inner_text("#tree")  # name shown as text, not HTML
    assert page.input_value("#page-title").startswith("<img")
    page.hover("#view-pane a.wikilink")
    for sel in ["#view-pane a"]:
        for a in page.query_selector_all(sel):
            try:
                a.click(timeout=500)
            except Exception:
                pass
    settle(page)
    page.fill("#search-q", "xss") if page.is_visible("#search-q") else None
    flags = page.evaluate("[1,2,3,4,5,6].map(i => window['__xss' + i])")
    assert flags == [None] * 6, flags


def test_forest_ui_features(page, server, web):
    web.post("/api/forest/pages", json={"name": "UI Hub", "content": "# One\n## Two\nlink [[tasks:ui-task|the task]] #uitag\n## Three\n"})
    web.post("/api/tasks/pages", json={"name": "UI Task", "state": "in-progress", "due": "2026-12-24", "tags": ["uitag"]})
    web.post("/api/tasks/pages", json={"name": "UI Other", "state": "todo", "tags": ["uitag"]})
    web.post("/api/forest/pages", json={"name": "UI Organizer", "content": "## Plan\n- [[tasks:ui-task]]\n- [[tasks:gone-task]]\n- literal: `[[not-a-link]]`\n\n```tasks\ntag: uitag\n```\n"})
    page.goto(server.base + "/#forest/ui-hub.md")
    settle(page, 1500)
    # permalink copy
    page.click(".page-actions button:text-is('link')")
    assert page.evaluate("navigator.clipboard.readText()") == server.base + "/#forest/ui-hub.md"
    # toc
    page.click("#toc-btn"); settle(page, 200)
    assert page.inner_text("#toc-panel").split("\n") == ["One", "Two", "Three"]
    page.keyboard.press("Escape")
    # tag modal
    page.click("#view-pane a.tag"); settle(page)
    assert "UI Hub" in page.inner_text("#tag-list")
    page.keyboard.press("Escape")
    # knowledge pages have no task fields (just tags)
    assert page.query_selector_all("#meta-row select") == [] and page.is_visible("#meta-row .meta-tags")
    # organizer page: live task links + tasks block
    page.goto(server.base + "/#forest/ui-organizer.md"); settle(page, 2000)
    live = page.inner_text("#view-pane a.tasklink")
    assert "◐" in live and "2026-12-24" in live
    assert page.query_selector("#view-pane a.wikilink.broken") is not None       # [[tasks:gone-task]]
    assert page.inner_text("#view-pane code") == "[[not-a-link]]"                # code stays literal
    block = page.inner_text("#view-pane .task-block")
    assert "tag: uitag" in block and "UI Task" in block and "UI Other" in block and "(2)" in block
    page.click("#view-pane a.tasklink"); settle(page, 1500)                    # opens the tasks UI
    assert page.url.endswith("/tasks#tasks/ui-task.md")
    assert "forest:ui-organizer" in page.inner_text("#refs")                   # referenced in
    page.goto(server.base + "/#forest/ui-hub.md"); settle(page, 1500)
    page.keyboard.press("e"); settle(page, 300)
    page.focus("#edit-textarea"); page.keyboard.press("Control+End")
    page.keyboard.type("\nsee [[ui ta"); settle(page, 800)
    assert "UI Task" in page.inner_text("#ac-box")
    page.keyboard.press("Enter")
    page.keyboard.type(" #uit"); settle(page, 600)
    page.keyboard.press("Enter")
    # paste an image
    page.evaluate("""() => { const ta = document.getElementById('edit-textarea'); const dt = new DataTransfer();
        dt.items.add(new File([new Uint8Array([137,80,78,71,13,10,26,10])], 'p.png', {type: 'image/png'}));
        ta.dispatchEvent(new ClipboardEvent('paste', {clipboardData: dt, bubbles: true})); }""")
    settle(page, 1200)
    page.keyboard.press("Control+s"); settle(page, 1000)
    assert page.inner_text("#save-status") in ("saved", "")
    # attachment shows in footer and can be deleted there
    page.keyboard.press("Escape"); settle(page, 800)
    assert "p.png" in page.inner_text("#footer-bar")
    page.click("#footer-bar a[title^='delete attachment']"); settle(page, 1000)
    assert web.get("/api/forest/assets/ui-hub.md").json() == []
    assert "p.png" not in page.inner_text("#footer-bar")
    web.post("/api/forest/shadow/restore", json={"shadow_path": "_assets/ui-hub/p.png"})
    txt = web.get("/api/forest/page/ui-hub.md").json()
    assert "[[tasks:ui-task]]" in txt["content"] and "#uitag" in txt["content"] and "_assets/ui-hub/p.png" in txt["content"]
    page.keyboard.press("Escape"); settle(page)
    # conflict: agent edits while we're editing
    page.keyboard.press("e"); settle(page, 300)
    web.post("/api/forest/page/ui-hub.md/append", json={"text": "concurrent"})
    page.fill("#edit-textarea", "mine")
    page.keyboard.press("Control+s"); settle(page, 1200)
    assert "concurrent" in web.get("/api/forest/page/ui-hub.md").json()["content"]
    # history + restore
    page.keyboard.press("Escape"); settle(page)
    page.click(".page-actions button:text-is('hist')"); settle(page, 800)
    assert len(page.query_selector_all(".hist-row")) >= 3
    page.keyboard.press("Escape")
    # graph explorer: starts with direct neighbours; click expands, click again collapses
    web.post("/api/forest/pages", json={"name": "G Folder", "as_folder": True})
    web.post("/api/forest/pages", json={"name": "G Child", "parent_path": "g-folder/index.md", "content": "[[ui-hub]]"})
    web.post("/api/forest/pages", json={"name": "G Sibling", "parent_path": "g-folder/index.md"})
    page.click(".page-actions button:text-is('graph')"); settle(page, 2500)
    n0 = len(page.query_selector_all("#graph-box g.node"))
    assert n0 >= 3   # ui-hub, tasks:ui-task (link), g-child (backlink)
    child = page.locator("#graph-box g.node", has_text="G Child").first
    child.click(); settle(page, 2000)
    labels = page.inner_text("#graph-box")
    assert "G Folder/" in labels                            # parent pulled in via hierarchy
    n1 = len(page.query_selector_all("#graph-box g.node"))
    assert n1 > n0
    page.locator("#graph-box g.node", has_text="G Child").first.click(); settle(page, 2000)
    assert len(page.query_selector_all("#graph-box g.node")) == n0 and "G Folder/" not in page.inner_text("#graph-box")
    page.uncheck("#g-links"); settle(page, 1500)          # hide link edges -> no dotted edges
    page.check("#g-links"); settle(page, 1500)
    page.locator("#graph-box g.node", has_text="G Child").first.dblclick(); settle(page, 1200)
    assert page.url.endswith("#forest/g-folder/g-child.md")
    page.goto(server.base + "/#forest/ui-hub.md"); settle(page, 1200)
    # today + day stepping
    page.evaluate("document.activeElement.blur()")
    page.keyboard.press("t"); settle(page, 1200)
    assert re.search(r"#forest/journal/\d{4}-\d{2}-\d{2}\.md$", page.url)
    d0 = page.url
    page.keyboard.press("ArrowLeft"); settle(page, 1200)
    assert page.url != d0
    # new page with template
    page.keyboard.press("n"); settle(page, 400)
    page.fill("#np-name", "Templated")
    page.select_option("#np-tpl", "project")
    page.click("#np-overlay .modal-btn.primary"); settle(page, 1200)
    p = web.get("/api/forest/page/templated.md").json()
    assert p["state"] is None and "**Goal:**" in p["content"] and "tag: templated" in p["content"]
    # search modal across vaults
    page.evaluate("document.activeElement.blur()")
    page.keyboard.press("/"); page.keyboard.type("UI Task"); settle(page, 900)
    assert "tasks:ui-task.md" in page.inner_text("#search-results")
    page.keyboard.press("Escape")
    # delete + restore from shadow
    page.goto(server.base + "/#forest/templated.md"); settle(page, 1200)
    page.click(".page-actions button:text-is('del')"); settle(page, 800)
    assert web.get("/api/forest/page/templated.md").status_code == 404
    page.click("button[title='View deleted pages']"); settle(page, 600)
    page.click("#shadow-list span >> text=Templated"); settle(page, 800)
    assert web.get("/api/forest/page/templated.md").status_code == 200
    assert not page.errors, page.errors


def test_tasks_ui(page, server, web):
    web.post("/api/tasks/pages", json={"name": "Browser Cat", "as_folder": True})
    page.goto(server.base + "/tasks"); settle(page, 1500)
    page.click(".cat-item[data-cat='browser-cat']"); settle(page)
    page.keyboard.press("a"); page.fill("#new-name", "Task from browser"); page.keyboard.press("Enter"); settle(page, 1200)
    t = web.get("/api/tasks/page/browser-cat/task-from-browser.md").json()
    assert t["state"] == "todo" and t["priority"] == "medium"
    assert page.url.endswith("#tasks/browser-cat/task-from-browser.md")
    # notes edit
    page.click("#edit-notes-btn"); page.fill("#d-notes", "notes from browser [[forest:ui-hub]]"); page.click("#edit-notes-btn"); settle(page, 800)
    assert "notes from browser" in web.get("/api/tasks/page/browser-cat/task-from-browser.md").json()["content"]
    # x toggles done
    page.evaluate("document.activeElement.blur()")
    page.keyboard.press("x"); settle(page, 1000)
    assert web.get("/api/tasks/page/browser-cat/task-from-browser.md").json()["state"] == "done"
    # copy permalink, open it fresh
    page.click("#copy-link"); link = page.evaluate("navigator.clipboard.readText()")
    assert link == server.base + "/tasks#tasks/browser-cat/task-from-browser.md"
    page.goto(server.base + "/"); page.goto(link); settle(page, 1500)
    assert page.input_value("#d-name") == "Task from browser"
    # calendar drag to reschedule
    web.patch("/api/tasks/page/browser-cat/task-from-browser.md", json={"state": "todo", "due": None})
    web.post("/api/tasks/pages", json={"name": "Cal item", "state": "todo", "due": __import__("datetime").date.today().replace(day=10).isoformat()})
    page.keyboard.press("c"); settle(page, 1200)
    src = page.locator(".cal-task", has_text="Cal item").first
    target_day = __import__("datetime").date.today().replace(day=12).isoformat()
    src.drag_to(page.locator(f".cal-day[data-day='{target_day}']")); settle(page, 1200)
    assert web.get("/api/tasks/page/cal-item.md").json()["due"] == target_day
    assert not page.errors, page.errors


def test_admin_and_clip(page, server, web):
    page.goto(server.base + "/admin"); settle(page, 1500)
    page.fill("#t-name", "from-admin-ui"); page.select_option("#t-scope", "write")
    page.click("button:text-is('generate')"); settle(page, 800)
    tok = page.inner_text("#tok").strip()
    assert tok.startswith("fst_")
    assert '"url": "' + server.base + '/mcp"' in page.inner_text("#new-token")
    assert "from-admin-ui" in page.inner_text("#tokens")
    assert page.get_attribute("#clipper a", "href").startswith("javascript:")
    assert "login.ok" in page.inner_text("#audit")
    page.click(f"#tokens tr:has-text('from-admin-ui') button"); settle(page, 800)
    assert "from-admin-ui" not in page.inner_text("#tokens")
    # clip page (bookmarklet / share target)
    page.goto(server.base + "/clip?text=Look%20at%20this%20https%3A%2F%2Fexample.net%2Fpost&title=Shared"); settle(page, 800)
    assert page.input_value("#url") == "https://example.net/post"
    page.click("button[type=submit]"); settle(page, 1200)
    assert "saved:" in page.inner_text("#result")
    caps = [p for p in web.get("/api/forest/pages").json() if p["path"].startswith("inbox/shared")]
    assert caps
    assert not page.errors, page.errors
