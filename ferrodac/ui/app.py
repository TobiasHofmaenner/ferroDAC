"""ferroDAC UI — an IDE-style dockable shell.

Layout:
  - central : the live **charts** (the dashboard).
  - left dock "Sources" : device management (add/remove/configure). Hidden by
    default; opened via the toolbar button or the View menu.
  - right dock "Channels" : one card per channel of every active device, with a
    per-channel **plot** toggle that routes it to (or out of) the chart.

Docks are movable/floatable/closable; the View menu toggles them.
"""

from __future__ import annotations

from .. import __version__
from .. import _qtbinding  # noqa: F401  selects QT_API before qtpy import

from qtpy.QtCore import Qt, Signal
from qtpy.QtGui import QColor, QPalette
from qtpy.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDockWidget,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

import pyqtgraph as pg

from ..core.engine import Engine
from ..core.manager import SourceManager
from ..core.registry import load_builtin_drivers
from ..core.source import ControlKind, RateMode, SourceDescriptor, Status

pg.setConfigOptions(antialias=True, background="#11151c", foreground="#c7d0db")

CHANNEL_COLORS = ["#4fc3f7", "#ff8a65", "#81c784", "#ba68c8", "#ffd54f", "#e57373",
                  "#64b5f6", "#a1887f", "#4db6ac", "#f06292"]

STATUS_COLORS = {
    Status.DISCOVERED: "#7f8a99",
    Status.CONNECTING: "#ffd54f",
    Status.CONNECTED: "#69db7c",
    Status.ERROR: "#ff6b6b",
    Status.DISCONNECTED: "#7f8a99",
}

# Stable colour per channel key so a channel card and its plot curve always match.
_COLOR_MAP: dict[str, str] = {}


def _color_for(key: str) -> str:
    if key not in _COLOR_MAP:
        _COLOR_MAP[key] = CHANNEL_COLORS[len(_COLOR_MAP) % len(CHANNEL_COLORS)]
    return _COLOR_MAP[key]


def _fmt(value, unit: str = "") -> str:
    if value is None or value != value:        # None / NaN
        return "—"
    a = abs(value)
    s = f"{value:.3e}" if (a != 0 and (a < 1e-3 or a >= 1e4)) else f"{value:.4g}"
    return f"{s} {unit}".rstrip()


