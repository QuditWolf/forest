# Forest Knowledge Base — Agent API Reference

> Complete reference for Claude and other agents to interact with the Forest knowledge base (TaskThink).
> Base URL: `http://sjcvl-ssahni:7000` (configurable via `tasks.toml` or env vars)

---

## Overview

Forest is a **hierarchical markdown knowledge base** with optional task metadata. Data is stored as plain `.md` files with YAML frontmatter on the filesystem — no database.

- Pages live in a tree (folders can have sub-pages)
- Each page can optionally carry task state, priority, and due date
- The API is fully open (no auth required)
- All content supports `[[wikilinks]]` for cross-referencing

---

## Configuration

### tasks.toml (or config.sample.toml)
```toml
[server]
host = "127.0.0.1"
port = 7000

[tasks]
root = "~/org/forest"   # where .md files live
```

### Environment Variable Overrides
| Variable | Purpose |
|----------|---------|
| `TASKS_CONFIG` | Explicit path to config file |
| `TASKS_ROOT` | Override knowledge base root directory |
| `TASKS_HOST` | Override server bind host |
| `TASKS_PORT` | Override server port |

---

## Data Models

### Page Object (full response)
```json
{
  "path": "category/subfolder/page.md",
  "short_id": "a1b2c3",
  "name": "Page Display Name",
  "parent_path": "category/subfolder",
  "is_folder": false,
  "children": [],

  "state": "todo | in-progress | blocked | waiting | done | null",
  "priority": "high | medium | low | null",
  "due": "2026-04-20",
  "created": "2026-04-10",
  "completed": null,

  "content": "# Markdown body\n\nSupports [[wikilinks]].",
  "backlinks": []
}
```

**Key concepts:**
- `path` — root-relative canonical path (always ends in `.md`)
- `short_id` — first 6 chars of sha1(path), for quick reference
- `is_folder` — if true, page is a directory-level `index.md` and can have children
- `parent_path` — canonical path of parent folder-page; `null` = root level
- `children` — list of canonical paths of immediate children
- `backlinks` — only populated by `GET /api/backlinks/{path}`; empty on other endpoints
- `completed` — auto-stamped when `state` → `"done"`; cleared if state changes again

### TreeNode Object
```json
{
  "path": "category",
  "name": "Category",
  "is_folder": true,
  "children": [
    {
      "path": "category/child.md",
      "name": "Child Page",
      "is_folder": false,
      "children": []
    }
  ]
}
```

### Page File Format (on disk)
```markdown
---
name: Optional Title Override
state: todo
priority: high
due: 2026-04-20
created: 2026-04-10
completed:
---

# Markdown content here

Root-relative link: [[category/page-name]]
Relative link: [[./sibling]]
Parent-relative link: [[../other-folder/page]]
```

---

## REST API Endpoints

### Navigation & Discovery

#### `GET /api/tree`
Returns the full hierarchical tree of all pages.

**Response:** `TreeNode[]`
```json
[
  {
    "path": "projects",
    "name": "Projects",
    "is_folder": true,
    "children": [
      { "path": "projects/alpha.md", "name": "Alpha", "is_folder": false, "children": [] }
    ]
  }
]
```

#### `GET /api/children`
Returns root-level pages only (not nested).

**Response:** `Page[]`

#### `GET /api/children/{parent_path}`
Returns direct children of a folder-page.

**Example:** `GET /api/children/projects`

**Response:** `Page[]`

#### `GET /api/search?q={query}`
Full-text search across all pages (case-insensitive, excludes `.shadow/`).

**Example:** `GET /api/search?q=machine+learning`

**Response:**
```json
[
  {
    "path": "notes/ml-intro.md",
    "name": "ML Introduction",
    "snippet": "...context around the matched text (120 char window)..."
  }
]
```

#### `GET /api/backlinks/{path}`
Returns paths of all pages that link to the given page via `[[wikilinks]]`.

**Example:** `GET /api/backlinks/notes/ml-intro.md`

**Response:** `string[]` — list of canonical paths

---

### Page CRUD

#### `GET /api/page/{path}`
Fetch a single page by its canonical path.

**Example:** `GET /api/page/projects/alpha.md`

**Response:** `Page` object (backlinks field will be empty; use `/api/backlinks/{path}` separately)

#### `POST /api/pages`
Create a new page.

