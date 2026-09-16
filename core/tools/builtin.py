"""Tools that need no platform support: time, and durable memory.

Memory is the difference between an assistant and a buddy — it is what lets
Bantu know things about you across sessions.
"""

from __future__ import annotations

import datetime

from ..memory import Memory
from .registry import Tier, ToolRegistry


def register(reg: ToolRegistry, memory: Memory) -> None:
    @reg.register(tier=Tier.AUTO, category="core")
    def get_datetime() -> str:
        """Return the current date, time, weekday and timezone.

        Use this whenever the answer depends on 'now' — you do not otherwise
        know what time it is.
        """
        now = datetime.datetime.now().astimezone()
        return now.strftime("%A, %d %B %Y, %H:%M:%S (%Z, UTC%z)")

    @reg.register(tier=Tier.AUTO, category="core")
    def remember(fact: str) -> str:
        """Store a durable fact about the user, recalled in every later session.

        Use it for lasting things: preferences, names, projects, how they like
        work done. Not for details that only matter in this conversation.

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
    def search_history(query: str) -> str:
        """Search what was said in earlier conversations.

        Args:
            query: Words to look for.
        """
        hits = memory.search_messages(query)
        if not hits:
            return f"Nothing in past conversations about {query!r}."
        return "\n".join(f"- {h[:220]}" for h in hits)