def _clear(layout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        w = item.widget()
        if w is not None:
            w.setParent(None)
            w.deleteLater()


# --------------------------------------------------------------------------- #
#  Channel card (right dock) — live value + plot routing toggle
# --------------------------------------------------------------------------- #
class ChannelCard(QFrame):
    plot_toggled = Signal(str, bool)   # (channel key, on)

    def __init__(self, key: str, channel, device_name: str, color: str,
                 plot_on: bool = True, parent=None):
        super().__init__(parent)
        self.key = key
        self.unit = channel.unit or ""
        self.setObjectName("ChannelCard")
        self.setStyleSheet(
            "#ChannelCard { background:#171c26; border:1px solid #232a38;"
            " border-radius:8px; }"
        )
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(2)

        top = QHBoxLayout()
        top.setSpacing(6)
        swatch = QLabel()
        swatch.setFixedSize(10, 10)
        swatch.setStyleSheet(f"background:{color}; border-radius:5px;")
        name = QLabel(channel.name)
        name.setStyleSheet("font-weight:700;")
        self.plot_check = QCheckBox("plot")
        self.plot_check.setChecked(plot_on)
        self.plot_check.setToolTip("Route this channel to the chart")
        self.plot_check.toggled.connect(lambda on: self.plot_toggled.emit(self.key, on))
        top.addWidget(swatch)
        top.addWidget(name)
        top.addStretch(1)
        top.addWidget(self.plot_check)
        lay.addLayout(top)

        self.value_label = QLabel("—")
        self.value_label.setStyleSheet(
            f"color:{color}; font-family:monospace; font-size:15px;"
        )
        lay.addWidget(self.value_label)

        sub = QLabel(f"{device_name}  ·  {self.unit}".rstrip(" ·"))
        sub.setStyleSheet("color:#7f8a99; font-size:10px;")
        lay.addWidget(sub)

    def set_value(self, text: str) -> None:
        self.value_label.setText(text)


# --------------------------------------------------------------------------- #
#  Device card (left dock) — identity + status, no values
# --------------------------------------------------------------------------- #
class DeviceCard(QFrame):
    def __init__(self, desc: SourceDescriptor, active: bool, on_action,
                 on_configure=None, parent=None):
        super().__init__(parent)
        self.setObjectName("DeviceCard")
        self.setStyleSheet(
            "#DeviceCard { background:#171c26; border:1px solid #232a38;"
            " border-radius:10px; }"
        )
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(5)

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
        if active and on_configure is not None and desc.controls:
            cfg = QPushButton("Configure…")
            cfg.clicked.connect(lambda: on_configure(desc.instance_id))
            header.addWidget(cfg)
        btn = QPushButton("Add" if not active else "Remove")
        btn.setFixedWidth(84)
        btn.clicked.connect(lambda: on_action(desc.instance_id))
        header.addWidget(btn)
        lay.addLayout(header)

        bits = [desc.status.value]
        if desc.firmware:
            bits.append(f"fw {desc.firmware}")
        if desc.hardware_id:
            bits.append(desc.hardware_id)
        if desc.last_error:
            bits.append(f"⚠ {desc.last_error}")
        n = len(desc.channels)
        if n:
            bits.append(f"{n} channel{'s' if n != 1 else ''}")
        info = QLabel("   ·   ".join(bits))
        info.setStyleSheet("color:#8b95a4; font-size:11px;")
        lay.addWidget(info)


# --------------------------------------------------------------------------- #
#  Configuration dialog (generated from the descriptor)
# --------------------------------------------------------------------------- #
class ConfigDialog(QDialog):
    """A device's configuration view, generated from its descriptor: editable
    name, read-only identity, sampling rate, and a form of declared controls."""

    def __init__(self, manager: SourceManager, instance_id: str, parent=None):
        super().__init__(parent)
        self.manager = manager
        self.instance_id = instance_id
        self.setWindowTitle("Configure source")
        self.setMinimumWidth(440)
        self._setpoint_labels: dict[str, tuple] = {}
        self._control_widgets: dict[str, QWidget] = {}
        self._info = QLabel()
        self._info.setStyleSheet("color:#8b95a4; font-size:11px;")
        self._info.setWordWrap(True)

        self._build(manager.descriptor(instance_id))
        manager.active_changed.connect(self._refresh)

    def _build(self, desc: SourceDescriptor) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(10)

        title = QLabel(desc.name if desc else self.instance_id)
        title.setStyleSheet("font-size:15px; font-weight:700;")
        root.addWidget(title)

        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("Name"))
        self._name_edit = QLineEdit(desc.name if desc else "")
        name_row.addWidget(self._name_edit, 1)
        rn = QPushButton("Rename")
        rn.clicked.connect(
            lambda: self.manager.rename(
                self.instance_id, self._name_edit.text().strip() or self.instance_id
            )
        )
        name_row.addWidget(rn)
        root.addLayout(name_row)
        root.addWidget(self._info)

        if desc and desc.rate and desc.rate.mode == RateMode.SETTABLE:
            srow = QHBoxLayout()
            srow.addWidget(QLabel("Sample rate"))
            spin = QDoubleSpinBox()
            spin.setRange(desc.rate.min_hz or 0.01, desc.rate.max_hz or 1000.0)
            spin.setDecimals(2)
            spin.setSingleStep(0.1)
            spin.setSuffix(" Hz")
            spin.setValue(desc.rate_hz or desc.rate.default_hz or 1.0)
            spin.valueChanged.connect(
                lambda hz: self.manager.set_rate(self.instance_id, hz)
            )
            srow.addWidget(spin)
            srow.addStretch(1)
            root.addLayout(srow)

        if desc and desc.controls:
            hdr = QLabel("Controls")
            hdr.setStyleSheet("font-weight:700; margin-top:2px;")
            root.addWidget(hdr)
            card = QFrame()
            card.setObjectName("CtrlCard")
            card.setStyleSheet(
                "#CtrlCard { background:#171c26; border:1px solid #232a38;"
                " border-radius:8px; }"
            )
            grid = QGridLayout(card)
            grid.setContentsMargins(10, 8, 10, 8)
            grid.setHorizontalSpacing(10)
            grid.setVerticalSpacing(8)
            for r, c in enumerate(desc.controls):
                lbl = QLabel(c.name)
                lbl.setStyleSheet("font-weight:600;")
                grid.addWidget(lbl, r, 0)
                grid.addWidget(self._control_widget(c), r, 1)
            root.addWidget(card)

        btnrow = QHBoxLayout()
        btnrow.addStretch(1)
        close = QPushButton("Close")
        close.clicked.connect(self.close)
        btnrow.addWidget(close)
        root.addLayout(btnrow)
        self._update_info(desc)

    def _control_widget(self, c) -> QWidget:
        iid = self.instance_id
        if c.kind == ControlKind.ACTION:
            b = QPushButton(f"Trigger {c.name}")
            b.clicked.connect(lambda _=False, cid=c.id: self.manager.invoke(iid, cid))
            return b
        if c.kind == ControlKind.TOGGLE:
            chk = QCheckBox("on")
            chk.setChecked(bool(c.value))
            chk.toggled.connect(lambda on, cid=c.id: self.manager.invoke(iid, cid, on))
            self._control_widgets[c.id] = chk
            return chk
        if c.kind == ControlKind.ENUM:
            combo = QComboBox()
            opts = list(c.params[0].options) if c.params else []
            combo.addItems(opts)
            if c.value in opts:
                combo.setCurrentText(c.value)
            combo.currentTextChanged.connect(
                lambda txt, cid=c.id: self.manager.invoke(iid, cid, txt)
            )
            self._control_widgets[c.id] = combo
            return combo
        unit = c.params[0].unit if c.params else ""
        edit = QLineEdit("" if c.value is None else f"{c.value:g}")
        edit.setFixedWidth(110)
        apply = QPushButton("Apply")
        cur = QLabel()
        cur.setStyleSheet("color:#8b95a4; font-size:11px;")
        self._setpoint_labels[c.id] = (cur, unit)
        self._set_current_label(cur, c.value, unit)

        def _apply(_=False, cid=c.id, e=edit):
            try:
                val = float(e.text())
            except ValueError:
                return
            self.manager.invoke(iid, cid, val)

        apply.clicked.connect(_apply)
        edit.returnPressed.connect(_apply)
        host = QWidget()
        cell = QHBoxLayout(host)
        cell.setContentsMargins(0, 0, 0, 0)
        cell.addWidget(edit)
        cell.addWidget(QLabel(unit))
        cell.addWidget(apply)
        cell.addWidget(cur)
        cell.addStretch(1)
        return host

    @staticmethod
    def _set_current_label(label: QLabel, value, unit: str) -> None:
        v = "—" if value is None else f"{value:g}"
        label.setText(f"current: {v} {unit}".rstrip())

    def _update_info(self, desc: SourceDescriptor) -> None:
        if desc is None:
            return
        bits = [f"driver {desc.driver}", f"iface {desc.interface.kind}"]
        if desc.interface.params:
            bits.append(", ".join(f"{k}={v}" for k, v in desc.interface.params.items()))
        if desc.hardware_id:
            bits.append(desc.hardware_id)
        if desc.firmware:
            bits.append(f"fw {desc.firmware}")
        bits.append(f"status: {desc.status.value}")
        self._info.setText("   ·   ".join(bits))

    def _refresh(self) -> None:
        if not self.manager.is_active(self.instance_id):
            self.close()
            return
        desc = self.manager.descriptor(self.instance_id)
        if desc is None:
            return
        self._update_info(desc)
        for c in desc.controls:
            w = self._control_widgets.get(c.id)
            if c.kind == ControlKind.SETPOINT and c.id in self._setpoint_labels:
                lbl, unit = self._setpoint_labels[c.id]
                self._set_current_label(lbl, c.value, unit)
            elif c.kind == ControlKind.TOGGLE and w is not None:
                w.blockSignals(True)
                w.setChecked(bool(c.value))
                w.blockSignals(False)
            elif c.kind == ControlKind.ENUM and w is not None and c.value:
                w.blockSignals(True)
                w.setCurrentText(c.value)
                w.blockSignals(False)

    def closeEvent(self, event):  # noqa: N802
        try:
            self.manager.active_changed.disconnect(self._refresh)
        except Exception:
            pass
        super().closeEvent(event)


