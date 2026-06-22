"""ferroDAC UI — an IDE-style dockable shell.

  - central : a dockable **workspace** of panels (charts / 7-seg / inputs).
  - left dock "Devices" : device management (hidden by default; toolbar button).
  - right dock "Sources" : one card per data-output Source of every active
    device, each with a "Route ▾" dropdown selecting which panel(s) it feeds.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
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
    QDialogButtonBox,
    QDockWidget,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..core.engine import Engine
from ..core.history import HistoryBuffer
from ..core.manager import DeviceManager
from ..core.markers import RECORDING
from ..core.projects import ProjectManager
from ..core.registry import load_builtin_drivers
from ..core.device import DeviceDescriptor, RateMode, SinkKind
from ..vision.detector import FAIL_LABELS, PARSE_LABELS, WHITELIST_PRESETS, Detector
from ..vision.ocr import available_engines, get_engine, ocr_backend, qimage_to_rgb
from ._common import STATUS_COLORS, clear_layout, color_for, fmt
from .hubclient import ConnectHubDialog, HubController
from .logview import LogPanel, QtLogHandler, SyncStatusWidget
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

    def __init__(self, title, count, collapsed, on_toggle, action=None, parent=None):
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
        head = QHBoxLayout()                     # title + optional right-aligned action
        head.setContentsMargins(0, 0, 0, 0)
        head.addWidget(self._btn)
        head.addStretch(1)
        if action is not None:
            text, cb = action
            ab = QToolButton()
            ab.setText(text)
            ab.setCursor(Qt.PointingHandCursor)
            ab.setStyleSheet(
                "QToolButton { color:#8b95a4; font-size:11px; border:none;"
                " padding:3px 4px; } QToolButton:hover { color:#c7d0db; }")
            ab.clicked.connect(lambda: cb())
            head.addWidget(ab)
        self._body = QWidget()
        self._bl = QVBoxLayout(self._body)
        self._bl.setContentsMargins(6, 0, 0, 4)
        self._bl.setSpacing(6)
        self._body.setVisible(not collapsed)
        v.addLayout(head)
        v.addWidget(self._body)

    def add(self, widget):
        self._bl.addWidget(widget)

    def _toggle(self):
        vis = not self._body.isVisible()
        self._body.setVisible(vis)
        self._btn.setArrowType(Qt.DownArrow if vis else Qt.RightArrow)
        self._on_toggle(self._title, not vis)


class SourcesPanel(QWidget):
    def __init__(self, manager: DeviceManager, dashboard: Dashboard,
                 on_curate=None, on_lens=None, parent=None):
        super().__init__(parent)
        self.manager = manager
        self.dashboard = dashboard
        self._on_curate = on_curate
        self._on_lens = on_lens
        self._cards: dict[str, SourceCard] = {}
        self._collapsed: set[str] = set()        # origins folded by the user

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)
        head = QHBoxLayout()
        self._label = QLabel("Sources")
        self._label.setStyleSheet("font-size:12px; font-weight:700; color:#c7d0db;")
        head.addWidget(self._label)
        head.addStretch(1)
        if on_curate is not None:
            cur = QToolButton()
            cur.setText("✔ Curate")
            cur.setToolTip("Pick which channels this project shows")
            cur.clicked.connect(lambda: self._on_curate())
            head.addWidget(cur)
        self._all = QCheckBox("All")             # off = the project's channel lens
        self._all.setToolTip("Show every channel, not just the project's selection")
        self._all.toggled.connect(lambda on: self._on_lens and self._on_lens(on))
        head.addWidget(self._all)
        root.addLayout(head)
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
        ports = self.dashboard.visible_source_ports()     # the project's channel lens
        total = len(self.dashboard.source_ports())
        self._label.setText(f"Sources  ({len(ports)}/{total})" if len(ports) != total
                            else f"Sources  ({len(ports)})")
        if not ports:
            lensed = self.dashboard.source_lens is not None
            msg = ("No channels curated for this project.\nHit “✔ Curate”, or tick "
                   "“All”." if lensed
                   else "No sources yet.\nAdd a device (Devices) or an input (Add menu).")
            ph = QLabel(msg)
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
class _SourceCurateDialog(QDialog):
    """Tick the channels this project should show. The selection is a LENS over
    the global catalog (it filters the Sources view), not a copy of any data."""

    def __init__(self, ports, selected, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Curate project channels")
        self.setMinimumSize(360, 440)
        lay = QVBoxLayout(self)
        hint = QLabel("Tick the channels relevant to this project — the Sources "
                      "panel will then show just these (untick “All” to see them).")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#7f8a99; font-size:11px;")
        lay.addWidget(hint)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        host = QWidget()
        col = QVBoxLayout(host)
        col.setContentsMargins(4, 4, 4, 4)
        col.setSpacing(3)
        self._checks: dict = {}
        sel = set(selected)
        last_origin = None
        for p in sorted(ports, key=lambda p: (p.origin or "", p.name)):
            if p.dtype not in ("float", "bool", "trace"):
                continue
            if p.origin != last_origin:             # a light per-device header
                last_origin = p.origin
                h = QLabel(p.origin or "other")
                h.setStyleSheet("color:#8a93a3; font-weight:700; font-size:11px;")
                col.addWidget(h)
            cb = QCheckBox(p.name)
            cb.setChecked(p.key in sel)
            self._checks[p.key] = cb
            col.addWidget(cb)
        col.addStretch(1)
        scroll.setWidget(host)
        lay.addWidget(scroll, 1)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def selected_keys(self) -> list:
        return [k for k, cb in self._checks.items() if cb.isChecked()]


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
                 on_export_plots=None, on_lens=None, projects_provider=None,
                 on_jump=None, parent=None):
        super().__init__(parent)
        self.markers = markers
        self.clock = clock
        self._on_zoom = on_zoom
        self._on_jump = on_jump                          # jump the timeline to a tag
        self._on_export_csv = on_export_csv
        self._on_export_plots = on_export_plots
        self._on_lens = on_lens
        self._projects_provider = projects_provider     # () -> [(id, name)]
        self._collapsed: set = set()                    # folded sections
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)
        head = QHBoxLayout()
        self._label = QLabel("Events")
        self._label.setStyleSheet("font-size:12px; font-weight:700; color:#c7d0db;")
        head.addWidget(self._label)
        head.addStretch(1)
        self._all = QCheckBox("All projects")       # off = active project lens
        self._all.setToolTip("Show tags from every project, not just the active one")
        self._all.toggled.connect(lambda on: self._on_lens and self._on_lens(on))
        head.addWidget(self._all)
        root.addLayout(head)
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
        ms = self.markers.visible()                 # the active project lens
        # split by shape: a RECORDING is a slice (a span over the data), a TAG is
        # a point in time. Show them as two distinct sections, not one flat list.
        recs = [m for m in ms if m.is_region]
        tags = [m for m in ms if not m.is_region]
        if not ms:
            hint = ("No events here.\nUntick “All projects” to widen the lens."
                    if self.markers.lens is not None
                    else "No events.\nDrop a tag with “＋ Tag”, or hit ● Record.")
            ph = QLabel(hint)
            ph.setStyleSheet("color:#7f8a99;")
            ph.setWordWrap(True)
            self._layout.addWidget(ph)
        else:
            if recs:
                self._add_section("Recordings", recs)   # slices
            if tags:
                self._add_section("Tags", tags)         # points
        self._layout.addStretch(1)
        total = len(self.markers.all())
        self._label.setText(f"Events  ({len(ms)}/{total})" if len(ms) != total
                            else f"Events  ({len(ms)})")

    def _add_section(self, title, markers):
        grp = CollapsibleGroup(title, len(markers), title in self._collapsed,
                               self._on_group_toggle)
        for m in markers:
            grp.add(self._row(m))
        self._layout.addWidget(grp)

    def _on_group_toggle(self, title, collapsed):
        (self._collapsed.add if collapsed else self._collapsed.discard)(title)

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
        if not is_rec and self._on_jump is not None:     # points: jump the timeline here
            jump = QToolButton()
            jump.setText("⌖")
            jump.setToolTip("Jump the timeline to this tag")
            jump.clicked.connect(lambda _=False, mid=m.id: self._on_jump(mid))
            top.addWidget(jump)
        if self._projects_provider is not None:
            proj = QToolButton()
            proj.setText("🏷")
            proj.setToolTip("Assign to projects")
            proj.clicked.connect(
                lambda _=False, mid=m.id, b=None: self._assign_menu(mid, self.sender()))
            top.addWidget(proj)
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

    def _assign_menu(self, mid, anchor):
        """A checkable menu of projects → add/remove this tag's membership (a tag
        can be in many). Reopen to toggle several."""
        m = self.markers.get(mid)
        if m is None or self._projects_provider is None:
            return
        member = set(m.projects or [])
        menu = QMenu(self)
        any_proj = False
        for pid, name in self._projects_provider():
            any_proj = True
            act = menu.addAction(name)
            act.setCheckable(True)
            act.setChecked(pid in member)
            act.toggled.connect(
                lambda on, pid=pid, mid=mid:
                self.markers.add_to_project(mid, pid) if on
                else self.markers.remove_from_project(mid, pid))
        if not any_proj:
            menu.addAction("(no projects)").setEnabled(False)
        pos = (anchor.mapToGlobal(anchor.rect().bottomLeft())
               if anchor is not None else self.cursor().pos())
        menu.exec(pos)

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


class ProjectsPanel(QWidget):
    """Lists projects — LOCAL folders and (☁) HUB projects — pick one to make it
    active. Add a project (a local folder, or a new one on the hub); a local one
    can be Shared to the hub from its right-click menu. Reveal the folder."""

    def __init__(self, manager, on_activate, on_create_local, on_create_hub,
                 on_reveal, on_share=None, hub_enabled=None, parent=None):
        super().__init__(parent)
        self.manager = manager
        self._on_activate = on_activate
        self._on_create_local = on_create_local
        self._on_create_hub = on_create_hub
        self._on_reveal = on_reveal
        self._on_share = on_share
        self._hub_enabled = hub_enabled or (lambda: False)
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)
        self._label = QLabel("Projects")
        self._label.setStyleSheet("font-size:12px; font-weight:700; color:#c7d0db;")
        root.addWidget(self._label)
        self._list = QListWidget()
        self._list.itemActivated.connect(self._activate)   # double-click or Enter
        self._list.setContextMenuPolicy(Qt.CustomContextMenu)
        self._list.customContextMenuRequested.connect(self._context_menu)
        root.addWidget(self._list, 1)
        row = QHBoxLayout()
        new = QToolButton()
        new.setText("＋ Add project")
        new.setToolButtonStyle(Qt.ToolButtonTextOnly)
        new.setPopupMode(QToolButton.InstantPopup)
        menu = QMenu(new)
        menu.addAction("Local folder…", lambda: self._on_create_local())
        self._hub_action = menu.addAction("On the hub…", lambda: self._on_create_hub())
        self._hub_action.setToolTip("Create a shared project on the hub (needs a connection)")
        new.setMenu(menu)
        rev = QPushButton("Reveal")
        rev.clicked.connect(lambda: self._on_reveal())
        row.addWidget(new)
        row.addWidget(rev)
        root.addLayout(row)
        self.refresh()

    def refresh(self):
        self._list.clear()
        active = self.manager.active
        projs = self.manager.projects()
        self._label.setText(f"Projects  ({len(projs)})")
        self._hub_action.setEnabled(self._hub_enabled())    # greyed when offline
        for p in projs:
            on = active is not None and p.id == active.id
            badge = "☁ " if getattr(p, "is_hub", False) else ""
            it = QListWidgetItem(("●  " if on else "○  ") + badge + p.name)
            it.setData(Qt.UserRole, p.id)
            if on:
                f = it.font(); f.setBold(True); it.setFont(f)
            tip = p.description or ""
            if getattr(p, "is_hub", False):
                tip = (tip + "  ·  " if tip else "") + "on the hub"
            if tip:
                it.setToolTip(tip)
            self._list.addItem(it)

    def _context_menu(self, pos):
        it = self._list.itemAt(pos)
        if it is None or self._on_share is None:
            return
        p = self.manager.get(it.data(Qt.UserRole))
        if p is None or getattr(p, "is_hub", False):
            return                                          # already on the hub
        menu = QMenu(self)
        act = menu.addAction("☁ Share to hub")
        act.setEnabled(self._hub_enabled())
        act.triggered.connect(lambda: self._on_share(p.id))
        menu.exec(self._list.mapToGlobal(pos))

    def _activate(self, item):
        if item is None:                        # list cleared mid-signal → ignore
            return
        pid = item.data(Qt.UserRole)
        if pid:
            self._on_activate(pid)


def _dur(seconds) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60:02d}s"
    return f"{s // 3600}h {(s % 3600) // 60:02d}m"


def _editor_args(command: str, path: str) -> list:
    """argv for an external-editor command template. ``{file}`` (or ``{path}``) is
    replaced with the file path; with no placeholder the path is appended.
    ``'konsole -e nvim {file}'`` → ``['konsole','-e','nvim', path]``."""
    import shlex
    parts = shlex.split(command)
    if not parts:
        return []
    if "{file}" in command or "{path}" in command:
        return [a.replace("{file}", path).replace("{path}", path) for a in parts]
    return parts + [path]


class ProjectExplorer(QWidget):
    """The ACTIVE project's contents, grouped and format-aware (Phase 3b). Every
    group is SCANNED fresh from the filesystem — `layouts/`, the curated channels
    in `sources.json`, the recordings in `reports/` — so it tracks disk with no
    mirrored index. It's a *view*: opening a layout, revealing a recording, or
    curating channels acts on what's already there; nothing here copies data."""

    def __init__(self, project_provider, on_open_layout, on_reveal_path,
                 on_curate, on_add_layout=None, active_layout=None,
                 on_add_doc=None, on_add_bookmark=None, on_jump_window=None,
                 on_remove_bookmark=None, parent=None):
        super().__init__(parent)
        self._project = project_provider
        self._on_open_layout = on_open_layout
        self._on_reveal = on_reveal_path
        self._on_curate = on_curate
        self._on_add_layout = on_add_layout
        self._active_layout = active_layout or (lambda: None)   # () -> active path
        self._on_add_doc = on_add_doc
        self._on_add_bookmark = on_add_bookmark
        self._on_jump_window = on_jump_window                   # (t0, t1) -> jump
        self._on_remove_bookmark = on_remove_bookmark           # (name) -> drop
        self._collapsed: set = set()
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)
        self._label = QLabel("Project")
        self._label.setStyleSheet("font-size:12px; font-weight:700; color:#c7d0db;")
        root.addWidget(self._label)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)   # cards wrap, don't scroll sideways
        host = QWidget()
        self._layout = QVBoxLayout(host)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(6)
        self._layout.addStretch(1)
        scroll.setWidget(host)
        root.addWidget(scroll, 1)
        self.refresh()

    def refresh(self):
        clear_layout(self._layout)
        p = self._project()
        if p is None:
            self._label.setText("Project")
            ph = QLabel("No active project.")
            ph.setStyleSheet("color:#7f8a99;")
            self._layout.addWidget(ph)
            self._layout.addStretch(1)
            return
        self._label.setText(p.name)
        self._add_group("Layouts", self._layout_cards(p),
                        "No layouts yet.\n＋ Add one — it then autosaves as you work.",
                        action=("＋ Add", self._on_add_layout) if self._on_add_layout else None)
        self._add_group("Channels", self._channel_cards(p),
                        "No channels curated — the Sources panel shows them all.\n"
                        "Curate to focus this project.",
                        action=("✔ Curate", self._on_curate))
        self._add_group("Recordings", self._recording_cards(p),
                        "No recordings yet.\nHit ● Record to capture a span.")
        self._add_group("Docs", self._doc_cards(p),
                        "No docs yet.\n＋ Add reference files (notes, datasheets, plots).",
                        action=("＋ Add", self._on_add_doc) if self._on_add_doc else None)
        self._add_group("Bookmarks", self._window_cards(p),
                        "No bookmarks yet.\n＋ Add saves the current timeline window.",
                        action=("＋ Add", self._on_add_bookmark) if self._on_add_bookmark else None)
        self._layout.addStretch(1)

    # -- groups & cards ------------------------------------------------------
    def _add_group(self, title, cards, empty_hint, action=None):
        grp = CollapsibleGroup(title, len(cards), title in self._collapsed,
                               self._on_toggle, action=action)
        if cards:
            for c in cards:
                grp.add(c)
        else:
            ph = QLabel(empty_hint)
            ph.setWordWrap(True)
            ph.setStyleSheet("color:#6b7480; font-size:11px;")
            grp.add(ph)
        self._layout.addWidget(grp)

    def _on_toggle(self, title, collapsed):
        (self._collapsed.add if collapsed else self._collapsed.discard)(title)

    def _card(self, title, sub, actions):
        card = QFrame()
        card.setObjectName("ExpCard")
        card.setStyleSheet("#ExpCard { background:#171c26; border:1px solid #232a38;"
                           " border-radius:8px; }")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(10, 7, 10, 7)
        lay.setSpacing(2)
        top = QHBoxLayout()
        top.setSpacing(6)
        t = QLabel(title)
        t.setStyleSheet("font-weight:700;")
        t.setToolTip(title)
        # a long title (e.g. a doc filename) must give way to the action buttons,
        # not push them off-screen — Ignored lets the row compress it (full name
        # stays in the tooltip).
        t.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        top.addWidget(t, 1)
        for text, cb in actions:
            b = QToolButton()
            b.setText(text)
            b.clicked.connect(lambda _=False, cb=cb: cb())
            top.addWidget(b)
        lay.addLayout(top)
        if sub:
            s = QLabel(sub)
            s.setStyleSheet("color:#7f8a99; font-size:11px;")
            lay.addWidget(s)
        return card

    def _layout_cards(self, p):
        cards = []
        active = self._active_layout()
        for name in p.layouts():
            path = p.layout_path(name)
            n = p.layout_panels(name)
            sub = f"{n} panel{'' if n == 1 else 's'}" if n else "layout"
            is_active = active is not None and os.path.abspath(path) == os.path.abspath(active)
            if is_active:
                sub += "  ·  autosaving"             # this one tracks live edits
                actions = []                          # already the live layout
            else:
                actions = [("Open", lambda path=path: self._on_open_layout(path))]
            cards.append(self._card(("● " if is_active else "") + name, sub, actions))
        return cards

    def _channel_cards(self, p):
        cards = []
        for s in p.sources():
            key = s.get("key") if isinstance(s, dict) else s
            label = (isinstance(s, dict) and s.get("label")) or key
            cards.append(self._card(label, key, []))
        return cards

    def _recording_cards(self, p):
        return [self._card(self._rec_title(r), self._rec_sub(r),
                           [("Reveal", lambda path=r["path"]: self._on_reveal(path))])
                for r in p.recordings()]

    @staticmethod
    def _rec_title(r):
        t0 = r.get("t0")
        return (time.strftime("%b %d, %H:%M", time.localtime(t0)) if t0
                else r["name"])           # a friendlier label than the run_ folder

    @staticmethod
    def _rec_sub(r):
        bits = []
        t0, t1 = r.get("t0"), r.get("t1")
        if t0 and t1 and t1 >= t0:
            bits.append(_dur(t1 - t0))
        bits.append(f"{r['sources']} src")
        if r.get("tags"):
            bits.append(f"{r['tags']} tags")
        return "  ·  ".join(bits)

    def _doc_cards(self, p):
        return [self._card(d["name"], d["ext"] or "file",
                           [("Open", lambda path=d["path"]: self._on_reveal(path))])
                for d in p.docs()]

    def _window_cards(self, p):
        cards = []
        for w in p.windows():
            name, t0, t1 = w.get("name", "window"), w.get("t0"), w.get("t1")
            sub = ""
            if t0 and t1 and t1 >= t0:
                sub = f"{time.strftime('%b %d, %H:%M', time.localtime(t0))}  ·  {_dur(t1 - t0)}"
            actions = []
            if self._on_jump_window is not None and t0 and t1:
                actions.append(("⌖ Jump", lambda t0=t0, t1=t1: self._on_jump_window(t0, t1)))
            if self._on_remove_bookmark is not None:
                actions.append(("✕", lambda name=name: self._on_remove_bookmark(name)))
            cards.append(self._card(name, sub, actions))
        return cards


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

        # data plane: always-on hot history. Built BEFORE the dashboard so the
        # dashboard can render through the replay playback bus. The durable
        # StoreWriter (Zarr) is the crash-safe write path; a "recording" is just
        # a marked span over it, auto-exported on Stop (no separate capture file).
        self.history = HistoryBuffer()
        engine.subscribe(self.history.feed)
        self._rec_start_mid = None         # the open REC marker while recording

        # durable store: persist EVERYTHING continuously (§7.4) so data survives a
        # restart and a span can be recorded retroactively. Degrades to the RAM
        # ring if zarr/disk is unavailable.
        self.store_writer = None
        self.resolver = None
        self.time_context = None
        self.replay = None
        try:
            from ..store import (RamTier, ReplayController, Resolver,
                                 StoreWriter, TimeContext, ZarrStore)
            os.makedirs(self._app_dir(), exist_ok=True)
            store = ZarrStore(os.path.join(self._app_dir(), "store.zarr"))
            self.store_writer = StoreWriter(store)
            self.store_writer.attach(engine)
            # the read path: one query() over the live RAM ring + the durable store
            self.resolver = Resolver([RamTier(self.history), store])
            # replay spine (DESIGN §7.4): one head + a playback Bus the whole
            # dashboard renders through. Following-now → the live engine passes
            # straight through (≡ today); parked → re-stream history (W2). W1
            # wires the pass-through and verifies it's behaviour-identical.
            self.time_context = TimeContext()
            # Start in GROW mode anchored at app launch: the window is
            # [launch, live] and grows — so charts and the time-axis waterfall
            # show the whole session by default (not a sliding tail). Drag the
            # back edge / hit Slide to change it.
            self.time_context.grow = True
            self.time_context.anchor = self.time_context.head
            self.replay = ReplayController(
                engine, store, self.time_context,
                sources=lambda: self.dashboard.source_keys(),
                on_reset=self._replay_reset,
                on_progress=self._replay_progress,
                reader=self.resolver,        # replay full-res via RAM+store+hub tier
            )
        except Exception as exc:                       # noqa: BLE001
            logging.getLogger("ferrodac").warning("durable store disabled: %s", exc)

        # the dashboard renders through the replay playback bus when available,
        # else straight off the engine (data plane disabled) — identical live.
        data_bus = self.replay.bus if self.replay is not None else engine
        self.dashboard = Dashboard(self.workspace, engine, manager, data_bus=data_bus,
                                   historic_sources=self._historic_sources)
        self.dashboard.add_panel("chart")

        # networking: publish to / consume from a hub (optional, needs grpcio)
        self.hub = HubController(
            self.dashboard, engine, manager, self,
            store=self.store_writer.store if self.store_writer is not None else None,
            resolver=self.resolver)
        # All three fire from hub worker threads (gRPC / sync) — force
        # QueuedConnection so the slots run on the GUI thread (see hubclient).
        # A bound method (not a lambda) gives the queued call an explicit
        # GUI-thread receiver, so showMessage's QTimer is never started off-thread.
        self.hub.status.connect(self._on_hub_status, Qt.QueuedConnection)
        # hub link + store-sync read-out in the status bar, and a recoloured Hub
        # button when connected (the sync runs headlessly in the background).
        self.sync_status = SyncStatusWidget()
        self.statusBar().addPermanentWidget(self.sync_status)
        self.hub.sync_status.connect(self.sync_status.set_state, Qt.QueuedConnection)
        self.hub.connection_changed.connect(self._on_hub_connection,
                                            Qt.QueuedConnection)

        # working-LAYOUT autosave (per-project working.json) — layout only now
        self._active_layout_path = None    # a named layout open → autosave to it too
        self._autosave_on = False
        self._autosave_timer = QTimer(self)
        self._autosave_timer.setSingleShot(True)
        self._autosave_timer.setInterval(1500)
        self._autosave_timer.timeout.connect(self._do_autosave)
        self.dashboard.ports_changed.connect(self._schedule_autosave)
        # tags are GLOBAL — autosaved to tags.json on any change (own debounce)
        self._tag_save_timer = QTimer(self)
        self._tag_save_timer.setSingleShot(True)
        self._tag_save_timer.setInterval(1000)
        self._tag_save_timer.timeout.connect(self._save_global_tags)
        self.dashboard.markers.changed.connect(self._schedule_tag_save)

        self._sources_show_all = False
        self.sources_panel = SourcesPanel(manager, self.dashboard,
                                          on_curate=self._curate_sources,
                                          on_lens=self._set_source_lens_all)
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
        self._tags_show_all = False
        self.events_panel = EventsPanel(
            self.dashboard.markers, self.dashboard.clock,
            on_zoom=self._zoom_recording, on_export_csv=self._export_recording_csv,
            on_export_plots=self._export_plots, on_lens=self._set_tag_lens_all,
            on_jump=self._jump_to_tag,
            projects_provider=lambda: [(p.id, p.name)
                                       for p in self._project_mgr.projects()]
            if getattr(self, "_project_mgr", None) else [])
        self.events_dock = QDockWidget("Events", self)
        self.events_dock.setObjectName("EventsDock")
        self.events_dock.setWidget(self.events_panel)
        self.events_dock.setMinimumWidth(280)
        self.addDockWidget(Qt.RightDockWidgetArea, self.events_dock)

        # Docs: an in-app markdown/LaTeX view of the project's README/notes. The
        # QtWebEngine view is created LAZILY (on first show) so launch + the UI
        # tests don't spin up Chromium, and the app still runs if WebEngine is
        # absent. The .md file is truth — edit it in your own editor too.
        self._docs_view = None
        self._docs_unavailable = False
        self.docs_dock = QDockWidget("Docs", self)
        self.docs_dock.setObjectName("DocsDock")
        self.docs_dock.setMinimumWidth(320)
        self.addDockWidget(Qt.RightDockWidgetArea, self.docs_dock)
        self.docs_dock.visibilityChanged.connect(self._on_docs_visible)

        self.tabifyDockWidget(self.sources_dock, self.sinks_dock)
        self.tabifyDockWidget(self.sinks_dock, self.events_dock)
        self.tabifyDockWidget(self.events_dock, self.docs_dock)
        self.docs_dock.setVisible(False)
        self.sources_dock.raise_()

        self.devices_panel = DevicesPanel(manager, self._open_config)
        self.devices_dock = QDockWidget("Devices", self)
        self.devices_dock.setObjectName("DevicesDock")
        self.devices_dock.setWidget(self.devices_panel)
        self.devices_dock.setMinimumWidth(300)
        self.addDockWidget(Qt.LeftDockWidgetArea, self.devices_dock)
        self.devices_dock.setVisible(False)

        # projects: a curation overlay over the global catalog (the active project
        # owns the working layout; Phase 2 adds the tag lens).
        self._setup_projects()
        self.projects_panel = ProjectsPanel(
            self._project_mgr, on_activate=self._switch_project,
            on_create_local=self._add_project, on_create_hub=self._add_project_hub,
            on_reveal=self._reveal_project, on_share=self._share_project,
            hub_enabled=lambda: self.hub.connected)
        self.projects_dock = QDockWidget("Projects", self)
        self.projects_dock.setObjectName("ProjectsDock")
        self.projects_dock.setWidget(self.projects_panel)
        self.projects_dock.setMinimumWidth(220)
        self.addDockWidget(Qt.LeftDockWidgetArea, self.projects_dock)

        # the active project's contents — layouts, curated channels, recordings —
        # scanned from disk and shown as format-aware cards (Phase 3b).
        self.project_explorer = ProjectExplorer(
            lambda: self._project_mgr.active, on_open_layout=self._open_layout,
            on_reveal_path=self._reveal_path, on_curate=self._curate_sources,
            on_add_layout=self._on_add_layout,
            active_layout=lambda: self._active_layout_path,
            on_add_doc=self._add_doc, on_add_bookmark=self._add_bookmark,
            on_jump_window=self._jump_to_window,
            on_remove_bookmark=self._remove_bookmark)
        self.explorer_dock = QDockWidget("Project", self)
        self.explorer_dock.setObjectName("ProjectExplorerDock")
        self.explorer_dock.setWidget(self.project_explorer)
        self.explorer_dock.setMinimumWidth(220)
        self.addDockWidget(Qt.LeftDockWidgetArea, self.explorer_dock)
        self.tabifyDockWidget(self.projects_dock, self.explorer_dock)
        self.projects_dock.raise_()

        # transport player: control the shared replay head from the main window
        # (without opening the Timeline). The app owns the clock heartbeat below
        # so the Timeline and the player never double-drive it.
        if self.time_context is not None:
            from .player import PlayerBar
            self.player = PlayerBar(self.time_context)
            self.player_dock = QDockWidget("Player", self)
            self.player_dock.setObjectName("PlayerDock")
            self.player_dock.setWidget(self.player)
            self.addDockWidget(Qt.BottomDockWidgetArea, self.player_dock)
            # the single clock heartbeat: advance the head while following and
            # walk it while playing — owned here so live/play work without the
            # Timeline and the two views never double-drive the clock.
            self._play_wall = None
            self._tc_live_timer = QTimer(self)
            self._tc_live_timer.timeout.connect(self._tc_live_tick)
            self._tc_live_timer.start(500)
            self._tc_play_timer = QTimer(self)
            self._tc_play_timer.timeout.connect(self._tc_play_tick)
            self._tc_play_timer.start(50)
            # a slim progress bar in the status bar for the (possibly slow) full-
            # res slice load on a scrub/park — so a big load reads as "loading",
            # not "frozen".
            self._load_bar = QProgressBar()
            self._load_bar.setMaximumWidth(220)
            self._load_bar.setFormat("loading %p%")
            self._load_bar.setVisible(False)
            self.statusBar().addPermanentWidget(self._load_bar)

        # in-app log viewer: a QtLogHandler on the root logger forwards every
        # record (incl. worker-thread ones, e.g. the sync runner) here.
        self._log_handler = QtLogHandler()
        self.log_panel = LogPanel(self._log_handler)
        logging.getLogger().addHandler(self._log_handler)
        self.log_dock = QDockWidget("Log", self)
        self.log_dock.setObjectName("LogDock")
        self.log_dock.setWidget(self.log_panel)
        self.addDockWidget(Qt.BottomDockWidgetArea, self.log_dock)
        if getattr(self, "player_dock", None) is not None:
            self.tabifyDockWidget(self.player_dock, self.log_dock)
            self.player_dock.raise_()
        else:
            self.log_dock.setVisible(False)

        self._build_menus()

        self.engine.tick.connect(self._on_tick)
        self.statusBar().showMessage(
            "Scanning for devices…  ·  open “Devices” to add one"
        )
        self.manager.start()
        if self._restore_last:
            self._init_session_persistence()    # restores markers, then recovers

    def _build_menus(self):
        filemenu = self.menuBar().addMenu("&File")
        filemenu.addAction("Export CSV…", self._on_export)
        filemenu.addSeparator()
        filemenu.addAction("Add Layout…", self._on_add_layout)
        filemenu.addAction("Open Layout…", self._on_open)

        view = self.menuBar().addMenu("&View")
        view.addAction(self.projects_dock.toggleViewAction())
        view.addAction(self.explorer_dock.toggleViewAction())
        view.addAction(self.devices_dock.toggleViewAction())
        view.addAction(self.sources_dock.toggleViewAction())
        view.addAction(self.sinks_dock.toggleViewAction())
        view.addAction(self.events_dock.toggleViewAction())
        view.addAction(self.docs_dock.toggleViewAction())
        if getattr(self, "player_dock", None) is not None:
            view.addAction(self.player_dock.toggleViewAction())
        view.addAction(self.log_dock.toggleViewAction())
        view.addSeparator()
        self.edit_action = view.addAction("Edit layout")
        self.edit_action.setCheckable(True)
        self.edit_action.setChecked(False)          # start in locked layout
        self.edit_action.toggled.connect(self.dashboard.set_edit_mode)
        self.edit_action.toggled.connect(self._lock_chrome)
        self._lock_chrome(False)                    # Player/Log start locked too
        view.addAction("Export defaults…", self.dashboard.configure_export_default)

        add = self.menuBar().addMenu("&Add")
        for kind, (label, _cls) in PANEL_TYPES.items():
            act = add.addAction(f"Add {label}")
            act.triggered.connect(lambda _=False, k=kind: self._add_panel(k))

        netmenu = self.menuBar().addMenu("&Cloud")
        self.hub_action = netmenu.addAction("ferroDAC Cloud…", self._open_hub)

        tb = self.addToolBar("Main")
        self.main_toolbar = tb
        tb.setObjectName("MainToolBar")
        tb.setMovable(False)
        tb.addAction(self.devices_dock.toggleViewAction())
        tb.addAction(self.edit_action)
        tb.addSeparator()
        self.record_action = tb.addAction("● Record", self._toggle_record)
        tb.addAction("＋ Tag", self._add_tag)
        tb.addAction("🕑 Timeline", self._open_timeline)
        tb.addAction("📄 Docs", self._open_docs)
        tb.addSeparator()
        tb.addAction(self.hub_action)

    def _timeline_sources(self) -> dict:
        """{key: name} for the Timeline: the dashboard's live/historic sources
        UNIONED with the hub's catalog (so hub-only history — e.g. after a local
        wipe — is listed and served via the resolver's hub read tier)."""
        names = dict(self.dashboard.source_names())
        if getattr(self, "hub", None) is not None:
            for key, name, _unit, _dtype in self.hub.hub_sources():
                names.setdefault(key, name or "")
        return names

    def _open_timeline(self):
        if self.resolver is None or self.time_context is None:
            self.statusBar().showMessage("Durable store unavailable — timeline disabled", 6000)
            return
        if getattr(self, "_timeline_win", None) is None:
            from .timeline import TimelineWindow
            win = TimelineWindow(self.resolver, self.store_writer.store,
                                 self.time_context, self,
                                 names=self._timeline_sources(),
                                 sources_fn=self._timeline_sources,
                                 lens_fn=self._curated_source_keys)
            win.destroyed.connect(lambda: setattr(self, "_timeline_win", None))
            self._timeline_win = win
        self._timeline_win.show()
        self._timeline_win.raise_()
        self._timeline_win.activateWindow()

    # -- docs (in-app markdown/LaTeX view; the .md file is truth) -------------
    def _open_docs(self) -> None:
        self.docs_dock.setVisible(True)
        self.docs_dock.raise_()          # → visibilityChanged → lazy-create the view

    def _on_docs_visible(self, visible: bool) -> None:
        if visible and self._docs_view is None and not self._docs_unavailable:
            self._ensure_docs_view()

    def _ensure_docs_view(self) -> None:
        try:
            from .docs import DocView
        except Exception as exc:         # noqa: BLE001 — QtWebEngine not installed
            self._docs_unavailable = True
            ph = QLabel("Document view needs QtWebEngine.\n\nInstall:\n"
                        "python3-pyside6.qtwebenginewidgets")
            ph.setAlignment(Qt.AlignCenter)
            ph.setWordWrap(True)
            ph.setStyleSheet("color:#7f8a99; padding:24px;")
            ph.setToolTip(str(exc))
            self.docs_dock.setWidget(ph)
            return
        self._docs_view = DocView(on_edit=self._open_doc_external,
                                  on_configure=self._configure_editor,
                                  on_list_recordings=self._list_recordings,
                                  on_export_recording=self._export_recording_for_doc,
                                  on_list_recording_exports=self._list_recording_exports,
                                  on_list_processors=self._list_processors,
                                  on_processor_source=self._processor_source)
        self.docs_dock.setWidget(self._docs_view)
        self._open_active_doc()

    def _open_doc_external(self, path: str) -> None:
        """Open `path` in the user's CONFIGURED editor command (e.g.
        ``konsole -e nvim {file}``) — run directly, no OS app-chooser. Falls back to
        the OS default when no command is set."""
        from qtpy.QtCore import QSettings
        cmd = QSettings("ferroDAC", "ferroDAC").value("editor/command", "", type=str) or ""
        if cmd.strip():
            import subprocess
            try:
                subprocess.Popen(_editor_args(cmd, path), start_new_session=True)
                return
            except Exception as exc:                   # noqa: BLE001
                self.statusBar().showMessage(f"Editor command failed: {exc}", 7000)
        self._reveal_path(path)                        # OS default (the .md handler)

    def _configure_editor(self) -> None:
        from qtpy.QtCore import QSettings
        s = QSettings("ferroDAC", "ferroDAC")
        cur = s.value("editor/command", "", type=str) or ""
        text, ok = QInputDialog.getText(
            self, "External editor command",
            "Command to open a file (use {file} for the path; blank = OS default).\n"
            "e.g.   konsole -e nvim {file}",
            text=cur)
        if ok:
            s.setValue("editor/command", text.strip())
            self.statusBar().showMessage(
                f"External editor: {text.strip() or 'OS default'}", 5000)

    def _add_panel(self, kind: str) -> None:
        """Add a dashboard panel (the &Add menu). A new Document panel opens on the
        active project's README by default — same starting point as the Docs dock."""
        pid = self.dashboard.add_panel(kind)
        if kind == "doc":
            panel = self.dashboard.panel(pid)
            self._wire_doc_panels()
            readme = self._active_readme()
            if panel is not None and readme:
                panel.open(readme)

    def _wire_doc_panels(self) -> None:
        """Give every Document panel's editor the /rec macro services. Doc panels are
        created generically (Add menu / layout restore), so they can't receive the
        callbacks at construction — wire them here (idempotent)."""
        for panel in self.dashboard.panels():
            if hasattr(panel, "set_doc_macros"):
                panel.set_doc_macros(self._list_recordings,
                                     self._export_recording_for_doc,
                                     self._list_recording_exports,
                                     self._list_processors,
                                     self._processor_source)

    def _active_readme(self) -> str | None:
        """The active project's README.md path, bootstrapping a starter if missing."""
        p = self._project_mgr.active
        return p.ensure_readme() if p is not None else None

    def _open_active_doc(self) -> None:
        """Show the active project's README.md in the Docs dock."""
        if self._docs_view is None:
            return
        readme = self._active_readme()
        if readme:
            self._docs_view.open(readme)
        self._refresh_doc_collab()

    def _refresh_doc_collab(self) -> None:
        """Offer the Docs view's Collaborate toggle when it's showing a HUB
        project's doc and the hub is connected; otherwise hide it (ending any live
        session). doc_id = "<project_id>::README.md" — the server maps it under the
        project's docs/ folder."""
        if self._docs_view is None:
            return
        p = self._project_mgr.active
        doc_id = (f"{p.id}::README.md"
                  if getattr(p, "is_hub", False) and self.hub.connected else None)
        self._docs_view.set_collab_target(self.hub if doc_id else None, doc_id)

    def _lock_chrome(self, editable: bool) -> None:
        """Player + Log docks follow the 'Edit layout' toggle, like the panel
        docks: locked (can't be dragged/floated/closed) when off, freely movable
        when on. Keeps their title bars/tabs so they stay usable while locked."""
        feats = (QDockWidget.DockWidgetMovable | QDockWidget.DockWidgetFloatable
                 | QDockWidget.DockWidgetClosable) if editable \
            else QDockWidget.NoDockWidgetFeatures
        for name in ("player_dock", "log_dock"):
            dock = getattr(self, name, None)
            if dock is not None:
                dock.setFeatures(feats)

    def _on_hub_status(self, msg: str) -> None:
        self.statusBar().showMessage(msg, 6000)

    def _on_hub_connection(self, connected: bool) -> None:
        """Recolour the Hub toolbar button to signal the live link, and seed the
        sync read-out (the SyncRunner then drives it from its background pass)."""
        self.hub_action.setText("ferroDAC Cloud ✓" if connected else "ferroDAC Cloud…")
        btn = self.main_toolbar.widgetForAction(self.hub_action)
        if btn is not None:
            btn.setStyleSheet(
                "QToolButton{color:#0b0f16;background:#3fb950;border-radius:3px;"
                "padding:2px 8px;font-weight:700;}" if connected else "")
        self.sync_status.set_state("connecting" if connected else "offline")
        # surface (or retire) the hub's historic catalog as routable ports
        self.dashboard.refresh_ports()
        if getattr(self, "projects_panel", None) is not None:
            self.projects_panel.refresh()       # enable/disable the “On the hub…” item
        self._refresh_doc_collab()              # offer/retire the Collaborate toggle

    def _open_hub(self):
        if not self.hub.available:
            self.statusBar().showMessage(
                "ferroDAC Cloud needs grpcio — install it in this Python "
                "environment (pip install grpcio).", 8000)
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
        """Where recordings/exports are filed — the ACTIVE project's reports/."""
        p = self._project_mgr.active if getattr(self, "_project_mgr", None) else None
        return p.reports_dir if p is not None else os.path.join(self._app_dir(), "runs")

    # -- projects (curation overlay) ----------------------------------------
    def _setup_projects(self) -> None:
        # a REGISTRY of tracked project folders (anywhere on disk); the active id
        # lives in it too. Migrates Phase-1 projects from the old scanned root.
        reg = os.path.join(self._app_dir(), "projects.json")
        self._project_mgr = ProjectManager(
            reg, hub_cache_dir=os.path.join(self._app_dir(), "hub_cache"))
        self._project_mgr.ensure_default(
            default_dir=os.path.join(self._app_dir(), "projects", "Default"),
            legacy_root=os.path.join(self._app_dir(), "projects"))
        # hub projects sync through this manager (opt-in; the hub is authoritative)
        self.hub.set_projects(self._project_mgr, self._on_hub_projects_changed)
        self._migrate_legacy_session()
        # new tags file under the active project; tags themselves stay GLOBAL
        self.dashboard.markers.default_projects = [self._project_mgr.active.id]
        self._apply_tag_lens()
        self._apply_source_lens()                         # the project's channel lens
        self._load_global_tags()
        self._update_project_title()

    def _apply_tag_lens(self) -> None:
        """Show all projects' tags, or just the active one's (the clutter fix)."""
        p = self._project_mgr.active
        self.dashboard.markers.set_lens(
            None if (self._tags_show_all or p is None) else [p.id])

    def _set_tag_lens_all(self, show_all: bool) -> None:
        self._tags_show_all = bool(show_all)
        self._apply_tag_lens()

    # -- source lens (the project's curated channels) ------------------------
    def _curated_source_keys(self) -> set:
        """The active project's curated channel keys (empty = no curation). The
        single source of truth for the Sources panel AND Timeline lens."""
        p = self._project_mgr.active
        return p.source_keys() if p is not None else set()

    def _apply_source_lens(self) -> None:
        """Filter the Sources view to the project's curated channels. An empty
        selection means 'no lens' (show all) — so a fresh project isn't blank."""
        keys = self._curated_source_keys()
        self.dashboard.set_source_lens(
            None if (self._sources_show_all or not keys) else keys)

    def _set_source_lens_all(self, show_all: bool) -> None:
        self._sources_show_all = bool(show_all)
        self._apply_source_lens()

    def _curate_sources(self) -> None:
        """Pick which channels this project shows (a lens over the catalog)."""
        p = self._project_mgr.active
        if p is None:
            return
        dlg = _SourceCurateDialog(self.dashboard.source_ports(), p.source_keys(), self)
        if dlg.exec():
            p.set_sources([{"key": k} for k in dlg.selected_keys()])
            self._apply_source_lens()
            self._refresh_explorer()               # the Channels group reflects it
            self._republish_active_if_hub()        # sync the lens if it's a hub project

    # -- global tags (one catalog, filtered by the active project lens) ------
    def _global_tags_path(self) -> str:
        return os.path.join(self._app_dir(), "tags.json")

    def _load_global_tags(self) -> None:
        path = self._global_tags_path()
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    self.dashboard.markers.from_list(json.load(fh))
                return
            except Exception:                       # noqa: BLE001
                pass
        # one-time migration: lift markers embedded in a legacy session / working
        # layout into the global tag store.
        for src in (os.path.join(self._app_dir(), "session.json"),
                    self._project_mgr.active.working_path):
            if src and os.path.exists(src):
                try:
                    with open(src, encoding="utf-8") as fh:
                        embedded = json.load(fh).get("layout", {}).get("markers", [])
                except Exception:                   # noqa: BLE001
                    embedded = []
                if embedded:
                    self.dashboard.markers.from_list(embedded)
                    self._save_global_tags()
                    return

    def _schedule_tag_save(self):
        if getattr(self, "_autosave_on", False):
            self._tag_save_timer.start()

    def _save_global_tags(self):
        path = self._global_tags_path()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.dashboard.markers.to_list(), fh)
            os.replace(tmp, path)                    # atomic
        except Exception:                            # noqa: BLE001
            pass

    def _migrate_legacy_session(self) -> None:
        """Carry an existing global session.json into the Default project's
        working layout, so upgrading users keep their dashboard (zero migration)."""
        legacy = os.path.join(self._app_dir(), "session.json")
        p = self._project_mgr.active
        if p is not None and os.path.exists(legacy) and not os.path.exists(p.working_path):
            try:
                shutil.copy2(legacy, p.working_path)
            except Exception:                       # noqa: BLE001
                pass

    def _update_project_title(self) -> None:
        p = self._project_mgr.active
        self.setWindowTitle(f"ferroDAC — {p.name}" if p else "ferroDAC")
        if getattr(self, "projects_panel", None) is not None:
            self.projects_panel.refresh()

    def _add_project(self) -> None:
        """Pick a folder → ADOPT the project there if it already is one, else create
        a new one in it. Either way the folder is tracked in the registry."""
        from ..core.projects import is_project
        folder = QFileDialog.getExistingDirectory(self, "Project folder")
        if not folder:
            return
        if is_project(folder):
            p = self._project_mgr.track(folder)           # adopt existing
        else:
            base = os.path.basename(folder.rstrip("/\\")) or "Project"
            name, ok = QInputDialog.getText(self, "New project", "Project name:",
                                            text=base)
            if not ok or not name.strip():
                return
            p = self._project_mgr.track(folder, name.strip())   # create here
        self.projects_panel.refresh()
        self._switch_project(p.id)

    def _add_project_hub(self) -> None:
        """Create a NEW project on the hub (opt-in). It's published as a record; the
        hub materialises a folder and echoes it back as a ☁ project. We apply it
        optimistically so it appears at once, then switch to it."""
        if not self.hub.connected:
            self.statusBar().showMessage("Connect to a hub first (☁ Hub).", 6000)
            return
        name, ok = QInputDialog.getText(self, "New hub project", "Project name:")
        if not ok or not name.strip():
            return
        import uuid
        rec = {"id": uuid.uuid4().hex, "name": name.strip(), "version": 1,
               "sources": [], "windows": [], "layouts": {}, "deleted": False}
        self._project_mgr.apply_hub_record(rec)           # optimistic local
        self.hub.publish_project(rec)                     # push up (echo is idempotent)
        self.projects_panel.refresh()
        self._switch_project(rec["id"])

    def _share_project(self, pid: str) -> None:
        """Promote (MOVE) a LOCAL project to the hub: publish its record, render it
        as a ☁ project (same id) and untrack the local entry — the local folder
        stays on disk as an offline backup, and takes over again if the hub drops."""
        if not self.hub.connected:
            self.statusBar().showMessage("Connect to a hub first (☁ Hub).", 6000)
            return
        rec = self._project_mgr.share_to_hub(pid)
        if not rec:
            return
        self._project_mgr.apply_hub_record(rec)           # now a ☁ project (same id)
        self._project_mgr.untrack(pid)                    # drop the local entry
        self.hub.publish_project(rec)
        self.projects_panel.refresh()
        self._refresh_explorer()
        self.statusBar().showMessage(f"Shared “{rec.get('name')}” to the hub.", 5000)

    def _republish_active_if_hub(self) -> None:
        """A local edit to a hub project — bump its version and push the record up."""
        p = self._project_mgr.active
        if getattr(p, "is_hub", False):
            self.hub.publish_project(p.bump())

    def _reveal_path(self, path: str) -> None:
        from qtpy.QtCore import QUrl
        from qtpy.QtGui import QDesktopServices
        if path and os.path.exists(path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def _reveal_project(self) -> None:
        p = self._project_mgr.active
        if p is not None:
            self._reveal_path(p.path)

    def _refresh_explorer(self) -> None:
        ex = getattr(self, "project_explorer", None)
        if ex is not None:
            ex.refresh()

    def _on_hub_projects_changed(self) -> None:
        """Hub projects arrived / changed / vanished (sync or disconnect) — refresh
        the Projects views. Runs on the GUI thread (queued from the sync)."""
        if getattr(self, "projects_panel", None) is not None:
            self.projects_panel.refresh()
        self._refresh_explorer()

    # -- docs (reference files filed under the project) ----------------------
    def _add_doc(self) -> None:
        """Pick file(s) and copy them into the project's docs/ — they then show as
        cards. (The folder is the source of truth; you can also just drop files in.)"""
        p = self._project_mgr.active
        if p is None:
            return
        paths, _ = QFileDialog.getOpenFileNames(self, "Add document(s) to project")
        for src in paths:
            try:
                p.import_doc(src)
            except Exception as exc:                       # noqa: BLE001
                self.statusBar().showMessage(f"Could not add {os.path.basename(src)}: {exc}", 6000)
        if paths:
            self._refresh_explorer()

    # -- favourites: saved time-windows (bookmarks) --------------------------
    def _add_bookmark(self) -> None:
        """Bookmark the current timeline window under a name (a nav aid)."""
        p = self._project_mgr.active
        if p is None or self.time_context is None:
            self.statusBar().showMessage("No timeline window to bookmark.", 5000)
            return
        t0, t1 = self.time_context.window
        name, ok = QInputDialog.getText(
            self, "Add bookmark", "Name this window:",
            text=time.strftime("%b %d, %H:%M", time.localtime(t0)))
        name = name.strip()
        if ok and name:
            p.add_window(name, t0, t1)
            self._refresh_explorer()
            self._republish_active_if_hub()
            self.statusBar().showMessage(f"Bookmarked “{name}”", 4000)

    def _jump_to_window(self, t0, t1) -> None:
        """Jump the timeline to a saved window (park + frame), like a recording."""
        if self.time_context is not None:
            self.time_context.park_window(t0, t1)
        self.dashboard.zoom_to(t0, t1)

    def _remove_bookmark(self, name) -> None:
        p = self._project_mgr.active
        if p is not None:
            p.remove_window(name)
            self._refresh_explorer()
            self._republish_active_if_hub()

    def _switch_project(self, pid: str) -> None:
        """Make `pid` active: persist the current project's working layout, then
        swap the dashboard to the target's (devices stay — they're global)."""
        mgr = self._project_mgr
        if mgr.active is None or mgr.active.id == pid or mgr.get(pid) is None:
            return
        try:
            self._write_session(mgr.active.working_path)   # save current
        except Exception:                          # noqa: BLE001
            pass
        mgr.set_active(pid)                               # persists to the registry
        self._active_layout_path = None                   # not in a named layout yet
        self.dashboard.markers.default_projects = [pid]   # new tags file here
        self._apply_tag_lens()                            # and the view filters to it
        self._apply_source_lens()                         # follow the channel lens too
        wp = mgr.active.working_path
        layout = {}
        existed = os.path.exists(wp)
        if existed:
            try:
                with open(wp, encoding="utf-8") as fh:
                    layout = json.load(fh).get("layout", {})
            except Exception:                      # noqa: BLE001
                layout = {}
        self.dashboard.import_layout(layout)       # swap panels/routes/markers only
        if not existed and not self.dashboard.panels():
            self.dashboard.add_panel("chart")      # fresh project → a default chart
        self._wire_doc_panels()                    # macro services for any doc panels
        self._update_project_title()
        self._refresh_explorer()                   # show the new project's contents
        if self._docs_view is not None:
            self._open_active_doc()                # follow the switch to its README
        self.statusBar().showMessage(f"Project: {mgr.active.name}", 5000)

    def _on_export(self):
        """File ▸ Export CSV — materialise the current TIMELINE WINDOW for ALL
        available sources (scalars + spectra), read through the resolver (RAM +
        local store + hub), into a self-describing, reimportable bundle. So you
        can export anything you can see — not just the RAM ring or a recording.
        Per-recording slice export still lives on each Events-dock card."""
        if self.resolver is None:
            self.statusBar().showMessage("Durable store unavailable — export disabled", 6000)
            return
        sources = self.dashboard.export_sources()
        if not sources:
            self.statusBar().showMessage("Nothing to export — no data sources yet.", 5000)
            return
        if self.time_context is not None:
            t0, t1 = self.time_context.window
        else:
            t0, t1 = time.time() - 3600, time.time()
        folder = QFileDialog.getExistingDirectory(self, "Export window to folder")
        if not folder:
            return
        from ..store import export_window
        stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(t1))
        dest = os.path.join(folder, f"ferrodac_export_{stamp}")
        try:
            man = export_window(dest, sources, self.resolver, t0, t1,
                                tags=self.dashboard.markers.to_list())
        except Exception as exc:                       # noqa: BLE001
            self.statusBar().showMessage(f"Export failed: {exc}", 8000)
            return
        n = len(man.get("sources", []))
        dur = max(0, int(t1 - t0))
        self.statusBar().showMessage(
            f"Exported {n} source(s) over {dur} s → {dest}", 8000)

    # -- recording-region actions (from the Events dock) ---------------------
    def _jump_to_tag(self, mid):
        """Jump the timeline to a tag (a point in time): park a window of the
        current width centred on it, which re-streams that slice so you actually
        land on the data around the tag — the point analogue of Zoom-to-recording."""
        m = self.dashboard.markers.get(mid)
        if m is None:
            return
        if self.time_context is not None:
            w = max(1.0, self.time_context.width)
            self.time_context.park_window(m.t - w / 2, m.t + w / 2)
            self.dashboard.zoom_to(*self.time_context.window)   # frame charts + waterfalls
        else:
            w = 60.0
            self.dashboard.zoom_to(m.t - w / 2, m.t + w / 2)

    def _zoom_recording(self, mid):
        m = self.dashboard.markers.get(mid)
        if m is None or m.t_end is None:
            return
        # park the timeline window ON the recording so the controller re-streams
        # that slice (its data may not be loaded yet) — then fit the charts to it.
        if self.time_context is not None:
            self.time_context.park_window(m.t, m.t_end)
        self.dashboard.zoom_to(m.t, m.t_end)

    def _export_recording_csv(self, mid):
        """Export a recording's span as the same self-describing bundle as
        File ▸ Export — read through the resolver, so it includes TRACE sources
        (spectra) too, not just the scalar capture set the old path saw."""
        m = self.dashboard.markers.get(mid)
        if m is None or m.t_end is None:
            return
        if self.resolver is None:
            self.statusBar().showMessage("Durable store unavailable — export disabled", 6000)
            return
        sources = self.dashboard.export_sources()
        if not sources:
            self.statusBar().showMessage("Nothing to export — no data sources.", 5000)
            return
        folder = QFileDialog.getExistingDirectory(self, "Export recording to folder")
        if not folder:
            return
        from ..store import export_window
        label = re.sub(r"[^\w.-]", "_", m.label or "recording").strip("_") or "recording"
        stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(m.t))
        dest = os.path.join(folder, f"{label}_{stamp}")
        try:
            man = export_window(dest, sources, self.resolver, m.t, m.t_end,
                                tags=self.dashboard.markers.to_list())
        except Exception as exc:                       # noqa: BLE001
            self.statusBar().showMessage(f"Export failed: {exc}", 8000)
            return
        n = len(man.get("sources", []))
        if n == 0:
            self.statusBar().showMessage("This recording's window has no stored data.", 6000)
            return
        self.statusBar().showMessage(f"Exported {n} source(s) → {dest}", 8000)

    def _export_plots(self, mid=None):
        """The rec-card 🖼 Plots button: render THIS recording's charts (through the
        processors, via the shared park+ImageExporter helper) into the canonical
        reports/<run>/plots/ — so the /rec macro finds them — and ALSO copy them to a
        folder of your choosing. One render, two homes."""
        m = self.dashboard.markers.get(mid) if mid else None
        if m is None or m.t_end is None:
            self.statusBar().showMessage("Select a finished recording to export.", 6000)
            return
        dest = self._recording_run_dir(m)
        if dest is None:
            self.statusBar().showMessage("No active project — can't export plots.", 6000)
            return
        files = self._render_recording_plots(mid, os.path.join(dest, "plots"))
        if not files:
            self.statusBar().showMessage("No charts to export (or the window is empty).", 6000)
            return
        folder = QFileDialog.getExistingDirectory(self, "Also save a copy to…")
        extra = 0
        if folder:
            import shutil
            for f in files:
                try:
                    shutil.copy2(f["abspath"], os.path.join(folder, os.path.basename(f["abspath"])))
                    extra += 1
                except Exception:              # noqa: BLE001
                    pass
        tail = f" (+ copy → {folder})" if extra else ""
        self.statusBar().showMessage(
            f"Exported {len(files)} plot(s) → project reports{tail}", 7000)

    # -- editor /proc macro: cite a processor's source (open science) --------
    def _list_processors(self) -> list:
        """The DISTINCT processor kinds in use (source is per-class, so dedupe by
        kind) for the editor's /proc macro: [{kind, label}]."""
        seen = {}
        for proc in self.dashboard._processors.values():
            if proc.kind not in seen:
                seen[proc.kind] = {"kind": proc.kind,
                                   "label": getattr(type(proc), "label", proc.kind)}
        return list(seen.values())

    def _processor_source(self, kind: str) -> str:
        """The source of a used processor's class — so its analysis can be pasted,
        readable, into a doc (open science)."""
        import inspect
        for proc in self.dashboard._processors.values():
            if proc.kind == kind:
                try:
                    return inspect.getsource(type(proc))
                except Exception:                  # noqa: BLE001 — e.g. C-defined / no source
                    return ""
        return ""

    # -- editor /rec macro: list recordings + export one on demand -----------
    def _list_recordings(self) -> list:
        """Recordings (closed REC spans) for the editor's /rec macro: id, label, span."""
        out = []
        for m in self.dashboard.markers.of_kind(RECORDING):
            if m.t_end is None:
                continue
            out.append({"id": m.id, "label": m.label or "recording",
                        "t0": float(m.t), "t1": float(m.t_end)})
        return out

    def _recording_run_dir(self, m, create: bool = True):
        """The canonical reports/<run>/ folder for a recording — shared by the CSV
        and plot exports (and the /rec macro) so a recording's artifacts land together.
        Reuses the marker's run_dir if set; else derives <label>_<stamp> under the
        active project and remembers it on the marker."""
        p = self._project_mgr.active
        if p is None:
            return None
        dest = m.run_dir if (m.run_dir and os.path.isdir(m.run_dir)) else None
        if dest is None:
            label = re.sub(r"[^\w.-]", "_", m.label or "recording").strip("_") or "recording"
            stamp = time.strftime("%Y-%m-%dT%H-%M-%S", time.localtime(m.t))
            dest = os.path.join(p.reports_dir, f"{label}_{stamp}")
        if create:
            os.makedirs(dest, exist_ok=True)
            if m.run_dir != dest:
                try:
                    self.dashboard.markers.update(m.id, run_dir=dest)
                except Exception:                  # noqa: BLE001
                    pass
        return dest

    def _render_recording_plots(self, rec_id: str, dest_dir: str, spec=None) -> list:
        """Render a recording's charts to PNGs via the REAL pipeline so the dataflow
        PROCESSORS apply (charts aren't raw Zarr): park the timeline on the recording —
        re-streaming the slice through the processor graph into the panels — then
        ImageExporter the populated plots at the configured resolution (off-screen,
        independent of on-screen size), and restore the prior view. A deliberate,
        progress-pumped action. Returns [{name, abspath, kind}]."""
        from qtpy.QtWidgets import QApplication
        from qtpy.QtCore import Qt
        from pyqtgraph.exporters import ImageExporter
        m = self.dashboard.markers.get(rec_id)
        if m is None or m.t_end is None:
            return []
        charts = [p for p in self.dashboard.panels()
                  if getattr(p, "export_item", None) and p.export_item() is not None]
        if not charts:
            return []
        os.makedirs(dest_dir, exist_ok=True)
        tc = self.time_context
        was_following = tc.following if tc is not None else False
        prev = tc.window if tc is not None else None
        QApplication.setOverrideCursor(Qt.WaitCursor)
        self.statusBar().showMessage("Rendering recording plots…")
        out = []
        try:
            for p in charts:                       # clean figures — no record overlay
                if hasattr(p, "set_regions_visible"):
                    p.set_regions_visible(False)
            if tc is not None:                     # ← LOADS the slice through processors
                tc.park_window(m.t, m.t_end)
            self.dashboard.zoom_to(m.t, m.t_end)
            QApplication.processEvents()           # flush the re-stream + repaint
            for p in charts:
                pi = p.export_item()
                if pi is None:
                    continue
                pspec = spec or self.dashboard.export_spec_for(p)   # per-panel resolution
                png = os.path.join(dest_dir, f"{p.panel_id}.png")
                try:
                    exporter = ImageExporter(pi)
                    exporter.parameters()["width"] = int(pspec.get("width", 1600))
                    if int(pspec.get("height", 0)) > 0:
                        exporter.parameters()["height"] = int(pspec["height"])
                    exporter.export(png)
                    self._tag_png_dpi(png, int(pspec.get("dpi", 0)))
                    if os.path.exists(png):
                        out.append({"name": getattr(p, "title", "") or p.panel_id,
                                    "abspath": png, "kind": "plot"})
                except Exception:                  # noqa: BLE001 — skip a bad panel
                    pass
        finally:
            for p in charts:
                if hasattr(p, "set_regions_visible"):
                    p.set_regions_visible(True)
            if tc is not None:                     # put the user back where they were
                if was_following:
                    tc.follow_now()
                elif prev is not None:
                    tc.park_window(*prev)
                    self.dashboard.zoom_to(*prev)
            QApplication.restoreOverrideCursor()
            self.statusBar().clearMessage()
        return out

    @staticmethod
    def _tag_png_dpi(png: str, dpi: int) -> None:
        """Write the DPI into a freshly-exported PNG (ImageExporter sets pixels, not
        DPI) so consumers like Word/LaTeX place it at the intended physical size."""
        if dpi <= 0 or not os.path.exists(png):
            return
        try:
            from qtpy.QtGui import QImage
            img = QImage(png)
            if img.isNull():
                return
            dpm = int(round(dpi / 0.0254))         # dots per metre
            img.setDotsPerMeterX(dpm)
            img.setDotsPerMeterY(dpm)
            img.save(png)
        except Exception:                          # noqa: BLE001
            pass

    def _export_recording_for_doc(self, rec_id: str) -> list:
        """Export-NOW for the /rec macro: render the recording's CSV + plots fresh into
        the canonical reports/<run>/. Returns [{name, abspath, kind}]."""
        m = self.dashboard.markers.get(rec_id)
        if m is None or m.t_end is None or self.resolver is None:
            return []
        dest = self._recording_run_dir(m)
        if dest is None:
            return []
        files = []
        from ..store import export_window
        try:
            export_window(dest, self.dashboard.export_sources(), self.resolver,
                          m.t, m.t_end, tags=self.dashboard.markers.to_list())
            csv = os.path.join(dest, "data.csv")
            if os.path.exists(csv):
                files.append({"name": "data.csv", "abspath": csv, "kind": "csv"})
        except Exception:                          # noqa: BLE001
            pass
        files += self._render_recording_plots(rec_id, os.path.join(dest, "plots"))
        return files

    def _list_recording_exports(self, rec_id: str) -> list:
        """The recording's ALREADY-exported files (the /rec macro lists these first,
        before offering Export-now). Scans the canonical reports/<run>/."""
        m = self.dashboard.markers.get(rec_id)
        if m is None:
            return []
        dest = self._recording_run_dir(m, create=False)
        if not dest or not os.path.isdir(dest):
            return []
        titles = {p.panel_id: (getattr(p, "title", "") or p.panel_id)
                  for p in self.dashboard.panels()}
        out = []
        csv = os.path.join(dest, "data.csv")
        if os.path.exists(csv):
            out.append({"name": "data.csv", "abspath": csv, "kind": "csv"})
        plots = os.path.join(dest, "plots")
        if os.path.isdir(plots):
            for fn in sorted(os.listdir(plots)):
                if fn.lower().endswith(".png"):
                    stem = os.path.splitext(fn)[0]
                    out.append({"name": titles.get(stem, stem),
                                "abspath": os.path.join(plots, fn), "kind": "plot"})
        return out

    def _toggle_record(self):
        """A recording is just a marked SPAN over the always-on durable store.
        Start opens a REC marker (data is already being persisted); Stop closes it
        and auto-exports the span as a bundle from the resolver/Zarr."""
        ms = self.dashboard.markers
        if self._rec_start_mid is None:                 # start
            self._rec_start_mid = ms.add(time.time(), kind=RECORDING, label="REC",
                                         comment="recording…")
            self.record_action.setText("■ Stop")
            self.statusBar().showMessage("● Recording — persisting to the store")
        else:                                            # stop → finalise + export
            mid, self._rec_start_mid = self._rec_start_mid, None
            self.record_action.setText("● Record")
            m = ms.get(mid)
            t0 = m.t if m else time.time()
            t1 = time.time()
            ms.update(mid, t_end=t1)                      # close the region
            self._finalize_recording(mid, t0, t1)

    def _finalize_recording(self, mid, t0, t1) -> None:
        """Flush the store and materialise the span as a self-describing bundle
        (export_window via the resolver — RAM + store + hub). The durable Zarr
        store IS the crash-safe data; there's no separate capture file."""
        if self.store_writer is not None:
            try:
                self.store_writer.flush_all()             # a clean stop loses nothing
            except Exception:                             # noqa: BLE001
                pass
        if self.resolver is None:
            return
        sources = self.dashboard.export_sources()
        from ..store import export_window
        dest = os.path.join(self._runs_dir(),
                            "run_" + time.strftime("%Y-%m-%dT%H-%M-%S", time.localtime(t0)))
        try:
            man = export_window(dest, sources, self.resolver, t0, t1,
                                tags=self.dashboard.markers.to_list())
        except Exception as exc:                          # noqa: BLE001
            self.statusBar().showMessage(f"Recording kept; export failed: {exc}", 8000)
            return
        n = len(man.get("sources", []))
        self.dashboard.markers.update(mid, run_dir=dest, comment=f"{n} sources")
        self._refresh_explorer()                   # the new recording card shows up
        self.statusBar().showMessage(f"■ Saved recording: {n} source(s) → {dest}", 8000)

    def _recover_open_recordings(self) -> None:
        """A recording interrupted by a crash survives as an OPEN REC marker
        (t_end=None) in the restored session — the data is already in the store.
        Finalise the span (t_end = the last data we have) and export the bundle."""
        ms = self.dashboard.markers
        open_recs = [m for m in ms.all() if m.kind == RECORDING and m.t_end is None]
        for m in open_recs:
            t_end = self._last_data_time(m.t) or m.t
            ms.update(m.id, t_end=t_end)
            self._finalize_recording(m.id, m.t, t_end)
        if open_recs:
            self.statusBar().showMessage(
                f"Recovered {len(open_recs)} recording(s) interrupted by a crash.", 8000)

    def _last_data_time(self, t0):
        """Latest stored sample time in [t0, now] across sources (for finalising a
        crashed recording's end). None if nothing was stored."""
        if self.resolver is None:
            return None
        now = time.time()
        latest = None
        for key in self.dashboard.export_sources():
            for a, b in self.resolver.coverage(key):
                if a <= now and b >= t0:
                    latest = min(b, now) if latest is None else max(latest, min(b, now))
        return latest

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
        self._refresh_explorer()                   # a new layout card may have landed
        self.statusBar().showMessage(f"Saved {os.path.basename(path)}", 4000)

    # -- working-session autosave (so tags/layout survive a restart or crash) -
    def _schedule_autosave(self):
        if getattr(self, "_autosave_on", False):
            self._autosave_timer.start()

    def _working_path(self) -> str:
        """The active project's autosaved working layout."""
        return self._project_mgr.active.working_path

    def _do_autosave(self):
        try:
            self._write_session(self._working_path())
            # a named layout open → it tracks live edits too (layouts autosave)
            if self._active_layout_path:
                self._write_session(self._active_layout_path)
                # …and if it's a HUB project, push the layout live (the named layout
                # IS in the shared record; the working layout stays local). Inherits
                # the autosave debounce, so this is ~one push per edit-burst.
                self._republish_active_if_hub()
        except Exception:
            pass

    def _init_session_persistence(self):
        if os.path.exists(self._working_path()):
            QTimer.singleShot(300, self._restore_and_enable_autosave)
        else:
            self._autosave_on = True

    def _restore_and_enable_autosave(self):
        self.open_session(self._working_path())
        self._autosave_on = True
        self._recover_open_recordings()         # finalise any crash-interrupted REC

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

    def _open_layout(self, path: str) -> None:
        """Make `path` the active named layout: load it AND autosave edits back to
        it from now on (a layout behaves like a live document, not a snapshot)."""
        self._active_layout_path = path
        self.open_session(path)
        self._refresh_explorer()                   # mark the active layout

    def _on_add_layout(self):
        """Create a new named layout in the project's layouts/ — name it, the file
        is made for you (no file picker), and it becomes the live, autosaving one."""
        p = self._project_mgr.active
        if p is None:
            return
        name, ok = QInputDialog.getText(self, "Add layout", "Layout name:")
        name = name.strip()
        if not ok or not name:
            return
        path = p.layout_path(name)
        if os.path.exists(path) and QMessageBox.question(
                self, "Replace layout?",
                f"A layout named “{name}” already exists. Replace it?") \
                != QMessageBox.Yes:
            return
        self._write_session(path)                  # snapshot the current dashboard
        self._active_layout_path = path            # …then keep it live (autosaves)
        self._remember(path)
        self._refresh_explorer()
        self._republish_active_if_hub()            # share the named layout if on hub
        self.statusBar().showMessage(f"Added layout “{name}” — it now autosaves", 5000)

    def _on_open(self):
        start = self._project_mgr.active.layouts_dir if self._project_mgr.active else ""
        path, _ = QFileDialog.getOpenFileName(
            self, "Open Layout", start, "ferroDAC layout (*.json)")
        if path:
            self._open_layout(path)

    @staticmethod
    def _remember(path: str) -> None:
        QSettings("ferroDAC", "ferroDAC").setValue("lastSession", path)

    def _replay_progress(self, frac) -> None:
        """ReplayController load progress: frac 0..1 → show the status-bar bar;
        None → hide. Pump the event loop so it actually paints during the
        synchronous load (the real fix for huge slices is an off-thread read)."""
        bar = getattr(self, "_load_bar", None)
        if bar is None:
            return
        if frac is None:
            bar.setVisible(False)
            return
        if not bar.isVisible():
            bar.setVisible(True)
        bar.setValue(max(0, min(100, int(frac * 100))))
        QApplication.processEvents()

    def _historic_sources(self):
        """Recorded channels (key, name, unit, dtype) routable for replay even
        with no live device: the local durable store UNIONED with the hub catalog
        when connected — so a pure viewer can route purely-historic HUB sources
        onto a chart (served via the resolver's hub read tier). Local wins on key
        collision; the hub fills what's only remote."""
        dtmap = {"scalar": "float", "trace": "trace", "bool": "bool"}
        out = {}                                    # key -> (label, unit, dtype)
        if self.store_writer is not None:
            st = self.store_writer.store
            for key in st.sources():
                name, unit, dtype = st.source_meta(key)
                label = name if (name and name != key) else key.rsplit("/", 1)[-1]
                out[key] = (label, unit, dtmap.get(dtype, "float"))
        if getattr(self, "hub", None) is not None:
            for key, name, unit, dtype in self.hub.hub_sources():
                if key not in out:
                    label = name if (name and name != key) else key.rsplit("/", 1)[-1]
                    out[key] = (label, unit, dtmap.get(dtype, "float"))
        return [(k, lbl, u, dt) for k, (lbl, u, dt) in out.items()]

    def _tc_live_tick(self) -> None:
        """Advance the head to now while following (live), and slide the live
        window — trim panels to the window start so live honours slide/grow."""
        tc = self.time_context
        if tc is None:
            return
        tc.tick_live()
        if tc.following:
            self.dashboard.trim_live(tc.window[0])
        self.dashboard.set_time_window(*tc.window)   # waterfalls track the window

    def _tc_play_tick(self) -> None:
        """Walk the parked head forward while playing — a FIXED sim-step per frame
        (speed × 0.05) with the real wall gap measured, so the achieved rate (on
        tc.rate, shown by the player + Timeline HUD) falls below requested when
        frames can't keep up. Settles to live when it catches now."""
        tc = self.time_context
        if tc is None or not tc.playing:
            self._play_wall = None
            return
        now = time.perf_counter()
        wall = (now - self._play_wall) if self._play_wall else 0.05
        self._play_wall = now
        tc.tick_play(0.05)
        if tc.playing:
            ach = min(tc.speed, (tc.speed * 0.05) / max(1e-4, wall))
            tc.rate = 0.7 * tc.rate + 0.3 * ach
        self.dashboard.set_time_window(*tc.window)   # waterfalls follow the playhead

    def _replay_reset(self) -> None:
        """Called by the ReplayController when the head jumps (park / scrub /
        return to live): drop accumulated display data so the panels re-experience
        the new slice from scratch. Charts plot ABSOLUTE time (DateAxis), so no
        origin rebasing — a parked window just shows its real timestamps."""
        for panel in self.dashboard.panels():
            try:
                panel.clear_history()
            except Exception:
                pass
        if self.time_context is not None:           # re-bin waterfalls to the new window
            self.dashboard.set_time_window(*self.time_context.window)

    def closeEvent(self, event):  # noqa: N802
        if self._rec_start_mid is not None:    # close the open recording cleanly
            self.dashboard.markers.update(self._rec_start_mid, t_end=time.time())
            self._rec_start_mid = None         # store_writer.stop() below flushes it
        if self._autosave_on:
            self._do_autosave()
            self._save_global_tags()        # flush the global tag catalog
        self.hub.disconnect()
        if self.replay is not None:
            self.replay.stop()              # unsubscribe the playback bus
        if self.store_writer is not None:
            self.store_writer.stop()        # flush the buffer + build final rollups
        self.dashboard.shutdown()
        self.manager.stop()
        self.engine.shutdown()
        super().closeEvent(event)
        # closing the MAIN window quits the app — otherwise a still-open Timeline or
        # config dialog (a top-level window) keeps the Qt event loop alive and the
        # process never exits. (quitOnLastWindowClosed only fires if nothing lingers.)
        QApplication.quit()


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
        QPushButton:checked, QToolButton:checked { background:#4dabf7;
            color:#0b0b10; border-color:#4dabf7; }
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

    # crash + threading diagnostics: a segfault now prints a Python stack of every
    # thread, and a Qt call from the wrong thread is flagged with its origin stack.
    from ..diagnostics import install as _install_diagnostics
    from ..diagnostics import install_gui_thread_gc
    _install_diagnostics(os.path.dirname(logpath) if logpath else "")

    # QtWebEngine (the in-app Docs view) wants shared GL contexts set BEFORE the
    # QApplication exists. Harmless when WebEngine isn't used.
    try:
        from qtpy.QtCore import Qt as _Qt
        QApplication.setAttribute(_Qt.AA_ShareOpenGLContexts)
    except Exception:                              # noqa: BLE001
        pass

    app = QApplication(sys.argv if argv is None else argv)
    install_gui_thread_gc()                # collect garbage on the GUI thread only —
    #                                        prevents the zarr_io cross-thread-GC segfault
    app.setApplicationName("ferroDAC")
    icon_path = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                             "assets", "app.png")
    if os.path.exists(icon_path):
        app.setWindowIcon(QIcon(icon_path))
    apply_dark_theme(app)

    cfg = QStandardPaths.writableLocation(QStandardPaths.AppConfigLocation)
    registry = DeviceRegistry(os.path.join(cfg, "registry.json") if cfg else None)

    # Load enabled extensions BEFORE the driver scan + the dashboard build, so their
    # processors/widgets/drivers are registered in time (driver_types() then includes
    # extension drivers; the Add menu includes extension widgets). Defensive — a broken
    # extension is logged and skipped, never blocking launch.
    try:
        from ..extensions import ExtensionManager
        ext_root = os.path.join(cfg, "extensions") if cfg else \
            os.path.join(os.path.expanduser("~"), ".ferrodac", "extensions")
        ExtensionManager(ext_root).load_enabled()
    except Exception as exc:                        # noqa: BLE001
        log.warning("extension loading failed: %s", exc)

    drivers = load_builtin_drivers()
    log.info("loaded %d driver(s): %s", len(drivers),
             ", ".join(getattr(d, "driver", "?") for d in drivers) or "—")
    engine = Engine()
    manager = DeviceManager(drivers, engine=engine, registry=registry)
    win = MainWindow(manager, engine)
    win.show()
    return app.exec()
