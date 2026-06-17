"""ferroDAC UI — an IDE-style dockable shell.

  - central : a dockable **workspace** of panels (charts / 7-seg / inputs).
  - left dock "Devices" : device management (hidden by default; toolbar button).
  - right dock "Sources" : one card per data-output Source of every active
    device, each with a "Route ▾" dropdown selecting which panel(s) it feeds.
"""

from __future__ import annotations

import json
import os
import time

from .. import __version__
from .. import _qtbinding  # noqa: F401  selects QT_API before qtpy import

from qtpy.QtCore import QByteArray, QRect, QSettings, Qt, QTimer, Signal
from qtpy.QtGui import QColor, QIcon, QImage, QPainter, QPalette, QPen, QPixmap
from qtpy.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDockWidget,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..core.engine import Engine
from ..core.history import HistoryBuffer
from ..core.manager import DeviceManager
from ..core.markers import RECORDING
from ..core import recorder as rec
from ..core.recorder import Recorder
from ..core.registry import load_builtin_drivers
from ..core.device import DeviceDescriptor, RateMode, SinkKind
from ..vision.detector import FAIL_LABELS, PARSE_LABELS, WHITELIST_PRESETS, Detector
from ..vision.ocr import available_engines, get_engine, ocr_backend, qimage_to_rgb
from ._common import STATUS_COLORS, clear_layout, color_for, fmt
from .hubclient import ConnectHubDialog, HubController
from .panels import PANEL_TYPES
from .workspace import Dashboard, WorkspaceArea


