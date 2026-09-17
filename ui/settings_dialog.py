"""Settings, reachable from the tray icon and the panel's gear.

Saving applies immediately — no restart. What changed is reported through
`applied`, so the app can re-register a new hotkey, reconnect with new keys, or
switch microphone without the dialog knowing how any of that works.
"""

from __future__ import annotations

import os
from typing import Any, Callable

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QCheckBox, QDialog, QHBoxLayout, QLineEdit, QPushButton, QTabWidget,
    QVBoxLayout, QWidget,
)

from core.providers.validate import KEY_PAGES

from .setup_parts import (
    BAD, THEME, KeyField, MicPicker, Services, VoicePicker, label, validate_hotkey,
)


class SettingsDialog(QDialog):
    #: The names of the settings that changed, e.g. {"hotkey", "keys"}.
    applied = pyqtSignal(set)

    def __init__(
        self,
        settings: Any,
        services: Services,
        data_dir: str = "",
        status: Callable[[], str] | None = None,
        parent: QWidget | None = None,
        knowledge: Any = None,
    ):
        super().__init__(parent)
        self.knowledge = knowledge
        self.settings = settings
        self.services = services
        self.data_dir = data_dir
        self.setWindowTitle("Bantu settings")
        self.setStyleSheet(THEME)
        self.setMinimumSize(600, 520)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowContextHelpButtonHint)

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 16)
        root.setSpacing(12)
        self.tabs = QTabWidget()
        self.tabs.addTab(self._general(), "General")
        self.tabs.addTab(self._keys(), "Keys")
        self.tabs.addTab(self._about(status), "About")
        root.addWidget(self.tabs, 1)

        self.error = label("", "hint")
        self.error.setStyleSheet(f"color:{BAD};font-size:12px;")
        root.addWidget(self.error)
        # An error should last until the thing it is about is touched, not linger
        # on every tab after the problem is being fixed.
        for edit in (self.hotkey, self.groq.edit, self.gemini.edit):
            edit.textChanged.connect(lambda _t: self.error.setText(""))

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        self.save_button = QPushButton("Save")
        self.save_button.setObjectName("primary")
        self.save_button.setDefault(True)
        self.save_button.clicked.connect(self.save)
        buttons.addWidget(self.save_button)
        root.addLayout(buttons)

    # --- tabs -------------------------------------------------------------

    def _tab(self) -> tuple[QWidget, QVBoxLayout]:
        page = QWidget()
        page.setObjectName("page")
        lay = QVBoxLayout(page)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(10)
        return page, lay

    def _general(self) -> QWidget:
        page, lay = self._tab()
        lay.addWidget(label("YOUR NAME", "section"))
        self.name = QLineEdit(getattr(self.settings, "username", "") or "")
        self.name.setPlaceholderText("What Bantu calls you")
        lay.addWidget(self.name)

        lay.addSpacing(6)
        lay.addWidget(label("VOICE", "section"))
        self.voice = VoicePicker(self.settings, self.services, lambda: self.name.text())
        lay.addWidget(self.voice)
        self.speak_replies = QCheckBox("Speak replies out loud")
        self.speak_replies.setChecked(getattr(self.settings, "voice_enabled", True))
        lay.addWidget(self.speak_replies)

        lay.addSpacing(6)
        lay.addWidget(label("MICROPHONE", "section"))
        self.mic = MicPicker(self.services, getattr(self.settings, "mic_device", None))
        lay.addWidget(self.mic)

        lay.addSpacing(6)
        lay.addWidget(label("SHORTCUT TO SPEAK", "section"))
        self.hotkey = QLineEdit(getattr(self.settings, "hotkey", "ctrl+alt+space"))
        self.hotkey.setPlaceholderText("e.g. ctrl+alt+space")
        lay.addWidget(self.hotkey)
        lay.addWidget(label("Works from any app. It needs Ctrl, Alt or Windows.", "hint"))
        lay.addStretch(1)
        return page

    def _keys(self) -> QWidget:
        page, lay = self._tab()
        self.groq = KeyField("groq", "Groq", "Conversation, tools and speech recognition.",
                             self.services, KEY_PAGES["groq"])
        self.gemini = KeyField("gemini", "Gemini", "Seeing the screen.",
                               self.services, KEY_PAGES["gemini"])
        for field in (self.groq, self.gemini):
            key, source = self.services.existing_key(field.provider)
            field.prefill(key, source)
            lay.addWidget(field)
        lay.addWidget(label(
            "A changed key is tested before it is saved. Keys live in Windows Credential Manager.",
            "hint"))
        lay.addStretch(1)
        return page

    def _about(self, status: Callable[[], str] | None) -> QWidget:
        page, lay = self._tab()
        lay.addWidget(label("CONNECTIONS", "section"))
        self.status_text = label(status() if status else "Not available.", "lead")
        lay.addWidget(self.status_text)
        lay.addSpacing(6)
        lay.addWidget(label("YOUR DATA", "section"))
        lay.addWidget(label(
            "Conversations, remembered facts, reminders and settings stay on this computer, in:",
            "lead"))
        path = label(self.data_dir or "(unknown)", "hint")
        path.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(path)
        open_folder = QPushButton("Open folder")
        open_folder.setEnabled(bool(self.data_dir) and os.path.isdir(self.data_dir))
        open_folder.clicked.connect(lambda: os.startfile(self.data_dir))  # noqa: S606
        row = QHBoxLayout()
        row.addWidget(open_folder)
        row.addStretch(1)
        lay.addLayout(row)

        if self.knowledge is not None:
            lay.addSpacing(6)
            lay.addWidget(label("KNOWLEDGE FOLDER", "section"))
            kinds = ", ".join(self.knowledge.suffixes)
            lay.addWidget(label(
                f"Drop documents here and {getattr(self.settings, 'assistant_name', 'Bantu')} can answer "
                f"from them ({kinds}).", "lead"))
            where = label(str(self.knowledge.folder), "hint")
            where.setTextInteractionFlags(Qt.TextSelectableByMouse)
            lay.addWidget(where)
            self.knowledge_summary = label(self.knowledge.summary(), "hint")
            lay.addWidget(self.knowledge_summary)
            open_knowledge = QPushButton("Open knowledge folder")
            open_knowledge.clicked.connect(self._open_knowledge)
            row = QHBoxLayout()
            row.addWidget(open_knowledge)
            row.addStretch(1)
            lay.addLayout(row)
        lay.addStretch(1)
        return page

    def _open_knowledge(self) -> None:
        self.knowledge.folder.mkdir(parents=True, exist_ok=True)
        os.startfile(str(self.knowledge.folder))  # noqa: S606

    # --- saving -----------------------------------------------------------

    def _fail(self, message: str, tab: int) -> None:
        self.error.setText(message)
        self.tabs.setCurrentIndex(tab)

    def save(self) -> None:
        ok, message, combo = validate_hotkey(self.hotkey.text())
        if not ok:
            self._fail(message, 0)
            return

        changed_keys = [f for f in (self.groq, self.gemini) if f.changed and f.key]
        unverified = [f for f in changed_keys if not f.verified]
        if unverified:
            names = " and ".join(f.provider.capitalize() for f in unverified)
            self._fail(f"Test the new {names} key before saving.", 1)
            return
        if not any(f.key for f in (self.groq, self.gemini)):
            self._fail("Bantu needs at least one key.", 1)
            return

        s = self.settings
        changes: set[str] = set()

        def update(attr: str, value: Any, tag: str | None = None) -> None:
            if getattr(s, attr, None) != value:
                setattr(s, attr, value)
                changes.add(tag or attr)

        update("username", self.name.text().strip())
        update("voice_gender", self.voice.gender)
        update("voice_enabled", self.speak_replies.isChecked())
        update("mic_device", self.mic.device)
        update("hotkey", combo)

        for field in changed_keys:
            self.services.store_key(field.provider, field.key)
            changes.add("keys")

        s.save()
        self.error.setText("")
        self.applied.emit(changes)
        self.accept()
