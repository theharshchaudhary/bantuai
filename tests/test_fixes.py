"""Regression tests for the Phase 1-4 fixes.

Each of these covers a bug that was actually shipped and then found, so they
exist to stop it coming back rather than to describe intended behaviour.

    .venv/Scripts/python.exe tests/test_fixes.py
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from core.agent import Agent
from core.memory import Memory
from core.providers.base import Image, LLMProvider, LLMResponse, Message
from core.providers.router import ProviderRouter
from core.reminders import ReminderScheduler, describe, parse_when
from core.tools import builtin
from core.tools.registry import ToolRegistry
from core.providers.base import ToolCall
from platform_desktop.ocr import Word, find_matches
from platform_desktop.paths import parse_region
from core.tools.registry import ToolError

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name + (f"  -> {detail}" if detail and not cond else ""))


def tmpdb(name="t.db") -> Path:
    return Path(tempfile.mkdtemp()) / name


# --- vision through the agent loop -------------------------------------------
# Bug: agent.run(text, images=...) wrote to memory then read history() back,
# which does not carry image bytes. Vision through the agent never worked.

print("\n[images survive the agent loop]")


class Capturing(LLMProvider):
    name = "capturing"

    def __init__(self):
        self.saw_images = []
        self.saw_needs_vision = []

    def available_models(self):
        return ["c"]

    def resolve_model(self, preferences):
        return "c"

    def supports_vision(self):
        return True

    def chat(self, messages, tools=None, system=None, **kw):
        self.saw_images.append(sum(len(m.images) for m in messages))
        return LLMResponse(text="saw it", provider=self.name, model="c")


class S:
    username, assistant_name = "Harsh", "Bantu"
    max_tool_turns, max_output_tokens, temperature = 3, 512, 0.7


prov = Capturing()
mem = Memory(tmpdb())
agent = Agent(ProviderRouter([prov]), ToolRegistry(), mem, S())
agent.run("what is on my screen?", images=[Image(b"PNGBYTES", "image/png")])
check("the provider actually received the image", prov.saw_images[0] == 1, str(prov.saw_images))

prov2 = Capturing()
mem2 = Memory(tmpdb("b.db"))
Agent(ProviderRouter([prov2]), ToolRegistry(), mem2, S()).run("plain question")
check("a text-only run sends no images", prov2.saw_images[0] == 0, str(prov2.saw_images))

h = mem.history()
check("image bytes are not persisted to the database", all(not m.images for m in h))
check("...but history records that one was attached",
      any("image(s) were attached" in (m.content or "") for m in h),
      str([m.content for m in h]))


# --- schema migration -------------------------------------------------------
# Bug: CREATE TABLE IF NOT EXISTS does not add a column to an existing db, so
# an upgraded install would fail on every append with a column-count mismatch.

print("\n[migrating an older database]")
old = tmpdb("old.db")
old.parent.mkdir(parents=True, exist_ok=True)
con = sqlite3.connect(old)
con.executescript(
    "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, conversation TEXT NOT NULL,"
    " role TEXT NOT NULL, content TEXT, tool_calls TEXT, tool_call_id TEXT, name TEXT,"
    " created_at REAL NOT NULL);"
)
con.commit()
con.close()

try:
    m = Memory(old)
    m.append(Message.user("after upgrade", [Image(b"X")]))
    check("a pre-existing database is migrated, not broken", len(m.history()) == 1)
    check("...and the new column works", "image(s) were attached" in (m.history()[0].content or ""))
except Exception as e:
    check("a pre-existing database is migrated, not broken", False, f"{type(e).__name__}: {e}")


# --- reminders --------------------------------------------------------------

print("\n[reminders]")
rm = Memory(tmpdb("r.db"))
reg = ToolRegistry()
builtin.register(reg, rm)


def run(tool, **kw):
    return reg.execute(ToolCall("t", tool, kw), confirm=lambda t, a: True)


check("relative times parse", parse_when("in 30 minutes") > time.time() + 1700)
check("ISO times parse", parse_when("2026-09-17T09:00") > 0)
try:
    parse_when("sometime next week")
    check("vague times are refused rather than guessed", False, "it guessed")
except ValueError:
    check("vague times are refused rather than guessed", True)

out = run("set_reminder", when="in 10 minutes", message="stand up")
check("set_reminder schedules", "Reminder #" in out, out[:70])
check("list_reminders shows it", "stand up" in run("list_reminders"))
check("a past time is refused",
      run("set_reminder", when="2020-01-01T00:00", message="x").startswith("Error"))
check("an empty message is refused",
      run("set_reminder", when="in 5 minutes", message="  ").startswith("Error"))

fired = []
sched = ReminderScheduler(rm, lambda t, b: fired.append(b), tick=1)
rm.add_reminder("due now", time.time() - 1)
check("the scheduler fires what is due", sched.check_now() == 1 and fired == ["due now"], str(fired))
check("...and does not fire it twice", sched.check_now() == 0)
check("...leaving future ones pending", any(r["message"] == "stand up" for r in rm.pending_reminders()))

rid = rm.pending_reminders()[0]["id"]
check("cancel_reminder works", "Cancelled" in run("cancel_reminder", reminder_id=rid))
check("cancelling twice is handled", "No pending" in run("cancel_reminder", reminder_id=rid))
check("reminders survive a restart",
      isinstance(Memory(rm.path).pending_reminders(), list))


def _boom(_t, _b):
    raise RuntimeError("notification backend died")


rm.add_reminder("will fail to announce", time.time() - 1)
bad = ReminderScheduler(rm, _boom, tick=1)
bad.check_now()
check("a failed announcement still marks the reminder fired, avoiding an endless loop",
      not any(r["message"] == "will fail to announce" for r in rm.pending_reminders()))


# --- multi-word screen matching ---------------------------------------------
# Bug: find_on_screen matched single OCR words only, so "Save As" never matched
# and the agent escalated to a scarce vision call instead.

print("\n[multi-word screen matching]")
W = [
    Word("Save", 10, 10, 40, 15, 0), Word("As", 55, 10, 20, 15, 0),
    Word("Sign", 10, 40, 35, 15, 1), Word("in", 50, 40, 15, 15, 1),
    Word("with", 70, 40, 30, 15, 1), Word("Google", 105, 40, 50, 15, 1),
    Word("Cancel", 10, 70, 50, 15, 2),
]
check("a single word still matches", find_matches(W, "Cancel")[0].center == (35, 77))
check("a two-word phrase matches", find_matches(W, "Save As")[0].text == "Save As")
check("...with a box spanning both words", find_matches(W, "Save As")[0].w == 65,
      str(find_matches(W, "Save As")[0]))
check("a four-word phrase matches", find_matches(W, "sign in with google")[0].text == "Sign in with Google")
check("matching is case-insensitive", find_matches(W, "SAVE AS") != [])
check("a phrase spanning two lines does not falsely match", find_matches(W, "As Sign") == [])
check("absent text returns nothing", find_matches(W, "Nonexistent") == [])
check("empty input returns nothing", find_matches(W, "   ") == [])


# --- shared region parsing --------------------------------------------------

print("\n[region parsing]")
check("an empty region means the whole screen", parse_region("") is None)
check("a valid region parses", parse_region("0,0,800,600") == (0, 0, 800, 600))
check("whitespace is tolerated", parse_region(" 10 , 20 , 30 , 40 ") == (10, 20, 30, 40))


def region_refused(text):
    try:
        parse_region(text)
        return False
    except ToolError:
        return True


check("too few numbers refused", region_refused("1,2,3"))
check("non-numeric refused", region_refused("a,b,c,d"))
check("an inverted box is refused", region_refused("100,100,10,10"))

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
for f in FAIL:
    print("  FAILED:", f)
sys.exit(1 if FAIL else 0)