# --------------------------------------------------------------------------- #
#  Source card (right dock) — live value + routing dropdown
# --------------------------------------------------------------------------- #
class SourceCard(QFrame):
    """One source port (device output or virtual input), with a Route dropdown
    listing datatype-compatible sinks."""

    def __init__(self, port, color, sinks, routed, on_route, parent=None):
        super().__init__(parent)
        self.key = port.key
        self.unit = port.unit or ""
        self.dtype = port.dtype
        self.online = getattr(port, "online", True)
        self.setObjectName("SourceCard")
        border = "#232a38" if self.online else "#3a2f24"
        self.setStyleSheet(
            "#SourceCard { background:#171c26; border:1px solid " + border + ";"
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
        name = QLabel(port.name)
        name.setStyleSheet("font-weight:700;")
        top.addWidget(swatch)
        top.addWidget(name)
        top.addStretch(1)

        route = QToolButton()
        route.setText("Route ▾")
        route.setPopupMode(QToolButton.InstantPopup)
        menu = QMenu(route)
        if sinks:
            for skey, title in sinks:
                act = menu.addAction(title)
                act.setCheckable(True)
                act.setChecked(skey in routed)
                act.toggled.connect(lambda on, skey=skey: on_route(skey, on))
        else:
            a = menu.addAction("(no compatible sinks)")
            a.setEnabled(False)
        route.setMenu(menu)
        top.addWidget(route)
        lay.addLayout(top)

        self.value_label = QLabel("—")
        self.value_label.setStyleSheet(
            f"color:{color}; font-family:monospace; font-size:15px;"
        )
        lay.addWidget(self.value_label)

        bits = [port.origin, port.dtype]
        if self.unit:
            bits.append(self.unit)
        if not self.online:
            bits.append("offline")
        sub = QLabel("  ·  ".join(bits))
        sub.setStyleSheet("color:#7f8a99; font-size:10px;")
        lay.addWidget(sub)
        if not self.online:
            self.value_label.setText("offline")
            self.value_label.setStyleSheet(
                "color:#caa472; font-family:monospace; font-size:15px;")

    def set_value(self, text: str) -> None:
        self.value_label.setText(text)

    def set_live(self, value) -> None:
        if not self.online:
            return
        if self.dtype == "image":
            if isinstance(value, QImage) and not value.isNull():
                self.value_label.setText(f"▷ {value.width()}×{value.height()}")
            else:
                self.value_label.setText("▷ live")
        elif self.dtype == "trace":
            if hasattr(value, "peak"):
                self.value_label.setText(
                    f"▆ {len(value)} pts · max {fmt(value.peak, self.unit)}")
            else:
                self.value_label.setText("▆ trace")
        elif self.dtype == "string":
            self.value_label.setText(str(value) if value not in (None, "") else "—")
        elif isinstance(value, bool):
            self.value_label.setText("on" if value else "off")
        else:
            self.value_label.setText(fmt(value, self.unit))


# --------------------------------------------------------------------------- #
#  Device card (left dock)
# --------------------------------------------------------------------------- #
class DeviceCard(QFrame):
    def __init__(self, desc: DeviceDescriptor, active: bool, on_action,
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
        if active and on_configure is not None and (desc.sinks or desc.options):
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
        n = len(desc.sources)
        if n:
            bits.append(f"{n} source{'s' if n != 1 else ''}")
        info = QLabel("   ·   ".join(bits))
        info.setStyleSheet("color:#8b95a4; font-size:11px;")
        lay.addWidget(info)


# --------------------------------------------------------------------------- #
#  Configuration dialog (generated from the descriptor)
# --------------------------------------------------------------------------- #
class ConfigDialog(QDialog):
    def __init__(self, manager: DeviceManager, instance_id: str, parent=None):
        super().__init__(parent)
        self.manager = manager
        self.instance_id = instance_id
        self.setWindowTitle("Configure device")
        self.setMinimumWidth(440)
        self._setpoint_labels: dict[str, tuple] = {}
        self._sink_widgets: dict[str, QWidget] = {}
        self._info = QLabel()
        self._info.setStyleSheet("color:#8b95a4; font-size:11px;")
        self._info.setWordWrap(True)
        self._build(manager.descriptor(instance_id))
        manager.active_changed.connect(self._refresh)

    def _build(self, desc: DeviceDescriptor) -> None:
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

        if desc and desc.options:
            for opt in desc.options:
                orow = QHBoxLayout()
                orow.addWidget(QLabel(opt.name))
                combo = QComboBox()
                for value, label in opt.choices:
                    combo.addItem(label, value)
                ix = combo.findData(opt.value)
                if ix >= 0:
                    combo.setCurrentIndex(ix)
                combo.currentIndexChanged.connect(
                    lambda _i, c=combo, key=opt.key:
                    self.manager.set_option(self.instance_id, key, c.currentData())
                )
                orow.addWidget(combo, 1)
                root.addLayout(orow)

        if desc and desc.sinks:
            hdr = QLabel("Sinks")
            hdr.setStyleSheet("font-weight:700; margin-top:2px;")
            root.addWidget(hdr)
            card = QFrame()
            card.setObjectName("SinkCard")
            card.setStyleSheet(
                "#SinkCard { background:#171c26; border:1px solid #232a38;"
                " border-radius:8px; }"
            )
            grid = QGridLayout(card)
            grid.setContentsMargins(10, 8, 10, 8)
            grid.setHorizontalSpacing(10)
            grid.setVerticalSpacing(8)
            for r, s in enumerate(desc.sinks):
                lbl = QLabel(s.name)
                lbl.setStyleSheet("font-weight:600;")
                grid.addWidget(lbl, r, 0)
                grid.addWidget(self._sink_widget(s), r, 1)
            root.addWidget(card)

        btnrow = QHBoxLayout()
        btnrow.addStretch(1)
        close = QPushButton("Close")
        close.clicked.connect(self.close)
        btnrow.addWidget(close)
        root.addLayout(btnrow)
        self._update_info(desc)

    def _sink_widget(self, s) -> QWidget:
        iid = self.instance_id
        if s.kind == SinkKind.ACTION:
            b = QPushButton(f"Trigger {s.name}")
            b.clicked.connect(lambda _=False, sid=s.id: self.manager.write(iid, sid))
            return b
        if s.kind == SinkKind.TOGGLE:
            chk = QCheckBox("on")
            chk.setChecked(bool(s.value))
            chk.toggled.connect(lambda on, sid=s.id: self.manager.write(iid, sid, on))
            self._sink_widgets[s.id] = chk
            return chk
        if s.kind == SinkKind.ENUM:
            combo = QComboBox()
            opts = list(s.params[0].options) if s.params else []
            combo.addItems(opts)
            if s.value in opts:
                combo.setCurrentText(s.value)
            combo.currentTextChanged.connect(
                lambda txt, sid=s.id: self.manager.write(iid, sid, txt)
            )
            self._sink_widgets[s.id] = combo
            return combo
        unit = s.params[0].unit if s.params else ""
        edit = QLineEdit("" if s.value is None else f"{s.value:g}")
        edit.setFixedWidth(110)
        apply = QPushButton("Apply")
        cur = QLabel()
        cur.setStyleSheet("color:#8b95a4; font-size:11px;")
        self._setpoint_labels[s.id] = (cur, unit)
        self._set_current_label(cur, s.value, unit)

        def _apply(_=False, sid=s.id, e=edit):
            try:
                val = float(e.text())
            except ValueError:
                return
            self.manager.write(iid, sid, val)

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

    def _update_info(self, desc: DeviceDescriptor) -> None:
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
        for s in desc.sinks:
            w = self._sink_widgets.get(s.id)
            if s.kind == SinkKind.SETPOINT and s.id in self._setpoint_labels:
                lbl, unit = self._setpoint_labels[s.id]
                self._set_current_label(lbl, s.value, unit)
            elif s.kind == SinkKind.TOGGLE and w is not None:
                w.blockSignals(True)
                w.setChecked(bool(s.value))
                w.blockSignals(False)
            elif s.kind == SinkKind.ENUM and w is not None and s.value:
                w.blockSignals(True)
                w.setCurrentText(s.value)
                w.blockSignals(False)

    def closeEvent(self, event):  # noqa: N802
        try:
            self.manager.active_changed.disconnect(self._refresh)
        except Exception:
            pass
        super().closeEvent(event)


# --------------------------------------------------------------------------- #
#  Devices panel (left dock)
# --------------------------------------------------------------------------- #
class DevicesPanel(QWidget):
    def __init__(self, manager: DeviceManager, on_configure, parent=None):
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
#  Sources panel (right dock) — data outputs
# --------------------------------------------------------------------------- #
class CollapsibleGroup(QWidget):
    """A titled, collapsible container — groups cards by what created them."""

    def __init__(self, title, count, collapsed, on_toggle, parent=None):
        super().__init__(parent)
        self._title = title
        self._on_toggle = on_toggle
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(2)
        self._btn = QToolButton()
        self._btn.setText(f"{title}  ({count})")
        self._btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self._btn.setArrowType(Qt.RightArrow if collapsed else Qt.DownArrow)
        self._btn.setCursor(Qt.PointingHandCursor)
        self._btn.setStyleSheet(
            "QToolButton { color:#8b95a4; font-size:11px; font-weight:700;"
            " border:none; padding:3px 2px; text-align:left; }"
            "QToolButton:hover { color:#c7d0db; }")
        self._btn.clicked.connect(self._toggle)
        self._body = QWidget()
        self._bl = QVBoxLayout(self._body)
        self._bl.setContentsMargins(6, 0, 0, 4)
        self._bl.setSpacing(6)
        self._body.setVisible(not collapsed)
        v.addWidget(self._btn)
        v.addWidget(self._body)

    def add(self, widget):
        self._bl.addWidget(widget)

    def _toggle(self):
        vis = not self._body.isVisible()
        self._body.setVisible(vis)
        self._btn.setArrowType(Qt.DownArrow if vis else Qt.RightArrow)
        self._on_toggle(self._title, not vis)


class SourcesPanel(QWidget):
    def __init__(self, manager: DeviceManager, dashboard: Dashboard, parent=None):
        super().__init__(parent)
        self.manager = manager
        self.dashboard = dashboard
        self._cards: dict[str, SourceCard] = {}
        self._collapsed: set[str] = set()        # origins folded by the user

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)
        self._label = QLabel("Sources")
        self._label.setStyleSheet("font-size:12px; font-weight:700; color:#c7d0db;")
        root.addWidget(self._label)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        host = QWidget()
        self._layout = QVBoxLayout(host)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(6)
        self._layout.addStretch(1)
        scroll.setWidget(host)
        root.addWidget(scroll, 1)

        dashboard.ports_changed.connect(self._rebuild)
        self._rebuild()

    def _rebuild(self):
        clear_layout(self._layout)
        self._cards = {}
        ports = self.dashboard.source_ports()
        self._label.setText(f"Sources  ({len(ports)})")
        if not ports:
            ph = QLabel("No sources yet.\nAdd a device (Devices) or an input (Add menu).")
            ph.setStyleSheet("color:#7f8a99;")
            ph.setWordWrap(True)
            self._layout.addWidget(ph)
            self._layout.addStretch(1)
            return
        groups: dict[str, list] = {}             # origin -> ports (insertion order)
        for port in ports:
            groups.setdefault(port.origin or "other", []).append(port)
        for origin, gports in groups.items():
            grp = CollapsibleGroup(origin, len(gports), origin in self._collapsed,
                                   self._on_group_toggle)
            for port in gports:
                card = SourceCard(
                    port, color_for(port.key),
                    self.dashboard.compatible_sinks(port.key),
                    self.dashboard.routed(port.key),
                    lambda skey, on, key=port.key: self.dashboard.set_route(key, skey, on),
                )
                self._cards[port.key] = card
                grp.add(card)
            self._layout.addWidget(grp)
        self._layout.addStretch(1)

    def _on_group_toggle(self, origin, collapsed):
        if collapsed:
            self._collapsed.add(origin)
        else:
            self._collapsed.discard(origin)

    def update_live(self, latest: dict):
        for key, card in self._cards.items():
            r = latest.get(key)
            if r is not None:
                card.set_live(r.value)


# --------------------------------------------------------------------------- #
#  Sinks panel (right dock) — data consumers (device controls + displays)
# --------------------------------------------------------------------------- #
class SinkCard(QFrame):
    def __init__(self, port, value_text, bound, color, on_cv=None, on_peaks=None,
                 parent=None):
        super().__init__(parent)
        self.online = getattr(port, "online", True)
        if not self.online:
            value_text = "offline"
        self.setObjectName("SinkCardItem")
        border = "#232a38" if self.online else "#3a2f24"
        self.setStyleSheet(
            "#SinkCardItem { background:#171c26; border:1px solid " + border + ";"
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
        name = QLabel(port.name)
        name.setStyleSheet("font-weight:700;")
        top.addWidget(swatch)
        top.addWidget(name)
        top.addStretch(1)
        if on_cv is not None:
            det = QToolButton()
            det.setText("◎ Detections…")
            det.clicked.connect(on_cv)
            top.addWidget(det)
        if on_peaks is not None:
            pk = QToolButton()
            pk.setText("◷ Peaks…")
            pk.clicked.connect(on_peaks)
            top.addWidget(pk)
        lay.addLayout(top)

        self.value_label = QLabel(value_text)
        self.value_label.setStyleSheet(
            f"color:{color}; font-family:monospace; font-size:14px;"
        )
        lay.addWidget(self.value_label)

        bits = [port.origin, port.dtype]
        if port.unit:
            bits.append(port.unit)
        sub = QLabel("  ·  ".join(bits) + (f"   ←  {bound}" if bound else ""))
        sub.setStyleSheet("color:#7f8a99; font-size:10px;")
        sub.setWordWrap(True)
        lay.addWidget(sub)

    def set_value(self, text: str) -> None:
        self.value_label.setText(text)


class SinksPanel(QWidget):
    def __init__(self, manager: DeviceManager, dashboard: Dashboard,
                 on_cv=None, on_peaks=None, parent=None):
        super().__init__(parent)
        self.manager = manager
        self.dashboard = dashboard
        self._on_cv = on_cv
        self._on_peaks = on_peaks

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)
        self._label = QLabel("Sinks")
        self._label.setStyleSheet("font-size:12px; font-weight:700; color:#c7d0db;")
        root.addWidget(self._label)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        host = QWidget()
        self._layout = QVBoxLayout(host)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(6)
        self._layout.addStretch(1)
        scroll.setWidget(host)
        root.addWidget(scroll, 1)

        self._cards: dict = {}      # sink_key -> (SinkCard, port)
        dashboard.ports_changed.connect(self._rebuild)
        self._rebuild()

    def _device_value(self, port):
        desc = self.manager.descriptor(port.device_id)
        if desc is None:
            return "—"
        for sk in desc.sinks:
            if sk.id == port.sink_id:
                return "(action)" if sk.value is None else fmt(sk.value, port.unit)
        return "—"

    def _rebuild(self):
        clear_layout(self._layout)
        self._cards = {}
        ports = self.dashboard.sink_ports()
        for port in ports:
            if port.kind == "device":
                value_text = self._device_value(port)
                bound = self.dashboard.source_bound_to(port.key)
                bound = f"from {bound}" if bound else "unbound"
            else:
                srcs = self.dashboard.sources_into(port.key)
                value_text = f"{len(srcs)} source{'s' if len(srcs) != 1 else ''}"
                bound = ", ".join(srcs) if srcs else None
            on_cv = on_peaks = None
            if port.kind == "display" and "image" in port.accepts \
                    and self._on_cv is not None:
                on_cv = lambda _=False, k=port.key: self._on_cv(k)
            if port.kind == "display" and "trace" in port.accepts \
                    and self._on_peaks is not None:
                on_peaks = lambda _=False, k=port.key: self._on_peaks(k)
            card = SinkCard(port, value_text, bound,
                            color_for("sink:" + port.key), on_cv=on_cv,
                            on_peaks=on_peaks)
            self._cards[port.key] = (card, port)
            self._layout.addWidget(card)
        self._layout.addStretch(1)
        self._label.setText(f"Sinks  ({len(ports)})")

    def update_live(self):
        for card, port in self._cards.values():
            if port.kind == "device" and getattr(port, "online", True):
                card.set_value(self._device_value(port))


# --------------------------------------------------------------------------- #
#  Events / tags (markers shared across all charts)
# --------------------------------------------------------------------------- #
class _MarkerDialog(QDialog):
    def __init__(self, label="", comment="", parent=None):
        super().__init__(parent)
        self.setWindowTitle("Tag")
        self.setMinimumWidth(360)
        form = QFormLayout(self)
        self._label = QLineEdit(label)
        self._comment = QLineEdit(comment)
        form.addRow("Label", self._label)
        form.addRow("Comment", self._comment)
        row = QHBoxLayout()
        row.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        ok = QPushButton("OK")
        ok.setDefault(True)
        ok.clicked.connect(self.accept)
        row.addWidget(cancel)
        row.addWidget(ok)
        form.addRow(row)

    def values(self):
        return self._label.text().strip(), self._comment.text().strip()


class EventsPanel(QWidget):
    """Lists session markers (tags + record bookmarks); edit/remove."""

    def __init__(self, markers, clock, on_zoom=None, on_export_csv=None,
                 on_export_plots=None, parent=None):
        super().__init__(parent)
        self.markers = markers
        self.clock = clock
        self._on_zoom = on_zoom
        self._on_export_csv = on_export_csv
        self._on_export_plots = on_export_plots
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)
        self._label = QLabel("Events")
        self._label.setStyleSheet("font-size:12px; font-weight:700; color:#c7d0db;")
        root.addWidget(self._label)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        host = QWidget()
        self._layout = QVBoxLayout(host)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(6)
        self._layout.addStretch(1)
        scroll.setWidget(host)
        root.addWidget(scroll, 1)
        markers.changed.connect(self._rebuild)
        self._rebuild()

    def _rebuild(self):
        clear_layout(self._layout)
        ms = self.markers.all()
        if not ms:
            ph = QLabel("No events.\nDrop a tag with “＋ Tag”.")
            ph.setStyleSheet("color:#7f8a99;")
            ph.setWordWrap(True)
            self._layout.addWidget(ph)
        for m in ms:
            self._layout.addWidget(self._row(m))
        self._layout.addStretch(1)
        self._label.setText(f"Events  ({len(ms)})")

    def _row(self, m):
        card = QFrame()
        card.setObjectName("EventCard")
        card.setStyleSheet(
            "#EventCard { background:#171c26; border:1px solid #232a38;"
            " border-radius:8px; }")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(10, 6, 10, 6)
        lay.setSpacing(4)
        is_rec = m.is_region
        top = QHBoxLayout()
        top.setSpacing(6)
        dot = QLabel()
        dot.setFixedSize(10, 10)
        dot.setStyleSheet(f"background:{m.color}; border-radius:5px;")
        name = QLabel(("◧ " if is_rec else "") + m.label)
        name.setStyleSheet("font-weight:700;")
        if is_rec:
            info = QLabel(f"{self.clock.rel(m.t):.0f}–{self.clock.rel(m.t_end):.0f}s "
                          f"· {m.duration:.0f}s")
        else:
            info = QLabel(f"t={self.clock.rel(m.t):.1f}s")
        info.setStyleSheet("color:#7f8a99; font-size:10px;")
        top.addWidget(dot)
        top.addWidget(name)
        top.addStretch(1)
        top.addWidget(info)
        edit = QToolButton()
        edit.setText("✎")
        edit.clicked.connect(lambda _=False, mid=m.id: self._edit(mid))
        rm = QToolButton()
        rm.setText("✕")
        rm.clicked.connect(lambda _=False, mid=m.id: self.markers.remove(mid))
        top.addWidget(edit)
        top.addWidget(rm)
        lay.addLayout(top)
        if m.comment:
            c = QLabel(m.comment)
            c.setStyleSheet("color:#8b95a4; font-size:11px;")
            c.setWordWrap(True)
            lay.addWidget(c)
        if is_rec:
            acts = QHBoxLayout()
            acts.setSpacing(4)
            for text, cb in (("⤢ Zoom", self._on_zoom),
                             ("⬇ CSV", self._on_export_csv),
                             ("🖼 Plots", self._on_export_plots)):
                if cb is None:
                    continue
                b = QToolButton()
                b.setText(text)
                b.clicked.connect(lambda _=False, cb=cb, mid=m.id: cb(mid))
                acts.addWidget(b)
            acts.addStretch(1)
            lay.addLayout(acts)
        return card

    def _edit(self, mid):
        m = self.markers.get(mid)
        if m is None:
            return
        dlg = _MarkerDialog(m.label, m.comment, self)
        if dlg.exec():
            label, comment = dlg.values()
            self.markers.update(mid, label=label or m.label, comment=comment)


