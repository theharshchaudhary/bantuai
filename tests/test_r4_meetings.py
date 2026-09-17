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
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
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
check("a segment with no words at all is dropped however loud (seen live: a lone '.')",
      is_phantom(".", c, 0.0, 2.0) and is_phantom(" ... ", c, 0.0, 2.0))
check("real words over silence are kept (only stock lines and fragments are dropped)",
      not is_phantom("The launch moves to the fourteenth", c, 4.0, 5.0))
check("a one- or two-word fragment over silence is dropped (seen live: a lone 'We.')",
      is_phantom("We.", c, 4.0, 5.0) and is_phantom("so yeah", c, 4.0, 5.0))
check("...but a short reply over real speech is kept", not is_phantom("Yes, agreed.", c, 2.0, 4.0))
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


def transcribe(wav, prompt=""):
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


def limited_once(wav, prompt=""):
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


def broken(wav, prompt=""):
    raise ValueError("bad audio")


meeting3 = store.create("Broken")
session = MeetingSession(store, meeting3.id, broken, statuses.append)
session.submit(chunk([LOUD] * 4, [QUIET] * 4, offset=240))
session.submit(chunk([LOUD] * 4, [QUIET] * 4, offset=360))
session.finish(timeout=5)
check("a chunk that fails is recorded and the rest carry on", len(session.failures) == 2
      and session.failures[0].startswith("04:00") and session.chunks_done == 2)

print("\n[context for Whisper]")
prompts = []


def remembers(wav, prompt=""):
    prompts.append(prompt)
    return [Segment(0.0, 2.0, f"Part {len(prompts)}: the launch moves to August fourteenth.")]


meeting4 = store.create("Launch sync")
session = MeetingSession(store, meeting4.id, remembers, statuses.append, "Launch sync. Names: Ram, Sita.")
session.submit(chunk([LOUD] * 4, [QUIET] * 4))
session.submit(chunk([LOUD] * 4, [QUIET] * 4, offset=120))
session.finish(timeout=5)
check("the first chunk is told the names to spell", prompts[0] == "Launch sync. Names: Ram, Sita.", str(prompts))
check("later chunks also get the end of what was just said, for continuity across the cut",
      prompts[1].startswith("Launch sync. Names: Ram, Sita.") and "Part 1: the launch moves" in prompts[1], str(prompts))
long_session = MeetingSession(store, meeting4.id, remembers, statuses.append, "Names: " + "Someone, " * 200)
long_session._previous = "x" * 5000
check("the prompt stays within Whisper's limit", len(long_session.prompt()) <= meetings_module.PROMPT_CHARS + 1,
      str(len(long_session.prompt())))
long_session.finish(timeout=2)
empty_session = MeetingSession(store, meeting4.id, remembers, statuses.append)
check("with no names and nothing said yet, there is no prompt", empty_session.prompt() == "")
empty_session.finish(timeout=2)

names_store_memory, names_store, names_records = fresh()
names_records.add("action_item", "Send the plan", "Sita")
names_records.add("commitment", "Call the landlord", "Ram, sita")
from core.meetings import MeetingNotes as _MN

glossary_notes = _MN(names_store, names_records, lambda cb: None, lambda w, p="": [], lambda m, s: "{}",
                     known_names=lambda: ["Harsh"] + names_records.people())
check("the glossary lists the user and people on record, once each, any case",
      glossary_notes.glossary("Vendor call") == "Vendor call. Names: Harsh, Ram, Sita.", glossary_notes.glossary("Vendor call"))
broken_names = _MN(names_store, names_records, lambda cb: None, lambda w, p="": [], lambda m, s: "{}",
                   known_names=lambda: 1 / 0)
check("a failing name lookup never stops a recording starting", broken_names.glossary("x") == "x.")


print("\n[cutting chunks at a pause]")
import wave as _wave
import io as _io

from platform_desktop.recorder import FRAME_S, RATE, split_point, to_wav

# Found live: fixed 5-second cuts split "August fourteenth", Whisper heard "August 5",
# and the summary then recorded the wrong launch date.
speech = [0.05] * 40
speech[34] = 0.001  # a breath six frames from the end
check("a chunk ends at the quietest moment in its last stretch", split_point(speech, search_frames=8) == 35)
check("a pause outside the last stretch is not used", split_point([0.001] + [0.05] * 39, search_frames=8) >= 32)
check("with no pause at all it still cuts, at the end of the stretch", split_point([0.05] * 40, 8) in range(33, 41))
check("the latest of equally quiet moments is kept, so chunks stay long",
      split_point([0.05] * 30 + [0.001, 0.05, 0.001, 0.05], 6) == 33)
