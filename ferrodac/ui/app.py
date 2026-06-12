"""ferroDAC UI — an IDE-style dockable shell.

Layout:
  - central : a dockable **workspace** of panels (charts / 7-seg displays …).
    Panels move/resize/tile natively; an "Edit layout" toggle locks them and
    hides their title bars for clean interaction.
  - left dock "Sources" : device management (hidden by default; toolbar button).
  - right dock "Channels" : one card per channel of every active device, each
    with a "Route ▾" dropdown selecting which panel(s) it feeds.
"""

from __future__ import annotations

from .. import __version__
from .. import _qtbinding  # noqa: F401  selects QT_API before qtpy import

from qtpy.QtCore import Qt
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
    QMenu,
    QPushButton,
    QScrollArea,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..core.engine import Engine
from ..core.manager import SourceManager
from ..core.registry import load_builtin_drivers
from ..core.source import ControlKind, RateMode, SourceDescriptor
from ._common import STATUS_COLORS, clear_layout, color_for, fmt
from .panels import PANEL_TYPES
from .workspace import Dashboard, WorkspaceArea


# --------------------------------------------------------------------------- #
#  Channel card (right dock) — live value + routing dropdown
# --------------------------------------------------------------------------- #
class ChannelCard(QFrame):
    def __init__(self, key, channel, device_name, color, panels, routed, on_route,
                 parent=None):
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
        top.addWidget(swatch)
        top.addWidget(name)
        top.addStretch(1)

        route = QToolButton()
        route.setText("Route ▾")
        route.setPopupMode(QToolButton.InstantPopup)
        menu = QMenu(route)
        if panels:
            for pid, title in panels:
                act = menu.addAction(title)
                act.setCheckable(True)
                act.setChecked(pid in routed)
                act.toggled.connect(lambda on, pid=pid: on_route(pid, on))
        else:
            a = menu.addAction("(add a panel first)")
            a.setEnabled(False)
        route.setMenu(menu)
        top.addWidget(route)
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
#  Device card (left dock)
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
#  Sources panel (left dock)
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
        clear_layout(layout)
        on_action = self.manager.remove if active else self.manager.add
        for desc in sorted(descs, key=lambda d: d.name):
            layout.addWidget(
                DeviceCard(desc, active, on_action,
                           self.on_configure if active else None)
            )
        layout.addStretch(1)


# --------------------------------------------------------------------------- #
#  Channels panel (right dock)
# --------------------------------------------------------------------------- #
class ChannelsPanel(QWidget):
    def __init__(self, manager: SourceManager, dashboard: Dashboard, parent=None):
        super().__init__(parent)
        self.manager = manager
        self.dashboard = dashboard
        self._cards: dict[str, ChannelCard] = {}
        self._keys: list[str] = []

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
        dashboard.panels_changed.connect(self._refresh_routes)

    def _items(self):
        out = []
        for d in self.manager.active_descriptors():
            for ch in d.channels:
                out.append((f"{d.instance_id}/{ch.id}", ch, d.name))
        return out

    def _on_active_changed(self):
        items = self._items()
        keys = [k for k, _, _ in items]
        chmap = {k: ch for k, ch, _ in items}
        prev, cur = set(self._keys), set(keys)
        for k in prev - cur:
            self.dashboard.remove_channel(k)
        for k in cur - prev:
            self.dashboard.ensure_channel(k, chmap[k])
        if keys != self._keys:
            self._rebuild(items)
            self._keys = keys

    def _refresh_routes(self):
        self._rebuild(self._items())

    def _rebuild(self, items):
        clear_layout(self._layout)
        self._cards = {}
        if not items:
            self._layout.addWidget(self._placeholder)
            self._placeholder.setVisible(True)
        panels = self.dashboard.panels()
        for key, ch, dev in items:
            card = ChannelCard(
                key, ch, dev, color_for(key), panels, self.dashboard.routed(key),
                lambda pid, on, key=key, ch=ch: self.dashboard.set_route(key, ch, pid, on),
            )
            self._cards[key] = card
            self._layout.addWidget(card)
        self._layout.addStretch(1)
        self._label.setText(f"Channels  ({len(items)})")

    def update_live(self, latest: dict):
        for key, card in self._cards.items():
            r = latest.get(key)
            if r is not None:
                card.set_value(fmt(r.value, card.unit))


# --------------------------------------------------------------------------- #
#  Main window — dockable shell
# --------------------------------------------------------------------------- #
class MainWindow(QMainWindow):
    def __init__(self, manager: SourceManager, engine: Engine, parent=None):
        super().__init__(parent)
        self.manager = manager
        self.engine = engine
        self.setWindowTitle("ferroDAC")
        self.resize(1320, 840)
        self._dialogs: dict[str, ConfigDialog] = {}

        # central = dockable workspace of panels
        self.workspace = WorkspaceArea()
        self.setCentralWidget(self.workspace)
        self.dashboard = Dashboard(self.workspace, engine)
        self.dashboard.add_panel("chart")     # a default chart to route into

        # right dock: channels
        self.channels_panel = ChannelsPanel(manager, self.dashboard)
        self.channels_dock = QDockWidget("Channels", self)
        self.channels_dock.setObjectName("ChannelsDock")
        self.channels_dock.setWidget(self.channels_panel)
        self.channels_dock.setMinimumWidth(280)
        self.addDockWidget(Qt.RightDockWidgetArea, self.channels_dock)

        # left dock: sources (hidden by default)
        self.sources_panel = SourcesPanel(manager, self._open_config)
        self.sources_dock = QDockWidget("Sources", self)
        self.sources_dock.setObjectName("SourcesDock")
        self.sources_dock.setWidget(self.sources_panel)
        self.sources_dock.setMinimumWidth(300)
        self.addDockWidget(Qt.LeftDockWidgetArea, self.sources_dock)
        self.sources_dock.setVisible(False)

        self._build_menus()

        self.engine.tick.connect(self._on_tick)
        self.statusBar().showMessage(
            "Scanning for sources…  ·  open “Sources” to add a device"
        )
        self.manager.start()

    def _build_menus(self):
        view = self.menuBar().addMenu("&View")
        view.addAction(self.sources_dock.toggleViewAction())
        view.addAction(self.channels_dock.toggleViewAction())
        view.addSeparator()
        self.edit_action = view.addAction("Edit layout")
        self.edit_action.setCheckable(True)
        self.edit_action.setChecked(True)
        self.edit_action.toggled.connect(self.dashboard.set_edit_mode)

        add = self.menuBar().addMenu("&Add")
        for kind, (label, _cls) in PANEL_TYPES.items():
            act = add.addAction(f"Add {label}")
            act.triggered.connect(lambda _=False, k=kind: self.dashboard.add_panel(k))

        tb = self.addToolBar("Main")
        tb.setMovable(False)
        tb.addAction(self.sources_dock.toggleViewAction())
        tb.addAction(self.edit_action)

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
        QPushButton, QToolButton { background:#222b3a; border:1px solid #2c374a;
            border-radius:7px; padding:5px 10px; }
        QPushButton:hover:enabled, QToolButton:hover:enabled { background:#2b3850; }
        QToolButton::menu-indicator { image: none; }
        QStatusBar { color:#8b95a4; }
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
