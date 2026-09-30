---
name: forest
description: Use the user's Forest knowledge base and task planner (MCP server "forest"). Use whenever the user mentions notes, pages, the journal/daily note, tasks, todos, agenda, what to do today, reviews, inbox, capturing something, or asks to remember/look up something they wrote.
---

# Forest: agent guide

Forest is the user's personal knowledge base and task planner. Everything is plain Markdown
with YAML frontmatter, stored in git-versioned **vaults** on the user's server and reached
through the `forest` MCP tools (or the REST API at `/api`).

## Mental model

- **Vaults**: separate page trees. Usually `forest` (notes, knowledge, journal, templates,
  inbox) and `tasks` (task planner, top-level folders = categories). `vaults()` lists them.
- **Pages**: `x.md` is a leaf page, `dir/index.md` is a folder-page that can hold children.
  A leaf used as a parent is promoted to a folder automatically.
- **Refs**: `path/to/page.md` with a `vault` argument, or `vault:path` (`tasks:work/report`).
  `.md` is optional, a folder resolves to its index, and a 6-char short id or a unique page
  name also works. `page#Heading` addresses one section.
- **Frontmatter**: `name`, `state` (todo | in-progress | blocked | waiting | done),
  `priority` (high | medium | low), `due` (YYYY-MM-DD), `tags` (list), `created`,
  `completed` (auto when state becomes done). Any other keys are preserved, e.g. `source:`
  (web clips), `remind: 2026-10-01T09:00` (push reminder), `ai: readonly`.
- **Links**: `[[path/to/page]]`, `[[./sibling]]`, `[[../up]]`, `[[tasks:work/report]]` across
  vaults, `[[page|label]]`, `[[page#Heading]]`. Never use markdown `[text](url)` for internal links.
- **Tags**: frontmatter `tags: [a, b]` plus inline `#tag` or `#area/sub` in text. `#area`
  matches `#area/sub`.
- **Journal**: `forest:journal/YYYY-MM-DD.md`, one daily note per day (`daily_note`).
- **Inbox**: `forest:inbox/` holds captures and web clips; loose root-level pages in `tasks`
  are also unsorted.
- **Templates**: editable pages in `forest:_templates/` (daily, weekly-review, meeting,
  project, task). Variables: `{{title}} {{date}} {{time}} {{weekday}}`.
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
| `graph` | how things connect | nodes/edges, `depth` hops, across vaults |
| `tags` | tag list or pages with a tag | hierarchical |
| `agenda` | task lists | `state`, `priority`, `due_within`, `overdue`, `tag`, `under`, `vault` |
| `review` | daily/weekly bundle | overdue, due soon, in progress, stale, done, inbox, changes (you vs agents) |
| `daily_note` | today's (or a date's) journal page | creates from template if missing |
| `templates` | available templates | for `create(template=)` |
| `create` | new page | `parent`, metadata, `template`, `as_folder` |
| `capture` | quick note or link into the inbox | `url`, `fetch=True` saves the article, `as_task` |
| `append` | add to the end of a page | timestamped by default; best for logs and notes |
| `edit` | surgical change | exact `old` → `new` on the raw file; unique match or `replace_all` |
| `update` | metadata (state, priority, due, tags, name) | `""` clears a field; `content` replaces the whole body |
| `write` | whole-file create/replace | last resort for existing pages |
| `move` / `promote` | restructure | move takes children along |
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
2. Triage: `review()` gives `inbox`; for each item `search` for a home, then `move`, add
   `[[links]]` with `edit`/`append`, set `state`/`due` with `update` if it is a task. Confirm
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

**Link a note to a task across vaults**
In the note: `append("forest:projects/x", "Task: [[tasks:work/write-report]]")`. In the
task: `append("tasks:work/write-report", "Notes: [[forest:projects/x]]")`. Backlinks then
work in both directions.

**Log your own work**
At the end of a work session: `append(<today's daily note>, "Agent: did X, see [[...]]")`.

## Rules

1. Search before creating; extend existing pages instead of making near-duplicates.
2. Prefer `append` and `edit`; never overwrite a whole page when a smaller change works.
3. Respect `ai: readonly`; if a write is refused, tell the user and suggest the change.
4. Ask before deleting, moving many pages, or bulk-changing task states or due dates.
5. Always link related pages with `[[...]]`, including across vaults; pick up unlinked mentions from `links`.
6. Tag consistently: reuse existing tags (`tags()`) before inventing new ones; lowercase, `area/sub` for hierarchy.
7. Use `expected_sha` for edits on pages the user may be editing.
8. Keep answers grounded: cite pages as `[[vault:path]]` and give the `url` when the user wants to open them.

## Ready-made queries

- Overdue: `agenda(overdue=True)`
- This week: `agenda(due_within="7d")`
- Blocked or waiting: `agenda(state="blocked")`, `agenda(state="waiting")`
- By tag: `agenda(tag="work")`; under a folder: `agenda(vault="tasks", under="work")`
- High priority open: `agenda(priority="high")`
- Recent activity: `changes(since="2 days ago")`
- Frontmatter via grep: `grep("^state: blocked")`, `grep("^due: 2026-10", glob="work/*")`,
  `grep("^source: ")` (web clips), `grep("^remind: ")`
- Open checkboxes: `grep("^\\s*- \\[ \\]")`
