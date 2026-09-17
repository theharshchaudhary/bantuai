"""R4 tests: meeting notes - speakers, phantom lines, storage, summaries, the session, tools.

No API key, no microphone, no network: audio chunks, Whisper and the model are faked.

    .venv/Scripts/python.exe tests/test_r4_meetings.py
"""

from __future__ import annotations

import datetime
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

os.environ["APPDATA"] = tempfile.mkdtemp(prefix="bantu_r4_appdata_")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

import core.meetings as meetings_module
from core.meetings import (
    AudioChunk, MeetingNotes, Meetings, MeetingSession, MeetingSummary, Segment, attribute, clock,
    is_phantom, label_segments, parse_summary, register, render_summary, save_to_memory, summarize,
)
from core.memory import Memory
from core.providers.base import AllProvidersFailed, RateLimited, RequestTooLarge, ToolCall
from core.records import Records
from core.tools.registry import Tier, ToolRegistry

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name + (f"  -> {detail}" if detail and not cond else ""))


def raises(fn, exc):
    try:
        fn()
        return False
    except exc:
        return True


def chunk(mic, call, offset=0.0, frame_s=0.5):
    return AudioChunk(b"RIFFfake", offset, frame_s, list(mic), list(call))


def fresh():
    memory = Memory(Path(tempfile.mkdtemp()) / "m.db")
    return memory, Meetings(memory.db), Records(memory.db)


LOUD, QUIET = 0.05, 0.0005

# --- who spoke ---------------------------------------------------------------------

print("\n[who spoke]")
# 10 frames = 5 seconds. Frames 0-3: the call is talking (the mic hears it too, through the speakers).
# Frames 4-7: only the microphone. Frames 8-9: silence.
c = chunk(mic=[0.03] * 4 + [LOUD] * 4 + [QUIET] * 2, call=[LOUD] * 4 + [QUIET] * 6, offset=120)
check("call audio wins, even though the mic hears it through the speakers", attribute(c, 0.0, 2.0) == "call")
check("mic-only speech during a call is you", attribute(c, 2.0, 4.0) == "you")
in_person = chunk(mic=[LOUD] * 10, call=[QUIET] * 10)
check("with no call audio at all, the microphone is the room", attribute(in_person, 0.0, 5.0) == "room")
check("a chunk quiet on both tracks is silent", chunk([QUIET] * 6, [QUIET] * 6).silent and not c.silent)

check("'Thank you.' over silence is a phantom line", is_phantom("Thank you.", c, 4.0, 5.0))
check("'Thank you.' over real speech is kept", not is_phantom("Thank you.", c, 2.0, 4.0))
check("real words over silence are kept (only stock lines are dropped)",
      not is_phantom("The launch moves to the fourteenth", c, 4.0, 5.0))
labelled = label_segments(c, [Segment(0.0, 2.0, "  Ram, the backend needs another week. "),
                              Segment(2.0, 4.0, "Understood, I'll tell marketing."),
                              Segment(4.0, 5.0, "Thank you."), Segment(4.5, 5.0, "   ")])
check("segments get meeting-relative times, labels and tidy text",
      [(s.start, s.speaker, s.text) for s in labelled] == [
          (120.0, "call", "Ram, the backend needs another week."), (122.0, "you", "Understood, I'll tell marketing.")],
      str(labelled))
check("clock formats minutes and hours", clock(75) == "01:15" and clock(3725) == "1:02:05" and clock(-3) == "00:00")

# --- storage ---------------------------------------------------------------------------

print("\n[storage]")
memory, store, records = fresh()
m = store.create()
check("a meeting gets a dated title when none is given", m.title.startswith("Meeting ") and m.status == "recording")
named = store.create("  Vendor   review ")
check("a given title is tidied", named.title == "Vendor review")
store.add_segments(m.id, [Segment(65, 70, "The launch moves to August fourteenth.", "call"),
                          Segment(5, 9, "Good morning, let's start.", "you"),
                          Segment(3700, 3705, "बजट सोमवार तक भेजना है।", "room")])
