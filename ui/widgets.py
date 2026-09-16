"""The HUD: a floating orb and the panel that expands from it.

An assistant you have to go and open is a program; one that is simply present
is something else. The orb's ring colour is the single most informative pixel
on screen — idle, listening, thinking, speaking, or failed — so state reads at a
glance without reading anything.

Stylesheets target object names, never bare class selectors: QLabel and
QScrollArea both subclass QFrame, so a `QFrame{}` rule silently restyles every
label inside it.
"""

from __future__ import annotations

import math
from enum import Enum
from pathlib import Path

from PyQt5.QtCore import QPoint, QRect, QRectF, QSize, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QIcon, QMovie, QPainter, QPainterPath, QPen
from PyQt5.QtWidgets import (
    QFrame, QGraphicsDropShadowEffect, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

ASSETS = Path(__file__).resolve().parents[1] / "Frontend" / "Graphics"

ORB_SIZE = 84
PANEL_W, PANEL_H = 430, 560
#: Transparent border so the drop shadow and rounded corners are not clipped
#: by the window edge, which is what happens when the frame fills the window.
SHADOW = 14


class State(Enum):
    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"
    ERROR = "error"


#: Ring colour per state. Semantic, not decorative — this is the status display.
RING = {
    State.IDLE: QColor("#3C6E8F"),
    State.LISTENING: QColor("#2FA36B"),
    State.THINKING: QColor("#C98A24"),
    State.SPEAKING: QColor("#2AA5B8"),
    State.ERROR: QColor("#C0483F"),
}

INK = "#E6EDF0"
INK_SOFT = "#97A5AF"
SURFACE = "#141B1F"
SURFACE_2 = "#1B242A"
RULE = "#222E35"
ACCENT = "#47C4D0"
MONO = "Consolas, 'Cascadia Mono', monospace"


class Orb(QWidget):
    """Frameless always-on-top circle. Drag to move, click to open the panel."""

    clicked = pyqtSignal()
    dragged = pyqtSignal()

    def __init__(self) -> None:
        super().__init__(None)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedSize(ORB_SIZE, ORB_SIZE)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("Bantu — click to open, drag to move")

        self._state = State.IDLE
        self._phase = 0.0
        self._press: QPoint | None = None
        self._moved = False

        self._movie: QMovie | None = None
        gif = ASSETS / "Jarvis.gif"
        if gif.exists():
            movie = QMovie(str(gif))
            native = movie.scaledSize() if movie.scaledSize().isValid() else QSize()
            movie.jumpToFrame(0)
            src = movie.currentImage().size() if native.isEmpty() else native
            # Scale once, preserving the 16:9 aspect, so the circle crops the
            # middle of the animation rather than squashing it into a square.
            h = ORB_SIZE * 2
            w = int(h * src.width() / src.height()) if src.height() else h
            movie.setScaledSize(QSize(w, h))
            movie.frameChanged.connect(self.update)
            movie.start()
            self._movie = movie

        self._tick = QTimer(self)
        self._tick.timeout.connect(self._animate)
        self._tick.start(40)

    # --- state --------------------------------------------------------------

    @property
    def state(self) -> State:
        return self._state

    def set_state(self, state: State) -> None:
        if state is not self._state:
            self._state = state
            self.update()

    def _animate(self) -> None:
        self._phase = (self._phase + 0.08) % (2 * math.pi)
        if self._state is not State.IDLE:
            self.update()

    # --- painting -----------------------------------------------------------

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)

        pad = 7
        inner = QRect(pad, pad, self.width() - pad * 2, self.height() - pad * 2)
        colour = RING[self._state]

        # Pulse halo. Idle sits still so a resting assistant is not distracting.
        if self._state is not State.IDLE:
            pulse = (math.sin(self._phase) + 1) / 2
            glow = QColor(colour)
            glow.setAlpha(int(40 + 70 * pulse))
            p.setPen(QPen(glow, 3 + 3 * pulse))
            p.drawEllipse(inner.adjusted(-4, -4, 4, 4))

        path = QPainterPath()
        # QRectF, never QRect: QPainterPath.addEllipse(QRect) is a hard native
        # crash in this PyQt5 build (verified by bisection), not an exception.
        path.addEllipse(QRectF(inner))
        p.setClipPath(path)
        frame = self._movie.currentPixmap() if self._movie is not None else None
        if frame is not None and not frame.isNull():
            side = min(frame.width(), frame.height())
            crop = frame.copy((frame.width() - side) // 2, (frame.height() - side) // 2, side, side)
            p.drawPixmap(inner, crop)
        else:
            p.fillRect(inner, QColor(SURFACE_2))
        p.setClipping(False)

        # State ring, drawn last so it always reads.
        p.setPen(QPen(colour, 3))
        p.drawEllipse(inner)

        if self._state is State.THINKING:
            # A travelling arc says "working" without a spinner widget.
            p.setPen(QPen(QColor(ACCENT), 3, Qt.SolidLine, Qt.RoundCap))
            start = int(math.degrees(self._phase) * 16) % (360 * 16)
            p.drawArc(inner.adjusted(-4, -4, 4, 4), start, 70 * 16)

    # --- interaction --------------------------------------------------------

    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.LeftButton:
            self._press = e.globalPos() - self.frameGeometry().topLeft()
            self._moved = False

    def mouseMoveEvent(self, e) -> None:
        if self._press is not None and e.buttons() & Qt.LeftButton:
            self.move(e.globalPos() - self._press)
            self._moved = True

    def mouseReleaseEvent(self, e) -> None:
        if e.button() == Qt.LeftButton:
            (self.dragged if self._moved else self.clicked).emit()
            self._press = None


def _label(text: str, css: str) -> QLabel:
    lab = QLabel(text)
    lab.setObjectName("plain")
    lab.setStyleSheet(f"QLabel#plain{{{css}background:transparent;border:none;}}")
    return lab


class Bubble(QFrame):
    """One turn in the conversation."""

    def __init__(self, text: str, role: str) -> None:
        super().__init__()
        self.role = role
        self.setObjectName("bubble")
        mine = role == "user"
        self.setStyleSheet(
            f"QFrame#bubble{{background:{'#1D2B33' if mine else SURFACE_2};"
            f"border:1px solid {RULE};border-radius:9px;}}"
        )
        lay = QVBoxLayout(self)
        lay.setContentsMargins(11, 8, 11, 9)
        self.label = _label(text, f"color:{INK};font-size:13px;")
        self.label.setWordWrap(True)
        self.label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self.label)


