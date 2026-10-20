"""Two-way sync between the tasks vault and a "forest" calendar in the user's Google account.

forest -> Google (the main direction): every task with a due date is an event (timed: at +
duration + the task's `remind` offsets as popup reminders; date-only: all-day, the calendar's
default reminders). Done tasks stay as history: "✓ name", grey, no reminders, not busy.
Google -> forest (incremental, sync tokens): moving an event, renaming it or changing its
reminders updates the task; deleting it removes the task's date; a new event created in the
forest calendar becomes a task.

OAuth scope is calendar.app.created: forest can only touch calendars it created, nothing else.
State (refresh token, calendar id, event map) lives in STATE_DIR/google.json, mode 0600.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import threading
import time
from datetime import date, datetime, timedelta
from typing import Optional
from urllib.parse import urlencode

import httpx

from . import auth, store
from .config import GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, PUBLIC_URL, STATE_DIR, TIMEZONE

log = logging.getLogger("forest.gcal")
_TEST = os.environ.get("FOREST_GOOGLE_TEST_BASE", "").rstrip("/")     # fake Google in the e2e tests
AUTH_URL = (_TEST + "/auth") if _TEST else "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = (_TEST + "/token") if _TEST else "https://oauth2.googleapis.com/token"
REVOKE_URL = (_TEST + "/revoke") if _TEST else "https://oauth2.googleapis.com/revoke"
API = (_TEST + "/calendar/v3") if _TEST else "https://www.googleapis.com/calendar/v3"
SCOPES = "openid email https://www.googleapis.com/auth/calendar.app.created"
DEFAULT_MINUTES = 30
_lock = threading.RLock()
_pending: dict = {}          # oauth state -> (pkce verifier, created)
_access: dict = {}           # cached access token


class ReconnectNeeded(Exception):
    pass


def configured() -> bool:
    return bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)


def redirect_uri() -> str:
    return f"{PUBLIC_URL}/api/google/callback"


# ── state ─────────────────────────────────────────────────────────────────────

def _file():
    return STATE_DIR / "google.json"


def _load() -> dict:
    try:
        return json.loads(_file().read_text())
    except Exception:
        return {}


def _save(s: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _file().with_suffix(".tmp")
    tmp.write_text(json.dumps(s, indent=1))
    os.chmod(tmp, 0o600)
    tmp.replace(_file())


def status() -> dict:
    s = _load()
    return {"configured": configured(), "connected": bool(s.get("refresh_token")), "email": s.get("email"),
            "calendar": bool(s.get("calendar_id")), "last_sync": s.get("last_sync"), "error": s.get("error"),
            "events": len(s.get("map", {})), "redirect_uri": redirect_uri() if configured() else None}


# ── OAuth (authorization code + PKCE, offline access) ─────────────────────────

def auth_url() -> str:
    if not configured():
        raise ValueError("set FOREST_GOOGLE_CLIENT_ID and FOREST_GOOGLE_CLIENT_SECRET first")
    st, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(48)
    now = time.time()
    for k in [k for k, (_, t) in _pending.items() if now - t > 900]:
        _pending.pop(k, None)
    _pending[st] = (verifier, now)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return AUTH_URL + "?" + urlencode({
        "client_id": GOOGLE_CLIENT_ID, "redirect_uri": redirect_uri(), "response_type": "code", "scope": SCOPES,
        "access_type": "offline", "prompt": "consent", "include_granted_scopes": "false", "state": st,
        "code_challenge": challenge, "code_challenge_method": "S256"})


def _email_from(id_token: Optional[str]) -> Optional[str]:
    try:   # straight from Google's token endpoint over TLS: only reading the email claim
        part = id_token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))).get("email")
    except Exception:
        return None


def finish(code: str, state: str) -> dict:
    pending = _pending.pop(state, None)
    if not pending or time.time() - pending[1] > 900:
        raise ValueError("this Google sign-in link expired; start again from /admin")
    r = httpx.post(TOKEN_URL, data={"grant_type": "authorization_code", "code": code, "client_id": GOOGLE_CLIENT_ID,
                                    "client_secret": GOOGLE_CLIENT_SECRET, "redirect_uri": redirect_uri(),
                                    "code_verifier": pending[0]}, timeout=20)
    if r.status_code != 200:
        raise ValueError(f"Google refused the sign-in: {r.text[:200]}")
    tok = r.json()
    if not tok.get("refresh_token"):
        raise ValueError("Google sent no refresh token; remove forest at myaccount.google.com/permissions and connect again")
    with _lock:
        s = _load()
        same = s.get("calendar_id") and s.get("email") == _email_from(tok.get("id_token"))
        s.update(refresh_token=tok["refresh_token"], email=_email_from(tok.get("id_token")), error=None)
        if not same:
            s.update(calendar_id=None, map={}, sync_token=None)
        _save(s)
        _access.clear()
        _access.update(token=tok.get("access_token"), exp=time.time() + int(tok.get("expires_in", 3600)) - 60)
    auth.audit("google.connect", email=s.get("email"))
    sync()
    return status()


def disconnect() -> None:
    with _lock:
        s = _load()
        if s.get("refresh_token"):
            try:
                httpx.post(REVOKE_URL, data={"token": s["refresh_token"]}, timeout=10)
            except httpx.HTTPError:
                pass
        # keep which calendar is ours, so reconnecting the same account doesn't make a second one
        _save({k: s[k] for k in ("calendar_id", "email", "map", "sync_token") if k in s})
        _access.clear()
    auth.audit("google.disconnect")


def _token() -> str:
    if _access.get("token") and _access.get("exp", 0) > time.time():
        return _access["token"]
    s = _load()
    if not s.get("refresh_token"):
        raise ReconnectNeeded("not connected")
    r = httpx.post(TOKEN_URL, data={"grant_type": "refresh_token", "refresh_token": s["refresh_token"],
                                    "client_id": GOOGLE_CLIENT_ID, "client_secret": GOOGLE_CLIENT_SECRET}, timeout=20)
    if r.status_code in (400, 401) and "invalid_grant" in r.text:
        raise ReconnectNeeded("Google ended the connection (expired or revoked): reconnect on /admin")
    r.raise_for_status()
    tok = r.json()
    _access.update(token=tok["access_token"], exp=time.time() + int(tok.get("expires_in", 3600)) - 60)
    return _access["token"]


def _api(method: str, path: str, **kw) -> httpx.Response:
    for attempt in (0, 1):
        r = httpx.request(method, API + path, headers={"Authorization": f"Bearer {_token()}"}, timeout=30, **kw)
        if r.status_code == 401 and attempt == 0:
            _access.clear()
            continue
        return r
    return r


# ── task <-> event ────────────────────────────────────────────────────────────

DONE = "✓ "


def _sig(name, due, at, minutes, remind, done=False) -> list:
    """What both sides agree on; equal signatures mean nothing to sync."""
    return [name, str(due) if due else None, at or None, int(minutes) if at else None,
            sorted(store.parse_duration(x) for x in remind) if (at and not done and isinstance(remind, list)) else None,
            bool(done)]


def _task_sig(p) -> list:
    at = p.extra.get("at")
    mins = store.parse_duration(p.extra["duration"]) if p.extra.get("duration") else DEFAULT_MINUTES
    return _sig(p.name, p.due, at, mins, p.extra.get("remind"), p.state == "done")


FOOTER = "\n\nforest: "


def _notes(p) -> str:
    """Task notes for the event description (plain, trimmed)."""
    text = (p.content or "").strip()
    return text if len(text) <= 1500 else text[:1500].rsplit("\n", 1)[0] + "\n..."


def _description(p, url: str) -> str:
    meta = " · ".join(x for x in (p.priority and f"priority {p.priority}", p.tags and " ".join("#" + t for t in p.tags)) if x)
    return _notes(p) + FOOTER + url + (f"\n{meta}" if meta else "")


def _user_text(desc: str) -> str:
    """The free-text part of a description (Google's editor turns it into HTML)."""
    if "<" in desc and ">" in desc:
        from markdownify import markdownify
        desc = markdownify(desc.replace("<br>", "\n"), strip=["a"]) if "<" in desc else desc
    i = desc.rfind("forest: ")
    return " ".join((desc[:i] if i >= 0 else desc).split())


def _h(x) -> str:
    return hashlib.sha256(json.dumps(x, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _event_body(p, key: str) -> dict:
    sig = _task_sig(p)
    _, due, at, minutes, remind, done = sig
    url = f"{PUBLIC_URL}/tasks#{key.replace(':', '/', 1)}"
    body = {"summary": (DONE if done else "") + p.name, "description": _description(p, url),
            "extendedProperties": {"private": {"forest": key}}, "source": {"title": "forest", "url": url}}
    if at:
        h, m = map(int, at.split(":"))
        start = datetime(p.due.year, p.due.month, p.due.day, h, m)
        body["start"] = {"dateTime": start.isoformat(timespec="seconds"), "timeZone": TIMEZONE}
        body["end"] = {"dateTime": (start + timedelta(minutes=minutes)).isoformat(timespec="seconds"), "timeZone": TIMEZONE}
        body["reminders"] = ({"useDefault": False, "overrides": [{"method": "popup", "minutes": x} for x in remind]}
                             if remind is not None else {"useDefault": True})
        body["transparency"] = "opaque"
    else:
        body["start"] = {"date": p.due.isoformat()}
        body["end"] = {"date": (p.due + timedelta(days=1)).isoformat()}
        body["reminders"] = {"useDefault": True}
        body["transparency"] = "transparent"
    if done:      # history: no alerts, not busy, grey
        body.update(reminders={"useDefault": False, "overrides": []}, transparency="transparent", colorId="8")
    return body


def _event_fields(ev: dict) -> dict:
    """Event -> task fields (in the server timezone)."""
    from .features import TZ
    st, en = ev.get("start", {}), ev.get("end", {})
    summary = ev.get("summary") or "untitled"
    done = summary.startswith(DONE)
    ev = {**ev, "summary": summary[len(DONE):] if done else summary}
    if "dateTime" in st:
        s = datetime.fromisoformat(st["dateTime"].replace("Z", "+00:00"))
        e = datetime.fromisoformat(en["dateTime"].replace("Z", "+00:00"))
        if s.tzinfo:
            s, e = s.astimezone(TZ), e.astimezone(TZ)
        minutes = max(1, int((e - s).total_seconds() // 60))
        rem = ev.get("reminders") or {}
        remind = None if rem.get("useDefault", True) else \
            [store.fmt_duration(o["minutes"]) for o in rem.get("overrides", []) if o.get("method") == "popup"] or None
        return {"name": ev["summary"], "due": s.date(), "at": s.strftime("%H:%M"),
                "minutes": minutes, "remind": None if done else remind, "done": done}
    d = date.fromisoformat(st["date"])
    return {"name": ev["summary"], "due": d, "at": None, "minutes": None, "remind": None, "done": done}


def _fields_sig(f: dict) -> list:
    return _sig(f["name"], f["due"], f["at"], f["minutes"] or DEFAULT_MINUTES, f["remind"], f["done"])


# ── sync ──────────────────────────────────────────────────────────────────────

def _task_vault() -> Optional[store.Vault]:
    return next((v for v in store.VAULTS.values() if v.is_tasks), None)


def _ensure_calendar(s: dict) -> str:
    if s.get("calendar_id"):
        return s["calendar_id"]
    r = _api("POST", "/calendars", json={"summary": "forest", "description": f"Tasks from {PUBLIC_URL}/tasks",
                                         "timeZone": TIMEZONE})
    r.raise_for_status()
    s.update(calendar_id=r.json()["id"], map={}, sync_token=None)
    _save(s)
    return s["calendar_id"]


def _pull(s: dict, cal: str, v: store.Vault) -> int:
    """Apply changes made in Google. Returns how many tasks changed."""
    from urllib.parse import quote
    changed, page_token = 0, None
    params = {"singleEvents": "false", "showDeleted": "true", "maxResults": "250"}
    if s.get("sync_token"):
        params["syncToken"] = s["sync_token"]
    by_event = {m["id"]: k for k, m in s["map"].items()}
    while True:
        r = _api("GET", f"/calendars/{quote(cal, safe='')}/events", params={**params, **({"pageToken": page_token} if page_token else {})})
        if r.status_code == 410:          # sync token expired: start over with a full listing
            s["sync_token"] = None
            params.pop("syncToken", None)
            continue
        r.raise_for_status()
        data = r.json()
        for ev in data.get("items", []):
            key = (ev.get("extendedProperties") or {}).get("private", {}).get("forest") or by_event.get(ev["id"])
            m = s["map"].get(key) if key else None
            if ev.get("status") == "cancelled":
                if m and m["id"] == ev["id"] and not m.get("gone"):
                    vn, path = key.split(":", 1)
                    try:
                        p = store.vault(vn).get_page(path)
                        if p.state == "done":           # history entry removed: fine, don't bring it back
                            m["gone"] = True
                        else:                           # deleted in Google: unschedule the task
                            s["map"].pop(key)
                            tok = store.actor.set("google-calendar")
                            try:
                                store.vault(vn).update_page(path, due=None, extra={"at": None})
                            finally:
                                store.actor.reset(tok)
                            changed += 1
                    except (FileNotFoundError, ValueError, KeyError):
                        s["map"].pop(key, None)
                continue
            if ev.get("recurringEventId") or ev.get("recurrence"):
                continue                        # recurring events are not tasks
            try:
                f = _event_fields(ev)
                if not key and len(f["remind"] or []) > 5:
                    f["remind"] = f["remind"][:5]
            except (KeyError, ValueError):
                continue
            if not key:                         # created in Google: becomes a task
                p = v.create_page(None, f["name"], state="todo", due=f["due"],
                                  content=_user_text(ev.get("description") or "") + "\n" if ev.get("description") else "",
                                  extra={k: x for k, x in (("at", f["at"]),
                                         ("duration", store.fmt_duration(f["minutes"]) if f["at"] and f["minutes"] != DEFAULT_MINUTES else None),
                                         ("remind", f["remind"])) if x})
                key = f"{v.name}:{p.path}"
                _api("PATCH", f"/calendars/{quote(cal, safe='')}/events/{ev['id']}",
                     json={"extendedProperties": {"private": {"forest": key}}})
                s["map"][key] = {"id": ev["id"], "sig": _task_sig(p)}
                changed += 1
                continue
            if not m:
                continue                        # an event of ours we no longer track
            new = _fields_sig(f)
            note = _user_text(ev.get("description") or "")
            note_changed = "dh" in m and _h(note) != m["dh"]
            if new == m["sig"] and not note_changed:
                continue                        # our own write coming back
            vn, path = key.split(":", 1)
            try:
                p = store.vault(vn).get_page(path)
            except (FileNotFoundError, ValueError, KeyError):
                continue
            if p.state == "done":
                continue                        # history: Google edits don't change done tasks
            upd: dict = {}
            extra: dict = {}
            if f["name"] != p.name:
                upd["name"] = f["name"]
            if f["due"] != p.due:
                upd["due"] = f["due"]
            if (f["at"] or None) != (p.extra.get("at") or None):
                extra["at"] = f["at"]
            if f["at"]:
                cur = store.parse_duration(p.extra["duration"]) if p.extra.get("duration") else DEFAULT_MINUTES
                if f["minutes"] != cur:
                    extra["duration"] = store.fmt_duration(f["minutes"])
                if (f["remind"] or None) != (p.extra.get("remind") if isinstance(p.extra.get("remind"), list) else None):
                    extra["remind"] = f["remind"]
            if extra:
                upd["extra"] = extra
            if upd or note_changed:
                tok = store.actor.set("google-calendar")
                try:
                    if upd:
                        p = store.vault(vn).update_page(path, **upd)
                    if note_changed and note and note != " ".join(_notes(p).split()):
                        p = store.vault(vn).append_to_page(path, "edited in Google Calendar:\n\n" + note)
                finally:
                    store.actor.reset(tok)
                changed += 1
            m["sig"] = _task_sig(p)
            m["dh"] = _h(note)
            m.pop("bh", None)                   # re-send our full description on the next push
        page_token = data.get("nextPageToken")
        if not page_token:
            s["sync_token"] = data.get("nextSyncToken") or s.get("sync_token")
            return changed


def _push(s: dict, cal: str) -> int:
    """Make the calendar match the tasks. Returns how many events changed."""
    from urllib.parse import quote
    base = f"/calendars/{quote(cal, safe='')}/events"
    want = {}
    for v in store.VAULTS.values():
        if not v.is_tasks:
            continue
        for p in v.all_pages():
            if p.due and p.state and not p.is_folder:
                want[f"{v.name}:{p.path}"] = p
    n = 0
    for key, p in want.items():
        m, sig = s["map"].get(key), _task_sig(p)
        body = _event_body(p, key)
        bh = _h(body)
        if m and m["sig"] == sig and m.get("bh") == bh:
            continue                            # unchanged (or a history entry the user deleted)
        if m and m.get("gone"):
            m = None                            # changed since: put it back
        r = _api("PUT", f"{base}/{m['id']}", json=body) if m else _api("POST", base, json=body)
        if m and r.status_code in (404, 410):       # gone on Google's side: create again
            r = _api("POST", base, json=body)
        r.raise_for_status()
        s["map"][key] = {"id": r.json()["id"], "sig": sig, "bh": bh, "dh": _h(_user_text(body["description"]))}
        n += 1
    for key in [k for k in s["map"] if k not in want]:
        r = _api("DELETE", f"{base}/{s['map'][key]['id']}")
        if r.status_code not in (200, 204, 404, 410):
            r.raise_for_status()
        s["map"].pop(key)
        n += 1
    return n


def sync() -> dict:
    """One round: pull Google's changes, then push forest's. Safe to call any time."""
    if not configured():
        return {"skipped": "not configured"}
    with _lock:
        s = _load()
        if not s.get("refresh_token"):
            return {"skipped": "not connected"}
        s.setdefault("map", {})
        v = _task_vault()
        if not v:
            return {"skipped": "no tasks vault"}
        try:
            cal = _ensure_calendar(s)
            pulled = _pull(s, cal, v)
            pushed = _push(s, cal)
            s.update(last_sync=datetime.now().astimezone().isoformat(timespec="seconds"), error=None)
            _save(s)
            return {"pulled": pulled, "pushed": pushed}
        except ReconnectNeeded as e:
            first = s.get("error") != str(e)
            s["error"] = str(e)
            _save(s)
            if first:
                auth.audit("google.reconnect_needed")
                try:
                    from . import reminders
                    reminders.send("forest: reconnect Google Calendar", "Google ended forest's calendar access. "
                                   "Tasks are fine; open /admin and press reconnect.", click=f"{PUBLIC_URL}/admin#google")
                except Exception:
                    pass
            return {"error": str(e)}
        except httpx.HTTPError as e:
            s["error"] = f"sync failed: {str(e)[:160]}"
            _save(s)
            log.warning("google sync: %s", e)
            return {"error": s["error"]}


async def loop() -> None:
    import asyncio
    while True:
        await asyncio.sleep(60)
        if configured() and _load().get("refresh_token"):
            try:
                await asyncio.to_thread(sync)
            except Exception as e:      # keep the loop alive
                log.warning("google sync loop: %s", e)