check("the transcript is in time order with speakers",
      store.transcript_lines(m.id) == ["[00:05] You: Good morning, let's start.",
                                       "[01:05] Call: The launch moves to August fourteenth.",
                                       "[1:01:40] Room: बजट सोमवार तक भेजना है।"], str(store.transcript_lines(m.id)))
hits = store.search("launch fourteenth")
check("search finds what was said, with meeting, time and speaker",
      hits and hits[0]["meeting_id"] == m.id and hits[0]["start"] == 65 and hits[0]["speaker"] == "call", str(hits))
check("Hindi is searchable too", store.search("बजट") and store.search("बजट")[0]["text"].startswith("बजट"))
check("date filters apply to when the meeting was", store.search("launch", since=time.time() + 3600) == [])
store.update(m.id, status="done", ended_at=m.started_at + 3720, summary="All good.")
got = store.get(m.id)
check("a finished meeting describes its length", "1:02:00" in got.describe() and "done" not in got.describe(),
      got.describe())
check("the latest meeting is the newest", store.latest().id == named.id)
check("delete removes the meeting and what was said", store.delete(m.id) and store.get(m.id) is None
      and store.search("launch") == [])

old = store.create("Last spring")
store.update(old.id, status="done")
memory.db.execute("UPDATE meetings SET started_at = started_at - ? WHERE id = ?", (91 * 86400, old.id))
live = store.create("Still recording")
memory.db.execute("UPDATE meetings SET started_at = started_at - ? WHERE id = ?", (200 * 86400, live.id))
memory.db.commit()
check("keeping everything (0 days) prunes nothing", store.prune(0) == 0)
check("transcripts older than 90 days are pruned, but never one still recording",
      store.prune(90) == 1 and store.get(old.id) is None and store.get(live.id) is not None)

# --- summaries ---------------------------------------------------------------------------

print("\n[summaries]")
good = parse_summary('Here are the notes:\n```json\n{"title": "Launch review", "summary": "Launch slips a week.",'
                     ' "topics": ["launch"], "decisions": ["Launch moves to 14 August."],'
                     ' "action_items": [{"text": "Revise the campaign plan", "owner": "Sita", "due": "2026-09-19"},'
                     ' "Book the room"], "commitments": [{"text": "Call the landlord on Monday", "due": "2026-09-21"}]}\n```')
check("JSON is read even inside a code fence with prose around it",
      good.title == "Launch review" and good.decisions == ["Launch moves to 14 August."])
check("a bare string action item becomes an item with no owner",
      good.action_items[1] == {"text": "Book the room", "owner": "", "due": ""})
check("a reply that is not JSON becomes the summary rather than being lost",
      parse_summary("The meeting covered the launch.").summary == "The meeting covered the launch.")

calls = []


def fake_chat(reply_for):
    def chat(messages, system):
        calls.append((system, messages[0].content))
        return reply_for(system, messages[0].content)
    return chat


started = datetime.datetime(2026, 9, 17, 10, 0).timestamp()
lines = ["[00:05] You: Good morning.", "[01:05] Call: The launch moves to August fourteenth."]
summary = summarize(lines, started, fake_chat(lambda s, u: json.dumps({"title": "Launch", "decisions": ["Moved."]})))
check("a short meeting is summarised in one request", len(calls) == 1 and summary.decisions == ["Moved."])
check("the prompt forbids judging people and dates the meeting",
      "Never judge" in calls[0][0] and "Thursday 17 September 2026" in calls[0][0], calls[0][0][:300])

calls.clear()
long_lines = [f"[{clock(i * 10)}] Call: " + "We discussed the vendor dashboard timeline in detail. " * 4 for i in range(900)]
summary = summarize(long_lines, started, fake_chat(
    lambda s, u: "notes for this part" if "this part" in s else json.dumps({"title": "Long", "summary": "Long one."})))
