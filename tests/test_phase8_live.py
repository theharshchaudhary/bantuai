"""Phase 8 live test — really moves the mouse and types. Do not touch the mouse
or keyboard while it runs (~30s).

Starts tests/gui_target.py as a separate app, drives it with Bantu's GUI tools,
and checks the target app's own record of what happened. This is the only test
that proves an OCR position becomes a click on the right pixel at the machine's
real display scaling.

    .venv/Scripts/python.exe tests/test_phase8_live.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from platform_desktop import gui  # noqa: E402

MODE = gui.ensure_dpi_awareness()

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from core.providers.base import ToolCall  # noqa: E402
from core.tools.registry import ToolRegistry  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name + (f"  -> {detail}" if detail and not cond else ""), flush=True)


STATE = Path(tempfile.mkdtemp()) / "gui_state.json"
TITLE = "Bantu GUI Test 8431"


def state() -> dict:
    for _ in range(20):
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            time.sleep(0.05)
    return {}


def settle(pred, timeout=4.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if pred(state()):
            return True
        time.sleep(0.08)
    return pred(state())


class TargetGone(Exception):
    pass


def section(title: str) -> None:
    """Refuse to continue against a dead target: its stale state file would
    otherwise make later checks pass or fail for reasons unrelated to Bantu."""
    print(f"\n[{title}]", flush=True)
    if target.poll() is not None:
        raise TargetGone(f"the test app exited (code {target.poll()}) before '{title}' - was its window closed?")
    try:
        gui.resolve_window("Bantu GUI Test")
    except Exception as e:
        raise TargetGone(f"the test window is gone before '{title}': {e}") from e


router = None
try:
    from core import config as cfg
    from core.providers.router import build_router

    if cfg.get_key("gemini"):
        router = build_router(cfg.Settings.load())
except Exception as e:
    print("  (vision unavailable:", e, ")")

reg = ToolRegistry()
gui.register(reg, router, None)


def run(tool, **kw):
    return reg.execute(ToolCall("t", tool, kw), confirm=lambda t, a: True)


print("\n[setup]")
check("the process is DPI-aware", gui.coordinates_consistent(), MODE)

target = subprocess.Popen([sys.executable, str(ROOT / "tests" / "gui_target.py"), str(STATE)])
try:
    check("the target app starts", settle(lambda s: s.get("dot_center") is not None, 15))
    time.sleep(0.8)
    w = gui.resolve_window("Bantu GUI Test")
    check("the target window is found by title", w is not None and TITLE in w.title,
          w.title if w else None)
    check("it belongs to another process", w is not None and w.pid != os.getpid())
    check("it can be brought to the front", w is not None and gui.activate(w))

    section("click by text")
    out = run("click_text", text="Launch probe", window="Bantu GUI Test")
    check("click_text reports the click", out.startswith("Clicked"), out)
    check("THE CLICK LANDED ON THE BUTTON at real display scaling",
          settle(lambda s: s.get("launch") == 1), f"launch={state().get('launch')}")

    section("ambiguity")
    out = run("click_text", text="Duplicate", window="Bantu GUI Test")
    check("two matches are refused rather than guessed", "2 places on screen match" in out, out[:120])
    time.sleep(0.5)
    check("...and nothing was clicked", state().get("dup") == [0, 0], state().get("dup"))
    out = run("click_text", text="Duplicate", window="Bantu GUI Test", occurrence=2)
    check("occurrence picks the one asked for", settle(lambda s: s.get("dup") == [0, 1]),
          f"{out} dup={state().get('dup')}")

    section("typing")
    out = run("click_text", text="Type here please", window="Bantu GUI Test")
    check("the input field is clicked by its placeholder", out.startswith("Clicked"), out)
    typed = "hello नमस्ते 42"
    out = run("type_text", text=typed, window="Bantu GUI Test")
    check("type_text reports success", out.startswith("Typed"), out)
    check("the exact text arrived, Devanagari included",
          settle(lambda s: s.get("text") == typed), repr(state().get("text")))
    out = run("press_keys", keys="ctrl+a, backspace", window="Bantu GUI Test")
    check("press_keys ran a two-step sequence", out.startswith("Pressed"), out)
    check("select-all then backspace cleared the field", settle(lambda s: s.get("text") == ""),
          repr(state().get("text")))

    section("scrolling")
    lr = state()["list_rect"]
    import pyautogui

    pyautogui.moveTo((lr[0] + lr[2]) // 2, (lr[1] + lr[3]) // 2)
    before = state().get("scroll", 0)
    out = run("scroll", direction="down", notches=4)
    check("scrolling down moves the list", settle(lambda s: s.get("scroll", 0) > before),
          f"{out} {before}->{state().get('scroll')}")
    after_down = state().get("scroll", 0)
    run("scroll", direction="up", notches=2)
    check("scrolling up moves it back", settle(lambda s: s.get("scroll", 0) < after_down),
          f"{after_down}->{state().get('scroll')}")

    section("waiting")
    run("click_text", text="Show later", window="Bantu GUI Test")
    t0 = time.time()
    out = run("wait_for_text", text="Ready now", timeout_seconds=8, window="Bantu GUI Test")
    check("wait_for_text sees text that appears later", "is visible" in out, out)
    check("...and genuinely waited for it", time.time() - t0 > 1.0, f"{time.time() - t0:.1f}s")
    out = run("wait_for_text", text="never going to appear xyz", timeout_seconds=1, window="Bantu GUI Test")
    check("a timeout is reported, not raised", "did not appear" in out, out[:80])

    section("refusals")
    out = run("click_at", x=-50000, y=10)
    check("a click off the screen is refused", out.startswith("Error") and "outside the screen" in out, out)
    out = run("click_text", text="nothing like this is on screen qq", window="Bantu GUI Test")
    check("missing text lists what IS visible so the model can retry",
          "not visible" in out and "Launch" in out, out[:160])

    section("vision fallback")
    if router is None:
        print("  skip  no Gemini key")
    else:
        dot = state()["dot_center"]
        out = run("locate_on_screen", description="the solid red circle", window="Bantu GUI Test")
        print("       ", out)
        import re

        m = re.search(r"\((\d+), (\d+)\)", out)
        if m:
            fx, fy = int(m.group(1)), int(m.group(2))
            err = ((fx - dot[0]) ** 2 + (fy - dot[1]) ** 2) ** 0.5
            check("vision located the icon within its radius", err <= 30,
                  f"found ({fx},{fy}) true {dot} off by {err:.0f}px")
        else:
            check("vision located the icon", False, out)
except TargetGone as e:
    check("the test app stayed open for the whole run", False, str(e))
finally:
    target.terminate()
    try:
        target.wait(5)
    except subprocess.TimeoutExpired:
        target.kill()

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
for f in FAIL:
    print("  FAILED:", f)
sys.exit(1 if FAIL else 0)
