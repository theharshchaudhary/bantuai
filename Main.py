"""BantuAI — command line entry point.

A terminal front end while the HUD is built (Phase 07). It wires the portable
core to the Windows tool set and runs a conversation.

    .venv/Scripts/python.exe main.py
    .venv/Scripts/python.exe main.py "read my screen"
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

from core import config as cfg
from core.agent import Agent, Event
from core.memory import Memory
from core.providers.base import ProviderError
from core.providers.router import build_router
from core.tools import builtin
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


def build() -> tuple[Agent, cfg.Settings]:
    settings = cfg.Settings.load()
    router = build_router(settings)
    memory = Memory(cfg.user_data_dir() / "history.db")

    builtin.register(_REGISTRY, memory)

    # Windows-only tools are registered INTO the portable core, never imported by it.
    if sys.platform == "win32":
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

    agent = Agent(
        router=router,
        registry=_REGISTRY,
        memory=memory,
        settings=settings,
        on_event=make_printer(verbose="-v" in sys.argv),
        confirm=confirm,
    )
    return agent, settings


HELP = """\
  /tools     list registered tools and their permission tier
  /status    provider quota and cooldown state
  /facts     what Bantu remembers about you
  /new       start a fresh conversation
  /quit      exit
"""


def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    try:
        agent, settings = build()
    except ProviderError as e:
        print(f"{RED}{e}{RESET}")
        return 1

    args = [a for a in sys.argv[1:] if not a.startswith("-")]
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
        if line == "/new":
            agent.memory.new_conversation()
            print(f"{DIM}  new conversation{RESET}")
            continue

        result = agent.run(line)
        print(f"\n{CYAN}{settings.assistant_name} ›{RESET} {result.text}\n")
        if "-v" in sys.argv:
            print(f"{DIM}  {result.turns} turns, {result.tool_calls} tool calls, "
                  f"{result.provider}/{result.model}{RESET}\n")


if __name__ == "__main__":
    raise SystemExit(main())
