"""Meeting notes: timed transcripts, who spoke, summaries, and search over what was said.

Portable. Storage, speaker attribution, the transcription queue and the summary
live here; capturing audio and calling Whisper are injected by the platform layer.

Choices Harsh made (2026-09-17): audio is never stored - each chunk is transcribed
and dropped; transcripts are kept 90 days by default; a spoken or typed "start
taking notes" begins at once, with a visible indicator and a reminder to tell the
other participants. Summaries report what was said, never judgments about people.

Measured before building (see CLAUDE.md): free Whisper covers 8 hours of audio a
day; the PC's own output (the other side of a call) can be captured; the
microphone also hears the call through the speakers; and Whisper invents
"Thank you." from silence, so silent audio is gated out and known phantom lines
are dropped when nobody was speaking.
"""

from __future__ import annotations

import datetime
import json
import logging
import queue
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .memory import FTS_TOKENIZE, _fts_query, estimate_text_tokens
from .providers.base import AllProvidersFailed, Message, ProviderError, RateLimited, RequestTooLarge
from .records import Records, day_label
from .tools.registry import Tier, ToolError, ToolRegistry

log = logging.getLogger("bantu.meetings")

RETENTION_DAYS = 90
#: A frame of RMS below this on both tracks is silence.
SILENCE_RMS = 0.004
#: A track is "active" in a stretch when its mean RMS is above this.
ACTIVE_RMS = 0.008
#: Short, generic lines Whisper produces from near-silence.
PHANTOM_LINES = {
    "thank you", "thank you very much", "thanks for watching", "thank you for watching", "you", "bye",
    "subtitles by the amara org community", "please subscribe", "so", "okay",
}
#: Above this, one request cannot hold the transcript even on Gemini's free tier comfortably.
SINGLE_PASS_TOKENS = 25_000
MAP_CHARS = 12_000

SCHEMA = """
CREATE TABLE IF NOT EXISTS meetings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    started_at  REAL NOT NULL,
    ended_at    REAL,
    status      TEXT NOT NULL,
    summary     TEXT NOT NULL DEFAULT '',
    error       TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS meeting_segments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id  INTEGER NOT NULL,
    start       REAL NOT NULL,
    end_        REAL NOT NULL,
    speaker     TEXT NOT NULL,
    text        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_segments_meeting ON meeting_segments(meeting_id, start);
CREATE VIRTUAL TABLE IF NOT EXISTS meeting_segments_fts USING fts5(
    text, content='meeting_segments', content_rowid='id', tokenize="{TOKENIZE}"
);
CREATE TRIGGER IF NOT EXISTS meeting_segments_ai AFTER INSERT ON meeting_segments BEGIN
    INSERT INTO meeting_segments_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS meeting_segments_ad AFTER DELETE ON meeting_segments BEGIN
    INSERT INTO meeting_segments_fts(meeting_segments_fts, rowid, text) VALUES('delete', old.id, old.text);
END;
"""

SPEAKER_LABELS = {"you": "You", "call": "Call", "room": "Room"}