check("an empty chunk keeps nothing", split_point([], 5) == 0)

pcm = to_wav([0.0] * RATE + [0.5] * RATE)
with _wave.open(_io.BytesIO(pcm)) as w:
    check("chunks are 16kHz mono 16-bit WAV, as Whisper wants", (w.getframerate(), w.getnchannels(), w.getsampwidth(),
                                                                 w.getnframes()) == (16000, 1, 2, 2 * RATE))
check("loud samples are clipped rather than wrapping around", to_wav([2.0])[-2:] == (32767).to_bytes(2, "little", signed=True))
check("two minutes of chunk stay far under Whisper's 25MB limit", 120 * RATE * 2 / 1e6 < 5)


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
    [chunk([0.03] * 8, [LOUD] * 8)], lambda wav, prompt="": [Segment(0, 2, "The launch moves to the fourteenth.")],
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

notes, store, records, made, done, status = build([chunk([QUIET] * 8, [QUIET] * 8)], lambda wav, prompt="": [],
                                                  lambda m, s: summary_json)
meeting = notes.start()
notes.stop(wait=True)
check("a meeting where nothing was said says so, with no invented summary",
      "nothing was said" in done[0][1] and store.get(meeting.id).summary.startswith("Nothing was said"))


def quota_gone(messages, system):
    raise AllProvidersFailed({"groq": RateLimited("quota")})


notes, store, records, made, done, status = build([chunk([LOUD] * 8, [QUIET] * 8)],
                                                  lambda wav, prompt="": [Segment(0, 1, "Let's begin.")], quota_gone)
meeting = notes.start()
notes.stop(wait=True)
check("if the summary cannot be written, the transcript is still kept and the user told",
      store.transcript_lines(meeting.id) == ["[00:00] Room: Let's begin."] and "transcript is saved" in done[0][1]
      and store.get(meeting.id).status == "done", str(done))

notes, store, records, made, done, status = build([], lambda wav, prompt="": [], lambda m, s: "{}", fail=True)
check("a recorder that cannot start is reported, and nothing is left recording",
      raises(lambda: notes.start("No mic"), RuntimeError) and not notes.recording
      and store.latest().status == "failed" and "no microphone" in store.latest().error)

# --- tools ------------------------------------------------------------------------------------

print("\n[meeting tools]")
notes, store, records, made, done, status = build(
    [chunk([0.03] * 8, [LOUD] * 8)], lambda wav, prompt="": [Segment(0, 2, "The vendor contract waits until October.")],
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

# --- quitting mid-meeting, and finishing later ---------------------------------------------

print("\n[interrupted meetings]")
memory, store, records = fresh()
left = [store.create("Left recording"), store.create("Left paused"), store.create("Left summarising")]
store.update(left[1].id, status="paused")
store.update(left[2].id, status="processing")
store.add_segments(left[0].id, [Segment(10, 42, "We agreed on the budget.", "room")])
finished = store.create("Finished")
store.update(finished.id, status="done", ended_at=finished.started_at + 60, summary="Fine.")
check("recovery marks meetings cut off by a quit or crash as interrupted", store.recover() == 3
      and all(store.get(m.id).status == "interrupted" for m in left) and store.get(finished.id).status == "done")
check("...ending them where the transcript ends", store.get(left[0].id).ended_at == left[0].started_at + 42)
check("recovering twice changes nothing", store.recover() == 0)

notes, store, records, made, done, status = build(
    [chunk([LOUD] * 8, [QUIET] * 8)], lambda wav, prompt="": [Segment(0, 2, "We agreed to hire two engineers.")],
    lambda messages, system: json.dumps({"title": "Hiring", "decisions": ["Hire two engineers."]}))
meeting = notes.start("Hiring sync")
abandoned = notes.abandon()
check("quitting mid-meeting stops the recorder at once and keeps the meeting as interrupted",
      made[0].events[-1] == "stop" and abandoned.status == "interrupted" and not notes.recording and done == [])
check("abandoning with nothing recording is harmless", notes.abandon() is None)
reg = ToolRegistry()
register(reg, notes)
store.add_segments(meeting.id, [Segment(0, 2, "We agreed to hire two engineers.", "room")])
check("an interrupted meeting says a summary can still be written",
      "summarize_meeting can write one" in reg.execute(ToolCall("g", "get_meeting", {})))
written = reg.execute(ToolCall("s", "summarize_meeting", {}))
check("summarize_meeting writes it later and saves what was decided",
      "Hire two engineers." in written and store.get(meeting.id).status == "done"
      and records.find(kind="decision")[0].text == "Hire two engineers.", written)
check("asking again returns the summary rather than writing a second one",
      reg.execute(ToolCall("s", "summarize_meeting", {})).startswith(f"{store.get(meeting.id).describe()} already has"))

# --- the HUD -------------------------------------------------------------------------------------

print("\n[recording from the HUD]")
from PyQt5.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])
from core.agent import Agent
from core.providers.base import LLMProvider, LLMResponse
from core.providers.router import ProviderRouter
from ui.app import BantuApp
from ui.widgets import Bubble, Orb

