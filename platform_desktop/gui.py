"""GUI control: click, type, press keys and scroll in any application.

Targets are found by reading the screen with Windows OCR, so "click Save" is a
local lookup plus a mouse move — no model call. Only targets with no text on
them (icons, sliders) fall back to Gemini vision, which is capped near 120
calls a day.

Three things make this correct rather than approximately correct:

1. **DPI awareness.** At 125% display scaling, a process that has not opted in
   gets screenshots in physical pixels but window rectangles and cursor
   positions in scaled ones — every click lands 25% off. `ensure_dpi_awareness`
   must run before anything else touches the screen.
2. **Absolute coordinates.** OCR boxes are relative to the captured image;
   they are shifted by the capture origin, which is negative on some
   multi-monitor layouts.
3. **Bantu never operates itself.** Its own windows are excluded from OCR
   matches and a click that would land on one is refused, so "click Save"
   cannot hit the words "click Save" in Bantu's own chat panel.

Every action that changes state is CONFIRM. A wrong click cannot be undone.
Moving the mouse into any screen corner aborts (pyautogui's fail-safe).
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import re
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from core.tools.registry import Tier, ToolError, ToolRegistry

log = logging.getLogger("bantu.gui")

#: Processes that execute what is typed into them. Typing into one of these
#: goes through the same deny-list as run_powershell, otherwise type_text plus
#: Enter would be a way around it.
TERMINALS = {
    "powershell.exe", "pwsh.exe", "cmd.exe", "windowsterminal.exe", "wt.exe",
    "conhost.exe", "openconsole.exe", "bash.exe", "wsl.exe", "mintty.exe",
    "alacritty.exe", "wezterm-gui.exe", "putty.exe",
}

#: Callbacks run before any screen capture or input, so an always-on-top UI can
#: step out of the way. The HUD registers one that hides its panel.
_before_action: list[Callable[[], None]] = []

WHEEL_DELTA = 120  # one notch; pyautogui's raw scroll units are 1/120 of a notch


# --- process setup ----------------------------------------------------------


def ensure_dpi_awareness() -> str:
    """Opt the process into physical-pixel coordinates. Call before anything else.

    Returns which mode is in effect. Safe to call repeatedly: Windows lets the
    awareness be set once, and later calls are no-ops.
    """
    if sys.platform != "win32":
        return "n/a"
    try:
        if ctypes.windll.shcore.SetProcessDpiAwareness(2) == 0:
            return "per-monitor"
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except (AttributeError, OSError):
        pass
    return "already set" if coordinates_consistent() else "unaware"


def coordinates_consistent() -> bool:
    """True when window/cursor coordinates and screenshots share one pixel space."""
    if sys.platform != "win32":
        return True
    user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
    hdc = user32.GetDC(0)
    try:
        physical = gdi32.GetDeviceCaps(hdc, 118)  # DESKTOPHORZRES
    finally:
        user32.ReleaseDC(0, hdc)
    return user32.GetSystemMetrics(0) == physical


def virtual_screen() -> tuple[int, int, int, int]:
    """(left, top, right, bottom) of the whole desktop across all monitors."""
    u = ctypes.windll.user32
    left, top = u.GetSystemMetrics(76), u.GetSystemMetrics(77)
    return left, top, left + u.GetSystemMetrics(78), top + u.GetSystemMetrics(79)


def add_before_action(hook: Callable[[], None]) -> None:
    _before_action.append(hook)


def remove_before_action(hook: Callable[[], None]) -> None:
    if hook in _before_action:
        _before_action.remove(hook)


def _step_aside() -> None:
    for hook in list(_before_action):
        try:
            hook()
        except Exception:
            log.exception("before-action hook failed")


# --- windows ----------------------------------------------------------------


@dataclass
class Window:
    hwnd: int
    title: str
    pid: int
    process: str
    rect: tuple[int, int, int, int]

    @property
    def is_terminal(self) -> bool:
        return self.process.lower() in TERMINALS


def _describe(hwnd: int) -> Window | None:
    import psutil
    import win32gui
    import win32process

    try:
        if not win32gui.IsWindow(hwnd) or not win32gui.IsWindowVisible(hwnd):
            return None
        pid = win32process.GetWindowThreadProcessId(hwnd)[1]
        try:
            proc = psutil.Process(pid).name()
        except (psutil.Error, OSError):
            proc = "?"
        return Window(hwnd, win32gui.GetWindowText(hwnd), pid, proc, win32gui.GetWindowRect(hwnd))
    except Exception:
        return None


def top_level_windows() -> list[Window]:
    """Visible titled top-level windows, front to back."""
    import win32gui

    found: list[Window] = []

    def collect(hwnd, _):
        if win32gui.IsWindowVisible(hwnd) and win32gui.GetWindowText(hwnd):
            w = _describe(hwnd)
            if w and w.rect[2] - w.rect[0] > 1 and w.rect[3] - w.rect[1] > 1:
                found.append(w)

    win32gui.EnumWindows(collect, None)
    return found


def own_rects() -> list[tuple[int, int, int, int]]:
    """Screen rectangles of every visible window this process owns."""
    import win32gui
    import win32process

    me = os.getpid()
    rects: list[tuple[int, int, int, int]] = []

    def collect(hwnd, _):
        if win32gui.IsWindowVisible(hwnd) and win32process.GetWindowThreadProcessId(hwnd)[1] == me:
            rects.append(win32gui.GetWindowRect(hwnd))

    win32gui.EnumWindows(collect, None)
    return rects


def point_is_ours(x: int, y: int) -> bool:
    """Whether a click at (x, y) would land on one of Bantu's own windows."""
    import win32gui
    import win32process

    try:
        hwnd = win32gui.WindowFromPoint((x, y))
        root = ctypes.windll.user32.GetAncestor(hwnd, 2) or hwnd  # GA_ROOT
        return win32process.GetWindowThreadProcessId(root)[1] == os.getpid()
    except Exception:
        return False


