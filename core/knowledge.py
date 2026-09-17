"""A folder of documents Bantu can search: drop files in to teach it.

Portable. The index is SQLite FTS5 beside everything else, so nothing is
downloaded. Reading PDFs or Word files is injected by the platform layer, and
the folder is polled rather than watched, so the stdlib is enough: a poll only
compares file times and sizes, and re-reads what changed.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .memory import FTS_TOKENIZE, _fts_query
from .tools.registry import Tier, ToolRegistry

log = logging.getLogger("bantu.knowledge")

ReaderFn = Callable[[Path], str]

#: Roughly a few paragraphs: enough context to answer from, small enough that
#: four matches stay well inside one Groq request.
CHUNK_CHARS = 1000
MAX_FILE_BYTES = 25 * 1024 * 1024
#: Text indexed per file. A 500-page manual is still searchable in its first ~100 pages.
MAX_INDEXED_CHARS = 400_000
POLL_SECONDS = 30
SEARCH_RESULTS = 4

SCHEMA = """
CREATE TABLE IF NOT EXISTS knowledge_files (
    path        TEXT PRIMARY KEY,
    mtime       REAL NOT NULL,
    size        INTEGER NOT NULL,
    chunks      INTEGER NOT NULL DEFAULT 0,
    error       TEXT,
    indexed_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS knowledge_chunks (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    path  TEXT NOT NULL,
    n     INTEGER NOT NULL,
    text  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_path ON knowledge_chunks(path);
CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
    text, content='knowledge_chunks', content_rowid='id', tokenize="{TOKENIZE}"
);
CREATE TRIGGER IF NOT EXISTS knowledge_ai AFTER INSERT ON knowledge_chunks BEGIN
    INSERT INTO knowledge_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS knowledge_ad AFTER DELETE ON knowledge_chunks BEGIN
    INSERT INTO knowledge_fts(knowledge_fts, rowid, text) VALUES('delete', old.id, old.text);
END;
"""

_SENTENCE_END = re.compile(r"(?<=[.!?।॥])\s+")


def read_text_file(path: Path) -> str:
    raw = path.read_bytes()
    # UTF-16 only with its byte-order mark, as Notepad writes it: any even-length
    # run of bytes "decodes" as UTF-16, so without the mark old Windows-1252
    # text came out as nonsense instead of falling through.
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


def chunk_text(text: str, size: int = CHUNK_CHARS) -> list[str]:
    """Split text into pieces of up to `size` characters, at paragraphs, then sentences."""
    pieces: list[str] = []
    for para in re.split(r"\n\s*\n", text or ""):
        para = " ".join(para.split())
        if not para:
            continue
        if len(para) <= size:
            pieces.append(para)
            continue
        for sentence in _SENTENCE_END.split(para):
            while len(sentence) > size:  # one enormous "sentence": cut it
                pieces.append(sentence[:size])
                sentence = sentence[size:]
            if sentence.strip():
                pieces.append(sentence.strip())

    chunks: list[str] = []
    current = ""
    for piece in pieces:
        if current and len(current) + 1 + len(piece) > size:
            chunks.append(current)
            current = piece
        else:
            current = f"{current}\n{piece}" if current else piece
    if current:
        chunks.append(current)
    return chunks


@dataclass
class SyncReport:
    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.added or self.updated or self.removed or self.failed)


class Knowledge:
    """Index and search the documents in one folder."""

    def __init__(self, db: sqlite3.Connection, folder: Path, readers: dict[str, ReaderFn] | None = None):
        self.db = db
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA.replace("{TOKENIZE}", FTS_TOKENIZE))
        self.db.commit()
        self.folder = Path(folder)
        self.readers: dict[str, ReaderFn] = {".txt": read_text_file, ".md": read_text_file, ".csv": read_text_file}
        self.readers.update({k.lower(): v for k, v in (readers or {}).items()})
        self._sync_lock = threading.Lock()
        self._stop = threading.Event()
        self._poller: threading.Thread | None = None

    @property
    def suffixes(self) -> list[str]:
        return sorted(self.readers)

    # --- indexing ---------------------------------------------------------------

    def _candidates(self) -> dict[str, tuple[float, int]]:
        found: dict[str, tuple[float, int]] = {}
        if not self.folder.is_dir():
            return found
        for p in self.folder.rglob("*"):
            # "~$report.docx" is Word's lock file for an open document, not a document.
            if p.name.startswith(("~$", ".")) or p.suffix.lower() not in self.readers:
                continue
            try:
                if not p.is_file():
                    continue
                st = p.stat()
            except OSError:
                continue
            found[p.relative_to(self.folder).as_posix()] = (st.st_mtime, st.st_size)
        return found

    def sync(self) -> SyncReport:
        """Bring the index in line with the folder: new, changed and removed files."""
        report = SyncReport()
        with self._sync_lock:
            try:
                self.folder.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                log.warning("knowledge folder unavailable: %s", e)
                return report
            on_disk = self._candidates()
            known = {r["path"]: (r["mtime"], r["size"])
                     for r in self.db.execute("SELECT path, mtime, size FROM knowledge_files")}
            for rel in sorted(set(known) - set(on_disk)):
                self._forget(rel)
                report.removed.append(rel)
            for rel, (mtime, size) in sorted(on_disk.items()):
                if known.get(rel) == (mtime, size):
                    continue
                error = self._index(rel, mtime, size)
                if error:
                    report.failed.append((rel, error))
                else:
                    (report.updated if rel in known else report.added).append(rel)
            self.db.commit()
        if report.changed:
            log.info("knowledge sync: %d added, %d updated, %d removed, %d failed",
                     len(report.added), len(report.updated), len(report.removed), len(report.failed))
        return report

    def _forget(self, rel: str) -> None:
        self.db.execute("DELETE FROM knowledge_chunks WHERE path=?", (rel,))
        self.db.execute("DELETE FROM knowledge_files WHERE path=?", (rel,))

    def _index(self, rel: str, mtime: float, size: int) -> str | None:
        path = self.folder / rel
        error, chunks = None, []
        if size > MAX_FILE_BYTES:
            error = f"larger than {MAX_FILE_BYTES // (1024 * 1024)} MB"
        else:
            try:
                text = self.readers[path.suffix.lower()](path)
                chunks = chunk_text(text[:MAX_INDEXED_CHARS])
                if not chunks:
                    error = "no readable text (a scanned PDF has none)"
            except Exception as e:  # one unreadable file must not stop the rest
                error = f"{type(e).__name__}: {e}"[:200]
        self.db.execute("DELETE FROM knowledge_chunks WHERE path=?", (rel,))
        self.db.executemany(
            "INSERT INTO knowledge_chunks (path, n, text) VALUES (?,?,?)",
            [(rel, i + 1, c) for i, c in enumerate(chunks)],
        )
        # A failed file is still recorded with its time and size, so it is not
        # re-read on every poll - only when it changes.
        self.db.execute(
            "INSERT OR REPLACE INTO knowledge_files (path, mtime, size, chunks, error, indexed_at)"
            " VALUES (?,?,?,?,?,?)",
            (rel, mtime, size, len(chunks), error, time.time()),
        )
        return error

    # --- reading ----------------------------------------------------------------

    def search(self, query: str, limit: int = SEARCH_RESULTS, all_words: bool = False) -> list[dict[str, Any]]:
        match = _fts_query(query, all_words=all_words)
        if not match:
            return []
        try:
            rows = self.db.execute(
                "SELECT c.path, c.n, c.text FROM knowledge_fts JOIN knowledge_chunks c"
                " ON c.id = knowledge_fts.rowid WHERE knowledge_fts MATCH ? ORDER BY rank LIMIT ?",
                (match, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [dict(r) for r in rows]

    def files(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.execute(
            "SELECT path, chunks, error, indexed_at FROM knowledge_files ORDER BY path")]

    def summary(self) -> str:
        files = self.files()
        readable = sum(1 for f in files if not f["error"])
        text = f"{readable} document{'s' if readable != 1 else ''} indexed"
        failed = len(files) - readable
        return text + (f", {failed} could not be read" if failed else "")

    # --- polling ----------------------------------------------------------------

    def start_polling(self, every: float = POLL_SECONDS) -> None:
        """Sync now and then every `every` seconds, on a daemon thread."""
        if self._poller and self._poller.is_alive():
            return
        self._stop.clear()

        def loop() -> None:
            while not self._stop.is_set():
                try:
                    self.sync()
                except Exception:
                    log.exception("knowledge sync failed")
                self._stop.wait(every)

        self._poller = threading.Thread(target=loop, name="bantu-knowledge", daemon=True)
        self._poller.start()

    def stop_polling(self) -> None:
        self._stop.set()
        if self._poller:
            self._poller.join(timeout=2)


def register(reg: ToolRegistry, knowledge: Knowledge) -> None:
    # Found live: "according to my documents" sent the model to search_files across
    # the disk eight times before it tried this group. The line has to claim the phrase.
    reg.describe_category(
        "knowledge",
        "the user's own documents, notes, policies and manuals, kept for you to answer from - "
        "use this first when they ask what their documents or notes say",
    )

    @reg.register(tier=Tier.AUTO, category="knowledge")
    def search_knowledge(query: str) -> str:
        """Search the user's knowledge folder: documents they added so you can answer from them.

        Use it when a question may be answered by their own notes, policies, manuals or
        reference files. Answer from what it returns and name the file. If it finds
        nothing, say their documents do not cover it rather than answering from memory.

        Args:
            query: A few key words to look for, in the language the documents are written in.
        """
        knowledge.sync()  # a file dropped in a moment ago counts
        hits = knowledge.search(query, all_words=True)
        note = ""
        if not hits:
            # Any-word matches are only near misses: "parking policy" finds the leave
            # policy on "policy" alone, which must not read like an answer.
            hits = knowledge.search(query)
            note = (f"No passage mentions all of {query!r}. These only share some words with it, "
                    f"so they may not answer the question:\n\n")
        if not hits:
            return (f"Nothing in the knowledge folder matches {query!r} ({knowledge.summary()}). "
                    f"If you have not yet, try once with other key words; otherwise say the documents "
                    f"do not cover it.")
        return note + "\n\n".join(f"From {h['path']} (part {h['n']}):\n{h['text']}" for h in hits)

    @reg.register(tier=Tier.AUTO, category="knowledge")
    def list_knowledge() -> str:
        """List the documents in the user's knowledge folder and whether each could be read."""
        knowledge.sync()
        files = knowledge.files()
        if not files:
            return (f"The knowledge folder is empty: {knowledge.folder}. Files of these types can be "
                    f"added: {', '.join(knowledge.suffixes)}.")
        lines = [f"Knowledge folder: {knowledge.folder} ({knowledge.summary()})"]
        for f in files:
            lines.append(f"- {f['path']}: " + (f"could not be read ({f['error']})" if f["error"]
                                                 else f"{f['chunks']} part(s)"))
        return "\n".join(lines)