_HUDS = []  # kept alive: destroying a HUD mid-run crashed natively (see test_phase7)


def pump_until(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.02)
    return False


class Quiet(LLMProvider):
    name = "quiet"

    def available_models(self):
        return ["q"]

    def resolve_model(self, preferences):
        return "q"

    def supports_vision(self):
        return False

    def chat(self, messages, tools=None, system=None, **kw):
        return LLMResponse(text="ok", provider=self.name, model="q")


class HudSettings:
    username, assistant_name, voice_enabled = "Harsh", "Bantu", False
    max_tool_turns, max_output_tokens, temperature = 4, 256, 0.5


def texts(hud):
    return [w.label.text() for w in hud.panel.items() if isinstance(w, Bubble)]


notes, store, records, made, done, status = build(
    [chunk([0.03] * 8, [LOUD] * 8)], lambda wav, prompt="": [Segment(0, 2, "The launch moves to August fourteenth.")],
    lambda messages, system: summary_json)
agent = Agent(ProviderRouter([Quiet()]), ToolRegistry(), Memory(Path(tempfile.mkdtemp()) / "hud.db"), HudSettings())
hud = BantuApp(agent, HudSettings(), install_hotkey=False, show_tray=False, meeting_notes=notes)
_HUDS.append(hud)
notes.on_status = hud.meeting_status.emit
notes.on_done = hud.meeting_done.emit
check("the record button shows when meeting notes are available", not hud.panel.record.isHidden())
hud.toggle_recording()
check("pressing record starts a meeting", pump_until(lambda: notes.recording and hud.orb.recording))
check("...the panel reminds you to tell the others and that audio is not kept",
      pump_until(lambda: any("Let everyone know" in t and "never kept" in t for t in texts(hud))), str(texts(hud)))
check("...and the header counts the recording", pump_until(lambda: hud.panel.recording_label.text().startswith("● REC")
                                                             and hud.panel.recording_label.isVisibleTo(hud.panel)),
      hud.panel.recording_label.text())
check("...and the button now stops", hud.panel.record.toolTip() == "Stop meeting notes")
notes.pause()
hud._sync_recording()
check("a paused meeting says PAUSED", hud.panel.recording_label.text() == "PAUSED")
notes.resume()
hud.toggle_recording()
check("pressing again stops it and the red dot goes", pump_until(lambda: not notes.recording and not hud.orb.recording))
check("the summary arrives in the panel when it is written",
      pump_until(lambda: any("Launch moves to 14 August." in t for t in texts(hud)), 8), str(texts(hud)[-2:]))
check("progress was shown while it was written",
      "meeting notes:" in hud.panel.status.text() or any("Launch" in t for t in texts(hud)))

notes.start("Started by voice")
check("a meeting started by a spoken request also shows the red dot and timer",
      pump_until(lambda: hud.orb.recording and hud.panel.recording_label.text().startswith("● REC"), 3))
t0 = time.time()
hud.shutdown()
check("quitting mid-meeting does not hang, and keeps the meeting as interrupted",
      time.time() - t0 < 3 and store.latest().status == "interrupted" and not notes.recording, f"{time.time() - t0:.1f}s")

failing, *_ = build([], lambda wav, prompt="": [], lambda m, s_: "{}", fail=True)
hud2 = BantuApp(Agent(ProviderRouter([Quiet()]), ToolRegistry(), Memory(Path(tempfile.mkdtemp()) / "h2.db"), HudSettings()),
                HudSettings(), install_hotkey=False, show_tray=False, meeting_notes=failing)