class ToolChip(QFrame):
    """A tool call on one line; click to expand its full output."""

    def __init__(self, name: str, args: dict) -> None:
        super().__init__()
        self.name = name
        self.result: str | None = None
        self._expanded = False
        self.setObjectName("chip")
        self.setStyleSheet(f"QFrame#chip{{background:transparent;border-left:2px solid {RULE};}}")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 2, 6, 2)
        lay.setSpacing(2)

        shown = ", ".join(f"{k}={v!r}" for k, v in list(args.items())[:2])
        self._head_text = f"↳ {name}({shown[:70]})"
        self.head = _label(self._head_text, f"color:{INK_SOFT};font-size:11px;font-family:{MONO};")
        self.head.setWordWrap(True)
        lay.addWidget(self.head)

        self.body = _label("", f"color:{INK_SOFT};font-size:11px;font-family:{MONO};")
        self.body.setWordWrap(True)
        self.body.hide()
        lay.addWidget(self.body)

    def set_result(self, result: str, ok: bool = True) -> None:
        self.result = result
        first = (result or "").strip().split("\n")[0][:90]
        colour = INK_SOFT if ok else "#E8737F"
        self.head.setStyleSheet(
            f"QLabel#plain{{color:{colour};font-size:11px;font-family:{MONO};"
            f"background:transparent;border:none;}}"
        )
        self.head.setText(f"{self._head_text}  →  {first}")
        self.body.setText((result or "")[:1500])
        self.setCursor(Qt.PointingHandCursor)

    def mousePressEvent(self, _e) -> None:
        if self.result is not None:
            self._expanded = not self._expanded
            self.body.setVisible(self._expanded)


