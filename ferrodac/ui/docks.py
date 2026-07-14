"""Dockable view widgets + dialogs for the app shell.

The cards, docks, and dialogs the MainWindow composes (Sources/Sinks/Events
panels, device + config dialogs, the Project navigator, …). Split out of
app.py so the shell is the application wiring and these are the reusable views
(DESIGN §4.1 — L4 views). No dependency on MainWindow (constructed with plain
callbacks), so they stay independently testable."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Callable

from .. import _qtbinding  # noqa: F401  selects QT_API before qtpy import

from qtpy.QtCore import QRect, Qt, QTimer, Signal
from qtpy.QtGui import QColor, QIcon, QImage, QPainter, QPen, QPixmap
from qtpy.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
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
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..core.manager import DeviceManager
from ..core.device import DeviceDescriptor, RateMode, SinkKind
from ..vision.detector import FAIL_LABELS, PARSE_LABELS, WHITELIST_PRESETS, Detector
from ..vision.ocr import available_engines, get_engine, ocr_backend, qimage_to_rgb
from ._common import STATUS_COLORS, clear_layout, color_for, fmt
from .workspace import Dashboard


# --------------------------------------------------------------------------- #
#  Source card (right dock) — live value + routing dropdown
# --------------------------------------------------------------------------- #
def _origin_badge(port):
    """(text, fg, bg) for a small pill showing where a source's data comes from —
    so a card is never ambiguous about local vs a remote client vs stored/derived."""
    kind = getattr(port, "kind", "device")
    if kind == "remote":
        return ("☁ Cloud", "#58a6ff", "#102132")     # streamed by a remote client
    if kind == "historic":
        return ("🕓 Stored", "#8b96a5", "#1a1f2a")     # from the store, no live device
    if kind == "virtual":
        return (("ƒ Derived", "#d2a8ff", "#221a2e") if getattr(port, "proc_id", "")
                else ("⌁ Input", "#e3b341", "#282112"))
    return ("⬤ Local", "#3fb950", "#13251a")           # a device on this machine


class SourceCard(QFrame):
    """One source port (device output or virtual input), with a Route dropdown
    listing datatype-compatible sinks."""

    def __init__(self, port, color, sinks, routed, on_route, on_config=None,
                 parent=None):
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

        btext, bfg, bbg = _origin_badge(port)         # local / cloud / stored / derived
        badge = QLabel(btext)
        badge.setStyleSheet(
            f"color:{bfg}; background:{bbg}; border-radius:6px; padding:1px 6px;"
            " font-size:10px; font-weight:700;")
        top.addWidget(badge)

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
        # ⚙ jump to the owning device's config/control section — for real devices
        # (local or hub-remote), not virtual/derived/display ports.
        if on_config is not None and port.kind in ("device", "remote"):
            cfg = QToolButton()
            cfg.setText("⚙")
            cfg.setToolTip("Open this device's config / control")
            cfg.clicked.connect(lambda _=False, key=port.key: on_config(key))
            top.addWidget(cfg)
        lay.addLayout(top)

        # the device (origin) on its own clear line — two devices' identically-named
        # channels (both "Pirani") are told apart, and a remote card names its device.
        origin = (port.origin or "").strip()
        if origin and origin.lower() not in port.name.lower():
            dev = QLabel(origin)
            dev.setStyleSheet("color:#aeb8c6; font-size:11px;")
            dev.setToolTip(f"Device: {origin}")
            lay.addWidget(dev)

        self.value_label = QLabel("—")
        self.value_label.setStyleSheet(
            f"color:{color}; font-family:monospace; font-size:15px;"
        )
        lay.addWidget(self.value_label)

        bits = [port.dtype]
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

        # a driver may ship a dedicated config panel (registered by driver name); it
        # AUGMENTS this dialog (embedded below) and can opt to own the options itself.
        from .device_config import DEVICE_CONFIG_WIDGETS, DeviceConfigController
        panel_cls = DEVICE_CONFIG_WIDGETS.get(desc.driver) if desc else None
        owns_options = bool(panel_cls and getattr(panel_cls, "owns_options", False))

        if desc and desc.options and not owns_options:
            for opt in desc.options:
                orow = QHBoxLayout()
                orow.addWidget(QLabel(opt.name))
                kind = getattr(opt, "kind", "choice")
                if kind in ("text", "secret"):       # free-text / masked (server, key…)
                    edit = QLineEdit(str(opt.value or ""))
                    if kind == "secret":
                        edit.setEchoMode(QLineEdit.Password)
                    edit.setPlaceholderText(opt.name)
                    edit.editingFinished.connect(
                        lambda e=edit, key=opt.key:
                        self.manager.set_option(self.instance_id, key, e.text().strip())
                    )
                    orow.addWidget(edit, 1)
                else:                                # a dropdown over choices
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

        self._driver_panel = None
        if panel_cls is not None:
            sep = QFrame()
            sep.setFrameShape(QFrame.HLine)
            sep.setStyleSheet("color:#232a38;")
            root.addWidget(sep)
            self._driver_panel = panel_cls(
                DeviceConfigController(self.manager, self.instance_id))
            root.addWidget(self._driver_panel)
            self._driver_panel.refresh(desc)

        btnrow = QHBoxLayout()
        meta = QPushButton("📝 Notes & journal…")
        meta.setToolTip("Describe this device's setup + lab-journal fields "
                        "(notes, calibration, asset tag)")
        meta.clicked.connect(self._edit_meta)
        btnrow.addWidget(meta)
        btnrow.addStretch(1)
        close = QPushButton("Close")
        close.clicked.connect(self.close)
        btnrow.addWidget(close)
        root.addLayout(btnrow)
        self._update_info(desc)

    def _edit_meta(self) -> None:
        win = self.parent()
        if win is not None and hasattr(win, "_open_device_meta"):
            win._open_device_meta(self.instance_id)

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
        if getattr(self, "_driver_panel", None) is not None:
            self._driver_panel.refresh(desc)
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


class RemoteControlDialog(QDialog):
    """Control a HUB device's sinks over the wire (DESIGN §5.3). The remote analog of
    ConfigDialog's sink section: the same SETPOINT/TOGGLE/ENUM/ACTION widgets, but
    each dispatches through `send_command(sink_id, value)` (→ HubViewer.SendCommand)
    instead of the local manager. The device's readback source (on the chart) shows
    the effect (§7.5). Device OPTIONS over the hub (a Configure RPC) are a later
    milestone — this surfaces control inputs only."""

    def __init__(self, uuid, name, sinks, options, send_command, send_config,
                 parent=None):
        super().__init__(parent)
        self._uuid = uuid
        self._send = send_command
        self._config = send_config
        self.setWindowTitle(f"Config · {name}")
        lay = QVBoxLayout(self)
        head = QLabel(f"{name}   ·   hub device")
        head.setStyleSheet("font-weight:700;")
        lay.addWidget(head)
        note = QLabel("Changes are applied on the owning agent over the hub; the "
                      "readback / re-announced descriptor shows the result.")
        note.setWordWrap(True)
        note.setStyleSheet("color:#8b95a4; font-size:11px;")
        lay.addWidget(note)

        nrow = QHBoxLayout()                          # rename (§5.3 configure)
        nrow.addWidget(QLabel("Name"))
        self._name_edit = QLineEdit(name)
        nrow.addWidget(self._name_edit, 1)
        rn = QPushButton("Rename")
        rn.clicked.connect(
            lambda: self._configure(rename=self._name_edit.text().strip() or name))
        nrow.addWidget(rn)
        lay.addLayout(nrow)

        if options:                                   # config params (Configure RPC)
            lay.addWidget(self._section("Options"))
            oform = QFormLayout()
            for o in options:
                oform.addRow(o.name, self._option_widget(o))
            lay.addLayout(oform)

        if sinks:                                     # control inputs (SendCommand)
            lay.addWidget(self._section("Controls"))
            sform = QFormLayout()
            for s in sinks:
                sform.addRow(s.name, self._sink_widget(s))
            lay.addLayout(sform)

        if not sinks and not options:
            empty = QLabel("This device exposes no controls or options.")
            empty.setStyleSheet("color:#8b95a4;")
            lay.addWidget(empty)

        row = QHBoxLayout()
        row.addStretch(1)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        row.addWidget(close)
        lay.addLayout(row)

    @staticmethod
    def _section(text) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet("font-weight:700; margin-top:6px; color:#c7d0dc;")
        return lbl

    def _configure(self, **kw) -> None:
        if self._config is not None:
            self._config(self._uuid, **kw)

    def _option_widget(self, o) -> QWidget:
        if o.kind == "choice":
            combo = QComboBox()
            vals = []
            for c in (o.choices or ()):
                combo.addItem(str(c[1]), c[0])
                vals.append(c[0])
            if o.value in vals:
                combo.setCurrentIndex(vals.index(o.value))
            combo.currentIndexChanged.connect(
                lambda _i, cb=combo, key=o.key:
                self._configure(option=(key, cb.currentData())))
            return combo
        edit = QLineEdit("" if o.value is None else str(o.value))   # text / secret
        if o.kind == "secret":
            edit.setEchoMode(QLineEdit.Password)
            edit.setPlaceholderText("(unchanged)")
        apply = QPushButton("Apply")

        def _apply(_=False, key=o.key, e=edit):
            self._configure(option=(key, e.text()))

        apply.clicked.connect(_apply)
        edit.returnPressed.connect(_apply)
        host = QWidget()
        cell = QHBoxLayout(host)
        cell.setContentsMargins(0, 0, 0, 0)
        cell.addWidget(edit, 1)
        cell.addWidget(apply)
        return host

    def _write(self, sink_id, value) -> None:
        if self._send is not None:
            self._send(self._uuid, sink_id, value)

    def _sink_widget(self, s) -> QWidget:
        if s.kind == SinkKind.ACTION:
            b = QPushButton(f"Trigger {s.name}")
            b.clicked.connect(lambda _=False, sid=s.id: self._write(sid, None))
            return b
        if s.kind == SinkKind.TOGGLE:
            chk = QCheckBox("on")
            chk.setChecked(bool(s.value))
            chk.toggled.connect(lambda on, sid=s.id: self._write(sid, on))
            return chk
        if s.kind == SinkKind.ENUM:
            combo = QComboBox()
            opts = list(s.params[0].options) if s.params else []
            combo.addItems(opts)
            if s.value in opts:
                combo.setCurrentText(s.value)
            combo.currentTextChanged.connect(
                lambda txt, sid=s.id: self._write(sid, txt))
            return combo
        unit = s.params[0].unit if s.params else ""      # SETPOINT
        edit = QLineEdit("" if s.value is None else f"{s.value:g}")
        edit.setFixedWidth(110)
        apply = QPushButton("Apply")

        def _apply(_=False, sid=s.id, e=edit):
            try:
                val = float(e.text())
            except ValueError:
                return
            self._write(sid, val)

        apply.clicked.connect(_apply)
        edit.returnPressed.connect(_apply)
        host = QWidget()
        cell = QHBoxLayout(host)
        cell.setContentsMargins(0, 0, 0, 0)
        cell.addWidget(edit)
        cell.addWidget(QLabel(unit))
        cell.addWidget(apply)
        cell.addStretch(1)
        return host


# --------------------------------------------------------------------------- #
#  Devices panel (left dock)
# --------------------------------------------------------------------------- #
class DevicesPanel(QWidget):
    def __init__(self, manager: DeviceManager, on_configure, hub=None, parent=None):
        super().__init__(parent)
        self.manager = manager
        self.on_configure = on_configure
        self._hub = hub
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)
        # opt-out: advertise our AVAILABLE devices to other clients (default on)
        self._share = QCheckBox("Share my available devices with other clients")
        self._share.setChecked(hub.share_devices if hub is not None else True)
        self._share.setVisible(hub is not None)
        if hub is not None:
            self._share.toggled.connect(hub.set_share_devices)
        root.addWidget(self._share)
        self._avail_label, avail_scroll, self._avail_layout = self._section("Available")
        self._active_label, active_scroll, self._active_layout = self._section("Active")
        # other clients' addable devices (grouped by client)
        self._remote_label, self._remote_scroll, self._remote_layout = \
            self._section("Available on other clients")
        root.addWidget(self._avail_label)
        root.addWidget(avail_scroll, 1)
        root.addWidget(self._active_label)
        root.addWidget(active_scroll, 2)
        root.addWidget(self._remote_label)
        root.addWidget(self._remote_scroll, 1)
        manager.available_changed.connect(self._rebuild_available)
        manager.active_changed.connect(self._rebuild_active)
        if hub is not None:
            hub.remote_available_changed.connect(self._rebuild_remote)
        self._rebuild_available()
        self._rebuild_active()
        self._rebuild_remote()

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
        # user-initiated add (user=True → auto-curates its channels); remove as-is
        on_action = (self.manager.remove if active
                     else lambda iid: self.manager.add(iid, user=True))
        for desc in sorted(descs, key=lambda d: d.name):
            layout.addWidget(
                DeviceCard(desc, active, on_action,
                           self.on_configure if active else None)
            )
        layout.addStretch(1)

    def _rebuild_remote(self):
        """Other clients' AVAILABLE devices, grouped by client — Add asks that client
        to onboard the device, which then appears as an active remote device."""
        clear_layout(self._remote_layout)
        by_agent = self._hub.remote_available() if self._hub is not None else {}
        total = 0
        for agent in sorted(by_agent):
            descs = by_agent[agent]
            if not descs:
                continue
            hdr = QLabel(f"on {agent}")
            hdr.setStyleSheet("color:#8b95a4; font-size:11px; font-weight:700;"
                              " margin-top:4px;")
            self._remote_layout.addWidget(hdr)
            for desc in sorted(descs, key=lambda d: d.name):
                on_action = (lambda iid, aid=agent:
                             self._hub.request_add_remote(aid, iid))
                self._remote_layout.addWidget(DeviceCard(desc, False, on_action))
                total += 1
        self._remote_layout.addStretch(1)
        self._remote_label.setText(f"Available on other clients  ({total})")
        vis = self._hub is not None and total > 0
        self._remote_label.setVisible(vis)
        self._remote_scroll.setVisible(vis)


class DeviceMetaDialog(QDialog):
    """Edit a device's lab-journal metadata: a prominent free-text NOTES field (the
    setup description, e.g. "RGA mounted to UHV-CTS SN:12345") plus the structured
    journal fields. User entries override what the device reports and are frozen
    alongside recorded data via the provenance change-log — so moving a sensor and
    updating its note keeps past data correctly labelled (DESIGN §5/§8.2)."""

    _FIELDS = [("manufacturer", "Manufacturer", "manufacturer"),
               ("model", "Model", "model"),
               ("serial", "Serial", "hardware_id"),
               ("firmware", "Firmware", "firmware"),
               ("cal_date", "Cal date", "cal_date"),
               ("cal_due", "Cal due", "cal_due"),
               ("cal_cert", "Cal cert", "cal_cert"),
               ("asset_tag", "Asset tag", "asset_tag")]

    def __init__(self, manager, instance_id, devmeta, on_saved=None,
                 focus_notes=False, parent=None):
        super().__init__(parent)
        from ..core.devicemeta import device_key
        self._iid = instance_id
        self._devmeta = devmeta
        self._on_saved = on_saved
        desc = manager.descriptor(instance_id)
        self._key = (device_key(desc) if desc else "") or instance_id
        self.setWindowTitle(f"Notes & journal — {desc.name if desc else instance_id}")
        self.setMinimumWidth(460)
        user = devmeta.get(self._key)

        root = QVBoxLayout(self)
        root.setSpacing(8)
        intro = QLabel("Describe this device's current setup. Notes are kept with the "
                       "device and frozen alongside any data you record — so moving a "
                       "sensor and updating the note keeps past data labelled correctly.")
        intro.setWordWrap(True)
        intro.setStyleSheet("color:#8b95a4; font-size:11px;")
        root.addWidget(intro)

        lbl = QLabel("Notes / setup")
        lbl.setStyleSheet("font-weight:600;")
        root.addWidget(lbl)
        self._notes = QPlainTextEdit(user.get("notes", ""))
        self._notes.setPlaceholderText(
            "e.g. RGA mounted to UHV-CTS SN:12345 — chamber side, post-bake")
        self._notes.setFixedHeight(80)
        root.addWidget(self._notes)

        form = QFormLayout()
        self._edits = {}
        for key, label, attr in self._FIELDS:
            e = QLineEdit(user.get(key, ""))
            reported = getattr(desc, attr, None) if desc else None
            e.setPlaceholderText(f"{reported}  (reported by device)" if reported else "—")
            self._edits[key] = e
            form.addRow(label, e)
        root.addLayout(form)

        bb = QDialogButtonBox()
        bb.addButton("Save", QDialogButtonBox.AcceptRole)
        bb.addButton("Skip", QDialogButtonBox.RejectRole)
        bb.accepted.connect(self._save)
        bb.rejected.connect(self.reject)
        root.addWidget(bb)
        if focus_notes:
            self._notes.setFocus()

    def _save(self):
        fields = {k: e.text() for k, e in self._edits.items()}
        fields["notes"] = self._notes.toPlainText().strip()
        self._devmeta.set(self._key, fields)        # only non-empty user fields persist
        if self._on_saved is not None:
            self._on_saved()                        # refresh records → freeze on next flush
        self.accept()


class BackupFolderDialog(QDialog):
    """Pick (or create) a folder on the hub's backup store for a project — tree-browse
    via the Backup service. A folder used by another project can't be taken; re-selecting
    this project's own folder re-attaches. DESIGN §20 Phase 2."""

    def __init__(self, client, project_id, project_name, parent=None):
        super().__init__(parent)
        self._client = client
        self._pid = project_id
        self._selected = ""
        self.result_detail = ""
        self.setWindowTitle(f"Hub backup folder — {project_name}")
        self.setMinimumSize(460, 440)
        root = QVBoxLayout(self)
        intro = QLabel("Choose where the hub mirrors this project. Drill in to browse, or "
                       "make a new folder. A folder used by another project can't be taken.")
        intro.setWordWrap(True)
        intro.setStyleSheet("color:#8b95a4; font-size:11px;")
        root.addWidget(intro)
        self._tree = QTreeWidget()
        self._tree.setHeaderHidden(True)
        self._tree.itemExpanded.connect(self._on_expand)
        self._tree.currentItemChanged.connect(self._on_select)
        root.addWidget(self._tree, 1)
        self._sel = QLabel("Selected: (root)")
        self._sel.setStyleSheet("font-size:11px; color:#c7d0db;")
        root.addWidget(self._sel)
        rowb = QHBoxLayout()
        newb = QPushButton("New folder…")
        newb.clicked.connect(self._new_folder)
        rowb.addWidget(newb)
        rowb.addStretch(1)
        root.addLayout(rowb)
        bb = QDialogButtonBox()
        bb.addButton("Use this folder", QDialogButtonBox.AcceptRole)
        bb.addButton(QDialogButtonBox.Cancel)
        bb.accepted.connect(self._use)
        bb.rejected.connect(self.reject)
        root.addWidget(bb)
        self._fill(self._tree.invisibleRootItem(), "")

    def _busy(self, fn, *a):
        from qtpy.QtWidgets import QApplication
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            return fn(*a)
        finally:
            QApplication.restoreOverrideCursor()

    def _fill(self, parent_item, path):
        try:
            folders = self._busy(self._client.list_folders, path)
        except Exception as exc:                       # noqa: BLE001
            self._sel.setText(f"Hub backup unavailable: {exc}")
            return
        parent_item.takeChildren()
        for f in folders:
            it = QTreeWidgetItem(parent_item)
            label = f["name"]
            if f["project_id"] == self._pid:
                label += "   → (this project)"
            elif f["project_id"]:
                label += f"   → {f['project_name'] or 'another project'}"
            it.setText(0, label)
            it.setData(0, Qt.UserRole, f)
            if f["has_children"]:
                QTreeWidgetItem(it)                    # placeholder → shows the expander

    def _on_expand(self, item):
        pay = item.data(0, Qt.UserRole) or {}
        if pay.get("_loaded"):
            return
        pay["_loaded"] = True
        item.setData(0, Qt.UserRole, pay)
        self._fill(item, pay.get("path", ""))

    def _on_select(self, cur, _prev):
        pay = (cur.data(0, Qt.UserRole) if cur else None) or {}
        self._selected = pay.get("path", "")
        self._sel.setText(f"Selected: {self._selected or '(root)'}")

    def _new_folder(self):
        name, ok = QInputDialog.getText(self, "New folder", "New folder name:")
        name = (name or "").strip().strip("/\\")
        if not ok or not name:
            return
        self._selected = f"{self._selected}/{name}" if self._selected else name
        self._sel.setText(f"Selected: {self._selected}   (new)")

    def _use(self):
        if not self._selected:
            self._sel.setText("Pick or create a folder first.")
            return
        try:
            res = self._busy(self._client.set_folder, self._pid, self._selected)
        except Exception as exc:                       # noqa: BLE001
            QMessageBox.warning(self, "Backup", f"Could not set the folder: {exc}")
            return
        if res["ok"]:
            self.result_detail = (("Re-attached to the backup at "
                                   if res["claimed"] else "Backing up to ")
                                  + (res["folder"] or self._selected))
            self.accept()
        else:
            QMessageBox.warning(self, "Can't use that folder",
                                res["detail"] or "That folder can't be used.")


