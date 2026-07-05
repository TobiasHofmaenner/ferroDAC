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

from .. import _qtbinding  # noqa: F401  selects QT_API before qtpy import

from qtpy.QtCore import QByteArray, QSettings, Qt, QTimer
from qtpy.QtGui import QColor, QIcon, QPalette
from qtpy.QtWidgets import (
    QApplication,
    QDockWidget,
    QFileDialog,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
)

from ..core.engine import Engine
from ..core.history import HistoryBuffer
from ..core.manager import DeviceManager
from ..core.markers import RECORDING
from ..core.projects import ProjectManager
from ..core.reading import Reading
from ..core.registry import load_builtin_drivers
from ._common import color_for, fmt
from .hubclient import ConnectHubDialog, HubController
from .logview import LogPanel, QtLogHandler, SyncStatusWidget
from .panels import PANEL_TYPES
from .workspace import Dashboard, WorkspaceArea
# View widgets/dialogs live in docks.py; re-exported here so the shell
# (and existing imports/tests) can reach them unchanged.
from .docks import (   # noqa: F401,E501
    _origin_badge,
    _dur,
    SourceCard,
    DeviceCard,
    ConfigDialog,
    DevicesPanel,
    DeviceMetaDialog,
    BackupFolderDialog,
    DevicesWindow,
    CollapsibleGroup,
    SourcesPanel,
    SinkCard,
    SinksPanel,
    _SourceCurateDialog,
    _MarkerDialog,
    EventsPanel,
    _ROIEditor,
    ImageConfigDialog,
    CursorDialog,
    ProjectActions,
    ProjectNavigator,
)



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


