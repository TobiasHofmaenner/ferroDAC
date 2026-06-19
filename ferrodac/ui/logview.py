"""In-app log viewer + a hub/sync status indicator.

`QtLogHandler` is a `logging.Handler` that re-emits records as a Qt signal, so
log lines produced on *any* thread (the sync runner, device workers, asyncio)
land safely on the GUI thread via a queued connection. `LogPanel` shows them in
a filterable, colour-coded read-only view — a window into "what is the app
doing right now" without tailing the on-disk log.

`SyncStatusWidget` is a small coloured-dot + label for the status bar: it reads
out the hub connection and the store-and-forward sync direction/progress
(DESIGN §12.1) at a glance.
"""

from __future__ import annotations

import logging
from collections import deque
from html import escape

from qtpy.QtCore import QObject, Qt, Signal
from qtpy.QtGui import QTextCursor
from qtpy.QtWidgets import (QCheckBox, QComboBox, QFrame, QHBoxLayout, QLabel,
                            QPlainTextEdit, QPushButton, QVBoxLayout, QWidget)

# level → (label, colour) for the filter combo and line tinting
_LEVELS = [
    ("All", logging.NOTSET),
    ("Info", logging.INFO),
    ("Warning", logging.WARNING),
    ("Error", logging.ERROR),
]
_COLOURS = {
    logging.DEBUG: "#6b7689",
    logging.INFO: "#c5ccd6",
    logging.WARNING: "#e3b341",
    logging.ERROR: "#f06a5a",
    logging.CRITICAL: "#ff5151",
}


class _Emitter(QObject):
    message = Signal(str, int)            # formatted line, levelno


class QtLogHandler(logging.Handler):
    """A logging handler that forwards each record to the GUI as a Qt signal.

    Emitting a signal across threads is queued by Qt, so this is safe to attach
    to the root logger even though records originate on worker threads."""

    def __init__(self, level=logging.NOTSET):
        super().__init__(level)
        self.emitter = _Emitter()
        self.setFormatter(logging.Formatter(
            "%(asctime)s  %(name)s  %(message)s", datefmt="%H:%M:%S"))

    def emit(self, record):
        try:
            msg = self.format(record)
        except Exception:                 # never let logging crash the app
            return
        self.emitter.message.emit(msg, record.levelno)


class LogPanel(QWidget):
    """Read-only, colour-coded, filterable log view backed by a ring buffer."""

    def __init__(self, handler: QtLogHandler, capacity: int = 5000, parent=None):
        super().__init__(parent)
        self._buf: deque = deque(maxlen=capacity)
        self._min_level = logging.NOTSET

        head = QHBoxLayout()
        head.setContentsMargins(6, 4, 6, 0)
        head.addWidget(QLabel("Level"))
        self._filter = QComboBox()
        for label, lvl in _LEVELS:
            self._filter.addItem(label, lvl)
        self._filter.currentIndexChanged.connect(self._on_filter)
        head.addWidget(self._filter)
        self._autoscroll = QCheckBox("Auto-scroll")
        self._autoscroll.setChecked(True)
        head.addWidget(self._autoscroll)
        head.addStretch(1)
        clear = QPushButton("Clear")
        clear.clicked.connect(self.clear)
        head.addWidget(clear)

        self._view = QPlainTextEdit()
        self._view.setReadOnly(True)
        self._view.setMaximumBlockCount(capacity)
        self._view.setLineWrapMode(QPlainTextEdit.NoWrap)
        self._view.setStyleSheet(
            "QPlainTextEdit{background:#10141c;border:none;"
            "font-family:'JetBrains Mono','Consolas',monospace;font-size:11px;}")

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        lay.addLayout(head)
        lay.addWidget(self._view, 1)

        handler.emitter.message.connect(self.append)

    # -- slots ---------------------------------------------------------------
    def append(self, msg: str, level: int) -> None:
        self._buf.append((msg, level))
        if level >= self._min_level:
            self._emit_line(msg, level)

    def _emit_line(self, msg: str, level: int) -> None:
        colour = _COLOURS.get(level, "#c5ccd6")
        self._view.appendHtml(
            f'<span style="color:{colour};white-space:pre">{escape(msg)}</span>')
        if self._autoscroll.isChecked():
            self._view.moveCursor(QTextCursor.End)

    def _on_filter(self, _idx: int) -> None:
        self._min_level = self._filter.currentData()
        self._view.clear()
        for msg, level in self._buf:
            if level >= self._min_level:
                self._emit_line(msg, level)

    def clear(self) -> None:
        self._buf.clear()
        self._view.clear()


class SyncStatusWidget(QFrame):
    """A coloured dot + short label for the status bar: hub link + sync state."""

    _STATES = {
        "offline": ("#5b6472", "offline"),
        "connecting": ("#e3b341", "connecting…"),
        "idle": ("#3fb950", "synced"),
        "syncing": ("#58a6ff", "syncing…"),
        "error": ("#f06a5a", "sync error"),
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 0, 8, 0)
        lay.setSpacing(5)
        self._dot = QLabel("●")
        self._text = QLabel("offline")
        self._text.setStyleSheet("color:#8a93a3;font-size:11px;")
        lay.addWidget(self._dot)
        lay.addWidget(self._text)
        self.set_state("offline")

    def set_state(self, state: str, detail: str = "") -> None:
        colour, label = self._STATES.get(state, self._STATES["offline"])
        self._dot.setStyleSheet(f"color:{colour};font-size:13px;")
        self._text.setText(detail or label)
        self.setToolTip(detail or label)
