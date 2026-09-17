"""Tools that need no platform support: time, and durable memory.

Memory is the difference between an assistant and a buddy — it is what lets
Bantu know things about you across sessions.
"""

from __future__ import annotations

import datetime

from ..memory import Memory
from ..records import day_label
from ..reminders import describe, parse_when
from .registry import Tier, ToolError, ToolRegistry


def register(reg: ToolRegistry, memory: Memory) -> None:
    @reg.register(tier=Tier.AUTO, category="core")
    def get_datetime() -> str:
        """Return the current date, time, weekday and timezone.

        Use this whenever the answer depends on 'now' — you do not otherwise
        know what time it is.
        """
        now = datetime.datetime.now().astimezone()
        # Listed out because weaker models get weekday arithmetic wrong: asked for
        # "this Friday" on Thursday the 17th, gpt-oss-20b answered the 16th.
        coming = ", ".join(
            f"{d:%a} {d:%Y-%m-%d}" for d in (now.date() + datetime.timedelta(days=i) for i in range(1, 8))
        )
        return now.strftime("%A, %d %B %Y, %H:%M:%S (%Z, UTC%z)") + f". Next 7 days: {coming}."

    @reg.register(tier=Tier.AUTO, category="core")
    def remember(fact: str) -> str:
        """Store a durable fact about the user, recalled in every later session.

        Use it for lasting things: preferences, projects, how they like work done.
        Not for details that only matter in this conversation, and not for promises,
        decisions, tasks, deadlines or who someone is: note those with the notes
        tools, which keep the date, the people and whether it is done.

        Args:
            fact: One self-contained sentence, meaningful without context.
        """
        return memory.remember(fact)

    @reg.register(tier=Tier.AUTO, category="core")
    def recall(query: str) -> str:
        """Search previously remembered facts about the user.

        Args:
            query: Words to look for.
        """
        hits = memory.recall(query)
        if not hits:
            return f"Nothing remembered about {query!r}."
        return "\n".join(f"- {h}" for h in hits)

    @reg.register(tier=Tier.CONFIRM, category="core")
    def forget(fact: str) -> str:
        """Delete a remembered fact. Must match the stored text exactly.

        Args:
            fact: The exact fact text to remove.
        """
        return memory.forget(fact)

    @reg.register(tier=Tier.AUTO, category="core")
    def set_reminder(when: str, message: str) -> str:
        """Schedule a reminder that will pop up and be spoken at a given time.

        Call get_datetime first and work out the absolute time yourself, then
        pass it as ISO 8601 (e.g. '2026-09-17T09:00'). 'in 20 minutes' also
        works. Reminders survive restarting Bantu.

        Args:
            when: ISO 8601 date-time, or a relative form like 'in 30 minutes'.
            message: What to say when it fires.
        """
        if not message.strip():
            raise ToolError("a reminder needs a message")
        try:
            due = parse_when(when)
        except ValueError as e:
            raise ToolError(str(e)) from e
        import time as _t

        if due < _t.time() - 60:
            raise ToolError(f"{when!r} is in the past. Check the current time with get_datetime.")
        rid = memory.add_reminder(message, due)
        return f"Reminder #{rid} set for {describe(due)}: {message}"

    @reg.register(tier=Tier.AUTO, category="core")
    def list_reminders() -> str:
        """List reminders that have not fired yet."""
        items = memory.pending_reminders()
        if not items:
            return "No reminders pending."
        return "\n".join(f"  #{r['id']}  {describe(r['due_at'])}  {r['message']}" for r in items)

    @reg.register(tier=Tier.AUTO, category="core")
    def cancel_reminder(reminder_id: int) -> str:
        """Cancel a pending reminder by its number, as shown by list_reminders.

        Args:
            reminder_id: The reminder's number.
        """
        ok = memory.cancel_reminder(reminder_id)
        return f"Cancelled reminder #{reminder_id}." if ok else f"No pending reminder #{reminder_id}."

    @reg.register(tier=Tier.AUTO, category="core")
    def search_history(query: str) -> str:
        """Search what was said in earlier conversations, with when it was said.

        Args:
            query: Words to look for.
        """
        hits = memory.find_messages(query)
        if not hits:
            return f"Nothing in past conversations about {query!r}. There is no record of it; say so."
        who = {"user": "user said", "assistant": "you replied"}
        return "\n".join(
            f"- {day_label(h['created_at'])} {datetime.datetime.fromtimestamp(h['created_at']):%H:%M}, "
            f"{who[h['role']]}: {' '.join(h['content'].split())[:220]}"
            for h in hits
        )
