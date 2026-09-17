"""Pieces shared by first-run setup and Settings.

Everything that touches the outside world — checking a key over the network,
storing it in Windows Credential Manager, listing microphones, speaking a
sample — goes through `Services`, so the windows can be tested offscreen with
fakes and never write to the real credential store.

Network and speech work run off the UI thread and report back with a signal:
a key check can take seconds, and a frozen window reads as a crash.
"""

from __future__ import annotations

import dataclasses
import threading
import webbrowser
from dataclasses import dataclass
from typing import Any, Callable

from PyQt5.QtCore import QObject, Qt, QTimer, pyqtSignal
from PyQt5.QtWidgets import (
    QButtonGroup, QComboBox, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QRadioButton, QVBoxLayout, QWidget,
)

from .widgets import ACCENT, INK, INK_SOFT, RULE, SURFACE, SURFACE_2

OK = "#4FBF95"
BAD = "#E8737F"
WARN = "#E0A44A"

THEME = f"""
QDialog, QWidget#page {{ background:{SURFACE}; color:{INK}; }}
QLabel {{ color:{INK}; font-size:13px; }}
QLabel#title {{ font-size:22px; font-weight:600; }}
QLabel#lead {{ color:{INK_SOFT}; font-size:13px; }}
QLabel#hint {{ color:{INK_SOFT}; font-size:12px; }}
QLabel#section {{ color:{INK_SOFT}; font-size:11px; font-weight:600; letter-spacing:1px; }}
QLineEdit, QComboBox {{
    background:{SURFACE_2}; color:{INK}; border:1px solid {RULE};
    border-radius:7px; padding:7px 9px; font-size:13px; selection-background-color:{ACCENT};
}}
QLineEdit:focus, QComboBox:focus {{ border-color:{ACCENT}; }}
QComboBox QAbstractItemView {{ background:{SURFACE_2}; color:{INK}; selection-background-color:#1D3B40; }}
QPushButton {{
    background:{SURFACE_2}; color:{INK}; border:1px solid {RULE};
    border-radius:7px; padding:7px 14px; font-size:13px;
}}
QPushButton:hover {{ border-color:{ACCENT}; }}
QPushButton:focus {{ border-color:{ACCENT}; }}
QPushButton:disabled {{ color:#5B6770; border-color:{RULE}; }}
QPushButton#primary {{ background:{ACCENT}; color:#06181B; border-color:{ACCENT}; font-weight:600; }}
QPushButton#primary:disabled {{ background:#24474B; color:#6E8A8D; border-color:#24474B; }}
QPushButton#link {{ background:transparent; border:none; color:{ACCENT}; padding:0; text-align:left; }}
QPushButton#link:hover {{ text-decoration:underline; }}
QCheckBox, QRadioButton {{ color:{INK}; font-size:13px; spacing:8px; }}
QTabWidget::pane {{ border:1px solid {RULE}; border-radius:8px; top:-1px; }}
QTabBar::tab {{
    background:transparent; color:{INK_SOFT}; padding:8px 16px; border:none;
    border-bottom:2px solid transparent; font-size:13px;
}}
QTabBar::tab:selected {{ color:{INK}; border-bottom:2px solid {ACCENT}; }}
"""


# --- services ---------------------------------------------------------------


@dataclass
class Services:
    """Side effects the setup and settings windows need, injectable for tests."""

    check_key: Callable[[str, str], Any]
    store_key: Callable[[str, str], None]
    existing_key: Callable[[str], tuple[str | None, str | None]]
    list_devices: Callable[[], list[tuple[int, str]]]
    preview_voice: Callable[[Any, str, str], None]
    open_url: Callable[[str], None] = webbrowser.open


def real_services() -> Services:
    from core import config as cfg
    from core.providers.validate import check_key

    def existing(name: str) -> tuple[str | None, str | None]:
        return cfg.get_key(name), cfg.key_source(name)

    def devices() -> list[tuple[int, str]]:
        try:
            from voice import list_devices

            return list_devices()
        except Exception:
            return []

    def preview(settings: Any, gender: str, text: str) -> None:
        from voice import Speaker

        trial = dataclasses.replace(settings, voice_gender=gender, voice_enabled=True)
        Speaker(trial).speak(text, lang="en", blocking=True)

    return Services(check_key, cfg.set_key, existing, devices, preview)