class DevicesWindow(QMainWindow):
    """The Devices manager as a standalone window (like the Timeline) rather than a
    cramped dock — Available + Active devices, add/remove/configure. Just hosts a
    DevicesPanel; the panel is unchanged."""

    def __init__(self, manager: DeviceManager, on_configure, hub=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("ferroDAC — Devices")
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        self.resize(460, 680)
        self.setStyleSheet(
            "QMainWindow,QWidget{background:#0e1116;color:#c7d0db;}"
            "QScrollArea{border:none;}")
        self.panel = DevicesPanel(manager, on_configure, hub=hub)
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
                 on_curate=None, on_lens=None, on_config=None, parent=None):
        super().__init__(parent)
        self.manager = manager
        self.dashboard = dashboard
        self._on_curate = on_curate
        self._on_lens = on_lens
        self._on_config = on_config              # (source_key) -> open its device's config
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
        # a ports_changed rebuild tears down + recreates every card; freeze painting so
        # the clear→refill doesn't flash a blank/half-built panel (#9, cfg-event flicker)
        self.setUpdatesEnabled(False)
        try:
            self._rebuild_inner()
        finally:
            self.setUpdatesEnabled(True)

    def _rebuild_inner(self):
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
                    on_config=self._on_config,
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
                 on_jump=None, on_open_media=None, media_resolver=None,
                 parent=None):
        super().__init__(parent)
        self.markers = markers
        self.clock = clock
        self._on_zoom = on_zoom
        self._on_jump = on_jump                          # jump the timeline to a tag
        self._on_open_media = on_open_media              # open a media tag's photo
        self._media_resolver = media_resolver            # marker -> abs path | None
        self._thumbs: dict = {}                          # path -> QPixmap (44 px)
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

    def _media_thumb(self, m):
        """A 44 px-high thumbnail for a media tag whose file is on this machine
        (None otherwise — the 🖼 button then reports 'file on another box').
        Cached per path: _rebuild redraws every row on each markers change, and
        re-reading every photo from disk each time would not scale."""
        if m.kind != "media" or self._media_resolver is None:
            return None
        try:
            path = self._media_resolver(m)
        except Exception:                                # noqa: BLE001
            return None
        if not path:
            return None
        pm = self._thumbs.get(path)
        if pm is None:
            src = QPixmap(path)
            if src.isNull():
                return None
            pm = src.scaledToHeight(44, Qt.SmoothTransformation)
            self._thumbs[path] = pm
        return pm

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
        if m.kind == "media" and self._on_open_media is not None:
            show = QToolButton()
            show.setText("🖼")
            show.setToolTip("Open the photo")
            show.clicked.connect(lambda _=False, mid=m.id: self._on_open_media(mid))
            top.addWidget(show)
        thumb = self._media_thumb(m)
        if thumb is not None:
            pic = QToolButton()                          # the thumbnail IS a button
            pic.setIcon(QIcon(thumb))
            pic.setIconSize(thumb.size())
            pic.setToolTip("Open the photo")
            pic.setStyleSheet("QToolButton{border:1px solid #232a38;"
                              "border-radius:4px;padding:1px;}")
            if self._on_open_media is not None:
                pic.clicked.connect(
                    lambda _=False, mid=m.id: self._on_open_media(mid))
            lay.addWidget(pic, 0, Qt.AlignLeft)
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


