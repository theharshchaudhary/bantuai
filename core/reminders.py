"""Reminder scheduling.

Portable: the storage and timing live here, while *how* a reminder is announced
is injected by the platform layer. On the desktop that is a toast plus speech;
a future client would pass something else.

Reminders survive a restart because they live in SQLite, not in the thread.
"""

from __future__ import annotations

import datetime
import logging
import re
import threading
import time
from typing import Callable

from .memory import Memory

log = logging.getLogger("bantu.reminders")

#: How often to check for anything due. Reminders are minute-grained, so this
#: is frequent enough while staying invisible in CPU usage.
TICK_SECONDS = 20

NotifyFn = Callable[[str, str], None]

_RELATIVE = re.compile(
    r"^\s*in\s+(\d+)\s*(min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days|s|sec|secs|second|seconds)\s*$",
    re.IGNORECASE,
)
_UNIT_SECONDS = {
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
}


def parse_when(when: str) -> float:
    """Turn a time expression into a POSIX timestamp.

    Accepts an ISO datetime (what the model should normally send, since it can
    call get_datetime first) or a simple 'in N minutes' form for convenience.
    Deliberately does not attempt broad natural language: a reminder that fires
    at the wrong time is worse than one that is refused.
    """
    text = (when or "").strip()
    if not text:
        raise ValueError("no time given")

    rel = _RELATIVE.match(text)
    if rel:
        return time.time() + int(rel.group(1)) * _UNIT_SECONDS[rel.group(2).lower()]

    iso = text.replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(iso)
    except ValueError as e:
        raise ValueError(
            f"could not understand {when!r}. Call get_datetime, work out the absolute "
            f"time, and pass it as ISO 8601 such as '2026-09-17T09:00', or use "
            f"'in 20 minutes'."
        ) from e
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.timestamp()


def describe(due_at: float) -> str:
    dt = datetime.datetime.fromtimestamp(due_at)
    delta = due_at - time.time()
    if delta < 0:
        rel = "overdue"
    elif delta < 3600:
        rel = f"in {int(delta // 60)} min"
    elif delta < 86400:
        rel = f"in {delta / 3600:.1f} h"
    else:
        rel = f"in {int(delta // 86400)} day(s)"
    return f"{dt.strftime('%a %d %b %H:%M')} ({rel})"


class ReminderScheduler:
    """Background thread that fires due reminders.

    A daemon thread so it never keeps the process alive on its own.
    """

    def __init__(self, memory: Memory, notify: NotifyFn, tick: int = TICK_SECONDS):
        self.memory = memory
        self.notify = notify
        self.tick = max(1, tick)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="bantu-reminders", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def check_now(self) -> int:
        """Fire anything due. Returns how many fired. Used by the loop and tests."""
        fired = 0
        for r in self.memory.due_reminders():
            try:
                self.notify("Reminder", r["message"])
            except Exception:
                # A failed notification must not leave the reminder pending
                # forever, re-firing on every tick.
                log.exception("could not announce reminder %s", r["id"])
            self.memory.mark_fired(r["id"])
            fired += 1
        return fired

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.check_now()
            except Exception:
                log.exception("reminder tick failed")
            self._stop.wait(self.tick)