# --------------------------------------------------------------------------- #
#  Live chart — a data-plane SINK; plots only *routed* channels
# --------------------------------------------------------------------------- #
class ChartPanel(QWidget):
    def __init__(self, engine: Engine, parent=None):
        super().__init__(parent)
        self.engine = engine
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.plot = pg.PlotWidget()
        self.plot.showGrid(x=True, y=True, alpha=0.25)
        self.plot.setLabel("bottom", "Time", units="s")
        self.plot.getAxis("bottom").enableAutoSIPrefix(False)
        self.plot.setLogMode(x=False, y=True)
        self.plot.addLegend(offset=(-10, 10))
        item = self.plot.getPlotItem()
        item.setDownsampling(auto=True, mode="peak")
        item.setClipToView(True)
        lay.addWidget(self.plot)

        self._curves: dict = {}
        self._buf: dict = {}
        self._routed: set = set()    # channel keys allowed on the chart
        self._t0 = None
        engine.subscribe(self._feed)

    def _feed(self, batch: list) -> None:    # GUI thread (engine drain)
        for r in batch:
            if r.key not in self._routed:
                continue
            if self._t0 is None:
                self._t0 = r.t
            xs, ys = self._buf.setdefault(r.key, ([], []))
            xs.append(r.t - self._t0)
            ok = r.status == 0 and r.value == r.value and r.value > 0
            ys.append(r.value if ok else float("nan"))
            curve = self._curves.get(r.key)
            if curve is None:
                curve = self.plot.plot(
                    [], [], pen=pg.mkPen(_color_for(r.key), width=2), name=r.key
                )
                self._curves[r.key] = curve
            curve.setData(xs, ys, connect="finite")

    def set_routed(self, keys: set) -> None:
        """Authoritative set of channel keys to plot; drops the rest."""
        self._routed = set(keys)
        for key in list(self._curves):
            if key not in self._routed:
                self.plot.removeItem(self._curves.pop(key))
                self._buf.pop(key, None)