parts = sum(1 for sys_prompt, _ in calls if "this part" in sys_prompt)
check("a very long meeting is summarised in parts, then once more from the notes",
      parts > 1 and "this part" not in calls[-1][0] and summary.title == "Long", f"{parts} parts")
check("every part is small enough for one Groq request",
      all(len(u) <= meetings_module.MAP_CHARS + 400 for s, u in calls if "this part" in s))

calls.clear()


def too_large_then_fine(system, user):
    if "this part" not in system and len(calls) == 1:
        raise AllProvidersFailed({"groq": RequestTooLarge("413"), "gemini": RateLimited("out of quota")})
    return "part notes" if "this part" in system else json.dumps({"title": "Recovered"})


check("when one request is too large everywhere, it falls back to parts",
      summarize(lines, started, fake_chat(too_large_then_fine)).title == "Recovered" and len(calls) >= 3)


def out_of_quota(system, user):
    raise AllProvidersFailed({"groq": RateLimited("quota"), "gemini": RateLimited("quota")})


check("when everything is out of quota it gives up instead of sending more requests",
      raises(lambda: summarize(lines, started, out_of_quota), AllProvidersFailed))

memory, store, records = fresh()
meeting = store.create("Launch review")
saved = save_to_memory(good, records, meeting.id)
found = records.find(limit=50)
check("decisions, action items and your commitments become records", saved == 4 and
      {r.kind for r in found} == {"decision", "action_item", "commitment"})
sita = next(r for r in found if r.kind == "action_item" and r.people)
check("an action item keeps its owner and due date", sita.people == ["Sita"] and
      datetime.date.fromtimestamp(sita.due_at).isoformat() == "2026-09-19")
check("records point back to the meeting", all(r.conversation == f"meeting:{meeting.id}" for r in found))
text = render_summary(store.get(meeting.id), good, saved)
check("the rendered summary lists decisions, owners, due dates and what was saved",
      "Decisions:\n- Launch moves to 14 August." in text and "Revise the campaign plan (Sita), due 2026-09-19" in text
      and "You promised:" in text and "Saved 4 items to memory." in text, text)

# --- the session ---------------------------------------------------------------------------

print("\n[transcribing as it goes]")
memory, store, records = fresh()
meeting = store.create("Standup")
statuses, seen = [], []


def transcribe(wav):
    seen.append(wav)
    return [Segment(0.0, 2.0, "Backend needs another week.")]


session = MeetingSession(store, meeting.id, transcribe, statuses.append)
session.submit(chunk([QUIET] * 8, [QUIET] * 8, offset=0))
session.submit(chunk([0.03] * 8, [LOUD] * 8, offset=120))
session.finish(timeout=5)
check("a silent chunk is never sent to Whisper", len(seen) == 1 and session.chunks_skipped == 1)
check("a spoken chunk is stored with its time and speaker",
      store.transcript_lines(meeting.id) == ["[02:00] Call: Backend needs another week."], str(store.transcript_lines(meeting.id)))

meetings_module.MeetingSession.MAX_RETRY_WAIT_S = 0.05
attempts = []


def limited_once(wav):
    attempts.append(1)
    if len(attempts) == 1:
        raise RateLimited("audio seconds per hour", retry_after=30)
    return [Segment(0.0, 1.0, "Second try worked.")]


meeting2 = store.create("Retry")
session = MeetingSession(store, meeting2.id, limited_once, statuses.append)
session.submit(chunk([LOUD] * 4, [QUIET] * 4))
session.finish(timeout=5)
check("a rate-limited chunk waits and is retried, not lost",
      len(attempts) == 2 and store.transcript_lines(meeting2.id) == ["[00:00] Room: Second try worked."])
check("...and the wait is reported", any("waiting" in s and "transcription quota" in s for s in statuses), str(statuses))