# --------------------------------------------------------------------------- #
#  CV text-detection config — the ROI editor
# --------------------------------------------------------------------------- #
class _ROIEditor(QWidget):
    """Shows a live frame; lets the user rubber-band a ROI and draws existing
    detector regions. ROIs are kept in image-pixel coordinates."""

    roi_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._img = None
        self._rois = []          # (label, (x,y,w,h), color, selected)
        self._roi = None         # current committed ROI (image coords)
        self._drag0 = self._drag1 = None
        self.setMinimumSize(480, 340)

    def set_frame(self, img):
        self._img = img
        self.update()

    def set_rois(self, rois):
        self._rois = rois
        self.update()

    def current_roi(self):
        return self._roi

    def set_current_roi(self, roi):
        self._roi = roi
        self.update()

    # -- coordinate mapping --------------------------------------------------
    def _content_rect(self) -> QRect:
        if self._img is None or self._img.isNull():
            return self.rect()
        iw, ih = self._img.width(), self._img.height()
        if iw == 0 or ih == 0:
            return self.rect()
        s = min(self.width() / iw, self.height() / ih)
        w, h = int(iw * s), int(ih * s)
        return QRect((self.width() - w) // 2, (self.height() - h) // 2, w, h)

    def _to_image(self, pt):
        cr = self._content_rect()
        if self._img is None or cr.width() == 0 or cr.height() == 0:
            return (0, 0)
        iw, ih = self._img.width(), self._img.height()
        x = (pt.x() - cr.x()) * iw / cr.width()
        y = (pt.y() - cr.y()) * ih / cr.height()
        return (max(0, min(iw, x)), max(0, min(ih, y)))

    def _to_widget(self, roi) -> QRect:
        cr = self._content_rect()
        if self._img is None:
            return QRect()
        iw, ih = self._img.width(), self._img.height()
        x, y, w, h = roi
        sx, sy = cr.width() / iw, cr.height() / ih
        return QRect(int(cr.x() + x * sx), int(cr.y() + y * sy),
                     int(w * sx), int(h * sy))

    # -- mouse ---------------------------------------------------------------
    def mousePressEvent(self, e):  # noqa: N802
        if self._img is not None and not self._img.isNull():
            self._drag0 = self._drag1 = e.pos()
            self.update()

    def mouseMoveEvent(self, e):  # noqa: N802
        if self._drag0 is not None:
            self._drag1 = e.pos()
            self.update()

    def mouseReleaseEvent(self, e):  # noqa: N802
        if self._drag0 is None:
            return
        p0, p1 = self._to_image(self._drag0), self._to_image(e.pos())
        x, y = int(min(p0[0], p1[0])), int(min(p0[1], p1[1]))
        w, h = int(abs(p1[0] - p0[0])), int(abs(p1[1] - p0[1]))
        self._drag0 = self._drag1 = None
        if w >= 4 and h >= 4:
            self._roi = (x, y, w, h)
            self.roi_changed.emit()
        self.update()

    def paintEvent(self, _ev):  # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#0b0e13"))
        if self._img is None or self._img.isNull():
            p.setPen(QColor("#5b6b7f"))
            p.drawText(self.rect(), Qt.AlignCenter, "waiting for camera frames…")
            return
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        p.drawImage(self._content_rect(), self._img)
        for label, roi, color, selected in self._rois:
            r = self._to_widget(roi)
            pen = QPen(QColor(color))
            pen.setWidth(3 if selected else 2)
            p.setPen(pen)
            p.drawRect(r)
            p.fillRect(QRect(r.x(), r.y() - 16, max(36, len(label) * 8), 15),
                       QColor(color))
            p.setPen(QColor("#0b0e13"))
            p.drawText(r.x() + 3, r.y() - 4, label)
        if self._roi is not None:
            pen = QPen(QColor("#4fc3f7"))
            pen.setStyle(Qt.DashLine)
            pen.setWidth(2)
            p.setPen(pen)
            p.drawRect(self._to_widget(self._roi))
        if self._drag0 is not None and self._drag1 is not None:
            pen = QPen(QColor("#ffd54f"))
            pen.setStyle(Qt.DashLine)
            pen.setWidth(2)
            p.setPen(pen)
            p.drawRect(QRect(self._drag0, self._drag1))


class ImageConfigDialog(QDialog):
    """Add/edit OCR text-detection sources on one image (camera) display sink."""

    def __init__(self, dashboard: Dashboard, sink_key: str, parent=None):
        super().__init__(parent)
        self.dashboard = dashboard
        self.sink_key = sink_key
        sink = dashboard._sinks.get(sink_key)
        self.panel = sink.panel if sink is not None else None
        self.setWindowTitle(f"Text detection — {sink.name if sink else ''}")
        self.setMinimumSize(940, 580)

        root = QHBoxLayout(self)
        left = QVBoxLayout()
        self.editor = _ROIEditor()
        left.addWidget(self.editor, 1)
        hint = QLabel("Drag a box over the value to read, set the options, "
                      "then “Add detection”.  OCR: " + ocr_backend())
        hint.setStyleSheet("color:#8b95a4; font-size:11px;")
        hint.setWordWrap(True)
        left.addWidget(hint)
        root.addLayout(left, 3)
        root.addLayout(self._build_form(), 2)

        self._selected_did = None
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh_frame)
        self._timer.start(150)
        self.editor.roi_changed.connect(lambda: self._test_read())
        self._reload_list()

    # -- form ----------------------------------------------------------------
    def _spin(self, lo, hi, val, step=1.0, decimals=0, prefix="", suffix=""):
        s = QDoubleSpinBox() if decimals else QSpinBox()
        s.setRange(lo, hi)
        s.setValue(val)
        if decimals:
            s.setDecimals(decimals)
        s.setSingleStep(step)
        if prefix:
            s.setPrefix(prefix)
        if suffix:
            s.setSuffix(suffix)
        return s

    def _build_form(self):
        right = QVBoxLayout()
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)
        self._name = QLineEdit("Reading")
        self._unit = QLineEdit()
        self._unit.setPlaceholderText("unit")
        self._unit.setFixedWidth(70)
        form.addRow("Name", self._pair(self._name, self._unit, 1, 0))
        self._engine = QComboBox()
        for key, label in (available_engines() or [("tesseract", "Tesseract")]):
            self._engine.addItem(label, key)
        form.addRow("Engine", self._engine)
        self._type = QComboBox()
        for val, label in PARSE_LABELS:
            self._type.addItem(label, val)
        self._type.currentIndexChanged.connect(self._on_type)
        self._fail = QComboBox()
        for val, label in FAIL_LABELS:
            self._fail.addItem(label, val)
        form.addRow("Type", self._pair(self._type, self._fail, 1, 1))
        self._whitelist = QLineEdit(WHITELIST_PRESETS["float"])
        form.addRow("Whitelist", self._whitelist)

        # preprocessing
        self._invert = QCheckBox("Invert")
        self._thresh = QCheckBox("Threshold")
        self._adaptive = QCheckBox("Adaptive")
        self._denoise = QCheckBox("Denoise")
        pp = QHBoxLayout()
        for w in (self._invert, self._thresh, self._adaptive, self._denoise):
            pp.addWidget(w)
        pp.addStretch(1)
        form.addRow("Clean-up", self._wrap(pp))
        self._scale = self._spin(1, 6, 3, prefix="×")
        self._rotate = self._spin(-45, 45, 0, step=0.5, decimals=1, suffix="°")
        sr = QHBoxLayout()
        sr.addWidget(QLabel("Scale"))
        sr.addWidget(self._scale)
        sr.addWidget(QLabel("Rotate"))
        sr.addWidget(self._rotate)
        sr.addStretch(1)
        form.addRow("", self._wrap(sr))

        # value pipeline
        self._gain = self._spin(-1e6, 1e6, 1.0, step=0.1, decimals=4)
        self._offset = self._spin(-1e9, 1e9, 0.0, step=0.1, decimals=4)
        vt = QHBoxLayout()
        vt.addWidget(QLabel("gain ×"))
        vt.addWidget(self._gain)
        vt.addWidget(QLabel("+ offset"))
        vt.addWidget(self._offset)
        vt.addStretch(1)
        form.addRow("Value", self._wrap(vt))
        self._vmin = QLineEdit()
        self._vmin.setPlaceholderText("min")
        self._vmax = QLineEdit()
        self._vmax.setPlaceholderText("max")
        rg = QHBoxLayout()
        rg.addWidget(self._vmin)
        rg.addWidget(QLabel("…"))
        rg.addWidget(self._vmax)
        rg.addStretch(1)
        form.addRow("Accept range", self._wrap(rg))
        self._smooth = self._spin(1, 25, 1, suffix=" smpl")
        self._rate = self._spin(0.1, 10, 5, step=0.5, decimals=1, suffix=" Hz")
        sm = QHBoxLayout()
        sm.addWidget(QLabel("Stabilise"))
        sm.addWidget(self._smooth)
        sm.addWidget(QLabel("Rate"))
        sm.addWidget(self._rate)
        sm.addStretch(1)
        form.addRow("Sampling", self._wrap(sm))
        right.addLayout(form)

        # live preview
        self._preview = QLabel("draw a box, then Test")
        self._preview.setFixedHeight(64)
        self._preview.setAlignment(Qt.AlignCenter)
        self._preview.setStyleSheet("background:#0b0e13; border:1px solid #232a38;")
        right.addWidget(self._preview)
        prow = QHBoxLayout()
        self._result = QLabel("—")
        self._result.setStyleSheet("font-family:monospace; color:#4fc3f7;")
        self._live = QCheckBox("Live")
        self._live.setChecked(True)
        prow.addWidget(self._result, 1)
        prow.addWidget(self._live)
        right.addLayout(prow)

        row = QHBoxLayout()
        test = QPushButton("Test read")
        test.clicked.connect(lambda: self._test_read())
        self._add_btn = QPushButton("Add detection")
        self._add_btn.clicked.connect(self._add)
        row.addWidget(test)
        row.addWidget(self._add_btn)
        right.addLayout(row)

        lbl = QLabel("Detections")
        lbl.setStyleSheet("font-weight:700; margin-top:6px;")
        right.addWidget(lbl)
        self._list = QListWidget()
        self._list.currentRowChanged.connect(self._on_select)
        right.addWidget(self._list, 1)
        lrow = QHBoxLayout()
        upd = QPushButton("Update selected")
        upd.clicked.connect(self._update)
        rm = QPushButton("Remove selected")
        rm.clicked.connect(self._remove)
        lrow.addWidget(upd)
        lrow.addWidget(rm)
        right.addLayout(lrow)
        return right

    @staticmethod
    def _wrap(layout):
        w = QWidget()
        w.setLayout(layout)
        return w

    def _pair(self, a, b, sa, sb):
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(a, sa)
        row.addWidget(b, sb)
        return self._wrap(row)

    # -- behaviour -----------------------------------------------------------
    def _on_type(self):
        preset = WHITELIST_PRESETS.get(self._type.currentData(), "")
        self._whitelist.setText(preset)

    def _refresh_frame(self):
        if self.panel is not None:
            self.editor.set_frame(getattr(self.panel, "_last_img", None))
        # live preview: re-OCR every ~3rd tick (~450 ms) so the value tracks
        self._preview_tick = getattr(self, "_preview_tick", 0) + 1
        if (getattr(self, "_live", None) is not None and self._live.isChecked()
                and self.editor.current_roi() is not None
                and self._preview_tick % 3 == 0):
            self._test_read()

    @staticmethod
    def _opt_float(le):
        t = le.text().strip()
        try:
            return float(t) if t else None
        except ValueError:
            return None

    def _gather(self) -> dict:
        return dict(
            name=self._name.text().strip() or "Reading",
            unit=self._unit.text().strip(),
            engine=self._engine.currentData(),
            parse_as=self._type.currentData(),
            on_fail=self._fail.currentData(),
            whitelist=self._whitelist.text(),
            invert=self._invert.isChecked(),
            threshold=self._thresh.isChecked(),
            adaptive=self._adaptive.isChecked(),
            denoise=self._denoise.isChecked(),
            scale=self._scale.value(),
            rotate=self._rotate.value(),
            gain=self._gain.value(),
            offset=self._offset.value(),
            vmin=self._opt_float(self._vmin),
            vmax=self._opt_float(self._vmax),
            smooth=self._smooth.value(),
            rate_hz=self._rate.value(),
        )

    def _current_detector(self):
        cfg = self._gather()
        return Detector(id="_preview", sink_key=self.sink_key,
                        roi=self.editor.current_roi() or (0, 0, 1, 1), **cfg)

    def _test_read(self):
        roi = self.editor.current_roi()
        img = getattr(self.panel, "_last_img", None)
        if roi is None or img is None or img.isNull():
            self._result.setText("draw a box over a live frame first")
            return
        det = self._current_detector()
        rgb = qimage_to_rgb(img)
        text, dbg = get_engine(det.engine).read(det.crop(rgb), det)
        det.last_text = text
        value, status = det._finalize(*det._parse_raw(text))
        if dbg is not None and dbg.ndim == 2:        # show what the engine sees
            h, w = dbg.shape
            qimg = QImage(dbg.tobytes(), w, h, w, QImage.Format.Format_Grayscale8)
            self._preview.setPixmap(QPixmap.fromImage(qimg).scaled(
                self._preview.width(), self._preview.height(),
                Qt.KeepAspectRatio, Qt.SmoothTransformation))
        self._result.setText(
            f"“{text}”  →  {value}" + ("  (parse failed)" if status else ""))

    def _add(self):
        roi = self.editor.current_roi()
        if roi is None:
            self._result.setText("draw a box first")
            return
        cfg = self._gather()
        self.dashboard.add_detector(self.sink_key, roi=roi, **cfg)
        self.editor.set_current_roi(None)
        self._reload_list()

    def _on_select(self, row):
        if row < 0 or row >= self._list.count():
            return
        did = self._list.item(row).data(Qt.UserRole)
        det = self.dashboard.detector(did)
        if det is None:
            return
        self._selected_did = did
        self._name.setText(det.name)
        self._unit.setText(det.unit)
        self._engine.setCurrentIndex(max(0, self._engine.findData(det.engine)))
        self._type.setCurrentIndex(max(0, self._type.findData(det.parse_as)))
        self._fail.setCurrentIndex(max(0, self._fail.findData(det.on_fail)))
        self._whitelist.setText(det.whitelist)
        self._invert.setChecked(det.invert)
        self._thresh.setChecked(det.threshold)
        self._adaptive.setChecked(det.adaptive)
        self._denoise.setChecked(det.denoise)
        self._scale.setValue(det.scale)
        self._rotate.setValue(det.rotate)
        self._gain.setValue(det.gain)
        self._offset.setValue(det.offset)
        self._vmin.setText("" if det.vmin is None else f"{det.vmin:g}")
        self._vmax.setText("" if det.vmax is None else f"{det.vmax:g}")
        self._smooth.setValue(det.smooth)
        self._rate.setValue(det.rate_hz)
        self.editor.set_current_roi(det.roi)
        self._reload_list()

    def _update(self):
        if not self._selected_did:
            return
        cfg = self._gather()
        roi = self.editor.current_roi()
        if roi is not None:
            cfg["roi"] = roi
        self.dashboard.update_detector(self._selected_did, **cfg)
        self._reload_list()

    def _remove(self):
        if not self._selected_did:
            self._result.setText("select a detection in the list to remove it")
            return
        self.dashboard.remove_detector(self._selected_did)
        self._selected_did = None
        self.editor.set_current_roi(None)
        self._reload_list()

    def _reload_list(self):
        dets = self.dashboard.detectors_for(self.sink_key)
        self._list.blockSignals(True)
        self._list.clear()
        sel_row = -1
        for i, det in enumerate(dets):
            item = QListWidgetItem(f"{det.name}  ·  {det.parse_as}")
            item.setData(Qt.UserRole, det.id)
            self._list.addItem(item)
            if det.id == self._selected_did:
                sel_row = i
        if sel_row >= 0:
            self._list.setCurrentRow(sel_row)      # restore selection (signals off)
        else:
            self._selected_did = None
        self._list.blockSignals(False)
        rois = [(d.name, d.roi, color_for(f"cv/{d.id}"), d.id == self._selected_did)
                for d in dets]
        self.editor.set_rois(rois)

    def closeEvent(self, event):  # noqa: N802
        self._timer.stop()
        super().closeEvent(event)


