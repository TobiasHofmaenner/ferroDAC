"""Qt glue between the app and a hub.

`HubController` owns the (Qt-free) `HubAgent`/`HubViewer` and bridges their
worker-thread callbacks to the GUI thread via signals:

  - **agent**: publish the app's active devices + their Engine readings.
  - **viewer**: inject the hub's devices into the Dashboard (§6.1 "bind REMOTE")
    and push their readings into the Engine, so they render like local ones.

`ConnectHubDialog` is the little "where's the hub?" form (host:port + roles).
grpcio is optional — if it's missing the menu offers a clear hint instead.
"""

from __future__ import annotations

import socket

from qtpy.QtCore import QObject, Qt, Signal
from qtpy.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QFormLayout,
                            QLabel, QLineEdit, QVBoxLayout)

from .. import net
from ..net import convert


class HubController(QObject):
    # emitted from worker threads → slots run on the GUI thread (queued)
    _catalog = Signal(str, object)        # event_type, pb.DeviceDescriptor
    _tag = Signal(object)                 # an incoming Marker from the hub
    _project = Signal(object)             # an incoming project record (dict) from the hub
    status = Signal(str)                  # human status line
    sync_status = Signal(str, str)        # store-sync (state, detail) — §12.1
    connection_changed = Signal(bool)     # connected ↔ disconnected

    def __init__(self, dashboard, engine, manager, parent=None, store=None,
                 resolver=None):
        super().__init__(parent)
        self.dashboard = dashboard
        self.engine = engine
        self.manager = manager
        self._store = store              # local durable ZarrStore (for hub sync)
        self._resolver = resolver        # local read resolver (gets a hub READ tier)
        self._read_chan = None           # sync channel for the hub read tier
        self._hub_sources: list = []     # cached [(key,name,unit,dtype)] from the hub
        self._agent = None
        self._viewer = None
        self._tagsync = None
        self._projsync = None            # hub project sync (opt-in, role-independent)
        self._project_mgr = None         # set post-construction (after _setup_projects)
        self._on_projects = None         # () -> refresh the Projects dock
        self._sync = None                # store-and-forward SyncRunner (agent role)
        self._agent_unsub = None
        self._tags_wired = False
        self._local: set = set()
        self.addr = ""
        # These signals are emitted from raw (non-QThread) gRPC worker threads;
        # force QueuedConnection so the slots ALWAYS run on the GUI thread. With
        # AutoConnection, PySide6 can mis-detect a non-QThread emitter and deliver
        # directly on the worker thread → Qt-off-GUI-thread → heap corruption.
        self._catalog.connect(self._on_catalog_gui, Qt.QueuedConnection)
        self._tag.connect(self._on_tag_gui, Qt.QueuedConnection)
        self._project.connect(self._on_project_gui, Qt.QueuedConnection)

    def set_projects(self, project_mgr, on_change) -> None:
        """Wire hub-project sync to the client ProjectManager (called by the app
        after it builds the manager). `on_change` refreshes the Projects UI."""
        self._project_mgr = project_mgr
        self._on_projects = on_change

    def publish_project(self, record: dict) -> None:
        """Push a project record up (opt-in: an 'on hub' create / edit / share)."""
        if self._projsync is not None and record:
            self._projsync.publish(record)

    @property
    def available(self) -> bool:
        return net.GRPC_AVAILABLE

    @property
    def connected(self) -> bool:
        return self._agent is not None or self._viewer is not None

    @property
    def roles(self) -> tuple:
        return (self._agent is not None, self._viewer is not None)

    def hub_sources(self) -> list:
        """Cached [(key, name, unit, dtype)] the hub holds (fetched once on
        connect) — so the historic catalog/Timeline can list hub-only sources
        without a gRPC call per refresh tick."""
        return list(self._hub_sources)

    # -- lifecycle -----------------------------------------------------------
    def connect(self, addr: str, as_agent: bool, as_viewer: bool) -> None:
        from ..net.agent import HubAgent
        from ..net.tags import HubTagSync
        from ..net.viewer import HubViewer
        self.disconnect()
        self.addr = addr
        self._update_local()
        aid = f"ferrodac@{socket.gethostname()}"
        if as_agent:
            self._agent = HubAgent(addr, agent_id=aid,
                                   on_state=self._state_cb("agent"))
            self._agent.start()
            self._agent.set_devices(self.manager.active_descriptors())
            self._agent_unsub = self.engine.subscribe(self._feed_agent)
            self.manager.active_changed.connect(self._on_active_changed)
            # store-and-forward: upload the local durable store to the hub (live
            # tails + backfill of anything recorded while offline). Headless —
            # a background thread, never blocks acquisition (DESIGN §12.1).
            if self._store is not None:
                from ..net.sync import SyncRunner
                self._sync = SyncRunner(
                    self._store, addr,
                    on_status=lambda s, d: self.sync_status.emit(s, d))
                self._sync.start()
        if as_viewer:
            self._viewer = HubViewer(
                addr,
                on_catalog=lambda et, dev: self._catalog.emit(et, dev),
                on_readings=self._on_readings_net,
                on_state=self._state_cb("viewer"))
            self._viewer.start()
        # hub READ tier: serve history the client lacks locally (DESIGN §12.1
        # read side) — wired whenever connected, independent of agent/viewer, so
        # a wiped local store can be back-read from the hub.
        if self._resolver is not None and net.GRPC_AVAILABLE:
            import grpc
            from ..net.readtier import HubReadTier
            self._read_chan = grpc.insecure_channel(addr)
            tier = HubReadTier(self._read_chan)
            self._resolver.set_remote(tier)
            self._hub_sources = tier.sources()        # one ListSources for the catalog
        # Tags ride their own channel and are role-independent — sync them
        # whenever connected, regardless of agent/viewer (DESIGN §7.3).
        self._tagsync = HubTagSync(addr, agent_id=aid,
                                   on_tag=lambda m: self._tag.emit(m),
                                   on_state=self._state_cb("tags"))
        self._tagsync.start()
        self._wire_tags()
        for m in self.dashboard.markers.snapshot():   # push our current tags up
            self._tagsync.publish(m)
        # Projects ride their own channel too, but are OPT-IN — we just watch +
        # publish on demand; we never auto-push local projects (DESIGN §8.1).
        if self._project_mgr is not None:
            from ..net.projects import HubProjectSync
            self._projsync = HubProjectSync(
                addr, agent_id=aid,
                on_project=lambda rec: self._project.emit(rec),
                on_state=self._state_cb("projects"))
            self._projsync.start()
        self.status.emit(f"ferroDAC Cloud: connecting to {addr} …")
        self.connection_changed.emit(True)

    def disconnect(self) -> None:
        if self._agent_unsub is not None:
            self._agent_unsub()
            self._agent_unsub = None
        try:
            self.manager.active_changed.disconnect(self._on_active_changed)
        except (TypeError, RuntimeError):
            pass
        self._unwire_tags()
        if self._resolver is not None:
            self._resolver.clear_remote()
        if self._read_chan is not None:
            self._read_chan.close()
            self._read_chan = None
        self._hub_sources = []
        if self._sync is not None:
            self._sync.stop()
            self._sync = None
        if self._agent is not None:
            self._agent.stop()
            self._agent = None
        if self._viewer is not None:
            self._viewer.stop()
            self._viewer = None
        if self._tagsync is not None:
            self._tagsync.stop()
            self._tagsync = None
        if self._projsync is not None:
            self._projsync.stop()
            self._projsync = None
        if self._project_mgr is not None:
            self._project_mgr.clear_hub()        # hub projects aren't offline
            if self._on_projects is not None:
                self._on_projects()
        self.dashboard.clear_remote_devices()
        if self.addr:
            self.status.emit("ferroDAC Cloud: disconnected")
            self.sync_status.emit("offline", "")
            self.connection_changed.emit(False)
        self.addr = ""

    # -- tag sync (role-independent) ----------------------------------------
    def _wire_tags(self) -> None:
        if self._tags_wired:
            return
        m = self.dashboard.markers
        m.tag_changed.connect(self._publish_tag)    # local create/edit
        m.tag_removed.connect(self._publish_tag)    # local delete (tombstone)
        self._tags_wired = True

    def _unwire_tags(self) -> None:
        if not self._tags_wired:
            return
        m = self.dashboard.markers
        for sig in (m.tag_changed, m.tag_removed):
            try:
                sig.disconnect(self._publish_tag)
            except (TypeError, RuntimeError):
                pass
        self._tags_wired = False

    def _publish_tag(self, mid: str) -> None:
        """A local tag changed — push it up. raw() so a just-deleted tag's
        tombstone is publishable (get() hides it)."""
        if self._tagsync is None:
            return
        marker = self.dashboard.markers.raw(mid)
        if marker is not None:
            self._tagsync.publish(marker)

    def _on_tag_gui(self, marker) -> None:
        """An incoming tag from the hub (GUI thread). upsert() merges LWW and
        emits only `changed` — it never re-fires tag_changed, so no echo."""
        self.dashboard.markers.upsert(marker)

    def _on_project_gui(self, record) -> None:
        """An incoming project record from the hub (GUI thread). Materialise it into
        the ProjectManager cache (LWW); the manager ignores our own echo by version.
        Then refresh the Projects UI."""
        if self._project_mgr is None:
            return
        self._project_mgr.apply_hub_record(record)
        if self._on_projects is not None:
            self._on_projects()

    # -- agent side (GUI thread) --------------------------------------------
    def _feed_agent(self, batch) -> None:
        if self._agent is not None:
            self._agent.feed(batch)

    def _on_active_changed(self) -> None:
        self._update_local()
        if self._agent is not None:
            self._agent.set_devices(self.manager.active_descriptors())

    def _update_local(self) -> None:
        self._local = self.dashboard.local_uuids()

    # -- viewer side ---------------------------------------------------------
    def _on_readings_net(self, readings) -> None:
        # worker thread; engine.publish is thread-safe (deque append)
        local = self._local
        for r in readings:
            if r.device not in local:           # skip our own devices echoed back
                self.engine.publish(r)

    def _on_catalog_gui(self, etype, dev) -> None:
        if dev.uuid in self.dashboard.local_uuids():
            return                              # never inject our own as 'remote'
        if etype in ("ADDED", "UPDATED"):
            sources = [(s.id, s.name,
                        convert._DTYPE_FROM_PROTO.get(s.dtype, "float"), s.unit)
                       for s in dev.sources]
            self.dashboard.add_remote_device(dev.uuid, dev.name, sources,
                                              online=dev.online)
        elif etype == "REMOVED":
            self.dashboard.set_remote_offline(dev.uuid)

    def _state_cb(self, role):
        return lambda connected, detail: self.status.emit(f"Cloud {role}: {detail}")


