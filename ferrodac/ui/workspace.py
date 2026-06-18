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

import threading
import time
from dataclasses import dataclass, field

from .. import _qtbinding  # noqa: F401  selects QT_API before qtpy import

from qtpy.QtCore import QEvent, QObject, Qt, Signal
from qtpy.QtWidgets import QDockWidget, QMainWindow, QToolButton, QWidget

from ..core.device import SinkKind
from ..core.graph import DataflowGraph, Node, PROCESSOR, SINK, SOURCE
from ..core.markers import MarkerModel, SessionClock
from ..core.reading import Reading
from ..core.trace import Trace
from ..analysis import PROCESSOR_TYPES
from ..vision import CVRunner, Detector
from ..vision.detector import CONFIG_FIELDS
from .panels import PANEL_TYPES, Panel, PanelConfigDialog

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
    online: bool = True   # False = referenced-but-absent placeholder


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
    online: bool = True   # False = referenced-but-absent placeholder


# --------------------------------------------------------------------------- #
#  Workspace area (dockable panels)
# --------------------------------------------------------------------------- #
class _GearButton(QToolButton):
    """A small ⚙ overlay pinned to a panel's top-right corner, shown only in
    edit mode. Kept off the dock title bar so Qt's native drag-to-dock (with
    snap indicators) stays fully intact."""

    def __init__(self, panel: Panel, area: "WorkspaceArea"):
        super().__init__(panel)
        self._panel = panel
        self._area = area
        self.setText("⚙")
        self.setToolTip("Configure panel")
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)
        self.setStyleSheet(
            "QToolButton{background:rgba(20,26,34,210);border:1px solid #3a4250;"
            "border-radius:4px;color:#cdd6e0;font-size:13px;padding:0 4px;}"
            "QToolButton:hover{background:rgba(46,57,74,235);border-color:#5b6675;}")
        self.clicked.connect(self._go)
        panel.installEventFilter(self)
        self._reposition()
        self.raise_()

    def _go(self) -> None:
        if self._area.on_configure is not None:
            self._area.on_configure(self._panel)

    def eventFilter(self, obj, ev):  # noqa: N802
        if obj is self._panel and ev.type() in (
            QEvent.Resize, QEvent.Show, QEvent.ChildAdded
        ):
            self._reposition()
        return False

    def _reposition(self) -> None:
        self.adjustSize()
        self.move(max(0, self._panel.width() - self.width() - 6), 6)
        self.raise_()


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
        self._gears: dict = {}
        self._edit = False              # start locked (presentation) mode
        self.on_configure = None        # set by the Dashboard: fn(panel)

    def add_panel(self, panel: Panel, title: str) -> PanelDock:
        dock = PanelDock(title, panel, self)
        dock.setObjectName(f"panel::{panel.panel_id}")   # for saveState/restoreState
        self.addDockWidget(Qt.TopDockWidgetArea, dock)
        self._docks[panel] = dock
        gear = _GearButton(panel, self)
        gear.setVisible(self._edit)
        self._gears[panel] = gear
        self._apply(dock)
        return dock

    def remove_panel(self, panel: Panel) -> None:
        self._gears.pop(panel, None)        # child of the panel — dies with it
        dock = self._docks.pop(panel, None)
        if dock is not None:
            dock.setParent(None)
            dock.deleteLater()

    def set_panel_title(self, panel: Panel, title: str) -> None:
        dock = self._docks.get(panel)
        if dock is not None:
            dock.setWindowTitle(title)

    def set_edit_mode(self, on: bool) -> None:
        self._edit = on
        for gear in self._gears.values():
            gear.setVisible(on)
            if on:
                gear._reposition()
        for dock in self._docks.values():
            self._apply(dock)

    def _apply(self, dock: PanelDock) -> None:
        if self._edit:
            dock.setFeatures(
                QDockWidget.DockWidgetMovable
                | QDockWidget.DockWidgetFloatable
                | QDockWidget.DockWidgetClosable
            )
            dock.setTitleBarWidget(None)            # native bar → native dragging
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
        self.clock = SessionClock()             # one shared time origin
        self.markers = MarkerModel(self)        # tags + record bookmarks
        self._sources: dict[str, SourcePort] = {}
        self._sinks: dict[str, SinkPort] = {}
        self._routes: dict[str, set] = {}        # source_key -> set(sink_key)
        self._counter = 0
        self.default_sink_id = None              # default chart panel id

        # CV detectors (virtual sources reading a ROI of an image sink)
        self._detectors: dict[str, Detector] = {}
        self._det_lock = threading.Lock()
        self._det_counter = 0
        self._cv: CVRunner | None = None

        # processors: data-plane transforms (trend cursors today; gas-composition
        # analyzer next) that consume a source and publish derived sources.
        self._processors: dict = {}
        self._proc_counters: dict = {}
        self._remote_names: dict = {}            # uuid -> name (hub-viewer devices)

        self.area.on_configure = self._configure_panel
        engine.subscribe(self._on_batch)
        manager.active_changed.connect(self._rebuild_device_ports)
        self._rebuild_device_ports()

    # -- panels --------------------------------------------------------------
    def add_panel(self, kind: str, pid: str = None, title: str = None) -> str:
        label, cls = PANEL_TYPES[kind]
        if pid is None:
            self._counter += 1
            pid = f"{kind}-{self._counter}"
            title = title or f"{label} {self._counter}"
        else:
            title = title or pid
            self._bump_counter(pid)
        panel = cls()
        panel.panel_id = pid
        panel.set_display_name(title)        # let panels show their name (slider…)
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

        if hasattr(panel, "attach_session"):
            panel.attach_session(self.clock, self.markers)
        if hasattr(panel, "on_cursor_move"):
            panel.on_cursor_move = lambda cid, mz: self.update_cursor(cid, mz=mz)
        if hasattr(panel, "set_processor_host"):
            panel.set_processor_host(self.add_processor, self.remove_processor,
                                     self.processor, self.processors_for)

        dock = self.area.add_panel(panel, panel.title)
        dock.closed.connect(lambda _p, pid=pid: self.remove_panel(pid))
        self.ports_changed.emit()
        return pid

    def _bump_counter(self, pid: str) -> None:
        """Keep the auto-id counter ahead of any restored panel id."""
        tail = pid.rsplit("-", 1)[-1]
        if tail.isdigit():
            self._counter = max(self._counter, int(tail))

    def _configure_panel(self, panel: Panel) -> None:
        """Open the panel's settings dialog (the ⚙ on its title bar) and apply
        the result, propagating any new display name to the dock + patch-bay."""
        dlg = PanelConfigDialog(panel.title, panel.config_fields(), self.area)
        if not dlg.exec():
            return
        panel.apply_config(dlg.values())
        # the display name doubles as the dock title and the port label
        self.area.set_panel_title(panel, panel.title)
        pid = panel.panel_id
        if getattr(panel, "is_input", False):
            port = self._sources.get(f"ui/{pid}")
        else:
            port = self._sinks.get(pid)
        if port is not None:
            port.name = panel.title
        self.ports_changed.emit()

    def remove_panel(self, pid: str) -> None:
        panel = self._panels.pop(pid, None)
        if panel is None:
            return
        if hasattr(panel, "cleanup"):            # drop any hosted processor
            panel.cleanup()
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
            for det in self.detectors_for(pid):     # drop detectors on this viewer
                self.remove_detector(det.id, _emit=False)
            if self.default_sink_id == pid:
                charts = [k for k, sp in self._sinks.items() if sp.kind == "display"]
                self.default_sink_id = charts[0] if charts else None
        self.area.remove_panel(panel)
        self.ports_changed.emit()

    # -- CV detectors (virtual sources reading an image sink's ROI) ----------
    def add_detector(self, sink_key: str, name: str, roi: tuple,
                     parse_as: str = "float", on_fail: str = "nan",
                     did: str = None, **ocr) -> str:
        if did is None:
            self._det_counter += 1
            did = f"det{self._det_counter}"
        else:
            tail = did[3:] if did.startswith("det") else ""
            if tail.isdigit():
                self._det_counter = max(self._det_counter, int(tail))
        sink = self._sinks.get(sink_key)
        panel = sink.panel if sink is not None else None
        det = Detector(id=did, name=name, sink_key=sink_key, roi=roi,
                       parse_as=parse_as, on_fail=on_fail, panel=panel, **ocr)
        with self._det_lock:
            self._detectors[did] = det
        key = f"cv/{did}"
        origin = f"CV · {sink.name}" if sink is not None else "CV"
        self._sources[key] = SourcePort(key, name, det.dtype, det.unit, origin, "virtual")
        self._routes.setdefault(key, set())
        self._ensure_cv()
        self.ports_changed.emit()
        return did

    def update_detector(self, did: str, **fields) -> None:
        with self._det_lock:
            det = self._detectors.get(did)
            if det is None:
                return
            for k, v in fields.items():
                setattr(det, k, v)
        sp = self._sources.get(f"cv/{did}")
        if sp is not None:
            sp.name = det.name
            sp.dtype = det.dtype
            sp.unit = det.unit
        self.ports_changed.emit()

    def remove_detector(self, did: str, _emit: bool = True) -> None:
        with self._det_lock:
            self._detectors.pop(did, None)
        key = f"cv/{did}"
        self._sources.pop(key, None)
        self._routes.pop(key, None)
        if _emit:
            self.ports_changed.emit()

    def zoom_to(self, t0: float, t1: float) -> None:
        """Set every chart's x-range to a time window (for a recording region)."""
        x0, x1 = self.clock.rel(t0), self.clock.rel(t1)
        if x1 <= x0:
            x1 = x0 + 1.0
        for p in self._panels.values():
            plot = getattr(p, "plot", None)
            if plot is not None:
                plot.setXRange(x0, x1, padding=0.05)

    def capture_sources(self) -> dict:
        """Numeric sources currently routed somewhere — the Record capture set
        (open-decision #4: the active dashboard's channels)."""
        out = {}
        for key, sp in self._sources.items():
            if sp.dtype in ("float", "bool") and self._routes.get(key):
                out[key] = {"name": sp.name, "unit": sp.unit}
        return out

    def capture_traces(self) -> dict:
        """Routed trace (array) sources — recorded full-scan to per-trace CSVs."""
        return {key: {"name": sp.name}
                for key, sp in self._sources.items()
                if sp.dtype == "trace" and self._routes.get(key)}

    def build_graph(self) -> DataflowGraph:
        """A core, Qt-free snapshot of the current dataflow (DESIGN §4.1) — the
        model introspection / replay / distribution consult, decoupled from the
        UI's internal port dicts. (The Dashboard stays the *editor*; this is the
        extracted *model* — step toward the graph owning routing outright.)"""
        g = DataflowGraph()
        for key, sp in self._sources.items():
            g.add_node(Node(key, SOURCE, sp.name, dtype=sp.dtype, unit=sp.unit,
                            origin=sp.origin,
                            meta={"port_kind": sp.kind, "online": sp.online}))
        for pid, sk in self._sinks.items():
            g.add_node(Node(pid, SINK, sk.name, dtype=sk.dtype, unit=sk.unit,
                            origin=sk.origin, accepts=frozenset(sk.accepts),
                            single_bind=sk.single_bind,
                            meta={"port_kind": sk.kind, "online": sk.online}))
        for pid, proc in self._processors.items():
            ik = getattr(proc, "input_key", "")
            g.add_node(Node(pid, PROCESSOR, getattr(proc, "name", pid),
                            ptype=getattr(proc, "kind", ""), input_key=ik))
            g.connect(ik, pid)                       # source → processor (input bind)
        for src, targets in self._routes.items():    # source → sink routes
            for dst in targets:
                g.connect(src, dst)
        return g

    def detectors_for(self, sink_key: str) -> list:
        with self._det_lock:
            return [d for d in self._detectors.values() if d.sink_key == sink_key]

    def detector(self, did: str):
        with self._det_lock:
            return self._detectors.get(did)

    # -- processors (data-plane transforms) ----------------------------------
    def add_processor(self, kind: str, input_key: str, pid: str = None,
                      **params) -> str:
        """Instantiate a registered Processor bound to `input_key` and register
        its output ports as virtual sources."""
        cls = PROCESSOR_TYPES[kind]
        if pid is None:
            n = self._proc_counters.get(kind, 0) + 1
            self._proc_counters[kind] = n
            pid = f"{cls.id_prefix}{n}"
        else:
            tail = pid[len(cls.id_prefix):] if pid.startswith(cls.id_prefix) else ""
            if tail.isdigit():
                self._proc_counters[kind] = max(self._proc_counters.get(kind, 0),
                                                int(tail))
        proc = cls(pid, input_key, **params)
        self._processors[pid] = proc
        src = self._sources.get(input_key)
        for port in proc.outputs():
            origin = f"{cls.label} · {src.name}" if src is not None else cls.label
            self._sources[port.key] = SourcePort(port.key, port.name, port.dtype,
                                                 port.unit or (src.unit if src else ""),
                                                 origin, "virtual")
            self._routes.setdefault(port.key, set())
        self.ports_changed.emit()
        return pid

    def remove_processor(self, pid: str) -> None:
        proc = self._processors.pop(pid, None)
        if proc is None:
            return
        for port in proc.outputs():
            self._sources.pop(port.key, None)
            self._routes.pop(port.key, None)
        self.ports_changed.emit()

    def processor(self, pid: str):
        return self._processors.get(pid)

    def processors_for(self, input_key: str, kind: str = None) -> list:
        return [p for p in self._processors.values()
                if p.input_key == input_key and (kind is None or p.kind == kind)]

    # -- trend cursors: thin wrappers over the processor machinery -----------
    def add_cursor(self, source_key: str, mz: float, name: str = None,
                   mode: str = "peak", width: float = 1.0, cid: str = None) -> str:
        return self.add_processor("cursor", source_key, pid=cid, mz=mz, name=name,
                                  mode=mode, width=width)

    def update_cursor(self, cid: str, **fields) -> None:
        proc = self._processors.get(cid)
        if proc is None:
            return
        proc.update(**fields)
        sp = self._sources.get(f"cur/{cid}")
        if sp is not None:
            sp.name = proc.name
        self.ports_changed.emit()

    def remove_cursor(self, cid: str) -> None:
        self.remove_processor(cid)

    def cursors_for(self, source_key: str) -> list:
        return self.processors_for(source_key, kind="cursor")

    def cursor(self, cid: str):
        return self._processors.get(cid)

    def _snapshot_detectors(self) -> list:
        with self._det_lock:
            return list(self._detectors.values())

    def _ensure_cv(self) -> None:
        if self._cv is None:
            self._cv = CVRunner(self.engine, self._snapshot_detectors)
            self._cv.start()

    def shutdown(self) -> None:
        if self._cv is not None:
            self._cv.stop()
            self._cv.wait(2000)
            self._cv = None

    # -- session (layout) serialization --------------------------------------
    def export_layout(self) -> dict:
        panels = []
        for pid, panel in self._panels.items():
            entry = {"id": pid, "kind": panel.kind, "title": panel.title}
            st = panel.state() if hasattr(panel, "state") else {}
            if st:
                entry["state"] = st
            panels.append(entry)
        detectors = []
        for det in self._snapshot_detectors():
            entry = {"id": det.id, "name": det.name, "sink": det.sink_key,
                     "roi": list(det.roi)}
            entry.update({f: getattr(det, f) for f in CONFIG_FIELDS})
            detectors.append(entry)
        processors = [{"kind": p.kind, "id": p.id, "input": p.input_key,
                       "state": p.state()} for p in self._processors.values()]
        routes = {k: sorted(v) for k, v in self._routes.items() if v}
        return {"panels": panels, "detectors": detectors, "processors": processors,
                "routes": routes, "default_sink": self.default_sink_id,
                "markers": self.markers.to_list()}

    def clear_layout(self) -> None:
        for pid in list(self._panels):
            self.remove_panel(pid)
        for pid in list(self._processors):
            self.remove_processor(pid)
        self._routes.clear()
        self.default_sink_id = None
        self._rebuild_device_ports()

    def import_layout(self, data: dict) -> None:
        self.clear_layout()
        self.markers.from_list(data.get("markers", []))
        for p in data.get("panels", []):
            pid = self.add_panel(p["kind"], pid=p["id"], title=p.get("title"))
            panel = self._panels.get(pid)
            if panel is not None and p.get("state") and hasattr(panel, "set_state"):
                panel.set_state(p["state"])
        if data.get("default_sink"):
            self.default_sink_id = data["default_sink"]
        for d in data.get("detectors", []):
            cfg = {f: d[f] for f in CONFIG_FIELDS if f in d}
            self.add_detector(d["sink"], name=d.get("name", "Reading"),
                              roi=tuple(d["roi"]), did=d["id"], **cfg)
        for p in data.get("processors", []):
            self.add_processor(p["kind"], p["input"], pid=p["id"], **p.get("state", {}))
        for c in data.get("cursors", []):         # legacy sessions (pre-processors)
            self.add_cursor(c["source"], c["mz"], name=c.get("name"),
                            mode=c.get("mode", "peak"), width=c.get("width", 1.0),
                            cid=c["id"])
        for src, sinks in data.get("routes", {}).items():
            for sink in sinks:
                self.set_route(src, sink, True)
        self.ports_changed.emit()

    # -- device ports --------------------------------------------------------
    def _rebuild_device_ports(self):
        new_src, new_snk = {}, {}
        for d in self.manager.active_descriptors():
            did = d.uuid or d.instance_id     # data-plane identity (portable)
            for s in d.sources:
                key = f"{did}/{s.id}"
                new_src[key] = SourcePort(key, s.name, getattr(s, "dtype", "float"),
                                          s.unit, d.name, "device")
            for sk in d.sinks:
                key = f"{did}#{sk.id}"
                dt = _SINK_DTYPE.get(sk.kind, "float")
                p = sk.params[0] if sk.params else None
                new_snk[key] = SinkPort(
                    key, sk.name, dt, p.unit if p else "", d.name, "device",
                    accepts=frozenset({dt}), single_bind=True,
                    device_id=d.instance_id, sink_id=sk.id,
                    smin=(p.minimum if p and p.minimum is not None else 0.0),
                    smax=(p.maximum if p and p.maximum is not None else 1.0),
                )

        # SOURCES: a device source that's gone but still routed survives as an
        # offline placeholder (desired routing != binding status); unreferenced
        # ones are dropped. A live port replaces any placeholder & re-binds.
        for key in [k for k, p in self._sources.items()
                    if p.kind == "device" and k not in new_src]:
            if self._routes.get(key):
                if self._sources[key].online:
                    self._sources[key].online = False
                    self._emit_offline_gap(key)         # NaN gap, not a frozen line
            else:
                del self._sources[key]
                self._routes.pop(key, None)
        returning_src = []
        for key, port in new_src.items():
            if key not in self._sources or not self._sources[key].online:
                returning_src.append(key)
            self._sources[key] = port

        # SINKS: same — a routed-into device sink that vanished stays as an
        # offline placeholder; a live port replaces it.
        referenced = set().union(*self._routes.values()) if self._routes else set()
        for key in [k for k, p in self._sinks.items()
                    if p.kind == "device" and k not in new_snk]:
            if key in referenced:
                self._sinks[key].online = False
            else:
                del self._sinks[key]
                for targets in self._routes.values():
                    targets.discard(key)
        returning_snk = []
        for key, port in new_snk.items():
            if key not in self._sinks or not self._sinks[key].online:
                returning_snk.append(key)
            self._sinks[key] = port

        # auto-rebind: re-apply the side-effects of existing routes that touch a
        # port which just came (back) online.
        for skey in returning_src:
            for sink_key in self._routes.get(skey, ()):
                self._apply_route(skey, sink_key)
        for sink_key in returning_snk:
            for skey, targets in self._routes.items():
                if sink_key in targets:
                    self._apply_route(skey, sink_key)

        # default-route genuinely new device sources to the default chart — but
        # only if datatype-compatible (an image source must not land on a chart).
        for key, port in new_src.items():
            if key not in self._routes:
                self._routes[key] = set()
                default = self._sinks.get(self.default_sink_id)
                if default is not None and port.dtype in default.accepts:
                    self.set_route(key, self.default_sink_id, True)
        self.ports_changed.emit()

    def _emit_offline_gap(self, source_key: str) -> None:
        """Publish one NaN so charts show a visible break when a source drops."""
        try:
            device, source = source_key.split("/", 1)
        except ValueError:
            return
        self.engine.publish(Reading(device, source, time.time(), float("nan"), 1))

    # -- remote devices (a hub viewer injects these; §6.1 "bind REMOTE") -----
    def local_uuids(self) -> set:
        """UUIDs of locally-bound device ports — so a viewer doesn't re-inject
        its own devices as 'remote' when it also publishes to the same hub."""
        out = set()
        for key, p in self._sources.items():
            if p.kind == "device":
                out.add(key.split("/", 1)[0])
        return out

    def add_remote_device(self, uuid: str, name: str, sources, online: bool = True):
        """Add/refresh a hub device's sources as local-looking ports. `sources`
        is a list of (source_id, name, dtype, unit). Returning online re-binds an
        existing placeholder (its routes/curves persist)."""
        self._remote_names[uuid] = name
        for sid, sname, dtype, unit in sources:
            key = f"{uuid}/{sid}"
            existing = self._sources.get(key)
            if existing is not None and existing.kind == "remote":
                existing.online = online
                existing.name = sname
            else:
                self._sources[key] = SourcePort(
                    key, sname, dtype, unit, name, "remote", online=online)
        self.ports_changed.emit()

    def set_remote_offline(self, uuid: str):
        """A remote device left the catalog → greyed placeholder + a NaN gap,
        routes kept so it re-binds when it returns (same as a local unplug)."""
        prefix = f"{uuid}/"
        for key, p in self._sources.items():
            if p.kind == "remote" and key.startswith(prefix) and p.online:
                p.online = False
                self._emit_offline_gap(key)
        self.ports_changed.emit()

    def remove_remote_device(self, uuid: str):
        """Drop a remote device entirely (its ports + routes)."""
        prefix = f"{uuid}/"
        for key in [k for k, p in self._sources.items()
                    if p.kind == "remote" and k.startswith(prefix)]:
            del self._sources[key]
            self._routes.pop(key, None)
        self._remote_names.pop(uuid, None)
        self.ports_changed.emit()

    def clear_remote_devices(self):
        for uuid in list(self._remote_names):
            self.set_remote_offline(uuid)

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
        """Record the *desired* route. Side-effects are applied only while both
        endpoints exist; an absent endpoint keeps the route as intent (it binds
        when the device appears) — never drops it."""
        targets = self._routes.setdefault(source_key, set())
        sink = self._sinks.get(sink_key)
        if on:
            if sink is not None and sink.single_bind:   # one source owns this sink
                for skey, tg in list(self._routes.items()):
                    if skey != source_key and sink_key in tg:
                        tg.discard(sink_key)
                        if sink.kind == "display" and sink.panel is not None:
                            sink.panel.remove_source(skey)
            targets.add(sink_key)
            self._apply_route(source_key, sink_key)
        else:
            targets.discard(sink_key)
            if sink is not None and sink.kind == "display" and sink.panel is not None:
                sink.panel.remove_source(source_key)
        self.ports_changed.emit()

    def _apply_route(self, source_key: str, sink_key: str) -> None:
        """Live side-effects of a route — a no-op unless both ports are present."""
        sink = self._sinks.get(sink_key)
        src = self._sources.get(source_key)
        if sink is None or src is None:
            return
        if sink.kind == "display":
            sink.panel.add_source(source_key, src)
        elif sink.kind == "device":
            if src.kind == "virtual" and hasattr(src.panel, "set_range") \
                    and sink.dtype == "float":
                src.panel.set_range(sink.smin, sink.smax, sink.unit)
            if src.kind == "virtual" and src.dtype in ("float", "bool"):
                self._write_to_device(sink, src.panel.current_value())

    # -- data flow -----------------------------------------------------------
    def _on_batch(self, batch):
        """Engine sink: run trace processors + write routed sources to sinks."""
        for r in batch:
            if isinstance(r.value, Trace) and not r.partial:
                self._run_processors(r)         # complete scans only
            for sink_key in self._routes.get(r.key, ()):
                sp = self._sinks.get(sink_key)
                if sp is not None and sp.kind == "device":
                    self._write_to_device(sp, r.value)

    def _run_processors(self, r):
        """Feed a reading to every processor bound to its source, publishing the
        derived values back into the data plane."""
        for proc in self._processors.values():
            if proc.input_key != r.key:
                continue
            out = proc.process(r.value)
            for port in proc.outputs():
                sp = self._sources.get(port.key)
                if sp is not None and not sp.unit and port.unit:
                    sp.unit = port.unit          # adopt unit once known
                if port.key in out:
                    dev, _, src = port.key.partition("/")
                    self.engine.publish(Reading(dev, src, r.t, out[port.key]))

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
