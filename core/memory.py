"""Conversation and fact storage on SQLite with FTS5.

FTS5 ships inside Python, so recall needs no embedding model and nothing to
download. Replaces the old Data/ChatLog.json, which had no size bound and would
eventually outgrow any context window.

Portable: stdlib only.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .providers.base import Message, ToolCall

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
        self.db.commit()
        if not self.conversation:
            self.conversation = uuid.uuid4().hex[:12]

    def close(self) -> None:
        self.db.close()

    # --- conversation -------------------------------------------------------

    def new_conversation(self) -> str:
        self.conversation = uuid.uuid4().hex[:12]
        return self.conversation

    def append(self, m: Message) -> None:
        self.db.execute(
            "INSERT INTO messages (conversation, role, content, tool_calls, tool_call_id, name,"
            " created_at) VALUES (?,?,?,?,?,?,?)",
            (
                self.conversation,
                m.role,
                m.content,
                json.dumps(
                    [{"id": t.id, "name": t.name, "arguments": t.arguments} for t in m.tool_calls]
                )
                if m.tool_calls
                else None,
                m.tool_call_id,
                m.name,
                time.time(),
            ),
        )
        self.db.commit()

    def history(self, max_tokens: int = 8000) -> list[Message]:
        """Recent history, newest-biased, trimmed to a token budget.

        Tool results are dropped before their calls when trimming, because a
        provider rejects a tool result whose originating call is missing.
        """
        rows = self.db.execute(
            "SELECT * FROM messages WHERE conversation=? ORDER BY id", (self.conversation,)
        ).fetchall()
        msgs = [self._to_message(r) for r in rows]

        while msgs and estimate_tokens(msgs) > max_tokens:
            msgs.pop(0)
            # Never start on an orphaned tool result.
            while msgs and msgs[0].role == "tool":
                msgs.pop(0)
        return msgs

    @staticmethod
    def _to_message(r: sqlite3.Row) -> Message:
        calls = []
        if r["tool_calls"]:
            for d in json.loads(r["tool_calls"]):
                calls.append(ToolCall(id=d["id"], name=d["name"], arguments=d["arguments"]))
        return Message(
            role=r["role"],
            content=r["content"],
            tool_calls=calls,
            tool_call_id=r["tool_call_id"],
            name=r["name"],
        )

    def recent_conversations(self, limit: int = 10) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT conversation, MIN(created_at) started, COUNT(*) n,"
            " (SELECT content FROM messages m2 WHERE m2.conversation=m.conversation"
            "  AND m2.role='user' ORDER BY id LIMIT 1) first_user"
            " FROM messages m GROUP BY conversation ORDER BY started DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

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
