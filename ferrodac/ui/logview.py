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

from qtpy.QtCore import QTimer
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


class QtLogHandler(logging.Handler):
    """A logging handler that buffers records for the GUI to drain.

    Records arrive on *any* thread — including raw (non-QThread) worker threads
    from gRPC/asyncio/the sync runner. Touching Qt from those threads corrupts
    the heap, so this handler does **zero** Qt work: it only appends to a
    thread-safe ``deque`` (CPython ``append``/``popleft`` are atomic). The GUI
    thread polls it (see ``LogPanel``)."""

    def __init__(self, level=logging.NOTSET, capacity: int = 10000):
        super().__init__(level)
        self.records: deque = deque(maxlen=capacity)
        self.setFormatter(logging.Formatter(
            "%(asctime)s  %(name)s  %(message)s", datefmt="%H:%M:%S"))

    def emit(self, record):
        try:
            msg = self.format(record)
        except Exception:                 # never let logging crash the app
            return
        self.records.append((msg, record.levelno))   # no Qt here — GUI drains


class LogPanel(QWidget):
    """Read-only, colour-coded, filterable log view backed by a ring buffer."""

    def __init__(self, handler: QtLogHandler, capacity: int = 5000, parent=None):
        super().__init__(parent)
        self._handler = handler
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

        # poll the handler's buffer on the GUI thread — never the worker threads
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._drain)
        self._timer.start(250)

    # -- slots (GUI thread) --------------------------------------------------
    def _drain(self) -> None:
        recs = self._handler.records
        while True:
            try:
                msg, level = recs.popleft()      # atomic in CPython
            except IndexError:
                break
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