# display-resolution point budget for the route-in history backfill (#8): more than
# any screen width, so the chart shows full detail without reading full-res raw
_BACKFILL_POINTS = 4000


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
        engine.subscribe(self.history.feed, thread="worker", mode="lossless",
                         name="history")
        # user-triggered waits run as background Tasks (park/scrub, exports, …)
        # so the GUI never freezes (DESIGN §21.3); the GuiBridge marshals worker
        # chunks back to the GUI thread for painting.
        from .tasks import GuiBridge, TaskRunner, set_default_runner
        self._tasks = TaskRunner(self)
        set_default_runner(self._tasks)    # reachable by dialogs deep in the tree
        self._gui_bridge = GuiBridge(self)

        # durable store: persist EVERYTHING continuously (§7.4) so data survives a
        # restart and a span can be recorded retroactively. Degrades to the RAM
        # ring if zarr/disk is unavailable.
        self.store_writer = None
        self.resolver = None
        self.reads = None                  # async resolver facade (§21.3)
        self.time_context = None
        self.replay = None
        try:
            from ..store import (RamTier, ReadService, ReplayController,
                                 Resolver, StoreWriter, TimeContext, ZarrStore)
            os.makedirs(self._app_dir(), exist_ok=True)
            store = ZarrStore(os.path.join(self._app_dir(), "store.zarr"))
            self.store_writer = StoreWriter(store)
            self.store_writer.attach(engine)
            # freeze device provenance ALONGSIDE the data: push the merged snapshot
            # (descriptor + user metadata) whenever the active set changes.
            manager.active_changed.connect(self._push_device_records)
            manager.provenance_changed.connect(self._push_device_records)  # σ re-declare
            self._push_device_records()
            # the read path: one query() over the live RAM ring + the durable store
            self.resolver = Resolver([RamTier(self.history), store])
            # every UI-initiated read (Timeline coverage tick + preview queries)
            # goes through here → a worker pool, off the paint thread, with a
            # coverage TTL cache + supersession (§21.3). Results marshal back via
            # the GuiBridge (queued → GUI thread).
            self.reads = ReadService(self.resolver, deliver=self._gui_bridge.post)
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
                runner=self._tasks,          # park/scrub off the GUI thread (§21.3)
                gui_pump=self._gui_bridge.post_and_wait,
            )
        except Exception as exc:                       # noqa: BLE001
            logging.getLogger("ferrodac").warning("durable store disabled: %s", exc)

        # the dashboard renders through the replay playback bus when available,
        # else straight off the engine (data plane disabled) — identical live.
        data_bus = self.replay.bus if self.replay is not None else engine
        self.dashboard = Dashboard(
            self.workspace, engine, manager, data_bus=data_bus,
            historic_sources=self._historic_sources,
            # a source routed onto a chart backfills from its recorded history (#8)
            on_display=self._backfill_route,
            # heavy processors run off-GUI while live, inline while parked (§21.3)
            is_live=(lambda: self.time_context.following)
            if self.time_context is not None else None)
        self.dashboard.add_panel("chart")
        # Uncertainty bands (DESIGN §19.0): charts get a σ provider — reconstruct over the
        # window, with the per-source model timeline CACHED so a live redraw is pure numpy
        # and never reads the store lock on the hot path. The cache is invalidated when a
        # device re-declares its model (provenance_changed) and on a slow tick (so a model
        # first logged at the opening flush shows up without a manual refresh).
        self._sigma_timelines: dict = {}
        manager.provenance_changed.connect(self._sigma_timelines.clear)
        self._sigma_refresh = QTimer(self)
        self._sigma_refresh.setInterval(2000)
        self._sigma_refresh.timeout.connect(self._sigma_timelines.clear)
        self._sigma_refresh.start()
        self.dashboard.set_sigma_provider(self._chart_sigma)

        # recording lifecycle (start/stop span → auto-export, crash recovery) lives
        # in a Qt-free, testable controller; the shell supplies the collaborators.
        from ..core.recording import RecordingController
        self._recording = RecordingController(
            markers=self.dashboard.markers, resolver=self.resolver,
            store_writer=self.store_writer, run_export=self._run_recording_export,
            runs_dir=self._runs_dir, export_sources=self.dashboard.export_sources,
            on_status=lambda msg, timeout=0: self.statusBar().showMessage(msg, timeout),
            on_saved=self._on_recording_saved, commit=self._commit_project)

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
        self.hub.link_state.connect(self._on_hub_link, Qt.QueuedConnection)

        # working-LAYOUT autosave (per-project working.json) — layout only now
        self._active_layout_path = None    # a named layout open → autosave to it too
        self._autosave_on = False
        self._autosave_timer = QTimer(self)
        self._autosave_timer.setSingleShot(True)
        self._autosave_timer.setInterval(1500)
        self._autosave_timer.timeout.connect(self._do_autosave)
        self.dashboard.ports_changed.connect(self._schedule_autosave)
        # a device the USER adds → its channels join the active project's curated lens
        # (so a curated project doesn't silently hide a device you just plugged in)
        self.manager.device_added.connect(self._curate_new_device)
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
        self.navigator = ProjectNavigator(self._project_mgr, ProjectActions(
            active_layout=lambda: self._active_layout_path,
            hub_enabled=lambda: self.hub.connected,
            activate=self._switch_project, create_local=self._add_project,
            create_hub=self._add_project_hub, reveal=self._reveal_project,
            share=self._share_project, clone=self._clone_hub_project,
            open_layout=self._open_layout, reveal_path=self._reveal_path,
            curate=self._curate_sources, add_layout=self._on_add_layout,
            add_doc=self._add_doc, add_bookmark=self._add_bookmark,
            jump_window=self._jump_to_window, remove_bookmark=self._remove_bookmark,
            open_doc=self._open_doc, edit=self._navigator_edit,
            label_for=self._source_label))
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
        # the what/why/ETA/cancel readout for every background Task (§21.3)
        from .tasks import TaskStatusWidget
        self.statusBar().addPermanentWidget(TaskStatusWidget(self._tasks, self))

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
        filemenu.addSeparator()
        filemenu.addAction("Back up project…", self._backup_project)
        filemenu.addAction("Set hub backup folder…", self._set_hub_backup_folder)
        filemenu.addAction("Download project copy…", self._download_project_copy)

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
                                 lens_fn=self._curated_source_keys,
                                 reads=self.reads)
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
        from .docs import DocServices
        self._docs_view = DocView(DocServices(
            edit=self._open_doc_external, configure=self._configure_editor,
            list_recordings=self._list_recordings,
            export_recording=self._export_recording_for_doc,
            list_recording_exports=self._list_recording_exports,
            list_processors=self._list_processors,
            processor_source=self._processor_source,
            device_table=self._device_journal_markdown,
            run_meta=self._run_meta_markdown,
            saved=lambda: self._schedule_project_commit("Edited documents")))
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
        """Connection MODE changed (intent: objects exist / torn down). Seed the sync
        read-out and refresh hub-dependent views. The button COLOUR is driven
        separately by the REAL link state (`_on_hub_link`), not by this assumption."""
        self.sync_status.set_state("connecting" if connected else "offline")
        if self.reads is not None:
            self.reads.invalidate()             # hub tier joined/left → coverage moved
        # surface (or retire) the hub's historic catalog as routable ports
        self.dashboard.refresh_ports()
        self._refresh_explorer()                # enable/disable the “On the hub…” item
        self._refresh_doc_collab()              # offer/retire the Collaborate toggle

    def _on_hub_link(self, state: str, detail: str) -> None:
        """Recolour the Cloud button from the ACTUAL gRPC link state, so it reflects
        reality (amber connecting → green connected → red on failure), not the
        optimistic 'we started the thread' assumption."""
        color, text = {
            "connecting": ("#d29922", "ferroDAC Cloud …"),
            "connected":  ("#3fb950", "ferroDAC Cloud ✓"),
            "error":      ("#f85149", "ferroDAC Cloud ⚠"),
            "offline":    (None,      "ferroDAC Cloud…"),
        }.get(state, (None, "ferroDAC Cloud…"))
        self.hub_action.setText(text)
        btn = self.main_toolbar.widgetForAction(self.hub_action)
        if btn is not None:
            btn.setStyleSheet(
                f"QToolButton{{color:#0b0f16;background:{color};border-radius:3px;"
                "padding:2px 8px;font-weight:700;}" if color else "")
        if detail:
            self.hub_action.setToolTip(detail)
        if state == "connected":                # we genuinely linked → reconnect next launch
            QSettings("ferroDAC", "ferroDAC").setValue("hub/autoconnect", True)

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
            s.setValue("hub/autoconnect", False)    # explicit disconnect → don't auto-reconnect
            self.hub.disconnect()
            return
        addr, as_agent, as_viewer = dlg.values()
        if addr and (as_agent or as_viewer):
            s.setValue("hub/addr", addr)            # remember for next time
            s.setValue("hub/agent", as_agent)
            s.setValue("hub/viewer", as_viewer)
            self.hub.connect(addr, as_agent, as_viewer)

    def maybe_autoconnect(self) -> None:
        """Reconnect to the last hub on launch if we were connected when we quit
        (set by a successful link; cleared on an explicit disconnect). Called by the
        app entry point AFTER show — never in headless tests."""
        if not (self.hub.available and self._restore_last):
            return
        s = QSettings("ferroDAC", "ferroDAC")
        if not s.value("hub/autoconnect", False, type=bool):
            return
        addr = s.value("hub/addr", "", type=str)
        agent = s.value("hub/agent", True, type=bool)
        viewer = s.value("hub/viewer", True, type=bool)
        if addr and (agent or viewer):
            self.hub.connect(addr, agent, viewer)

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

    def _curate_new_device(self, instance_id: str) -> None:
        """A just-added device's channels join the active project's curated list, so a
        CURATED project doesn't silently hide a device you just plugged in (#6). Only
        fires on an explicit user add (not restore/reconnect), and only for a project
        that already has a lens — a show-all project already shows the new channels.
        Never re-adds a channel that's still in the list (curated-out ones stay out;
        this only appends the device's channels not already present)."""
        p = self._project_mgr.active
        if p is None or not p.source_keys():          # no project / show-all → nothing to do
            return

        def apply(iid=instance_id):
            proj = self._project_mgr.active
            if proj is None:
                return
            existing = proj.source_keys()
            if not existing:
                return
            desc = next((d for d in self.manager.active_descriptors()
                         if d.instance_id == iid), None)
            if desc is None:
                return
            dev_id = getattr(desc, "uuid", "") or iid
            keys = [sp.key for sp in self.dashboard.source_ports()
                    if sp.key.split("/", 1)[0] == dev_id
                    and getattr(sp, "kind", "") in ("device", "remote")]
            to_add = [k for k in keys if k not in existing]
            if not to_add:
                return
            proj.set_sources(list(proj.sources()) + [{"key": k} for k in to_add])
            self._apply_source_lens()
            self._refresh_explorer()
            self._republish_active_if_hub()
        # defer one tick so the dashboard has rebuilt its ports for the new device
        QTimer.singleShot(0, apply)

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
        """Pick a folder. If it IS already a project, adopt it in place. Otherwise
        treat it as a LOCATION and create a dedicated `<location>/<name>/` subfolder
        for the project — so picking a big/system folder (e.g. /home) can never turn
        the whole tree into a project repo."""
        from ..core.projects import _safe, is_project, unsafe_project_dir
        folder = QFileDialog.getExistingDirectory(self, "Project location")
        if not folder:
            return
        if is_project(folder):                            # pointed AT an existing project
            p = self._project_mgr.track(folder)
        else:
            name, ok = QInputDialog.getText(self, "New project", "Project name:",
                                            text="My Project")
            if not ok or not name.strip():
                return
            reason = unsafe_project_dir(folder)
            # a system/home ROOT is fine as a LOCATION (we make a subfolder in it),
            # but not as the project itself — the subfolder keeps it contained.
            dest = os.path.join(folder, _safe(name.strip()) or "project")
            if unsafe_project_dir(dest):                  # e.g. picked "/" → dest still a root
                QMessageBox.warning(self, "Pick another location", reason or
                                    "That location can't hold a project folder.")
                return
            if is_project(dest):                          # the subfolder is already a project
                p = self._project_mgr.track(dest)
            elif os.path.isdir(dest) and os.listdir(dest):
                QMessageBox.warning(
                    self, "Folder in use",
                    f"“{dest}” already exists and isn't empty — pick another name.")
                return
            else:
                try:
                    p = self._project_mgr.track(dest, name.strip())   # create the subfolder
                except ValueError as exc:
                    QMessageBox.warning(self, "Can't create project", str(exc))
                    return
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

        def do_share():                                   # GUI thread: fast, in-memory
            rec = self._project_mgr.share_to_hub(pid)
            if not rec:
                return
            self._project_mgr.apply_hub_record(rec)       # now a ☁ project (same id)
            self._project_mgr.untrack(pid)                # drop the local entry
            self.hub.publish_project(rec)                 # async publish (own channel)
            if path:                                      # push once the hub provisions a repo
                self._pending_share[pid] = path
            self._refresh_explorer()
            self.statusBar().showMessage(
                f"Shared “{rec.get('name')}” — provisioning its repo…", 5000)

        if path:
            # Commit current content OFF the GUI thread: a chain of git subprocesses
            # (init/config/add/status/commit) can block for an unbounded time (a slow
            # or networked filesystem, an index lock, slow process spawn), and the
            # watchdog caught it freezing the paint thread. Share the record whether
            # the commit succeeds, fails, or has nothing to commit — never freeze.
            from ..core.projectgit import ProjectRepo
            author = self._git_identity()
            self.statusBar().showMessage("Preparing project to share…", 4000)
            self._tasks.run(
                lambda ctx: ProjectRepo(path).commit("Shared to hub", author=author),
                title="Sharing project",
                why="Committing the project's history before sharing it to the hub",
                exclusive=f"share:{pid}", on_busy="reject",
                on_done=lambda _r: do_share(),
                on_error=lambda _m: do_share())
        else:
            do_share()

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

            def work(ctx, path=path, url=url, pid=pid):
                repo = ProjectRepo(path)
                repo.set_remote(url)                      # credential-free origin
                cred = self.hub.git_credential(pid)       # ephemeral (url, user, pass)
                creds = (cred[1], cred[2]) if cred else None
                return repo.push(creds)                   # token injected for this push only

            self._tasks.run(
                work, title="Publishing shared project",
                why="Uploading the project's history so collaborators can clone it",
                exclusive=f"push:{path}", on_busy="reject",
                on_done=lambda res: self.statusBar().showMessage(
                    "Pushed the shared project to its repo — collaborators can clone it now."
                    if res[0] else
                    f"Couldn't push the shared project to its repo: {res[1]}", 8000),
                on_error=lambda m: self.statusBar().showMessage(
                    f"Couldn't push the shared project to its repo: {m}", 8000))

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
        name = p.name
        self.statusBar().showMessage(f"Cloning “{name}” in the background…", 4000)

        def done(_r):
            local = self._project_mgr.track(dest)     # adopt the clone (GUI thread)
            self._refresh_explorer()
            self._switch_project(local.id)
            self.statusBar().showMessage(f"Cloned “{name}” → {dest}", 6000)

        def clone_work(ctx, pid=pid, url=url, dest=dest):
            cred = self.hub.git_credential(pid)           # ephemeral (url, user, pass)
            creds = (cred[1], cred[2]) if cred else None
            return ProjectRepo.clone(url, dest, creds)    # token injected for this clone only

        self._tasks.run(
            clone_work,
            title="Cloning project", why=f"Checking out “{name}” from {url}",
            exclusive=f"clone:{dest}", on_busy="reject",
            on_done=done,
            on_error=lambda m: QMessageBox.warning(self, "Clone failed", m))

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
        self.dashboard.markers.default_projects = [pid]   # new tags file here
        self._apply_tag_lens()                            # and the view filters to it
        self._apply_source_lens()                         # follow the channel lens too
        p = mgr.active
        names = p.layouts()
        if names:                                  # open the FIRST named layout, made live
            path = p.layout_path(names[0])
            layout = {}
            try:
                with open(path, encoding="utf-8") as fh:
                    layout = json.load(fh).get("layout", {})
            except Exception:                      # noqa: BLE001
                layout = {}
            self.dashboard.import_layout(layout)   # swap panels/routes/markers only
        else:                                      # no named layout yet → create a default one
            path = p.layout_path("Default")
            self.dashboard.import_layout({})
            if not self.dashboard.panels():
                self.dashboard.add_panel("chart")  # a default chart to start from
            self._write_session(path)              # persist it as the project's first layout
        self._active_layout_path = path            # the open layout is live (autosaves to it)
        self._remember(path)
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
        stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(t1))
        dest = os.path.join(folder, f"ferrodac_export_{stamp}")
        dur = max(0, int(t1 - t0))

        def ok(man):
            n = len(man.get("sources", []))
            self.statusBar().showMessage(
                f"Exported {n} source(s) over {dur} s → {dest}", 8000)

        self.statusBar().showMessage("Exporting in the background…", 4000)
        self._export_task(dest, sources, t0, t1, title="Exporting window", on_ok=ok)

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
        label = re.sub(r"[^\w.-]", "_", m.label or "recording").strip("_") or "recording"
        stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(m.t))
        dest = os.path.join(folder, f"{label}_{stamp}")

        def ok(man):
            n = len(man.get("sources", []))
            if n == 0:
                self.statusBar().showMessage(
                    "This recording's window has no stored data.", 6000)
            else:
                self.statusBar().showMessage(f"Exported {n} source(s) → {dest}", 8000)

        self.statusBar().showMessage("Exporting recording in the background…", 4000)
        self._export_task(dest, sources, m.t, m.t_end, title="Exporting recording",
                          on_ok=ok)

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
            # Per-source σ MODEL (DESIGN §19.0), serialised so the change-log
            # time-resolves it (Keithley range → device_record_at at query time).
            for s in d.sources:
                u = getattr(s, "uncertainty", None)
                if u is not None:
                    rec[f"uncertainty:{s.id}"] = u.to_dict()
            recs[did] = rec
        self.store_writer.set_device_records(recs)

    def _chart_sigma(self, key, times, values):
        """σ(key, times, values) for a chart's uncertainty band: reconstruct over the
        window from the change-log. The per-source model timeline is cached (invalidated
        on provenance_changed + a slow tick), so a live redraw stays pure numpy and never
        blocks on the store lock. A source with no declared model → None (no band)."""
        store = self.store_writer.store if getattr(self, "store_writer", None) else None
        if store is None:
            return None
        from ..store.uncertainty import model_timeline, reconstruct
        if key not in self._sigma_timelines:
            self._sigma_timelines[key] = model_timeline(store, key)
        tl = self._sigma_timelines[key]
        if not tl:
            return None                        # no model logged (yet) → no band, no read
        return reconstruct(store, key, times, values, timeline=tl)

    # -- device journal / notes editor ---------------------------------------
    def _open_device_meta(self, instance_id: str, focus_notes: bool = False):
        """Open the lab-journal / notes editor for a device. Returns the dialog."""
        dlg = DeviceMetaDialog(self.manager, instance_id, self._device_meta(),
                               on_saved=self._push_device_records,
                               focus_notes=focus_notes, parent=self)
        dlg.setAttribute(Qt.WA_DeleteOnClose, True)
        dlg.show()
        return dlg

    def _maybe_prompt_device_meta(self) -> None:
        """Gentle, skippable nudge to describe a NEWLY added device — once per device
        (remembered across sessions), never if it's already described. Gated by
        ``_meta_prompt_on`` so it only fires in the live app (set in main())."""
        if not getattr(self, "_meta_prompt_on", False):
            return
        from ..core.devicemeta import device_key
        self._ensure_prompted_loaded()
        for d in self.manager.active_descriptors():
            key = device_key(d)
            if not key or key in self._prompted_meta:
                continue
            self._prompted_meta.add(key)                  # seen → never nag twice
            self._persist_prompted()
            if not self._device_meta().get(key):          # not yet described → nudge
                self._open_device_meta(d.instance_id, focus_notes=True)

    def _ensure_prompted_loaded(self) -> None:
        if not hasattr(self, "_prompted_meta"):
            from qtpy.QtCore import QSettings
            v = QSettings("ferroDAC", "ferroDAC").value(
                "devicemeta/prompted", [], type=list)
            self._prompted_meta = set(v or [])

    def _persist_prompted(self) -> None:
        from qtpy.QtCore import QSettings
        QSettings("ferroDAC", "ferroDAC").setValue(
            "devicemeta/prompted", list(self._prompted_meta))

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
                          m.t, m.t_end, tags=self.dashboard.markers.to_list(),
                          store=self.store_writer.store if self.store_writer else None)
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
        """Start/stop a recording — the lifecycle lives in RecordingController; here
        we just flip the toolbar button to match."""
        state = self._recording.toggle()
        self.record_action.setText("■ Stop" if state == "started" else "● Record")

    def _run_recording_export(self, dest, sources, t0, t1, *, flush, exclusive,
                              on_ok, on_fail):
        """Adapter: the RecordingController's export runner → the shell's task-backed
        export_window helper (off the GUI thread)."""
        self._export_task(dest, sources, t0, t1, title="Saving recording",
                          on_ok=on_ok, on_fail=on_fail, flush=flush,
                          exclusive=exclusive)

    def _on_recording_saved(self, mid, dest, n):
        self._refresh_explorer()                   # the new recording card shows up
        self.statusBar().showMessage(
            f"■ Saved recording: {n} source(s) → {dest}", 8000)

    def _export_task(self, dest, sources, t0, t1, *, title, on_ok,
                     on_fail=None, flush=False, exclusive="") -> None:
        """Run an export_window off the GUI thread as a cancellable Task (§21.3),
        so Stop-Recording / File▸Export never freeze on a big span. `flush` also
        drains the store writer's pending buffer first (thread-safe). `on_ok(man)`
        / `on_fail(msg)` run on the GUI thread."""
        if self._tasks is None or self.resolver is None:
            return
        from ..store import export_window
        resolver = self.resolver
        tags = self.dashboard.markers.to_list()
        writer = self.store_writer if flush else None
        store = self.store_writer.store if self.store_writer else None

        def work(ctx):
            if writer is not None:
                try:
                    writer.flush_all()               # a clean stop loses nothing
                except Exception:                    # noqa: BLE001
                    pass
            return export_window(dest, sources, resolver, t0, t1, tags=tags, store=store)

        def fail(msg):
            if on_fail is not None:
                on_fail(msg)
            else:
                self.statusBar().showMessage(f"Export failed: {msg}", 8000)

        span = max(0, int(t1 - t0))
        self._tasks.run(
            work, title=title,
            why=f"Materialising {span} s across {len(sources)} source(s) as a "
                "reimportable CSV bundle",
            exclusive=exclusive or f"export:{dest}", on_busy="reject",
            on_done=on_ok, on_error=fail)

    def _recover_open_recordings(self) -> None:
        """Finalise any recording interrupted by a crash (delegated to the
        controller); tell the user how many were recovered."""
        n = self._recording.recover_open()
        if n:
            self.statusBar().showMessage(
                f"Recovered {n} recording(s) interrupted by a crash.", 8000)

    def _on_tick(self):
        self.sources_panel.update_live(self.engine.latest())
        self.sinks_panel.update_live()
        self._update_image_overlays()
        self._update_trace_cursors()

    def _update_trace_cursors(self):
        for panel in self.dashboard._panels.values():
            if getattr(panel, "kind", "") not in ("spectrum", "specwf"):
                continue                            # set_cursors is a contract no-op elsewhere
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
        tmp = path + ".tmp"                        # atomic — this is the every-few-seconds
        with open(tmp, "w", encoding="utf-8") as fh:   # working autosave; a crash mid-write
            json.dump(data, fh, indent=2)              # must not truncate & lose the layout
        os.replace(tmp, path)

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
            # Freeze painting from now (before the entry point's show()) until the saved
            # layout is restored, so the window's FIRST paint is the assembled layout — not
            # the default dock arrangement flashing then re-docking into place (the
            # "windows spawn then assemble" flicker, worst on Windows). singleShot(0) runs
            # the restore on the first event-loop turn, right after show().
            self.setUpdatesEnabled(False)
            QTimer.singleShot(0, self._restore_and_enable_autosave)
        else:
            self._autosave_on = True

    def _restore_and_enable_autosave(self):
        try:
            self.open_session(self._working_path())
        except Exception as exc:                # a bad layout must never freeze the window
            logging.getLogger("ferrodac").warning("session restore failed: %s", exc)
        finally:
            self.setUpdatesEnabled(True)        # paint once, already assembled
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
        repo.sanitize_origin()                      # self-heal a pre-fix tokened origin
        def on_remote_changed(url):                 # persist the URL + share if on hub
            p = self._project_mgr.active
            if p is not None:
                p.set_git_remote(url)
                self._republish_active_if_hub()

        def hub_cred():                             # ephemeral (user, pass) for a hub
            p = self._project_mgr.active            # repo's manual push/pull, else None
            c = self.hub.git_credential(p.id) if p is not None else None
            return (c[1], c[2]) if c else None
        self._history_dialog = HistoryDialog(repo, self._project_mgr.active.name, self,
                                             on_remote_changed=on_remote_changed,
                                             author=self._git_identity(), cred=hub_cred)
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

    def _backup_project(self) -> None:
        """File ▸ Back up project — write a self-contained .zip of the active project
        (readable metadata + an invisible git history bundle) to a user-chosen path.
        Read-only by being a zip; measurements are not included (DESIGN §20.2)."""
        p = self._project_mgr.active if self._project_mgr else None
        if p is None:
            self.statusBar().showMessage("No active project to back up.", 5000)
            return
        s = QSettings("ferroDAC", "ferroDAC")
        last = s.value("backup/dir", "", type=str) or self._app_dir()
        safe = (p.name or "project").replace("/", "_").replace("\\", "_")
        path, _ = QFileDialog.getSaveFileName(
            self, "Back up project", os.path.join(last, f"{safe}.zip"),
            "ferroDAC backup (*.zip)")
        if not path:
            return
        if not path.lower().endswith(".zip"):
            path += ".zip"
        s.setValue("backup/dir", os.path.dirname(path))
        from ..core.archive import archive_project
        self.statusBar().showMessage(f"Backing up “{p.name}” in the background…", 4000)
        self._tasks.run(
            lambda ctx: archive_project(p, path),
            title="Backing up project",
            why=f"Zipping “{p.name}” (metadata + history bundle) to {path}",
            exclusive=f"backup:{path}", on_busy="reject",
            on_done=lambda _r: self.statusBar().showMessage(
                f"Backed up “{p.name}” → {path}", 8000),
            on_error=lambda m: self.statusBar().showMessage(f"Backup failed: {m}", 8000))

    def _set_hub_backup_folder(self) -> None:
        """File ▸ Set hub backup folder — pick where the hub mirrors the active project
        (DESIGN §20 Phase 2). The hub (server) is the authoritative writer; this just
        tells it which backend folder to use."""
        client = self.hub.backup if getattr(self, "hub", None) else None
        p = self._project_mgr.active if self._project_mgr else None
        if client is None:
            self.statusBar().showMessage("Connect to a hub first (Cloud).", 6000)
            return
        if p is None:
            self.statusBar().showMessage("No active project.", 5000)
            return
        dlg = BackupFolderDialog(client, p.id, p.name, self)
        if dlg.exec():
            self.statusBar().showMessage(dlg.result_detail or "Backup folder set.", 7000)

    def _download_project_copy(self) -> None:
        """File ▸ Download project copy — fetch a self-contained zip from the hub."""
        client = self.hub.backup if getattr(self, "hub", None) else None
        p = self._project_mgr.active if self._project_mgr else None
        if client is None or p is None:
            self.statusBar().showMessage("Connect to a hub and open a project first.", 6000)
            return
        safe = (p.name or "project").replace("/", "_").replace("\\", "_")
        path, _ = QFileDialog.getSaveFileName(
            self, "Download project copy",
            os.path.join(self._app_dir(), f"{safe}.zip"), "ferroDAC backup (*.zip)")
        if not path:
            return
        if not path.lower().endswith(".zip"):
            path += ".zip"
        pid = p.id
        self.statusBar().showMessage(f"Downloading “{p.name}” in the background…", 4000)
        self._tasks.run(
            lambda ctx: client.download(pid, path),
            title="Downloading project copy",
            why=f"Fetching a self-contained zip of “{p.name}” from the hub → {path}",
            exclusive=f"download:{path}", on_busy="reject",
            on_done=lambda _r: self.statusBar().showMessage(
                f"Downloaded “{p.name}” → {path}", 8000),
            on_error=lambda m: self.statusBar().showMessage(f"Download failed: {m}", 8000))

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

    def _backfill_route(self, source_key: str, panel) -> None:
        """A source was just routed onto a chart → backfill it from its recorded history
        over the CURRENT window, so the chart shows the existing data instead of starting
        live from the click moment (#8). Fed DIRECTLY to that panel (not the shared replay
        bus) so panels already showing this source aren't double-fed.

        Scalars come from a BOUNDED, DOWNSAMPLED query (the display only needs ~pixels of
        detail): rollup-backed, so it's ~10 ms even over a whole-session grow window and
        returns ~display-resolution points — NOT the full-res `read_raw`, which read
        millions of samples on the GUI thread (measured multi-second freezes on long
        sessions) only for the chart buffer to decimate them away. Because the query is
        cheap it stays SYNCHRONOUS on the GUI thread, so no live tick interleaves older
        history behind newer points. Traces (low volume) keep their full read. No-op with
        no store / no feed target."""
        if (self.replay is None or self.resolver is None
                or self.time_context is None or not hasattr(panel, "feed")):
            return
        t0, t1 = self.time_context.window
        try:
            if self.replay.playback._is_trace(source_key):
                readings = self.replay.playback.read_window([source_key], t0, t1)
            else:
                from .timeline import _envelope_midline
                x, y = self.resolver.query(source_key, t0, t1, max_points=_BACKFILL_POINTS)
                x, y = _envelope_midline(x, y)       # min/max envelope → one clean line
                dev, _, src = source_key.rpartition("/")
                readings = [Reading(dev, src, float(x[i]), float(y[i]))
                            for i in range(len(x)) if x[i] == x[i]]   # drop NaN gap markers
        except Exception as exc:                    # noqa: BLE001 — never break a route
            logging.getLogger("ferrodac").debug("route backfill failed for %s: %s",
                                                 source_key, exc)
            return
        if readings:
            panel.feed(readings)

    def _source_label(self, key: str) -> str:
        """A human, device-qualified label for a source key ('temp · Sim Thermometer 2
        (durga)') — live first, then the store's historic provenance; the bare key if
        nothing resolves. Used by the Workspace channel list so curated channels read as
        names, not UUIDs."""
        live = self.dashboard.source_names()          # {key: qualified label}, live
        if key in live:
            return live[key]
        from ..core.sourceid import resolve_source
        st = self.store_writer.store if self.store_writer is not None else None
        return resolve_source(key, store=st).label or key

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
        self.dashboard.trim_live(tc.window[0])       # slide: drop data behind the window
        #                                              (playback now appends incrementally)
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
        if getattr(self, "_recording", None) is not None:
            self._recording.close_open_marker()   # store_writer.stop() below flushes it
        if self._autosave_on:
            self._do_autosave()
            self._save_global_tags()        # flush the global tag catalog
        self.hub.disconnect()
        if self.replay is not None:
            self.replay.stop()              # unsubscribe the playback bus (+ cancel
            #                                 any in-flight re-stream via generation)
        if getattr(self, "_tasks", None) is not None:
            from .tasks import set_default_runner
            self._tasks.shutdown()          # cancel background exports/loads
            set_default_runner(None)        # don't leave a shut-down runner as default
        if getattr(self, "reads", None) is not None:
            self.reads.shutdown()           # cancel in-flight timeline reads
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
    from ..diagnostics import install_gui_thread_gc, install_gui_watchdog
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
    _watchdog = install_gui_watchdog()     # log any GUI stall >0.5 s with its stack —
    #                                        keep freezes observable (DESIGN §21.2)
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
    win._meta_prompt_on = True              # gently nudge for notes on newly-added devices
    manager.active_changed.connect(win._maybe_prompt_device_meta)
    win.maybe_autoconnect()                # reconnect to the last hub if we were linked
    return app.exec()
