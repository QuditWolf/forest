# Adding the Forest Skill to Claude Code

A log of everything done to wire up the Forest knowledge base as a Claude Code skill with auto-triggering.

---

## Files Created

### 1. `AGENT_API_REFERENCE.md` (project root)
Full API reference document for agents. Covers:
- All REST endpoints with request/response shapes
- Data models (`Page`, `TreeNode`) with field-by-field annotations
- Page file format (YAML frontmatter + Markdown)
- Filesystem layout rules
- CLI reference
- Wikilink syntax
- Agent usage patterns and tips
- State/priority value enumerations

This is the canonical reference that the skill and CLAUDE.md both point to.

---

### 2. `.claude/commands/forest.md` (project-level skill)
The `/forest` slash command, available within the forest project. Contains:
- Short curl examples for every major operation (read, search, tree, children, backlinks, create, update, append, delete, move)
- Field value reference (state, priority, date format, parent_path)
- Tips section including the search/navigation strategy

---

### 3. `~/.claude/commands/forest.md` (user-level skill)
An exact copy of the project-level skill, placed in the user-level commands directory so `/forest` is available **from any project**, not just forest.

Synced with:
```bash
cp /home/ssahni/software/forest/.claude/commands/forest.md ~/.claude/commands/forest.md
```

---

### 4. `CLAUDE.md` (project root)
Project-level persistent instructions loaded into every Claude session inside the forest directory. Tells Claude to:
- Auto-trigger Forest API calls when the user uses phrases like "read from forest", "add to forest", "search forest", etc. — without being explicitly asked
- Use the search/navigation strategy
- Prefer `/append` over `PATCH` for additive writes
- Tell the user to run `./start.sh` if the server isn't up

---

### 5. `~/.claude/CLAUDE.md` (global)
Same auto-trigger instructions as the project `CLAUDE.md`, but at the user level so they apply **from any project in any session**. Created fresh (did not exist before).

---

## Wikilink Correction (AGENT_API_REFERENCE.md + both skill files)

The initial docs incorrectly listed `[[/root-page]]` as a valid "absolute from root" link syntax. This is wrong — Python's pathlib treats a leading `/` as a filesystem absolute path, so it bypasses `TASKS_ROOT` entirely and the link silently fails to resolve.

**Correct wikilink rules:**

| Syntax | Meaning |
|--------|---------|
| `[[category/page-name]]` | Root-relative from TASKS_ROOT |
| `[[./sibling]]` | Relative to current page's directory |
| `[[../other-folder/page]]` | Parent-relative |

**Never use a leading `/`.** For root-level pages use `[[root-page]]`, not `[[/root-page]]`.

The skill (`forest.md`) was updated to include a dedicated **Wikilinks in page content** section so agents write correct links when creating or updating pages.

Additionally, all docs and the skill now explicitly state: **use `[[double brackets]]` for internal page links, never `[text](url)`**. Markdown `[]()` links are for external URLs only — they bypass Forest's link resolution and are invisible to backlink tracking.

---

## Tip Added (all three instruction files + AGENT_API_REFERENCE.md)

**Finding things:** two equally valid approaches:
1. Full-text `GET /api/search?q=...` — scans all page content
2. Hierarchical navigation via `GET /api/tree` or `GET /api/children/{path}` — since pages are logically organized by topic and structure, browsing the tree is often the most direct route

The tip does **not** mention a "description" field — no such field exists in the data model. Pages only have `name` (title) and `content` (full markdown body).

---

## Auto-trigger Phrases

Claude will automatically interact with the Forest API when the user says any of:

- "read from forest" / "get from forest" / "fetch from forest" / "look up in forest"
- "add to forest" / "create in forest" / "new page in forest" / "write to forest"
- "update in forest" / "edit in forest" / "change in forest" / "set in forest"
- "append to forest" / "log to forest" / "note in forest"
- "delete from forest" / "remove from forest"
- "search forest" / "find in forest" / "look in forest"
- "show forest" / "list forest" / "forest tree"
- "save this to forest" / "put this in forest"
- "what does forest say about X" / "mark X as done in forest"

---

## File Summary

| File | Scope | Purpose |
|------|-------|---------|
| `AGENT_API_REFERENCE.md` | Project | Full API reference for agents |
| `.claude/commands/forest.md` | Project | `/forest` skill (version-controlled) |
| `~/.claude/commands/forest.md` | Global | `/forest` skill from any project |
| `CLAUDE.md` | Project | Auto-trigger when inside forest dir |
| `~/.claude/CLAUDE.md` | Global | Auto-trigger from any project |
