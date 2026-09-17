"""Conversation and fact storage on SQLite with FTS5.

FTS5 ships inside Python, so recall needs no embedding model and nothing to
download. Replaces the old Data/ChatLog.json, which had no size bound and would
eventually outgrow any context window.

Portable: stdlib only.
"""

from __future__ import annotations

import base64
import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .providers.base import Message, ToolCall


# Provider data riding on a tool call must survive storage. Gemini's
# thought_signature is bytes, and without it Gemini rejects its own earlier call
# the next time history is read back, which the agent does every turn.
def _pack_meta(meta: dict[str, Any]) -> dict[str, Any]:
    return {
        k: {"b64": base64.b64encode(v).decode("ascii")} if isinstance(v, bytes) else v
        for k, v in (meta or {}).items()
    }


def _unpack_meta(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}  # rows stored before meta was kept
    return {
        k: base64.b64decode(v["b64"]) if isinstance(v, dict) and set(v) == {"b64"} else v
        for k, v in raw.items()
    }


#: Rough chars-per-token. Deliberately pessimistic — better to trim early than
#: to have a provider reject an oversized request mid-task.
CHARS_PER_TOKEN = 3.6

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation  TEXT NOT NULL,
    role          TEXT NOT NULL,
    content       TEXT,
    tool_calls    TEXT,
    tool_call_id  TEXT,
    name          TEXT,
    image_count   INTEGER NOT NULL DEFAULT 0,
    created_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation, id);

CREATE TABLE IF NOT EXISTS facts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    text       TEXT NOT NULL UNIQUE,
    created_at REAL NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(
    text, content='facts', content_rowid='id'
);
CREATE TRIGGER IF NOT EXISTS facts_ai AFTER INSERT ON facts BEGIN
    INSERT INTO facts_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS facts_ad AFTER DELETE ON facts BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, text) VALUES('delete', old.id, old.text);
END;