# --------------------------------------------------------------------------- #
#  Trend cursors — pick peaks off a spectrum as scalar sources
# --------------------------------------------------------------------------- #
class CursorDialog(QDialog):
    """Add/remove trend cursors on a spectrum panel: each extracts a scalar
    (peak / value-at / area) from a trace at an m/z and becomes a Source."""

    def __init__(self, dashboard: Dashboard, sink_key: str, parent=None):
        super().__init__(parent)
        self.dashboard = dashboard
        self.sink_key = sink_key
        sink = dashboard._sinks.get(sink_key)
        self.panel = sink.panel if sink is not None else None
        self.setWindowTitle(f"Peaks — {sink.name if sink else ''}")
        self.setMinimumWidth(380)

        root = QVBoxLayout(self)
        form = QFormLayout()
        self._source = QComboBox()
        form.addRow("Trace", self._source)
        self._name = QLineEdit()
        self._name.setPlaceholderText("name (optional)")
        form.addRow("Name", self._name)
        self._mz = QDoubleSpinBox()
        self._mz.setRange(0, 1000)
        self._mz.setDecimals(1)
        self._mz.setValue(18)
        self._mz.setSuffix(" m/z")
        form.addRow("Mass", self._mz)
        self._mode = QComboBox()
        for v, lbl in (("peak", "Peak"), ("value", "Value at"), ("area", "Area")):
            self._mode.addItem(lbl, v)
        self._width = QDoubleSpinBox()
        self._width.setRange(0.1, 20)
        self._width.setDecimals(1)
        self._width.setValue(1.0)
        self._width.setSuffix(" u")
        er = QHBoxLayout()
        er.addWidget(self._mode)
        er.addWidget(QLabel("±"))
        er.addWidget(self._width)
        er.addStretch(1)
        w = QWidget()
        w.setLayout(er)
        form.addRow("Extract", w)
        root.addLayout(form)
        add = QPushButton("Add peak")
        add.clicked.connect(self._add)
        root.addWidget(add)
        lbl = QLabel("Peaks")
        lbl.setStyleSheet("font-weight:700; margin-top:6px;")
        root.addWidget(lbl)
        self._list = QListWidget()
        root.addWidget(self._list, 1)
        rm = QPushButton("Remove selected")
        rm.clicked.connect(self._remove)
        root.addWidget(rm)
        dashboard.ports_changed.connect(self._reload)
        self._reload()

    def _trace_sources(self):
        return list(getattr(self.panel, "_curves", {}))

    def _add(self):
        key = self._source.currentData()
        if not key:
            self._name.setPlaceholderText("route a trace source to this panel first")
            return
        self.dashboard.add_cursor(
            key, self._mz.value(), name=self._name.text().strip() or None,
            mode=self._mode.currentData(), width=self._width.value())
        self._name.clear()

    def _remove(self):
        row = self._list.currentRow()
        if row >= 0:
            self.dashboard.remove_cursor(self._list.item(row).data(Qt.UserRole))

    def _reload(self):
        keys = self._trace_sources()
        cur = self._source.currentData()
        self._source.blockSignals(True)
        self._source.clear()
        for key in keys:
            sp = self.dashboard._sources.get(key)
            self._source.addItem(sp.name if sp else key, key)
        ix = self._source.findData(cur)
        if ix >= 0:
            self._source.setCurrentIndex(ix)
        self._source.blockSignals(False)
        self._list.clear()
        for key in keys:
            for c in self.dashboard.cursors_for(key):
                item = QListWidgetItem(f"{c.name}  ·  m/z {c.mz:g} · {c.mode}")
                item.setData(Qt.UserRole, c.id)
                self._list.addItem(item)