def broken(wav):
    raise ValueError("bad audio")


meeting3 = store.create("Broken")
session = MeetingSession(store, meeting3.id, broken, statuses.append)
session.submit(chunk([LOUD] * 4, [QUIET] * 4, offset=240))
session.submit(chunk([LOUD] * 4, [QUIET] * 4, offset=360))
session.finish(timeout=5)
check("a chunk that fails is recorded and the rest carry on", len(session.failures) == 2
      and session.failures[0].startswith("04:00") and session.chunks_done == 2)

# --- start, pause, stop -------------------------------------------------------------------

print("\n[start, pause, stop]")


class FakeRecorder:
    def __init__(self, on_chunk, chunks, fail=False):
        self.on_chunk, self.chunks, self.fail = on_chunk, chunks, fail
        self.events = []

    def start(self):
        if self.fail:
            raise OSError("no microphone")
        self.events.append("start")

    def pause(self):
        self.events.append("pause")

    def resume(self):
        self.events.append("resume")

    def stop(self):
        self.events.append("stop")
        for c_ in self.chunks:  # the last partial chunk is flushed on stop
            self.on_chunk(c_)


def build(chunks, transcriber, chat, fail=False):
    memory, store, records = fresh()
    made, done, status = [], [], []

    def factory(on_chunk):
        rec = FakeRecorder(on_chunk, chunks, fail)
        made.append(rec)
        return rec

    notes = MeetingNotes(store, records, factory, transcriber, chat, status.append,
                         lambda mid, text: done.append((mid, text)))
    return notes, store, records, made, done, status


summary_json = json.dumps({"title": "Launch review", "summary": "The launch moves a week.",
                           "decisions": ["Launch moves to 14 August."],
                           "action_items": [{"text": "Revise the campaign plan", "owner": "Sita", "due": "2026-09-19"}],
                           "commitments": []})
notes, store, records, made, done, status = build(
    [chunk([0.03] * 8, [LOUD] * 8)], lambda wav: [Segment(0, 2, "The launch moves to the fourteenth.")],
    lambda messages, system: summary_json)
meeting = notes.start("Weekly sync")
check("start begins recording a new meeting", notes.recording and made[0].events == ["start"]
      and store.get(meeting.id).status == "recording")
check("starting twice is refused", raises(lambda: notes.start("Again"), RuntimeError))
notes.pause()
check("pause pauses the recorder and the meeting", made[0].events[-1] == "pause" and store.get(meeting.id).status == "paused")
notes.resume()
check("resume carries on", made[0].events[-1] == "resume" and store.get(meeting.id).status == "recording")
stopped = notes.stop(wait=True)
check("stop flushes the recorder and frees it for the next meeting", made[0].events[-1] == "stop" and not notes.recording)
final = store.get(meeting.id)
check("when it is done the meeting is summarised and titled", final.status == "done" and final.title == "Launch review"
      and "Launch moves to 14 August." in final.summary, final.summary)
check("the summary is handed to the app", done and done[0][0] == meeting.id and "Saved 2 items to memory." in done[0][1],
      str(done))
check("what was decided is now in memory", records.find(kind="decision")[0].text == "Launch moves to 14 August.")
check("progress was reported along the way", "recording" in status and "writing the meeting summary" in status,
      str(status))
check("stopping when nothing is recording is refused", raises(lambda: notes.stop(), RuntimeError))

notes, store, records, made, done, status = build([chunk([QUIET] * 8, [QUIET] * 8)], lambda wav: [],
                                                  lambda m, s: summary_json)
meeting = notes.start()
notes.stop(wait=True)
check("a meeting where nothing was said says so, with no invented summary",
      "nothing was said" in done[0][1] and store.get(meeting.id).summary.startswith("Nothing was said"))


def quota_gone(messages, system):
    raise AllProvidersFailed({"groq": RateLimited("quota")})


