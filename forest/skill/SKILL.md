---
name: forest
description: The user's second brain - Forest knowledge base and task planner (forest MCP tools: the claude.ai "Forest" connector or a local "forest" server). Use whenever a conversation touches the user's own projects, work, setup, people, plans, decisions, ideas or past notes (to recall context and, in auto mode, save what is worth keeping), and whenever they mention notes, pages, journal, tasks, todos, agenda, today, reviews, inbox, capture, "remember this" or "what did I write about". Also handles the chat keywords `forest: off|recall|auto`.
---

# Forest: agent guide

Forest is the user's personal knowledge base and task planner. Everything is plain Markdown
with YAML frontmatter, stored in git-versioned **vaults** on the user's server and reached
through the forest MCP tools (or the REST API at `/api`). The tools are the same whether they come
from the claude.ai "Forest" connector or a local `forest` server (in Claude Code: `mcp__claude_ai_Forest__*`
or `mcp__forest__*`); use whichever is connected. If no forest tools are available, tell the user to
add the connector on claude.ai or run `curl -fsSL https://<their forest host>/install.sh | bash`, then log in with `/mcp`.

## Proactive mode (read this first)

The user controls how much you use forest on your own. The saved default is in the server
instructions and in every `recall` result (`mode`); the user overrides it per conversation by
writing a keyword in any message (latest wins):

| keyword | you do |
|---|---|
| `forest: off` | nothing unless asked |
| `forest: recall` | look things up on your own, save only when asked |
| `forest: auto` | look up and also save what is worth keeping, on your own |
| `forest: default <mode>` | `mode(set="<mode>")` changes the default for new conversations; use it now too |

Explicit requests ("save this", "what do I have on X") always work, in every mode.

**Recall (recall and auto).** When the conversation touches the user's own world (a project, their
server, a person, a plan, a past decision, something they are learning), call `recall(topic)` once
for that topic before answering, then `read` the one or two hits that matter. Use what you find
naturally and cite it as `[[links]]`; if nothing is relevant, say nothing about forest. Do not
recall for generic questions (syntax, trivia) or on every message.

**Save (auto only, or when asked).** Worth saving: decisions and why, facts about the user's
projects/setup/people, conclusions of research or debugging (the fix, not the journey), plans,
preferences, and things they need to do (those become tasks). Not worth saving: small talk, general
knowledge they can look up, half-finished thinking, anything they call private or temporary.
- Save when a point is settled, usually once or twice per conversation, not every turn.
- `recall` first; if a page fits, `remember(text, about="<that page>")` extends it. Otherwise
  `remember(text, title=...)` files a new note in the inbox for the user to triage.
