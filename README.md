# forest

Personal knowledge base + task planner. Plain Markdown pages with YAML frontmatter in
git-versioned **vaults**, served by one small server:

- `forest` is **knowledge**: notes, projects, meetings, journal, inbox. Pages are never tasks.
- `tasks` is the **task planner**: flat tasks (root or one category folder) with state,
  priority, due date and tags.
- They connect both ways: a task links notes with `[[forest:...]]`; a note links tasks with
  `[[tasks:...]]` (shown with live status) or lists them live with a block:

  ````markdown
  ```tasks
  tag: alpha-launch
  ```
  ````

  A "project page" is just a forest page doing that (the `project` template sets it up).

Interfaces:

- **Phones:** every web UI has a mobile layout (drawer + bottom bar for forest, one screen at a
  time for tasks); install it as an app from the browser menu.
- **Web UI** (`/`) - opens on an index of the vault (folders as columns, recent edits, continue where you left off, `H`), tree, markdown + mermaid, `[[links]]` across vaults, tags, history,
  local graph, outline, journal, templates, attachments, autocomplete.
- **Tasks UI** (`/tasks`) - categories, agenda (today, tomorrow, 7 days), calendar (day / week by
  the hour, month) with drag to reschedule, task times (`at`, `duration`, `remind: [10m, 1h]`), notes with
  `[[` links, "referenced in" (which notes point at a task).
- **Admin** (`/admin`) - read/write tokens, OAuth clients, audit log, backup/restore,
  clipper, reminders.
- **MCP** (`/mcp`) - 38 tools for AI assistants and agents (Streamable HTTP). Agent guide: `forest/skill/SKILL.md`.
- **Install for Claude Code** (`/install`, public) - `curl -fsSL https://<host>/install.sh | bash` adds the MCP
  server and the skill; or point Claude Code at `https://<host>/install` and it installs itself.
- **REST API** (`/api`, docs at `/api/docs`) and **git sync** (`/git/<vault>.git`).
- **CLI + TUI** clients that talk to the server over HTTPS.

Every write is a git commit authored by whoever made it (`web`, `token:laptop-agent`, ...),
so everything can be diffed and undone.

## Deploy (homeserver + nginx over VPN)

On the homeserver:

```sh
git clone <this repo> forest && cd forest
cp .env.example .env && chmod 600 .env
$EDITOR .env        # FOREST_PASSWORD, FOREST_SECRET, VAULTS_DIR, STATE_DIR, PUID/PGID,
                    # BIND_IP (VPN IP), FORWARDED_ALLOW_IPS (nginx VPN IP)
mkdir -p /srv/forest/vaults /srv/forest/state      # = VAULTS_DIR, STATE_DIR
docker compose up -d --build
docker compose logs -f forest
```

Without compose, the same thing with plain `docker run`:

```sh
docker build -t forest .
docker run -d --name forest --restart unless-stopped --env-file .env \
  --user "$(id -u):$(id -g)" --read-only --tmpfs /tmp --cap-drop ALL \
  -p 10.8.0.2:7700:7000 \
  -v /srv/forest/vaults:/data/vaults \
  -v /srv/forest/state:/data/.forest \
  forest
```

Inside the VPN you can also skip nginx and open `http://BIND_IP:PORT` directly (e.g. `http://10.8.0.2:7700`);
login works on both (the session cookie is `Secure` on HTTPS and plain on the direct http address).
Use the public HTTPS URL for OAuth connectors and anything outside the VPN.

Vaults and state live only in those host directories, never inside the container or a
Docker volume. Rebuilding or deleting the container loses nothing.

On the nginx server: point DNS `tools.example.com` at it, copy
`deploy/forest-proxy.conf` to `/etc/nginx/snippets/`, use `deploy/nginx-forest.conf`
as the site (change `10.8.0.2:7700` to `BIND_IP:PORT`), then
`certbot --nginx -d tools.example.com`.

Your existing pages: copy (or point `VAULTS_DIR` at) folders named `forest/` and `tasks/`,
e.g. `cp -r ~/org/tasks /srv/forest/vaults/tasks`. They're turned into git repos and
committed on first start. You can edit the files on the host directly too; changes are
committed on next start or with the next write.

