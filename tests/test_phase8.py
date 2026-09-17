"""Phase 8 tests — GUI control logic. No API key, no mouse movement, no windows.

The screen, OCR engine and input devices are replaced with fakes so the
decisions can be checked exactly: which match gets clicked, when a click is
refused, how coordinates are mapped, and what may be typed into a terminal.
tests/test_phase8_live.py covers the real screen.

    .venv/Scripts/python.exe tests/test_phase8.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from core.providers.base import ToolCall
from core.tools.registry import Tier, ToolError, ToolRegistry
from platform_desktop import gui, ocr
from platform_desktop.ocr import Word

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name + (f"  -> {detail}" if detail and not cond else ""))


def raises(fn, *a, **kw) -> str | None:
    try:
        fn(*a, **kw)
    except ToolError as e:
        return str(e)
    return None


# --- process setup ----------------------------------------------------------

print("\n[DPI]")
mode = gui.ensure_dpi_awareness()
check("the process ends up DPI-aware", gui.coordinates_consistent(), mode)
check("calling it twice is harmless", gui.ensure_dpi_awareness() in ("per-monitor", "already set"),
      gui.ensure_dpi_awareness())


# --- vision boxes -----------------------------------------------------------

print("\n[parsing vision boxes]")
check("plain JSON", gui.parse_box('{"found": true, "box_2d": [100, 200, 300, 400]}') == (100, 200, 300, 400))
check("inside a code fence", gui.parse_box('```json\n{"found": true, "box_2d": [10, 20, 30, 40]}\n```')
      == (10, 20, 30, 40))
check("with prose around it", gui.parse_box('Sure! {"box_2d": [1, 2, 3, 4]} hope that helps')
      == (1, 2, 3, 4))
check("a bare list of four numbers", gui.parse_box("[500, 600, 700, 800]") == (500, 600, 700, 800))
check("found:false means not found", gui.parse_box('{"found": false}') is None)
check("an inverted box is rejected", gui.parse_box('{"box_2d": [300, 200, 100, 400]}') is None)
check("values beyond 1000 are rejected", gui.parse_box('{"box_2d": [0, 0, 1200, 50]}') is None)
check("empty reply is not found", gui.parse_box("") is None)
check("prose with no box is not found", gui.parse_box("I could not see it anywhere.") is None)

print("\n[mapping boxes to the screen]")
check("centre of a full box is the centre of the rect",
      gui.box_center((0, 0, 1000, 1000), (0, 0, 1920, 1080)) == (960, 540))
check("x comes from xmin/xmax and y from ymin/ymax, not swapped",
      gui.box_center((0, 900, 100, 1000), (0, 0, 1000, 1000)) == (950, 50))
check("a window's offset is added",
      gui.box_center((0, 0, 1000, 1000), (160, 120, 720, 640)) == (440, 380))
check("a monitor left of the primary gives negative coordinates",
      gui.box_center((0, 0, 1000, 1000), (-1920, 0, 0, 1080)) == (-960, 540))


# --- OCR coordinates --------------------------------------------------------

print("\n[OCR coordinates are absolute]")


class FakeImage:
    def __init__(self, w, h):
        self.width, self.height = w, h
        self.size = (w, h)

    def resize(self, size, *_):
        return FakeImage(*size)

    def save(self, *a, **k):
        pass


def fake_capture(img_size, image_words):
    import PIL.ImageGrab

    grabbed = {}

    def grab(bbox=None, all_screens=False):
        grabbed["bbox"] = bbox
        return FakeImage(*img_size)

    async def recognise(_path):
        return "text", [Word(t, x, y, w, h, line) for t, x, y, w, h, line in image_words]

    PIL.ImageGrab.grab, ocr._recognize = grab, recognise
    return grabbed


real_grab = __import__("PIL.ImageGrab", fromlist=["grab"]).grab
real_recognize = ocr._recognize
try:
    fake_capture((400, 300), [("Save", 40, 20, 30, 10, 0)])
    _, words = ocr.capture_words((1000, 500, 1400, 800), scale=1)
    check("a word is shifted by where the capture sits on screen",
          (words[0].x, words[0].y) == (1040, 520), (words[0].x, words[0].y))

    fake_capture((400, 300), [("Save", 120, 60, 90, 30, 0)])
    _, words = ocr.capture_words((1000, 500, 1400, 800), scale=3)
    check("an upscaled pass is scaled back down before shifting",
          (words[0].x, words[0].y, words[0].w, words[0].h) == (1040, 520, 30, 10),
          (words[0].x, words[0].y, words[0].w, words[0].h))

    fake_capture((4000, 1000), [("x", 0, 0, 10, 10, 0)])
    ocr.capture_words((0, 0, 4000, 1000), scale=3)
    check("upscaling is capped below the OCR engine's size limit",
          ocr._OCR_MAX_SIDE // 4000 == 2)
finally:
    import PIL.ImageGrab

    PIL.ImageGrab.grab, ocr._recognize = real_grab, real_recognize


# --- choosing what to click -------------------------------------------------

print("\n[choosing a match]")


def row(*items):
    """items: (text, x, y, line) with a 40x16 box each."""
    return [Word(t, x, y, 40, 16, line) for t, x, y, line in items]


title_and_button = row(("Save", 10, 10, 0), ("As", 55, 10, 0), ("Save", 300, 400, 1))
m, tier, _ = gui.choose_match(title_and_button, "Save")
check("an exact match beats a partial one (button 'Save' over title 'Save As')",
      m is not None and m.x == 300 and tier == "exact", (m, tier))

two = row(("Delete", 10, 10, 0), ("Delete", 10, 200, 1))
msg = raises(gui.choose_match, two, "Delete")
check("two exact matches are refused, not guessed", msg is not None and "2 places" in msg, msg)
check("...and the refusal lists both positions", msg is not None and msg.count("occurrence=") == 2, msg)
m, _, _ = gui.choose_match(two, "Delete", occurrence=2)
check("occurrence picks the second, counted top to bottom", m.y == 200, m)
check("an occurrence that does not exist is refused",
      "does not exist" in (raises(gui.choose_match, two, "Delete", 3) or ""))

m, tier, _ = gui.choose_match(row(("Dupicate", 10, 10, 0)), "Duplicate")
check("an OCR misspelling still matches, marked fuzzy", m is not None and tier == "fuzzy", (m, tier))
m, tier, _ = gui.choose_match(row(("Cancel", 10, 10, 0)), "Duplicate")
check("an unrelated word is not a fuzzy match", m is None, (m, tier))
m, tier, _ = gui.choose_match(row(("Save", 10, 10, 0), ("As", 55, 10, 0)), "Save")
check("a partial match is used when there is no exact one", m is not None and tier == "partial", tier)
check("nothing on screen returns no match", gui.choose_match([], "Save")[0] is None)

one_pass = row(("Launch", 100, 100, 0), ("probe", 145, 100, 0))
other_pass = row(("Launch", 102, 101, 0), ("probe", 146, 101, 0))
m, _, pool = gui.choose_match([one_pass, other_pass], "Launch probe")
check("the same button found by two OCR passes counts once, not as ambiguous",
      m is not None and len(pool) == 1, len(pool))

split = row(("Show", 10, 10, 0), ("later", 55, 10, 0), ("Show", 10, 300, 1), ("Ster", 55, 300, 1))
exact, partial, fuzzy = gui.rank_matches(split, "Show later")
check("fuzzy matches are ignored when an exact one exists", exact and not fuzzy, (exact, fuzzy))


# --- keys -------------------------------------------------------------------

print("\n[key combinations]")
check("a simple shortcut is accepted", gui.validate_keys("Ctrl+S") == "ctrl+s")
check("a comma-separated sequence is accepted", gui.validate_keys("ctrl+a, backspace") == "ctrl+a, backspace")
check("an unknown key is refused", raises(gui.validate_keys, "ctrl+banana") is not None)
check("an empty combination is refused", raises(gui.validate_keys, "  ") is not None)


# --- tools, with the screen and devices faked -------------------------------

print("\n[tools]")
reg = ToolRegistry()
gui._tracker.start = lambda: None  # no background polling in tests
gui.register(reg, None, None)

tiers = {n: t.tier for n, t in reg.tools.items()}
check("clicking, typing and key presses need confirmation",
      all(tiers[n] is Tier.CONFIRM for n in ("click_text", "click_at", "type_text", "press_keys")), tiers)
check("scrolling, waiting and locating run without asking",
      all(tiers[n] is Tier.AUTO for n in ("scroll", "wait_for_text", "locate_on_screen")), tiers)

NOTEPAD = gui.Window(1, "Untitled - Notepad", 4242, "notepad.exe", (0, 0, 800, 600))
SHELL = gui.Window(2, "Windows PowerShell", 4343, "powershell.exe", (0, 0, 800, 600))

clicks, typed, keys, asides = [], [], [], []
screen = {"passes": [row(("Save", 100, 100, 0))], "window": NOTEPAD}

gui.resolve_window = lambda title="": screen["window"]
gui.activate = lambda w: True
gui.screen_passes = lambda w, scales=gui.OCR_SCALES: screen["passes"]
gui._click = lambda x, y, button, double: clicks.append((x, y, button, double))
gui._type = lambda text: typed.append(text)
gui.add_before_action(lambda: asides.append("aside"))
import keyboard

keyboard.send = lambda combo: keys.append(combo)


def run(tool, **kw):
    return reg.execute(ToolCall("t", tool, kw), confirm=lambda t, a: True)


out = run("click_text", text="Save", window="Notepad")
check("click_text clicks the centre of the match", clicks == [(120, 108, "left", False)], (out, clicks))
check("...and says where", "(120, 108)" in out and "Notepad" in out, out)
check("Bantu steps aside before reading the screen", asides, asides)

clicks.clear()
screen["passes"] = [row(("Save", 100, 100, 0), ("Save", 100, 300, 1))]
out = run("click_text", text="Save", window="Notepad")
check("an ambiguous click_text clicks nothing", clicks == [] and "2 places" in out, out)

screen["passes"] = [row(("Dupicate", 100, 100, 0))]
out = run("click_text", text="Duplicate", window="Notepad")
check("a fuzzy click tells the model it was a near match", "closest reading" in out, out)

clicks.clear()
screen["passes"] = [row(("Open", 100, 100, 0))]
out = run("click_text", text="Save", window="Notepad")
check("a missing target clicks nothing", clicks == [], clicks)
check("...and lists the text that is visible", "not visible" in out and "Open" in out, out)

out = run("click_text", text="   ", window="Notepad")
check("empty text is refused", out.startswith("Error"), out)

typed.clear()
out = run("type_text", text="hello नमस्ते", window="Notepad")
check("type_text types into an ordinary app", typed == ["hello नमस्ते"], (out, typed))

typed.clear()
screen["window"] = SHELL
out = run("type_text", text="Format-Volume -DriveLetter C", window="PowerShell", press_enter=True)
check("a blocked command typed into a terminal is refused", out.startswith("Error: refused"), out)
check("...nothing was typed", typed == [], typed)
check("...and Enter was not pressed", "enter" not in keys, keys)

out = run("type_text", text="Get-Date", window="PowerShell", press_enter=True)
check("an ordinary command in a terminal is typed", typed == ["Get-Date"], (out, typed))
check("...and Enter is pressed when asked", keys[-1:] == ["enter"], keys)

screen["window"] = NOTEPAD
typed.clear()
out = run("type_text", text="Format-Volume is a PowerShell cmdlet", window="Notepad")
check("the same words typed into a text editor are allowed", typed != [], out)

out = run("press_keys", keys="ctrl+s", window="Notepad")
check("press_keys sends the combination", keys[-1] == "ctrl+s", (out, keys))
out = run("press_keys", keys="ctrl+banana", window="Notepad")
check("press_keys refuses an unknown key before touching the app", out.startswith("Error"), out)

screen["window"] = None
out = run("type_text", text="hello")
check("with no known target app, typing is refused rather than sent anywhere",
      out.startswith("Error") and "no target window" in out, out)

out = run("scroll", direction="sideways")
check("scroll refuses a nonsense direction", out.startswith("Error"), out)

print("\n[a click on Bantu itself]")
real = gui.point_is_ours
gui.point_is_ours = lambda x, y: True
import importlib

guard_msg = raises(gui._guard_point, 10, 10)
check("a click that would land on Bantu's own window is refused",
      guard_msg is not None and "Bantu's own window" in guard_msg, guard_msg)
gui.point_is_ours = real

print("\n[a window entirely off screen]")
check("clipping an off-screen window is refused", raises(gui._clip, (-99999, -99999, -99000, -99000)) is not None)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
for f in FAIL:
    print("  FAILED:", f)
sys.exit(1 if FAIL else 0)