- To-dos go to `create(vault="tasks", state="todo", ...)` with a link back to the note.
- Write the note so it stands alone in a year: what, why, key details, `[[links]]` to related pages.
- End your answer with one line: `saved to forest: [[...]]` (one line per item, nothing more).

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
  (YYYY-MM-DD), `completed` (auto when done), and for scheduling `at: "14:30"` (time on the due
  date), `duration: 45m`, `remind: [10m, 1h]` (alerts before; omit to use the calendar's defaults). Any other
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
  Text inside pdf, docx, html and text files (txt, md, csv, json...) is extracted automatically and is
  searchable with search/grep; `read` lists a page's attachments.
- **Safety net**: deletes are soft (`.shadow/`, see `trash`/`restore`). Every change is a git
  commit authored by the caller, so `history`, `diff`, `version` and `restore_version` can undo anything.
- **ai: readonly**: pages (or folders whose index.md has it) you must not change. Writes fail;
  ask the user instead.

## Tools

| Tool | Use it for | Notes |
|---|---|---|
| `recall` | "what do I already have on X" | loose ranked lookup, any words; snippet + link + tags + task state; cheap, use it proactively |
| `remember` | save knowledge from the chat | `about=` appends to an existing page (preferred), else a new inbox page; returns the link |
| `mode` | saved proactive default | `set=` only when the user asks to change the default |
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
| `upload_link` | let the user add a file they have | returns a short-lived link that only uploads into one page; give it to the user |
| `history` / `changes` / `diff` / `version` / `restore_version` | git history and undo | `changes` = recent activity across vaults |
| `attachments` / `read_attachment` / `attach` / `delete_attachment` | files | `read` lists a page's files (`attachments=True` includes their text); images come back viewable; pdf/docx/html/text as text (`offset` for long ones); `attach(url=...)` downloads a public file, `embed=True` adds it to the page; delete is soft |

Choosing between neighbours:
- search vs grep vs find: search for topics in prose, grep for exact strings, regex and
  frontmatter fields, find for file names.
- append vs edit vs update vs write: append adds, edit changes a specific passage, update
  changes metadata, write replaces everything (avoid on existing pages).

## Shaping the knowledge base

Forest is worth more the more it is connected. When you write, aim for these page kinds (reuse the
folders that exist; check `tree(depth=1)` once):
- **Project hub** (`projects/<name>`): goal, status line, key decisions, links to notes, a
  ```tasks block for its tag. Everything about the project links here.
- **Topic / evergreen note**: one idea or subject per page, written to be re-read ("How backups
  work on kelper", not "notes 12 Oct"). Prefer extending these over creating new ones.
- **Decision**: a `## Decisions` section on the project page (date, decision, why, alternatives)
  rather than a page per decision, unless it is big.
- **Person** (`people/<name>`): who they are, context, what you discussed, links to meetings.
- **Journal** (daily note): what happened today, short, with links out. Logs, not knowledge:
  move anything lasting into a topic or project page and link it.
- **Inbox**: unsorted captures. Triage regularly into the pages above.

Good notes: a clear `name`, 1-3 tags from `tags()`, a first line that says what the page is, short
sections, links to at least one related page (and a link back from that page when it matters).

## Recipes

**Start of a conversation about the user's work** (recall/auto)
`recall("<topic in a few words>")` → `read` the best hit (`section=` for long pages) → answer
using it. If the project has a hub page, mention open tasks from its ```tasks tag via `agenda(tag=)` when relevant.

**End of a useful conversation** (auto, or when the user says "save this")
Summarise what was settled into 1-3 notes: `recall` to find homes → `remember(..., about=)` or a
new note → tasks for follow-ups → one `saved to forest:` line each.

**"What do I know about X / what did I decide about Y"**
`recall` → `search` for exact phrases if needed → `read` → answer with `[[links]]` and dates.

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
- Reading: `read(ref, attachments=True)` gives the page plus the text of its documents in one call;
  `read_attachment(path)` for images (you see them) or to page through a long PDF (`offset`).
  `search`/`grep` already cover attachment text, so search first when looking for something in documents.
- Adding: `attach(ref, url="https://...")` for files on the web (the server downloads it), or
  `attach(ref, base64_data=..., filename=...)` for content you have. It is embedded in the page by
  default; pass `embed=False` and place the returned markdown with `edit` when position matters.
  After attaching a document, summarise it into the page (with a link to the file) so it is useful at a glance.
- Files the user shared in the chat: you see their content, not the file itself, so you cannot attach
  the original. Offer `upload_link(ref)` and ask the user to open the link and pick the file (works on a
  phone), then `read(ref, attachments=True)`. You can also save what you read from it as page text.

**Plan a project (tasks + organizer page)**
1. `create(name="Alpha launch", vault="forest", parent="projects/index.md", template="project")`:
   a forest page whose ```tasks block lists everything tagged `#alpha-launch`.
2. For each piece of work: `create(name=..., vault="tasks", parent="work", state="todo",
   tags=["alpha-launch"], due=...)` (flat; the category is just the task's area).
3. Put context in the task description with links back: `[[forest:projects/alpha-launch]]`.
   The project page stays current by itself; don't copy task lists into it by hand.

**Schedule something** ("remind me 15 min before my 3pm call tomorrow")
`create(name="Call with Asha", vault="tasks", state="todo", due="<tomorrow>", at="15:00", duration="30m",
remind=["15m"])`, or `update(ref, due=..., at=..., remind=[...])` for an existing task. Use the user's
local time. Date-only tasks are all-day. `agenda(due_on="tomorrow")` lists them in time order.
Tasks with a due date also show up in the user's Google Calendar ("forest" calendar) with these alerts.
Meetings in the user's other Google calendars are not in forest; use a calendar connector for those.

**Link a note and a task**
In the note: `append("forest:meetings/2026-10-01", "Follow-up: [[tasks:work/write-report]]")`.
In the task: `append("tasks:work/write-report", "Context: [[forest:meetings/2026-10-01]]")`.
Backlinks then work in both directions (the tasks UI shows "referenced in").

**Log your own work**
At the end of a work session: `append(<today's daily note>, "Agent: did X, see [[...]]")`.

## Rules

1. Recall/search before creating; extend existing pages instead of making near-duplicates.
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
10. Follow the mode: never save unasked in `off`/`recall`; never go silent about something you saved in `auto`.
11. Never store secrets (passwords, tokens, keys) in forest, even when asked to remember setup details; note where they live instead.

## Ready-made queries

- Overdue: `agenda(overdue=True)`
- Tomorrow: `agenda(due_on="tomorrow")`; a given day: `agenda(due_on="2026-10-24")`
- This week: `agenda(due_within="7d")`
- Blocked or waiting: `agenda(state="blocked")`, `agenda(state="waiting")`
- By project tag: `agenda(tag="alpha-launch")`; by category: `agenda(under="work")`
- High priority open: `agenda(priority="high")`
- Recent activity: `changes(since="2 days ago")`
- Frontmatter via grep: `grep("^state: blocked")`, `grep("^due: 2026-10", glob="work/*")`,
  `grep("^source: ")` (web clips), `grep("^remind: ")`
- Open checkboxes: `grep("^\\s*- \\[ \\]")`