class _Relay(QObject):
    """Carries a background result onto the UI thread."""

    done = pyqtSignal(object)


def run_in_background(work: Callable[[], Any], on_done: Callable[[Any], None]) -> None:
    relay = _Relay()
    relay.done.connect(on_done)
    relay._keep = relay  # the thread holds the only other reference

    def body() -> None:
        try:
            result = work()
        except Exception as e:  # surface, never swallow
            result = e
        relay.done.emit(result)

    threading.Thread(target=body, daemon=True).start()


# --- hotkeys ----------------------------------------------------------------

MODIFIERS = {"ctrl", "alt", "windows", "win", "left windows", "right windows"}


def validate_hotkey(text: str) -> tuple[bool, str, str]:
    """(ok, message, normalised). A global hotkey needs Ctrl, Alt or Windows.

    Shift alone is not enough: Shift+A is how people type a capital A, and a
    global hook on it would swallow ordinary typing everywhere.
    """
    combo = "+".join(part.strip().lower() for part in (text or "").split("+") if part.strip())
    if not combo:
        return False, "Enter a shortcut, for example Ctrl+Alt+Space.", ""
    parts = set(combo.split("+"))
    if not parts & MODIFIERS:
        return False, "Include Ctrl, Alt or Windows, or the shortcut would fire while you type.", combo
    if parts <= MODIFIERS | {"shift"}:
        return False, "Add a key to go with the modifiers.", combo
    try:
        import keyboard

        keyboard.parse_hotkey(combo)
    except (ValueError, KeyError, ImportError) as e:
        return False, f"That is not a key combination Windows recognises ({e}).", combo
    if combo in ("ctrl+space", "ctrl+c", "ctrl+v", "ctrl+x", "ctrl+z", "ctrl+s", "alt+tab", "alt+f4"):
        return False, "That shortcut is already used everywhere. Pick something less common.", combo
    return True, "", combo


# --- widgets ----------------------------------------------------------------


def label(text: str, name: str = "", wrap: bool = True) -> QLabel:
    lab = QLabel(text)
    if name:
        lab.setObjectName(name)
    lab.setWordWrap(wrap)
    lab.setTextFormat(Qt.RichText if "<" in text else Qt.PlainText)
    return lab