class ConfirmBar(QFrame):
    """Inline approval. Shows the exact arguments before anything mutating runs."""

    answered = pyqtSignal(str)  # "yes" | "all" | "no"

    def __init__(self, tool_name: str, args: dict) -> None:
        super().__init__()
        self.tool_name = tool_name
        self.setObjectName("confirm")
        self.setStyleSheet(
            "QFrame#confirm{background:#2C2112;border:1px solid #6B5320;border-radius:9px;}"
        )
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 11)
        lay.setSpacing(7)

        lay.addWidget(_label(f"Allow <b>{tool_name}</b>?", "color:#E0A44A;font-size:13px;"))
        detail = "\n".join(f"{k} = {v!r}" for k, v in args.items()) or "(no arguments)"
        body = _label(detail, f"color:{INK};font-size:11.5px;font-family:{MONO};")
        body.setWordWrap(True)
        body.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(body)

        row = QHBoxLayout()
        row.setSpacing(6)
        self.buttons: dict[str, QPushButton] = {}
        for label, answer, primary in (
            ("Approve", "yes", True),
            ("Approve all this task", "all", False),
            ("Reject", "no", False),
        ):
            b = QPushButton(label)
            b.setCursor(Qt.PointingHandCursor)
            b.setStyleSheet(
                f"QPushButton{{background:{'#2FA36B' if primary else SURFACE_2};"
                f"color:{'#06181B' if primary else INK};border:1px solid {RULE};"
                f"border-radius:6px;padding:5px 10px;font-size:11.5px;}}"
                f"QPushButton:hover{{border-color:{ACCENT};}}"
                f"QPushButton:focus{{border-color:{ACCENT};}}"
            )
            b.clicked.connect(lambda _c=False, a=answer: self.answered.emit(a))
            self.buttons[answer] = b
            row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)


