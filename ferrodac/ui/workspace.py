"""The dashboard workspace: a dockable area of panels + a routing controller.

WorkspaceArea is a nested QMainWindow (the central area of the shell) whose dock
widgets are the user's panels — so they move/resize/tile natively. An Edit toggle
locks them and hides their title bars for a clean, interactive view.

Dashboard owns the panels and the channel→panel routing, and wires panels to the
engine.
"""

from __future__ import annotations

from .. import _qtbinding  # noqa: F401  selects QT_API before qtpy import

from qtpy.QtCore import QObject, Qt, Signal
from qtpy.QtWidgets import QDockWidget, QMainWindow, QWidget

from .panels import PANEL_TYPES, Panel


class PanelDock(QDockWidget):
    closed = Signal(object)   # emits the panel when the user closes the dock

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
            dock.setTitleBarWidget(None)            # restore default (draggable) title bar
        else:
            dock.setFeatures(QDockWidget.NoDockWidgetFeatures)
            bar = QWidget()
            bar.setFixedHeight(0)
            dock.setTitleBarWidget(bar)             # hide the title bar


class Dashboard(QObject):
    """Owns panels + channel→panel routing; wires panels to the engine."""

    panels_changed = Signal()

    def __init__(self, area: WorkspaceArea, engine, manager, parent=None):
        super().__init__(parent)
        self.area = area
        self.engine = engine
        self.manager = manager
        self._panels: dict = {}            # panel_id -> Panel
        self._input_panels: set = set()    # input panels (refresh control options)
        self._routes: dict = {}            # channel key -> set(panel_id)
        self._counter = 0
        self.default_id = None
        manager.active_changed.connect(self._refresh_inputs)

    # -- panels --------------------------------------------------------------
    def add_panel(self, kind: str) -> str:
        self._counter += 1
        label, cls = PANEL_TYPES[kind]
        if getattr(cls, "is_input", False):
            panel = cls(self.manager)
            self._input_panels.add(panel)
            panel.set_options(self._sink_options(panel.sink_kind))
        else:
            panel = cls()
            panel._unsub = self.engine.subscribe(panel.feed)
        pid = f"{kind}-{self._counter}"
        panel.panel_id = pid
        panel.title = f"{label} {self._counter}"
        self._panels[pid] = panel
        dock = self.area.add_panel(panel, panel.title)
        dock.closed.connect(lambda _p, pid=pid: self.remove_panel(pid))
        if self.default_id is None and kind == "chart":
            self.default_id = pid
        self.panels_changed.emit()
        return pid

    def remove_panel(self, pid: str) -> None:
        panel = self._panels.pop(pid, None)
        if panel is None:
            return
        self._input_panels.discard(panel)
        if panel._unsub:
            panel._unsub()
        self.area.remove_panel(panel)
        for targets in self._routes.values():
            targets.discard(pid)
        if self.default_id == pid:
            charts = [p for p, pn in self._panels.items() if pn.kind == "chart"]
            self.default_id = charts[0] if charts else None
        self.panels_changed.emit()

    def _sink_options(self, kind):
        out = []
        for d in self.manager.active_descriptors():
            for s in d.sinks:
                if s.kind == kind:
                    out.append((d.instance_id, s.id, s, d.name))
        return out

    def _refresh_inputs(self):
        for panel in self._input_panels:
            panel.set_options(self._sink_options(panel.sink_kind))

    def panels(self) -> list:
        return [(pid, p.title) for pid, p in self._panels.items()]

    # -- routing -------------------------------------------------------------
    def routed(self, key: str) -> set:
        return set(self._routes.get(key, set()))

    def set_route(self, key: str, source, pid: str, on: bool) -> None:
        targets = self._routes.setdefault(key, set())
        panel = self._panels.get(pid)
        if panel is None:
            return
        if on:
            targets.add(pid)
            panel.add_source(key, source)
        else:
            targets.discard(pid)
            panel.remove_source(key)

    def ensure_source(self, key: str, source) -> None:
        """First time a source appears, default-route it to the default chart."""
        if key not in self._routes:
            self._routes[key] = set()
            if self.default_id:
                self.set_route(key, source, self.default_id, True)

    def remove_source(self, key: str) -> None:
        for pid in self._routes.pop(key, set()):
            panel = self._panels.get(pid)
            if panel is not None:
                panel.remove_source(key)

    # -- edit mode -----------------------------------------------------------
    def set_edit_mode(self, on: bool) -> None:
        self.area.set_edit_mode(on)
