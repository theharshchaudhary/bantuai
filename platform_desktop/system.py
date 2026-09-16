"""System, app and window tools.

Launching and reading state is AUTO. Anything that terminates a process or
changes power state asks first, because both can lose unsaved work.
"""

from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

from core.tools.registry import Tier, ToolError, ToolRegistry

from .paths import human_size

#: Media virtual-key codes. Spelled out rather than taken from win32con,
#: which does not define VK_MEDIA_STOP - referencing it crashed every action.
MEDIA_KEYS = {
    "playpause": 0xB3, "play": 0xB3, "pause": 0xB3,
    "next": 0xB0, "previous": 0xB1, "prev": 0xB1,
    "stop": 0xB2,
}
KEYEVENTF_KEYUP = 0x0002

#: Processes that keep the desktop alive. Refuse to kill these regardless.
CRITICAL = {
    "system", "system idle process", "csrss.exe", "wininit.exe", "winlogon.exe",
    "services.exe", "lsass.exe", "smss.exe", "svchost.exe", "dwm.exe",
    "explorer.exe", "ntoskrnl.exe", "registry", "memory compression",
}


def _volume():
    try:
        from pycaw.pycaw import AudioUtilities

        return AudioUtilities.GetSpeakers().EndpointVolume
    except Exception as e:
        raise ToolError(f"audio control unavailable: {type(e).__name__}: {e}") from e


def _win():
    try:
        import win32con, win32gui, win32process

        return win32gui, win32process, win32con
    except ImportError as e:
        raise ToolError(f"window control needs pywin32: {e}") from e


