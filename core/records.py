"""Structured records: commitments, decisions, action items, tasks, notes and people.

"What did I promise Ram?" needs more than a pile of chat text. A promise has a
person, a due date and a status, so records keep those as fields, beside the
conversations in the same SQLite file, searched with FTS5 - nothing to download.

Every record keeps when it was made, so an answer about the past can say when,
and a search that finds nothing says so plainly rather than leaving a gap for
the model to fill.

Portable: stdlib only.
"""

from __future__ import annotations

import datetime
import sqlite3
import time
from dataclasses import dataclass, field

from .memory import FTS_TOKENIZE, _fts_query

KINDS = ("commitment", "decision", "action_item", "task", "note", "person")
#: Kinds that can be finished. Decisions, notes and people are simply on record.
TRACKED = frozenset({"commitment", "action_item", "task"})
STATUSES = ("open", "done", "cancelled")
PRIORITIES = ("high", "normal", "low")

SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    kind            TEXT NOT NULL,
    text            TEXT NOT NULL,
    people          TEXT NOT NULL DEFAULT '',
    due_at          REAL,
    priority        TEXT,
    status          TEXT,
    conversation    TEXT,
    created_at      REAL NOT NULL,
    done_at         REAL,
    followed_up_at  REAL
);
CREATE INDEX IF NOT EXISTS idx_records_open ON records(status, due_at);

CREATE VIRTUAL TABLE IF NOT EXISTS records_fts USING fts5(
    text, people, content='records', content_rowid='id', tokenize="{TOKENIZE}"
);
CREATE TRIGGER IF NOT EXISTS records_ai AFTER INSERT ON records BEGIN
    INSERT INTO records_fts(rowid, text, people) VALUES (new.id, new.text, new.people);
END;
CREATE TRIGGER IF NOT EXISTS records_ad AFTER DELETE ON records BEGIN
    INSERT INTO records_fts(records_fts, rowid, text, people) VALUES('delete', old.id, old.text, old.people);
END;
CREATE TRIGGER IF NOT EXISTS records_au AFTER UPDATE OF text, people ON records BEGIN
    INSERT INTO records_fts(records_fts, rowid, text, people) VALUES('delete', old.id, old.text, old.people);
    INSERT INTO records_fts(rowid, text, people) VALUES (new.id, new.text, new.people);
