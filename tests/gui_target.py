"""A throwaway app for the live GUI tests to operate.

Runs as its own process — Bantu refuses to click windows it owns, which is the
point — and writes everything that happens to it into a JSON file the test
reads back. Nothing here is part of Bantu.

    python tests/gui_target.py <state.json>
"""

from __future__ import annotations

import ctypes
import json
import sys
from pathlib import Path

ctypes.windll.shcore.SetProcessDpiAwareness(2)

from PyQt5.QtCore import QTimer, Qt
from PyQt5.QtGui import QColor, QPainter
from PyQt5.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QLineEdit, QListWidget, QPushButton,
    QVBoxLayout, QWidget,
)

TITLE = "Bantu GUI Test 8431"


class Dot(QWidget):
    """A red circle with no text: the target for the vision fallback."""

    def __init__(self):
        super().__init__()
        self.setFixedSize(46, 46)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setBrush(QColor("#d62828"))
        p.setPen(Qt.NoPen)
        p.drawEllipse(3, 3, 40, 40)


def main() -> int:
    out = Path(sys.argv[1])
    app = QApplication(sys.argv)
    w = QWidget()
    w.setWindowTitle(TITLE)
    w.setStyleSheet("QWidget{font-size:15px;} QPushButton{padding:6px 14px;}")

    state = {"launch": 0, "dup": [0, 0], "text": "", "scroll": 0, "later": False,
             "dot_center": None, "list_rect": None}

    root = QVBoxLayout(w)
    row = QHBoxLayout()
    launch = QPushButton("Launch probe")
    launch.clicked.connect(lambda: state.__setitem__("launch", state["launch"] + 1))
    row.addWidget(launch)
    for i in range(2):
        b = QPushButton("Duplicate")
        b.clicked.connect(lambda _c=False, i=i: state["dup"].__setitem__(i, state["dup"][i] + 1))
        row.addWidget(b)
    root.addLayout(row)

    edit = QLineEdit()
    edit.setPlaceholderText("Type here please")
    edit.textChanged.connect(lambda t: state.__setitem__("text", t))
    root.addWidget(edit)

    later_row = QHBoxLayout()
    show_later = QPushButton("Show later")
    later_label = QLabel("")
    later_row.addWidget(show_later)
    later_row.addWidget(later_label)
    later_row.addStretch(1)
    dot = Dot()
    later_row.addWidget(dot)
    root.addLayout(later_row)

    def reveal():
        later_label.setText("Ready now")
        state["later"] = True

    show_later.clicked.connect(lambda: QTimer.singleShot(1500, reveal))

    lst = QListWidget()
    for i in range(200):
        lst.addItem(f"row {i:03d}")
    lst.verticalScrollBar().valueChanged.connect(lambda v: state.__setitem__("scroll", v))
    root.addWidget(lst, 1)

    w.resize(560, 520)
    w.move(160, 120)
    w.show()
    w.raise_()
    w.activateWindow()

    def dump():
        # Report physical screen geometry via win32 so it matches the tools' space.
        import win32gui

        hwnd = int(w.winId())
        wl, wt, _, _ = win32gui.GetWindowRect(hwnd)
        ratio = w.devicePixelRatioF()
        c = dot.mapTo(w, dot.rect().center())
        client = win32gui.ClientToScreen(hwnd, (0, 0))
        state["dot_center"] = [int(client[0] + c.x() * ratio), int(client[1] + c.y() * ratio)]
        tl = lst.mapTo(w, lst.rect().topLeft())
        state["list_rect"] = [int(client[0] + tl.x() * ratio), int(client[1] + tl.y() * ratio),
                              int(client[0] + (tl.x() + lst.width()) * ratio),
                              int(client[1] + (tl.y() + lst.height()) * ratio)]
        tmp = out.with_suffix(".tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        tmp.replace(out)

    timer = QTimer()
    timer.timeout.connect(dump)
    timer.start(80)
    return app.exec_()


if __name__ == "__main__":
    raise SystemExit(main())
