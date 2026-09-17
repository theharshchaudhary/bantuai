"""BantuAI — command line entry point.

A terminal front end while the HUD is built (Phase 07). It wires the portable
core to the Windows tool set and runs a conversation.

    .venv/Scripts/python.exe main.py                  floating orb (default)
    .venv/Scripts/python.exe main.py --cli            terminal REPL
    .venv/Scripts/python.exe main.py "read my screen"  one-shot
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

# The Windows console defaults to cp1252, which cannot encode Devanagari - so
# without this every Hindi and Nepali reply raises UnicodeEncodeError. Bantu is
# required to speak all three languages, so this is not optional.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Before anything touches the screen. At 125% display scaling a process that has
# not opted in gets screenshots in physical pixels but window rectangles and the
# cursor in scaled ones, so every click would land 25% off target.
if sys.platform == "win32":
    try:
        from platform_desktop.gui import ensure_dpi_awareness

        ensure_dpi_awareness()
    except Exception:
        pass

from core import config as cfg
from core.agent import Agent, Event
from core.memory import Memory
from core.reminders import ReminderScheduler
from core.providers.base import ProviderError
from core.providers.router import build_router
from core import briefing
from core import weather as weather_tools
from core import events as calendar_tools
from core import meetings as meeting_tools
from core.activity import Activity
from core.knowledge import Knowledge
from core.knowledge import register as register_knowledge
from core.records import Records
from core.tools import builtin, notes
from core.tools.registry import Tier, Tool, ToolRegistry

BANNER = """\
  ____              _         _    ___
 | __ )  __ _ _ __ | |_ _   _| |  / _ \\   {assistant} is online.
 |  _ \\ / _` | '_ \\| __| | | | | | |_| |  {tools} tools · {providers}
 |_.__/ \\__,_|_| |_|\\__|\\__,_|_|  \\___/   /help for commands
"""

DIM, BOLD, RESET = "\033[2m", "\033[1m", "\033[0m"
CYAN, YELLOW, RED = "\033[36m", "\033[33m", "\033[31m"


def make_printer(verbose: bool):
    def show(e: Event) -> None:
        if e.kind == "tool_start":
            args = ", ".join(f"{k}={v!r}" for k, v in list(e.arguments.items())[:3])
            print(f"{DIM}  → {e.tool}({args[:100]}){RESET}", flush=True)
        elif e.kind == "tool_end" and verbose:
            first = (e.result or "").strip().split("\n")[0][:120]
            print(f"{DIM}    {first}{RESET}", flush=True)
        elif e.kind == "declined":
            print(f"{YELLOW}    declined{RESET}", flush=True)
        elif e.kind == "waiting":
            print(f"{DIM}  … free limit reached, trying again in {e.seconds:.0f}s{RESET}", flush=True)
        elif e.kind == "error":
            print(f"{RED}  ! {e.text}{RESET}", flush=True)

    return show


def confirm(tool: Tool, args: dict) -> bool:
    """Ask before anything that changes state. Shows the exact arguments."""
    print(f"\n{YELLOW}  {tool.name} wants to run{RESET}")
    for k, v in args.items():
        print(f"    {k} = {v!r}")
    try:
        answer = input(f"  allow? [y]es / [n]o / [a]ll this task: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    if answer.startswith("a"):
        _REGISTRY.allow_for_task(tool.name)
        return True
    return answer.startswith("y")


_REGISTRY = ToolRegistry()
_SPEAKER = None
_LISTENER = None
#: Extra reminder announcers. The HUD registers one; the CLI needs none.
_ANNOUNCERS: list = []
_KNOWLEDGE: Knowledge | None = None
_ACTIVITY: Activity | None = None
_CALENDAR: calendar_tools.Calendar | None = None
_MEETING_NOTES: meeting_tools.MeetingNotes | None = None
#: Told about meeting progress ("recording", "writing the meeting summary") and the finished
#: summary. The HUD registers here; the CLI prints.
_MEETING_STATUS: list = []
_MEETING_DONE: list = []


def knowledge_folder(settings: cfg.Settings) -> Path:
    """Where the user drops documents to teach Bantu: visible, not hidden in AppData.

    Not under %APPDATA%\\BantuAI: the path guard blocks every tool from Bantu's own
    folder, so Bantu could not even copy a document in for the user.
    """
    if getattr(settings, "knowledge_dir", ""):
        return Path(settings.knowledge_dir).expanduser()
    if sys.platform == "win32":
        from platform_desktop.paths import documents_dir

        return documents_dir() / "Bantu Knowledge"
    return Path.home() / "Bantu Knowledge"


def build(settings: cfg.Settings | None = None) -> tuple[Agent, cfg.Settings]:
    settings = settings or cfg.Settings.load()
    router = build_router(settings)
    memory = Memory(cfg.user_data_dir() / "history.db")

    builtin.register(_REGISTRY, memory)
    records = Records(memory.db)
    notes.register(_REGISTRY, records, memory)

    # Every approval, refusal, block and failure is kept locally, in code, not by the model.
    global _ACTIVITY
    _ACTIVITY = Activity(memory.db)
    _ACTIVITY.prune()
    _REGISTRY.audit = lambda tool, args, outcome, result: _ACTIVITY.record(
        tool.name, tool.tier.value, outcome, args, result
    )

    global _KNOWLEDGE
    readers = {}
    try:
        from platform_desktop.files import extract_document_text

        readers = {
            ".pdf": lambda p: extract_document_text(p, max_pages=500),
            ".docx": extract_document_text,
            ".xlsx": extract_document_text,
        }
    except Exception as e:
        logging.getLogger("bantu").warning("document readers unavailable: %s", e)
    _KNOWLEDGE = Knowledge(memory.db, knowledge_folder(settings), readers)
    register_knowledge(_REGISTRY, _KNOWLEDGE)
    _KNOWLEDGE.start_polling()  # the first sync runs in the background, not at startup

    weather = weather_tools.Weather()
    weather_tools.register(_REGISTRY, weather, settings)

    def weather_line():
        # Read when the briefing runs, so a city set in Settings counts straight away.
        city = getattr(settings, "weather_city", "").strip()
        return (lambda: weather.forecast(city).describe()) if city else None

    global _CALENDAR
    _CALENDAR = calendar_tools.Calendar(memory.db)
    calendar_tools.register(_REGISTRY, _CALENDAR)
    # The secret iCal address grants read access to the calendar, so it lives with the keys.
    _CALENDAR.start_polling(lambda: cfg.get_key("calendar"))

    briefing.register(
        _REGISTRY, memory, records, weather_line,
        events_for=lambda: (lambda day: [e.short() for e in _CALENDAR.on_day(day)]),
    )
    # Core tools go with every request; everything else loads on demand. Sending
    # all of them fit only ~2 agent turns a minute into Groq's free token cap.
    _REGISTRY.enable_lazy_loading(base={"core"})

    # Windows-only tools are registered INTO the portable core, never imported by it.
    if sys.platform == "win32":
        try:
            from platform_desktop import files

            files.register(_REGISTRY)
        except Exception as e:
            logging.getLogger("bantu").warning("file tools unavailable: %s", e)
        try:
            from platform_desktop import system as sys_tools

            sys_tools.register(_REGISTRY)
        except Exception as e:
            logging.getLogger("bantu").warning("system tools unavailable: %s", e)
        try:
            from platform_desktop import shell

            shell.register(_REGISTRY)
        except Exception as e:
            logging.getLogger("bantu").warning("shell tool unavailable: %s", e)
        try:
            from platform_desktop import web

            web.register(_REGISTRY)
        except Exception as e:
            logging.getLogger("bantu").warning("web tools unavailable: %s", e)
        try:
            from platform_desktop import ocr

            ocr.register(_REGISTRY)
        except Exception as e:  # missing winsdk should not stop the agent booting
            logging.getLogger("bantu").warning("screen reading unavailable: %s", e)
        try:
            from platform_desktop import vision

            vision.register(_REGISTRY, router, settings)
        except Exception as e:
            logging.getLogger("bantu").warning("screen vision unavailable: %s", e)
        try:
            from platform_desktop import gui

            gui.register(_REGISTRY, router, settings)
        except Exception as e:
            logging.getLogger("bantu").warning("GUI control unavailable: %s", e)

    # Reminders fire on a daemon thread; how they are announced is platform
    # specific, so the portable scheduler takes a callback.
    def announce(title: str, body: str) -> None:
        print(f"\n{YELLOW}  [{title}] {body}{RESET}", flush=True)
        try:
            _REGISTRY.tools["notify"].fn(title=title, message=body)
        except Exception:
            pass
        for hook in list(_ANNOUNCERS):
            try:
                hook(title, body)
            except Exception:
                pass

    global _MEETING_NOTES
    meetings_store = meeting_tools.Meetings(memory.db)
    interrupted = meetings_store.recover()
    if interrupted:
        logging.getLogger("bantu").info("marked %d meeting(s) interrupted by the last shutdown", interrupted)
    removed = meetings_store.prune(getattr(settings, "meeting_retention_days", meeting_tools.RETENTION_DAYS))
    if removed:
        logging.getLogger("bantu").info("removed %d meeting transcript(s) past retention", removed)

    def recorder_factory(on_chunk):
        from platform_desktop.recorder import MeetingRecorder, microphone_name

        return MeetingRecorder(on_chunk, mic_name=microphone_name(settings),
                               on_warning=lambda text: announce("Meeting notes", text))

    def meeting_chat(messages, system):
        return router.chat(messages, tools=None, system=system, temperature=0.2, max_output_tokens=3000).text

    def meeting_status(text):
        for hook in list(_MEETING_STATUS):
            try:
                hook(text)
            except Exception:
                pass

    def meeting_done(meeting_id, text):
        print(f"\n{CYAN}  [Meeting notes]\n{text}{RESET}", flush=True)
        for hook in list(_MEETING_DONE):
            try:
                hook(meeting_id, text)
            except Exception:
                pass

    try:
        from platform_desktop.recorder import groq_segments_transcriber

        _MEETING_NOTES = meeting_tools.MeetingNotes(
            meetings_store, records, recorder_factory, groq_segments_transcriber(lambda: cfg.get_key("groq")),
            meeting_chat, on_status=meeting_status, on_done=meeting_done,
            known_names=lambda: [n for n in [getattr(settings, "username", "")] if n] + records.people(),
        )
        meeting_tools.register(_REGISTRY, _MEETING_NOTES)
    except Exception as e:
        logging.getLogger("bantu").warning("meeting notes unavailable: %s", e)

    scheduler = ReminderScheduler(
        memory, announce, records=records, calendar=_CALENDAR,
        alert_minutes=lambda: getattr(settings, "event_alert_minutes", 0),
    )
    scheduler.start()

    global _SPEAKER, _LISTENER
    try:
        from voice import Listener, Speaker

        _SPEAKER = Speaker(settings)
        _LISTENER = Listener(settings, cfg.get_key)
    except Exception as e:
        logging.getLogger("bantu").warning("voice unavailable: %s", e)

    agent = Agent(
        router=router,
        registry=_REGISTRY,
        memory=memory,
        settings=settings,
        on_event=make_printer(verbose="-v" in sys.argv),
        confirm=confirm,
    )
    return agent, settings


def run_hud() -> int:
    """Launch the floating orb, running first-time setup if it is needed."""
    from PyQt5.QtCore import QCoreApplication, QLockFile, Qt
    from PyQt5.QtWidgets import QApplication

    QCoreApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QCoreApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    app = QApplication(sys.argv)
    app.setApplicationName("Bantu")
    # Closing the panel hides it; Bantu keeps running until quit from the tray.
    app.setQuitOnLastWindowClosed(False)

    lock_path = cfg.user_data_dir() / "bantu.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(lock_path))
    if not lock.tryLock(100):
        print("Bantu is already running - look for the orb or the tray icon.")
        return 0

    settings = cfg.Settings.load()
    if cfg.needs_onboarding(settings):
        from ui.onboarding import SetupWizard
        from ui.setup_parts import real_services

        # A new user gets setup, not a console error about missing keys.
        if SetupWizard(settings, real_services()).exec_() != SetupWizard.Accepted:
            lock.unlock()
            return 0

    try:
        agent, settings = build(settings)
    except ProviderError as e:
        lock.unlock()
        from PyQt5.QtWidgets import QMessageBox

        QMessageBox.warning(None, "Bantu", f"Bantu could not start: {e}")
        return 1

    from ui.app import BantuApp

    hud = BantuApp(
        agent, settings, listener=_LISTENER, speaker=_SPEAKER, knowledge=_KNOWLEDGE, activity=_ACTIVITY,
        calendar=_CALENDAR, meeting_notes=_MEETING_NOTES,
    )
    _ANNOUNCERS.append(hud.announced.emit)
    _MEETING_STATUS.append(hud.meeting_status.emit)
    _MEETING_DONE.append(hud.meeting_done.emit)
    try:
        return app.exec_()
    finally:
        hud.shutdown()
        lock.unlock()


HELP = """\
  /tools     list registered tools and their permission tier
  /status    provider quota and cooldown state
  /facts     what Bantu remembers about you
  /reminders what is scheduled
  /listen    speak your next message instead of typing
  /voice     toggle spoken replies on or off
  /mics      list microphones
  /new       start a fresh conversation
  /quit      exit
"""


def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if not args and "--cli" not in sys.argv:
        return run_hud()

    try:
        agent, settings = build()
    except ProviderError as e:
        print(f"{RED}{e}{RESET}")
        print("Run Bantu without --cli once to set up your keys.")
        return 1

    if args:  # one-shot mode
        print(agent.run(" ".join(args)).text)
        return 0

    print(
        BANNER.format(
            assistant=settings.assistant_name,
            tools=len(agent.registry),
            providers=" → ".join(p.name for p in agent.router.providers),
        )
    )
    while True:
        try:
            line = input(f"{BOLD}you ›{RESET} ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not line:
            continue
        if line in ("/quit", "/exit"):
            return 0
        if line == "/help":
            print(HELP)
            continue
        if line == "/tools":
            for t in sorted(agent.registry.tools.values(), key=lambda x: (x.category, x.name)):
                print(f"  {t.tier.value:8} {t.category:8} {t.name:18} {t.description.splitlines()[0][:60]}")
            continue
        if line == "/status":
            for s in agent.router.status():
                print(f"  {s}")
            continue
        if line == "/facts":
            facts = agent.memory.all_facts()
            print("\n".join(f"  - {f}" for f in facts) if facts else "  (nothing yet)")
            continue
        if line == "/reminders":
            items = agent.memory.pending_reminders()
            if items:
                from core.reminders import describe

                for r in items:
                    print(f"  #{r['id']}  {describe(r['due_at'])}  {r['message']}")
            else:
                print("  (nothing scheduled)")
            continue
        if line == "/voice":
            settings.voice_enabled = not settings.voice_enabled
            settings.save()
            print(f"{DIM}  spoken replies {'on' if settings.voice_enabled else 'off'}{RESET}")
            continue
        if line == "/mics":
            try:
                from voice import list_devices

                cur = getattr(settings, "mic_device", None)
                for i, name in list_devices():
                    mark = " <- current" if i == cur else (" <- default" if cur is None and i == 0 else "")
                    print(f"  [{i}] {name}{mark}")
                print(f"{DIM}  set one with /mic <number>{RESET}")
            except Exception as e:
                print(f"{RED}  {e}{RESET}")
            continue
        if line.startswith("/mic "):
            try:
                settings.mic_device = int(line.split()[1])
                settings.save()
                if _LISTENER:
                    _LISTENER.device = settings.mic_device
                    _LISTENER._threshold = None
                print(f"{DIM}  microphone set to {settings.mic_device}{RESET}")
            except (ValueError, IndexError):
                print(f"{RED}  usage: /mic <number>, see /mics{RESET}")
            continue
        if line == "/listen":
            if _LISTENER is None:
                print(f"{RED}  voice input is unavailable{RESET}")
                continue
            if _SPEAKER:
                _SPEAKER.stop()   # barge-in: never talk over the user
            print(f"{DIM}  listening... (speak, then pause){RESET}", flush=True)
            try:
                line = _LISTENER.listen(
                    on_start=lambda: print(f"{DIM}  hearing you{RESET}", flush=True)
                )
            except Exception as e:
                print(f"{RED}  {e}{RESET}")
                continue
            if not line.strip():
                print(f"{DIM}  nothing heard{RESET}")
                continue
            print(f"{BOLD}you ›{RESET} {line}")
        if line == "/new":
            agent.memory.new_conversation()
            print(f"{DIM}  new conversation{RESET}")
            continue

        result = agent.run(line)
        print(f"\n{CYAN}{settings.assistant_name} ›{RESET} {result.text}\n")
        if "-v" in sys.argv:
            print(f"{DIM}  {result.turns} turns, {result.tool_calls} tool calls, "
                  f"{result.provider}/{result.model}{RESET}\n")


def _cleanup() -> None:
    for shut in (lambda: _SPEAKER and _SPEAKER.shutdown(),):
        try:
            shut()
        except Exception:
            pass
    try:
        from platform_desktop import web

        web.shutdown()
    except Exception:
        pass


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        _cleanup()