END;
"""


def day_label(ts: float, now: float | None = None) -> str:
    """'Fri 19 Sep', with the year only when it is not this year."""
    dt = datetime.datetime.fromtimestamp(ts)
    this_year = datetime.datetime.fromtimestamp(time.time() if now is None else now).year
    return dt.strftime("%a %d %b") + ("" if dt.year == this_year else f" {dt.year}")


def is_end_of_day(ts: float) -> bool:
    dt = datetime.datetime.fromtimestamp(ts)
    return (dt.hour, dt.minute) == (23, 59)


def when_label(ts: float) -> str:
    """A due time: the day alone when it was given as a date, else day and time."""
    return day_label(ts) if is_end_of_day(ts) else f"{day_label(ts)} {datetime.datetime.fromtimestamp(ts):%H:%M}"


@dataclass
class Record:
    id: int
    kind: str
    text: str
    people: list[str] = field(default_factory=list)
    due_at: float | None = None
    priority: str | None = None
    status: str | None = None
    conversation: str | None = None
    created_at: float = 0.0
    done_at: float | None = None

    @property
    def overdue(self) -> bool:
        return self.status == "open" and self.due_at is not None and self.due_at < time.time()

    def describe(self) -> str:
        """One line the model can quote, always saying when it was noted."""
        parts = [f"#{self.id}", self.kind.replace("_", " ")]
        if self.people:
            parts.append(", ".join(self.people))
        if self.due_at is not None:
            parts.append(("OVERDUE, was due " if self.overdue else "due ") + when_label(self.due_at))
        if self.priority and self.priority != "normal":
            parts.append(f"{self.priority} priority")
        if self.status:
            parts.append(self.status if self.status != "done" or not self.done_at
                         else f"done {day_label(self.done_at)}")
        parts.append(f"noted {day_label(self.created_at)}")
        return " · ".join(parts) + f": {self.text}"


def _row(r: sqlite3.Row) -> Record:
    return Record(
        id=r["id"], kind=r["kind"], text=r["text"],
        people=[p for p in (r["people"] or "").split(", ") if p],
        due_at=r["due_at"], priority=r["priority"], status=r["status"],
        conversation=r["conversation"], created_at=r["created_at"], done_at=r["done_at"],
    )


def clean_people(people: str | list[str] | tuple[str, ...]) -> list[str]:
    """'Ram, sita ;  Ram' -> ['Ram', 'sita']: split, trim, drop repeats (any case)."""
    items = people.replace(";", ",").split(",") if isinstance(people, str) else list(people)
    seen, out = set(), []
    for p in items:
        name = " ".join(str(p).split())
        if name and name.lower() not in seen:
            seen.add(name.lower())
            out.append(name)
    return out


class Records:
    """Commitments, decisions, action items, tasks, notes and people."""

    def __init__(self, db: sqlite3.Connection):
        self.db = db
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA.replace("{TOKENIZE}", FTS_TOKENIZE))
        self.db.commit()

    def add(
        self,
        kind: str,
        text: str,
        people: str | list[str] = "",
        due_at: float | None = None,
        priority: str | None = None,
        conversation: str | None = None,
    ) -> Record:
        kind = (kind or "").strip().lower().replace(" ", "_")
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {', '.join(KINDS)}")
        text = " ".join((text or "").split())
        if not text:
            raise ValueError("a record needs some text")
        priority = (priority or "").strip().lower() or None
        if priority is not None and priority not in PRIORITIES:
            raise ValueError(f"priority must be one of {', '.join(PRIORITIES)}")
        cur = self.db.execute(
            "INSERT INTO records (kind, text, people, due_at, priority, status, conversation, created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (kind, text, ", ".join(clean_people(people)), due_at, priority,
             "open" if kind in TRACKED else None, conversation, time.time()),
        )
        self.db.commit()
        return self.get(cur.lastrowid)

    def get(self, record_id: int) -> Record | None:
        row = self.db.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        return _row(row) if row else None

    def find(
        self,
        query: str = "",
        kind: str | None = None,
        person: str | None = None,
        status: str | None = None,
        since: float | None = None,
        until: float | None = None,
        limit: int = 20,
    ) -> list[Record]:
        """Records matching every filter given. `since`/`until` bound when a record was noted."""
        where, args = [], []
        if kind:
            where.append("r.kind = ?")
            args.append(kind)
        if person:
            where.append("r.people LIKE ?")
            args.append(f"%{person.strip()}%")
        if status:
            where.append("r.status = ?")
            args.append(status)
        if since is not None:
            where.append("r.created_at >= ?")
            args.append(since)
        if until is not None:
            where.append("r.created_at <= ?")
            args.append(until)

        match = _fts_query(query) if query and query.strip() else ""
        if query and query.strip() and not match:
            return []
        if match:
            sql = ("SELECT r.* FROM records_fts JOIN records r ON r.id = records_fts.rowid"
                   " WHERE records_fts MATCH ?")
            args.insert(0, match)
            if where:
                sql += " AND " + " AND ".join(where)
            sql += " ORDER BY rank LIMIT ?"
        else:
            sql = "SELECT r.* FROM records r"
            if where:
                sql += " WHERE " + " AND ".join(where)
            # Open work reads soonest-due first; everything else newest first.
            sql += (" ORDER BY r.due_at IS NULL, r.due_at, r.created_at DESC" if status == "open"
                    else " ORDER BY r.created_at DESC")
            sql += " LIMIT ?"
        args.append(limit)
        try:
            return [_row(r) for r in self.db.execute(sql, args).fetchall()]
        except sqlite3.OperationalError:
            return []

    def set_status(self, record_id: int, status: str) -> Record:
        record = self.get(record_id)
        if record is None:
            raise KeyError(record_id)
        if status not in STATUSES:
            raise ValueError(f"status must be one of {', '.join(STATUSES)}")
        if record.kind not in TRACKED:
            raise ValueError(f"a {record.kind.replace('_', ' ')} has no status; only "
                             f"commitments, action items and tasks can be done")
        self.db.execute(
            "UPDATE records SET status=?, done_at=? WHERE id=?",
            (status, time.time() if status == "done" else None, record_id),
        )
        self.db.commit()
        return self.get(record_id)

    def set_due(self, record_id: int, due_at: float | None) -> Record:
        """Change when something is due. A new date earns a new follow-up if it passes too."""
        if self.get(record_id) is None:
            raise KeyError(record_id)
        self.db.execute("UPDATE records SET due_at=?, followed_up_at=NULL WHERE id=?", (due_at, record_id))
        self.db.commit()
        return self.get(record_id)

    def people(self, limit: int = 50) -> list[str]:
        """Names on record, most recently mentioned first."""
        seen: dict[str, str] = {}  # lower-case -> the spelling kept
        for row in self.db.execute("SELECT people FROM records WHERE people != '' ORDER BY created_at DESC LIMIT 500"):
            for name in row["people"].split(", "):
                key = name.lower()
                if not name:
                    continue
                if key not in seen:
                    if len(seen) >= limit:
                        return list(seen.values())
                    seen[key] = name
                elif name[:1].isupper() and not seen[key][:1].isupper():
                    seen[key] = name
        return list(seen.values())

    def delete(self, record_id: int) -> bool:
        cur = self.db.execute("DELETE FROM records WHERE id=?", (record_id,))
        self.db.commit()
        return cur.rowcount > 0

    # --- follow-up ------------------------------------------------------------

    def overdue_unannounced(self, now: float | None = None) -> list[Record]:
        """Open tracked items past due that have not been followed up yet."""
        rows = self.db.execute(
            "SELECT * FROM records WHERE status='open' AND due_at IS NOT NULL AND due_at < ?"
            " AND followed_up_at IS NULL ORDER BY due_at",
            (time.time() if now is None else now,),
        ).fetchall()
        return [_row(r) for r in rows]

    def mark_followed_up(self, record_id: int) -> None:
        self.db.execute("UPDATE records SET followed_up_at=? WHERE id=?", (time.time(), record_id))
        self.db.commit()