# --------------------------------------------------------------------------- #
#  Main window — dockable shell
# --------------------------------------------------------------------------- #
class MainWindow(QMainWindow):
    def __init__(self, manager: DeviceManager, engine: Engine, parent=None,
                 restore_last: bool = True):
        super().__init__(parent)
        self.manager = manager
        self.engine = engine
        self._restore_last = restore_last
        self.setWindowTitle("ferroDAC")
        self.resize(1320, 840)
        self._dialogs: dict[str, ConfigDialog] = {}
        self._cv_dialogs: dict[str, ImageConfigDialog] = {}

        self.workspace = WorkspaceArea()
        self.setCentralWidget(self.workspace)
        self.dashboard = Dashboard(self.workspace, engine, manager)
        self.dashboard.add_panel("chart")

        # networking: publish to / consume from a hub (optional, needs grpcio)
        self.hub = HubController(self.dashboard, engine, manager, self)
        self.hub.status.connect(lambda msg: self.statusBar().showMessage(msg, 6000))

        # data plane: always-on hot history + the recorder
        self.history = HistoryBuffer()
        engine.subscribe(self.history.feed)
        self.recorder = Recorder(engine, self.history, on_change=self._on_record_change)
        self._rec_start_mid = None

        # durable store: persist EVERYTHING continuously (§7.4) so data survives a
        # restart and a span can be recorded retroactively. Degrades to the RAM
        # ring if zarr/disk is unavailable.
        self.store_writer = None
        self.resolver = None
        try:
            from ..store import RamTier, Resolver, StoreWriter, ZarrStore
            os.makedirs(self._app_dir(), exist_ok=True)
            store = ZarrStore(os.path.join(self._app_dir(), "store.zarr"))
            self.store_writer = StoreWriter(store)
            self.store_writer.attach(engine)
            # the read path: one query() over the live RAM ring + the durable store
            self.resolver = Resolver([RamTier(self.history), store])
        except Exception as exc:                       # noqa: BLE001
            import logging
            logging.getLogger("ferrodac").warning("durable store disabled: %s", exc)

        # working-session autosave (tags/layout survive restart & crashes)
        self._autosave_on = False
        self._autosave_timer = QTimer(self)
        self._autosave_timer.setSingleShot(True)
        self._autosave_timer.setInterval(1500)
        self._autosave_timer.timeout.connect(self._do_autosave)
        self.dashboard.ports_changed.connect(self._schedule_autosave)
        self.dashboard.markers.changed.connect(self._schedule_autosave)

        self.sources_panel = SourcesPanel(manager, self.dashboard)
        self.sources_dock = QDockWidget("Sources", self)
        self.sources_dock.setObjectName("SourcesDock")
        self.sources_dock.setWidget(self.sources_panel)
        self.sources_dock.setMinimumWidth(280)
        self.addDockWidget(Qt.RightDockWidgetArea, self.sources_dock)

        self.sinks_panel = SinksPanel(manager, self.dashboard,
                                      on_cv=self._open_cv_config,
                                      on_peaks=self._open_peaks_config)
        self.sinks_dock = QDockWidget("Sinks", self)
        self.sinks_dock.setObjectName("SinksDock")
        self.sinks_dock.setWidget(self.sinks_panel)
        self.sinks_dock.setMinimumWidth(280)
        self.addDockWidget(Qt.RightDockWidgetArea, self.sinks_dock)
        self.events_panel = EventsPanel(
            self.dashboard.markers, self.dashboard.clock,
            on_zoom=self._zoom_recording, on_export_csv=self._export_recording_csv,
            on_export_plots=self._export_plots)
        self.events_dock = QDockWidget("Events", self)
        self.events_dock.setObjectName("EventsDock")
        self.events_dock.setWidget(self.events_panel)
        self.events_dock.setMinimumWidth(280)
        self.addDockWidget(Qt.RightDockWidgetArea, self.events_dock)
        self.tabifyDockWidget(self.sources_dock, self.sinks_dock)
        self.tabifyDockWidget(self.sinks_dock, self.events_dock)
        self.sources_dock.raise_()

        self.devices_panel = DevicesPanel(manager, self._open_config)
        self.devices_dock = QDockWidget("Devices", self)
        self.devices_dock.setObjectName("DevicesDock")
        self.devices_dock.setWidget(self.devices_panel)
        self.devices_dock.setMinimumWidth(300)
        self.addDockWidget(Qt.LeftDockWidgetArea, self.devices_dock)
        self.devices_dock.setVisible(False)

        self._build_menus()

        self.engine.tick.connect(self._on_tick)
        self.statusBar().showMessage(
            "Scanning for devices…  ·  open “Devices” to add one"
        )
        self.manager.start()
        self._check_recovery()
        if self._restore_last:
            self._init_session_persistence()

    def _build_menus(self):
        filemenu = self.menuBar().addMenu("&File")
        filemenu.addAction("Export CSV…", self._on_export)
        filemenu.addSeparator()
        filemenu.addAction("Save Layout…", self._on_save)
        filemenu.addAction("Open Layout…", self._on_open)

        view = self.menuBar().addMenu("&View")
        view.addAction(self.devices_dock.toggleViewAction())
        view.addAction(self.sources_dock.toggleViewAction())
        view.addAction(self.sinks_dock.toggleViewAction())
        view.addAction(self.events_dock.toggleViewAction())
        view.addSeparator()
        self.edit_action = view.addAction("Edit layout")
        self.edit_action.setCheckable(True)
        self.edit_action.setChecked(False)          # start in locked layout
        self.edit_action.toggled.connect(self.dashboard.set_edit_mode)

        add = self.menuBar().addMenu("&Add")
        for kind, (label, _cls) in PANEL_TYPES.items():
            act = add.addAction(f"Add {label}")
            act.triggered.connect(lambda _=False, k=kind: self.dashboard.add_panel(k))

        netmenu = self.menuBar().addMenu("&Hub")
        self.hub_action = netmenu.addAction("Connect to hub…", self._open_hub)

        tb = self.addToolBar("Main")
        tb.setObjectName("MainToolBar")
        tb.setMovable(False)
        tb.addAction(self.devices_dock.toggleViewAction())
        tb.addAction(self.edit_action)
        tb.addSeparator()
        self.record_action = tb.addAction("● Record", self._toggle_record)
        tb.addAction("＋ Tag", self._add_tag)
        tb.addAction("🕑 Timeline", self._open_timeline)
        tb.addSeparator()
        tb.addAction(self.hub_action)

    def _open_timeline(self):
        if self.resolver is None:
            self.statusBar().showMessage("Durable store unavailable — timeline disabled", 6000)
            return
        if getattr(self, "_timeline_win", None) is None:
            from .timeline import TimelineWindow
            self._timeline_win = TimelineWindow(self.resolver, self.store_writer.store, self)
        self._timeline_win.show()
        self._timeline_win.raise_()
        self._timeline_win.activateWindow()

    def _open_hub(self):
        if not self.hub.available:
            self.statusBar().showMessage(
                "Hub needs grpcio — install it in this Python environment "
                "(pip install grpcio).", 8000)
            return
        s = QSettings("ferroDAC", "ferroDAC")
        agent, viewer = self.hub.roles
        dlg = ConnectHubDialog(
            addr=self.hub.addr or s.value("hub/addr", "localhost:50051"),
            as_agent=agent if self.hub.connected
            else s.value("hub/agent", True, type=bool),
            as_viewer=viewer if self.hub.connected
            else s.value("hub/viewer", True, type=bool),
            connected=self.hub.connected, parent=self)
        if not dlg.exec():
            return
        if dlg.disconnect_requested:
            self.hub.disconnect()
            return
        addr, as_agent, as_viewer = dlg.values()
        if addr and (as_agent or as_viewer):
            s.setValue("hub/addr", addr)            # remember for next time
            s.setValue("hub/agent", as_agent)
            s.setValue("hub/viewer", as_viewer)
            self.hub.connect(addr, as_agent, as_viewer)

    def _add_tag(self):
        dlg = _MarkerDialog(parent=self)
        if dlg.exec():
            label, comment = dlg.values()
            self.dashboard.markers.add(time.time(), label=label, comment=comment)
            self.events_dock.raise_()

    # -- record --------------------------------------------------------------
    def _app_dir(self) -> str:
        from qtpy.QtCore import QStandardPaths
        docs = QStandardPaths.writableLocation(QStandardPaths.DocumentsLocation) \
            or os.path.expanduser("~")
        return os.path.join(docs, "ferroDAC")

    def _runs_dir(self) -> str:
        return os.path.join(self._app_dir(), "runs")

    def _on_export(self):
        """File ▸ Export CSV — dumps the current in-memory history. Per-recording
        slice export lives on each recording card in the Events dock."""
        sources = self.dashboard.capture_sources()
        if not sources:
            self.statusBar().showMessage(
                "Nothing to export — route some sources to a chart first.", 5000)
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export CSV", "", "CSV (*.csv)")
        if not path:
            return
        if not path.endswith(".csv"):
            path += ".csv"
        out = rec.materialize_from_history(path, sources, self.history)
        self.statusBar().showMessage(f"Exported → {out}", 6000)

    # -- recording-region actions (from the Events dock) ---------------------
    def _zoom_recording(self, mid):
        m = self.dashboard.markers.get(mid)
        if m and m.t_end is not None:
            self.dashboard.zoom_to(m.t, m.t_end)

    def _export_recording_csv(self, mid):
        m = self.dashboard.markers.get(mid)
        if m is None or not m.run_dir:
            return
        sources = rec.run_sources(m.run_dir)
        if not sources:
            self.statusBar().showMessage("This recording has no saved data.", 5000)
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export recording", (m.label or "recording") + ".csv",
            "CSV (*.csv)")
        if not path:
            return
        if not path.endswith(".csv"):
            path += ".csv"
        out = rec.materialize_capture(m.run_dir, sources, t_start=m.t,
                                      t_stop=m.t_end, out_path=path)
        self.statusBar().showMessage(f"Exported recording → {out}", 6000)

    def _export_plots(self, _mid=None):
        folder = QFileDialog.getExistingDirectory(self, "Export plots to folder")
        if not folder:
            return
        charts = [p for p in self.dashboard._panels.values()
                  if getattr(p, "plot", None) is not None]
        for p in charts:                       # keep the record overlay out of exports
            if hasattr(p, "set_regions_visible"):
                p.set_regions_visible(False)
        n = 0
        for p in charts:
            p.plot.grab().save(os.path.join(folder, f"{p.panel_id}.png"))
            n += 1
        for p in charts:
            if hasattr(p, "set_regions_visible"):
                p.set_regions_visible(True)
        self.statusBar().showMessage(f"Exported {n} plot(s) → {folder}", 6000)

    def _toggle_record(self):
        ms = self.dashboard.markers
        if not self.recorder.active:
            sources = self.dashboard.capture_sources()
            traces = self.dashboard.capture_traces()
            if not sources and not traces:
                self.statusBar().showMessage(
                    "Nothing to record — route some sources to a chart first.", 5000)
                return
            run_dir = os.path.join(self._runs_dir(),
                                   "run_" + time.strftime("%Y-%m-%dT%H-%M-%S"))
            n = len(sources) + len(traces)
            self._rec_start_mid = ms.add(
                time.time(), kind=RECORDING, label="REC",
                comment=f"{n} sources", run_dir=run_dir)
            self.recorder.start(run_dir, sources, traces)
            self.statusBar().showMessage(f"● Recording → {run_dir}")
        else:
            m = ms.get(self._rec_start_mid)
            t0 = m.t if m else None
            t1 = time.time()
            ms.update(self._rec_start_mid, t_end=t1)   # close the region
            out = self.recorder.stop(t_start=t0, t_stop=t1)
            self._rec_start_mid = None
            self.statusBar().showMessage(f"■ Saved {out}", 8000)

    def _on_record_change(self):
        if self.recorder.active:
            self.record_action.setText("■ Stop")
        else:
            self.record_action.setText("● Record")

    def _check_recovery(self):
        """Crash-safety: materialise any capture left unfinalised by a crash."""
        recovered = 0
        for run_dir in rec.find_unfinalized(self._runs_dir()):
            if rec.recover(run_dir):
                recovered += 1
        if recovered:
            self.statusBar().showMessage(
                f"Recovered {recovered} unfinalised recording(s) after a crash.",
                8000)

    def _on_tick(self):
        self.sources_panel.update_live(self.engine.latest())
        self.sinks_panel.update_live()
        self._update_image_overlays()
        self._update_trace_cursors()

    def _update_trace_cursors(self):
        for panel in self.dashboard._panels.values():
            if getattr(panel, "kind", "") not in ("spectrum", "specwf") \
                    or not hasattr(panel, "set_cursors"):
                continue
            cursors = []
            for src_key in getattr(panel, "_curves", {}):
                for cur in self.dashboard.cursors_for(src_key):
                    cursors.append((cur.id, cur.name, cur.mz, cur.last_value,
                                    color_for(f"cur/{cur.id}")))
            panel.set_cursors(cursors)

    def _update_image_overlays(self):
        for pid, panel in self.dashboard._panels.items():
            if getattr(panel, "kind", "") != "image" or not hasattr(panel, "view"):
                continue
            overlays = []
            for det in self.dashboard.detectors_for(pid):
                val = det.last_value
                ok = (isinstance(val, bool)
                      or (isinstance(val, (int, float)) and val == val)
                      or (det.dtype == "string" and bool(val)))
                if det.dtype == "string":
                    vt = str(val) if val else "?"
                elif isinstance(val, bool):
                    vt = "on" if val else "off"
                elif isinstance(val, (int, float)) and val == val:
                    vt = fmt(val, det.unit)
                else:
                    vt = "—"
                overlays.append((f"{det.name}: {vt}", det.roi,
                                 color_for(f"cv/{det.id}"), ok))
            panel.view.set_overlays(overlays)

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

    def _open_cv_config(self, sink_key: str) -> None:
        dlg = self._cv_dialogs.get(sink_key)
        if dlg is not None:
            dlg.raise_()
            dlg.activateWindow()
            return
        dlg = ImageConfigDialog(self.dashboard, sink_key, self)
        dlg.setAttribute(Qt.WA_DeleteOnClose, True)
        dlg.destroyed.connect(lambda *_: self._cv_dialogs.pop(sink_key, None))
        self._cv_dialogs[sink_key] = dlg
        dlg.show()

    def _open_peaks_config(self, sink_key: str) -> None:
        dlg = self._cv_dialogs.get(sink_key)
        if dlg is not None:
            dlg.raise_()
            dlg.activateWindow()
            return
        dlg = CursorDialog(self.dashboard, sink_key, self)
        dlg.setAttribute(Qt.WA_DeleteOnClose, True)
        dlg.destroyed.connect(lambda *_: self._cv_dialogs.pop(sink_key, None))
        self._cv_dialogs[sink_key] = dlg
        dlg.show()

    # -- session save / restore ---------------------------------------------
    @staticmethod
    def _b64(qba) -> str:
        return bytes(qba.toBase64().data()).decode("ascii")

    def _write_session(self, path: str) -> None:
        data = {
            "version": 1,
            "devices": self.manager.export_active(),
            "layout": self.dashboard.export_layout(),
            "dock": {
                "geometry": self._b64(self.saveGeometry()),
                "window": self._b64(self.saveState()),
                "workspace": self._b64(self.workspace.saveState()),
            },
        }
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)

    def save_session(self, path: str) -> None:
        self._write_session(path)
        self._remember(path)
        self.statusBar().showMessage(f"Saved {os.path.basename(path)}", 4000)

    # -- working-session autosave (so tags/layout survive a restart or crash) -
    def _schedule_autosave(self):
        if getattr(self, "_autosave_on", False):
            self._autosave_timer.start()

    def _do_autosave(self):
        try:
            self._write_session(os.path.join(self._app_dir(), "session.json"))
        except Exception:
            pass

    def _init_session_persistence(self):
        if os.path.exists(os.path.join(self._app_dir(), "session.json")):
            QTimer.singleShot(300, self._restore_and_enable_autosave)
        else:
            self._autosave_on = True

    def _restore_and_enable_autosave(self):
        self.open_session(os.path.join(self._app_dir(), "session.json"))
        self._autosave_on = True

    def open_session(self, path: str) -> None:
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception as exc:
            self.statusBar().showMessage(f"Could not open layout: {exc}", 5000)
            return
        # rebuild the model first (so docks exist), then restore Qt geometry
        self.dashboard.import_layout(data.get("layout", {}))
        self.manager.request_devices(data.get("devices", []))
        dock = data.get("dock", {})
        if dock.get("workspace"):
            self.workspace.restoreState(QByteArray.fromBase64(dock["workspace"].encode()))
        if dock.get("geometry"):
            self.restoreGeometry(QByteArray.fromBase64(dock["geometry"].encode()))
        if dock.get("window"):
            self.restoreState(QByteArray.fromBase64(dock["window"].encode()))
        self._remember(path)
        self.statusBar().showMessage(f"Loaded {os.path.basename(path)}", 4000)

    def _on_save(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Layout", "", "ferroDAC layout (*.json)")
        if path:
            if not path.endswith(".json"):
                path += ".json"
            self.save_session(path)

    def _on_open(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open Layout", "", "ferroDAC layout (*.json)")
        if path:
            self.open_session(path)

    @staticmethod
    def _remember(path: str) -> None:
        QSettings("ferroDAC", "ferroDAC").setValue("lastSession", path)

    def closeEvent(self, event):  # noqa: N802
        if self.recorder.active:        # finalize rather than leave it dangling
            ms = self.dashboard.markers
            m = ms.get(self._rec_start_mid) if self._rec_start_mid else None
            t1 = time.time()
            if m:
                ms.update(self._rec_start_mid, t_end=t1)
            self.recorder.stop(t_start=m.t if m else None, t_stop=t1)
        if self._autosave_on:
            self._do_autosave()
        self.hub.disconnect()
        if self.store_writer is not None:
            self.store_writer.stop()        # flush the buffer + build final rollups
        self.dashboard.shutdown()
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


def _setup_logging() -> str:
    """File + console logging so the frozen (windowed) app is diagnosable.
    Returns the log path. The file is rewritten each run (a fresh diagnostic)."""
    import logging
    import os

    from qtpy.QtCore import QStandardPaths
    docs = QStandardPaths.writableLocation(QStandardPaths.DocumentsLocation) \
        or os.path.expanduser("~")
    handlers = [logging.StreamHandler()]
    path = ""
    try:
        d = os.path.join(docs, "ferroDAC")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "ferrodac.log")
        handlers.insert(0, logging.FileHandler(path, mode="w", encoding="utf-8"))
    except Exception:
        pass
    logging.basicConfig(
        level=logging.INFO, force=True, handlers=handlers,
        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    return path


def main(argv=None) -> int:
    import logging
    import os
    import sys

    from qtpy.QtCore import QStandardPaths
    from .. import __version__
    from ..core.identity import DeviceRegistry

    logpath = _setup_logging()
    log = logging.getLogger("app")
    log.info("ferroDAC %s starting (frozen=%s); log → %s",
             __version__, getattr(sys, "frozen", False), logpath)

    app = QApplication(sys.argv if argv is None else argv)
    app.setApplicationName("ferroDAC")
    icon_path = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                             "assets", "app.png")
    if os.path.exists(icon_path):
        app.setWindowIcon(QIcon(icon_path))
    apply_dark_theme(app)

    cfg = QStandardPaths.writableLocation(QStandardPaths.AppConfigLocation)
    registry = DeviceRegistry(os.path.join(cfg, "registry.json") if cfg else None)

    drivers = load_builtin_drivers()
    log.info("loaded %d driver(s): %s", len(drivers),
             ", ".join(getattr(d, "driver", "?") for d in drivers) or "—")
    engine = Engine()
    manager = DeviceManager(drivers, engine=engine, registry=registry)
    win = MainWindow(manager, engine)
    win.show()
    return app.exec()
