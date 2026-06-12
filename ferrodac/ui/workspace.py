"""The dashboard workspace + the routing graph (a patch bay).

WorkspaceArea is a nested QMainWindow whose dock widgets are the user's panels.

Dashboard is the **router**. Everything is a port:
  - **Sources** (produce data): device data-outputs + virtual inputs (sliders…).
  - **Sinks** (consume data): device control-inputs + virtual displays (charts…).
A Source may be routed to any **datatype-compatible** Sink. Device control sinks
are single-bind; display sinks accept many. Device-source → device-sink is raw
passthrough (transforms come with the scripting layer).

Data flow is uniform: every source emits Readings into the engine; display sinks
subscribe and render their routed sources; the router (an engine sink) writes
routed source values to device sinks.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .. import _qtbinding  # noqa: F401  selects QT_API before qtpy import

from qtpy.QtCore import QObject, Qt, Signal
from qtpy.QtWidgets import QDockWidget, QMainWindow, QWidget

from ..core.device import SinkKind
from ..core.reading import Reading
from .panels import PANEL_TYPES, Panel

_SINK_DTYPE = {
    SinkKind.SETPOINT: "float",
    SinkKind.TOGGLE: "bool",
    SinkKind.ENUM: "enum",
    SinkKind.ACTION: "action",
}


# --------------------------------------------------------------------------- #
#  Ports
# --------------------------------------------------------------------------- #
@dataclass
class SourcePort:
    key: str
    name: str
    dtype: str
    unit: str
    origin: str          # device name, or "input"
    kind: str            # "device" | "virtual"
    panel: object = None


@dataclass
class SinkPort:
    key: str
    name: str
    dtype: str           # float / bool / enum / action / numeric
    unit: str
    origin: str          # device name, or "display"
    kind: str            # "device" | "display"
    accepts: frozenset = field(default_factory=frozenset)
    single_bind: bool = False
    device_id: str = ""
    sink_id: str = ""
    panel: object = None
    smin: float = 0.0
    smax: float = 1.0


# --------------------------------------------------------------------------- #
#  Workspace area (dockable panels)
# --------------------------------------------------------------------------- #
class PanelDock(QDockWidget):
    closed = Signal(object)

    def __init__(self, title: str, panel: Panel, parent=None):
        super().__init__(title, parent)
        self.panel = panel
        self.setWidget(panel)

    def closeEvent(self, event):  # noqa: N802
        self.closed.emit(self.panel)
        super().closeEvent(event)


class WorkspaceArea(QMainWindow):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setDockNestingEnabled(True)
        self._docks: dict = {}
        self._edit = True

    def add_panel(self, panel: Panel, title: str) -> PanelDock:
        dock = PanelDock(title, panel, self)
        self.addDockWidget(Qt.TopDockWidgetArea, dock)
        self._docks[panel] = dock
        self._apply(dock)
        return dock

    def remove_panel(self, panel: Panel) -> None:
        dock = self._docks.pop(panel, None)
        if dock is not None:
            dock.setParent(None)
            dock.deleteLater()

    def set_edit_mode(self, on: bool) -> None:
        self._edit = on
        for dock in self._docks.values():
            self._apply(dock)

    def _apply(self, dock: PanelDock) -> None:
        if self._edit:
            dock.setFeatures(
                QDockWidget.DockWidgetMovable
                | QDockWidget.DockWidgetFloatable
                | QDockWidget.DockWidgetClosable
            )
            dock.setTitleBarWidget(None)
        else:
            dock.setFeatures(QDockWidget.NoDockWidgetFeatures)
            bar = QWidget()
            bar.setFixedHeight(0)
            dock.setTitleBarWidget(bar)


# --------------------------------------------------------------------------- #
#  Dashboard / router
# --------------------------------------------------------------------------- #
class Dashboard(QObject):
    ports_changed = Signal()     # ports or routes changed (docks refresh)

    def __init__(self, area: WorkspaceArea, engine, manager, parent=None):
        super().__init__(parent)
        self.area = area
        self.engine = engine
        self.manager = manager
        self._panels: dict = {}                 # panel_id -> Panel
        self._sources: dict[str, SourcePort] = {}
        self._sinks: dict[str, SinkPort] = {}
        self._routes: dict[str, set] = {}        # source_key -> set(sink_key)
        self._counter = 0
        self.default_sink_id = None              # default chart panel id

        engine.subscribe(self._on_batch)
        manager.active_changed.connect(self._rebuild_device_ports)
        self._rebuild_device_ports()

    # -- panels --------------------------------------------------------------
    def add_panel(self, kind: str) -> str:
        self._counter += 1
        label, cls = PANEL_TYPES[kind]
        pid = f"{kind}-{self._counter}"
        panel = cls()
        panel.panel_id = pid
        panel.title = f"{label} {self._counter}"
        self._panels[pid] = panel

        if getattr(cls, "is_input", False):
            key = f"ui/{pid}"
            self._sources[key] = SourcePort(
                key, panel.title, cls.source_dtype, "", "input", "virtual", panel
            )
            panel.emitted.connect(lambda val, key=key: self._on_virtual_emit(key, val))
        else:
            panel._unsub = self.engine.subscribe(panel.feed)
            self._sinks[pid] = SinkPort(
                pid, panel.title, "numeric", "", "display", "display",
                accepts=getattr(cls, "accepts", frozenset({"float", "bool"})),
                single_bind=getattr(cls, "single_bind", False), panel=panel,
            )
            if self.default_sink_id is None and kind == "chart":
                self.default_sink_id = pid

        dock = self.area.add_panel(panel, panel.title)
        dock.closed.connect(lambda _p, pid=pid: self.remove_panel(pid))
        self.ports_changed.emit()
        return pid

    def remove_panel(self, pid: str) -> None:
        panel = self._panels.pop(pid, None)
        if panel is None:
            return
        if getattr(panel, "is_input", False):
            key = f"ui/{pid}"
            self._sources.pop(key, None)
            self._routes.pop(key, None)
        else:
            self._sinks.pop(pid, None)
            if panel._unsub:
                panel._unsub()
            for targets in self._routes.values():
                targets.discard(pid)
            if self.default_sink_id == pid:
                charts = [k for k, sp in self._sinks.items() if sp.kind == "display"]
                self.default_sink_id = charts[0] if charts else None
        self.area.remove_panel(panel)
        self.ports_changed.emit()

    # -- device ports --------------------------------------------------------
    def _rebuild_device_ports(self):
        new_src, new_snk = {}, {}
        for d in self.manager.active_descriptors():
            for s in d.sources:
                key = f"{d.instance_id}/{s.id}"
                new_src[key] = SourcePort(key, s.name, getattr(s, "dtype", "float"),
                                          s.unit, d.name, "device")
            for sk in d.sinks:
                key = f"{d.instance_id}#{sk.id}"
                dt = _SINK_DTYPE.get(sk.kind, "float")
                p = sk.params[0] if sk.params else None
                new_snk[key] = SinkPort(
                    key, sk.name, dt, p.unit if p else "", d.name, "device",
                    accepts=frozenset({dt}), single_bind=True,
                    device_id=d.instance_id, sink_id=sk.id,
                    smin=(p.minimum if p and p.minimum is not None else 0.0),
                    smax=(p.maximum if p and p.maximum is not None else 1.0),
                )

        for key in [k for k, p in self._sources.items() if p.kind == "device" and k not in new_src]:
            del self._sources[key]
            self._routes.pop(key, None)
        for key, port in new_src.items():
            self._sources.setdefault(key, port)

        for key in [k for k, p in self._sinks.items() if p.kind == "device" and k not in new_snk]:
            del self._sinks[key]
            for targets in self._routes.values():
                targets.discard(key)
        for key, port in new_snk.items():
            self._sinks.setdefault(key, port)

        # default-route new device sources to the default chart — but only if
        # datatype-compatible (an image source must not land on a chart).
        for key, port in new_src.items():
            if key not in self._routes:
                self._routes[key] = set()
                default = self._sinks.get(self.default_sink_id)
                if default is not None and port.dtype in default.accepts:
                    self.set_route(key, self.default_sink_id, True)
        self.ports_changed.emit()

    # -- queries for the docks ----------------------------------------------
    def source_ports(self) -> list:
        return sorted(self._sources.values(), key=lambda p: (p.kind != "device", p.name))

    def sink_ports(self) -> list:
        return sorted(self._sinks.values(), key=lambda p: (p.kind != "device", p.name))

    def compatible_sinks(self, source_key: str) -> list:
        src = self._sources.get(source_key)
        if src is None:
            return []
        return [(sp.key, sp.name) for sp in self.sink_ports() if src.dtype in sp.accepts]

    def routed(self, source_key: str) -> set:
        return set(self._routes.get(source_key, set()))

    def sources_into(self, sink_key: str) -> list:
        """Names of the sources routed into a sink — used by the Sinks dock."""
        out = []
        for skey, targets in self._routes.items():
            if sink_key in targets:
                sp = self._sources.get(skey)
                out.append(sp.name if sp else skey)
        return out

    def source_bound_to(self, sink_key: str):
        """Single bound source name (control sinks are single-bind), or None."""
        names = self.sources_into(sink_key)
        return names[0] if names else None

    # -- routing -------------------------------------------------------------
    def set_route(self, source_key: str, sink_key: str, on: bool) -> None:
        targets = self._routes.setdefault(source_key, set())
        sink = self._sinks.get(sink_key)
        src = self._sources.get(source_key)
        if sink is None or src is None:
            return
        if on:
            if sink.single_bind:        # one source owns this sink — displace others
                for skey, tg in list(self._routes.items()):
                    if skey != source_key and sink_key in tg:
                        tg.discard(sink_key)
                        if sink.kind == "display":
                            sink.panel.remove_source(skey)
            targets.add(sink_key)
            if sink.kind == "display":
                sink.panel.add_source(source_key, src)
            elif sink.kind == "device":
                if src.kind == "virtual" and hasattr(src.panel, "set_range") \
                        and sink.dtype == "float":
                    src.panel.set_range(sink.smin, sink.smax, sink.unit)
                # sync the device sink to the source's current value now
                if src.kind == "virtual" and src.dtype in ("float", "bool"):
                    self._write_to_device(sink, src.panel.current_value())
        else:
            targets.discard(sink_key)
            if sink.kind == "display":
                sink.panel.remove_source(source_key)
        self.ports_changed.emit()

    # -- data flow -----------------------------------------------------------
    def _on_batch(self, batch):
        """Engine sink: write routed source values to *device* sinks."""
        for r in batch:
            for sink_key in self._routes.get(r.key, ()):
                sp = self._sinks.get(sink_key)
                if sp is not None and sp.kind == "device":
                    self._write_to_device(sp, r.value)

    def _on_virtual_emit(self, source_key: str, value):
        src = self._sources.get(source_key)
        if src is None:
            return
        if src.dtype == "action":         # button: trigger routed device action sinks
            for sink_key in self._routes.get(source_key, ()):
                sp = self._sinks.get(sink_key)
                if sp is not None and sp.kind == "device":
                    self.manager.write(sp.device_id, sp.sink_id, None, silent=True)
            return
        # slider/toggle: publish as a reading so displays show it; _on_batch then
        # writes it to any routed device sinks.
        self.engine.publish(
            Reading("ui", source_key.split("/", 1)[1], time.time(), float(value))
        )

    def _write_to_device(self, sink: SinkPort, value) -> None:
        self.manager.write(sink.device_id, sink.sink_id, value, silent=True)

    # -- edit mode -----------------------------------------------------------
    def set_edit_mode(self, on: bool) -> None:
        self.area.set_edit_mode(on)
