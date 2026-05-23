---
description: Interact with the Forest knowledge base — read, search, create, update, append, delete pages via the REST API.
---

You are interacting with the **Forest knowledge base** — a hierarchical markdown wiki with optional task metadata, served locally at `http://sjcvl-ssahni:7000`.

The full API reference lives at `/workspace/task_think/AGENT_API_REFERENCE.md`. Read it if you need details beyond what's below.

## Your job

Based on what $ARGUMENTS says (or what the user just asked), perform the appropriate Forest operation(s) using `curl` via the Bash tool. Always show the user the result in a clean, readable way.

---

## Quick Reference

### Read a page
```bash
curl -s http://sjcvl-ssahni:7000/api/page/PATH_HERE
```

### Search
```bash
curl -s "http://sjcvl-ssahni:7000/api/search?q=KEYWORDS"
```

### Browse full tree
```bash
curl -s http://sjcvl-ssahni:7000/api/tree
```

### Children of a folder
```bash
curl -s http://sjcvl-ssahni:7000/api/children/PARENT_PATH
```

### Backlinks to a page
```bash
curl -s http://sjcvl-ssahni:7000/api/backlinks/PATH_HERE
```

### Create a page
```bash
curl -s -X POST http://sjcvl-ssahni:7000/api/pages \
  -H "Content-Type: application/json" \
  -d '{"name": "Page Name", "content": "# Page Name\n\nContent.", "parent_path": null}'
```

### Update a page (any field optional)
```bash
curl -s -X PATCH http://sjcvl-ssahni:7000/api/page/PATH_HERE \
  -H "Content-Type: application/json" \
  -d '{"content": "new content", "state": "done"}'
```

### Append a note (safe, non-destructive)
```bash
curl -s -X POST http://sjcvl-ssahni:7000/api/page/PATH_HERE/append \
  -H "Content-Type: application/json" \
  -d '{"text": "Your note here."}'
```

### Delete (soft — recoverable from .shadow/)
```bash
curl -X DELETE http://sjcvl-ssahni:7000/api/page/PATH_HERE
```

### Move to new parent
```bash
curl -s -X POST http://sjcvl-ssahni:7000/api/page/PATH_HERE/move \
  -H "Content-Type: application/json" \
  -d '{"new_parent_path": "new-parent"}'
```

---

## Wikilinks in page content

When writing `content` for any create or update operation, use `[[double brackets]]` to link to other pages. **Links are filesystem path refs, not URLs.**

**Always use `[[double brackets]]` for internal page links — never `[text](url)` markdown links.** `[]()`  is for external URLs only; it does not integrate with Forest's link resolution or backlink tracking.

| Write this | Meaning |
|------------|---------|
| `[[category/page-name]]` | Root-relative — resolves from TASKS_ROOT |
| `[[./sibling-page]]` | Same folder as current page |
| `[[../other-folder/page]]` | Parent-relative |

**Never use a leading slash.** `[[/root-page]]` does NOT mean "root of the knowledge base" — Python's pathlib treats it as an absolute filesystem path and it will silently fail to resolve. Use `[[root-page]]` for root-level pages.

Examples in content JSON:
```bash
# Link to a root-level page
-d '{"content": "See [[my-notes]] for details."}'

# Link to a nested page
-d '{"content": "Related: [[projects/alpha]]"}'

# Relative link (from inside projects/alpha.md, linking to sibling)
-d '{"content": "See also [[./beta]]"}'
```

---

## Field values

**state:** `todo` | `in-progress` | `blocked` | `waiting` | `done`
**priority:** `high` | `medium` | `low`
**due:** `YYYY-MM-DD`
**parent_path:** canonical path string, or `null` for root level
**as_folder:** `true` to create a folder-page (has children)

---

## Tips

- **Finding things**: two equally valid approaches — (1) full-text `GET /api/search?q=...` scans all page content; (2) hierarchical navigation via `GET /api/tree` or `GET /api/children/{path}`, since pages are logically organized by topic and structure. Use whichever fits.
- Use `/append` instead of `PATCH content` whenever you're only adding — it's non-destructive and timestamp-stamped.
- Use `/search` first to discover existing pages before creating duplicates.
- `short_id` (6-char hash of path) can be used as a stable reference in notes.
- All deletes are soft — data goes to `.shadow/` and can be restored.
- Prefer piping through `jq` for readable output: `curl -s ... | jq '.'`
