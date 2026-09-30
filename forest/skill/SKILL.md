---
name: forest
description: Use the user's Forest knowledge base and task planner (MCP server "forest"). Use whenever the user mentions notes, pages, the journal/daily note, tasks, todos, agenda, what to do today, reviews, inbox, capturing something, or asks to remember/look up something they wrote.
---

# Forest: agent guide

Forest is the user's personal knowledge base and task planner. Everything is plain Markdown
with YAML frontmatter, stored in git-versioned **vaults** on the user's server and reached
through the `forest` MCP tools (or the REST API at `/api`).

## Mental model

- **Two kinds of vault** (`vaults()` lists them):
  - `forest` = **knowledge**: notes, projects, meetings, journal, inbox, templates. Any tree
    depth. Pages here are never tasks: they have no state/priority/due (the server refuses them).
  - `tasks` = **the task planner**: one page per task, **flat**: at the root ("unsorted") or
    directly in a category (a top-level folder like `work/`). No subtasks, no deeper nesting.
- **Organising work** (the "organizer" is just a normal forest page): write a list or table of
  `[[tasks:work/report]]` links (they render with live state and due date), and/or a live
  block that lists tasks by tag:
  ````
  ```tasks
  tag: alpha          # optional: state: all|done|todo|..., under: work, priority: high, due_within: 2w, overdue: true
  ```
  ````
  Tag tasks `#alpha` and they appear on the project page automatically.
- **Pages**: `x.md` is a leaf page, `dir/index.md` is a folder-page that can hold children
  (knowledge vaults; in `tasks` only categories are folders).
- **Refs**: `path/to/page.md` with a `vault` argument, or `vault:path` (`tasks:work/report`).
  `.md` is optional, a folder resolves to its index, and a 6-char short id or a unique page
  name also works. `page#Heading` addresses one section.
- **Frontmatter**: `name`, `tags` (list), `created` everywhere. Tasks only: `state`
  (todo | in-progress | blocked | waiting | done), `priority` (high | medium | low), `due`
  (YYYY-MM-DD), `completed` (auto when done), `remind: 2026-10-01T09:00` (push reminder). Any other
  keys are preserved, e.g. `source:` (web clips), `ai: readonly`.
- **Links**: `[[path/to/page]]`, `[[./sibling]]`, `[[../up]]`, `[[tasks:work/report]]` across
  vaults, `[[page|label]]`, `[[page#Heading]]`. Never use markdown `[text](url)` for internal links.
- **Tags**: frontmatter `tags: [a, b]` plus inline `#tag` or `#area/sub` in text. `#area`
  matches `#area/sub`.
- **Journal**: `forest:journal/YYYY-MM-DD.md`, one daily note per day (`daily_note`).
- **Inbox**: `forest:inbox/` holds captures and web clips; loose root-level pages in `tasks`
  are also unsorted.
