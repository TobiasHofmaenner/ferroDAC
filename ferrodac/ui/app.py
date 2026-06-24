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
    QTreeWidget,
    QTreeWidgetItem,
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


class DevicesWindow(QMainWindow):
    """The Devices manager as a standalone window (like the Timeline) rather than a
    cramped dock — Available + Active devices, add/remove/configure. Just hosts a
    DevicesPanel; the panel is unchanged."""

    def __init__(self, manager: DeviceManager, on_configure, parent=None):
        super().__init__(parent)
        self.setWindowTitle("ferroDAC — Devices")
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        self.resize(460, 680)
        self.setStyleSheet(
            "QMainWindow,QWidget{background:#0e1116;color:#c7d0db;}"
            "QScrollArea{border:none;}")
        self.panel = DevicesPanel(manager, on_configure)
        self.setCentralWidget(self.panel)


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
            groups.setdefault(port.origin or "Unknown device", []).append(port)
        for origin, gports in groups.items():
            # a processor's outputs group under its name → give that group a Remove
            proc_id = next((p.proc_id for p in gports if getattr(p, "proc_id", "")), "")
            action = (("✕ Remove", lambda pid=proc_id: self.dashboard.remove_processor(pid))
                      if proc_id else None)
            grp = CollapsibleGroup(origin, len(gports), origin in self._collapsed,
                                   self._on_group_toggle, action=action)
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
                h = QLabel(p.origin or "Unknown device")
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