class ForegroundTracker:
    """Remembers the last window the user was in that is not Bantu.

    Opening Bantu's panel makes Bantu the foreground window, so "type this"
    would otherwise type into Bantu itself.
    """

    def __init__(self, interval: float = 0.3):
        self.interval = interval
        self.last: int | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="bantu-foreground", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def sample(self) -> None:
        import win32gui
        import win32process

        hwnd = win32gui.GetForegroundWindow()
        if not hwnd:
            return
        try:
            if win32process.GetWindowThreadProcessId(hwnd)[1] == os.getpid():
                return
        except Exception:
            return
        if win32gui.GetWindowText(hwnd):
            self.last = hwnd

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.sample()
            except Exception:
                pass
            self._stop.wait(self.interval)


_tracker = ForegroundTracker()


def resolve_window(title: str = "") -> Window | None:
    """The window a GUI action should target.

    A title picks the frontmost visible window containing it. Without one, the
    current foreground window is used unless it is Bantu, in which case the
    last window the user was actually working in.
    """
    import win32gui

    me = os.getpid()
    if title.strip():
        needle = title.lower().strip()
        for w in top_level_windows():
            if w.pid != me and needle in w.title.lower():
                return w
        raise ToolError(
            f"no open window has {title!r} in its title. Call list_windows to see the exact titles."
        )

    fg = _describe(win32gui.GetForegroundWindow())
    if fg and fg.pid != me:
        return fg
    if _tracker.last:
        prev = _describe(_tracker.last)
        if prev and prev.pid != me:
            return prev
    return None


