"""Google Calendar sync against a fake Google (OAuth + Calendar API): forest -> Google is the main
direction; Google -> forest picks up manual edits (time, alerts, title, description)."""

from datetime import date, timedelta
from urllib.parse import parse_qs, urlparse


def connect(web, google):
    r = web.get("/api/google/connect", follow_redirects=False)
    assert r.status_code == 302
    u = urlparse(r.headers["location"])
    q = {k: v[0] for k, v in parse_qs(u.query).items()}
    assert u.path == "/auth" and q["access_type"] == "offline" and q["code_challenge_method"] == "S256"
    assert "calendar.app.created" in q["scope"] and "auth/calendar " not in q["scope"] + " "   # narrow scope only
    assert q["redirect_uri"].endswith("/api/google/callback")
    r = web.get("/api/google/callback", params={"code": "good", "state": q["state"]}, follow_redirects=False)
    assert r.headers["location"] == "/admin?google=connected#google", r.headers["location"]
    return q


def test_google_calendar_sync(server, web, agent, google):
    st = web.get("/api/google").json()
    assert st["configured"] and not st["connected"]
    assert agent.get("/api/google").status_code == 403                  # tokens can't touch it
    # a forged/unknown state is refused
    r = web.get("/api/google/callback", params={"code": "good", "state": "forged"}, follow_redirects=False)
    assert "expired" in r.headers["location"]

    d = date.today() + timedelta(days=3)
    agent.post("/api/tasks/pages", json={"name": "Dentist", "state": "todo", "priority": "high", "due": d.isoformat(),
                                         "at": "14:00", "duration": "45m", "remind": ["10m", "1h"],
                                         "content": "bring the forms\n"})
    agent.post("/api/tasks/pages", json={"name": "Pay rent", "state": "todo", "due": d.isoformat()})
    agent.post("/api/tasks/pages", json={"name": "Old thing", "state": "done", "due": (d - timedelta(days=10)).isoformat()})
    agent.post("/api/tasks/pages", json={"name": "Someday", "state": "todo"})

    connect(web, google)                                                 # connecting syncs right away
    st = web.get("/api/google").json()
    assert st["connected"] and st["email"] == "me@gmail.com" and st["calendar"] and not st["error"]
    cal = google.calendars[next(iter(google.calendars))]
    assert cal["summary"] == "forest" and cal["timeZone"] == "UTC"

    ev = google.by_key("tasks:dentist.md")
    assert ev["summary"] == "Dentist" and ev["start"] == {"dateTime": f"{d}T14:00:00", "timeZone": "UTC"}
    assert ev["end"]["dateTime"] == f"{d}T14:45:00"
    assert ev["reminders"] == {"useDefault": False, "overrides": [{"method": "popup", "minutes": 10},
                                                                  {"method": "popup", "minutes": 60}]}
    assert ev["description"].startswith("bring the forms") and "/tasks#tasks/dentist.md" in ev["description"]
    assert "priority high" in ev["description"]
    rent = google.by_key("tasks:pay-rent.md")
    assert rent["start"] == {"date": d.isoformat()} and rent["reminders"] == {"useDefault": True}
    old = google.by_key("tasks:old-thing.md")                           # done: kept as history
    assert old["summary"] == "✓ Old thing" and old["reminders"]["overrides"] == [] and old["colorId"] == "8"
    assert google.by_key("tasks:someday.md") is None                    # no date, no event

    # idempotent: nothing to do on the next round
    assert web.post("/api/google/sync").json() == {"pulled": 0, "pushed": 0}

    # forest -> Google: reschedule + mark done
    agent.patch("/api/tasks/page/pay-rent.md", json={"at": "09:00"})
    web.post("/api/google/sync")
    assert google.by_key("tasks:pay-rent.md")["start"]["dateTime"] == f"{d}T09:00:00"
    agent.patch("/api/tasks/page/pay-rent.md", json={"state": "done"})
    web.post("/api/google/sync")
    assert google.by_key("tasks:pay-rent.md")["summary"] == "✓ Pay rent"

    # Google -> forest: move the event + change its alerts by hand
    eid = google.by_key("tasks:dentist.md")["id"]
    google.edit(eid, start={"dateTime": f"{d + timedelta(days=1)}T16:30:00+00:00"},
                end={"dateTime": f"{d + timedelta(days=1)}T17:30:00+00:00"},
                reminders={"useDefault": False, "overrides": [{"method": "popup", "minutes": 5}]})
    r = web.post("/api/google/sync").json()
    assert r["pulled"] == 1
    t = agent.get("/api/tasks/page/dentist.md").json()
    assert t["due"] == (d + timedelta(days=1)).isoformat() and t["extra"]["at"] == "16:30"
    assert t["extra"]["duration"] == "1h" and t["extra"]["remind"] == ["5m"]
    web.post("/api/google/sync")
    assert web.post("/api/google/sync").json() == {"pulled": 0, "pushed": 0}       # settled, no ping-pong

    # Google -> forest: description edited in Google's editor (HTML) -> added to the notes, nothing lost
    google.edit(eid, description="bring the forms<br>and the insurance card<br><br>forest: <a href='x'>link</a>")
    web.post("/api/google/sync")
    body = agent.get("/api/tasks/page/dentist.md").json()["content"]
    assert "bring the forms" in body and "edited in Google Calendar" in body and "insurance card" in body
    web.post("/api/google/sync")
    assert web.post("/api/google/sync").json() == {"pulled": 0, "pushed": 0}

    # a new event made in the forest calendar becomes a task (and gets linked)
    cal_id = next(iter(google.calendars))
    new = google.add(cal_id, {"summary": "Call Asha", "start": {"dateTime": f"{d}T11:00:00Z"},
                              "end": {"dateTime": f"{d}T11:15:00Z"}, "description": "about the move"})
    web.post("/api/google/sync")
    t = agent.get("/api/tasks/page/call-asha.md").json()
    assert t["state"] == "todo" and t["due"] == d.isoformat() and t["extra"]["at"] == "11:00"
    assert t["extra"]["duration"] == "15m" and "about the move" in t["content"]
    assert google.public(new["id"])["extendedProperties"]["private"]["forest"] == "tasks:call-asha.md"

    # deleted in Google: an open task loses its date (not re-created); a done one stays done
    google.delete(google.by_key("tasks:call-asha.md")["id"])
    web.post("/api/google/sync")
    t = agent.get("/api/tasks/page/call-asha.md").json()
    assert t["due"] is None and "at" not in t["extra"] and google.by_key("tasks:call-asha.md") is None
    google.delete(google.by_key("tasks:old-thing.md")["id"])
    web.post("/api/google/sync"); web.post("/api/google/sync")
    assert agent.get("/api/tasks/page/old-thing.md").json()["state"] == "done"
    assert google.by_key("tasks:old-thing.md") is None                   # history entry stays deleted

    # Google ends the connection: status says reconnect, a notification goes out once, tasks untouched
    google.revoke_all()
    r = web.post("/api/google/sync").json()
    assert "reconnect" in r["error"]
    st = web.get("/api/google").json()
    assert st["connected"] and "reconnect" in st["error"]
    assert agent.get("/api/tasks/page/dentist.md").json()["extra"]["at"] == "16:30"
    cals = len(google.calendars)
    web.delete("/api/google")
    assert not web.get("/api/google").json()["connected"]
    connect(web, google)                                                 # same account: same calendar, no duplicate
    st = web.get("/api/google").json()
    assert st["connected"] and not st["error"] and len(google.calendars) == cals
    assert web.post("/api/google/sync").json() == {"pulled": 0, "pushed": 0}