_HUDS.append(hud2)
hud2.toggle_recording()
check("a recording that cannot start says why in the panel",
      pump_until(lambda: any("could not start" in t and "no microphone" in t for t in texts(hud2))), str(texts(hud2)))
hud2.shutdown()

hud3 = BantuApp(Agent(ProviderRouter([Quiet()]), ToolRegistry(), Memory(Path(tempfile.mkdtemp()) / "h3.db"), HudSettings()),
                HudSettings(), install_hotkey=False, show_tray=False)
_HUDS.append(hud3)
check("without meeting notes there is no record button", hud3.panel.record.isHidden())
hud3.shutdown()

orb = Orb()
orb.set_recording(True)
check("the orb paints its recording dot without trouble", not orb.grab().isNull() and orb.recording)
_HUDS.append(orb)

print("\n[Meetings in Settings]")
from core import config as cfg
from ui.settings_dialog import SettingsDialog
from ui.setup_parts import Services

services = Services(check_key=lambda p, k: None, store_key=lambda p, k: None,
                    existing_key=lambda p: ("gsk_x", "keyring") if p != "calendar" else (None, None),
                    list_devices=lambda: [], preview_voice=lambda s, g, t: None, open_url=lambda u: None)
memory, store, records = fresh()
first = store.create("Vendor call")
store.add_segments(first.id, [Segment(3, 8, "The contract waits until October.", "call")])
store.update(first.id, status="done", ended_at=first.started_at + 600, summary="Contract on hold.")
second = store.create("Hiring sync")
store.update(second.id, status="done", ended_at=second.started_at + 60)
settings = cfg.Settings(username="Harsh")
dlg = SettingsDialog(settings, services, meetings=store)
_HUDS.append(dlg)
tabs = [dlg.tabs.tabText(i) for i in range(dlg.tabs.count())]
check("Settings has a Meetings tab", "Meetings" in tabs, str(tabs))
check("it lists meetings newest first", dlg.meeting_list.count() == 2 and "Hiring sync" in dlg.meeting_list.item(0).text())
dlg.meeting_list.setCurrentRow(1)
viewer = dlg.open_meeting()
check("opening one shows its summary and transcript",
      viewer is not None and "Contract on hold." in dlg.meeting_text(store.get(first.id))
      and "[00:03] Call: The contract waits until October." in dlg.meeting_text(store.get(first.id)))
asked = []
dlg.confirm_delete = lambda what: asked.append(what) or False
dlg.delete_meeting()
check("deleting asks first, and saying no keeps it", asked == ["'Vendor call'"] and store.get(first.id) is not None)
dlg.confirm_delete = lambda what: True
dlg.delete_meeting()
check("saying yes deletes it and refreshes the list", store.get(first.id) is None and dlg.meeting_list.count() == 1)
check("transcripts are kept 90 days by default", dlg.retention.currentData() == 90)
dlg.retention.setCurrentIndex(dlg.retention.findData(0))
dlg.save()
check("choosing 'until I delete them' saves 0 days", settings.meeting_retention_days == 0)
empty_memory, empty_store, _ = fresh()
empty = SettingsDialog(cfg.Settings(), services, meetings=empty_store)
_HUDS.append(empty)
check("with no meetings it says so and Open/Delete are disabled",
      empty.meeting_list.item(0).text() == "No meetings recorded yet." and not empty.delete_meeting_button.isEnabled())
live_memory, live_store, _ = fresh()
live_meeting = live_store.create("In progress")
live = SettingsDialog(cfg.Settings(), services, meetings=live_store)
_HUDS.append(live)
live.confirm_delete = lambda what: True
live.delete_meeting()
check("a meeting still recording cannot be deleted from Settings",
      live_store.get(live_meeting.id) is not None and "Stop the recording" in live.error.text())

main_src = (ROOT / "main.py").read_text(encoding="utf-8")
check("the app hands meeting notes to the HUD and recovers interrupted meetings at start",
      "meeting_notes=_MEETING_NOTES" in main_src and "_MEETING_DONE.append(hud.meeting_done.emit)" in main_src
      and "meetings_store.recover()" in main_src)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
for f in FAIL:
    print("  FAILED:", f)
sys.exit(1 if FAIL else 0)