- **Templates**: editable pages in `forest:_templates/` (daily, weekly-review, meeting,
  project, task). Variables: `{{title}} {{date}} {{time}} {{weekday}} {{slug}}`. `project`
  creates a forest page with a ```tasks block for the tag `{{slug}}`; `task` is for the tasks vault.
- **Attachments**: files in `<vault>/_assets/<page>/`, embedded as `![](/api/<vault>/asset/...)`.
  PDF text is searchable.
- **Safety net**: deletes are soft (`.shadow/`, see `trash`/`restore`). Every change is a git
  commit authored by the caller, so `history`, `diff`, `version` and `restore_version` can undo anything.
- **ai: readonly**: pages (or folders whose index.md has it) you must not change. Writes fail;
  ask the user instead.

## Tools

| Tool | Use it for | Notes |
|---|---|---|
| `vaults` | orientation | vaults, counts, valid states/priorities, your access (read/write) |
| `tree` | structure of a vault or subtree | `depth` to limit |
| `ls` | children of one folder with metadata | cheaper than tree |
| `read` | one page | returns sha, outline, url, link. `section=` to read one heading. `raw=True` before edit/write |
| `search` | "where did I write about X" | all words must match, names rank first, covers PDF text |
| `grep` | exact patterns, frontmatter queries | regex over raw files, `glob=` for paths, `context=` lines |
| `find` | locate files by name/path glob | `*meeting*`, `projects/**/index.md` |
| `links` | outgoing, backlinks, unlinked mentions | mentions = pages naming it without a link |
| `graph` | how things connect | nodes + edges of kind `child` (hierarchy) or `link`, `depth` hops, across vaults |
| `tags` | tag list or pages with a tag | hierarchical |
| `agenda` | task lists (tasks vault) | `state`, `priority`, `due_within`, `overdue`, `tag`, `under` (category) |
| `review` | daily/weekly bundle | overdue, due soon, in progress, stale, done, inbox, changes (you vs agents) |
| `daily_note` | today's (or a date's) journal page | creates from template if missing |
| `templates` | available templates | for `create(template=)` |
| `create` | new page or task | task fields without a vault go to `tasks`; `parent` = category for tasks; `template`, `as_folder` (forest, or a category at the tasks root) |
| `capture` | quick note or link into the inbox | `url`, `fetch=True` saves the article, `as_task` |
| `append` | add to the end of a page | timestamped by default; best for logs and notes |
| `edit` | surgical change | exact `old` → `new` on the raw file; unique match or `replace_all` |
| `update` | metadata (state, priority, due, tags, name) | `""` clears a field; `content` replaces the whole body |
| `write` | whole-file create/replace | last resort for existing pages |
| `move` / `promote` | restructure | move takes children along; tasks only move between categories; promote is forest-only |
| `delete` / `trash` / `restore` | soft delete and undo | ask before deleting |
| `history` / `changes` / `diff` / `version` / `restore_version` | git history and undo | `changes` = recent activity across vaults |
| `attachments` / `read_attachment` / `attach` / `delete_attachment` | files | images come back viewable, PDFs as text; delete is soft (`trash`/`restore`) |

Choosing between neighbours:
- search vs grep vs find: search for topics in prose, grep for exact strings, regex and
  frontmatter fields, find for file names.
- append vs edit vs update vs write: append adds, edit changes a specific passage, update
  changes metadata, write replaces everything (avoid on existing pages).

## Recipes

**Capture, then triage the inbox**
1. `capture(text, url=..., tags=[...])` (or `create` in the right place if you already know it).
   `capture(..., as_task=True)` puts it straight into the tasks vault instead.
2. Triage: `review()` gives `inbox`; notes get `move`d to a home in forest and linked; things
   to do become tasks (`create` in `tasks` with state/due, linking back to the note). Confirm
   the plan with the user before moving many items.

**"What should I do today?"**
1. `review(period="day")` and `daily_note()`.
2. Propose at most 5 items (overdue and high priority first, stale ones flagged) as `[[links]]`.
3. After the user confirms: `update(state="in-progress")` as agreed and `append` the plan to today's note.

**Weekly review**
1. `review(period="week")`.
2. `create(name="week-YYYY-Www", vault="forest", parent="journal/index.md", template="weekly-review")`, then fill it with `edit`.
3. List proposed task changes (reschedule, drop, split) and apply them only after confirmation.

**Research a topic**
`search` → `grep` for exact terms → `read` the best hits (use `section=` for long pages) →
`links`/`graph` to follow connections → answer with citations as `[[vault:path]]` links.
Offer to save the summary as a page that links its sources.

**Safe editing**
`read(ref, raw=True)` → `edit(ref, old, new, expected_sha=<sha>)`. On a conflict error,
re-read and redo the edit on the fresh text. Never retry blindly with `write`.

**Undo**
`history(ref)` → `diff(rev, ref=ref)` to see what changed → `restore_version(ref, rev)`
(creates a new commit, nothing is lost). For deleted pages: `trash()` → `restore(shadow_path)`.

**Files**
`attachments(ref)` → `read_attachment(path)` (images are visible to you, PDFs as text).
To add one: `attach(ref, filename, base64_data)`, then put the returned markdown into the
page with `edit` or `append`.

**Plan a project (tasks + organizer page)**
1. `create(name="Alpha launch", vault="forest", parent="projects/index.md", template="project")`:
   a forest page whose ```tasks block lists everything tagged `#alpha-launch`.
2. For each piece of work: `create(name=..., vault="tasks", parent="work", state="todo",
   tags=["alpha-launch"], due=...)` (flat; the category is just the task's area).
3. Put context in the task description with links back: `[[forest:projects/alpha-launch]]`.
   The project page stays current by itself; don't copy task lists into it by hand.

**Link a note and a task**
In the note: `append("forest:meetings/2026-10-01", "Follow-up: [[tasks:work/write-report]]")`.
In the task: `append("tasks:work/write-report", "Context: [[forest:meetings/2026-10-01]]")`.
Backlinks then work in both directions (the tasks UI shows "referenced in").

**Log your own work**
At the end of a work session: `append(<today's daily note>, "Agent: did X, see [[...]]")`.

## Rules

1. Search before creating; extend existing pages instead of making near-duplicates.
2. Tasks live only in `tasks`, flat (root or one category). Never put state/due on forest
   pages and never nest tasks; for bigger work, tag the tasks and use a forest page with a
   ```tasks block and/or `[[tasks:...]]` links.
3. Prefer `append` and `edit`; never overwrite a whole page when a smaller change works.
4. Respect `ai: readonly`; if a write is refused, tell the user and suggest the change.
5. Ask before deleting, moving many pages, or bulk-changing task states or due dates.
6. Always link related pages with `[[...]]`, including across vaults; pick up unlinked mentions from `links`.
7. Tag consistently: reuse existing tags (`tags()`) before inventing new ones; lowercase, `area/sub` for hierarchy.
8. Use `expected_sha` for edits on pages the user may be editing.
9. Keep answers grounded: cite pages as `[[vault:path]]` and give the `url` when the user wants to open them.

## Ready-made queries

- Overdue: `agenda(overdue=True)`
- This week: `agenda(due_within="7d")`
- Blocked or waiting: `agenda(state="blocked")`, `agenda(state="waiting")`
- By project tag: `agenda(tag="alpha-launch")`; by category: `agenda(under="work")`
- High priority open: `agenda(priority="high")`
- Recent activity: `changes(since="2 days ago")`
- Frontmatter via grep: `grep("^state: blocked")`, `grep("^due: 2026-10", glob="work/*")`,
  `grep("^source: ")` (web clips), `grep("^remind: ")`
- Open checkboxes: `grep("^\\s*- \\[ \\]")`