# --------------------------------------------------------------------------- #
#  Sources panel (left dock) — device management
# --------------------------------------------------------------------------- #
class SourcesPanel(QWidget):
    def __init__(self, manager: SourceManager, on_configure, parent=None):
        super().__init__(parent)
        self.manager = manager
        self.on_configure = on_configure

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)
        self._avail_label, avail_scroll, self._avail_layout = self._section("Available")
        self._active_label, active_scroll, self._active_layout = self._section("Active")
        root.addWidget(self._avail_label)
        root.addWidget(avail_scroll, 1)
        root.addWidget(self._active_label)
        root.addWidget(active_scroll, 2)

        manager.available_changed.connect(self._rebuild_available)
        manager.active_changed.connect(self._rebuild_active)
        self._rebuild_available()
        self._rebuild_active()

    def _section(self, title):
        label = QLabel(title)
        label.setStyleSheet("font-size:12px; font-weight:700; color:#c7d0db;")
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        host = QWidget()
        v = QVBoxLayout(host)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(6)
        v.addStretch(1)
        scroll.setWidget(host)
        return label, scroll, v

    def _rebuild_available(self):
        descs = self.manager.available_descriptors()
        self._fill(self._avail_layout, descs, active=False)
        self._avail_label.setText(f"Available  ({len(descs)})")

    def _rebuild_active(self):
        descs = self.manager.active_descriptors()
        self._fill(self._active_layout, descs, active=True)
        self._active_label.setText(f"Active  ({len(descs)})")

    def _fill(self, layout, descs, active):
        _clear(layout)
        on_action = self.manager.remove if active else self.manager.add
        for desc in sorted(descs, key=lambda d: d.name):
            layout.addWidget(
                DeviceCard(desc, active, on_action,
                           self.on_configure if active else None)
            )
        layout.addStretch(1)


# --------------------------------------------------------------------------- #
#  Channels panel (right dock) — auto-populated; routes to the chart
# --------------------------------------------------------------------------- #
class ChannelsPanel(QWidget):
    def __init__(self, manager: SourceManager, chart: ChartPanel, parent=None):
        super().__init__(parent)
        self.manager = manager
        self.chart = chart
        self._cards: dict[str, ChannelCard] = {}
        self._routed_state: dict[str, bool] = {}   # persists across rebuilds
        self._current_keys: list[str] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)
        self._label = QLabel("Channels")
        self._label.setStyleSheet("font-size:12px; font-weight:700; color:#c7d0db;")
        root.addWidget(self._label)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        host = QWidget()
        self._layout = QVBoxLayout(host)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(6)
        self._placeholder = QLabel(
            "No active sources yet.\nOpen Sources (toolbar / View menu) to add a device."
        )
        self._placeholder.setStyleSheet("color:#7f8a99;")
        self._placeholder.setWordWrap(True)
        self._layout.addWidget(self._placeholder)
        self._layout.addStretch(1)
        scroll.setWidget(host)
        root.addWidget(scroll, 1)

        manager.active_changed.connect(self._on_active_changed)

    def _channel_list(self):
        out = []
        for d in self.manager.active_descriptors():
            for ch in d.channels:
                out.append((f"{d.instance_id}/{ch.id}", ch, d.name))
        return out

    def _on_active_changed(self):
        items = self._channel_list()
        keys = [k for k, _, _ in items]
        if keys != self._current_keys:    # only rebuild when the channel set changes
            self._rebuild(items)
            self._current_keys = keys
        self._recompute_routed()

    def _rebuild(self, items):
        _clear(self._layout)
        self._cards = {}
        if not items:
            self._layout.addWidget(self._placeholder)
            self._placeholder.setVisible(True)
        for key, ch, dev in items:
            color = _color_for(key)
            on = self._routed_state.get(key, True)
            card = ChannelCard(key, ch, dev, color, plot_on=on)
            card.plot_toggled.connect(self._on_plot_toggled)
            self._cards[key] = card
            self._layout.addWidget(card)
        self._layout.addStretch(1)
        self._label.setText(f"Channels  ({len(items)})")

    def _on_plot_toggled(self, key: str, on: bool):
        self._routed_state[key] = on
        self._recompute_routed()

    def _recompute_routed(self):
        routed = {k for k in self._cards if self._routed_state.get(k, True)}
        self.chart.set_routed(routed)

    def update_live(self, latest: dict):
        for key, card in self._cards.items():
            r = latest.get(key)
            if r is not None:
                card.set_value(_fmt(r.value, card.unit))