class ChatPanel(QWidget):
    """The conversation, expanded from the orb."""

    submitted = pyqtSignal(str)
    listen_requested = pyqtSignal()
    closed = pyqtSignal()

    def __init__(self, hotkey_label: str = "") -> None:
        super().__init__(None)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedSize(PANEL_W + SHADOW * 2, PANEL_H + SHADOW * 2)

        shell = QFrame(self)
        shell.setObjectName("shell")
        shell.setGeometry(SHADOW, SHADOW, PANEL_W, PANEL_H)
        shell.setStyleSheet(
            f"QFrame#shell{{background:{SURFACE};border:1px solid {RULE};border-radius:12px;}}"
        )
        shadow = QGraphicsDropShadowEffect(blurRadius=24, xOffset=0, yOffset=5)
        shadow.setColor(QColor(0, 0, 0, 170))
        shell.setGraphicsEffect(shadow)

        root = QVBoxLayout(shell)
        root.setContentsMargins(12, 10, 12, 12)
        root.setSpacing(8)

        head = QHBoxLayout()
        head.addWidget(_label("Bantu", f"color:{INK};font-size:14px;font-weight:600;"))
        head.addStretch(1)
        self.status = _label("ready", f"color:{INK_SOFT};font-size:11px;")
        head.addWidget(self.status)
        close = QPushButton("✕")
        close.setFixedSize(22, 22)
        close.setCursor(Qt.PointingHandCursor)
        close.setToolTip("Hide (Bantu keeps running in the tray)")
        close.setStyleSheet(
            f"QPushButton{{background:transparent;color:{INK_SOFT};border:none;font-size:13px;}}"
            f"QPushButton:hover{{color:{INK};}}"
        )
        close.clicked.connect(self.closed.emit)
        head.addWidget(close)
        root.addLayout(head)

        self.scroll = QScrollArea()
        self.scroll.setObjectName("feed")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setStyleSheet(
            "QScrollArea#feed{background:transparent;border:none;}"
            f"QScrollBar:vertical{{background:transparent;width:7px;}}"
            f"QScrollBar::handle:vertical{{background:{RULE};border-radius:3px;min-height:24px;}}"
            "QScrollBar::add-line,QScrollBar::sub-line{height:0;}"
        )
        holder = QWidget()
        holder.setObjectName("holder")
        holder.setStyleSheet("QWidget#holder{background:transparent;}")
        self.feed = QVBoxLayout(holder)
        self.feed.setContentsMargins(0, 0, 4, 0)
        self.feed.setSpacing(6)
        self.feed.addStretch(1)
        self.scroll.setWidget(holder)
        self.scroll.viewport().setStyleSheet("background:transparent;")
        root.addWidget(self.scroll, 1)

        row = QHBoxLayout()
        row.setSpacing(6)
        self.entry = QLineEdit()
        self.entry.setPlaceholderText("Ask Bantu…")
        self.entry.setStyleSheet(
            f"QLineEdit{{background:{SURFACE_2};color:{INK};border:1px solid {RULE};"
            f"border-radius:8px;padding:8px 10px;font-size:13px;}}"
            f"QLineEdit:focus{{border-color:{ACCENT};}}"
            f"QLineEdit:disabled{{color:{INK_SOFT};}}"
        )
        self.entry.returnPressed.connect(self._submit)
        row.addWidget(self.entry, 1)

        self.mic = QPushButton()
        self.mic.setFixedSize(36, 36)
        self.mic.setCursor(Qt.PointingHandCursor)
        self.mic.setToolTip(f"Speak ({hotkey_label} anywhere)" if hotkey_label else "Speak")
        icon = ASSETS / "Mic_on.png"
        if icon.exists():
            self.mic.setIcon(QIcon(str(icon)))
            self.mic.setIconSize(QSize(17, 17))
        else:
            self.mic.setText("Mic")
        self.mic.setStyleSheet(
            f"QPushButton{{background:{SURFACE_2};border:1px solid {RULE};border-radius:8px;}}"
            f"QPushButton:hover{{border-color:{ACCENT};}}"
        )
        self.mic.clicked.connect(self.listen_requested.emit)
        row.addWidget(self.mic)
        root.addLayout(row)

    # --- content ------------------------------------------------------------

    def _submit(self) -> None:
        text = self.entry.text().strip()
        if text:
            self.entry.clear()
            self.submitted.emit(text)

    def _append(self, widget: QWidget) -> QWidget:
        self.feed.insertWidget(self.feed.count() - 1, widget)
        QTimer.singleShot(30, self._scroll_to_end)
        return widget

    def _scroll_to_end(self) -> None:
        bar = self.scroll.verticalScrollBar()
        bar.setValue(bar.maximum())

    def items(self) -> list[QWidget]:
        """Everything in the transcript, oldest first. Used by tests."""
        return [
            self.feed.itemAt(i).widget()
            for i in range(self.feed.count())
            if self.feed.itemAt(i).widget() is not None
        ]

    def add_message(self, text: str, role: str) -> Bubble:
        return self._append(Bubble(text, role))

    def add_tool(self, name: str, args: dict) -> ToolChip:
        return self._append(ToolChip(name, args))

    def add_confirm(self, tool_name: str, args: dict) -> ConfirmBar:
        return self._append(ConfirmBar(tool_name, args))

    def set_status(self, text: str) -> None:
        self.status.setText(text)

    def set_busy(self, busy: bool) -> None:
        self.entry.setEnabled(not busy)
        self.mic.setEnabled(not busy)
        if not busy:
            self.entry.setFocus()
