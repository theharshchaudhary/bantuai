"""The daily briefing, gathered when the user asks for it ("brief me").

Harsh chose on-request only (2026-09-17): nothing speaks up on its own. The
tool gathers facts - date, weather, calendar, reminders, what is due or overdue
- and the model turns them into a short spoken briefing in the user's tone.
Each section fails on its own: no weather must not mean no briefing.

Portable: stdlib only.
"""

from __future__ import annotations

import datetime
import logging
import time
from typing import Any, Callable

from .memory import Memory
from .records import Records, when_label
from .tools.registry import Tier, ToolRegistry

log = logging.getLogger("bantu.briefing")

#: How many open items to name before summarising the rest as a count.
LIST_LIMIT = 5

EventsFn = Callable[[datetime.date], list[str]]


def _day_bounds(day: datetime.date) -> tuple[float, float]:
    start = datetime.datetime.combine(day, datetime.time()).timestamp()
    return start, start + 86400


def build_briefing(
    memory: Memory,
    records: Records,
    weather_line: Callable[[], str] | None = None,
    events_for: EventsFn | None = None,
    now: datetime.datetime | None = None,
) -> str:
    now = now or datetime.datetime.now()
    today = now.date()
    start, end = _day_bounds(today)
    ts = now.timestamp()
    lines = [f"Now: {now:%A %d %B %Y, %H:%M}."]

    if weather_line is not None:
        try:
            lines.append(f"Weather: {weather_line()}")
        except Exception as e:  # each section stands alone
            lines.append(f"Weather: unavailable ({e}).")
    else:
        lines.append("Weather: no city is set (it can be added in Settings).")

    if events_for is not None:
        try:
            events = events_for(today)
            lines.append("Calendar today: " + ("; ".join(events) if events else "nothing scheduled."))
        except Exception as e:
            lines.append(f"Calendar: unavailable ({e}).")

    reminders = [r for r in memory.pending_reminders() if r["due_at"] < end]
    lines.append("Reminders still to come today: " + (
        "; ".join(f"{datetime.datetime.fromtimestamp(r['due_at']):%H:%M} {r['message']}" for r in reminders)
        if reminders else "none."))

    open_items = records.find(status="open", limit=200)
    overdue = [r for r in open_items if r.due_at is not None and r.due_at < ts]
    due_today = [r for r in open_items if r.due_at is not None and ts <= r.due_at < end]
    rest = [r for r in open_items if r not in overdue and r not in due_today]

    def names(items: list) -> str:
        shown = []
        for r in items[:LIST_LIMIT]:
            who = f" ({', '.join(r.people)})" if r.people else ""
            due = f", due {when_label(r.due_at)}" if r.due_at is not None else ""
            shown.append(f"{r.text}{who}{due}")
        extra = len(items) - LIST_LIMIT
        return "; ".join(shown) + (f"; and {extra} more" if extra > 0 else "")

    lines.append("Overdue: " + (names(overdue) if overdue else "nothing."))
    lines.append("Due today: " + (names(due_today) if due_today else "nothing."))
    lines.append("Other open commitments and tasks: " + (names(rest) if rest else "none."))

    since = start - 86400
    decided = records.find(kind="decision", since=since, limit=LIST_LIMIT)
    if decided:
        lines.append("Decisions noted since yesterday: " + "; ".join(r.text for r in decided))
    return "\n".join(lines)


def register(
    reg: ToolRegistry,
    memory: Memory,
    records: Records,
    weather_line: Callable[[], Callable[[], str] | None],
    events_for: Callable[[], EventsFn | None] = lambda: None,
) -> None:
    """`weather_line` and `events_for` are read at call time, so Settings changes apply at once."""

    @reg.register(tier=Tier.AUTO, category="core")
    def daily_briefing() -> str:
        """Gather the user's briefing: date, weather, calendar, reminders, and what is due or overdue.

        Use it when they ask to be briefed or ask how their day looks. Turn the result into a
        short spoken briefing in your usual manner: lead with what needs attention, skip
        empty sections, and do not add anything that is not in it.
        """
        started = time.monotonic()
        text = build_briefing(memory, records, weather_line(), events_for())
        log.info("briefing gathered in %.2fs", time.monotonic() - started)
        return text
