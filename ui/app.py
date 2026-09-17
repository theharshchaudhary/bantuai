"""Wiring for the HUD: worker thread, tray, global hotkey.

The agent runs on a worker thread so a thirty-second task never freezes the
orb. Confirmations originate on that thread but must be answered on the UI
thread, so the worker blocks on an Event while the panel shows the prompt — the
agent is genuinely paused, not racing ahead on an assumption. A prompt the user
has not answered never times out into a yes.
"""

from __future__ import annotations

import datetime
import logging
import math
import threading
import time
from typing import Any

from PyQt5.QtCore import QObject, QPoint, QThread, QTimer, Qt, pyqtSignal
from PyQt5.QtGui import QIcon
from PyQt5.QtWidgets import QAction, QApplication, QMenu, QSystemTrayIcon

from core.agent import Event
from core.tools.registry import Tool

from .widgets import ASSETS, ChatPanel, Orb, State

log = logging.getLogger("bantu.ui")

#: Opening Bantu this soon after the last message continues that chat. After a
#: longer gap it starts fresh; the earlier chat stays one click away in History.
RESUME_WITHIN_S = 6 * 3600
#: How much of an earlier chat the panel shows when it is reopened.
TRANSCRIPT_MESSAGES = 60


def relative_time(ts: float, now: float | None = None) -> str:
    """'just now', '5 min ago', '3 h ago', 'yesterday', or a date."""
    now = time.time() if now is None else now
    gap = max(0.0, now - ts)
    if gap < 60:
        return "just now"
    if gap < 3600:
        return f"{int(gap // 60)} min ago"
    then, today = datetime.datetime.fromtimestamp(ts), datetime.datetime.fromtimestamp(now)
    if then.date() == today.date():
        return f"{int(gap // 3600)} h ago"
    if (today.date() - then.date()).days == 1:
        return "yesterday"
    return f"{then.day} {then:%b}"


def chat_title(text: str, limit: int = 42) -> str:
    one_line = " ".join((text or "").split())
    return one_line if len(one_line) <= limit else one_line[: limit - 1].rstrip() + "…"


def hotkey_label(combo: str) -> str:
    return "+".join(part.capitalize() for part in combo.split("+"))


class Worker(QObject):
    """Runs one agent request at a time, off the UI thread."""

    # Never name a signal `event` or an attribute `thread`: both shadow virtual
    # QObject methods Qt calls internally, and the result is a native crash
    # (moveToThread delivers a ThreadChange event straight into event()).
    agent_event = pyqtSignal(object)    # core.agent.Event
    finished = pyqtSignal(str)
    confirm_needed = pyqtSignal(str, dict)
    heard = pyqtSignal(str)
    listening = pyqtSignal()

    def __init__(self, agent, listener=None, speaker=None):
        super().__init__()
        self.agent = agent
        self.listener = listener
        self.speaker = speaker
        self._answer: str | None = None
        self._answered = threading.Event()
        self._closing = False

        agent.on_event = self.agent_event.emit
        agent.confirm = self._confirm

    # --- confirmation across threads ----------------------------------------

    def _confirm(self, tool: Tool, args: dict[str, Any]) -> bool:
        """Called on the worker thread; answered on the UI thread."""
        if self._closing:
            return False
        self._answered.clear()
        self._answer = None
        self.confirm_needed.emit(tool.name, dict(args))
        self._answered.wait()
        if self._answer == "all":
            self.agent.registry.allow_for_task(tool.name)
            return True
        return self._answer == "yes"

    def answer(self, value: str) -> None:
        self._answer = value
        self._answered.set()

    def close(self) -> None:
        """Release a pending confirmation as a refusal so the thread can exit."""
        self._closing = True
        self.answer("no")

    # --- work ---------------------------------------------------------------

    def run_text(self, text: str) -> None:
        try:
            result = self.agent.run(text)
            self.finished.emit(result.text)
        except Exception as e:
            log.exception("agent run failed")
            self.finished.emit(f"Something went wrong: {type(e).__name__}: {e}")

    def listen_and_run(self) -> None:
        if self.listener is None:
            self.finished.emit("Voice input is unavailable on this machine.")
            return
        if self.speaker:
            self.speaker.stop()          # barge-in: never talk over the user
        try:
            self.listening.emit()
            text = self.listener.listen()
        except Exception as e:
            self.finished.emit(f"Microphone problem: {e}")
            return
        if not text.strip():
            self.finished.emit("")
            return
        self.heard.emit(text)
        self.run_text(text)