def clock(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rest = divmod(seconds, 3600)
    return f"{h}:{rest // 60:02d}:{rest % 60:02d}" if h else f"{rest // 60:02d}:{rest % 60:02d}"


# --- audio chunks and who spoke ------------------------------------------------------


@dataclass
class Segment:
    start: float  # seconds from the start of the meeting
    end: float
    text: str
    speaker: str = "room"


@dataclass
class AudioChunk:
    """One stretch of mixed audio, with each track's loudness alongside."""

    wav: bytes
    offset: float           # seconds from the start of the meeting
    frame_s: float          # length of one RMS frame
    mic_rms: list[float]
    call_rms: list[float]   # the PC's own output: the other side of a call

    @property
    def silent(self) -> bool:
        return max(self.mic_rms or [0.0]) < SILENCE_RMS and max(self.call_rms or [0.0]) < SILENCE_RMS

    def mean(self, track: list[float], start: float, end: float) -> float:
        first = max(0, int(start // self.frame_s))
        last = max(first + 1, int(-(-end // self.frame_s)))
        frames = track[first:last]
        return sum(frames) / len(frames) if frames else 0.0


def attribute(chunk: AudioChunk, start: float, end: float) -> str:
    """'call' when the PC was playing speech, 'you' when only the microphone was, else 'room'.

    The microphone also hears the call through the speakers, so call audio wins
    whenever it is active. With no call audio in the whole chunk, the microphone
    is the room - it may be anyone present.
    """
    call = chunk.mean(chunk.call_rms, start, end)
    mic = chunk.mean(chunk.mic_rms, start, end)
    if call >= ACTIVE_RMS:
        return "call"
    if mic >= ACTIVE_RMS and max(chunk.call_rms or [0.0]) >= ACTIVE_RMS:
        return "you"
    return "room"


def is_phantom(text: str, chunk: AudioChunk, start: float, end: float) -> bool:
    """A stock line over near-silence, or no words at all: Whisper made it up."""
    words = " ".join(re.findall(r"[\w']+", text.lower()))
    if not words:
        return True  # "." or "..." - seen live at the end of a recording
    # A fragment of one or two words is also suspect: seen live, a lone "We." after the
    # speech ended, echoed from the previous chunk's text in the prompt.
    if words not in PHANTOM_LINES and len(words.split()) > 2:
        return False
    loud = max(chunk.mean(chunk.mic_rms, start, end), chunk.mean(chunk.call_rms, start, end))
    return loud < ACTIVE_RMS


def label_segments(chunk: AudioChunk, raw: list[Segment]) -> list[Segment]:
    """Speaker labels and meeting-relative times; phantom lines removed."""
    out = []
    for s in raw:
        text = " ".join((s.text or "").split())
        if not text or is_phantom(text, chunk, s.start, s.end):
            continue
        out.append(Segment(chunk.offset + s.start, chunk.offset + s.end, text, attribute(chunk, s.start, s.end)))
    return out


# --- storage ----------------------------------------------------------------------------


@dataclass
class Meeting:
    id: int
    title: str
    started_at: float
    ended_at: float | None
    status: str
    summary: str
    error: str

    @property
    def duration(self) -> float:
        return (self.ended_at or time.time()) - self.started_at

    def describe(self) -> str:
        when = datetime.datetime.fromtimestamp(self.started_at)
        return (f"#{self.id} · {day_label(self.started_at)} {when:%H:%M} · {self.title} · {clock(self.duration)}"
                + ("" if self.status == "done" else f" · {self.status}"))


class Meetings:
    def __init__(self, db: sqlite3.Connection):
        self.db = db
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA.replace("{TOKENIZE}", FTS_TOKENIZE))
        self.db.commit()
        self._lock = threading.Lock()

    def create(self, title: str = "") -> Meeting:
        now = time.time()
        title = " ".join((title or "").split()) or f"Meeting {datetime.datetime.fromtimestamp(now):%d %b %H:%M}"
        with self._lock:
            cur = self.db.execute("INSERT INTO meetings (title, started_at, status) VALUES (?,?,'recording')",
                                  (title, now))
            self.db.commit()
        return self.get(cur.lastrowid)

    def get(self, meeting_id: int) -> Meeting | None:
        row = self.db.execute("SELECT * FROM meetings WHERE id=?", (meeting_id,)).fetchone()
        return Meeting(**dict(row)) if row else None

    def latest(self) -> Meeting | None:
        row = self.db.execute("SELECT id FROM meetings ORDER BY started_at DESC LIMIT 1").fetchone()
        return self.get(row["id"]) if row else None

    def recent(self, limit: int = 50) -> list[Meeting]:
        rows = self.db.execute("SELECT * FROM meetings ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()
        return [Meeting(**dict(r)) for r in rows]

    def update(self, meeting_id: int, **fields: Any) -> None:
        allowed = {"title", "ended_at", "status", "summary", "error"}
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return
        with self._lock:
            self.db.execute(f"UPDATE meetings SET {', '.join(f'{k}=?' for k in sets)} WHERE id=?",
                            (*sets.values(), meeting_id))
            self.db.commit()

    def add_segments(self, meeting_id: int, segments: list[Segment]) -> None:
        if not segments:
            return
        with self._lock:
            self.db.executemany(
                "INSERT INTO meeting_segments (meeting_id, start, end_, speaker, text) VALUES (?,?,?,?,?)",
                [(meeting_id, s.start, s.end, s.speaker, s.text) for s in segments],
            )
            self.db.commit()

    def segments(self, meeting_id: int) -> list[Segment]:
        rows = self.db.execute("SELECT * FROM meeting_segments WHERE meeting_id=? ORDER BY start", (meeting_id,))
        return [Segment(r["start"], r["end_"], r["text"], r["speaker"]) for r in rows]

    def transcript_lines(self, meeting_id: int) -> list[str]:
        return [f"[{clock(s.start)}] {SPEAKER_LABELS.get(s.speaker, s.speaker)}: {s.text}"
                for s in self.segments(meeting_id)]

    def search(self, query: str, since: float | None = None, until: float | None = None,
               limit: int = 12) -> list[dict[str, Any]]:
        match = _fts_query(query)
        if not match:
            return []
        sql = ("SELECT s.start, s.speaker, s.text, m.id AS meeting_id, m.title, m.started_at"
               " FROM meeting_segments_fts JOIN meeting_segments s ON s.id = meeting_segments_fts.rowid"
               " JOIN meetings m ON m.id = s.meeting_id WHERE meeting_segments_fts MATCH ?")
        args: list[Any] = [match]
        if since is not None:
            sql += " AND m.started_at >= ?"
            args.append(since)
        if until is not None:
            sql += " AND m.started_at <= ?"
            args.append(until)
        sql += " ORDER BY rank LIMIT ?"
        args.append(limit)
        try:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]
        except sqlite3.OperationalError:
            return []

    def delete(self, meeting_id: int) -> bool:
        with self._lock:
            self.db.execute("DELETE FROM meeting_segments WHERE meeting_id=?", (meeting_id,))
            cur = self.db.execute("DELETE FROM meetings WHERE id=?", (meeting_id,))
            self.db.commit()
        return cur.rowcount > 0

    def prune(self, days: int = RETENTION_DAYS) -> int:
        """Delete transcripts older than `days` (0 keeps everything). Records saved from them stay."""
        if days <= 0:
            return 0
        cutoff = time.time() - days * 86400
        old = [r["id"] for r in self.db.execute(
            "SELECT id FROM meetings WHERE started_at < ? AND status != 'recording'", (cutoff,))]
        for meeting_id in old:
            self.delete(meeting_id)
        return len(old)


# --- summaries ----------------------------------------------------------------------------

SUMMARY_SYSTEM = """You write meeting notes from a transcript. Each line is [time] Speaker: words. "You" is
the user; "Call" is anyone on the other side of a call; "Room" is anyone in the room.

Report only what was said. Never judge, characterise or guess at anyone's feelings or intentions, and
never add anything that is not in the transcript. Write in the main language of the meeting.

Return only JSON with these keys:
"title": a short title, at most 8 words;
"summary": two to four sentences;
"topics": a list of short strings;
"decisions": a list of self-contained sentences;
"action_items": a list of {{"text": what must be done, "owner": who, or "", "due": "YYYY-MM-DD" or ""}};
"commitments": promises "You" made, as a list of {{"text": ..., "due": "YYYY-MM-DD" or ""}}.
Use empty lists when there are none. The meeting started on {date}; resolve relative days against it."""

PART_SYSTEM = """Write concise notes of this part of a meeting transcript: key points, decisions, action
items with owners and due dates, and promises made by "You". Keep the times. Report only what was said,
never judgments about people."""


@dataclass
class MeetingSummary:
    title: str = ""
    summary: str = ""
    topics: list[str] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    action_items: list[dict[str, str]] = field(default_factory=list)
    commitments: list[dict[str, str]] = field(default_factory=list)


def parse_summary(text: str) -> MeetingSummary:
    """The model's JSON, forgiving code fences and stray prose; plain text becomes the summary."""
    match = re.search(r"\{.*\}", text or "", re.S)
    try:
        data = json.loads(match.group(0)) if match else None
    except ValueError:
        data = None
    if not isinstance(data, dict):
        return MeetingSummary(summary=(text or "").strip())

    def strings(key: str) -> list[str]:
        return [str(x).strip() for x in data.get(key) or [] if str(x).strip()]

    def items(key: str) -> list[dict[str, str]]:
        out = []
        for x in data.get(key) or []:
            if isinstance(x, str):
                x = {"text": x}
            if isinstance(x, dict) and str(x.get("text", "")).strip():
                out.append({"text": str(x["text"]).strip(), "owner": str(x.get("owner") or "").strip(),
                            "due": str(x.get("due") or "").strip()})
        return out

    return MeetingSummary(str(data.get("title") or "").strip(), str(data.get("summary") or "").strip(),
                          strings("topics"), strings("decisions"), items("action_items"), items("commitments"))


ChatFn = Callable[[list[Message], str], str]


def summarize(lines: list[str], started_at: float, chat: ChatFn) -> MeetingSummary:
    """One pass when the transcript fits; otherwise notes per part, then one summary of the notes."""
    system = SUMMARY_SYSTEM.format(date=datetime.datetime.fromtimestamp(started_at).strftime("%A %d %B %Y"))
    transcript = "\n".join(lines)
    if estimate_text_tokens(transcript) <= SINGLE_PASS_TOKENS:
        try:
            return parse_summary(chat([Message.user(transcript)], system))
        except AllProvidersFailed as e:
            # Too large for one request: parts will fit. Out of quota everywhere: more requests will not help.
            if not any(isinstance(f, RequestTooLarge) for f in e.failures.values()):
                raise
            log.info("meeting summary did not fit in one request; summarising in parts")
    parts, current = [], ""
    for line in lines:
        if current and len(current) + len(line) + 1 > MAP_CHARS:
            parts.append(current)
            current = ""
        current = f"{current}\n{line}" if current else line
    if current:
        parts.append(current)
    notes = [chat([Message.user(part)], PART_SYSTEM) for part in parts]
    return parse_summary(chat([Message.user("Notes from each part of the meeting, in order:\n\n"
                                            + "\n\n".join(notes))], system))


def _due(text: str) -> float | None:
    try:
        return datetime.datetime.fromisoformat(text.strip()[:10] + "T23:59").astimezone().timestamp()
    except ValueError:
        return None


def save_to_memory(summary: MeetingSummary, records: Records, meeting_id: int) -> int:
    """Decisions, action items and the user's own commitments become records. Returns how many."""
    source = f"meeting:{meeting_id}"
    saved = 0
    for text in summary.decisions:
        records.add("decision", text, conversation=source)
        saved += 1
    for item in summary.action_items:
        records.add("action_item", item["text"], item.get("owner", ""), _due(item.get("due", "")), conversation=source)
        saved += 1
    for item in summary.commitments:
        records.add("commitment", item["text"], due_at=_due(item.get("due", "")), conversation=source)
        saved += 1
    return saved


def render_summary(meeting: Meeting, summary: MeetingSummary, saved: int) -> str:
    lines = [f"{summary.title or meeting.title} ({clock(meeting.duration)})"]
    if summary.summary:
        lines.append(summary.summary)
    if summary.decisions:
        lines.append("Decisions:\n" + "\n".join(f"- {d}" for d in summary.decisions))
    if summary.action_items:
        lines.append("Action items:\n" + "\n".join(
            f"- {a['text']}" + (f" ({a['owner']})" if a.get("owner") else "") + (f", due {a['due']}" if a.get("due") else "")
            for a in summary.action_items))
    if summary.commitments:
        lines.append("You promised:\n" + "\n".join(f"- {c['text']}" for c in summary.commitments))
    if saved:
        lines.append(f"Saved {saved} item{'s' if saved != 1 else ''} to memory.")
    return "\n\n".join(lines)


# --- the running session ------------------------------------------------------------------

#: (wav, prompt) -> segments. The prompt carries names to spell and the end of the
#: previous chunk, which Whisper uses as context across the boundary.
TranscribeFn = Callable[[bytes, str], list[Segment]]
#: Whisper reads at most 224 tokens of prompt; keep well under.
PROMPT_CHARS = 500
StatusFn = Callable[[str], None]


class MeetingSession:
    """Transcribes chunks on a worker thread as they arrive, so a crash loses at most one chunk."""

    #: Longest a rate-limited chunk waits before trying again.
    MAX_RETRY_WAIT_S = 300

    def __init__(self, store: Meetings, meeting_id: int, transcribe: TranscribeFn, on_status: StatusFn,
                 glossary: str = ""):
        self.store = store
        self.meeting_id = meeting_id
        self.transcribe = transcribe
        self.on_status = on_status
        self.glossary = glossary
        self._previous = ""
        self._queue: queue.Queue = queue.Queue()
        self.chunks_done = 0
        self.chunks_skipped = 0
        self.failures: list[str] = []
        self._worker = threading.Thread(target=self._run, name="bantu-meeting-transcribe", daemon=True)
        self._worker.start()

    def submit(self, chunk: AudioChunk) -> None:
        self._queue.put(chunk)

    def finish(self, timeout: float = 600) -> None:
        """Wait for every submitted chunk to be transcribed."""
        self._queue.put(None)
        self._worker.join(timeout)

    def _run(self) -> None:
        while True:
            chunk = self._queue.get()
            if chunk is None:
                return
            if chunk.silent:
                self.chunks_skipped += 1  # never sent: Whisper would invent words
                continue
            prompt = self.prompt()
            while True:
                try:
                    raw = self.transcribe(chunk.wav, prompt)
                    break
                except RateLimited as e:
                    wait = min(self.MAX_RETRY_WAIT_S, e.retry_after or 60)
                    self.on_status(f"waiting {wait:.0f}s for the transcription quota")
                    time.sleep(wait)
                except Exception as e:  # one bad chunk must not end the meeting
                    log.warning("chunk at %s could not be transcribed: %s", clock(chunk.offset), e)
                    self.failures.append(f"{clock(chunk.offset)}: {e}")
                    raw = []
                    break
            segments = label_segments(chunk, raw)
            self.store.add_segments(self.meeting_id, segments)
            if segments:
                self._previous = " ".join(s.text for s in segments)
            self.chunks_done += 1

    def prompt(self) -> str:
        """Names worth spelling right, then the end of what was just said."""
        glossary = self.glossary[:PROMPT_CHARS]
        room = PROMPT_CHARS - len(glossary) - 1
        tail = self._previous[-room:] if self._previous and room > 0 else ""
        return " ".join(p for p in (glossary, tail) if p).strip()


class Recorder:
    """What the platform provides: capture that calls back with AudioChunks."""

    def start(self) -> None: ...
    def pause(self) -> None: ...
    def resume(self) -> None: ...
    def stop(self) -> None: ...  # flushes the last partial chunk before returning


RecorderFactory = Callable[[Callable[[AudioChunk], None]], Recorder]


class MeetingNotes:
    """Start, pause, stop; transcribe as it goes; summarise and save at the end."""

    def __init__(self, store: Meetings, records: Records, recorder_factory: RecorderFactory,
                 transcribe: TranscribeFn, chat: ChatFn, on_status: StatusFn = lambda s: None,
                 on_done: Callable[[int, str], None] = lambda m, t: None,
                 known_names: Callable[[], list[str]] = lambda: []):
        self.store = store
        self.records = records
        self.recorder_factory = recorder_factory
        self.transcribe = transcribe
        self.chat = chat
        self.on_status = on_status
        self.on_done = on_done
        self.known_names = known_names
        self._lock = threading.Lock()
        self.meeting: Meeting | None = None
        self._recorder: Recorder | None = None
        self._session: MeetingSession | None = None
        self.paused = False

    @property
    def recording(self) -> bool:
        return self.meeting is not None

    def start(self, title: str = "") -> Meeting:
        with self._lock:
            if self.meeting is not None:
                raise RuntimeError(f"already recording '{self.meeting.title}'")
            meeting = self.store.create(title)
            session = MeetingSession(self.store, meeting.id, self.transcribe, self.on_status, self.glossary(title))
            try:
                recorder = self.recorder_factory(session.submit)
                recorder.start()
            except Exception as e:
                session.finish(timeout=5)
                self.store.update(meeting.id, status="failed", ended_at=time.time(), error=str(e))
                raise RuntimeError(f"recording could not start: {e}") from e
            self.meeting, self._recorder, self._session, self.paused = meeting, recorder, session, False
        self.on_status("recording")
        return meeting

    def glossary(self, title: str = "") -> str:
        """'Launch sync. Names: Ram, Sita, Harsh.' - found live: without it, "Sita" came back "CETA"."""
        try:
            chosen: dict[str, str] = {}
            for name in self.known_names():
                name = (name or "").strip()
                key = name.lower()
                # One spelling per person, preferring a capitalised one: it is what Whisper should write.
                if name and (key not in chosen or (name[:1].isupper() and not chosen[key][:1].isupper())):
                    chosen[key] = name
            names = list(chosen.values())[:25]
        except Exception:
            names = []
        title = title.strip()
        if title and title[-1] not in ".!?":
            title += "."
        parts = [title] if title else []
        if names:
            parts.append("Names: " + ", ".join(names) + ".")
        return " ".join(parts)

    def pause(self) -> None:
        if self._recorder and not self.paused:
            self._recorder.pause()
            self.paused = True
            self.store.update(self.meeting.id, status="paused")

    def resume(self) -> None:
        if self._recorder and self.paused:
            self._recorder.resume()
            self.paused = False
            self.store.update(self.meeting.id, status="recording")

    def stop(self, wait: bool = False) -> Meeting:
        """Stop capturing now; transcription and the summary finish in the background."""
        with self._lock:
            if self.meeting is None:
                raise RuntimeError("no meeting is being recorded")
            meeting, recorder, session = self.meeting, self._recorder, self._session
            self.meeting = self._recorder = self._session = None
            self.paused = False
        recorder.stop()
        self.store.update(meeting.id, status="processing", ended_at=time.time())
        worker = threading.Thread(target=self._finish, args=(meeting.id, session), name="bantu-meeting-summary",
                                  daemon=True)
        worker.start()
        if wait:
            worker.join()
        return self.store.get(meeting.id)

    def _finish(self, meeting_id: int, session: MeetingSession) -> None:
        self.on_status("transcribing the last part")
        session.finish()
        meeting = self.store.get(meeting_id)
        lines = self.store.transcript_lines(meeting_id)
        if not lines:
            self.store.update(meeting_id, status="done", summary="Nothing was said that could be transcribed.")
            self.on_done(meeting_id, f"{meeting.title}: nothing was said that could be transcribed.")
            return
        self.on_status("writing the meeting summary")
        try:
            summary = summarize(lines, meeting.started_at, self.chat)
        except ProviderError as e:
            note = f"The transcript is saved, but the summary could not be written: {e}"
            self.store.update(meeting_id, status="done", error=str(e), summary="")
            self.on_done(meeting_id, f"{meeting.title} ({clock(meeting.duration)}). {note}")
            return
        saved = save_to_memory(summary, self.records, meeting_id)
        if summary.title:
            self.store.update(meeting_id, title=summary.title)
        meeting = self.store.get(meeting_id)
        text = render_summary(meeting, summary, saved)
        if session.failures:
            text += f"\n\n{len(session.failures)} part(s) could not be transcribed."
        self.store.update(meeting_id, status="done", summary=text)
        self.on_done(meeting_id, text)


# --- tools ----------------------------------------------------------------------------------


def _meeting_ref(store: Meetings, meeting_id: int) -> Meeting:
    meeting = store.get(meeting_id) if meeting_id > 0 else store.latest()
    if meeting is None:
        raise ToolError("there is no such meeting" if meeting_id > 0 else "no meetings have been recorded yet")
    return meeting


def register(reg: ToolRegistry, notes: MeetingNotes) -> None:
    store = notes.store
    reg.describe_category(
        "meetings", "record and take notes of a meeting or call, and look up what was said, decided and "
                    "assigned in past meetings",
    )

    @reg.register(tier=Tier.AUTO, category="meetings")
    def start_meeting_notes(title: str = "") -> str:
        """Start recording and transcribing a meeting: the microphone and the computer's own audio.

        Tell the user it has started, and remind them to let everyone present know it is being
        recorded.

        Args:
            title: What the meeting is about, if they said. Leave empty otherwise.
        """
        try:
            meeting = notes.start(title)
        except RuntimeError as e:
            raise ToolError(str(e)) from e
        return (f"Recording started: {meeting.title}. Audio is transcribed as it goes and never kept. "
                f"Remind the user to tell everyone present that the meeting is being recorded.")

    @reg.register(tier=Tier.AUTO, category="meetings")
    def stop_meeting_notes() -> str:
        """Stop recording the current meeting. The summary follows in a minute or two."""
        try:
            meeting = notes.stop()
        except RuntimeError as e:
            raise ToolError(str(e)) from e
        return (f"Stopped recording {meeting.title} after {clock(meeting.duration)}. The last part is being "
                f"transcribed and the summary will appear when it is ready.")

    @reg.register(tier=Tier.AUTO, category="meetings")
    def list_meetings() -> str:
        """List recorded meetings, newest first."""
        meetings = store.recent(20)
        return "\n".join(m.describe() for m in meetings) if meetings else "No meetings have been recorded."

    @reg.register(tier=Tier.AUTO, category="meetings")
    def get_meeting(meeting_id: int = 0) -> str:
        """The summary of a recorded meeting: decisions, action items and what the user promised.

        Args:
            meeting_id: The meeting number from list_meetings. 0 means the most recent.
        """
        meeting = _meeting_ref(store, meeting_id)
        if meeting.status in ("recording", "paused"):
            return f"{meeting.describe()} is still being recorded."
        if meeting.status == "processing":
            return f"{meeting.describe()}: the summary is still being written."
        return f"{meeting.describe()}\n\n{meeting.summary or 'No summary was written.'}"

    @reg.register(tier=Tier.AUTO, category="meetings")
    def search_meetings(query: str, since: str = "", until: str = "") -> str:
        """Search what was said in recorded meetings. Each result gives the meeting, date, time and speaker.

        If it finds nothing, say there is no record of it in the meetings - never guess.

        Args:
            query: Words to look for.
            since: Only meetings on or after this date, e.g. '2026-09-01'.
            until: Only meetings on or before this date.
        """
        try:
            start = datetime.datetime.fromisoformat(since.strip()[:10]).timestamp() if since.strip() else None
            end = datetime.datetime.fromisoformat(until.strip()[:10]).timestamp() + 86400 if until.strip() else None
        except ValueError as e:
            raise ToolError("dates must look like '2026-09-21'") from e
        hits = store.search(query, start, end)
        if not hits:
            return f"Nothing matching {query!r} was said in any recorded meeting. Say there is no record of it."
        return "\n".join(
            f"- {h['title']} ({day_label(h['started_at'])}) at {clock(h['start'])}, "
            f"{SPEAKER_LABELS.get(h['speaker'], h['speaker'])}: {h['text']}"
            for h in hits
        )

    @reg.register(tier=Tier.CONFIRM, category="meetings")
    def delete_meeting(meeting_id: int) -> str:
        """Delete a recorded meeting's transcript and summary. Notes already saved to memory stay.

        Args:
            meeting_id: The meeting number from list_meetings.
        """
        meeting = _meeting_ref(store, meeting_id)
        if meeting.status in ("recording", "paused"):
            raise ToolError("stop the recording before deleting it")
        store.delete(meeting.id)
        return f"Deleted {meeting.describe()}."
