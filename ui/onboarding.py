"""First-run setup: name, keys, voice. Nothing is saved until Finish.

Keys are tested against the real services before the window will let you
continue, so a mistyped key is caught while the person is still looking at it,
not as a failure the first time they ask for something.
"""

from __future__ import annotations

from typing import Any

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import (
    QCheckBox, QDialog, QHBoxLayout, QLineEdit, QPushButton, QStackedWidget,
    QVBoxLayout, QWidget,
)

from core.providers.validate import KEY_PAGES

from .setup_parts import THEME, KeyField, MicPicker, Services, VoicePicker, label

STEPS = ("Welcome", "Keys", "Voice", "Ready")


class SetupWizard(QDialog):
    def __init__(self, settings: Any, services: Services, parent: QWidget | None = None):
        super().__init__(parent)
        self.settings = settings
        self.services = services
        self.stored: list[str] = []
        self.setWindowTitle("Set up Bantu")
        self.setStyleSheet(THEME)
        self.setMinimumSize(600, 560)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowContextHelpButtonHint)

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 22, 28, 20)
        root.setSpacing(14)

        self.progress = label("", "section", wrap=False)
        root.addWidget(self.progress)

        self.stack = QStackedWidget()
        self.stack.addWidget(self._welcome())
        self.stack.addWidget(self._keys())
        self.stack.addWidget(self._voice())
        self.stack.addWidget(self._ready())
        root.addWidget(self.stack, 1)

        nav = QHBoxLayout()
        self.back = QPushButton("Back")
        self.back.clicked.connect(lambda: self.go(self.stack.currentIndex() - 1))
        nav.addWidget(self.back)
        nav.addStretch(1)
        self.next = QPushButton("Next")
        self.next.setObjectName("primary")
        self.next.setDefault(True)
        self.next.clicked.connect(self._advance)
        nav.addWidget(self.next)
        root.addLayout(nav)

        self.go(0)
        QTimer.singleShot(0, self.name.setFocus)

    # --- pages ------------------------------------------------------------

    def _page(self) -> tuple[QWidget, QVBoxLayout]:
        page = QWidget()
        page.setObjectName("page")
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)
        return page, lay

    def _welcome(self) -> QWidget:
        page, lay = self._page()
        lay.addWidget(label("Hi, I'm Bantu.", "title"))
        lay.addWidget(label(
            "I work on your computer: files, apps, the web, and anything on your screen. "
            "Ask by typing or speaking, in English, Hindi or Nepali.", "lead"))
        lay.addWidget(label(
            "I run on free AI services using your own keys. Nothing is sent anywhere except "
            "the requests you make, and I ask before doing anything that changes your files, "
            "apps or accounts.", "lead"))
        lay.addSpacing(8)
        lay.addWidget(label("What should I call you?"))
        self.name = QLineEdit(getattr(self.settings, "username", "") or "")
        self.name.setPlaceholderText("Your first name")
        self.name.returnPressed.connect(self._advance)
        lay.addWidget(self.name)
        lay.addStretch(1)
        return page

    def _keys(self) -> QWidget:
        page, lay = self._page()
        lay.addWidget(label("Connect the free AI services", "title"))
        lay.addWidget(label(
            "Both are free and need no card. Groq does the thinking and hears your voice; Gemini "
            "lets me see your screen. You need at least one, and both is best.", "lead"))
        lay.addSpacing(4)
        self.groq = KeyField(
            "groq", "Groq", "Conversation, tools and speech recognition. About 1,000 requests a day.",
            self.services, KEY_PAGES["groq"], "recommended")
        self.gemini = KeyField(
            "gemini", "Gemini", "Seeing the screen. About 120 looks a day on the free tier.",
            self.services, KEY_PAGES["gemini"], "for vision")
        for field in (self.groq, self.gemini):
            key, source = self.services.existing_key(field.provider)
            field.verified_changed.connect(lambda _ok: self._refresh())
            field.prefill(key, source)
            if key and source == "keyring":
                # Stored is not the same as working - check it too, without a click.
                QTimer.singleShot(0, field.test)
            lay.addWidget(field)
        lay.addWidget(label(
            "Keys are stored in Windows Credential Manager, never in a plain file.", "hint"))
        lay.addStretch(1)
        return page

    def _voice(self) -> QWidget:
        page, lay = self._page()
        lay.addWidget(label("How should I sound?", "title"))
        lay.addWidget(label(
            "I pick the right accent for English, Hindi or Nepali automatically. "
            "Choose a voice, and play a sample.", "lead"))
        lay.addSpacing(4)
        self.voice = VoicePicker(self.settings, self.services, lambda: self.name.text())
        lay.addWidget(self.voice)
        self.speak_replies = QCheckBox("Speak my replies out loud")
        self.speak_replies.setChecked(getattr(self.settings, "voice_enabled", True))
        lay.addWidget(self.speak_replies)
        lay.addSpacing(10)
        lay.addWidget(label("Microphone"))
        self.mic = MicPicker(self.services, getattr(self.settings, "mic_device", None))
        lay.addWidget(self.mic)
        lay.addWidget(label(
            "Speech in English and Hindi is recognised well. Nepali is recognised poorly, so "
            "typing works better for Nepali — especially for anything with a time or number in it.",
            "hint"))
        lay.addStretch(1)
        return page

    def _ready(self) -> QWidget:
        page, lay = self._page()
        self.ready_title = label("", "title")
        lay.addWidget(self.ready_title)
        self.ready_body = label("", "lead")
        lay.addWidget(self.ready_body)
        lay.addStretch(1)
        return page

    # --- navigation -------------------------------------------------------

    def keys_ok(self) -> bool:
        return self.groq.verified or self.gemini.verified

    def go(self, index: int) -> None:
        index = max(0, min(index, len(STEPS) - 1))
        self.stack.setCurrentIndex(index)
        self.progress.setText(
            "   ·   ".join(s.upper() if i == index else s for i, s in enumerate(STEPS)))
        if index == len(STEPS) - 1:
            self._fill_ready()
        self._refresh()

    def _refresh(self) -> None:
        index = self.stack.currentIndex()
        self.back.setVisible(index > 0)
        last = index == len(STEPS) - 1
        self.next.setText("Start Bantu" if last else "Next")
        blocked = index == 1 and not self.keys_ok()
        self.next.setEnabled(not blocked)
        self.next.setToolTip("Test at least one key first." if blocked else "")

    def _advance(self) -> None:
        if not self.next.isEnabled():
            return
        if self.stack.currentIndex() == len(STEPS) - 1:
            self.finish()
        else:
            self.go(self.stack.currentIndex() + 1)

    def _fill_ready(self) -> None:
        name = self.name.text().strip()
        self.ready_title.setText(f"You're all set{', ' + name if name else ''}.")
        services = [n for n, f in (("Groq", self.groq), ("Gemini", self.gemini)) if f.verified]
        missing = "" if self.gemini.verified else (
            " Without a Gemini key I can still read text on screen, but not see pictures or icons.")
        self.ready_body.setText(
            f"Connected: {' and '.join(services)}.{missing}\n\n"
            "Look for the glowing orb in the bottom-right corner of your screen. Click it to type, "
            "or press Ctrl+Alt+Space anywhere to speak. You can change all of this later from "
            "Settings in the tray icon.")

    # --- saving -----------------------------------------------------------

    def finish(self) -> None:
        """Store verified keys, save settings, and close. Only called from the last page."""
        if not self.keys_ok():
            self.go(1)
            return
        for field in (self.groq, self.gemini):
            if field.verified:
                self.services.store_key(field.provider, field.key)
                self.stored.append(field.provider)
        s = self.settings
        s.username = self.name.text().strip()
        s.voice_gender = self.voice.gender
        s.voice_enabled = self.speak_replies.isChecked()
        s.mic_device = self.mic.device
        s.onboarded = True
        s.save()
        self.accept()