class ProjectNavigator(QWidget):
    """One OneNote-style tree for the whole left side: PROJECTS (notebooks) at the top
    level; the ACTIVE project expands into SECTIONS (Layouts, Channels, Recordings,
    Docs, Bookmarks) → item rows (pages). Click a project to switch to it; click an
    item to open it; right-click for add / remove / share / clone. A VIEW over the
    unchanged ProjectManager/Project model — it scans disk fresh on every refresh,
    keeping no mirrored index. Replaces the old Projects + Project Explorer panels."""

    SECTIONS = ("Layouts", "Channels", "Recordings", "Docs", "Bookmarks")

    def __init__(self, manager, active_layout=None, on_activate=None,
                 on_create_local=None, on_create_hub=None, on_reveal=None,
                 on_share=None, on_clone=None, hub_enabled=None, on_open_layout=None,
                 on_reveal_path=None, on_curate=None, on_add_layout=None,
                 on_add_doc=None, on_add_bookmark=None, on_jump_window=None,
                 on_remove_bookmark=None, on_open_doc=None, on_edit=None, parent=None):
        super().__init__(parent)
        self.manager = manager
        self._active_layout = active_layout or (lambda: None)
        self._on_edit = on_edit                         # (verb, payload) edit dispatcher
        self._on_activate = on_activate
        self._on_create_local = on_create_local
        self._on_create_hub = on_create_hub
        self._on_reveal = on_reveal                     # reveal the active project folder
        self._on_share = on_share
        self._on_clone = on_clone
        self._hub_enabled = hub_enabled or (lambda: False)
        self._on_open_layout = on_open_layout
        self._on_reveal_path = on_reveal_path           # reveal a recording's folder
        self._on_curate = on_curate
        self._on_add_layout = on_add_layout
        self._on_add_doc = on_add_doc
        self._on_add_bookmark = on_add_bookmark
        self._on_jump_window = on_jump_window
        self._on_remove_bookmark = on_remove_bookmark
        self._on_open_doc = on_open_doc                 # open a doc in the in-app view
        self._expanded = None        # set of stable keys; None = first build → expand all

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)
        self._label = QLabel("Workspace")
        self._label.setStyleSheet("font-size:12px; font-weight:700; color:#c7d0db;")
        root.addWidget(self._label)
        self._tree = QTreeWidget()
        self._tree.setHeaderHidden(True)
        self._tree.setColumnCount(1)
        self._tree.setIndentation(14)
        self._tree.setExpandsOnDoubleClick(False)       # single click drives everything
        self._tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._context_menu)
        self._tree.itemClicked.connect(self._on_clicked)
        self._tree.itemDoubleClicked.connect(self._on_double_clicked)
        self._tree.itemSelectionChanged.connect(self._update_action_bar)
        root.addWidget(self._tree, 1)
        # bottom: a persistent "＋ Project" + a CONTEXT-SENSITIVE action bar showing the
        # selected node's actions (open / rename / delete / add / share / …).
        bottom = QHBoxLayout()
        new = QToolButton()
        new.setText("＋ Project")
        new.setToolTip("Add a project")
        new.setToolButtonStyle(Qt.ToolButtonTextOnly)
        new.setPopupMode(QToolButton.InstantPopup)
        menu = QMenu(new)
        menu.addAction("Local folder…", lambda: self._on_create_local())
        self._hub_action = menu.addAction("On the hub…", lambda: self._on_create_hub())
        self._hub_action.setToolTip("Create a shared project on the hub (needs a connection)")
        new.setMenu(menu)
        bottom.addWidget(new)
        self._action_host = QWidget()
        self._action_bar = QHBoxLayout(self._action_host)
        self._action_bar.setContentsMargins(0, 0, 0, 0)
        self._action_bar.setSpacing(4)
        bottom.addWidget(self._action_host, 1)
        root.addLayout(bottom)
        self.refresh()

    # -- build ---------------------------------------------------------------
    def _active_id(self):
        a = self.manager.active
        return a.id if a is not None else None

    def refresh(self):
        if self._expanded is not None:           # remember which nodes are open
            self._expanded = self._snapshot()
        self._tree.blockSignals(True)
        self._tree.clear()
        active = self.manager.active
        aid = active.id if active is not None else None
        projs = self.manager.projects()
        self._label.setText(f"Workspace  ({len(projs)})")
        self._hub_action.setEnabled(self._hub_enabled())
        if self._expanded is None:               # first build → active project + sections open
            self._expanded = {("project", aid)} | {("section", s) for s in self.SECTIONS}
        for p in projs:
            on = aid is not None and p.id == aid
            pit = QTreeWidgetItem(self._tree)
            badge = "☁ " if getattr(p, "is_hub", False) else ""
            pit.setText(0, ("●  " if on else "○  ") + badge + p.name)
            pit.setData(0, Qt.UserRole, {"t": "project", "id": p.id})
            if on:
                f = pit.font(0); f.setBold(True); pit.setFont(0, f)
            tip = p.description or ""
            if getattr(p, "is_hub", False):
                tip = (tip + "  ·  " if tip else "") + "on the hub"
            if tip:
                pit.setToolTip(0, tip)
            if on:                               # only the active project shows sections
                self._build_sections(pit, p)
                pit.setExpanded(("project", p.id) in self._expanded)
            else:
                pit.setChildIndicatorPolicy(QTreeWidgetItem.DontShowIndicator)
        self._tree.blockSignals(False)
        self._update_action_bar()                # selection was cleared → clear the bar

    def _build_sections(self, pit, p):
        self._section(pit, "Layouts", self._layout_rows(p))
        self._section(pit, "Channels", self._channel_rows(p))
        self._section(pit, "Recordings", self._recording_rows(p))
        self._section(pit, "Docs", self._doc_rows(p))
        self._section(pit, "Bookmarks", self._bookmark_rows(p))

    def _section(self, pit, name, rows):
        sit = QTreeWidgetItem(pit)
        sit.setText(0, f"{name}  ({len(rows)})")
        sit.setData(0, Qt.UserRole, {"t": "section", "name": name})
        f = sit.font(0); f.setBold(True); sit.setFont(0, f)
        sit.setForeground(0, QColor("#9aa4b2"))
        for text, payload, tip in rows:
            cit = QTreeWidgetItem(sit)
            cit.setText(0, text)
            cit.setData(0, Qt.UserRole, payload)
            if tip:
                cit.setToolTip(0, tip)
        sit.setExpanded(("section", name) in (self._expanded or set()))

    # -- per-section rows: (text, payload, tooltip) — reuse the Project model --
    def _layout_rows(self, p):
        active = self._active_layout()
        rows = []
        for name in p.layouts():
            path = p.layout_path(name)
            n = p.layout_panels(name)
            sub = f"{n} panel{'' if n == 1 else 's'}" if n else "layout"
            is_active = active is not None and os.path.abspath(path) == os.path.abspath(active)
            label = ("● " if is_active else "") + name + ("  ·  autosaving" if is_active else "")
            rows.append((label, {"t": "layout", "path": path, "active": is_active}, sub))
        return rows

    def _channel_rows(self, p):
        rows = []
        for s in p.sources():
            key = s.get("key") if isinstance(s, dict) else s
            label = (isinstance(s, dict) and s.get("label")) or key
            rows.append((label, {"t": "channel", "key": key}, key))
        return rows

    def _recording_rows(self, p):
        return [(self._rec_title(r), {"t": "recording", "path": r["path"]}, self._rec_sub(r))
                for r in p.recordings()]

    @staticmethod
    def _rec_title(r):
        t0 = r.get("t0")
        return time.strftime("%b %d, %H:%M", time.localtime(t0)) if t0 else r["name"]

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

    def _doc_rows(self, p):
        return [(d["name"], {"t": "doc", "path": d["path"]}, d.get("ext") or "file")
                for d in p.docs()]

    def _bookmark_rows(self, p):
        rows = []
        for w in p.windows():
            name, t0, t1 = w.get("name", "window"), w.get("t0"), w.get("t1")
            sub = (f"{time.strftime('%b %d, %H:%M', time.localtime(t0))}  ·  {_dur(t1 - t0)}"
                   if t0 and t1 and t1 >= t0 else "")
            rows.append((name, {"t": "bookmark", "name": name, "t0": t0, "t1": t1}, sub))
        return rows

    # -- interaction: single click selects/switches, double click opens ------
    def _on_clicked(self, item, _col=0):
        pay = item.data(0, Qt.UserRole) or {}
        t = pay.get("t")
        if t == "section":
            item.setExpanded(not item.isExpanded())
        elif t == "project":
            if pay["id"] == self._active_id():
                item.setExpanded(not item.isExpanded())
            elif self._on_activate is not None:          # switch (deferred — rebuilds tree)
                QTimer.singleShot(0, lambda pid=pay["id"]: self._on_activate(pid))
        # item rows: single-click only SELECTS → the bottom bar shows its actions;
        # double-click opens (the primary action) so a select-click never opens.

    def _on_double_clicked(self, item, _col=0):
        pay = item.data(0, Qt.UserRole) or {}
        if pay.get("t") in ("layout", "recording", "doc", "bookmark"):
            QTimer.singleShot(0, lambda d=dict(pay): self._open_item(d))

    def _open_item(self, pay):
        t = pay.get("t")
        if t == "layout" and not pay.get("active") and self._on_open_layout:
            self._on_open_layout(pay["path"])
        elif t == "recording" and self._on_reveal_path:
            self._on_reveal_path(pay["path"])
        elif t == "doc":
            cb = self._on_open_doc or self._on_reveal_path
            if cb:
                cb(pay["path"])
        elif t == "bookmark" and self._on_jump_window and pay.get("t0") and pay.get("t1"):
            self._on_jump_window(pay["t0"], pay["t1"])

    def _actions_for(self, pay) -> list:
        """[(label, callback|None)] — the action/edit set for a node, used by BOTH the
        right-click menu and the context-sensitive bottom bar. None = disabled. The new
        edit verbs (rename/delete/duplicate/…) route through the single `on_edit`."""
        t = pay.get("t")
        e = self._on_edit or (lambda _v, _p: None)
        out = []
        if t == "project":
            p = self.manager.get(pay.get("id"))
            if p is None:
                return out
            out.append(("✎ Rename", lambda d=dict(pay): e("rename_project", d)))
            if not getattr(p, "is_hub", False):
                if self._on_share is not None:
                    out.append(("☁ Share", (lambda pid=p.id: self._on_share(pid))
                                if self._hub_enabled() else None))
                if self._on_reveal is not None:
                    out.append(("📂 Reveal", lambda: self._on_reveal()))
            elif self._on_clone is not None and getattr(p, "git_remote", ""):
                out.append(("⬇ Clone…", lambda pid=p.id: self._on_clone(pid)))
            out.append(("⌫ Remove", lambda d=dict(pay): e("remove_project", d)))
        elif t == "section":
            adders = {"Layouts": (self._on_add_layout, "＋ Add layout"),
                      "Channels": (self._on_curate, "✔ Curate…"),
                      "Docs": (self._on_add_doc, "＋ Add doc…"),
                      "Bookmarks": (self._on_add_bookmark, "＋ Add bookmark")}
            cb, label = adders.get(pay.get("name"), (None, ""))
            if cb is not None:
                out.append((label, lambda cb=cb: cb()))
        elif t == "layout":
            if not pay.get("active") and self._on_open_layout:
                out.append(("↗ Open", lambda path=pay["path"]: self._on_open_layout(path)))
            out.append(("✎ Rename", lambda d=dict(pay): e("rename_layout", d)))
            out.append(("⧉ Duplicate", lambda d=dict(pay): e("duplicate_layout", d)))
            out.append(("✕ Delete", lambda d=dict(pay): e("delete_layout", d)))
        elif t == "channel":
            out.append(("✕ Uncurate", lambda d=dict(pay): e("uncurate", d)))
        elif t == "recording":
            if self._on_reveal_path:
                out.append(("📂 Reveal", lambda path=pay["path"]: self._on_reveal_path(path)))
            out.append(("✕ Delete", lambda d=dict(pay): e("delete_recording", d)))
        elif t == "doc":
            cb = self._on_open_doc or self._on_reveal_path
            if cb:
                out.append(("↗ Open", lambda path=pay["path"], cb=cb: cb(path)))
            out.append(("✎ Rename", lambda d=dict(pay): e("rename_doc", d)))
            out.append(("✕ Delete", lambda d=dict(pay): e("delete_doc", d)))
        elif t == "bookmark":
            if self._on_jump_window and pay.get("t0") and pay.get("t1"):
                out.append(("⌖ Jump",
                            lambda t0=pay["t0"], t1=pay["t1"]: self._on_jump_window(t0, t1)))
            out.append(("✎ Rename", lambda d=dict(pay): e("rename_bookmark", d)))
            if self._on_remove_bookmark:
                out.append(("✕ Delete", lambda name=pay.get("name"): self._on_remove_bookmark(name)))
        return out

    def _context_menu(self, pos):
        item = self._tree.itemAt(pos)
        if item is None:
            return
        actions = self._actions_for(item.data(0, Qt.UserRole) or {})
        if not actions:
            return
        menu = QMenu(self)
        for label, cb in actions:
            a = menu.addAction(label)
            if cb is None:
                a.setEnabled(False)
            else:
                a.triggered.connect(lambda _=False, cb=cb: cb())
        menu.exec(self._tree.mapToGlobal(pos))

    def _update_action_bar(self) -> None:
        """Rebuild the bottom bar to the SELECTED node's actions (or empty)."""
        clear_layout(self._action_bar)
        items = self._tree.selectedItems()
        if not items:
            return
        for label, cb in self._actions_for(items[0].data(0, Qt.UserRole) or {}):
            b = QToolButton()
            b.setText(label)
            b.setToolButtonStyle(Qt.ToolButtonTextOnly)
            if cb is None:
                b.setEnabled(False)
            else:
                b.clicked.connect(lambda _=False, cb=cb: cb())
            self._action_bar.addWidget(b)
        self._action_bar.addStretch(1)

    # -- expand-state preservation across the imperative refresh -------------
    def _snapshot(self) -> set:
        keys = set()
        root = self._tree.invisibleRootItem()
        for i in range(root.childCount()):
            pit = root.child(i)
            pp = pit.data(0, Qt.UserRole) or {}
            if pit.isExpanded() and pp.get("t") == "project":
                keys.add(("project", pp["id"]))
            for j in range(pit.childCount()):
                sit = pit.child(j)
                sp = sit.data(0, Qt.UserRole) or {}
                if sit.isExpanded() and sp.get("t") == "section":
                    keys.add(("section", sp["name"]))
        return keys

    # -- stable query surface (for tests; no QTreeWidgetItem traversal needed)
    def project_ids(self) -> list:
        root = self._tree.invisibleRootItem()
        return [(root.child(i).data(0, Qt.UserRole) or {}).get("id")
                for i in range(root.childCount())]

    @property
    def active_project_name(self):
        a = self.manager.active
        return a.name if a is not None else None

    def _active_section(self, name):
        root = self._tree.invisibleRootItem()
        aid = self._active_id()
        for i in range(root.childCount()):
            pit = root.child(i)
            if (pit.data(0, Qt.UserRole) or {}).get("id") != aid:
                continue
            for j in range(pit.childCount()):
                sit = pit.child(j)
                if (sit.data(0, Qt.UserRole) or {}).get("name") == name:
                    return sit
        return None

    def section_items(self, name) -> list:
        sit = self._active_section(name)
        return ([sit.child(k).data(0, Qt.UserRole) for k in range(sit.childCount())]
                if sit is not None else [])

    def is_section_expanded(self, name) -> bool:
        sit = self._active_section(name)
        return bool(sit is not None and sit.isExpanded())


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