## Maintain and update

Install the helper once on the server:

```sh
ln -sf /srv/forest/app/deploy/forestctl /usr/local/bin/forestctl    # adjust to where the repo lives
```

| command | what it does |
|---|---|
| `forestctl update` | only when there is new code (or the running image is older than the code): snapshot vaults + state, `git pull`, rebuild, restart, health check; rolls back automatically if unhealthy; prunes old images. Otherwise prints "already at latest". `--force` rebuilds anyway |
| `forestctl rollback` | back to the image that ran before the last update |
| `forestctl status` | container state, code version, pending updates on GitHub, health, disk, last backup |
| `forestctl logs` / `restart` / `shell` | the usual |
| `forestctl backup` | snapshot now (keeps the last 30) |
| `forestctl token NAME [read\|write]` / `tokens` | create / list access tokens |

Data only ever lives in `VAULTS_DIR` and `STATE_DIR`; updates never touch it.

## Install on a phone

Open the site in Chrome or Firefox and use "Install app" / "Add to Home screen". Forest and
tasks are separate apps: install from `/` for forest and from `/tasks` for tasks (own name and
checkbox icon). Each opens full screen; long-press the icon for shortcuts (today, capture / new task).
On Android Chrome it also appears in the Share menu ("forest" -> capture). The layouts adapt to
phones (incl. landscape), tablets and desktops; the back gesture closes sheets and steps back
through screens; when the server is unreachable you get an offline notice instead of an error.

## Notifications