def activate(w: Window) -> bool:
    """Bring a window to the front and verify it actually got there.

    Windows only lets the foreground process hand focus away, so the input
    queues are briefly attached. No synthetic Alt tap: that opens the menu bar
    in Notepad, VS Code and others, and typed text then triggers menu items.
    """
    import win32con
    import win32gui
    import win32process

    user32 = ctypes.windll.user32
    if win32gui.GetForegroundWindow() == w.hwnd:
        return True
    try:
        if win32gui.IsIconic(w.hwnd):
            win32gui.ShowWindow(w.hwnd, win32con.SW_RESTORE)
        fg = win32gui.GetForegroundWindow()
        fg_thread = win32process.GetWindowThreadProcessId(fg)[0] if fg else 0
        me_thread = ctypes.windll.kernel32.GetCurrentThreadId()
        attached = bool(fg_thread) and fg_thread != me_thread and user32.AttachThreadInput(me_thread, fg_thread, True)
        try:
            user32.BringWindowToTop(w.hwnd)
            user32.SetForegroundWindow(w.hwnd)
        finally:
            if attached:
                user32.AttachThreadInput(me_thread, fg_thread, False)
    except Exception as e:
        log.info("activate %r: %s", w.title, e)

    for _ in range(25):
        if win32gui.GetForegroundWindow() == w.hwnd:
            return True
        time.sleep(0.02)

    # Last resort that Windows always honours: minimise and restore.
    try:
        win32gui.ShowWindow(w.hwnd, win32con.SW_MINIMIZE)
        win32gui.ShowWindow(w.hwnd, win32con.SW_RESTORE)
    except Exception:
        pass
    for _ in range(25):
        if win32gui.GetForegroundWindow() == w.hwnd:
            return True
        time.sleep(0.02)
    return False


# --- reading the screen -----------------------------------------------------


