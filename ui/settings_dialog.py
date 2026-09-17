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
    QCheckBox, QComboBox, QDialog, QHBoxLayout, QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
    QPlainTextEdit, QPushButton, QTabWidget, QVBoxLayout, QWidget,
)

from core.activity import OUTCOMES, RETENTION_DAYS, describe_entry
from core.providers.validate import KEY_PAGES

from .setup_parts import (
    BAD, OK, THEME, KeyField, MicPicker, Services, VoicePicker, label, run_in_background, validate_hotkey,
)


GOOGLE_ICAL_HELP = "https://support.google.com/calendar/answer/37648"

EVENT_ALERT_CHOICES = (
    (0, "Don't remind me"),
    (5, "Remind me 5 minutes before"),
    (10, "Remind me 10 minutes before"),
    (15, "Remind me 15 minutes before"),
    (30, "Remind me 30 minutes before"),
)

RETENTION_CHOICES = (
    (0, "Until I delete them"),
    (30, "For 30 days"),
    (90, "For 90 days"),
    (365, "For a year"),
)

TONE_CHOICES = (
    ("warm", "Warm professional - friendly, respectful, never gushing"),
    ("playful", "Playful - witty and light, still exact when working"),
    ("professional", "Strictly professional - crisp, no small talk"),
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
        activity: Any = None,
        calendar: Any = None,
        meetings: Any = None,
    ):
        super().__init__(parent)
        self.meetings = meetings
        #: Asks before deleting a meeting. Replaceable, so tests need no real message box.
        self.confirm_delete: Callable[[str], bool] = lambda what: QMessageBox.question(
            self, "Delete meeting", f"Delete {what}? Its transcript and summary are removed for good.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No) == QMessageBox.Yes
        self.knowledge = knowledge
        self.activity = activity
        self.calendar = calendar
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
        # Tab labels take the dialog font. A stylesheet font-size here clipped them:
        # Qt measured each tab with one font and drew it with another.
        tab_font = self.tabs.tabBar().font()
        tab_font.setPixelSize(13)
        self.tabs.tabBar().setFont(tab_font)
        self.tabs.addTab(self._general(), "General")
        self.tabs.addTab(self._day(), "Your day")
        self.tabs.addTab(self._keys(), "Keys")
        if self.meetings is not None:
            self.tabs.addTab(self._meetings(), "Meetings")
        if self.activity is not None:
            self.tabs.addTab(self._activity(), "Activity")
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
        lay.addWidget(label("PERSONALITY", "section"))
        self.tone = QComboBox()
        for value, text in TONE_CHOICES:
            self.tone.addItem(text, value)
        current = self.tone.findData(getattr(self.settings, "tone", "warm"))
        self.tone.setCurrentIndex(max(0, current))
        lay.addWidget(self.tone)

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

    def _day(self) -> QWidget:
        # Its own tab: added to General, seven sections crushed every input into a clipped line.
        page, lay = self._tab()
        lay.addWidget(label("Say \"brief me\" any time for the weather, today's calendar, reminders, "
                            "and what is due or overdue.", "lead"))
        lay.addSpacing(6)
        lay.addWidget(label("WEATHER CITY", "section"))
        row = QHBoxLayout()
        self.city = QLineEdit(getattr(self.settings, "weather_city", "") or "")
        self.city.setPlaceholderText("e.g. Kathmandu - in Latin letters; empty for no weather")
        row.addWidget(self.city, 1)
        self.check_city = QPushButton("Check")
        self.check_city.setEnabled(self.services.find_place is not None)
        self.check_city.clicked.connect(self._check_city)
        row.addWidget(self.check_city)
        lay.addLayout(row)
        self.city_result = label("", "hint")
        lay.addWidget(self.city_result)
        self.city.textChanged.connect(lambda _t: self.city_result.setText(""))

        lay.addSpacing(6)
        lay.addWidget(label("BEFORE AN EVENT", "section"))
        self.event_alert = QComboBox()
        for minutes, text in EVENT_ALERT_CHOICES:
            self.event_alert.addItem(text, minutes)
        self.event_alert.setCurrentIndex(max(0, self.event_alert.findData(
            int(getattr(self.settings, "event_alert_minutes", 0) or 0))))
        lay.addWidget(self.event_alert)
        lay.addWidget(label("Off by default. Connect Google Calendar on the Keys tab.", "hint"))
        lay.addStretch(1)
        return page

    def _check_city(self) -> None:
        city = self.city.text().strip()
        if not city:
            self.city_result.setText("Leave it empty for no weather, or type a city.")
            return
        self.check_city.setEnabled(False)
        self.city_result.setStyleSheet("")
        self.city_result.setText("Looking it up…")

        def done(result) -> None:
            self.check_city.setEnabled(True)
            if isinstance(result, Exception):
                self.city_result.setStyleSheet(f"color:{BAD};")
                message = str(result)
                self.city_result.setText(message[:1].upper() + message[1:])
            else:
                self.city_result.setStyleSheet(f"color:{OK};")
                self.city_result.setText(f"Found: {result}")

        run_in_background(lambda: self.services.find_place(city), done)

    def _keys(self) -> QWidget:
        page, lay = self._tab()
        self.groq = KeyField("groq", "Groq", "Conversation, tools and speech recognition.",
                             self.services, KEY_PAGES["groq"])
        self.gemini = KeyField("gemini", "Gemini", "Seeing the screen.",
                               self.services, KEY_PAGES["gemini"])
        self.calendar_field = KeyField(
            "calendar", "Google Calendar (optional)",
            "Read-only. In Google Calendar: Settings, your calendar, Integrate calendar, "
            "'Secret address in iCal format'.",
            self.services, GOOGLE_ICAL_HELP, link_text="Where to find it ↗",
            placeholder="Paste the secret iCal address",
        )
        for field in (self.groq, self.gemini, self.calendar_field):
            key, source = self.services.existing_key(field.provider)
            field.prefill(key, source)
            lay.addWidget(field)
        if self.calendar is not None:
            status = self.calendar.feed_status()
            if self.calendar_field.key and status == "No Google Calendar connected.":
                status = "Waiting for the first update, which runs in the background."
            self.calendar_status = label(status, "hint")
            lay.addWidget(self.calendar_status)
        lay.addWidget(label(
            "A changed key or address is tested before it is saved. They live in Windows Credential Manager.",
            "hint"))
        lay.addStretch(1)
        return page

    def _meetings(self) -> QWidget:
        page, lay = self._tab()
        lay.addWidget(label("MEETINGS", "section"))
        lay.addWidget(label("Meetings Bantu has recorded. Audio is never kept, only the transcript and "
                            "summary; decisions and action items also go into memory.", "lead"))
        self.meeting_list = QListWidget()
        self.meeting_list.setObjectName("meetinglist")
        self.meeting_list.setStyleSheet(
            "QListWidget#meetinglist{background:#1B242A;color:#E6EDF0;border:1px solid #222E35;"
            "border-radius:7px;font-size:12px;padding:4px;}"
            "QListWidget#meetinglist::item{padding:6px 4px;border-bottom:1px solid #222E35;}"
            "QListWidget#meetinglist::item:selected{background:#1D3B40;color:#E6EDF0;}"
        )
        self.meeting_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.meeting_list.itemDoubleClicked.connect(lambda _item: self.open_meeting())
        lay.addWidget(self.meeting_list, 1)
        row = QHBoxLayout()
        self.open_meeting_button = QPushButton("Open")
        self.open_meeting_button.clicked.connect(self.open_meeting)
        row.addWidget(self.open_meeting_button)
        self.delete_meeting_button = QPushButton("Delete")
        self.delete_meeting_button.clicked.connect(self.delete_meeting)
        row.addWidget(self.delete_meeting_button)
        row.addStretch(1)
        lay.addLayout(row)
        lay.addSpacing(6)
        lay.addWidget(label("KEEP TRANSCRIPTS", "section"))
        self.retention = QComboBox()
        for days, text in RETENTION_CHOICES:
            self.retention.addItem(text, days)
        self.retention.setCurrentIndex(max(0, self.retention.findData(
            int(getattr(self.settings, "meeting_retention_days", 90)))))
        lay.addWidget(self.retention)
        self.refresh_meetings()
        return page

    def refresh_meetings(self) -> None:
        self.meeting_list.clear()
        meetings = self.meetings.recent(100)
        for meeting in meetings:
            item = QListWidgetItem(meeting.describe())
            item.setData(Qt.UserRole, meeting.id)
            self.meeting_list.addItem(item)
        if not meetings:
            empty = QListWidgetItem("No meetings recorded yet.")
            empty.setFlags(Qt.NoItemFlags)
            self.meeting_list.addItem(empty)
        else:
            self.meeting_list.setCurrentRow(0)
        for button in (self.open_meeting_button, self.delete_meeting_button):
            button.setEnabled(bool(meetings))

    def _selected_meeting(self):
        item = self.meeting_list.currentItem()
        meeting_id = item.data(Qt.UserRole) if item is not None else None
        return self.meetings.get(meeting_id) if meeting_id is not None else None

    def meeting_text(self, meeting) -> str:
        lines = self.meetings.transcript_lines(meeting.id)
        summary = meeting.summary or "No summary has been written."
        return f"{meeting.describe()}\n\n{summary}\n\nTranscript\n\n" + ("\n".join(lines) or "(nothing transcribed)")

    def open_meeting(self) -> QDialog | None:
        meeting = self._selected_meeting()
        if meeting is None:
            return None
        viewer = QDialog(self)
        viewer.setWindowTitle(meeting.title)
        viewer.setStyleSheet(THEME)
        viewer.resize(640, 560)
        box = QVBoxLayout(viewer)
        text = QPlainTextEdit(self.meeting_text(meeting))
        text.setReadOnly(True)
        text.setStyleSheet("QPlainTextEdit{background:#1B242A;color:#E6EDF0;border:1px solid #222E35;"
                           "border-radius:7px;font-size:13px;padding:8px;}")
        box.addWidget(text)
        viewer.show()
        self._viewer = viewer  # kept, or it is collected while open
        return viewer

    def delete_meeting(self) -> None:
        meeting = self._selected_meeting()
        if meeting is None:
            return
        if meeting.status in ("recording", "paused"):
            self._fail("Stop the recording before deleting it.", self.tabs.indexOf(self.meeting_list.parentWidget()))
            return
        if self.confirm_delete(f"'{meeting.title}'"):
            self.meetings.delete(meeting.id)
            self.refresh_meetings()

    def _activity(self) -> QWidget:
        page, lay = self._tab()
        lay.addWidget(label("ACTIVITY", "section"))
        lay.addWidget(label(
            "Everything that needed your approval, was refused, or failed - recorded by the app "
            f"itself, not by the assistant. Kept on this computer for {RETENTION_DAYS} days.", "lead"))
        self.activity_list = QListWidget()
        self.activity_list.setObjectName("activity")
        self.activity_list.setStyleSheet(
            "QListWidget#activity{background:#1B242A;color:#E6EDF0;border:1px solid #222E35;"
            "border-radius:7px;font-size:12px;padding:4px;}"
            "QListWidget#activity::item{padding:5px 4px;border-bottom:1px solid #222E35;}"
            "QListWidget#activity::item:selected{background:#1D3B40;color:#E6EDF0;}"
        )
        # Long commands wrap rather than scroll sideways under an unstyled scrollbar.
        self.activity_list.setWordWrap(True)
        self.activity_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        lay.addWidget(self.activity_list, 1)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh_activity)
        row = QHBoxLayout()
        row.addWidget(refresh)
        row.addStretch(1)
        lay.addLayout(row)
        self.refresh_activity()
        return page

    def refresh_activity(self) -> None:
        self.activity_list.clear()
        entries = self.activity.recent(200)
        if not entries:
            empty = QListWidgetItem("Nothing yet.")
            empty.setFlags(Qt.NoItemFlags)
            self.activity_list.addItem(empty)
            return
        for entry in entries:
            item = QListWidgetItem(describe_entry(entry))
            meaning = OUTCOMES.get(entry["outcome"], "")
            item.setToolTip(f"{entry['outcome']}: {meaning}\n\n{entry['detail']}".strip())
            self.activity_list.addItem(item)

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
        calendar_changed = self.calendar_field.changed
        if calendar_changed and self.calendar_field.key:
            changed_keys.append(self.calendar_field)
        unverified = [f for f in changed_keys if not f.verified]
        if unverified:
            names = " and ".join(
                "Google Calendar address" if f.provider == "calendar" else f"{f.provider.capitalize()} key"
                for f in unverified)
            self._fail(f"Test the new {names} before saving.", self.tabs.indexOf(self.groq.parentWidget()))
            return
        if not any(f.key for f in (self.groq, self.gemini)):
            self._fail("Bantu needs at least one key.", self.tabs.indexOf(self.groq.parentWidget()))
            return

        s = self.settings
        changes: set[str] = set()

        def update(attr: str, value: Any, tag: str | None = None) -> None:
            if getattr(s, attr, None) != value:
                setattr(s, attr, value)
                changes.add(tag or attr)

        update("username", self.name.text().strip())
        update("tone", self.tone.currentData())
        update("weather_city", " ".join(self.city.text().split()))
        update("event_alert_minutes", self.event_alert.currentData())
        if self.meetings is not None:
            update("meeting_retention_days", self.retention.currentData())
        update("voice_gender", self.voice.gender)
        update("voice_enabled", self.speak_replies.isChecked())
        update("mic_device", self.mic.device)
        update("hotkey", combo)

        for field in changed_keys:
            self.services.store_key(field.provider, field.key)
            changes.add("calendar" if field.provider == "calendar" else "keys")
        if calendar_changed and not self.calendar_field.key:
            # Cleared: disconnect. The next sync removes the copied events.
            if self.services.forget_key is not None:
                self.services.forget_key("calendar")
            changes.add("calendar")

        s.save()
        self.error.setText("")
        self.applied.emit(changes)
        self.accept()
