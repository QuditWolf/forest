"""ntfy push notifications: daily digest, weekly overview, per-page `remind:` reminders.

Runs as an asyncio loop inside the server (no extra service). Action buttons ("done",
"snooze 1d") call back into /api/remind/action/<signed token>, so no API token is ever
sent to ntfy.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import Optional

import httpx
from itsdangerous import BadSignature, URLSafeTimedSerializer

from . import auth, store
from .config import (DIGEST_TIME, NTFY_TOKEN, NTFY_TOPIC, NTFY_URL, PUBLIC_URL, STATE_DIR,
                     WEEKLY_TIME, session_secret)

log = logging.getLogger("forest.reminders")
_signer: Optional[URLSafeTimedSerializer] = None
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def enabled() -> bool:
    return bool(NTFY_URL and NTFY_TOPIC)


def signer() -> URLSafeTimedSerializer:
    global _signer
    if _signer is None:
        _signer = URLSafeTimedSerializer(session_secret(), salt="forest-remind")
    return _signer


def action_url(vault: str, path: str, action: str) -> str:
    tok = signer().dumps({"v": vault, "p": path, "a": action})
    return f"{PUBLIC_URL}/api/remind/action/{tok}"


def run_action(token: str) -> dict:
    try:
        d = signer().loads(token, max_age=14 * 86400)
    except BadSignature:
        raise ValueError("invalid or expired action link")
    v = store.vault(d["v"])
    tok = store.actor.set("ntfy")
    try:
        if d["a"] == "done":
            v.update_page(d["p"], state="done")
        elif d["a"] == "snooze":
            from .features import now
            v.update_page(d["p"], extra={"remind": (now() + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")})
            _state_update(lambda s: s["sent"].pop(f"{d['v']}:{d['p']}", None))
        else:
            raise ValueError("unknown action")
    finally:
        store.actor.reset(tok)
    auth.audit("remind.action", vault=d["v"], path=d["p"], action=d["a"])
    return {"ok": True, "action": d["a"], "page": f"{d['v']}:{d['p']}"}


def send(title: str, message: str, click: Optional[str] = None, actions: Optional[list] = None,
         tags: Optional[list] = None, priority: int = 3) -> None:
    if not enabled():
        raise RuntimeError("ntfy not configured (FOREST_NTFY_URL / FOREST_NTFY_TOPIC)")
    body = {"topic": NTFY_TOPIC, "title": title, "message": message[:3900], "priority": priority}
    if click:
        body["click"] = click
    if actions:
        body["actions"] = actions
    if tags:
        body["tags"] = tags
    headers = {"Authorization": f"Bearer {NTFY_TOKEN}"} if NTFY_TOKEN else {}
    r = httpx.post(NTFY_URL, json=body, headers=headers, timeout=15)
    r.raise_for_status()


# ── state (what has been sent) ────────────────────────────────────────────────

def _state() -> dict:
    f = STATE_DIR / "reminders.json"
    s = json.loads(f.read_text()) if f.exists() else {}
    s.setdefault("sent", {})
    return s


def _state_update(fn) -> None:
    s = _state()
    fn(s)
    (STATE_DIR / "reminders.json").write_text(json.dumps(s, indent=1))


# ── messages ──────────────────────────────────────────────────────────────────

def _lines(items: list, n: int = 8) -> str:
    out = [f"- {p['name']}" + (f" (due {p['due']})" if p.get("due") else "") for p in items[:n]]
    if len(items) > n:
        out.append(f"  +{len(items) - n} more")
    return "\n".join(out)


def daily_digest() -> None:
    from .features import review
    r = review("day")
    parts = []
    if r["overdue"]:
        parts.append(f"Overdue ({len(r['overdue'])}):\n{_lines(r['overdue'])}")
    if r["due_soon"]:
        parts.append(f"Due soon ({len(r['due_soon'])}):\n{_lines(r['due_soon'])}")
    if r["in_progress"]:
        parts.append(f"In progress ({len(r['in_progress'])}):\n{_lines(r['in_progress'], 5)}")
    parts.append(f"Inbox: {r['inbox_count']}")
    send(f"forest: {r['today']}", "\n\n".join(parts), click=f"{PUBLIC_URL}/tasks", tags=["calendar"])


def weekly_overview() -> None:
    from .features import review
    r = review("week")
    nxt = [p for p in store.agenda(due_within="7d") if p not in r["overdue"]]
    parts = [f"Done this week: {len(r['done'])}"]
    if r["overdue"]:
        parts.append(f"Slipped ({len(r['overdue'])}):\n{_lines(r['overdue'])}")
    if nxt:
        parts.append(f"Due next 7 days ({len(nxt)}):\n{_lines(nxt)}")
    if r["stale"]:
        parts.append(f"Stale ({len(r['stale'])}):\n{_lines(r['stale'], 5)}")
    parts.append(f"Changes: {r['changes']['by_you']} by you, {r['changes']['by_agents']} by agents")
    send("forest: weekly overview", "\n\n".join(parts), click=f"{PUBLIC_URL}/tasks", tags=["bar_chart"])


def _parse_remind(val) -> Optional[datetime]:
    from .features import TZ
    if isinstance(val, datetime):
        dt = val
    else:
        try:
            dt = datetime.fromisoformat(str(val).strip().replace(" ", "T"))
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=TZ)


def due_reminders() -> None:
    from .features import now, page_url
    sent = _state()["sent"]
    t = now()
    for v in store.VAULTS.values():
        for p in v.all_pages():
            val = p.extra.get("remind")
            if not val or p.state == "done":
                continue
            when = _parse_remind(val)
            key = f"{v.name}:{p.path}"
            if not when or when > t or sent.get(key) == str(val):
                continue
            msg = p.name + (f"\ndue {p.due}" if p.due else "") + (f"\n{p.state}" if p.state else "")
            send(f"reminder: {p.name}", msg, click=page_url(v.name, p.path), tags=["bell"], priority=4,
                 actions=[{"action": "http", "label": "done", "url": action_url(v.name, p.path, "done"),
                           "method": "POST", "clear": True},
                          {"action": "http", "label": "snooze 1d", "url": action_url(v.name, p.path, "snooze"),
                           "method": "POST", "clear": True}])
            _state_update(lambda s, k=key, val=str(val): s["sent"].__setitem__(k, val))
            auth.audit("remind.sent", vault=v.name, path=p.path)


def _due_now(spec: str, t: datetime, key: str, weekly: bool = False) -> bool:
    """spec 'HH:MM' or 'sun HH:MM'; fires once per day/week after that time."""
    if not spec:
        return False
    parts = spec.lower().split()
    if weekly:
        if len(parts) != 2 or parts[0][:3] not in DAYS or DAYS.index(parts[0][:3]) != t.weekday():
            return False
        hhmm = parts[1]
    else:
        hhmm = parts[-1]
    h, m = (int(x) for x in hhmm.split(":"))
    if (t.hour, t.minute) < (h, m):
        return False
    stamp = t.strftime("%G-W%V") if weekly else t.date().isoformat()
    s = _state()
    if s.get(key) == stamp:
        return False
    _state_update(lambda st: st.__setitem__(key, stamp))
    return True


async def loop() -> None:
    from .features import now
    if not enabled():
        return
    log.info("ntfy reminders enabled → %s/%s", NTFY_URL, NTFY_TOPIC)
    while True:
        try:
            t = now()
            if _due_now(DIGEST_TIME, t, "digest"):
                await asyncio.to_thread(daily_digest)
            if _due_now(WEEKLY_TIME, t, "weekly", weekly=True):
                await asyncio.to_thread(weekly_overview)
            await asyncio.to_thread(due_reminders)
        except Exception as e:  # keep the loop alive
            log.warning("reminder loop: %s", e)
        await asyncio.sleep(60)