def _clip(rect: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    vl, vt, vr, vb = virtual_screen()
    left, top, right, bottom = rect
    clipped = max(left, vl), max(top, vt), min(right, vr), min(bottom, vb)
    if clipped[2] - clipped[0] < 2 or clipped[3] - clipped[1] < 2:
        raise ToolError("the target window is entirely off screen")
    return clipped


def _inside(x: int, y: int, rect: tuple[int, int, int, int]) -> bool:
    return rect[0] <= x < rect[2] and rect[1] <= y < rect[3]


#: OCR passes. Each scale misreads different words, so reading twice and
#: merging finds text that either pass alone would miss.
OCR_SCALES = (1, 3)
FUZZY_RATIO = 0.8
SAME_SPOT_PX = 12
_PUNCT = ".:,;!?\u2026'\"()[]"


def screen_passes(w: Window | None, scales: tuple[int, ...] = OCR_SCALES):
    """OCR the target at several scales, each in absolute screen coordinates.

    Bantu's own windows are removed from every pass so it never matches, and
    never clicks, the text of its own chat panel.
    """
    from .ocr import capture_words

    bbox = _clip(w.rect) if w else None
    mine = own_rects()
    passes = []
    for scale in scales:
        _, words = capture_words(bbox, scale=scale)
        passes.append([wd for wd in words if not any(_inside(*wd.center, r) for r in mine)])
    return passes


def screen_words(w: Window | None):
    """Single-pass OCR words, for callers that only need what is readable."""
    return screen_passes(w, (1,))[0]


def _norm(text: str) -> str:
    return " ".join(text.lower().split()).strip(_PUNCT)


def _fuzzy_matches(words, needle: str):
    """Near-misses for text OCR slightly misspelled ("Dupicate" for "Duplicate")."""
    from difflib import SequenceMatcher

    from .ocr import Match

    target = _norm(needle)
    span = len(target.split()) + 2
    out = []
    for start in range(len(words)):
        parts = []
        for i in range(start, min(start + span, len(words))):
            if words[i].line != words[start].line:
                break
            parts.append(words[i])
            cand = _norm(" ".join(p.text for p in parts))
            if abs(len(cand) - len(target)) > max(2, len(target) * 0.3):
                continue
            if SequenceMatcher(None, cand, target).ratio() >= FUZZY_RATIO:
                x = min(p.x for p in parts)
                y = min(p.y for p in parts)
                out.append(Match(" ".join(p.text for p in parts), x, y,
                                 max(p.x + p.w for p in parts) - x,
                                 max(p.y + p.h for p in parts) - y))
    return out


def _dedupe(matches):
    """Two OCR passes find the same button twice; keep one per place on screen."""
    kept = []
    for m in matches:
        cx, cy = m.center
        if all(abs(cx - k.center[0]) > SAME_SPOT_PX or abs(cy - k.center[1]) > SAME_SPOT_PX
               for k in kept):
            kept.append(m)
    return kept


def _standalone(match, words) -> bool:
    """Whether a match is a whole label rather than part of a longer one.

    "Save" inside a window title reading "Save As" must not count as an exact
    match for a Save button: if OCR misses the button, that title would be the
    only exact hit and get clicked. A neighbour on the same line closer than
    ordinary word spacing means the match is embedded in a longer phrase.
    """
    if match.line < 0:
        return True
    gap = max(4, int(match.h * 0.9))
    left, right = match.x, match.x + match.w
    for wd in words:
        if wd.line != match.line:
            continue
        if left - gap <= wd.x + wd.w <= left - 1 or right + 1 <= wd.x <= right + gap:
            return False
    return True


def rank_matches(passes, needle: str):
    """Tiers of matches — exact, then partial, then fuzzy — each deduplicated, top to bottom.

    Accepts one list of words or several OCR passes. Fuzzy matches are only
    considered when nothing matched exactly or partially.
    """
    from .ocr import find_matches

    if passes and not isinstance(passes[0], list):
        passes = [passes]
    target = _norm(needle)
    exact, partial = [], []
    for words in passes:
        for h in find_matches(words, needle):
            whole = _norm(h.text) == target and _standalone(h, words)
            (exact if whole else partial).append(h)
    fuzzy = []
    if not exact and not partial:
        for words in passes:
            fuzzy.extend(_fuzzy_matches(words, needle))
    key = lambda h: (h.y // 12, h.x)  # rows of text first, then left to right
    return tuple(sorted(_dedupe(t), key=key) for t in (exact, partial, fuzzy))


def choose_match(passes, needle: str, occurrence: int = 0):
    """Pick one match, refusing to guess between several.

    A wrong click cannot be undone, so an ambiguous target is an error that
    lists the candidates, not a coin toss. Returns (match, tier, pool).
    """
    exact, partial, fuzzy = rank_matches(passes, needle)
    tier, pool = "", []
    for name, found in (("exact", exact), ("partial", partial), ("fuzzy", fuzzy)):
        if found:
            tier, pool = name, found
            break
    if not pool:
        return None, "", []
    if occurrence >= 1:
        if occurrence > len(pool):
            raise ToolError(
                f"only {len(pool)} match(es) for {needle!r}; occurrence {occurrence} does not exist"
            )
        return pool[occurrence - 1], tier, pool
    if len(pool) == 1:
        return pool[0], tier, pool
    listing = "\n".join(
        f"  occurrence={i}: {h.text!r} at ({h.center[0]}, {h.center[1]})"
        for i, h in enumerate(pool[:8], 1)
    )
    raise ToolError(
        f"{len(pool)} places on screen match {needle!r}, so nothing was clicked:\n{listing}\n"
        f"Call again with occurrence set to the one you mean."
    )


def visible_text_hint(words, limit: int = 40) -> str:
    sample = " ".join(wd.text for wd in words[:limit])
    return sample + (" …" if len(words) > limit else "")


# --- vision fallback --------------------------------------------------------

_BOX_JSON = re.compile(r"\{.*\}", re.S)
_NUMBERS = re.compile(r"-?\d+(?:\.\d+)?")


def parse_box(reply: str) -> tuple[float, float, float, float] | None:
    """Extract Gemini's box_2d = [ymin, xmin, ymax, xmax], normalised to 0-1000.

    Tolerates code fences, surrounding prose and a bare list. Returns None when
    the model says it found nothing or the numbers are not a sane box.
    """
    if not reply:
        return None
    text = reply.strip()
    box: list[float] | None = None
    m = _BOX_JSON.search(text)
    if m:
        try:
            data = json.loads(m.group(0))
            if data.get("found") is False:
                return None
            raw = data.get("box_2d") or data.get("box") or data.get("bbox")
            if isinstance(raw, list) and len(raw) == 4:
                box = [float(v) for v in raw]
        except (ValueError, TypeError, AttributeError):
            box = None
    if box is None:
        nums = [float(n) for n in _NUMBERS.findall(text)]
        if len(nums) == 4:
            box = nums
    if box is None:
        return None
    ymin, xmin, ymax, xmax = box
    if not (0 <= ymin < ymax <= 1000 and 0 <= xmin < xmax <= 1000):
        return None
    return ymin, xmin, ymax, xmax


def box_center(box: tuple[float, float, float, float], rect: tuple[int, int, int, int]) -> tuple[int, int]:
    """Map a normalised box inside a captured rectangle to an absolute screen point."""
    ymin, xmin, ymax, xmax = box
    left, top, right, bottom = rect
    x = left + (xmin + xmax) / 2 / 1000 * (right - left)
    y = top + (ymin + ymax) / 2 / 1000 * (bottom - top)
    return int(round(x)), int(round(y))


LOCATE_PROMPT = (
    "Find this on the screenshot: {description}\n"
    "Reply with JSON only, no prose: "
    '{{"found": true, "box_2d": [ymin, xmin, ymax, xmax]}} '
    "with coordinates normalised to 0-1000, or "
    '{{"found": false}} if it is not visible.'
)


# --- input ------------------------------------------------------------------


def _pyautogui():
    try:
        import pyautogui
    except ImportError as e:
        raise ToolError(f"pyautogui is required for mouse control: {e}") from e
    pyautogui.FAILSAFE = True   # slam the mouse into a corner to abort
    pyautogui.PAUSE = 0.05
    return pyautogui


def _guard_point(x: int, y: int) -> None:
    vl, vt, vr, vb = virtual_screen()
    if not (vl <= x < vr and vt <= y < vb):
        raise ToolError(f"({x}, {y}) is outside the screen ({vl},{vt})-({vr},{vb})")
    if point_is_ours(x, y):
        raise ToolError(
            f"({x}, {y}) is covered by Bantu's own window, so the click would hit Bantu "
            f"rather than the app. Move Bantu's panel or orb out of the way and try again."
        )


def _click(x: int, y: int, button: str, double: bool) -> None:
    pg = _pyautogui()
    if button not in ("left", "right", "middle"):
        raise ToolError("button must be 'left', 'right' or 'middle'")
    _guard_point(x, y)
    try:
        pg.click(x=x, y=y, clicks=2 if double else 1, interval=0.08, button=button)
    except pg.FailSafeException as e:
        raise ToolError("aborted: the mouse was moved into a screen corner") from e


def _type(text: str) -> None:
    import keyboard

    if len(text) > 300:
        _paste(text)
    else:
        # exact=True sends Unicode events, so Devanagari and every keyboard layout work.
        keyboard.write(text, delay=0.004, exact=True)


def _paste(text: str) -> None:
    """Long text goes through the clipboard, which is then put back as it was."""
    import keyboard
    import win32clipboard
    import win32con

    previous = None
    try:
        win32clipboard.OpenClipboard()
        try:
            if win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
                previous = win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
        finally:
            win32clipboard.CloseClipboard()
        keyboard.send("ctrl+v")
        time.sleep(0.25)
    finally:
        if previous is not None:
            try:
                win32clipboard.OpenClipboard()
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, previous)
                win32clipboard.CloseClipboard()
            except Exception:
                pass


def validate_keys(keys: str) -> str:
    import keyboard

    combo = keys.strip().lower()
    if not combo:
        raise ToolError("no keys given")
    try:
        keyboard.parse_hotkey(combo)
    except (ValueError, KeyError) as e:
        raise ToolError(f"{keys!r} is not a key combination keyboard understands: {e}") from e
    return combo


def _target_and_activate(window: str) -> Window:
    w = resolve_window(window)
    if w is None:
        raise ToolError(
            "no target window: Bantu does not know which app you mean. Pass `window` with part "
            "of its title (list_windows shows them)."
        )
    if not activate(w):
        raise ToolError(f"could not bring {w.title!r} to the front, so nothing was sent to it")
    return w


# --- tools ------------------------------------------------------------------


def register(reg: ToolRegistry, router: Any = None, settings: Any = None) -> None:
    reg.describe_category("gui", "operate any app with mouse and keyboard: click buttons by their text, type, press shortcuts, scroll, wait for text, find icons")

    _tracker.start()

    @reg.register(tier=Tier.CONFIRM, category="gui")
    def click_text(
        text: str,
        window: str = "",
        occurrence: int = 0,
        button: str = "left",
        double_click: bool = False,
    ) -> str:
        """Click a button, link, tab or label by the words written on it.

        Reads the screen locally (free, no quota). Pass `window` with part of
        the app's title whenever you know it, so the right app is brought
        forward and searched. If several places match, nothing is clicked and
        the candidates are listed — then call again with `occurrence`.
        For icons with no text, use locate_on_screen then click_at.

        Args:
            text: The visible words to click, e.g. 'Save' or 'Sign in'.
            window: Part of the target window's title. Empty means the app the user was last in.
            occurrence: Which match to click when there are several, counting from 1.
            button: 'left', 'right' or 'middle'.
            double_click: Double-click instead of a single click.
        """
        if not text.strip():
            raise ToolError("nothing to click: text is empty")
        w = _target_and_activate(window) if window or resolve_window() else None
        _step_aside()
        time.sleep(0.15)  # let a just-activated window finish repainting
        passes = screen_passes(w)
        match, tier, _ = choose_match(passes, text, occurrence)
        where = f" in {w.title!r}" if w else ""
        if match is None:
            return (
                f"Error: {text!r} is not visible{where}. Text that is visible: "
                f"{visible_text_hint(passes[0])}"
            )
        x, y = match.center
        _click(x, y, button, double_click)
        note = f" (closest reading of {text!r})" if tier == "fuzzy" else ""
        return f"Clicked {match.text!r}{note} at ({x}, {y}){where}."

    @reg.register(tier=Tier.CONFIRM, category="gui")
    def click_at(x: int, y: int, button: str = "left", double_click: bool = False) -> str:
        """Click an exact screen position. Use coordinates from locate_on_screen or find_on_screen.

        Args:
            x: Horizontal pixel position on screen.
            y: Vertical pixel position on screen.
            button: 'left', 'right' or 'middle'.
            double_click: Double-click instead of a single click.
        """
        _step_aside()
        _click(int(x), int(y), button, double_click)
        return f"Clicked at ({x}, {y})."

    @reg.register(tier=Tier.CONFIRM, category="gui")
    def type_text(text: str, window: str = "", press_enter: bool = False) -> str:
        """Type text into the focused field of an app, as if on the keyboard.

        Works for any language, including Hindi and Nepali. Click the field
        first if it is not already focused. Text typed into a terminal is
        checked against the same blocked commands as run_powershell.

        Args:
            text: What to type.
            window: Part of the target window's title. Empty means the app the user was last in.
            press_enter: Press Enter afterwards.
        """
        if not text:
            raise ToolError("nothing to type")
        w = _target_and_activate(window)
        if w.is_terminal:
            from .shell import check_command

            check_command(text)
        _step_aside()
        if not activate(w):
            raise ToolError(f"{w.title!r} lost focus, so nothing was typed")
        _type(text)
        if press_enter:
            import keyboard

            keyboard.send("enter")
        return f"Typed {len(text)} character(s) into {w.title!r}{' and pressed Enter' if press_enter else ''}."

    @reg.register(tier=Tier.CONFIRM, category="gui")
    def press_keys(keys: str, window: str = "") -> str:
        """Press a key or shortcut in an app, e.g. 'ctrl+s', 'enter', 'alt+f4', 'tab'.

        Args:
            keys: The combination, joined with '+'. Several steps can be comma-separated: 'ctrl+a, delete'.
            window: Part of the target window's title. Empty means the app the user was last in.
        """
        import keyboard

        combo = validate_keys(keys)
        w = _target_and_activate(window)
        _step_aside()
        if not activate(w):
            raise ToolError(f"{w.title!r} lost focus, so no keys were sent")
        keyboard.send(combo)
        return f"Pressed {combo} in {w.title!r}."

    @reg.register(tier=Tier.AUTO, category="gui")
    def scroll(direction: str = "down", notches: int = 5, window: str = "") -> str:
        """Scroll the app under the mouse, or a named window.

        Args:
            direction: 'up' or 'down'.
            notches: How far, in mouse-wheel notches.
            window: Part of the target window's title. Empty scrolls wherever the mouse is.
        """
        if direction not in ("up", "down"):
            raise ToolError("direction must be 'up' or 'down'")
        pg = _pyautogui()
        n = max(1, min(int(notches), 50))
        if window:
            w = _target_and_activate(window)
            x = (w.rect[0] + w.rect[2]) // 2
            y = (w.rect[1] + w.rect[3]) // 2
            _guard_point(x, y)
            pg.moveTo(x, y)
        try:
            pg.scroll(n * WHEEL_DELTA * (1 if direction == "up" else -1))
        except pg.FailSafeException as e:
            raise ToolError("aborted: the mouse was moved into a screen corner") from e
        return f"Scrolled {direction} {n} notch(es)."

    @reg.register(tier=Tier.AUTO, category="gui")
    def wait_for_text(text: str, timeout_seconds: int = 10, window: str = "") -> str:
        """Wait until some text appears on screen — e.g. after clicking, for a dialog to open.

        Args:
            text: The words to wait for.
            timeout_seconds: Give up after this long.
            window: Only look inside this window.
        """
        if not text.strip():
            raise ToolError("nothing to wait for")
        limit = max(1, min(int(timeout_seconds), 120))
        w = resolve_window(window) if window else None
        _step_aside()
        deadline = time.time() + limit
        words: list = []
        while True:
            # A fast single pass first; the slower upscaled pass only when it misses.
            words = screen_words(w)
            exact, partial, fuzzy = rank_matches(words, text)
            hits = exact or partial
            if not hits:
                exact, partial, fuzzy = rank_matches(screen_passes(w, (3,)), text)
                hits = exact or partial or fuzzy
            if hits:
                h = hits[0]
                return f"{text!r} is visible at ({h.center[0]}, {h.center[1]})."
            if time.time() >= deadline:
                break
            time.sleep(0.6)
        return f"{text!r} did not appear within {limit}s. Visible text: {visible_text_hint(words, 25)}"

    @reg.register(tier=Tier.AUTO, category="gui")
    def locate_on_screen(description: str, window: str = "") -> str:
        """Find something with no text on it — an icon, a slider, a picture — and return its position.

        COSTS A SCARCE VISION REQUEST. Use click_text for anything that has
        words on it. Pass the returned coordinates to click_at.

        Args:
            description: What to find, e.g. 'the gear-shaped settings icon'.
            window: Only look inside this window.
        """
        if router is None:
            raise ToolError("vision is not configured")
        from PIL import ImageGrab

        from core.providers.base import Image, Message

        w = resolve_window(window) if window else None
        if w:
            activate(w)
        _step_aside()
        time.sleep(0.15)
        rect = _clip(w.rect) if w else virtual_screen()
        img = ImageGrab.grab(bbox=rect, all_screens=True)
        if max(img.size) > 1600:
            ratio = 1600 / max(img.size)
            img = img.resize((int(img.width * ratio), int(img.height * ratio)))
        import io

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        try:
            resp = router.chat(
                [Message.user(LOCATE_PROMPT.format(description=description),
                              [Image(buf.getvalue(), "image/png")])],
                system="You locate interface elements precisely and reply with JSON only.",
                max_output_tokens=512,
                needs_vision=True,
            )
        except Exception as e:
            raise ToolError(f"vision lookup failed: {e}") from e
        box = parse_box(resp.text)
        if box is None:
            return f"{description!r} was not found on screen."
        x, y = box_center(box, rect)
        return f"Found {description!r} at ({x}, {y}). Pass these to click_at to click it."