notes, store, records, made, done, status = build([chunk([LOUD] * 8, [QUIET] * 8)],
                                                  lambda wav: [Segment(0, 1, "Let's begin.")], quota_gone)
meeting = notes.start()
notes.stop(wait=True)
check("if the summary cannot be written, the transcript is still kept and the user told",
      store.transcript_lines(meeting.id) == ["[00:00] Room: Let's begin."] and "transcript is saved" in done[0][1]
      and store.get(meeting.id).status == "done", str(done))

notes, store, records, made, done, status = build([], lambda wav: [], lambda m, s: "{}", fail=True)
check("a recorder that cannot start is reported, and nothing is left recording",
      raises(lambda: notes.start("No mic"), RuntimeError) and not notes.recording
      and store.latest().status == "failed" and "no microphone" in store.latest().error)

# --- tools ------------------------------------------------------------------------------------

print("\n[meeting tools]")
notes, store, records, made, done, status = build(
    [chunk([0.03] * 8, [LOUD] * 8)], lambda wav: [Segment(0, 2, "The vendor contract waits until October.")],
    lambda messages, system: summary_json)
reg = ToolRegistry()
register(reg, notes)
check("starting and stopping run at once, as Harsh chose; deleting asks first",
      reg.tools["start_meeting_notes"].tier is Tier.AUTO and reg.tools["stop_meeting_notes"].tier is Tier.AUTO
      and reg.tools["delete_meeting"].tier is Tier.CONFIRM)
started_text = reg.execute(ToolCall("s", "start_meeting_notes", {"title": "Vendor call"}))
check("start says audio is not kept and asks to remind the others",
      "Recording started: Vendor call" in started_text and "never kept" in started_text and "tell everyone" in started_text)
check("a second start is an error", reg.execute(ToolCall("s", "start_meeting_notes", {})).startswith("Error: already recording"))
check("deleting a meeting still recording is refused",
      reg.execute(ToolCall("d", "delete_meeting", {"meeting_id": store.latest().id}), confirm=lambda t, a: True)
      .startswith("Error: stop the recording"))
stopped_text = reg.execute(ToolCall("t", "stop_meeting_notes", {}))
check("stop says the summary will follow", "summary will appear" in stopped_text, stopped_text)
deadline = time.time() + 5
while time.time() < deadline and store.latest().status != "done":
    time.sleep(0.05)
check("get_meeting with no number gives the latest summary",
      "Launch moves to 14 August." in reg.execute(ToolCall("g", "get_meeting", {})))
found = reg.execute(ToolCall("q", "search_meetings", {"query": "vendor contract"}))
check("search_meetings names the meeting, day, time and speaker",
      "Launch review" in found and "at 00:00, Call: The vendor contract waits until October." in found, found)
check("finding nothing says there is no record", "no record" in reg.execute(ToolCall("q", "search_meetings", {"query": "parking"})))
check("list_meetings lists them", "Launch review" in reg.execute(ToolCall("l", "list_meetings", {})))
check("stopping with nothing recording is an error", reg.execute(ToolCall("t", "stop_meeting_notes", {})).startswith("Error"))
check("deleting asks, then removes the meeting", reg.execute(
    ToolCall("d", "delete_meeting", {"meeting_id": store.latest().id}), confirm=lambda t, a: True).startswith("Deleted")
      and store.latest() is None)
check("get_meeting with nothing recorded says so", "no meetings have been recorded" in reg.execute(ToolCall("g", "get_meeting", {})))

lazy = ToolRegistry()
register(lazy, notes)
lazy.enable_lazy_loading(base={"core"})
catalog = next(sp for sp in lazy.specs() if sp.name == "load_tools").description
check("meeting tools load on demand, with a catalog line naming recording and past meetings",
      "meetings:" in catalog and "record" in catalog and "past meetings" in catalog, catalog)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
for f in FAIL:
    print("  FAILED:", f)
sys.exit(1 if FAIL else 0)
