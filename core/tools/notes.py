"""Tools over structured records: note things, find them, finish them.

A separate group from core so its schemas load only when a request needs them.
Portable: no platform imports.
"""

from __future__ import annotations

import datetime
import re
from typing import Literal

from ..memory import Memory
from ..records import KINDS, STATUSES, Records
from ..reminders import parse_when
from .registry import Tier, ToolError, ToolRegistry

Kind = Literal["commitment", "decision", "action_item", "task", "note", "person"]

_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def parse_day(text: str, end_of_day: bool) -> float:
    """A date or date-time; a bare date means the start or the end of that day."""
    text = (text or "").strip()
    if _DATE_ONLY.match(text):
        text += "T23:59:59" if end_of_day else "T00:00"
    return parse_when(text)


def parse_due(text: str) -> float:
    """A due date given as a bare day is due by the end of it, shown as 23:59."""
    text = (text or "").strip()
    if _DATE_ONLY.match(text):
        return datetime.datetime.fromisoformat(text + "T23:59").astimezone().timestamp()
    return parse_when(text)


def _past_due_warning(due_at: float | None) -> str:
    """Flag a due date before today: usually a date worked out wrongly, not a real one."""
    if due_at is None:
        return ""
    today = datetime.datetime.combine(datetime.date.today(), datetime.time()).timestamp()
    if due_at >= today:
        return ""
    return (
        f"\nWarning: that due date is already past (today is {datetime.date.today():%a %Y-%m-%d}). "
        f"If the user meant a coming day, correct it with update_note using get_datetime's list of days."
    )


def register(reg: ToolRegistry, records: Records, memory: Memory) -> None:
    reg.describe_category(
        "notes",
        "note and look up commitments, promises, decisions, action items, tasks, deadlines and people; "
        "mark them done",
    )

    @reg.register(tier=Tier.AUTO, category="notes")
    def note(kind: Kind, text: str, people: str = "", due: str = "", priority: str = "") -> str:
        """Put something on record, to be found later by what it says or who it involves.

        Use it whenever the user makes a promise, reaches a decision, takes on or hands out
        a task, mentions a deadline, or tells you something worth keeping about a person.
        For a due date, call get_datetime first and take the date from its list of coming days.

        Args:
            kind: commitment (a promise the user made), decision, action_item (something
                someone must do), task (the user's own to-do), note, or person (who someone is).
            text: One self-contained sentence, meaningful without this conversation, in the
                user's own language and script - do not translate it.
            people: Names involved, as the user wrote them, comma-separated, e.g. "Ram, Sita".
            due: When it is due, as '2026-09-19' or '2026-09-19T17:00'. Leave empty if none.
            priority: high, normal or low, for tasks. Leave empty if not said.
        """
        due_at = None
        if due and due.strip():
            try:
                due_at = parse_due(due)
            except ValueError as e:
                raise ToolError(str(e)) from e
        try:
            record = records.add(kind, text, people, due_at, priority or None, memory.conversation)
        except ValueError as e:
            raise ToolError(str(e)) from e
        return f"Noted {record.describe()}" + _past_due_warning(due_at)

    @reg.register(tier=Tier.AUTO, category="notes")
    def find_notes(
        query: str = "",
        kind: str = "",
        person: str = "",
        status: str = "",
        since: str = "",
        until: str = "",
    ) -> str:
        """Look up what is on record. Every result says when it was noted.

        Use it for questions like "what did I promise Ram?", "what did we decide about the
        vendor?", "what are my open tasks?" or "who is Sita?". Records keep the user's own
        words, so search in the language they used; if that finds nothing, try once more in the
        other language, or with a name in the other script (Devanagari or Latin letters). If still
        nothing, tell the user there is no record - never fill the gap from guesswork.

        Args:
            query: Words to search for. Leave empty to list by the other filters.
            kind: commitment, decision, action_item, task, note or person. Empty for all.
            person: Only records involving this name.
            status: open, done or cancelled. Use open for outstanding work.
            since: Only records noted on or after this date, e.g. '2026-09-01'.
            until: Only records noted on or before this date.
        """
        kind = kind.strip().lower() or None
        status = status.strip().lower() or None
        if kind and kind not in KINDS:
            raise ToolError(f"kind must be one of {', '.join(KINDS)}")
        if status and status not in STATUSES:
            raise ToolError(f"status must be one of {', '.join(STATUSES)}")
        try:
            start = parse_day(since, end_of_day=False) if since.strip() else None
            end = parse_day(until, end_of_day=True) if until.strip() else None
        except ValueError as e:
            raise ToolError(str(e)) from e

        found = records.find(query, kind, person.strip() or None, status, start, end)
        if not found:
            asked = ", ".join(
                f"{label} {value!r}" for label, value in
                (("matching", query), ("kind", kind), ("involving", person), ("status", status),
                 ("since", since), ("until", until)) if value
            ) or "at all"
            return (
                f"No records {asked}. If you have not yet, try once with other words or the name in the "
                f"other script; otherwise tell the user nothing is on record rather than guessing."
            )
        return "\n".join(r.describe() for r in found)

    @reg.register(tier=Tier.AUTO, category="notes")
    def update_note(note_id: int, status: str = "", due: str = "") -> str:
        """Mark a commitment, action item or task done, cancelled or open again, or change its due date.

        Args:
            note_id: The record number, as shown by find_notes.
            status: done, cancelled, or open. Leave empty to keep it.
            due: A new due date, as '2026-09-19' or '2026-09-19T17:00'. Leave empty to keep it.
        """
        if records.get(note_id) is None:
            raise ToolError(f"there is no record #{note_id}")
        status, due = status.strip().lower(), due.strip()
        if not status and not due:
            raise ToolError("give a status, a due date, or both")
        due_at = None
        try:
            if due:
                due_at = parse_due(due)
                record = records.set_due(note_id, due_at)
            if status:
                record = records.set_status(note_id, status)
        except ValueError as e:
            raise ToolError(str(e)) from e
        return f"Updated {record.describe()}" + _past_due_warning(due_at)

    @reg.register(tier=Tier.CONFIRM, category="notes")
    def delete_note(note_id: int) -> str:
        """Delete a record for good. Prefer update_note to mark work done or cancelled.

        Args:
            note_id: The record number, as shown by find_notes.
        """
        record = records.get(note_id)
        if record is None:
            raise ToolError(f"there is no record #{note_id}")
        records.delete(note_id)
        return f"Deleted {record.describe()}"