Turn them on per device on `/admin` (open it in the installed app on your phone, "turn on for this
device"). Forest pushes straight to the app (Web Push, end-to-end encrypted, no extra app or
service): a morning digest (today's tasks with times, overdue, in progress), an evening preview of
tomorrow, a weekly overview (times editable on `/admin`, empty = off), and task reminders from a
task's "remind before" (`remind: [10m, 1h]`; date-only tasks count back from 09:00), with a "done"
button. They respect silent mode and do-not-disturb. Needs the HTTPS address; on iPhone, install
to the home screen first (iOS 16.4+). ntfy (`FOREST_NTFY_*`) is an optional second channel.

## Google Calendar

Tasks with a due date appear in a "forest" calendar in your Google account, with their time,
duration and "remind before" alerts, so your phone's calendar alarms them. Date-only tasks are
all-day events (alerts: the calendar's default all-day notifications, set in Google Calendar).
Done tasks stay as grey history. Edits made in Google (move, rename, alerts, description) come
back to the task; deleting an event removes the task's date; a new event in the forest calendar
becomes a task. forest only gets access to calendars it creates.

Setup: in Google Cloud create a project, enable the Google Calendar API, set up the OAuth consent
screen (External, publish to production; skip verification, you'll see an "unverified app" screen
once), create an OAuth client "Web application" with redirect URI
`https://<your domain>/api/google/callback`, put `FOREST_GOOGLE_CLIENT_ID` and
`FOREST_GOOGLE_CLIENT_SECRET` in `.env`, set `FOREST_CONTACT_EMAIL` (shown on `/privacy`, the
privacy policy URL for the consent screen), deploy, then "connect Google" on `/admin`.

## Connect agents

Create tokens on `/admin` (read-only by default; limit to vaults; optional expiry).

| Client | How |
|---|---|
| Claude.ai / Claude desktop | Settings > Connectors > add custom connector `https://tools.example.com/mcp`. You approve on your server (choose read/write + vaults). |
| Other MCP clients | HTTP transport, URL `https://tools.example.com/mcp`, header `Authorization: Bearer fst_...` |
| Proactive use | see below |
| Skill / guide | `GET /api/skill` or MCP prompt `skill` (source: `forest/skill/SKILL.md`) |

### Proactive mode

How much assistants use forest on their own. Default for new conversations on `/admin`; switch
per conversation by typing a keyword in your message:

| keyword | assistant |
|---|---|
| `forest: off` | uses forest only when asked |
| `forest: recall` (default) | looks up relevant notes on its own (`recall` tool), saves only when asked |
| `forest: auto` | also saves decisions, facts and todos on its own (`remember` tool) and says `saved to forest: [[...]]` |
| `forest: default auto` | changes the saved default |

Assistants learn this from the server instructions sent when they connect (new chats pick up a
changed default) and from the skill. For Claude.ai, adding one line to your profile preferences
("Use the forest connector proactively, following its mode") makes it more reliable.

## Clients on your laptop

```sh
bash install.sh --tool          # installs the `forest` command (uv tool)
forest login --url https://tools.example.com   # paste a token
forest tui                      # full-screen client
forest agenda | forest today "did X" | forest capture "idea" | forest search "..."
forest show tasks:work/report | forest edit auth-design | forest link auth-design
```

## Backups, offline, sync

- `forest backup ~/backups` (or the button on `/admin`) downloads a tar.gz of all vaults
  with full git history. Restore on `/admin` or locally with `forest restore file.tar.gz --data DIR`
  (current contents are moved aside, never deleted).
- Offline: `forest clone ~/forest` git-clones each vault; run a local server on them
  (`FOREST_VAULT_FOREST=~/forest/forest FOREST_VAULT_TASKS=~/forest/tasks forest serve`)
  or a local stdio MCP (`forest mcp`). `forest sync ~/forest` pulls then pushes.
  Plain git also works: `git clone https://tools.../git/forest.git` (token as password,
  push needs a write token, fast-forward only).

## Security model

- Web: password login (rate limited, 30-day `Secure`/`HttpOnly`/`SameSite=Lax` cookie).
  CSRF header on writes, CSP, and sanitized markdown (agents can write pages, so rendered
  HTML is sanitized with DOMPurify).
- Tokens: `fst_...`, stored hashed; `read` or `write`; optional vault limit and expiry;
  revocable on `/admin`. Tokens can't reach admin endpoints or the UI.
- OAuth 2.1 (PKCE, dynamic registration) only for connectors; the tokens it issues are
  normal revocable tokens.
- `ai: readonly` in a page's (or folder's `index.md`) frontmatter blocks all token writes
  there; your web session can still edit.
- Audit log (`/admin`): logins, failures, every token/API/MCP call, git pushes, token changes.
- Container: non-root, read-only root filesystem, no capabilities, bound to the VPN IP only.

## Features at a glance

Daily notes (`journal/YYYY-MM-DD.md`, `T` key), templates (`forest:_templates/*.md`,
`{{title}} {{date}} {{time}} {{weekday}}`), `[[`/`#` autocomplete, unlinked mentions with
one-click linking, attachments (drag/paste; PDFs searchable), local graph, outline and
`[[page#heading]]`, calendar + `.ics` feed (`/api/calendar.ics?token=<read token>`),
ntfy reminders (daily digest, weekly overview, per-page `remind:` with done/snooze
buttons), web clipper (bookmarklet on `/admin`, Android share target via the installable
PWA, optional article snapshot), permalinks (`/#vault/path`, `/tasks#vault/path`).

## Local development

```sh
bash install.sh && ./start.sh          # http://127.0.0.1:7000, data in ~/org/forest-data
```

Configuration: environment variables (see `.env.example`), or `forest.toml`
(see `config.sample.toml`). Env wins.

## Tests

```sh
VIRTUAL_ENV=.venv uv pip install -e ".[dev]"     # pytest + playwright (browser tests need Chromium)
.venv/bin/python -m pytest -q                     # ~1 min, starts its own throwaway server
deploy/smoke.sh https://tools.example.com fst_...   # after deploying, through nginx
```

The end-to-end suite (`tests/`) drives a real server: web auth and attacks (open redirect,
CSRF, path traversal, XSS, tar-slip), tokens and vault limits, every REST route, every MCP
tool through the official MCP client, the OAuth connector flow, git clone/push/pull,
backup/restore, ntfy reminders (fake ntfy server), CLI, TUI, and the web UIs in Chromium.