class BantuApp(QObject):
    """Owns the orb, the panel, the tray and the worker."""

    _run_text = pyqtSignal(str)
    _run_voice = pyqtSignal()
    _hotkey_pressed = pyqtSignal()
    _step_aside_signal = pyqtSignal()
    announced = pyqtSignal(str, str)

    def __init__(
        self,
        agent,
        settings,
        listener=None,
        speaker=None,
        install_hotkey: bool = True,
        show_tray: bool = True,
        services: Any = None,
    ):
        super().__init__()
        self.services = services
        self._hotkey_enabled = install_hotkey
        self._settings_dialog = None
        self.agent = agent
        self.settings = settings
        self.speaker = speaker
        self.busy = False
        self.hotkey = getattr(settings, "hotkey", "ctrl+alt+space")
        self.hotkey_installed = False

        self.orb = Orb()
        self.panel = ChatPanel(hotkey_label(self.hotkey))
        self._place_orb()

        self.agent_thread = QThread()
        self.agent_thread.setObjectName("bantu-agent")
        self.worker = Worker(agent, listener, speaker)
        self.worker.moveToThread(self.agent_thread)
        self.agent_thread.start()

        # Queued across threads: the worker's slots run on its own thread.
        self._run_text.connect(self.worker.run_text)
        self._run_voice.connect(self.worker.listen_and_run)

        self.worker.agent_event.connect(self._on_event)
        self.worker.finished.connect(self._on_finished)
        self.worker.confirm_needed.connect(self._on_confirm)
        self.worker.heard.connect(lambda t: self.panel.add_message(t, "user"))
        self.worker.listening.connect(lambda: self._set_state(State.LISTENING, "listening…"))

        self.orb.clicked.connect(self.toggle_panel)
        self.orb.dragged.connect(self._reposition_panel)
        self.panel.submitted.connect(self.ask)
        self.panel.listen_requested.connect(self.listen)
        self.panel.closed.connect(self.panel.hide)
        self.panel.settings_requested.connect(self.open_settings)
        self.panel.new_chat_requested.connect(self.new_chat)
        self.panel.history_requested.connect(self.show_history)
        self._history_menu: QMenu | None = None
        self._hotkey_pressed.connect(self.listen)
        self.announced.connect(self._on_announced)

        # A free-tier limit is waited out rather than spending Gemini's vision
        # budget; count it down so a pause never looks like a freeze.
        self._wait_until = 0.0
        self._wait_timer = QTimer(self)
        self._wait_timer.setInterval(250)
        self._wait_timer.timeout.connect(self._tick_wait)

        # GUI tools run on the worker thread and must never screenshot or click
        # Bantu's own panel. A blocking queued connection makes the worker wait
        # until the UI thread has really hidden it, so no capture races the hide.
        self._stepped_aside = False
        self._step_aside_signal.connect(self._hide_for_gui, Qt.BlockingQueuedConnection)
        try:
            from platform_desktop import gui as _gui

            _gui.add_before_action(self._step_aside)
            self._gui = _gui
        except Exception:
            self._gui = None

        self._chips: list[Any] = []
        self._tray = self._build_tray() if show_tray else None
        if install_hotkey:
            self._install_hotkey()

        self.orb.show()
        if not self._resume_recent_chat():
            self._greet(f"{settings.assistant_name} is ready.")

    # --- chats --------------------------------------------------------------

    def _greet(self, opening: str) -> None:
        hint = f"Press {hotkey_label(self.hotkey)} anywhere to speak, or type below." \
            if self.hotkey_installed else "Type below, or use the microphone button."
        self.panel.add_message(f"{opening} {hint}", "assistant")

    def _resume_recent_chat(self) -> bool:
        """Continue the last chat if it was recent. True if one was reopened."""
        memory = getattr(self.agent, "memory", None)
        if memory is None:
            return False
        recent = memory.recent_conversations(1)
        if not recent or time.time() - recent[0]["last"] > RESUME_WITHIN_S:
            return False
        if not memory.open_conversation(recent[0]["conversation"]):
            return False
        self._show_transcript()
        self.panel.set_status("continuing your last chat")
        return True

    def _show_transcript(self) -> None:
        self.panel.clear()
        self._chips = []
        for role, text in self.agent.memory.transcript(TRANSCRIPT_MESSAGES):
            self.panel.add_message(text, role)

    def new_chat(self) -> None:
        """Start a fresh conversation. The current one stays in History."""
        if self.busy:
            return
        if self.speaker:
            self.speaker.stop()
        self.agent.memory.new_conversation()
        self.panel.clear()
        self._chips = []
        self._greet("New chat.")
        self.panel.set_status("new chat")

    def open_chat(self, conversation: str) -> None:
        """Switch the panel, and the agent's context, to an earlier conversation."""
        memory = self.agent.memory
        if self.busy or conversation == memory.conversation:
            return
        if not memory.open_conversation(conversation):
            return
        if self.speaker:
            self.speaker.stop()
        self._show_transcript()
        self.panel.set_status("earlier chat")

    def history_menu(self) -> QMenu:
        """The recent chats, newest first, with the open one ticked."""
        memory = self.agent.memory
        menu = QMenu(self.panel)
        menu.setStyleSheet(
            "QMenu{background:#141B1F;color:#E6EDF0;border:1px solid #222E35;padding:4px;}"
            "QMenu::item{padding:6px 14px;border-radius:5px;}"
            "QMenu::item:selected{background:#1B242A;}"
            "QMenu::item:disabled{color:#97A5AF;}"
        )
        chats = memory.recent_conversations(15)
        if not chats:
            empty = QAction("No earlier chats yet", menu)
            empty.setEnabled(False)
            menu.addAction(empty)
        for chat in chats:
            action = QAction(f"{chat_title(chat['first_user'])}   ·   {relative_time(chat['last'])}", menu)
            action.setCheckable(True)
            action.setChecked(chat["conversation"] == memory.conversation)
            action.triggered.connect(lambda _checked=False, cid=chat["conversation"]: self.open_chat(cid))
            menu.addAction(action)
        return menu

    def show_history(self) -> None:
        if self.busy:
            return
        # Kept on self: a menu with no reference is collected while still open.
        self._history_menu = self.history_menu()
        button = self.panel.history
        self._history_menu.popup(button.mapToGlobal(QPoint(0, button.height() + 2)))

    # --- placement ----------------------------------------------------------

    def _place_orb(self) -> None:
        screen = QApplication.primaryScreen().availableGeometry()
        self.orb.move(screen.right() - self.orb.width() - 28,
                      screen.bottom() - self.orb.height() - 28)

    def _reposition_panel(self) -> None:
        if self.panel.isVisible():
            self._show_panel()

    def _show_panel(self) -> None:
        screen = QApplication.primaryScreen().availableGeometry()
        o = self.orb.frameGeometry()
        x = min(max(screen.left() + 8, o.center().x() - self.panel.width() // 2),
                screen.right() - self.panel.width() - 8)
        y = o.top() - self.panel.height() - 10
        if y < screen.top() + 8:
            y = o.bottom() + 10
        self.panel.move(x, y)
        self.panel.show()
        self.panel.raise_()
        self.panel.activateWindow()
        self.panel.entry.setFocus()

    def toggle_panel(self) -> None:
        if self.panel.isVisible():
            self.panel.hide()
        else:
            self._show_panel()

    # --- state --------------------------------------------------------------

    def _set_state(self, state: State, label: str) -> None:
        self.orb.set_state(state)
        self.panel.set_status(label)

    # --- driving ------------------------------------------------------------

    def ask(self, text: str) -> None:
        if not text.strip() or self.busy:
            return
        self.busy = True
        if self.speaker:
            self.speaker.stop()
        self.panel.add_message(text, "user")
        self.panel.set_busy(True)
        self._set_state(State.THINKING, "thinking…")
        self._run_text.emit(text)

    def listen(self) -> None:
        if self.busy:
            return
        self.busy = True
        if not self.panel.isVisible():
            self._show_panel()
        self.panel.set_busy(True)
        self._run_voice.emit()

    # --- agent events (UI thread) -------------------------------------------

    def _on_event(self, ev: Event) -> None:
        if ev.kind != "waiting":
            self._wait_timer.stop()
        if ev.kind == "waiting":
            self._wait_until = time.monotonic() + ev.seconds
            self._tick_wait()
            self._wait_timer.start()
        elif ev.kind == "thinking":
            self._set_state(State.THINKING, "thinking…")
        elif ev.kind == "tool_start":
            self._chips.append(self.panel.add_tool(ev.tool, ev.arguments))
            self.panel.set_status(f"running {ev.tool}…")
        elif ev.kind == "tool_end":
            chip = self._pending_chip(ev.tool)
            if chip is not None:
                chip.set_result(ev.result, ok=not (ev.result or "").startswith("Error"))
        elif ev.kind == "declined":
            chip = self._pending_chip(ev.tool)
            if chip is not None:
                chip.set_result("declined by you", ok=False)
        elif ev.kind == "error":
            self._set_state(State.ERROR, "failed")

    def _tick_wait(self) -> None:
        left = math.ceil(self._wait_until - time.monotonic())
        if left <= 0:
            self._wait_timer.stop()
            self.panel.set_status("thinking…")
        else:
            self.panel.set_status(f"free limit reached · trying again in {left}s")

    def _pending_chip(self, tool: str):
        """Oldest chip for this tool still awaiting a result."""
        for chip in self._chips:
            if chip.name == tool and chip.result is None:
                return chip
        return None

    def _on_confirm(self, tool_name: str, args: dict) -> None:
        if not self.panel.isVisible():
            self._show_panel()
        self._set_state(State.IDLE, "waiting for you")
        bar = self.panel.add_confirm(tool_name, args)

        def answered(value: str) -> None:
            bar.setEnabled(False)
            bar.hide()
            self._set_state(State.THINKING, "thinking…")
            self.worker.answer(value)

        bar.answered.connect(answered)
        # Deferred one tick: the bar is not laid out yet, and Qt silently skips
        # focusing an unshown widget - focus fell to the panel close button.
        # Reject is the safe choice, so it is the one a stray Enter should hit.
        QTimer.singleShot(0, bar.buttons["no"].setFocus)

    def _step_aside(self) -> None:
        """Called by GUI tools on whatever thread they run on."""
        if QThread.currentThread() == self.thread():
            # Already on the UI thread: a blocking connection to ourselves would deadlock.
            self._hide_for_gui()
        else:
            self._step_aside_signal.emit()

    def _hide_for_gui(self) -> None:
        if self.panel.isVisible():
            self.panel.hide()
            self._stepped_aside = True

    def _on_finished(self, text: str) -> None:
        self.busy = False
        self._wait_timer.stop()
        if self._stepped_aside:
            # Come back once the work on screen is done, to show the result.
            self._stepped_aside = False
            self._show_panel()
        self._chips = [c for c in self._chips if c.result is None]
        self.panel.set_busy(False)
        if text:
            self.panel.add_message(text, "assistant")
            if self.speaker and getattr(self.settings, "voice_enabled", True):
                self._set_state(State.SPEAKING, "speaking…")
                self.speaker.speak(text)
                QTimer.singleShot(400, self._settle)
                return
        elif self.orb.state is State.LISTENING:
            self.panel.set_status("nothing heard")
        self._settle()

    def _settle(self) -> None:
        if self.speaker and self.speaker.is_speaking():
            QTimer.singleShot(300, self._settle)
            return
        if self.orb.state is not State.ERROR:
            self._set_state(State.IDLE, "ready")

    def _on_announced(self, title: str, body: str) -> None:
        self.panel.add_message(f"⏰ {body}", "assistant")
        if self._tray is not None:
            self._tray.showMessage(title, body, QSystemTrayIcon.Information, 8000)
        if self.speaker and getattr(self.settings, "voice_enabled", True) and not self.busy:
            self.speaker.speak(body)

    # --- tray ---------------------------------------------------------------

    def _build_tray(self) -> QSystemTrayIcon | None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            log.warning("no system tray on this desktop")
            return None
        icon_path = ASSETS / "Home.png"
        tray = QSystemTrayIcon(QIcon(str(icon_path)) if icon_path.exists() else QIcon(), self)
        tray.setToolTip("Bantu")
        menu = QMenu()

        show = QAction("Show / hide", menu)
        show.triggered.connect(self.toggle_panel)
        menu.addAction(show)

        speak = QAction("Speak now", menu)
        speak.triggered.connect(self.listen)
        menu.addAction(speak)

        self._voice_action = QAction("Spoken replies", menu)
        self._voice_action.setCheckable(True)
        self._voice_action.setChecked(getattr(self.settings, "voice_enabled", True))
        self._voice_action.triggered.connect(self._toggle_voice)
        menu.addAction(self._voice_action)

        settings_action = QAction("Settings…", menu)
        settings_action.triggered.connect(self.open_settings)
        menu.addAction(settings_action)

        menu.addSeparator()
        quit_action = QAction("Quit Bantu", menu)
        quit_action.triggered.connect(self.quit)
        menu.addAction(quit_action)

        self._menu = menu  # a tray menu with no Python reference is garbage collected
        tray.setContextMenu(menu)
        tray.activated.connect(
            lambda reason: self.toggle_panel() if reason == QSystemTrayIcon.Trigger else None
        )
        tray.show()
        return tray

    def _toggle_voice(self, checked: bool) -> None:
        self.settings.voice_enabled = checked
        try:
            self.settings.save()
        except Exception:
            pass
        if not checked and self.speaker:
            self.speaker.stop()

    # --- settings -----------------------------------------------------------

    def open_settings(self) -> None:
        from core import config as cfg

        from .settings_dialog import SettingsDialog
        from .setup_parts import real_services

        if self._settings_dialog is not None and self._settings_dialog.isVisible():
            self._settings_dialog.raise_()
            self._settings_dialog.activateWindow()
            return
        dialog = SettingsDialog(
            self.settings,
            self.services or real_services(),
            str(cfg.user_data_dir()),
            self.connection_status,
        )
        dialog.applied.connect(self.apply_settings)
        self._settings_dialog = dialog
        dialog.show()

    def connection_status(self) -> str:
        lines = []
        for st in self.agent.router.status():
            name = st["provider"].capitalize()
            if st["disabled"]:
                state = "key rejected - check it in Keys"
            elif st["cooldown_s"]:
                state = f"resting for {st['cooldown_s']}s after hitting its free limit"
            else:
                state = "ready"
            lines.append(f"{name}: {state} · {st['calls']} request(s) this session")
        return "\n".join(lines) or "No services connected."

    def apply_settings(self, changes: set) -> None:
        """Make saved settings take effect now, without a restart."""
        if "hotkey" in changes:
            self.set_hotkey(self.settings.hotkey)
        if "keys" in changes:
            try:
                from core.providers.router import build_providers

                self.agent.router.replace(build_providers(self.settings))
            except Exception as e:
                self.panel.add_message(f"The new keys could not be used: {e}", "assistant")
        if "mic_device" in changes and self.worker.listener is not None:
            self.worker.listener.device = self.settings.mic_device
            self.worker.listener._threshold = None  # recalibrate for the new microphone
        if "voice_enabled" in changes and not self.settings.voice_enabled and self.speaker:
            self.speaker.stop()
        if changes:
            self.panel.set_status("settings saved")

    def set_hotkey(self, combo: str) -> None:
        if self.hotkey_installed:
            try:
                import keyboard

                keyboard.remove_hotkey(self.hotkey)
            except Exception:
                pass
            self.hotkey_installed = False
        self.hotkey = combo
        if self._hotkey_enabled:
            self._install_hotkey()
        self.panel.mic.setToolTip(f"Speak ({hotkey_label(combo)} anywhere)")

    # --- global hotkey ------------------------------------------------------

    def _install_hotkey(self) -> None:
        """Summon from any app, including fullscreen ones."""
        try:
            import keyboard

            # keyboard calls back on its own thread. Emitting a signal queues the
            # work onto the UI thread instead of touching widgets from there.
            keyboard.add_hotkey(self.hotkey, self._hotkey_pressed.emit)
            self.hotkey_installed = True
            log.info("global hotkey %s installed", self.hotkey)
        except Exception as e:
            log.warning("global hotkey unavailable (%s); use the tray or the orb", e)

    # --- lifecycle ----------------------------------------------------------

    def shutdown(self) -> None:
        if self._settings_dialog is not None:
            self._settings_dialog.close()
        if self.hotkey_installed:
            try:
                import keyboard

                keyboard.remove_hotkey(self.hotkey)
            except Exception:
                pass
        if self._gui is not None:
            self._gui.remove_before_action(self._step_aside)
        self._wait_timer.stop()
        router = getattr(self.agent, "router", None)
        if hasattr(router, "interrupt"):
            router.interrupt()   # a free-limit wait would otherwise outlast the thread join
        self.worker.close()      # a pending confirmation would otherwise hang the thread
        if self.speaker:
            self.speaker.shutdown()
        self.agent_thread.quit()
        self.agent_thread.wait(3000)
        if self._tray is not None:
            self._tray.hide()
        self.orb.close()
        self.panel.close()

    def quit(self) -> None:
        self.shutdown()
        QApplication.quit()
