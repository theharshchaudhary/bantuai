"""R2 tests: the activity log - what was approved, refused, blocked or failed.

No API key, no network, no real config (APPDATA points at a temp folder).

    .venv/Scripts/python.exe tests/test_r2_activity.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["APPDATA"] = tempfile.mkdtemp(prefix="bantu_activity_appdata_")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from core.activity import MAX_VALUE_CHARS, OUTCOMES, Activity, describe_entry
from core.agent import Agent
from core.memory import Memory
from core.providers.base import LLMProvider, LLMResponse, ToolCall
from core.providers.router import ProviderRouter
from core.tools.registry import Rejected, Tier, ToolError, ToolRegistry

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name + (f"  -> {detail}" if detail and not cond else ""))


def setup():
    memory = Memory(Path(tempfile.mkdtemp()) / "a.db")
    activity = Activity(memory.db)
    reg = ToolRegistry()
    reg.audit = lambda tool, args, outcome, result: activity.record(tool.name, tool.tier.value, outcome, args, result)

    @reg.register(tier=Tier.AUTO, category="t")
    def look(what: str) -> str:
        """Read something.

        Args:
            what: Subject.
        """
        if what == "secret":
            raise ToolError(".env holds credentials and is blocked")
        if what == "crash":
            raise RuntimeError("bug")
        return f"saw {what}"

    @reg.register(tier=Tier.CONFIRM, category="t")
    def delete_file(path: str) -> str:
        """Delete a file.

        Args:
            path: Target.
        """
        return f"deleted {path}"

    @reg.register(tier=Tier.BLOCKED, category="t")
    def format_disk(drive: str) -> str:
        """Never.

        Args:
            drive: Drive.
        """
        return "formatted"

    return memory, activity, reg


def outcomes(activity):
    return [(e["tool"], e["outcome"]) for e in reversed(activity.recent())]


def yes(t, a):
    return True


def no(t, a):
    return False


def reject(t, a):
    raise Rejected("clicked Reject")


# --- what gets logged ---------------------------------------------------------------

print("\n[what the registry logs]")
memory, activity, reg = setup()
reg.execute(ToolCall("1", "look", {"what": "battery"}))
check("a successful read-only lookup is not logged", activity.recent() == [])
reg.execute(ToolCall("2", "delete_file", {"path": "a.txt"}), confirm=yes)
reg.execute(ToolCall("3", "delete_file", {"path": "b.txt"}), confirm=no)
reg.execute(ToolCall("4", "delete_file", {"path": "c.txt"}), confirm=reject)
reg.execute(ToolCall("5", "delete_file", {"path": "d.txt"}))
reg.execute(ToolCall("6", "format_disk", {"drive": "C:"}), confirm=yes)
reg.execute(ToolCall("7", "look", {"what": "secret"}))
reg.execute(ToolCall("8", "look", {"what": "crash"}))
reg.execute(ToolCall("9", "delete_file", {}), confirm=yes)
check("approved, declined, rejected, unaskable, blocked and failed are each recorded", outcomes(activity) == [
    ("delete_file", "approved"), ("delete_file", "declined"), ("delete_file", "declined"),
    ("delete_file", "not asked"), ("format_disk", "blocked"), ("look", "failed"), ("look", "failed"),
    ("delete_file", "failed"),
], str(outcomes(activity)))
entries = {e["arguments"].get("path") or e["arguments"].get("what"): e for e in activity.recent()}
check("the arguments are kept", entries["b.txt"]["arguments"] == {"path": "b.txt"})
check("a safety refusal keeps its reason", ".env holds credentials" in entries["secret"]["detail"])
check("the tier is kept", entries["a.txt"]["tier"] == "confirm" and entries["secret"]["tier"] == "auto")
check("every outcome has a plain explanation", {o for _, o in outcomes(activity)} <= set(OUTCOMES))

reg.allow_for_task("delete_file")
reg.execute(ToolCall("10", "delete_file", {"path": "e.txt"}), confirm=no)
check("a call covered by an earlier 'approve all' is logged as allowed, not approved",
      outcomes(activity)[-1] == ("delete_file", "allowed"))

reg.execute(ToolCall("11", "no_such_tool", {}))
check("a call to a tool that does not exist is not logged", len(activity.recent()) == 9)

memory, activity, reg = setup()


def broken_audit(tool, args, outcome, result):
    raise RuntimeError("disk full")


reg.audit = broken_audit
out = reg.execute(ToolCall("1", "delete_file", {"path": "x"}), confirm=yes)
check("a broken log never blocks or changes the action", out == "deleted x")
check("results are unchanged by logging", setup()[2].execute(ToolCall("1", "look", {"what": "sky"})) == "saw sky")


# --- the store ----------------------------------------------------------------------

print("\n[the activity store]")
memory, activity, reg = setup()
long_text = "x" * 5000
activity.record("write_file", "confirm", "approved", {"path": "notes.txt", "content": long_text,
                                                       "nested": {"deep": long_text}, "n": 3})
entry = activity.recent()[0]
check("long values are cut short, so the log never holds a whole file",
      len(entry["arguments"]["content"]) == MAX_VALUE_CHARS and entry["arguments"]["content"].endswith("…"))
check("...nested values too", len(entry["arguments"]["nested"]["deep"]) == MAX_VALUE_CHARS)
check("...and other values are kept as they were", entry["arguments"]["n"] == 3 and entry["arguments"]["path"] == "notes.txt")
activity.record("close_app", "confirm", "declined", {"name": "chrome"}, "The user said no")
check("newest entries come first", [e["tool"] for e in activity.recent()] == ["close_app", "write_file"])
check("the limit is honoured", len(activity.recent(1)) == 1)
line = describe_entry(activity.recent()[0])
check("an entry reads as one line: when, outcome, tool, arguments",
      " · declined · close_app · name=chrome" in line, line)

memory.db.execute("UPDATE activity SET at = at - ? WHERE tool = 'write_file'", (91 * 86400,))
memory.db.commit()
check("pruning removes entries older than 90 days", activity.prune() == 1)
check("...and keeps recent ones", [e["tool"] for e in activity.recent()] == ["close_app"])


# --- through the agent ----------------------------------------------------------------

print("\n[through the agent]")


class Script(LLMProvider):
    name = "script"

    def __init__(self, replies):
        self.replies = list(replies)

    def available_models(self):
        return ["s"]

    def resolve_model(self, preferences):
        return "s"

    def supports_vision(self):
        return False

    def chat(self, messages, tools=None, system=None, **kw):
        r = self.replies.pop(0) if self.replies else LLMResponse(text="done")
        r.provider, r.model = self.name, "s"
        return r


class S:
    username, assistant_name = "Harsh", "Bantu"
    max_tool_turns, max_output_tokens, temperature = 6, 512, 0.7


memory, activity, reg = setup()
agent = Agent(ProviderRouter([Script([
    LLMResponse(text="", tool_calls=[ToolCall("1", "delete_file", {"path": "notes.txt"})]),
    LLMResponse(text="I left it alone."),
])]), reg, memory, S(), confirm=no)
agent.run("delete notes.txt")
check("a no given in the conversation lands in the log", outcomes(activity) == [("delete_file", "declined")])


# --- Settings and wiring ----------------------------------------------------------------

print("\n[Settings and wiring]")
from PyQt5.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])
from core import config as cfg
from ui.settings_dialog import SettingsDialog
from ui.setup_parts import Services

services = Services(check_key=lambda p, k: None, store_key=lambda p, k: None,
                    existing_key=lambda p: (None, None), list_devices=lambda: [],
                    preview_voice=lambda s, g, t: None, open_url=lambda u: None)
memory, activity, reg = setup()
dlg = SettingsDialog(cfg.Settings(), services, activity=activity)
tabs = [dlg.tabs.tabText(i) for i in range(dlg.tabs.count())]
check("Settings has an Activity tab when there is a log", "Activity" in tabs, str(tabs))
check("an empty log says so", dlg.activity_list.count() == 1 and dlg.activity_list.item(0).text() == "Nothing yet.")
reg.execute(ToolCall("1", "delete_file", {"path": "report.docx"}), confirm=no)
dlg.refresh_activity()
item = dlg.activity_list.item(0)
check("refreshing shows new entries", dlg.activity_list.count() == 1 and "declined · delete_file · path=report.docx"
      in item.text(), item.text())
check("hovering explains the outcome", "the user said no" in item.toolTip(), item.toolTip())
check("Settings has no Activity tab without a log",
      "Activity" not in [SettingsDialog(cfg.Settings(), services).tabs.tabText(i) for i in range(3)])

main_src = (ROOT / "main.py").read_text(encoding="utf-8")
check("the app logs through the registry and prunes old entries at start",
      "_REGISTRY.audit = " in main_src and "_ACTIVITY.prune()" in main_src and "activity=_ACTIVITY" in main_src)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
for f in FAIL:
    print("  FAILED:", f)
sys.exit(1 if FAIL else 0)