@dataclass
class ProjectActions:
    """The app verbs the ProjectNavigator invokes, bundled into one object so its
    constructor takes a single `actions` instead of 18 loose callbacks. Every field
    defaults to a safe no-op / falsy query, so a navigator builds (and tests) without
    wiring the whole app."""
    active_layout: Callable = field(default=lambda: None)      # () -> current layout name
    hub_enabled: Callable = field(default=lambda: False)       # () -> is a hub connected
    activate: Callable = field(default=lambda *a: None)        # (pid) switch project
    create_local: Callable = field(default=lambda *a: None)
    create_hub: Callable = field(default=lambda *a: None)
    reveal: Callable = field(default=lambda *a: None)          # reveal the project folder
    reveal_path: Callable = field(default=lambda *a: None)     # (path) reveal a folder
    share: Callable = field(default=lambda *a: None)           # (pid)
    clone: Callable = field(default=lambda *a: None)           # (pid)
    open_layout: Callable = field(default=lambda *a: None)     # (name)
    curate: Callable = field(default=lambda *a: None)
    add_layout: Callable = field(default=lambda *a: None)
    add_doc: Callable = field(default=lambda *a: None)
    add_bookmark: Callable = field(default=lambda *a: None)
    jump_window: Callable = field(default=lambda *a: None)     # (window)
    remove_bookmark: Callable = field(default=lambda *a: None)
    open_doc: Callable = field(default=lambda *a: None)        # (doc)
    edit: Callable = field(default=lambda *a: None)            # (verb, payload) dispatcher
    label_for: Callable = field(default=lambda key: key)       # (key) -> device-qualified
    #                                                            channel label (live+historic)