# --------------------------------------------------------------------------- #
#  Main window — dockable shell
# --------------------------------------------------------------------------- #
class MainWindow(QMainWindow):
    def __init__(self, manager: SourceManager, engine: Engine, parent=None):
        super().__init__(parent)
        self.manager = manager
        self.engine = engine
        self.setWindowTitle("ferroDAC")
        self.resize(1280, 820)
        self._dialogs: dict[str, ConfigDialog] = {}

        self.chart = ChartPanel(engine)
        self.setCentralWidget(self.chart)

        self.channels_panel = ChannelsPanel(manager, self.chart)
        self.channels_dock = QDockWidget("Channels", self)
        self.channels_dock.setObjectName("ChannelsDock")
        self.channels_dock.setWidget(self.channels_panel)
        self.channels_dock.setMinimumWidth(260)
        self.addDockWidget(Qt.RightDockWidgetArea, self.channels_dock)

        self.sources_panel = SourcesPanel(manager, self._open_config)
        self.sources_dock = QDockWidget("Sources", self)
        self.sources_dock.setObjectName("SourcesDock")
        self.sources_dock.setWidget(self.sources_panel)
        self.sources_dock.setMinimumWidth(280)
        self.addDockWidget(Qt.LeftDockWidgetArea, self.sources_dock)
        self.sources_dock.setVisible(False)   # separate; opened via the Sources button

        view = self.menuBar().addMenu("&View")
        view.addAction(self.sources_dock.toggleViewAction())
        view.addAction(self.channels_dock.toggleViewAction())
        toolbar = self.addToolBar("Main")
        toolbar.setMovable(False)
        toolbar.addAction(self.sources_dock.toggleViewAction())

        self.engine.tick.connect(self._on_tick)
        self.statusBar().showMessage(
            "Scanning for sources…  ·  open “Sources” to add a device"
        )
        self.manager.start()

    def _on_tick(self):
        self.channels_panel.update_live(self.engine.latest())

    def _open_config(self, instance_id: str) -> None:
        dlg = self._dialogs.get(instance_id)
        if dlg is not None:
            dlg.raise_()
            dlg.activateWindow()
            return
        dlg = ConfigDialog(self.manager, instance_id, self)
        dlg.setAttribute(Qt.WA_DeleteOnClose, True)
        dlg.destroyed.connect(lambda *_: self._dialogs.pop(instance_id, None))
        self._dialogs[instance_id] = dlg
        dlg.show()

    def closeEvent(self, event):  # noqa: N802
        self.manager.stop()
        self.engine.shutdown()
        super().closeEvent(event)


# --------------------------------------------------------------------------- #
#  Bootstrap / theming
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
        QDockWidget { titlebar-close-icon: none; }
        QDockWidget::title { background:#171c26; padding:5px 8px; font-weight:700; }
        QToolBar { background:#11151c; border:none; spacing:6px; padding:4px; }
        """
    )


def main(argv=None) -> int:
    import sys

    app = QApplication(sys.argv if argv is None else argv)
    app.setApplicationName("ferroDAC")
    apply_dark_theme(app)

    drivers = load_builtin_drivers()
    engine = Engine()
    manager = SourceManager(drivers, engine=engine)
    win = MainWindow(manager, engine)
    win.show()
    return app.exec()