class ConnectHubDialog(QDialog):
    """host:port + which role(s). Result via `values()`; `disconnect_requested`
    is True if the user hit Disconnect."""

    def __init__(self, addr="localhost:50051", as_agent=True, as_viewer=True,
                 connected=False, parent=None):
        super().__init__(parent)
        self.setWindowTitle("ferroDAC Cloud")
        self.setMinimumWidth(340)
        self.disconnect_requested = False
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self._addr = QLineEdit(addr)
        self._addr.setPlaceholderText("host:port  (e.g. 10.0.0.5:50051)")
        form.addRow("Cloud address", self._addr)
        self._agent = QCheckBox("Publish my devices (agent)")
        self._agent.setChecked(as_agent)
        self._viewer = QCheckBox("Show the hub's devices (viewer)")
        self._viewer.setChecked(as_viewer)
        lay.addLayout(form)
        lay.addWidget(self._agent)
        lay.addWidget(self._viewer)
        hint = QLabel("The lab machine acts as the agent; anyone else connects "
                      "as a viewer. You can be both.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#7f8a99; font-size:11px;")
        lay.addWidget(hint)

        bb = QDialogButtonBox()
        bb.addButton("Connect", QDialogButtonBox.AcceptRole)
        if connected:
            disc = bb.addButton("Disconnect", QDialogButtonBox.DestructiveRole)
            disc.clicked.connect(self._on_disconnect)
        bb.addButton(QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def _on_disconnect(self):
        self.disconnect_requested = True
        self.accept()

    def values(self) -> tuple:
        return (self._addr.text().strip(),
                self._agent.isChecked(), self._viewer.isChecked())