# --------------------------------------------------------------------------- #
#  Main window — dockable shell
# --------------------------------------------------------------------------- #
class MainWindow(QMainWindow):
    def __init__(self, manager: DeviceManager, engine: Engine, parent=None,
                 restore_last: bool = True, extensions=None):
        super().__init__(parent)
        self.manager = manager
        self.engine = engine
        self._restore_last = restore_last
        self._extensions = extensions      # ExtensionManager (the Extensions dialog uses it)
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
            # freeze device provenance ALONGSIDE the data: push the merged snapshot
            # (descriptor + user metadata) whenever the active set changes.
            manager.active_changed.connect(self._push_device_records)
            self._push_device_records()
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
        # project git history (DESIGN §8.2): boundary commits are immediate; doc edits
        # debounce through this timer so a burst of edits → one "settled" commit.
        self._commit_timer = QTimer(self)
        self._commit_timer.setSingleShot(True)
        self._commit_timer.setInterval(15000)
        self._commit_timer.timeout.connect(self._do_scheduled_commit)
        self._pending_commit_msg = "Edited documents"
        self._pending_share: dict = {}      # pid -> local path: push its history once the
        #                                     hub provisions a repo (git_remote arrives)
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

        self._devices_win = None        # the Devices manager opens as a window (below)

        # projects: a curation overlay over the global catalog (the active project
        # owns the working layout; Phase 2 adds the tag lens).
        self._setup_projects()
        # ONE OneNote-style tree for the whole left side: projects (notebooks) →
        # the active one's Layouts/Channels/Recordings/Docs/Bookmarks (sections) →
        # items (pages). A view over the unchanged ProjectManager/Project model.
        self.navigator = ProjectNavigator(
            self._project_mgr, active_layout=lambda: self._active_layout_path,
            on_activate=self._switch_project, on_create_local=self._add_project,
            on_create_hub=self._add_project_hub, on_reveal=self._reveal_project,
            on_share=self._share_project, on_clone=self._clone_hub_project,
            hub_enabled=lambda: self.hub.connected, on_open_layout=self._open_layout,
            on_reveal_path=self._reveal_path, on_curate=self._curate_sources,
            on_add_layout=self._on_add_layout, on_add_doc=self._add_doc,
            on_add_bookmark=self._add_bookmark, on_jump_window=self._jump_to_window,
            on_remove_bookmark=self._remove_bookmark, on_open_doc=self._open_doc,
            on_edit=self._navigator_edit)
        self.navigator_dock = QDockWidget("Workspace", self)
        self.navigator_dock.setObjectName("NavigatorDock")
        self.navigator_dock.setWidget(self.navigator)
        self.navigator_dock.setMinimumWidth(240)
        self.addDockWidget(Qt.LeftDockWidgetArea, self.navigator_dock)

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

        projmenu = self.menuBar().addMenu("&Project")
        projmenu.addAction("Checkpoint…", self._checkpoint)
        projmenu.addAction("History…", self._open_history)
        projmenu.addAction("Git identity…", self._set_git_identity)

        view = self.menuBar().addMenu("&View")
        view.addAction(self.navigator_dock.toggleViewAction())
        view.addAction("Devices…", self._open_devices)
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
        add.addSeparator()
        procmenu = add.addMenu("Processor")
        from ..analysis import PROCESSOR_TYPES
        for pkind, pcls in sorted(PROCESSOR_TYPES.items(),
                                  key=lambda kc: getattr(kc[1], "label", kc[0]).lower()):
            procmenu.addAction(getattr(pcls, "label", pkind),
                               lambda _=False, k=pkind: self._add_processor(k))

        netmenu = self.menuBar().addMenu("&Cloud")
        self.hub_action = netmenu.addAction("ferroDAC Cloud…", self._open_hub)

        extmenu = self.menuBar().addMenu("E&xtensions")
        extmenu.addAction("Manage extensions…", self._open_extensions)

        tb = self.addToolBar("Main")
        self.main_toolbar = tb
        tb.setObjectName("MainToolBar")
        tb.setMovable(False)
        tb.addAction("🔌 Devices", self._open_devices)
        tb.addAction(self.edit_action)
        tb.addSeparator()
        self.record_action = tb.addAction("● Record", self._toggle_record)
        tb.addAction("＋ Tag", self._add_tag)
        tb.addAction("🕑 Timeline", self._open_timeline)
        tb.addAction("📄 Docs", self._open_docs)
        tb.addSeparator()
        tb.addAction(self.hub_action)

    def _timeline_sources(self) -> dict:
        """{key: device-qualified name} for the Timeline: the dashboard's LIVE sources
        (derived excluded) unioned with the HISTORIC catalog (local store + hub),
        which now carries the device name so historic channels read 'ch1 · Sim Gauge
        A', not a bare 'ch1'. Live wins on key collision."""
        from ..core.sourceid import compose_label
        names = dict(self.dashboard.source_names())          # live, derived already filtered
        for key, channel, device, _u, _dt in self._historic_sources():
            names.setdefault(key, compose_label(channel, device))
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

    def _open_devices(self):
        """The Devices manager (Available + Active, add/remove/configure) as a window."""
        if getattr(self, "_devices_win", None) is None:
            win = DevicesWindow(self.manager, self._open_config, self)
            win.destroyed.connect(lambda: setattr(self, "_devices_win", None))
            self._devices_win = win
        self._devices_win.show()
        self._devices_win.raise_()
        self._devices_win.activateWindow()

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
                                  on_processor_source=self._processor_source,
                                  on_device_table=self._device_journal_markdown,
                                  on_run_meta=self._run_meta_markdown,
                                  on_saved=lambda: self._schedule_project_commit(
                                      "Edited documents"))
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

    def _add_processor(self, kind: str) -> None:
        """Add menu ▸ Processor ▸ <kind> — add a processor as a blank routable node.
        Route a source into its input (in the Sources panel), then route its outputs."""
        from ..analysis import PROCESSOR_TYPES
        self.dashboard.add_processor(kind)              # blank — bound by routing
        label = getattr(PROCESSOR_TYPES.get(kind), "label", kind)
        self.statusBar().showMessage(
            f"Added {label} — in Sources, route a source into its input, then route "
            "its outputs onward.", 9000)

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
                                     self._processor_source,
                                     self._device_journal_markdown,
                                     self._run_meta_markdown)

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

    def _open_doc(self, path: str) -> None:
        """Open a project doc (from the navigator's Docs section) in the in-app Docs
        view; if QtWebEngine is unavailable, fall back to revealing the file."""
        self._open_docs()                                  # show the dock
        if self._docs_view is None and not self._docs_unavailable:
            self._ensure_docs_view()                       # lazy-create now
        if self._docs_view is not None:
            self._docs_view.open(path)
            self._refresh_doc_collab()
        else:
            self._reveal_path(path)

    def _refresh_doc_collab(self) -> None:
        """Offer the Docs view's Collaborate toggle when it's showing a HUB
        project's doc and the hub is connected; otherwise hide it (ending any live
        session). doc_id = "<project_id>::README.md" — the server maps it under the
        project's docs/ folder."""
        if self._docs_view is None:
            return
        p = self._project_mgr.active
        # collab-eligible if it's a hub project OR a LOCAL working copy of a shared one
        # (a clone — is_hub is False now, but it's still the same hub doc).
        on_hub = p is not None and (getattr(p, "is_hub", False)
                                    or self._project_mgr.is_on_hub(p.id))
        doc_id = f"{p.id}::README.md" if (on_hub and self.hub.connected) else None
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
        self._refresh_explorer()                # enable/disable the “On the hub…” item
        self._refresh_doc_collab()              # offer/retire the Collaborate toggle

    def _ensure_ext_manager(self):
        if self._extensions is None:
            from ..extensions import ExtensionManager
            cfg = QStandardPaths.writableLocation(QStandardPaths.AppConfigLocation)
            root = (os.path.join(cfg, "extensions") if cfg else
                    os.path.join(os.path.expanduser("~"), ".ferrodac", "extensions"))
            self._extensions = ExtensionManager(root)
        return self._extensions

    def _open_extensions(self):
        from .extensions_view import ExtensionsDialog
        ExtensionsDialog(self._ensure_ext_manager(), self).exec()

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
        self._refresh_explorer()

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
        self._refresh_explorer()
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
        self._refresh_explorer()
        self._switch_project(rec["id"])

    def _share_project(self, pid: str) -> None:
        """Promote (MOVE) a LOCAL project to the hub: publish its record, render it
        as a ☁ project (same id) and untrack the local entry — the local folder
        stays on disk as an offline backup, and takes over again if the hub drops."""
        if not self.hub.connected:
            self.statusBar().showMessage("Connect to a hub first (☁ Hub).", 6000)
            return
        p = self._project_mgr.get(pid)
        path = p.path if p is not None else None
        if path:                                          # commit current content so the
            try:                                          # provisioned repo has history to hold
                from ..core.projectgit import ProjectRepo
                ProjectRepo(path).commit("Shared to hub", author=self._git_identity())
            except Exception:                             # noqa: BLE001
                pass
        rec = self._project_mgr.share_to_hub(pid)
        if not rec:
            return
        self._project_mgr.apply_hub_record(rec)           # now a ☁ project (same id)
        self._project_mgr.untrack(pid)                    # drop the local entry
        self.hub.publish_project(rec)
        if path:                                          # push once the hub provisions a repo
            self._pending_share[pid] = path
        self._refresh_explorer()
        self.statusBar().showMessage(
            f"Shared “{rec.get('name')}” — provisioning its repo…", 5000)

    def _push_pending_shares(self) -> None:
        """When the hub provisions a repo for a just-shared project (git_remote arrives
        via the fan-out), push the project's history into it so collaborators clone the
        real thing (not an empty repo). No-op in the native dial (no auto-provision)."""
        if not self._pending_share:
            return
        from ..core.projectgit import ProjectRepo
        for pid, path in list(self._pending_share.items()):
            url = self._project_mgr.hub_git_remote(pid)
            if not url:
                continue                                  # not provisioned (yet / native dial)
            self._pending_share.pop(pid, None)
            QApplication.setOverrideCursor(Qt.WaitCursor)
            QApplication.processEvents()
            try:
                repo = ProjectRepo(path)
                repo.set_remote(url)
                ok, msg = repo.push()
            finally:
                QApplication.restoreOverrideCursor()
            self.statusBar().showMessage(
                "Pushed the shared project to its repo — collaborators can clone it now."
                if ok else f"Couldn't push the shared project to its repo: {msg}", 8000)

    def _republish_active_if_hub(self) -> None:
        """A local edit to a hub project — bump its version and push the record up."""
        p = self._project_mgr.active
        if getattr(p, "is_hub", False):
            self.hub.publish_project(p.bump())

    def _clone_hub_project(self, pid: str) -> None:
        """Check out a shared (hub) project: clone its git repo to a local folder, adopt
        it as your working copy, and switch to it (§8.2 — your clone IS the checkout)."""
        from ..core.projectgit import ProjectRepo
        from ..core.projects import _safe
        p = self._project_mgr.get(pid)
        url = getattr(p, "git_remote", "") if p is not None else ""
        if not url:
            return
        parent = QFileDialog.getExistingDirectory(self, "Clone into which folder?")
        if not parent:
            return
        dest = os.path.join(parent, _safe(p.name) or "project")
        if os.path.exists(dest):
            QMessageBox.warning(self, "Folder exists",
                                f"{dest} already exists — choose another folder.")
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        QApplication.processEvents()
        try:
            ProjectRepo.clone(url, dest)
        except Exception as exc:                        # noqa: BLE001
            QApplication.restoreOverrideCursor()
            QMessageBox.warning(self, "Clone failed", str(exc))
            return
        QApplication.restoreOverrideCursor()
        local = self._project_mgr.track(dest)
        self._refresh_explorer()
        self._switch_project(local.id)
        self.statusBar().showMessage(f"Cloned “{p.name}” → {dest}", 6000)

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
        """Rebuild the unified workspace navigator (projects + the active project's
        sections). One call covers what the old Projects + Explorer panels needed."""
        nav = getattr(self, "navigator", None)
        if nav is not None:
            nav.refresh()

    def _on_hub_projects_changed(self) -> None:
        """Hub projects arrived / changed / vanished (sync or disconnect) — refresh
        the Projects views. Runs on the GUI thread (queued from the sync)."""
        self._push_pending_shares()                       # a provisioned repo just echoed back?
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

    # -- navigator edit actions (rename / delete / duplicate / …) -------------
    def _confirm(self, text: str) -> bool:
        return QMessageBox.question(self, "ferroDAC", text) == QMessageBox.Yes

    @staticmethod
    def _layout_name(path: str) -> str:
        return os.path.splitext(os.path.basename(path))[0]

    def _navigator_edit(self, verb: str, pay: dict) -> None:
        """One dispatcher for the workspace navigator's edit verbs. Confirms
        destructive ops, applies them via the Project model, then refreshes."""
        mgr = self._project_mgr
        if verb == "rename_project":
            proj = mgr.get(pay.get("id"))
            if proj is None:
                return
            name, ok = QInputDialog.getText(self, "Rename project", "New name:",
                                            text=proj.name)
            if ok and proj.rename(name):
                self._update_project_title()           # title + refresh
                self._republish_active_if_hub()
            return
        if verb == "remove_project":
            proj = mgr.get(pay.get("id"))
            if proj is None or not self._confirm(
                    f"Remove “{proj.name}” from the workspace?\n"
                    "The folder on disk is kept — this only untracks it."):
                return
            was_active = mgr.active is not None and mgr.active.id == proj.id
            mgr.untrack(proj.id)
            if was_active:
                rest = mgr.projects()
                if rest:
                    self._switch_project(rest[0].id)
            self._refresh_explorer()
            return

        p = mgr.active
        if p is None:
            return
        if verb == "rename_layout":
            old = self._layout_name(pay["path"])
            name, ok = QInputDialog.getText(self, "Rename layout", "New name:", text=old)
            if ok and p.rename_layout(old, name):
                if (self._active_layout_path and os.path.abspath(self._active_layout_path)
                        == os.path.abspath(pay["path"])):
                    self._active_layout_path = p.layout_path(name.strip())
                self._refresh_explorer()
        elif verb == "duplicate_layout":
            p.duplicate_layout(self._layout_name(pay["path"]))
            self._refresh_explorer()
        elif verb == "delete_layout":
            name = self._layout_name(pay["path"])
            if self._confirm(f"Delete layout “{name}”?"):
                if (self._active_layout_path and os.path.abspath(self._active_layout_path)
                        == os.path.abspath(pay["path"])):
                    self._active_layout_path = None
                p.delete_layout(name)
                self._refresh_explorer()
        elif verb == "rename_doc":
            old = os.path.basename(pay["path"])
            name, ok = QInputDialog.getText(self, "Rename doc", "New name:", text=old)
            if ok and p.rename_doc(old, name):
                self._refresh_explorer()
        elif verb == "delete_doc":
            name = os.path.basename(pay["path"])
            if self._confirm(f"Delete “{name}”?"):
                p.delete_doc(name)
                self._refresh_explorer()
        elif verb == "delete_recording":
            if self._confirm("Delete this recording and its exported files?"):
                p.delete_recording(pay["path"])
                self._refresh_explorer()
        elif verb == "rename_bookmark":
            old = pay.get("name")
            name, ok = QInputDialog.getText(self, "Rename bookmark", "New name:", text=old)
            if ok and p.rename_window(old, name):
                self._refresh_explorer()
                self._republish_active_if_hub()
        elif verb == "uncurate":
            key = pay.get("key")
            kept = [s for s in p.sources()
                    if (s.get("key") if isinstance(s, dict) else s) != key]
            p.set_sources(kept)
            self._apply_source_lens()
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

    def _processor_source(self, kind: str) -> dict:
        """The source of a used processor's class — so its analysis can be pasted,
        readable, into a doc (open science) — plus, for an extension processor that
        ships one, its white paper COPIED into the project (so the citation is
        self-contained). Returns {source, whitepaper-abspath|None}."""
        import inspect
        src = ""
        for proc in self.dashboard._processors.values():
            if proc.kind == kind:
                try:
                    src = inspect.getsource(type(proc))
                except Exception:                  # noqa: BLE001 — e.g. C-defined / no source
                    src = ""
                break
        return {"source": src, "whitepaper": self._copy_processor_whitepaper(kind)}

    def _copy_processor_whitepaper(self, kind: str):
        """If `kind` comes from an extension with a white paper, copy it into the active
        project's papers/ (idempotent) and return the destination path; else None."""
        mgr = self._extensions
        p = self._project_mgr.active
        if mgr is None or p is None:
            return None
        src = mgr.whitepaper_for(kind)
        if not src or not os.path.exists(src):
            return None
        try:
            dest = os.path.join(p.subdir("papers"), os.path.basename(src))
            import shutil
            shutil.copy2(src, dest)
            return dest
        except Exception:                          # noqa: BLE001
            return None

    # -- editor /dev macro: an "instruments used" table for the lab journal --
    def _push_device_records(self) -> None:
        """Push current merged device provenance (descriptor + user metadata) to the
        store writer, keyed by the data-plane id (uuid|instance_id) — the source-key
        prefix — so it's frozen alongside the data. Carries all three ids for later
        reconciliation (uuid / instance_id / serial)."""
        if getattr(self, "store_writer", None) is None:
            return
        from ..core.devicemeta import device_key, merge_device_info
        meta = self._device_meta()
        recs = {}
        for d in self.manager.active_descriptors():
            did = d.uuid or d.instance_id
            if not did:
                continue
            rec = merge_device_info(d, meta.get(device_key(d)))
            rec["device_id"] = did
            rec["uuid"] = d.uuid or ""
            rec["instance_id"] = d.instance_id or ""
            recs[did] = rec
        self.store_writer.set_device_records(recs)

    def _device_meta(self):
        if getattr(self, "_devmeta", None) is None:
            from ..core.devicemeta import DeviceMeta
            from qtpy.QtCore import QStandardPaths
            cfg = (QStandardPaths.writableLocation(QStandardPaths.AppConfigLocation)
                   or os.path.join(os.path.expanduser("~"), ".ferrodac"))
            self._devmeta = DeviceMeta(os.path.join(cfg, "device_meta.json"))
        return self._devmeta

    def _journal_devices(self) -> list:
        """Resolved journal ROWS for the DEVICES behind the project's curated sources
        (first-seen order). LIVE descriptors are merged with current user metadata;
        HISTORIC-only devices (no longer connected) come from the store's frozen
        provenance record (already merged at record time — point-in-time, per §8.2).
        Reconciled on uuid/instance_id/serial so a device shown both ways appears once."""
        from ..core.devicemeta import device_key, merge_device_info
        meta = self._device_meta()
        live_by_id = {}
        for d in self.manager.active_descriptors():
            for k in (d.uuid, d.instance_id, getattr(d, "hardware_id", None)):
                if k:
                    live_by_id[k] = d
        store = self.store_writer.store if getattr(self, "store_writer", None) else None
        hist_by_id = {}
        if store is not None:
            for rec in store.device_records():
                ids = [rec.get("uuid"), rec.get("instance_id"), rec.get("device_id"),
                       rec.get("serial")]
                if any(i and i in live_by_id for i in ids):
                    continue                              # already represented live
                for k in ids:
                    if k:
                        hist_by_id[k] = rec
        seen, rows = set(), []
        for p in self.dashboard.visible_source_ports():   # the curated channel lens
            if getattr(p, "kind", "") not in ("device", "historic", "remote"):
                continue
            did = p.key.split("/")[0]                     # key = "<device-id>/<source>"
            d = live_by_id.get(did)
            if d is not None:
                ident = d.uuid or d.instance_id
                if ident not in seen:
                    seen.add(ident)
                    rows.append(merge_device_info(d, meta.get(device_key(d))))
                continue
            rec = hist_by_id.get(did)
            if rec is not None:
                ident = rec.get("uuid") or rec.get("instance_id") or did
                if ident not in seen:
                    seen.add(ident)
                    rows.append(dict(rec))                # frozen merged record
        return rows

    def _device_journal_markdown(self) -> str:
        """A Markdown 'Instruments' table for the curated devices (live + historic).
        For the /dev macro."""
        from .. import __version__
        rows = self._journal_devices()
        if not rows:
            return "_No instruments — curate some device channels first._"
        lines = ["## Instruments", "",
                 "| Instrument | Manufacturer | Model | Serial | Firmware | Calibration | Asset |",
                 "|---|---|---|---|---|---|---|"]
        for r in rows:
            cal = "—"
            if r.get("cal_date") or r.get("cal_due"):
                cal = r.get("cal_date") or "?"
                if r.get("cal_due"):
                    cal += f" → due {r['cal_due']}"
                if r.get("cal_cert"):
                    cal += f" ({r['cal_cert']})"
            cells = [r.get("name"), r.get("manufacturer"), r.get("model"), r.get("serial"),
                     r.get("firmware"), cal, r.get("asset_tag")]
            lines.append("| " + " | ".join(str(c) if c else "—" for c in cells) + " |")
        lines += ["", f"_Acquired with ferroDAC {__version__}._"]
        return "\n".join(lines)

    def _run_meta_markdown(self) -> str:
        """A report front-matter block — experiment, date(s), experimenter(s),
        sample, instruments, recordings, software. For the /meta macro. Folds
        what it can self-populate; the rest (sample) is a fill-in placeholder."""
        import datetime as _dt
        from .. import __version__
        p = self._project_mgr.active if getattr(self, "_project_mgr", None) else None
        experiment = (p.name if p is not None else "") or "Experiment"

        # experimenter(s): the user's identity, then anyone who has committed history
        people = []
        ident = self._git_identity()
        if ident:
            people.append(ident[0])
        try:
            repo = self._project_repo()
            if repo is not None:
                for row in repo.log(limit=200):
                    a = (row.get("author") or "").strip()
                    if a and a not in people:
                        people.append(a)
        except Exception:                     # noqa: BLE001
            pass
        experimenters = ", ".join(people) if people else "—"

        # date(s): the span the recordings cover, else today
        recs = self._list_recordings()
        if recs:
            d0 = _dt.date.fromtimestamp(min(r["t0"] for r in recs))
            d1 = _dt.date.fromtimestamp(max(r["t1"] for r in recs))
            date = d0.isoformat() if d0 == d1 else f"{d0.isoformat()} – {d1.isoformat()}"
        else:
            date = _dt.date.today().isoformat()

        devices = self._journal_devices()
        instruments = ", ".join(d.get("name") or "?" for d in devices) if devices else "—"

        rows = [
            ("Experiment", experiment),
            ("Date", date),
            ("Experimenter(s)", experimenters),
            ("Sample", "—"),                  # fill in (pending sample tracking)
            ("Instruments", instruments),
            ("Recordings", str(len(recs)) if recs else "—"),
            ("Software", f"ferroDAC {__version__}"),
        ]
        lines = ["| | |", "|---|---|"]
        lines += [f"| **{k}** | {v} |" for k, v in rows]
        return "\n".join(lines)

    # -- editor /rec macro: list recordings + export one on demand -----------
    def _list_recordings(self) -> list:
        """Closed REC spans IN THE ACTIVE PROJECT for the editor's /rec macro:
        id, label, span. Uses the project lens (`visible()` — the same view as the
        Events list and Timeline), so a doc only offers its own experiment's
        recordings, not every recording on the machine."""
        out = []
        for m in self.dashboard.markers.visible():
            if m.kind != RECORDING or m.t_end is None:
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
        self._commit_project(f"Recorded {os.path.basename(dest)}")   # §8.2 boundary commit

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

    # -- project git history (DESIGN §8.2) -----------------------------------
    def _project_repo(self):
        from ..core.projectgit import ProjectRepo
        p = self._project_mgr.active
        return ProjectRepo(p.path) if p is not None else None

    def _git_identity(self):
        """(name, email) for project commits — the user's, if set, else None (the
        repo's default identity is used)."""
        s = QSettings("ferroDAC", "ferroDAC")
        name = (s.value("git/name", "", type=str) or "").strip()
        email = (s.value("git/email", "", type=str) or "").strip()
        return (name, email) if name and email else None

    def _set_git_identity(self) -> None:
        """Project ▸ Git identity… — who project-history commits are attributed to."""
        s = QSettings("ferroDAC", "ferroDAC")
        name, ok = QInputDialog.getText(self, "Git identity",
                                        "Your name (for project history):",
                                        text=s.value("git/name", "", type=str) or "")
        if not ok:
            return
        email, ok2 = QInputDialog.getText(self, "Git identity", "Your email:",
                                          text=s.value("git/email", "", type=str) or "")
        if not ok2:
            return
        s.setValue("git/name", name.strip())
        s.setValue("git/email", email.strip())
        self.statusBar().showMessage("Git identity saved — used for project commits.", 5000)

    def _commit_project(self, message: str) -> None:
        """Commit the active project's folder at a boundary (recording, layout,
        checkpoint). Best-effort — never blocks or raises into the UI."""
        repo = self._project_repo()
        if repo is None:
            return
        sha = repo.commit(message, author=self._git_identity())
        if sha:
            self.statusBar().showMessage(f"✔ {message}  ({sha[:8]})", 4000)
            if getattr(self, "_history_dialog", None) is not None:
                self._history_dialog.refresh()

    def _schedule_project_commit(self, message: str) -> None:
        """Debounced commit for churny sources (doc edits) — coalesces a burst."""
        self._pending_commit_msg = message
        self._commit_timer.start()

    def _do_scheduled_commit(self) -> None:
        self._commit_project(self._pending_commit_msg)

    def _checkpoint(self) -> None:
        """Project ▸ Checkpoint… — a manual, named commit of the project's state."""
        if self._project_mgr.active is None:
            return
        msg, ok = QInputDialog.getText(self, "Checkpoint",
                                       "Describe this checkpoint:", text="Checkpoint")
        if ok:
            self._commit_project(msg.strip() or "Checkpoint")

    def _open_history(self) -> None:
        from .history_view import HistoryDialog
        repo = self._project_repo()
        if repo is None:
            self.statusBar().showMessage("No active project.", 4000)
            return
        def on_remote_changed(url):                 # persist the URL + share if on hub
            p = self._project_mgr.active
            if p is not None:
                p.set_git_remote(url)
                self._republish_active_if_hub()
        self._history_dialog = HistoryDialog(repo, self._project_mgr.active.name, self,
                                             on_remote_changed=on_remote_changed,
                                             author=self._git_identity())
        self._history_dialog.finished.connect(
            lambda _=0: setattr(self, "_history_dialog", None))
        self._history_dialog.show()

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
        self._commit_project(f"Layout: {name}")    # §8.2 boundary commit

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
        """Recorded channels (key, channel_name, device_name, unit, dtype) routable
        for replay even with no live device: the local durable store UNIONED with the
        hub catalog. Device names come from the store's per-device provenance record
        so historic channels are device-qualified (not bare 'ch1'); unknown → "".
        Local wins on key collision; the hub fills what's only remote."""
        from ..core.sourceid import resolve_source
        dtmap = {"scalar": "float", "trace": "trace", "bool": "bool"}
        out = {}                                    # key -> (channel, device, unit, dtype)
        if self.store_writer is not None:
            st = self.store_writer.store
            for key in st.sources():
                info = resolve_source(key, store=st)
                out[key] = (info.channel_name, info.device_name, info.unit, info.dtype)
        if getattr(self, "hub", None) is not None:
            for key, name, unit, dtype in self.hub.hub_sources():
                if key not in out:
                    channel = name if (name and name != key) else key.rsplit("/", 1)[-1]
                    out[key] = (channel, "", unit, dtmap.get(dtype, "float"))
        return [(k, ch, dev, u, dt) for k, (ch, dev, u, dt) in out.items()]

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
    ext_mgr = None
    try:
        from ..extensions import ExtensionManager
        ext_root = os.path.join(cfg, "extensions") if cfg else \
            os.path.join(os.path.expanduser("~"), ".ferrodac", "extensions")
        ext_mgr = ExtensionManager(ext_root)
        ext_mgr.load_enabled()
    except Exception as exc:                        # noqa: BLE001
        log.warning("extension loading failed: %s", exc)

    drivers = load_builtin_drivers()
    log.info("loaded %d driver(s): %s", len(drivers),
             ", ".join(getattr(d, "driver", "?") for d in drivers) or "—")
    engine = Engine()
    manager = DeviceManager(drivers, engine=engine, registry=registry)
    win = MainWindow(manager, engine, extensions=ext_mgr)
    win.show()
    return app.exec()
