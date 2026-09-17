"""The calendar: events Bantu keeps, plus the user's Google Calendar, read-only.

Google Calendar is read through its "secret address in iCal format": no sign-in,
no OAuth app, no server. That address grants read access to the calendar, so it
is stored like a key, in the credential store. Feed events are copied into a
rolling window and refreshed, never edited here - changes happen in Google.

Verified live 2026-09-17: Google's public "Holidays in Nepal" feed fetched in
0.7s; weekly recurrence with an excluded date, UTC-to-local conversion and
all-day events all expand correctly with icalendar + recurring-ical-events.

Portable: stdlib plus two small pure-Python libraries.
"""

from __future__ import annotations

import datetime
import logging
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

from .tools.registry import Tier, ToolError, ToolRegistry

log = logging.getLogger("bantu.events")

FEED_PAST_DAYS = 1
FEED_FUTURE_DAYS = 60
FEED_POLL_S = 15 * 60
MAX_FEED_BYTES = 20 * 1024 * 1024

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    starts_at   REAL NOT NULL,
    ends_at     REAL,
    all_day     INTEGER NOT NULL DEFAULT 0,
    location    TEXT NOT NULL DEFAULT '',
    notes       TEXT NOT NULL DEFAULT '',
    source      TEXT NOT NULL DEFAULT 'local',
    uid         TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_start ON events(starts_at);
