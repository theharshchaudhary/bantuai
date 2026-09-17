"""A local log of what Bantu was allowed, refused, or failed to do.

The registry reports every approval, decline, block and failure here, so
"what did Bantu do on my machine?" has an answer that does not depend on the
model remembering. Successful read-only lookups are not logged: they change
nothing, and logging them would bury the entries that matter.

Portable: stdlib only. Stays on this computer; pruned after 90 days.
"""

from __future__ import annotations

import datetime
import json
import sqlite3
import time
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS activity (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    at         REAL NOT NULL,
    tool       TEXT NOT NULL,
    tier       TEXT NOT NULL,
    outcome    TEXT NOT NULL,
    arguments  TEXT NOT NULL,
    detail     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_activity_at ON activity(at);
"""

RETENTION_DAYS = 90
#: Per argument value. Enough to recognise a path or a command; not a whole file's contents.
MAX_VALUE_CHARS = 160
MAX_DETAIL_CHARS = 300

#: What each outcome means, for anyone reading the log.
OUTCOMES = {
    "approved": "the user approved it and it ran",
    "allowed": "ran under an approval the user gave earlier in the same request",
    "declined": "the user said no",
    "not asked": "needed approval but nothing could ask, so it did not run",
    "blocked": "blocked in code; never runs",
    "failed": "it was attempted and failed or was refused by a safety check",
}


def _short(value: Any, limit: int = MAX_VALUE_CHARS) -> Any:
    if isinstance(value, str):
        return value if len(value) <= limit else value[: limit - 1] + "…"
    if isinstance(value, dict):
        return {k: _short(v, limit) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_short(v, limit) for v in value]
    return value


def argument_summary(arguments: dict[str, Any]) -> str:
    return ", ".join(f"{k}={v}" for k, v in arguments.items() if v not in (None, ""))


class Activity:
    def __init__(self, db: sqlite3.Connection):
        self.db = db
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    def record(self, tool: str, tier: str, outcome: str, arguments: dict[str, Any] | None, detail: str = "") -> None:
        self.db.execute(
            "INSERT INTO activity (at, tool, tier, outcome, arguments, detail) VALUES (?,?,?,?,?,?)",
            (time.time(), tool, tier, outcome,
             json.dumps(_short(dict(arguments or {})), ensure_ascii=False, default=str),
             (detail or "")[:MAX_DETAIL_CHARS]),
        )
        self.db.commit()

    def recent(self, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT * FROM activity ORDER BY at DESC, id DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for r in rows:
            entry = dict(r)
            try:
                entry["arguments"] = json.loads(entry["arguments"])
            except ValueError:
                entry["arguments"] = {}
            out.append(entry)
        return out

    def prune(self, days: int = RETENTION_DAYS) -> int:
        cur = self.db.execute("DELETE FROM activity WHERE at < ?", (time.time() - days * 86400,))
        self.db.commit()
        return cur.rowcount


def describe_entry(entry: dict[str, Any]) -> str:
    """'Thu 17 Sep 14:02 · declined · delete_file · path=C:\\notes.txt'"""
    when = datetime.datetime.fromtimestamp(entry["at"]).strftime("%a %d %b %H:%M")
    args = argument_summary(entry.get("arguments") or {})
    return " · ".join(part for part in (when, entry["outcome"], entry["tool"], args) if part)
