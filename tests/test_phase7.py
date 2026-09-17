"""Phase 7 tests — the HUD, driven offscreen against a scripted agent.

No window appears. Qt runs on its offscreen platform and the tests push real
signals through the real worker thread, so the parts most likely to break —
the cross-thread confirmation handshake, state transitions, shutdown while a
prompt is pending — are exercised rather than assumed.

    .venv/Scripts/python.exe tests/test_phase7.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from PyQt5.QtWidgets import QApplication

from core import config as cfg
from core.agent import Agent
from core.memory import Memory
from core.providers.base import LLMProvider, LLMResponse, RateLimited, ToolCall
from core.providers.router import ProviderRouter
from core.tools.registry import Tier, ToolRegistry

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name + (f"  -> {detail}" if detail and not cond else ""))


app = QApplication.instance() or QApplication(sys.argv)

from ui.app import BantuApp, hotkey_label
from ui.widgets import ASSETS, Bubble, ConfirmBar, State, ToolChip


def pump_until(pred, timeout: float = 8.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        app.processEvents()
        if pred():
            return True
        time.sleep(0.01)
    app.processEvents()
    return pred()


class Scripted(LLMProvider):
    name = "scripted"

    def __init__(self, script):
        self.script = list(script)

    def available_models(self):
        return ["s"]

    def resolve_model(self, preferences):
        return "s"

    def supports_vision(self):
        return True

    def chat(self, messages, tools=None, system=None, **kw):
        item = self.script.pop(0) if self.script else LLMResponse(text="done")
        if isinstance(item, Exception):
            raise item
        item.provider = self.name
        return item


class S:
    username, assistant_name = "Harsh", "Bantu"
    max_tool_turns, max_output_tokens, temperature = 5, 512, 0.7
    voice_enabled = False
    hotkey = "ctrl+alt+space"

    def save(self):
        pass


def make(script):
    ran: list[str] = []
    reg = ToolRegistry()

    @reg.register(tier=Tier.AUTO, category="test")
    def peek(what: str) -> str:
        """Look something up.

        Args:
            what: Subject.
        """
        ran.append(f"peek:{what}")
        return f"result for {what}"

    @reg.register(tier=Tier.CONFIRM, category="test")
    def destroy(path: str) -> str:
        """Delete something.

        Args:
            path: Target.
        """
        ran.append(f"destroy:{path}")
        return "gone"

    agent = Agent(ProviderRouter([Scripted(script)]), reg,
                  Memory(Path(tempfile.mkdtemp()) / "h.db"), S())
    hud = BantuApp(agent, S(), install_hotkey=False, show_tray=False)
    # Kept alive to the end. Rebinding `hud` let Python destroy the previous HUD's
    # Qt objects mid-run in whatever order it liked, and about one run in two then
    # died natively (exit 127, no traceback) once stdout was redirected to a file.
    _HUDS.append(hud)
    return hud, ran


_HUDS: list = []


def kinds(hud):
    return [type(w).__name__ for w in hud.panel.items()]


# --- settings ---------------------------------------------------------------

print("\n[hotkey]")
check("the default hotkey is not Ctrl+Space, which VS Code and Windows IME already own",
      cfg.Settings().hotkey.lower() != "ctrl+space", cfg.Settings().hotkey)
check("hotkeys display readably", hotkey_label("ctrl+alt+space") == "Ctrl+Alt+Space")

# Both of these shadowed a virtual QObject method and hard-crashed the process.
from PyQt5.QtCore import QObject
from ui.app import BantuApp as _B, Worker as _W
# Only names Bantu declares itself: QObject's own signals (destroyed, ...) are inherited.
_mine = [n for cls in (_W, _B) for n in vars(cls) if hasattr(QObject, n) and not n.startswith('__')]
check("no Bantu signal or method shadows a QObject member", not _mine, str(_mine))
# Assignment is what shadowed it and crashed; calling the real self.thread() is fine.
import re as _re

check("BantuApp does not shadow QObject.thread by assigning to it",
      not _re.search(r"self\.thread\s*=(?!=)",
                     Path(__file__).resolve().parents[1].joinpath("ui", "app.py").read_text(encoding="utf-8")))


# --- construction -----------------------------------------------------------

print("\n[construction]")
hud, ran = make([LLMResponse(text="Hello Harsh.")])
check("the orb exists and is shown", hud.orb.isVisible())
check("the panel starts hidden", not hud.panel.isVisible())
check("the orb starts idle", hud.orb.state is State.IDLE)
check("a greeting is already in the transcript", kinds(hud) == ["Bubble"], str(kinds(hud)))
check("the greeting does not promise a hotkey that was never installed",
      "anywhere to speak" not in hud.panel.items()[0].label.text(),
      hud.panel.items()[0].label.text())
check("the agent worker runs on its own thread", hud.agent_thread.isRunning())
check("the worker is not on the UI thread", hud.worker.thread() is not app.thread())

if (ASSETS / "Jarvis.gif").exists():
    size = hud.orb._movie.scaledSize()
    check("the gif is scaled keeping its 16:9 aspect, not squashed square",
          abs(size.width() / size.height() - 16 / 9) < 0.02, f"{size.width()}x{size.height()}")
    pix = hud.orb.grab()
    check("the orb paints", not pix.isNull() and pix.width() > 0)


# --- a plain exchange -------------------------------------------------------

print("\n[plain exchange]")
hud.toggle_panel()
check("clicking the orb opens the panel", hud.panel.isVisible())
hud.ask("hi")
check("asking marks the app busy", hud.busy)
check("...disables the input while working", not hud.panel.entry.isEnabled())
check("...and sets the orb thinking", hud.orb.state is State.THINKING)

hud.ask("a second message while busy")
check("a second message while busy is ignored, not queued into chaos",
      sum(1 for w in hud.panel.items() if isinstance(w, Bubble) and w.role == "user") == 1)

check("the reply arrives", pump_until(lambda: not hud.busy), "timed out")
texts = [w.label.text() for w in hud.panel.items() if isinstance(w, Bubble)]
check("the reply is shown", "Hello Harsh." in texts, str(texts))
check("input is re-enabled", hud.panel.entry.isEnabled())
check("the orb returns to idle", pump_until(lambda: hud.orb.state is State.IDLE, 2))
hud.shutdown()


# --- tool calls -------------------------------------------------------------

print("\n[tool activity]")
hud, ran = make([
    LLMResponse(text="", tool_calls=[ToolCall("1", "peek", {"what": "disk"}),
                                     ToolCall("2", "peek", {"what": "ram"})]),
    LLMResponse(text="All good."),
])
hud.ask("check things")
pump_until(lambda: not hud.busy)
chips = [w for w in hud.panel.items() if isinstance(w, ToolChip)]
check("each tool call gets a chip", len(chips) == 2, str(kinds(hud)))
check("...both tools ran", ran == ["peek:disk", "peek:ram"], str(ran))
check("two calls to the same tool fill their own chips, not one twice",
      chips[0].result == "result for disk" and chips[1].result == "result for ram",
      f"{chips[0].result!r} {chips[1].result!r}")
chips[0].mousePressEvent(None)
check("a chip expands to show its output", not chips[0].body.isHidden())
hud.shutdown()


# --- confirmation across threads --------------------------------------------

print("\n[confirmation: reject]")
hud, ran = make([
    LLMResponse(text="", tool_calls=[ToolCall("1", "destroy", {"path": "C:/important"})]),
    LLMResponse(text="Left it alone."),
])
hud.ask("delete it")
check("a confirm-tier tool raises a prompt in the panel",
      pump_until(lambda: any(isinstance(w, ConfirmBar) for w in hud.panel.items())), str(kinds(hud)))
bar = next(w for w in hud.panel.items() if isinstance(w, ConfirmBar))
check("the prompt names the tool", bar.tool_name == "destroy")
check("the worker is genuinely paused, not racing ahead", ran == [] and hud.busy)
# focusWidget, not hasFocus: an offscreen window is never active, but Qt still
# records which widget would receive keys when it is.
pump_until(lambda: hud.panel.focusWidget() is bar.buttons["no"], 1)
check("Reject holds focus, so a stray Enter picks the safe choice",
      hud.panel.focusWidget() is bar.buttons["no"],
      getattr(hud.panel.focusWidget(), "text", lambda: "?")().encode("ascii", "replace"))
time.sleep(0.3)
app.processEvents()
check("...and still paused after waiting", ran == [])
bar.buttons["no"].click()
check("the run completes after rejecting", pump_until(lambda: not hud.busy))
check("a rejected tool never runs", ran == [], str(ran))
check("the chip records the refusal",
      any(isinstance(w, ToolChip) and w.result == "declined by you" for w in hud.panel.items()))
hud.shutdown()

print("\n[confirmation: approve]")
hud, ran = make([
    LLMResponse(text="", tool_calls=[ToolCall("1", "destroy", {"path": "C:/tmp/x"})]),
    LLMResponse(text="Deleted."),
])
hud.ask("delete x")
pump_until(lambda: any(isinstance(w, ConfirmBar) for w in hud.panel.items()))
next(w for w in hud.panel.items() if isinstance(w, ConfirmBar)).buttons["yes"].click()
check("approving lets it finish", pump_until(lambda: not hud.busy))
check("an approved tool runs exactly once", ran == ["destroy:C:/tmp/x"], str(ran))
hud.shutdown()

print("\n[confirmation: approve all]")
hud, ran = make([
    LLMResponse(text="", tool_calls=[ToolCall("1", "destroy", {"path": "a"})]),
    LLMResponse(text="", tool_calls=[ToolCall("2", "destroy", {"path": "b"})]),
    LLMResponse(text="Both gone."),
])
hud.ask("delete a then b")
pump_until(lambda: any(isinstance(w, ConfirmBar) for w in hud.panel.items()))
next(w for w in hud.panel.items() if isinstance(w, ConfirmBar)).buttons["all"].click()
check("approve-all finishes the task", pump_until(lambda: not hud.busy))
check("...running both calls", ran == ["destroy:a", "destroy:b"], str(ran))
check("...after asking only once",
      sum(1 for w in hud.panel.items() if isinstance(w, ConfirmBar)) == 1, str(kinds(hud)))
hud.shutdown()


# --- failure ----------------------------------------------------------------

print("\n[failure]")
hud, ran = make([RateLimited("quota exhausted")])
hud.ask("anything")
check("a provider failure still finishes the request", pump_until(lambda: not hud.busy))
check("the orb shows the error state", hud.orb.state is State.ERROR, str(hud.orb.state))
texts = [w.label.text() for w in hud.panel.items() if isinstance(w, Bubble)]
check("the failure is explained in words", any("quota" in t for t in texts), str(texts))
check("the input is usable again after a failure", hud.panel.entry.isEnabled())
hud.shutdown()


# --- shutdown with a prompt pending -----------------------------------------

print("\n[shutdown while waiting for approval]")
hud, ran = make([
    LLMResponse(text="", tool_calls=[ToolCall("1", "destroy", {"path": "z"})]),
    LLMResponse(text="stopped"),
])
hud.ask("delete z")
pump_until(lambda: any(isinstance(w, ConfirmBar) for w in hud.panel.items()))
t0 = time.time()
hud.shutdown()
check("quitting with a prompt open does not hang", time.time() - t0 < 3.5, f"{time.time() - t0:.1f}s")
check("...the worker thread actually stopped", not hud.agent_thread.isRunning())
check("...and closing counts as a refusal, never an approval", ran == [], str(ran))


# --- reminders reach the HUD ------------------------------------------------

print("\n[reminders]")
hud, _ = make([])
hud.announced.emit("Reminder", "stretch your legs")
check("a reminder appears in the transcript",
      pump_until(lambda: any(isinstance(w, Bubble) and "stretch your legs" in w.label.text()
                             for w in hud.panel.items())))
hud.shutdown()

# --- stepping aside for GUI control -----------------------------------------

print("\n[stepping aside for GUI control]")
import threading as _threading

from platform_desktop import gui as _gui

hud, _ = make([LLMResponse(text="done on screen")])
hud.toggle_panel()
check("the HUD registers a step-aside hook", hud._step_aside in _gui._before_action)

seen = {}


def _tool_thread():
    _gui._step_aside()  # what a GUI tool does before capturing the screen
    seen["hidden_when_hook_returned"] = not hud.panel.isVisible()


t = _threading.Thread(target=_tool_thread)
t.start()
check("the hook returns once the UI thread has acted",
      pump_until(lambda: not t.is_alive(), 3), "worker thread still blocked")
check("...and the panel was already hidden by then — no capture can race the hide",
      seen.get("hidden_when_hook_returned") is True, seen)

hud._step_aside()  # the same call on the UI thread must not deadlock
check("calling it on the UI thread does not deadlock", True)

hud.ask("click something")
check("the panel comes back with the result once the run finishes",
      pump_until(lambda: not hud.busy and hud.panel.isVisible(), 5), hud.panel.isVisible())
hud.shutdown()
check("shutting down unregisters the hook", hud._step_aside not in _gui._before_action)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
for f in FAIL:
    print("  FAILED:", f)
sys.exit(1 if FAIL else 0)