**Request body:**
```json
{
  "name": "My New Page",
  "content": "# My New Page\n\nContent here.",
  "parent_path": "projects",
  "as_folder": false,
  "state": "todo",
  "priority": "high",
  "due": "2026-04-20"
}
```

| Field | Type | Required | Default | Notes |
|-------|------|----------|---------|-------|
| `name` | string | **yes** | — | Display name, also used for filename |
| `content` | string | no | `""` | Markdown body |
| `parent_path` | string\|null | no | `null` | Path of parent folder-page; `null` = root |
| `as_folder` | boolean | no | `false` | Create as folder-page with `index.md` |
| `state` | string | no | `null` | `todo\|in-progress\|blocked\|waiting\|done` |
| `priority` | string | no | `null` | `high\|medium\|low` |
| `due` | string | no | `null` | `YYYY-MM-DD` |

**Response:** Created `Page` object

#### `PATCH /api/page/{path}`
Update one or more fields of an existing page. All fields optional.

**Example:** `PATCH /api/page/projects/alpha.md`

**Request body (all optional):**
```json
{
  "name": "Alpha Project",
  "content": "# Updated content",
  "state": "in-progress",
  "priority": "medium",
  "due": "2026-05-01"
}
```

**Response:** Updated `Page` object

#### `DELETE /api/page/{path}`
Soft-delete a page (moves to `.shadow/` directory, recoverable).

**Example:** `DELETE /api/page/projects/alpha.md`

**Response:** `204 No Content`

#### `POST /api/page/{path}/move`
Move a page to a new parent folder.

**Request body:**
```json
{ "new_parent_path": "archive" }
```

Use `null` to move to root: `{ "new_parent_path": null }`

**Response:** Updated `Page` object with new path

#### `POST /api/page/{path}/promote`
Convert a leaf page into a folder-page (so it can have children).

**Request body:** `{}` (empty)

**Response:** Updated `Page` object with `is_folder: true`

---

### Agent-Friendly Endpoints

#### `POST /api/page/{path}/append`
Append a timestamped note to a page's content. Ideal for agents logging observations or updates without reading/rewriting the full content.

**Example:** `POST /api/page/projects/alpha.md/append`

**Request body:**
```json
{ "text": "Reviewed dependencies. All look good." }
```

The system auto-prepends a timestamp. Result is appended to the existing `content`.

**Response:** Updated `Page` object

#### `GET /api/shadow`
List all soft-deleted pages.

**Response:**
```json
[
  {
    "shadow_path": ".shadow/projects/alpha.md",
    "path": "projects/alpha.md",
    "name": "Alpha",
    "is_folder": false
  }
]
```

#### `POST /api/shadow/restore`
Restore a soft-deleted page back to its original location.

**Request body:**
```json
{ "shadow_path": ".shadow/projects/alpha.md" }
```

**Response:** Restored `Page` object

---

## CLI Reference

Invoked as `tasks` after install.

### Page Operations
```bash
# List pages (tree view)
tasks list
tasks list --path projects         # list children of a folder

# View a page
tasks show projects/alpha.md
tasks show a1b2c3                  # by short_id

# Create a page
tasks new "My Page"
tasks new "My Page" --parent projects --state todo --priority high
tasks new "My Folder" --folder     # create as folder-page

# Edit content in $EDITOR
tasks edit projects/alpha.md

# Update metadata
tasks set projects/alpha.md --state done
tasks set projects/alpha.md --priority low --due 2026-05-01

# Append a note
tasks append projects/alpha.md "Finished the review."

# Move a page
tasks move projects/alpha.md --parent archive

# Delete (soft)
tasks delete projects/alpha.md

# Promote leaf → folder
tasks promote projects/alpha.md
```

### Search & Navigation
```bash
tasks search "machine learning"
tasks backlinks notes/ml-intro.md
tasks shadow                        # list deleted pages
tasks shadow restore .shadow/projects/alpha.md
```

---

## Filesystem Layout

```
TASKS_ROOT/                         # default: ~/org/forest
├── folder-name/
│   ├── index.md                   # folder-page (is_folder=true)
│   ├── child-page.md              # leaf page
│   └── nested/
│       ├── index.md
│       └── deep-page.md
├── root-page.md                   # leaf page at root level
└── .shadow/                       # soft-deleted pages (hidden)
    └── old-page.md
```

