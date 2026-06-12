"""Minimal v1 UI: a source-management view with nested source/channel cards.

Left column = available (discovered) sources you can add; right column = active
(connected) sources, each showing its channels as sub-cards. No plotting, no
data plane yet — cards show identity + status; values are placeholders.
"""

from __future__ import annotations

from .. import __version__
from .. import _qtbinding  # noqa: F401  selects QT_API before qtpy import

from qtpy.QtCore import Qt
from qtpy.QtGui import QColor, QPalette
from qtpy.QtWidgets import (
    QApplication,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..core.manager import SourceManager
from ..core.registry import load_builtin_drivers
from ..core.source import SourceDescriptor, Status

CHANNEL_COLORS = ["#4fc3f7", "#ff8a65", "#81c784", "#ba68c8", "#ffd54f", "#e57373"]

STATUS_COLORS = {
    Status.DISCOVERED: "#7f8a99",
    Status.CONNECTING: "#ffd54f",
    Status.CONNECTED: "#69db7c",
    Status.ERROR: "#ff6b6b",
    Status.DISCONNECTED: "#7f8a99",
}


def _clear(layout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        w = item.widget()
        if w is not None:
            w.setParent(None)
            w.deleteLater()


# --------------------------------------------------------------------------- #
#  Cards
# --------------------------------------------------------------------------- #
class ChannelCard(QFrame):
    def __init__(self, channel, color: str, parent=None):
        super().__init__(parent)
        self.setObjectName("ChannelCard")
        self.setStyleSheet(
            "#ChannelCard { background:#1c2230; border:1px solid #2a3340;"
            " border-radius:7px; }"
        )
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(1)

        top = QHBoxLayout()
        top.setSpacing(6)
        swatch = QLabel()
        swatch.setFixedSize(9, 9)
        swatch.setStyleSheet(f"background:{color}; border-radius:4px;")
        name = QLabel(channel.name)
        name.setStyleSheet("font-weight:600;")
        top.addWidget(swatch)
        top.addWidget(name)
        top.addStretch(1)
        lay.addLayout(top)

        value = QLabel("—")
        value.setStyleSheet(f"color:{color}; font-family:monospace; font-size:14px;")
        lay.addWidget(value)

        unit = QLabel(channel.unit or "")
        unit.setStyleSheet("color:#7f8a99; font-size:10px;")
        lay.addWidget(unit)


class SourceCard(QFrame):
    """Renders a SourceDescriptor. `active=False` shows an Add button; `active=True`
    shows status, the primary value, nested channel cards, and a Remove button."""

    def __init__(self, desc: SourceDescriptor, active: bool, on_action, parent=None):
        super().__init__(parent)
        self.setObjectName("SourceCard")
        self.setStyleSheet(
            "#SourceCard { background:#171c26; border:1px solid #232a38;"
            " border-radius:10px; }"
        )
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(6)

        # -- header --
        header = QHBoxLayout()
        header.setSpacing(8)
        dot = QLabel("●")
        dot.setStyleSheet(f"color:{STATUS_COLORS.get(desc.status, '#7f8a99')};")
        title = QLabel(desc.name)
        title.setStyleSheet("font-size:14px; font-weight:700;")
        sub = QLabel(f"{desc.driver} · {desc.interface.kind}")
        sub.setStyleSheet("color:#7f8a99;")
        header.addWidget(dot)
        header.addWidget(title)
        header.addWidget(sub)
        header.addStretch(1)

        btn = QPushButton("Add" if not active else "Remove")
        btn.setFixedWidth(84)
        btn.clicked.connect(lambda: on_action(desc.instance_id))
        header.addWidget(btn)
        lay.addLayout(header)

        # -- status / provenance line --
        bits = [desc.status.value]
        if desc.firmware:
            bits.append(f"fw {desc.firmware}")
        if desc.hardware_id:
            bits.append(desc.hardware_id)
        if desc.last_error:
            bits.append(f"⚠ {desc.last_error}")
        info = QLabel("   ·   ".join(bits))
        info.setStyleSheet("color:#8b95a4; font-size:11px;")
        lay.addWidget(info)

        # -- primary value (featured) --
        primary = desc.primary
        if primary is not None:
            pcolor = CHANNEL_COLORS[
                self._channel_index(desc, primary.id) % len(CHANNEL_COLORS)
            ]
            pv = QLabel(f"{primary.name}:  —  {primary.unit}".rstrip())
            pv.setStyleSheet(f"color:{pcolor}; font-family:monospace; font-size:15px;")
            lay.addWidget(pv)

        # -- channel sub-cards (active cards only) --
        if active and desc.channels:
            grid_host = QWidget()
            grid = QGridLayout(grid_host)
            grid.setContentsMargins(0, 4, 0, 0)
            grid.setSpacing(6)
            for i, ch in enumerate(desc.channels):
                color = CHANNEL_COLORS[i % len(CHANNEL_COLORS)]
                grid.addWidget(ChannelCard(ch, color), i // 3, i % 3)
            lay.addWidget(grid_host)
        elif not active and desc.channels:
            n = len(desc.channels)
            chl = QLabel(f"{n} channel{'s' if n != 1 else ''}")
            chl.setStyleSheet("color:#7f8a99; font-size:11px;")
            lay.addWidget(chl)

    @staticmethod
    def _channel_index(desc: SourceDescriptor, channel_id: str) -> int:
        for i, ch in enumerate(desc.channels):
            if ch.id == channel_id:
                return i
        return 0


# --------------------------------------------------------------------------- #
#  Main window
# --------------------------------------------------------------------------- #
class MainWindow(QMainWindow):
    def __init__(self, manager: SourceManager, parent=None):
        super().__init__(parent)
        self.manager = manager
        self.setWindowTitle("ferroDAC")
        self.resize(1040, 680)

        self._available_box = self._build_column("Available sources")
        self._active_box = self._build_column("Active sources")

        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(8)

        head = QLabel("Source management")
        head.setStyleSheet("font-size:16px; font-weight:700;")
        outer.addWidget(head)

        cols = QHBoxLayout()
        cols.setSpacing(12)
        cols.addWidget(self._available_box["frame"], 1)
        cols.addWidget(self._active_box["frame"], 1)
        outer.addLayout(cols)

        self.manager.available_changed.connect(self._rebuild_available)
        self.manager.active_changed.connect(self._rebuild_active)
        self.statusBar().showMessage("Scanning for sources…")
        self._rebuild_available()
        self._rebuild_active()
        self.manager.start()

    def _build_column(self, title: str) -> dict:
        frame = QFrame()
        v = QVBoxLayout(frame)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(6)
        lbl = QLabel(title)
        lbl.setStyleSheet("font-size:13px; font-weight:700; color:#c7d0db;")
        v.addWidget(lbl)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        host = QWidget()
        cards = QVBoxLayout(host)
        cards.setContentsMargins(0, 0, 0, 0)
        cards.setSpacing(8)
        cards.addStretch(1)
        scroll.setWidget(host)
        v.addWidget(scroll, 1)
        return {"frame": frame, "layout": cards, "label": lbl, "title": title}

    def _rebuild_available(self) -> None:
        self._rebuild(self._available_box, self.manager.available_descriptors(),
                      active=False, on_action=self.manager.add)

    def _rebuild_active(self) -> None:
        self._rebuild(self._active_box, self.manager.active_descriptors(),
                      active=True, on_action=self.manager.remove)

    def _rebuild(self, box: dict, descriptors, active: bool, on_action) -> None:
        layout = box["layout"]
        _clear(layout)
        for desc in sorted(descriptors, key=lambda d: d.name):
            layout.addWidget(SourceCard(desc, active, on_action))
        layout.addStretch(1)
        box["label"].setText(f"{box['title']}  ({len(descriptors)})")

    def closeEvent(self, event):  # noqa: N802 (Qt signature)
        self.manager.stop()
        super().closeEvent(event)


# --------------------------------------------------------------------------- #
#  Bootstrap
# --------------------------------------------------------------------------- #
def apply_dark_theme(app: QApplication) -> None:
    app.setStyle("Fusion")
    hints = app.styleHints()
    if hasattr(hints, "setColorScheme"):
        try:
            hints.setColorScheme(Qt.ColorScheme.Dark)
        except Exception:
            pass
    base, panel, text = QColor("#11151c"), QColor("#171c26"), QColor("#c7d0db")
    pal = QPalette()
    pal.setColor(QPalette.Window, base)
    pal.setColor(QPalette.WindowText, text)
    pal.setColor(QPalette.Base, panel)
    pal.setColor(QPalette.AlternateBase, base)
    pal.setColor(QPalette.Text, text)
    pal.setColor(QPalette.Button, panel)
    pal.setColor(QPalette.ButtonText, text)
    pal.setColor(QPalette.Highlight, QColor("#4fc3f7"))
    pal.setColor(QPalette.HighlightedText, QColor("#0b0e13"))
    app.setPalette(pal)
    app.setStyleSheet(
        """
        QWidget { font-size: 12px; }
        QPushButton { background:#222b3a; border:1px solid #2c374a;
            border-radius:7px; padding:5px 10px; }
        QPushButton:hover:enabled { background:#2b3850; }
        QStatusBar { color:#8b95a4; }
        """
    )


def main(argv=None) -> int:
    import sys

    app = QApplication(sys.argv if argv is None else argv)
    app.setApplicationName("ferroDAC")
    apply_dark_theme(app)

    drivers = load_builtin_drivers()
    manager = SourceManager(drivers)
    win = MainWindow(manager)
    win.show()
    return app.exec()