class KeyField(QWidget):
    """Paste a key, test it against the real service, see why it failed."""

    verified_changed = pyqtSignal(bool)

    def __init__(self, provider: str, title: str, purpose: str, services: Services,
                 key_page: str, required_note: str = ""):
        super().__init__()
        self.provider = provider
        self.services = services
        self._verified_key: str | None = None
        self._stored_key: str | None = None
        self._checking = False

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(6)

        head = QHBoxLayout()
        head.addWidget(label(f"<b>{title}</b>", wrap=False))
        if required_note:
            head.addWidget(label(required_note, "hint", wrap=False))
        head.addStretch(1)
        get = QPushButton("Get a free key ↗")
        get.setObjectName("link")
        get.setCursor(Qt.PointingHandCursor)
        get.clicked.connect(lambda: services.open_url(key_page))
        head.addWidget(get)
        root.addLayout(head)
        root.addWidget(label(purpose, "hint"))

        row = QHBoxLayout()
        row.setSpacing(6)
        self.edit = QLineEdit()
        self.edit.setEchoMode(QLineEdit.Password)
        self.edit.setPlaceholderText("Paste your key here")
        self.edit.textChanged.connect(self._edited)
        self.edit.returnPressed.connect(self.test)
        row.addWidget(self.edit, 1)
        self.reveal = QPushButton("Show")
        self.reveal.setCheckable(True)
        self.reveal.setFixedWidth(64)
        self.reveal.toggled.connect(
            lambda on: (self.edit.setEchoMode(QLineEdit.Normal if on else QLineEdit.Password),
                        self.reveal.setText("Hide" if on else "Show"))
        )
        row.addWidget(self.reveal)
        self.test_button = QPushButton("Test")
        self.test_button.setFixedWidth(72)
        self.test_button.clicked.connect(self.test)
        row.addWidget(self.test_button)
        root.addLayout(row)

        self.status = label("", "hint")
        self.status.setMinimumHeight(18)
        root.addWidget(self.status)

    # --- state ------------------------------------------------------------

    @property
    def key(self) -> str:
        return self.edit.text().strip()

    @property
    def verified(self) -> bool:
        return bool(self.key) and self._verified_key == self.key

    @property
    def changed(self) -> bool:
        """True when the field no longer holds the key that is already stored."""
        return self.key != (self._stored_key or "")

    def prefill(self, key: str | None, source: str | None) -> None:
        if not key:
            return
        self._stored_key = key if source == "keyring" else None
        self.edit.blockSignals(True)
        self.edit.setText(key)
        self.edit.blockSignals(False)
        if source == "keyring":
            self._set_status("Saved in Windows Credential Manager.", INK_SOFT)
        else:
            self._set_status(
                f"Found in {source}. Checking it, then it moves into Windows Credential Manager.",
                WARN,
            )
            # A key the person already has should not need a click to prove itself.
            QTimer.singleShot(0, self.test)

    def _set_status(self, text: str, colour: str) -> None:
        self.status.setText(text)
        self.status.setStyleSheet(f"color:{colour};font-size:12px;")

    def _edited(self, _text: str) -> None:
        was = self._verified_key is not None
        self._verified_key = None
        self._set_status("", INK_SOFT)
        if was:
            self.verified_changed.emit(False)

    # --- testing ----------------------------------------------------------

    def test(self) -> None:
        if self._checking:
            return
        key = self.key
        if not key:
            self._set_status("Paste a key first.", BAD)
            return
        self._checking = True
        self.test_button.setEnabled(False)
        self._set_status("Checking…", INK_SOFT)

        def finished(result: Any) -> None:
            self._checking = False
            self.test_button.setEnabled(True)
            if key != self.key:  # edited while the check was running
                return
            if isinstance(result, Exception):
                self._set_status(f"Couldn't check the key: {result}", BAD)
                return
            if result.ok:
                self._verified_key = key
                self._set_status(f"✓ {result.message}", OK)
                self.verified_changed.emit(True)
            else:
                self._set_status(f"✗ {result.message}", BAD if result.reachable else WARN)
                self.verified_changed.emit(False)

        run_in_background(lambda: self.services.check_key(self.provider, key), finished)


class VoicePicker(QWidget):
    """Female or male voice, with a sample you can hear before choosing."""

    def __init__(self, settings: Any, services: Services, name_source: Callable[[], str]):
        super().__init__()
        self.settings = settings
        self.services = services
        self.name_source = name_source

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(14)
        self.group = QButtonGroup(self)
        self.female = QRadioButton("Female")
        self.male = QRadioButton("Male")
        for i, b in enumerate((self.female, self.male)):
            self.group.addButton(b, i)
            row.addWidget(b)
        (self.male if getattr(settings, "voice_gender", "female") == "male" else self.female).setChecked(True)
        row.addStretch(1)
        self.play = QPushButton("▶ Play sample")
        self.play.clicked.connect(self.preview)
        row.addWidget(self.play)

    @property
    def gender(self) -> str:
        return "male" if self.male.isChecked() else "female"

    def sample_text(self) -> str:
        name = (self.name_source() or "").strip()
        greeting = f"Hi {name}." if name else "Hi."
        return f"{greeting} I'm Bantu. Press Control, Alt and Space whenever you need me."

    def preview(self) -> None:
        self.play.setEnabled(False)
        self.play.setText("Playing…")
        gender, text = self.gender, self.sample_text()

        def done(_result: Any) -> None:
            self.play.setEnabled(True)
            self.play.setText("▶ Play sample")

        run_in_background(lambda: self.services.preview_voice(self.settings, gender, text), done)


class MicPicker(QComboBox):
    """System default, or a specific microphone."""

    def __init__(self, services: Services, current: int | None):
        super().__init__()
        self.addItem("System default", None)
        for index, name in services.list_devices():
            self.addItem(name, index)
        pos = self.findData(current) if current is not None else 0
        self.setCurrentIndex(max(0, pos))

    @property
    def device(self) -> int | None:
        return self.currentData()