CREATE TABLE IF NOT EXISTS reminders (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    message    TEXT NOT NULL,
    due_at     REAL NOT NULL,
    created_at REAL NOT NULL,
    fired_at   REAL
);
CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(fired_at, due_at);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content, content='messages', content_rowid='id'
);
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;
"""


def estimate_tokens(messages: Iterable[Message]) -> int:
    n = 0
    for m in messages:
        n += len(m.content or "")
        for tc in m.tool_calls:
            n += len(tc.name) + len(json.dumps(tc.arguments, default=str))
        n += 600 * len(m.images)  # an image bills as a flat block, not as text
    return int(n / CHARS_PER_TOKEN)


def estimate_text_tokens(text: str) -> int:
    return int(len(text or "") / CHARS_PER_TOKEN)


def _fts_query(text: str) -> str:
    """Turn free text into a safe FTS5 MATCH expression.

    User text goes straight into MATCH otherwise, where a stray quote or a bare
    AND is a syntax error rather than a search.
    """
    words = [w for w in "".join(c if c.isalnum() else " " for c in text).split() if len(w) > 1]
    return " OR ".join(f'"{w}"' for w in words[:12])


@dataclass
class Memory:
    path: Path
    conversation: str = ""

    def __post_init__(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self._migrate()
        self.db.commit()
        if not self.conversation:
            self.conversation = uuid.uuid4().hex[:12]

    def _migrate(self) -> None:
        """Add columns introduced after a database was first created."""
        have = {r["name"] for r in self.db.execute("PRAGMA table_info(messages)")}
        if "image_count" not in have:
            self.db.execute(
                "ALTER TABLE messages ADD COLUMN image_count INTEGER NOT NULL DEFAULT 0"
            )

    def close(self) -> None:
        self.db.close()

    # --- conversation -------------------------------------------------------

    def new_conversation(self) -> str:
        self.conversation = uuid.uuid4().hex[:12]
        return self.conversation

    def append(self, m: Message) -> None:
        self.db.execute(
            "INSERT INTO messages (conversation, role, content, tool_calls, tool_call_id, name,"
            " image_count, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                self.conversation,
                m.role,
                m.content,
                json.dumps(
                    [
                        {"id": t.id, "name": t.name, "arguments": t.arguments, "meta": _pack_meta(t.meta)}
                        for t in m.tool_calls
                    ]
                )
                if m.tool_calls
                else None,
                m.tool_call_id,
                m.name,
                len(m.images),
                time.time(),
            ),
        )
        self.db.commit()

    def history(self, max_tokens: int = 8000) -> list[Message]:
        """Recent history, newest-biased, trimmed to a token budget.

        Tool results are dropped before their calls when trimming, because a
        provider rejects a tool result whose originating call is missing. The
        latest user message and everything after it are never trimmed: that is
        the request being worked on, and without it the model answers nothing.
        """
        rows = self.db.execute(
            "SELECT * FROM messages WHERE conversation=? ORDER BY id", (self.conversation,)
        ).fetchall()
        msgs = [self._to_message(r) for r in rows]
        protected = max((i for i, m in enumerate(msgs) if m.role == "user"), default=len(msgs))

        while protected > 0 and estimate_tokens(msgs) > max_tokens:
            msgs.pop(0)
            protected -= 1
            # Never start on an orphaned tool result.
            while protected > 0 and msgs and msgs[0].role == "tool":
                msgs.pop(0)
                protected -= 1
        return msgs

    @staticmethod
    def _to_message(r: sqlite3.Row) -> Message:
        calls = []
        if r["tool_calls"]:
            for d in json.loads(r["tool_calls"]):
                calls.append(
                    ToolCall(
                        id=d["id"], name=d["name"], arguments=d["arguments"], meta=_unpack_meta(d.get("meta"))
                    )
                )
        content = r["content"]
        # Image bytes are deliberately not persisted - they would bloat the database
        # and re-billing them every turn forever is wasteful. A note keeps later
        # history coherent; the live bytes are re-attached for the current run only.
        n = r["image_count"] if "image_count" in r.keys() else 0
        if n:
            content = f"{content or ''}\n[{n} image(s) were attached to this message]"
        return Message(
            role=r["role"],
            content=content,
            tool_calls=calls,
            tool_call_id=r["tool_call_id"],
            name=r["name"],
        )

    def recent_conversations(self, limit: int = 10) -> list[dict[str, Any]]:
        """Conversations with at least one user message, most recently active first.

        Each has: conversation, started, last (timestamps), n (messages), first_user.
        """
        rows = self.db.execute(
            "SELECT conversation, MIN(created_at) started, MAX(created_at) last, COUNT(*) n,"
            " (SELECT content FROM messages m2 WHERE m2.conversation=m.conversation"
            "  AND m2.role='user' ORDER BY id LIMIT 1) first_user"
            " FROM messages m GROUP BY conversation HAVING first_user IS NOT NULL"
            " ORDER BY last DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def open_conversation(self, conversation: str) -> bool:
        """Continue an earlier conversation. False, and nothing changes, if there is none."""
        found = self.db.execute(
            "SELECT 1 FROM messages WHERE conversation=? LIMIT 1", (conversation,)
        ).fetchone()
        if found is None:
            return False
        self.conversation = conversation
        return True

    def transcript(self, limit: int = 60) -> list[tuple[str, str]]:
        """What a person saw in this conversation, oldest first: (role, text).

        Only their messages and replies with words in them; tool calls and tool
        results are plumbing, not conversation.
        """
        rows = self.db.execute(
            "SELECT role, content FROM messages WHERE conversation=? AND role IN ('user','assistant')"
            " AND content IS NOT NULL AND TRIM(content) != '' ORDER BY id DESC LIMIT ?",
            (self.conversation, limit),
        ).fetchall()
        return [(r["role"], r["content"]) for r in reversed(rows)]

    # --- facts --------------------------------------------------------------

    def remember(self, text: str) -> str:
        text = text.strip()
        if not text:
            return "Nothing to remember."
        try:
            self.db.execute(
                "INSERT INTO facts (text, created_at) VALUES (?,?)", (text, time.time())
            )
            self.db.commit()
            return f"Remembered: {text}"
        except sqlite3.IntegrityError:
            return f"Already knew that: {text}"

    def recall(self, query: str, limit: int = 8) -> list[str]:
        q = _fts_query(query)
        if not q:
            return []
        try:
            rows = self.db.execute(
                "SELECT f.text FROM facts_fts JOIN facts f ON f.id = facts_fts.rowid"
                " WHERE facts_fts MATCH ? ORDER BY rank LIMIT ?",
                (q, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [r["text"] for r in rows]

    def all_facts(self, limit: int = 100) -> list[str]:
        rows = self.db.execute(
            "SELECT text FROM facts ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [r["text"] for r in rows]

    def forget(self, text: str) -> str:
        cur = self.db.execute("DELETE FROM facts WHERE text = ?", (text,))
        self.db.commit()
        return "Forgotten." if cur.rowcount else "No such fact."

    # --- reminders ----------------------------------------------------------

    def add_reminder(self, message: str, due_at: float) -> int:
        cur = self.db.execute(
            "INSERT INTO reminders (message, due_at, created_at) VALUES (?,?,?)",
            (message.strip(), due_at, time.time()),
        )
        self.db.commit()
        return int(cur.lastrowid)

    def pending_reminders(self) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT id, message, due_at FROM reminders WHERE fired_at IS NULL ORDER BY due_at"
        ).fetchall()
        return [dict(r) for r in rows]

    def due_reminders(self, now: float | None = None) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT id, message, due_at FROM reminders WHERE fired_at IS NULL AND due_at <= ?"
            " ORDER BY due_at",
            (now if now is not None else time.time(),),
        ).fetchall()
        return [dict(r) for r in rows]

    def mark_fired(self, reminder_id: int) -> None:
        self.db.execute("UPDATE reminders SET fired_at=? WHERE id=?", (time.time(), reminder_id))
        self.db.commit()

    def cancel_reminder(self, reminder_id: int) -> bool:
        cur = self.db.execute(
            "DELETE FROM reminders WHERE id=? AND fired_at IS NULL", (reminder_id,)
        )
        self.db.commit()
        return cur.rowcount > 0

    def search_messages(self, query: str, limit: int = 10) -> list[str]:
        q = _fts_query(query)
        if not q:
            return []
        try:
            rows = self.db.execute(
                "SELECT m.content FROM messages_fts JOIN messages m ON m.id = messages_fts.rowid"
                " WHERE messages_fts MATCH ? AND m.content IS NOT NULL ORDER BY rank LIMIT ?",
                (q, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [r["content"] for r in rows]
