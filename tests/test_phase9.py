"""Phase 9 tests — first-run setup and Settings, offscreen with fake services.

No network, no real credential store, no audio: every side effect goes
through a fake `Services`, and settings are saved into a temporary folder.

    .venv/Scripts/python.exe tests/test_phase9.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["APPDATA"] = tempfile.mkdtemp(prefix="bantu_p9_")  # never the real config
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from PyQt5.QtWidgets import QApplication

app = QApplication.instance() or QApplication(sys.argv)

from core import config as cfg
from core.providers.validate import KeyCheck
from ui.onboarding import SetupWizard
from ui.settings_dialog import SettingsDialog
from ui.setup_parts import Services, validate_hotkey

PASS, FAIL = [], []
MAIN_THREAD = threading.current_thread()


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name + (f"  -> {detail}" if detail and not cond else ""))


def pump_until(pred, timeout=3.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        app.processEvents()
        if pred():
            return True
        time.sleep(0.01)
    app.processEvents()
    return pred()


class Fakes:
    def __init__(self, existing=None, devices=None):
        self.existing = existing or {}
        self.devices = devices or [(1, "Microphone (Iriun Webcam)"), (3, "Microphone Array (Realtek)")]
        self.checked: list[tuple[str, str]] = []
        self.check_threads: list[threading.Thread] = []
        self.stored: dict[str, str] = {}
        self.previews: list[tuple[str, str]] = []
        self.opened: list[str] = []

    def services(self) -> Services:
        def check_key(provider, key):
            self.checked.append((provider, key))
            self.check_threads.append(threading.current_thread())
            time.sleep(0.05)
            if key.startswith("good"):
                return KeyCheck(True, "Key works - 5 models available.")
            if key.startswith("offline"):
                return KeyCheck(False, "Couldn't reach Groq. Check your internet connection.", reachable=False)
            return KeyCheck(False, "Groq rejected that key. Check it was copied completely.")

        return Services(
            check_key=check_key,
            store_key=lambda p, k: self.stored.__setitem__(p, k),
            existing_key=lambda p: self.existing.get(p, (None, None)),
            list_devices=lambda: list(self.devices),
            preview_voice=lambda s, g, t: self.previews.append((g, t)),
            open_url=lambda url: self.opened.append(url),
        )


# --- hotkey validation ------------------------------------------------------

print("\n[hotkey rules]")
check("the default is accepted", validate_hotkey("ctrl+alt+space")[0])
check("case and spaces are normalised", validate_hotkey(" Ctrl + Alt + B ") == (True, "", "ctrl+alt+b"))
check("a bare key is refused - it would fire while typing", not validate_hotkey("a")[0])
check("shift is not enough on its own", not validate_hotkey("shift+a")[0], validate_hotkey("shift+a"))
check("modifiers without a key are refused", not validate_hotkey("ctrl+alt")[0])
check("ctrl+space is refused - VS Code and Windows own it", not validate_hotkey("ctrl+space")[0])
check("copy/paste shortcuts are refused", not validate_hotkey("ctrl+c")[0])
check("an unknown key is refused", not validate_hotkey("ctrl+banana")[0])
check("empty is refused", not validate_hotkey("  ")[0])


# --- when setup runs --------------------------------------------------------

print("\n[when setup runs]")
real_configured = cfg.configured_providers
try:
    cfg.configured_providers = lambda: ["groq"]
    check("a fresh install needs setup", cfg.needs_onboarding(cfg.Settings()))
    done = cfg.Settings(onboarded=True)
    check("a finished setup with a working key does not", not cfg.needs_onboarding(done))
    cfg.configured_providers = lambda: []
    check("a finished setup whose keys have gone needs it again", cfg.needs_onboarding(done))
finally:
    cfg.configured_providers = real_configured
check("the default name is no longer the developer's", cfg.Settings().username == "")
check("an unnamed user is referred to neutrally", cfg.Settings().display_name == "the user")


# --- wizard: welcome --------------------------------------------------------

print("\n[setup: welcome]")
fakes = Fakes()
settings = cfg.Settings()
wiz = SetupWizard(settings, fakes.services())
wiz.show()
pump_until(lambda: True, 0.1)
check("setup opens on the welcome page", wiz.stack.currentIndex() == 0)
check("there is no Back on the first page", not wiz.back.isVisible())
check("the step indicator marks where you are", "WELCOME" in wiz.progress.text(), wiz.progress.text())
wiz.name.setText("Asha")
wiz.next.click()
check("Next moves to the keys page", wiz.stack.currentIndex() == 1)


# --- wizard: keys -----------------------------------------------------------

print("\n[setup: keys]")
check("with no working key, Next is disabled", not wiz.next.isEnabled())
check("...and says why", "Test at least one key" in wiz.next.toolTip(), wiz.next.toolTip())

wiz.groq.edit.setText("bad-key")
wiz.groq.test_button.click()
pump_until(lambda: wiz.groq.test_button.isEnabled() and fakes.checked)
check("a rejected key shows the reason in plain words",
      "rejected that key" in wiz.groq.status.text(), wiz.groq.status.text())
check("...and still blocks Next", not wiz.next.isEnabled())

wiz.groq.edit.setText("offline-key")
wiz.groq.test()
pump_until(lambda: "internet" in wiz.groq.status.text())
check("being offline is distinguished from a bad key",
      "internet connection" in wiz.groq.status.text(), wiz.groq.status.text())

wiz.groq.edit.setText("good-groq")
wiz.groq.test()
check("a working key unblocks Next", pump_until(lambda: wiz.next.isEnabled()), wiz.groq.status.text())
check("...with a tick", wiz.groq.status.text().startswith("✓"), wiz.groq.status.text())
check("key checks run off the UI thread, so the window never freezes",
      fakes.check_threads and all(t is not MAIN_THREAD for t in fakes.check_threads))

wiz.groq.edit.setText("good-groq-edited")
check("editing a verified key un-verifies it", not wiz.groq.verified and not wiz.next.isEnabled())
wiz.groq.edit.setText("good-groq")
wiz.groq.test()
pump_until(lambda: wiz.next.isEnabled())

check("the key is masked by default", wiz.groq.edit.echoMode() == wiz.groq.edit.Password)
wiz.groq.reveal.click()
check("Show reveals it", wiz.groq.edit.echoMode() == wiz.groq.edit.Normal)
wiz.groq.reveal.click()

next(b for b in wiz.groq.findChildren(type(wiz.next)) if "free key" in b.text()).click()
check("'Get a free key' opens the provider's key page", fakes.opened == ["https://console.groq.com/keys"],
      fakes.opened)

wiz.next.click()
check("Next moves to the voice page", wiz.stack.currentIndex() == 2)


# --- wizard: voice ----------------------------------------------------------

print("\n[setup: voice]")
check("female is selected by default", wiz.voice.gender == "female")
wiz.voice.male.setChecked(True)
wiz.voice.play.click()
check("Play sample speaks with the chosen voice",
      pump_until(lambda: wiz.voice.play.isEnabled() and fakes.previews), fakes.previews)
check("...greeting the person by the name they gave", fakes.previews and "Asha" in fakes.previews[-1][1]
      and fakes.previews[-1][0] == "male", fakes.previews)
check("microphones are listed after the system default",
      [wiz.mic.itemText(i) for i in range(wiz.mic.count())][0] == "System default" and wiz.mic.count() == 3)
wiz.mic.setCurrentIndex(2)
check("choosing a microphone maps to its device number", wiz.mic.device == 3, wiz.mic.device)
wiz.speak_replies.setChecked(False)
wiz.next.click()


# --- wizard: ready and finish -----------------------------------------------

print("\n[setup: ready]")
check("the last page greets by name", "Asha" in wiz.ready_title.text(), wiz.ready_title.text())
check("it names what is connected", "Connected: Groq" in wiz.ready_body.text(), wiz.ready_body.text())
check("it explains what is missing without a Gemini key",
      "Without a Gemini key" in wiz.ready_body.text(), wiz.ready_body.text())
check("the button says what happens", wiz.next.text() == "Start Bantu")

wiz.next.click()
check("finishing closes setup as accepted", wiz.result() == wiz.Accepted)
check("only the verified key is stored", fakes.stored == {"groq": "good-groq"}, fakes.stored)
check("the name is saved", settings.username == "Asha")
check("the voice choice is saved", settings.voice_gender == "male")
check("spoken replies choice is saved", settings.voice_enabled is False)
check("the microphone is saved", settings.mic_device == 3)
check("setup is marked done", settings.onboarded is True)
saved = json.loads(cfg.Settings().path.read_text(encoding="utf-8"))
check("settings reach disk", saved["username"] == "Asha" and saved["onboarded"] is True, saved.get("username"))


# --- wizard: abandoning and existing keys -----------------------------------

print("\n[setup: abandoned]")
fakes2 = Fakes()
s2 = cfg.Settings()
wiz2 = SetupWizard(s2, fakes2.services())
wiz2.go(1)
wiz2.groq.edit.setText("good-x")
wiz2.groq.test()
pump_until(lambda: wiz2.groq.verified)
wiz2.reject()
check("closing setup stores no keys", fakes2.stored == {})
check("...and does not mark it done", s2.onboarded is False)

print("\n[setup: keys you already have]")
fakes3 = Fakes(existing={"groq": ("good-from-env", ".env"), "gemini": ("good-stored", "keyring")})
wiz3 = SetupWizard(cfg.Settings(), fakes3.services())
check("keys found in .env and in the credential store are both checked without a click",
      pump_until(lambda: wiz3.groq.verified and wiz3.gemini.verified),
      (wiz3.groq.status.text(), wiz3.gemini.status.text()))
check("a key from .env is labelled as moving into Credential Manager",
      "Checking it" in wiz3.groq.status.text() or wiz3.groq.status.text().startswith("✓"),
      wiz3.groq.status.text())
wiz3.go(1)
check("so the keys page is ready to continue at once", wiz3.next.isEnabled())
wiz3.go(3)
check("with both keys, the ready page does not warn about vision",
      "Without a Gemini key" not in wiz3.ready_body.text())
wiz3.finish()
check("finishing moves the .env key into the credential store",
      fakes3.stored.get("groq") == "good-from-env", fakes3.stored)

s4 = cfg.Settings()
fakes4 = Fakes()
wiz4 = SetupWizard(s4, fakes4.services())
wiz4.finish()
check("finish without a verified key refuses and returns to the keys page",
      wiz4.stack.currentIndex() == 1 and fakes4.stored == {} and not s4.onboarded)


# --- settings dialog --------------------------------------------------------

print("\n[settings]")
fakes5 = Fakes(existing={"groq": ("good-saved", "keyring")})
s5 = cfg.Settings(username="Asha", voice_gender="male", hotkey="ctrl+alt+space", onboarded=True)
dlg = SettingsDialog(s5, fakes5.services(), data_dir=os.environ["APPDATA"],
                     status=lambda: "Groq: ready · 3 request(s) this session")
emitted: list[set] = []
dlg.applied.connect(emitted.append)

check("current values are shown", dlg.name.text() == "Asha" and dlg.voice.gender == "male"
      and dlg.hotkey.text() == "ctrl+alt+space")
check("a saved key is not re-checked just by opening Settings", fakes5.checked == [], fakes5.checked)
def tab(dialog, name):
    """By name, not position: R3 added a "Your day" tab and moved the others along."""
    return next(i for i in range(dialog.tabs.count()) if dialog.tabs.tabText(i) == name)


check("the About tab shows where data lives", os.environ["APPDATA"] in
      " ".join(w.text() for w in dlg.tabs.widget(tab(dlg, "About")).findChildren(type(dlg.error))))
check("...and the connection status", "3 request(s)" in dlg.status_text.text())

dlg.hotkey.setText("a")
dlg.save()
check("an unsafe hotkey blocks saving with a reason", "Include Ctrl" in dlg.error.text() and not emitted,
      dlg.error.text())
check("...on the tab where it can be fixed", dlg.tabs.currentIndex() == 0)
dlg.hotkey.setText("ctrl+alt+")
check("the error clears as soon as the field is edited", dlg.error.text() == "", dlg.error.text())

dlg.hotkey.setText("Ctrl+Alt+B")
dlg.name.setText("Asha K")
dlg.save()
check("saving reports exactly what changed", emitted and emitted[-1] == {"hotkey", "username"}, emitted)
check("the hotkey is stored normalised", s5.hotkey == "ctrl+alt+b")
check("an unchanged saved key needed no test and was not re-stored", fakes5.stored == {}, fakes5.stored)

dlg2 = SettingsDialog(s5, fakes5.services())
out: list[set] = []
dlg2.applied.connect(out.append)
dlg2.gemini.edit.setText("good-new-gemini")
dlg2.save()
check("a changed key must be tested before saving", "Test the new Gemini key" in dlg2.error.text() and not out,
      dlg2.error.text())
check("...on the Keys tab", dlg2.tabs.currentIndex() == tab(dlg2, "Keys"))
dlg2.gemini.test()
pump_until(lambda: dlg2.gemini.verified)
dlg2.save()
check("once tested it is stored", fakes5.stored.get("gemini") == "good-new-gemini", fakes5.stored)
check("...and reported as a key change", out and "keys" in out[-1], out)

dlg3 = SettingsDialog(s5, fakes5.services())
dlg3.groq.edit.setText("")
dlg3.gemini.edit.setText("")
dlg3.save()
check("removing every key is refused", "at least one key" in dlg3.error.text(), dlg3.error.text())


# --- the running app --------------------------------------------------------

print("\n[the running app]")
import tempfile as _tf

from core.agent import Agent
from core.memory import Memory
from core.providers.base import LLMProvider, LLMResponse
from core.providers.router import ProviderRouter
from core.tools.registry import ToolRegistry
from ui.app import BantuApp


class P(LLMProvider):
    def __init__(self, name):
        self.name = name

    def available_models(self):
        return ["m"]

    def resolve_model(self, p):
        return "m"

    def chat(self, *a, **k):
        return LLMResponse(text="hi", provider=self.name)


router = ProviderRouter([P("groq")])
s6 = cfg.Settings(username="Asha", onboarded=True, voice_enabled=False)
agent = Agent(router, ToolRegistry(), Memory(Path(_tf.mkdtemp()) / "h.db"), s6)
hud = BantuApp(agent, s6, install_hotkey=False, show_tray=False,
               services=Fakes(existing={"groq": ("good", "keyring")}).services())

hud.panel.gear.click()
check("the panel's gear opens Settings", hud._settings_dialog is not None and hud._settings_dialog.isVisible())
hud.panel.gear.click()
check("clicking it again reuses the open window rather than stacking another",
      len([w for w in app.topLevelWidgets() if isinstance(w, SettingsDialog) and w.isVisible()]) == 1)
hud._settings_dialog.close()

s6.hotkey = "ctrl+alt+b"
hud.apply_settings({"hotkey"})
check("a new hotkey takes effect without a restart", hud.hotkey == "ctrl+alt+b")
check("...and the mic button's hint shows it", "Ctrl+Alt+B" in hud.panel.mic.toolTip(), hud.panel.mic.toolTip())

import core.providers.router as router_module

real_build = router_module.build_providers
same_router = agent.router
try:
    router_module.build_providers = lambda settings=None: [P("groq"), P("gemini")]
    hud.apply_settings({"keys"})
finally:
    router_module.build_providers = real_build
check("new keys reconnect in place, without a restart",
      [p.name for p in agent.router.providers] == ["groq", "gemini"], [p.name for p in agent.router.providers])
check("...on the same router object tools already hold", agent.router is same_router)
check("connection status lists every service", "Groq: ready" in hud.connection_status()
      and "Gemini: ready" in hud.connection_status(), hud.connection_status())

try:
    router.replace([])
    check("a router cannot be left with no providers", False)
except ValueError:
    check("a router cannot be left with no providers", True)
hud.shutdown()

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
for f in FAIL:
    print("  FAILED:", f)
sys.exit(1 if FAIL else 0)