CREATE TABLE IF NOT EXISTS event_alerts (
    key  TEXT PRIMARY KEY,
    at   REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS calendar_state (
    k  TEXT PRIMARY KEY,
    v  TEXT NOT NULL
);
"""

FetchFn = Callable[[str], bytes]


class FeedError(Exception):
    """A calendar feed problem, in words the user can act on."""


def fetch_feed(url: str, timeout: float = 20) -> bytes:
    url = (url or "").strip()
    if url.startswith("webcal://"):
        url = "https://" + url[len("webcal://"):]
    if not url.startswith(("https://", "http://")):
        raise FeedError("that is not a web address. Copy the 'Secret address in iCal format' from Google Calendar.")
    request = urllib.request.Request(url, headers={"User-Agent": "Bantu"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = response.read(MAX_FEED_BYTES + 1)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403, 404):
            raise FeedError("the calendar address was refused - it may have been reset in Google Calendar.") from e
        raise FeedError(f"the calendar could not be fetched (HTTP {e.code}).") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise FeedError(f"the calendar could not be reached ({e}).") from e
    if len(data) > MAX_FEED_BYTES:
        raise FeedError("the calendar feed is too large to read.")
    return data


def _local_date(ts: float) -> datetime.date:
    return datetime.datetime.fromtimestamp(ts).date()


def _timestamp(value: Any) -> tuple[float, bool]:
    """(POSIX time, all_day) for an iCalendar DTSTART/DTEND value."""
    if isinstance(value, datetime.datetime):
        return (value if value.tzinfo else value.astimezone()).timestamp(), False
    if isinstance(value, datetime.date):
        return datetime.datetime.combine(value, datetime.time()).timestamp(), True
    raise ValueError(f"unexpected date value {value!r}")


def parse_feed(raw: bytes, now: float | None = None) -> tuple[str, list[dict[str, Any]]]:
    """(calendar name, occurrences in the window) from iCalendar bytes, recurrence expanded."""
    import icalendar
    import recurring_ical_events

    try:
        cal = icalendar.Calendar.from_ical(raw)
    except Exception as e:
        raise FeedError("that address did not return a calendar. Check it is the iCal address.") from e
    today = _local_date(time.time() if now is None else now)
    start = today - datetime.timedelta(days=FEED_PAST_DAYS)
    end = today + datetime.timedelta(days=FEED_FUTURE_DAYS)
    occurrences = []
    for ev in recurring_ical_events.of(cal).between(start, end):
        try:
            starts_at, all_day = _timestamp(ev.get("DTSTART").dt)
            if ev.get("DTEND") is not None:
                ends_at, _ = _timestamp(ev.get("DTEND").dt)
            elif ev.get("DURATION") is not None:
                ends_at = starts_at + ev.get("DURATION").dt.total_seconds()
            else:
                ends_at = starts_at + (86400 if all_day else 0)
        except (AttributeError, ValueError):
            continue
        occurrences.append({
            "title": str(ev.get("SUMMARY") or "(no title)"),
            "starts_at": starts_at, "ends_at": ends_at, "all_day": all_day,
            "location": str(ev.get("LOCATION") or ""),
            "uid": str(ev.get("UID") or ""),
        })
    name = str(cal.get("X-WR-CALNAME") or "Google Calendar")
    return name, sorted(occurrences, key=lambda o: o["starts_at"])


@dataclass
class CalendarEvent:
    id: int
    title: str
    starts_at: float
    ends_at: float | None
    all_day: bool
    location: str
    notes: str
    source: str
    uid: str

    @property
    def alert_key(self) -> str:
        return f"{self.source}:{self.uid or self.id}:{self.starts_at:.0f}"

    def when(self) -> str:
        start = datetime.datetime.fromtimestamp(self.starts_at)
        if self.all_day:
            last = _local_date((self.ends_at or self.starts_at + 86400) - 1)
            return f"{start:%a %d %b}, all day" + ("" if last == start.date() else f" to {last:%a %d %b}")
        if self.ends_at and self.ends_at > self.starts_at:
            end = datetime.datetime.fromtimestamp(self.ends_at)
            return f"{start:%a %d %b %H:%M}-{end:%H:%M}" if end.date() == start.date() \
                else f"{start:%a %d %b %H:%M} to {end:%a %d %b %H:%M}"
        return f"{start:%a %d %b %H:%M}"

    def describe(self) -> str:
        parts = [f"#{self.id}", self.when(), self.title]
        if self.location:
            parts.append(f"at {self.location}")
        if self.source == "feed":
            parts.append("from Google Calendar")
        return " · ".join(parts)

    def short(self) -> str:
        """For the briefing: '10:00 Team stand-up', 'all day: Constitution Day'."""
        head = "all day" if self.all_day else datetime.datetime.fromtimestamp(self.starts_at).strftime("%H:%M")
        return f"{head}: {self.title}" if self.all_day else f"{head} {self.title}"


def _event(r: sqlite3.Row) -> CalendarEvent:
    return CalendarEvent(r["id"], r["title"], r["starts_at"], r["ends_at"], bool(r["all_day"]), r["location"],
                         r["notes"], r["source"], r["uid"])


class Calendar:
    def __init__(self, db: sqlite3.Connection, fetch: FetchFn = fetch_feed):
        self.db = db
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()
        self.fetch = fetch
        self._sync_lock = threading.Lock()
        self._stop = threading.Event()
        self._poller: threading.Thread | None = None

    # --- Bantu's own events ----------------------------------------------------

    def add(self, title: str, starts_at: float, ends_at: float | None = None, all_day: bool = False,
            location: str = "", notes: str = "") -> CalendarEvent:
        title = " ".join((title or "").split())
        if not title:
            raise ValueError("an event needs a title")
        if ends_at is not None and ends_at < starts_at:
            raise ValueError("an event cannot end before it starts")
        cur = self.db.execute(
            "INSERT INTO events (title, starts_at, ends_at, all_day, location, notes, source, created_at)"
            " VALUES (?,?,?,?,?,?,'local',?)",
            (title, starts_at, ends_at, int(all_day), location.strip(), notes.strip(), time.time()),
        )
        self.db.commit()
        return self.get(cur.lastrowid)

    def get(self, event_id: int) -> CalendarEvent | None:
        row = self.db.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        return _event(row) if row else None

    def cancel(self, event_id: int) -> CalendarEvent:
        event = self.get(event_id)
        if event is None:
            raise KeyError(event_id)
        if event.source != "local":
            raise PermissionError("that event comes from Google Calendar, which Bantu only reads - change it there")
        self.db.execute("DELETE FROM events WHERE id=?", (event_id,))
        self.db.commit()
        return event

    def between(self, start: float, end: float) -> list[CalendarEvent]:
        """Events overlapping [start, end), soonest first."""
        rows = self.db.execute(
            "SELECT * FROM events WHERE starts_at < ? AND COALESCE(ends_at, starts_at) >= ?"
            " ORDER BY all_day DESC, starts_at",
            (end, start),
        ).fetchall()
        # An all-day event's end is the next midnight, which must not count that next day.
        return [e for e in map(_event, rows) if not (e.all_day and e.ends_at is not None and e.ends_at <= start)]

    def on_day(self, day: datetime.date) -> list[CalendarEvent]:
        start = datetime.datetime.combine(day, datetime.time()).timestamp()
        return self.between(start, start + 86400)

    # --- Google Calendar, read-only ----------------------------------------------

    def _state(self, key: str, value: str | None = None) -> str | None:
        if value is None:
            row = self.db.execute("SELECT v FROM calendar_state WHERE k=?", (key,)).fetchone()
            return row["v"] if row else None
        self.db.execute("INSERT OR REPLACE INTO calendar_state (k, v) VALUES (?,?)", (key, value))
        return value

    def sync_feed(self, url: str | None) -> int:
        """Replace the copied feed events with a fresh read. Returns how many are in the window."""
        with self._sync_lock:
            if not url:
                self.db.execute("DELETE FROM events WHERE source='feed'")
                self.db.execute("DELETE FROM calendar_state")
                self.db.commit()
                return 0
            try:
                name, occurrences = parse_feed(self.fetch(url))
            except FeedError as e:
                # Keep the last good copy: a flaky connection should not empty the calendar.
                self._state("error", str(e))
                self.db.commit()
                raise
            self.db.execute("DELETE FROM events WHERE source='feed'")
            self.db.executemany(
                "INSERT INTO events (title, starts_at, ends_at, all_day, location, source, uid, created_at)"
                " VALUES (?,?,?,?,?,'feed',?,?)",
                [(o["title"], o["starts_at"], o["ends_at"], int(o["all_day"]), o["location"], o["uid"], time.time())
                 for o in occurrences],
            )
            self._state("name", name)
            self._state("synced_at", str(time.time()))
            self.db.execute("DELETE FROM calendar_state WHERE k='error'")
            self.db.commit()
            return len(occurrences)

    def feed_status(self) -> str:
        name, synced, error = self._state("name"), self._state("synced_at"), self._state("error")
        if not synced and not error:
            return "No Google Calendar connected."
        parts = []
        if synced:
            minutes = int((time.time() - float(synced)) // 60)
            ago = "just now" if minutes < 1 else f"{minutes} min ago"
            count = self.db.execute("SELECT COUNT(*) FROM events WHERE source='feed'").fetchone()[0]
            parts.append(f"{name}: {count} events copied, updated {ago}.")
        if error:
            parts.append(f"Last update failed: {error}")
        return " ".join(parts)

    def start_polling(self, url: Callable[[], str | None], every: float = FEED_POLL_S) -> None:
        if self._poller and self._poller.is_alive():
            return
        self._stop.clear()

        def loop() -> None:
            while not self._stop.is_set():
                address = url()
                if address:
                    try:
                        self.sync_feed(address)
                    except FeedError as e:
                        log.warning("calendar feed: %s", e)
                    except Exception:
                        log.exception("calendar feed sync failed")
                self._stop.wait(every)

        self._poller = threading.Thread(target=loop, name="bantu-calendar", daemon=True)
        self._poller.start()

    def stop_polling(self) -> None:
        self._stop.set()
        if self._poller:
            self._poller.join(timeout=2)

    # --- alerts --------------------------------------------------------------------

    def due_alerts(self, minutes: int, now: float | None = None) -> list[CalendarEvent]:
        """Timed events starting within `minutes` that have not been announced."""
        if minutes <= 0:
            return []
        now = time.time() if now is None else now
        done = {r["key"] for r in self.db.execute("SELECT key FROM event_alerts")}
        return [e for e in self.between(now, now + minutes * 60)
                if not e.all_day and e.starts_at > now and e.alert_key not in done]

    def mark_alerted(self, event: CalendarEvent) -> None:
        self.db.execute("INSERT OR REPLACE INTO event_alerts (key, at) VALUES (?,?)", (event.alert_key, time.time()))
        self.db.execute("DELETE FROM event_alerts WHERE at < ?", (time.time() - 90 * 86400,))
        self.db.commit()


def check_feed(url: str, fetch: FetchFn = fetch_feed) -> tuple[bool, str]:
    """For Settings: does this address return a calendar, and what is in it?"""
    try:
        name, occurrences = parse_feed(fetch(url))
    except FeedError as e:
        return False, str(e)[:1].upper() + str(e)[1:]
    return True, f"Found '{name}' with {len(occurrences)} events in the next {FEED_FUTURE_DAYS} days."


def _parse_start(text: str) -> tuple[float, bool]:
    text = (text or "").strip()
    try:
        if len(text) == 10:
            return datetime.datetime.combine(datetime.date.fromisoformat(text), datetime.time()).timestamp(), True
        dt = datetime.datetime.fromisoformat(text)
    except ValueError as e:
        raise ToolError(
            f"could not read {text!r}. Call get_datetime, take the date from its list of coming days, "
            f"and pass '2026-09-21T10:00', or '2026-09-21' for an all-day event."
        ) from e
    return (dt if dt.tzinfo else dt.astimezone()).timestamp(), False


def register(reg: ToolRegistry, calendar: Calendar) -> None:
    reg.describe_category(
        "calendar", "the user's calendar and meetings, including Google Calendar if connected: "
                    "see what is on, add and cancel events"
    )

    @reg.register(tier=Tier.AUTO, category="calendar")
    def add_event(title: str, start: str, end: str = "", location: str = "", notes: str = "") -> str:
        """Put an event in Bantu's calendar. Google Calendar events cannot be added from here.

        For a time, call get_datetime first and take the date from its list of coming days.

        Args:
            title: What it is, e.g. "Call with Ram".
            start: '2026-09-21T10:00', or '2026-09-21' for an all-day event.
            end: When it ends, same form. Leave empty if not said.
            location: Where, if said.
            notes: Anything else worth keeping with it.
        """
        starts_at, all_day = _parse_start(start)
        ends_at = None
        if end.strip():
            ends_at, _ = _parse_start(end)
            if all_day:
                ends_at += 86400  # an all-day event ending on a day includes that day
        elif all_day:
            ends_at = starts_at + 86400
        try:
            event = calendar.add(title, starts_at, ends_at, all_day, location, notes)
        except ValueError as e:
            raise ToolError(str(e)) from e
        warning = ""
        if (event.ends_at or event.starts_at) < time.time():
            warning = ("\nWarning: that is already in the past. If a coming day was meant, cancel this and add it "
                       "again using get_datetime's list of days.")
        return f"Added {event.describe()}" + warning

    @reg.register(tier=Tier.AUTO, category="calendar")
    def list_events(start: str = "", end: str = "") -> str:
        """What is on the calendar between two dates, Google Calendar included.

        Args:
            start: First day, e.g. '2026-09-21'. Empty means today.
            end: Last day, included. Empty means a week from the start.
        """
        try:
            first = datetime.date.fromisoformat(start.strip()[:10]) if start.strip() else datetime.date.today()
            last = datetime.date.fromisoformat(end.strip()[:10]) if end.strip() else first + datetime.timedelta(days=6)
        except ValueError as e:
            raise ToolError("dates must look like '2026-09-21'") from e
        if last < first:
            raise ToolError("the end date is before the start date")
        lo = datetime.datetime.combine(first, datetime.time()).timestamp()
        hi = datetime.datetime.combine(last, datetime.time()).timestamp() + 86400
        events = calendar.between(lo, hi)
        span = f"{first:%a %d %b}" + ("" if last == first else f" to {last:%a %d %b}")
        if not events:
            return f"Nothing on the calendar {span}. ({calendar.feed_status()})"
        return f"Calendar {span}:\n" + "\n".join(e.describe() for e in events)

    @reg.register(tier=Tier.AUTO, category="calendar")
    def cancel_event(event_id: int) -> str:
        """Remove an event Bantu added, by its number from list_events.

        Args:
            event_id: The event number.
        """
        try:
            event = calendar.cancel(event_id)
        except KeyError:
            raise ToolError(f"there is no event #{event_id}") from None
        except PermissionError as e:
            raise ToolError(str(e)) from e
        return f"Cancelled {event.describe()}"