def register(reg: ToolRegistry) -> None:
    # --- apps ---------------------------------------------------------------

    @reg.register(tier=Tier.AUTO, category="system")
    def open_app(name: str) -> str:
        """Launch an installed application by name, e.g. 'spotify' or 'notepad'.

        Args:
            name: The application's name as a person would say it.
        """
        clean = name.strip()
        if not clean:
            raise ToolError("no application named")
        try:
            from AppOpener import open as app_open

            # AppOpener chatters on stdout; a CLI must stay clean.
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                app_open(clean, match_closest=True, throw_error=True, output=False)
            return f"Opened {clean}."
        except Exception:
            # Fall back to the shell, which handles UWP apps and anything on PATH.
            try:
                os.startfile(clean)  # noqa: S606 - a user-named app is the point
                return f"Opened {clean}."
            except Exception as e:
                raise ToolError(
                    f"could not open {clean!r}: {e}. Try the exact executable name, "
                    f"or open_url for a website."
                ) from e

    @reg.register(tier=Tier.CONFIRM, category="system")
    def close_app(name: str, force: bool = False) -> str:
        """Close a running application. Unsaved work may be lost.

        Args:
            name: Process or window name, e.g. 'notepad'.
            force: Kill immediately instead of asking the window to close.
        """
        import psutil

        want = name.lower().replace(".exe", "").strip()
        if not want:
            raise ToolError("no application named")
        if f"{want}.exe" in CRITICAL or want in CRITICAL:
            raise ToolError(f"{name} is a critical system process and will not be closed.")

        killed = []
        for proc in psutil.process_iter(["name", "pid"]):
            pname = (proc.info["name"] or "").lower()
            if want not in pname.replace(".exe", ""):
                continue
            if pname in CRITICAL:
                continue
            try:
                proc.kill() if force else proc.terminate()
                killed.append(f"{proc.info['name']} (pid {proc.info['pid']})")
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        if not killed:
            return f"Nothing running matched {name!r}."
        return f"Closed {len(killed)}: " + ", ".join(killed[:8])

    @reg.register(tier=Tier.AUTO, category="system")
    def list_running_apps(limit: int = 25) -> str:
        """List running applications with visible windows, biggest memory first.

        Args:
            limit: How many to show.
        """
        import psutil

        win32gui, win32process, _ = _win()
        with_windows: set[int] = set()

        def collect(hwnd, _):
            if win32gui.IsWindowVisible(hwnd) and win32gui.GetWindowText(hwnd):
                with_windows.add(win32process.GetWindowThreadProcessId(hwnd)[1])

        win32gui.EnumWindows(collect, None)

        rows = []
        for proc in psutil.process_iter(["pid", "name", "memory_info"]):
            if proc.info["pid"] in with_windows:
                mem = proc.info["memory_info"].rss if proc.info["memory_info"] else 0
                rows.append((mem, proc.info["name"], proc.info["pid"]))
        rows.sort(reverse=True)
        if not rows:
            return "No applications with visible windows."
        return "\n".join(f"  {human_size(m):>8}  {n} (pid {p})" for m, n, p in rows[:limit])

    # --- windows ------------------------------------------------------------

    @reg.register(tier=Tier.AUTO, category="system")
    def list_windows() -> str:
        """List open window titles. Useful before focusing one."""
        win32gui, win32process, _ = _win()
        import psutil

        found = []

        def collect(hwnd, _):
            if not win32gui.IsWindowVisible(hwnd):
                return
            title = win32gui.GetWindowText(hwnd)
            if not title:
                return
            try:
                pid = win32process.GetWindowThreadProcessId(hwnd)[1]
                owner = psutil.Process(pid).name()
            except Exception:
                owner = "?"
            found.append(f"  {title}   [{owner}]")

        win32gui.EnumWindows(collect, None)
        if not found:
            return "No visible windows."
        active = win32gui.GetWindowText(win32gui.GetForegroundWindow())
        return f"Active: {active}\n\nAll windows:\n" + "\n".join(found[:30])

    @reg.register(tier=Tier.AUTO, category="system")
    def focus_window(title: str) -> str:
        """Bring a window to the front by (partial) title.

        Args:
            title: Any part of the window's title, case-insensitive.
        """
        win32gui, _, win32con = _win()
        needle = title.lower().strip()
        if not needle:
            raise ToolError("no title given")
        matches = []

        def collect(hwnd, _):
            if win32gui.IsWindowVisible(hwnd):
                t = win32gui.GetWindowText(hwnd)
                if t and needle in t.lower():
                    matches.append((hwnd, t))

        win32gui.EnumWindows(collect, None)
        if not matches:
            raise ToolError(f"no open window matching {title!r}. Use list_windows to see them.")
        hwnd, found = matches[0]
        try:
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            win32gui.SetForegroundWindow(hwnd)
        except Exception as e:
            raise ToolError(f"could not focus {found!r}: {e}") from e
        return f"Focused {found!r}."

    # --- machine state ------------------------------------------------------

    @reg.register(tier=Tier.AUTO, category="system")
    def system_info() -> str:
        """CPU, memory, disk and battery status for this machine."""
        import psutil

        mem = psutil.virtual_memory()
        disk = psutil.disk_usage(str(Path.home().anchor))
        out = [
            f"CPU      {psutil.cpu_percent(interval=0.3):.0f}% across {psutil.cpu_count()} threads",
            f"Memory   {human_size(mem.used)} of {human_size(mem.total)} used ({mem.percent:.0f}%)",
            f"Disk     {human_size(disk.used)} of {human_size(disk.total)} used ({disk.percent:.0f}%), "
            f"{human_size(disk.free)} free",
        ]
        bat = psutil.sensors_battery()
        if bat:
            plugged = "charging" if bat.power_plugged else "on battery"
            out.append(f"Battery  {bat.percent:.0f}% ({plugged})")
        up = time.time() - psutil.boot_time()
        out.append(f"Uptime   {int(up // 3600)}h {int(up % 3600 // 60)}m")
        return "\n".join(out)

    # --- clipboard ----------------------------------------------------------

    @reg.register(tier=Tier.AUTO, category="system")
    def get_clipboard() -> str:
        """Read the text currently on the clipboard."""
        import win32clipboard
        import win32con

        try:
            win32clipboard.OpenClipboard()
            try:
                data = win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
            finally:
                win32clipboard.CloseClipboard()
        except Exception:
            return "The clipboard holds no text."
        return data or "The clipboard is empty."

    @reg.register(tier=Tier.AUTO, category="system")
    def set_clipboard(text: str) -> str:
        """Put text on the clipboard so the user can paste it.

        Args:
            text: What to copy.
        """
        import win32clipboard
        import win32con

        try:
            win32clipboard.OpenClipboard()
            try:
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
            finally:
                win32clipboard.CloseClipboard()
        except Exception as e:
            raise ToolError(f"could not set the clipboard: {e}") from e
        return f"Copied {len(text)} characters to the clipboard."

    # --- audio --------------------------------------------------------------

    @reg.register(tier=Tier.AUTO, category="system")
    def set_volume(level: int) -> str:
        """Set the system volume.

        Args:
            level: Percentage from 0 to 100.
        """
        if not 0 <= level <= 100:
            raise ToolError("level must be between 0 and 100")
        ev = _volume()
        ev.SetMasterVolumeLevelScalar(level / 100.0, None)
        return f"Volume set to {level}%."

    @reg.register(tier=Tier.AUTO, category="system")
    def get_volume() -> str:
        """Report the current system volume and mute state."""
        ev = _volume()
        return f"Volume is {ev.GetMasterVolumeLevelScalar() * 100:.0f}%, " \
               f"{'muted' if ev.GetMute() else 'not muted'}."

    @reg.register(tier=Tier.AUTO, category="system")
    def mute(on: bool = True) -> str:
        """Mute or unmute the system audio.

        Args:
            on: True to mute, False to unmute.
        """
        _volume().SetMute(1 if on else 0, None)
        return "Muted." if on else "Unmuted."

    @reg.register(tier=Tier.AUTO, category="system")
    def media_control(action: str) -> str:
        """Control whatever is playing: play/pause, next or previous track.

        Args:
            action: One of 'play', 'pause', 'playpause', 'next', 'previous', 'stop'.
        """
        import win32api

        key = MEDIA_KEYS.get(action.lower().strip())
        if key is None:
            raise ToolError(f"action must be one of: {', '.join(sorted(MEDIA_KEYS))}")
        win32api.keybd_event(key, 0, 0, 0)
        win32api.keybd_event(key, 0, KEYEVENTF_KEYUP, 0)
        return f"Sent {action} to the media player."

    # --- misc ---------------------------------------------------------------

    @reg.register(tier=Tier.AUTO, category="system")
    def notify(title: str, message: str) -> str:
        """Show a desktop notification.

        Args:
            title: Notification heading.
            message: Body text.
        """
        ps = (
            "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications,"
            " ContentType=WindowsRuntime] > $null;"
            "$t=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent(2);"
            f"$t.GetElementsByTagName('text').Item(0).AppendChild($t.CreateTextNode({title!r}))>$null;"
            f"$t.GetElementsByTagName('text').Item(1).AppendChild($t.CreateTextNode({message!r}))>$null;"
            "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('BantuAI')"
            ".Show([Windows.UI.Notifications.ToastNotification]::new($t))"
        )
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                capture_output=True, timeout=15, check=False,
            )
        except Exception as e:
            raise ToolError(f"could not show a notification: {e}") from e
        return f"Notified: {title}"

    @reg.register(tier=Tier.AUTO, category="system")
    def open_url(url: str) -> str:
        """Open a web address in the default browser.

        Args:
            url: The address to open.
        """
        clean = url.strip()
        if not clean.startswith(("http://", "https://")):
            clean = "https://" + clean
        webbrowser.open(clean)
        return f"Opened {clean}"

    @reg.register(tier=Tier.AUTO, category="system")
    def take_screenshot(path: str = "", region: str = "") -> str:
        """Save a screenshot to a file.

        To ask a question about the screen, use read_screen or look_at_screen
        instead — this only saves an image.

        Args:
            path: Where to save the PNG. Defaults to the Pictures folder.
            region: Optional 'left,top,right,bottom' pixel box.
        """
        from PIL import ImageGrab

        box = None
        if region:
            parts = [p.strip() for p in region.split(",")]
            if len(parts) != 4 or not all(p.lstrip("-").isdigit() for p in parts):
                raise ToolError("region must be 'left,top,right,bottom' in pixels")
            box = tuple(int(p) for p in parts)

        if path:
            from .paths import guard_write

            out = guard_write(path)
        else:
            out = Path.home() / "Pictures" / f"bantu_{time.strftime('%Y%m%d_%H%M%S')}.png"
        out.parent.mkdir(parents=True, exist_ok=True)
        ImageGrab.grab(bbox=box, all_screens=True).save(out, format="PNG")
        return f"Saved screenshot to {out}"

    # --- power --------------------------------------------------------------

    @reg.register(tier=Tier.CONFIRM, category="system")
    def lock_screen() -> str:
        """Lock the workstation."""
        import ctypes

        if not ctypes.windll.user32.LockWorkStation():
            raise ToolError("the workstation could not be locked")
        return "Locked."

    @reg.register(tier=Tier.CONFIRM, category="system")
    def power_action(action: str, delay_seconds: int = 0) -> str:
        """Sleep, restart or shut down the machine. Unsaved work may be lost.

        Args:
            action: One of 'sleep', 'restart', 'shutdown'.
            delay_seconds: Wait this long first, giving a chance to cancel.
        """
        act = action.lower().strip()
        if act == "sleep":
            subprocess.run(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"], check=False)
            return "Sleeping."
        if act in ("restart", "reboot"):
            subprocess.run(["shutdown", "/r", "/t", str(max(0, delay_seconds))], check=False)
            return f"Restarting in {delay_seconds}s. Run 'shutdown /a' to cancel."
        if act == "shutdown":
            subprocess.run(["shutdown", "/s", "/t", str(max(0, delay_seconds))], check=False)
            return f"Shutting down in {delay_seconds}s. Run 'shutdown /a' to cancel."
        raise ToolError("action must be 'sleep', 'restart' or 'shutdown'")