class ProjectNavigator(QWidget):
    """One OneNote-style tree for the whole left side: PROJECTS (notebooks) at the top
    level; the ACTIVE project expands into SECTIONS (Layouts, Channels, Recordings,
    Docs, Bookmarks) → item rows (pages). Click a project to switch to it; click an
    item to open it; right-click for add / remove / share / clone. A VIEW over the
    unchanged ProjectManager/Project model — it scans disk fresh on every refresh,
    keeping no mirrored index. Replaces the old Projects + Project Explorer panels."""

    SECTIONS = ("Layouts", "Channels", "Recordings", "Docs", "Bookmarks")

    def __init__(self, manager, actions=None, parent=None):
        super().__init__(parent)
        self.manager = manager
        # ONE bundle of app verbs instead of 18 loose constructor callbacks — adding
        # a verb is a field on ProjectActions, not a new param threaded through here
        # and the call site (the audit's 'ProjectNavigator takes 18 constructor
        # callables'). Fields default to safe no-ops so a bare navigator still builds.
        self.actions = actions or ProjectActions()
        self._expanded = None        # set of stable keys; None = first build → expand all
        self._last_active = None      # active project id at last refresh (switch → re-expand)

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
        menu.addAction("Local folder…", lambda: self.actions.create_local())
        self._hub_action = menu.addAction("On the hub…", lambda: self.actions.create_hub())
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
        active = self.manager.active
        aid = active.id if active is not None else None
        switched = aid != self._last_active      # project changed (or first build)
        if self._expanded is not None and not switched:
            self._expanded = self._snapshot()    # same project → keep the user's fold state
        self._tree.blockSignals(True)
        self._tree.clear()
        projs = self.manager.projects()
        self._label.setText(f"Workspace  ({len(projs)})")
        self._hub_action.setEnabled(self.actions.hub_enabled())
        if self._expanded is None or switched:   # first build / switch → active project + sections open
            self._expanded = {("project", aid)} | {("section", s) for s in self.SECTIONS}
        self._last_active = aid
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
        active = self.actions.active_layout()
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
            # human, device-qualified label ("temp · Sim Thermometer") — a curated
            # entry stores only the key, so resolve it live/historic; key as tooltip.
            label = (isinstance(s, dict) and s.get("label")) or self.actions.label_for(key)
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
        # Only the expand ARROW toggles fold/unfold (the tree handles that natively).
        # A click on the row BODY just SELECTS, so the bottom action bar shows the
        # node's actions without collapsing the section/project under the cursor.
        if t == "project" and pay["id"] != self._active_id() and self.actions.activate is not None:
            QTimer.singleShot(0, lambda pid=pay["id"]: self.actions.activate(pid))  # switch (rebuilds)
        # section / active-project / item rows: selection only → _update_action_bar;
        # double-click opens an item row (the primary action), never a select-click.

    def _on_double_clicked(self, item, _col=0):
        pay = item.data(0, Qt.UserRole) or {}
        if pay.get("t") in ("layout", "recording", "doc", "bookmark"):
            QTimer.singleShot(0, lambda d=dict(pay): self._open_item(d))

    def _open_item(self, pay):
        t = pay.get("t")
        if t == "layout" and not pay.get("active") and self.actions.open_layout:
            self.actions.open_layout(pay["path"])
        elif t == "recording" and self.actions.reveal_path:
            self.actions.reveal_path(pay["path"])
        elif t == "doc":
            cb = self.actions.open_doc or self.actions.reveal_path
            if cb:
                cb(pay["path"])
        elif t == "bookmark" and self.actions.jump_window and pay.get("t0") and pay.get("t1"):
            self.actions.jump_window(pay["t0"], pay["t1"])

    def _actions_for(self, pay) -> list:
        """[(label, callback|None)] — the action/edit set for a node, used by BOTH the
        right-click menu and the context-sensitive bottom bar. None = disabled. The new
        edit verbs (rename/delete/duplicate/…) route through the single `on_edit`."""
        t = pay.get("t")
        e = self.actions.edit or (lambda _v, _p: None)
        out = []
        if t == "project":
            p = self.manager.get(pay.get("id"))
            if p is None:
                return out
            out.append(("✎ Rename", lambda d=dict(pay): e("rename_project", d)))
            if not getattr(p, "is_hub", False):
                if self.actions.share is not None:
                    out.append(("☁ Share", (lambda pid=p.id: self.actions.share(pid))
                                if self.actions.hub_enabled() else None))
                if self.actions.reveal is not None:
                    out.append(("📂 Reveal", lambda: self.actions.reveal()))
            elif self.actions.clone is not None and getattr(p, "git_remote", ""):
                out.append(("⬇ Clone…", lambda pid=p.id: self.actions.clone(pid)))
            out.append(("⌫ Remove", lambda d=dict(pay): e("remove_project", d)))
        elif t == "section":
            adders = {"Layouts": (self.actions.add_layout, "＋ Add layout"),
                      "Channels": (self.actions.curate, "✔ Curate…"),
                      "Docs": (self.actions.add_doc, "＋ Add doc…"),
                      "Bookmarks": (self.actions.add_bookmark, "＋ Add bookmark")}
            cb, label = adders.get(pay.get("name"), (None, ""))
            if cb is not None:
                out.append((label, lambda cb=cb: cb()))
        elif t == "layout":
            if not pay.get("active") and self.actions.open_layout:
                out.append(("↗ Open", lambda path=pay["path"]: self.actions.open_layout(path)))
            out.append(("✎ Rename", lambda d=dict(pay): e("rename_layout", d)))
            out.append(("⧉ Duplicate", lambda d=dict(pay): e("duplicate_layout", d)))
            out.append(("✕ Delete", lambda d=dict(pay): e("delete_layout", d)))
        elif t == "channel":
            out.append(("✕ Uncurate", lambda d=dict(pay): e("uncurate", d)))
        elif t == "recording":
            if self.actions.reveal_path:
                out.append(("📂 Reveal", lambda path=pay["path"]: self.actions.reveal_path(path)))
            out.append(("✕ Delete", lambda d=dict(pay): e("delete_recording", d)))
        elif t == "doc":
            cb = self.actions.open_doc or self.actions.reveal_path
            if cb:
                out.append(("↗ Open", lambda path=pay["path"], cb=cb: cb(path)))
            out.append(("✎ Rename", lambda d=dict(pay): e("rename_doc", d)))
            out.append(("✕ Delete", lambda d=dict(pay): e("delete_doc", d)))
        elif t == "bookmark":
            if self.actions.jump_window and pay.get("t0") and pay.get("t1"):
                out.append(("⌖ Jump",
                            lambda t0=pay["t0"], t1=pay["t1"]: self.actions.jump_window(t0, t1)))
            out.append(("✎ Rename", lambda d=dict(pay): e("rename_bookmark", d)))
            if self.actions.remove_bookmark:
                out.append(("✕ Delete", lambda name=pay.get("name"): self.actions.remove_bookmark(name)))
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
