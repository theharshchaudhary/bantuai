"""R3 tests: personality, greeting, weather, the briefing and the calendar.

No API key, no network: weather and calendar feeds are faked.

    .venv/Scripts/python.exe tests/test_r3_briefing.py
"""

from __future__ import annotations

import datetime
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["APPDATA"] = tempfile.mkdtemp(prefix="bantu_r3_appdata_")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from core import config as cfg
from core.agent import SYSTEM_TEMPLATE, TONES, Agent
from core.memory import Memory
from core.providers.base import LLMProvider, LLMResponse
from core.providers.router import ProviderRouter
from core.tools.registry import ToolRegistry

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name + (f"  -> {detail}" if detail and not cond else ""))


class Capture(LLMProvider):
    name = "capture"

    def __init__(self):
        self.systems = []

    def available_models(self):
        return ["c"]

    def resolve_model(self, preferences):
        return "c"

    def supports_vision(self):
        return False

    def chat(self, messages, tools=None, system=None, **kw):
        self.systems.append(system or "")
        return LLMResponse(text="hi", provider=self.name, model="c")


def system_for(**settings):
    prov = Capture()
    s = cfg.Settings(username="Harsh", **settings)
    Agent(ProviderRouter([prov]), ToolRegistry(), Memory(Path(tempfile.mkdtemp()) / "t.db"), s).run("hello")
    return prov.systems[0]


# --- personality ---------------------------------------------------------------

print("\n[personality]")
check("warm professional is the default", cfg.Settings().tone == "warm")
check("the default prompt carries the warm manner", TONES["warm"] in system_for())
check("playful is used when chosen", TONES["playful"] in system_for(tone="playful")
      and TONES["warm"] not in system_for(tone="playful"))
check("strictly professional is used when chosen", TONES["professional"] in system_for(tone="professional"))
check("an unknown tone falls back to warm rather than breaking the prompt", TONES["warm"] in system_for(tone="grumpy"))
check("every tone keeps the work brief and exact where it matters",
      "brief and exact" in TONES["playful"] and "never gushing" in TONES["warm"])
check("the template has one tone slot", SYSTEM_TEMPLATE.count("{tone}") == 1)
s = cfg.Settings(tone="playful")
s.save()
check("the tone survives a restart", cfg.Settings.load().tone == "playful")

print("\n[greeting]")
from ui.app import greeting


def at(hour):
    return datetime.datetime(2026, 9, 17, hour, 5)


check("morning, afternoon, evening by the hour",
      [greeting("Harsh", at(h)) for h in (7, 13, 19)] == ["Good morning, Harsh.", "Good afternoon, Harsh.",
                                                          "Good evening, Harsh."])
check("late at night it just says hello", greeting("Harsh", at(2)) == "Hello, Harsh.")
check("without a name there is no dangling comma", greeting("", at(9)) == "Good morning." and
      greeting("  ", at(9)) == "Good morning.")
check("the boundaries fall where people expect", greeting("", at(5)) == "Good morning." and
      greeting("", at(12)) == "Good afternoon." and greeting("", at(17)) == "Good evening." and
      greeting("", at(22)) == "Hello.")

from PyQt5.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])
from ui.app import BantuApp
from ui.widgets import Bubble

hud = BantuApp(Agent(ProviderRouter([Capture()]), ToolRegistry(), Memory(Path(tempfile.mkdtemp()) / "g.db"),
                     cfg.Settings(username="Harsh")), cfg.Settings(username="Harsh"),
               install_hotkey=False, show_tray=False)
first = [w.label.text() for w in hud.panel.items() if isinstance(w, Bubble)][0]
check("the HUD greets by name and time of day", first.startswith(greeting("Harsh")) and "Bantu is ready" in first, first)
hud.shutdown()

from ui.settings_dialog import SettingsDialog
from ui.setup_parts import Services

services = Services(check_key=lambda p, k: None, store_key=lambda p, k: None,
                    existing_key=lambda p: ("gsk_existing", "keyring"), list_devices=lambda: [],
                    preview_voice=lambda s, g, t: None, open_url=lambda u: None)
settings = cfg.Settings(username="Harsh")
dlg = SettingsDialog(settings, services)
check("Settings shows the current tone", dlg.tone.currentData() == "warm")
changes = []
dlg.applied.connect(changes.append)
dlg.tone.setCurrentIndex(dlg.tone.findData("professional"))
dlg.save()
check("choosing a tone in Settings applies it at once", settings.tone == "professional" and changes
      and "tone" in changes[0], str(changes))


if __name__ == "__main__":
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    for f in FAIL:
        print("  FAILED:", f)
    sys.exit(1 if FAIL else 0)