**Rules:**
- A directory with `index.md` = folder-page (`is_folder: true`)
- A standalone `.md` file = leaf page (`is_folder: false`)
- Leaf pages can be promoted to folder-pages via the `/promote` endpoint
- Deleted pages go to `.shadow/` preserving their relative path
- No database — filesystem IS the source of truth

---

## Wikilink Syntax

Wikilinks are **not** URLs. They are filesystem path references resolved against `TASKS_ROOT`.

**Use `[[double brackets]]` for all internal page links — not `[text](url)`.** Markdown `[]()` links are for external URLs only; they do not integrate with Forest's link resolution or backlink tracking.

| Syntax | Meaning |
|--------|---------|
| `[[projects/alpha]]` | Root-relative: resolves to `TASKS_ROOT/projects/alpha.md` or `.../alpha/index.md` |
| `[[./sibling]]` | Relative to current page's directory |
| `[[../other-folder/page]]` | Parent-relative |

**Do not use a leading `/`** — `[[/root-page]]` does not mean "root of TASKS_ROOT". Python's pathlib would treat it as an absolute filesystem path (`/root-page.md`), which won't exist. Use `[[root-page]]` for root-level pages.

### Resolution algorithm (`resolve_page_ref`)

For a ref in page `current_path`:

1. If ref starts with `./` or `../`:
   - Compute base directory from `current_path`
   - For folder-pages (`index.md`), base = the *folder's parent directory* (not the index file's dir)
   - For leaf pages, base = the file's directory
   - Resolve ref relative to that base
2. Otherwise: resolve ref relative to `TASKS_ROOT` directly

Then try three candidates in order:
1. Resolved path as-is
2. Resolved path + `.md`
3. Resolved path + `/index.md`

First match that exists on disk wins.

### Backlinks

Computed on demand — no stored index. `GET /api/backlinks/{path}`:
1. Scans every `.md` under `TASKS_ROOT` (skips `.shadow/` and dot-prefixed paths)
2. Extracts all `[[...]]` refs via regex
3. Resolves each ref to a canonical path via the algorithm above
4. Returns paths of all pages where any ref resolves to the target

---

## Agent Usage Patterns

### Read a page
```
GET /api/page/{path}
```

### Search for relevant pages
```
GET /api/search?q=your+keywords
```

### Create a new note
```
POST /api/pages
{ "name": "Agent Note", "parent_path": "agent-logs", "content": "...", "state": "todo" }
```

### Append an observation without overwriting
```
POST /api/page/{path}/append
{ "text": "Your observation here." }
```

### Update task status
```
PATCH /api/page/{path}
{ "state": "done" }
```

### Find what links to a page
```
GET /api/backlinks/{path}
```

### Explore the full knowledge tree
```
GET /api/tree
```

### Get children of a section
```
GET /api/children/{parent_path}
```

---

## State & Priority Values

**State values:**
- `todo` — not started
- `in-progress` — actively being worked on
- `blocked` — waiting on external dependency
- `waiting` — waiting on someone else
- `done` — completed (auto-stamps `completed` date)

**Priority values:**
- `high`
- `medium`
- `low`

---

## Notes for Agents

- **Finding things** — two equally valid approaches: (1) full-text `GET /api/search?q=...` scans all page content; (2) hierarchical navigation via `GET /api/tree` or `GET /api/children/{path}` — since pages are logically organized by topic and structure, browsing the tree is often the most direct route. Use whichever fits the situation.
- **No auth required** — the API is fully open; handle network access at the infrastructure level
- **Soft deletes** — `DELETE` never destroys data; use `GET /api/shadow` + `POST /api/shadow/restore` to recover
- **`/append` is safe for concurrent writes** — prefer it over `PATCH content` when only adding to a page
- **`short_id`** — each page has a 6-char ID (`sha1(path)[:6]`) usable as a stable reference in notes
- **Search is full-text** — it reads all `.md` files; useful for discovery before fetching a specific page
- **Backlinks are computed live** — no need to maintain a separate index
- **`as_folder: true`** in `POST /api/pages` creates a folder-page with an `index.md` that can hold children
- **Date format** — always `YYYY-MM-DD` for `due`, `created`, `completed`
- **Content is Markdown** — supports all standard Markdown plus Mermaid diagram blocks (` ```mermaid `)
