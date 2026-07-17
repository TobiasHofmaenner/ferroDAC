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

from qtpy.QtCore import QByteArray, QSettings, Qt, QTimer, Signal
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
from ..core.markers import ORIGIN_DEVICE, RECORDING
from ..core.projects import ProjectManager
from ..core.registry import load_builtin_drivers
from ._common import color_for, fmt
from .hubclient import ConnectHubDialog, HubController
from .interactions import PendingInteractions, RequestsPanel, RequestToast
from .logview import LogPanel, QtLogHandler, SyncStatusWidget
from .panels import PANEL_TYPES
from .tasks import run_task
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


# The control API binds this port by default (stable address for a connector); overridable
# via FERRODAC_CONTROL_PORT / the 'control/port' setting, or set to 0 to force ephemeral. A
# busy default falls back to an OS-assigned port at start (still discoverable via connector.json).
DEFAULT_CONTROL_PORT = 8765


# --------------------------------------------------------------------------- #
#  Main window — dockable shell
# --------------------------------------------------------------------------- #
class MainWindow(QMainWindow):
    # a pairing request arrives on the API server thread → hop to the GUI to pop the
    # approval dialog (external control surface).
    _pairing_request = Signal(object)

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
        self._video_store = None         # §9.3 ambient video segment store
        self._video_capture = None       # the rotation service
        self.resolver = None
        self.reads = None                  # async resolver facade (§21.3)
        self._prefetch_cache = None        # local hub-fill cache tier (§12.1)
        self._prefetcher = None            # PlaybackPrefetcher (started on hub connect)
        self._prefetch_redraw_pending = False   # coalesce prefetch-fill chart redraws
        self.time_context = None
        self.replay = None
        # historic-catalog resolve cache — must exist BEFORE the Dashboard is
        # constructed (its _rebuild_device_ports walks _historic_sources on a
        # store that already holds sources). Cleared on provenance edits.
        self._srcinfo_cache: dict = {}
        manager.provenance_changed.connect(self._srcinfo_cache.clear)
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
            # a local hub-fill cache sits between the store and the (future) hub tier:
            # the playback prefetcher pulls hub history into it so GUI-thread reads
            # stay local (§12.1). Always present; empty until a hub connects.
            from ..store import PrefetchCache
            self._prefetch_cache = PrefetchCache()
            self.resolver.set_prefetch(self._prefetch_cache)
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
                # §22 step 4: the re-stream serves ANALYSIS — all sources when
                # processors are bound, else only traces (scalar curves are
                # query-drawn); with neither, park/scrub re-streams nothing.
                sources=lambda: self.dashboard.replay_source_keys(),
                on_reset=self._replay_reset,
                on_progress=self._replay_progress,
                reader=self.resolver,        # replay full-res via RAM+store+hub tier
                runner=self._tasks,          # park/scrub off the GUI thread (§21.3)
                gui_pump=self._gui_bridge.post_and_wait,
                # a play-step's newly-entered slice reaches charts via the owner
                on_advance=lambda a, b: self.chart_feed.advance(a, b),
            )
        except Exception as exc:                       # noqa: BLE001
            logging.getLogger("ferrodac").warning("durable store disabled: %s", exc)

        # the single owner of chart curve-buffer writes (DESIGN §22 I-6, step 1):
        # every store-query draw goes through it. Accessors late-bind because the
        # dashboard is constructed just below and the data plane may be degraded.
        from .chartfeed import ChartFeed
        self.chart_feed = ChartFeed(
            panels=lambda: self.dashboard.panels(),
            resolver=lambda: self.resolver,
            reads=lambda: self.reads,
            time_context=lambda: self.time_context,
            replay=lambda: self.replay,
        )

        # the dashboard renders through the replay playback bus when available,
        # else straight off the engine (data plane disabled) — identical live.
        data_bus = self.replay.bus if self.replay is not None else engine
        self.dashboard = Dashboard(
            self.workspace, engine, manager, data_bus=data_bus,
            historic_sources=self._historic_sources,
            # a source routed onto a chart backfills from its recorded history (#8)
            on_display=self.chart_feed.backfill_route,
            # heavy processors run off-GUI while live, inline while parked (§21.3)
            is_live=(lambda: self.time_context.following)
            if self.time_context is not None else None)
        # §22 steps 3+4: panels are fed through ChartFeed's single forwarding point —
        # raw live tail from the ENGINE bus (LIVE mode only), derived readings and
        # traces from the playback bus. Raw historic scalars never reach a panel.
        self.chart_feed.attach(engine,
                               self.replay.bus if self.replay is not None else None,
                               is_derived=lambda k: self.dashboard.is_derived_key(k))
        self.dashboard.add_panel("chart")
        # Uncertainty bands (DESIGN §19.0): charts get a σ provider — reconstruct over the
        # window, with the per-source model timeline CACHED so a live redraw is pure numpy
        # and never reads the store lock on the hot path. The cache is invalidated when a
        # device re-declares its model (provenance_changed) and on a slow tick (so a model
        # first logged at the opening flush shows up without a manual refresh).
        self._sigma_timelines: dict = {}
        manager.provenance_changed.connect(self._sigma_timelines.clear)
        # Gap breaks (DESIGN §7.4): charts break the drawn curve at a recorded-data gap
        # via a coverage provider. resolver.coverage takes the store lock, and this is
        # consulted on the per-batch draw path, so cache it — invalidated on the same
        # slow tick as σ (coverage grows as data records) and on every park/scrub reset.
        self._coverage_cache: dict = {}
        self._coverage_stale: dict = {}     # last-known coverage per key — served while a
        #                                     miss refreshes async (never cleared: stale
        #                                     gap-breaks beat a network stall on the draw)
        self._sigma_refresh = QTimer(self)
        self._sigma_refresh.setInterval(2000)
        # retry only keys with NO model yet (the "model logged at the opening
        # flush" case). A blind clear made every band redraw re-read the store —
        # model_timeline takes the store lock, and the draw path must never queue
        # behind a writer/sync hold (same class as the sources() stall). Resolved
        # models are dropped on provenance_changed, the signal that changes them.
        self._sigma_refresh.timeout.connect(self._retry_empty_sigma_timelines)
        self._sigma_refresh.timeout.connect(self._coverage_cache.clear)
        self._sigma_refresh.timeout.connect(self._refresh_dirty_sinks)
        self._sigma_refresh.start()
        self.dashboard.set_sigma_provider(self._chart_sigma)
        self.dashboard.set_gap_provider(self._chart_coverage)
        self.dashboard.set_media_provider(self._resolve_media)   # photo tile (§9)
        self.dashboard.set_snapshot_handler(lambda key: self._snap([key]))
        # §9.3 ambient video: a segment store next to store.zarr + a rotation
        # service gated by each camera's mode + record state + a disk floor.
        try:
            from ..core.videostore import VideoStore
            from .videocapture import VideoCaptureService
            self._video_store = VideoStore(os.path.join(self._app_dir(), "video"))
            self._video_capture = VideoCaptureService(
                self._video_store,
                devices=self.manager.active_devices,
                is_recording=lambda: (self._recording.recording
                                      if getattr(self, "_recording", None) else False),
                now=time.time,
                on_status=lambda msg, timeout=0: self.statusBar().showMessage(
                    msg, timeout))
            self._video_capture.start()
            self.manager.active_changed.connect(self._video_capture.reconcile)
            self.dashboard.markers.tag_changed.connect(self._rematerialize_clips_for)
        except Exception:                            # noqa: BLE001 — video is optional
            logging.getLogger("ferrodac").warning(
                "ambient video unavailable", exc_info=True)
        self.dashboard.on_chart_zoom = self.chart_feed.on_chart_zoom   # zoom → re-query (Fix B)

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
            resolver=self.resolver, video_store=self._video_store)
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
        # interaction §7.3 hub relay: a viewer surfaces + answers device prompts raised on
        # the owning agent. All three fire from hub worker threads → QueuedConnection.
        self._remote_prompt_ids: set = set()   # hub-injected prompts — the agent owns their tag
        self.hub.remote_prompt_opened.connect(self._on_remote_prompt_opened, Qt.QueuedConnection)
        self.hub.remote_prompt_closed.connect(self._on_remote_prompt_closed, Qt.QueuedConnection)
        self.hub.agent_prompt_answered.connect(self._on_agent_prompt_answered, Qt.QueuedConnection)

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
        # a hub device that arrives → auto-curate it too (local devices get this via
        # device_added, but remote ones are injected straight into the dashboard)
        self.dashboard.remote_added.connect(self._curate_remote_device)
        # a device raising a tag (alarm / event / gas-detected) → the shared TagStore
        # (DESIGN §7.3). Emitted from the device's poll thread, so marshal to the GUI.
        self.manager.device_tag.connect(self._on_device_tag, Qt.QueuedConnection)
        # a device raising an operator REQUEST (core.interaction) → the shared
        # PendingInteractions store, which the inbox / toast / control surface all
        # answer through (first-responder-wins). Also from the device's poll/reader
        # thread → QueuedConnection. On resolve we auto-emit a provenance tag.
        self.interactions = PendingInteractions(self)
        self.manager.device_prompt.connect(self._on_device_prompt, Qt.QueuedConnection)
        self.manager.device_prompt_withdrawn.connect(
            self._on_device_prompt_withdrawn, Qt.QueuedConnection)
        self.manager.device_removed.connect(self._on_device_removed)
        self.interactions.added.connect(self._on_prompt_added)
        self.interactions.resolved.connect(self._on_prompt_resolved)
        self._requests_toast = RequestToast(self.interactions, self._device_name_for, self)
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
                                          on_lens=self._set_source_lens_all,
                                          on_config=self._open_source_config)
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
            on_jump=self._jump_to_tag, on_open_media=self._open_media_tag,
            media_resolver=self._resolve_media,
            projects_provider=lambda: [(p.id, p.name)
                                       for p in self._project_mgr.projects()]
            if getattr(self, "_project_mgr", None) else [])
        self.events_dock = QDockWidget("Events", self)
        self.events_dock.setObjectName("EventsDock")
        self.events_dock.setWidget(self.events_panel)
        self.events_dock.setMinimumWidth(280)
        self.addDockWidget(Qt.RightDockWidgetArea, self.events_dock)

        # Requests: the operator inbox for device→app→device prompts (core.interaction).
        # A persistent list with a pending badge; each row's answer controls are
        # auto-generated from the prompt's kind. The dock title carries the live count.
        self.requests_panel = RequestsPanel(self.interactions, self._device_name_for)
        self.requests_dock = QDockWidget("Requests", self)
        self.requests_dock.setObjectName("RequestsDock")
        self.requests_dock.setWidget(self.requests_panel)
        self.requests_dock.setMinimumWidth(280)
        self.addDockWidget(Qt.RightDockWidgetArea, self.requests_dock)
        self.interactions.changed.connect(self._refresh_requests_badge)

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
        self.tabifyDockWidget(self.events_dock, self.requests_dock)
        self.tabifyDockWidget(self.requests_dock, self.docs_dock)
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
        self._restore_python_devices()       # rehydrate user-authored Python devices
        self._setup_control_api()
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
        filemenu.addSeparator()
        filemenu.addAction("Manage video storage…", self._open_video_cleanup)
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
        view.addAction(self.requests_dock.toggleViewAction())
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
        view.addSeparator()
        view.addAction("Benchmark…", self._open_benchmark)   # measure the real paths

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
        netmenu.addAction("External Control…", self._open_connections)
        netmenu.addAction("Connect a phone…", self._connect_phone)
        netmenu.addAction("Add Python device…", self._add_python_device)

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
        tb.addAction("📷 Photo", self._take_photo)
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

    def _open_benchmark(self):
        """Open the in-app benchmark (measures the real data-plane + render paths)."""
        from .benchmark import BenchmarkDialog
        if getattr(self, "_bench_dlg", None) is None:
            self._bench_dlg = BenchmarkDialog(self)
            self._bench_dlg.finished.connect(
                lambda *_: setattr(self, "_bench_dlg", None))
        self._bench_dlg.show()
        self._bench_dlg.raise_()
        self._bench_dlg.activateWindow()

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
                                 reads=self.reads,
                                 markers=self.dashboard.markers,
                                 video_store=self._video_store)
            win.destroyed.connect(lambda: setattr(self, "_timeline_win", None))
            self._timeline_win = win
        # scrub-to-preview backfill: a hub-only instant pulls its segment (§9.3 ph3)
        self._timeline_win.set_video_backfill(self.hub.video_backfill)
        self._timeline_win.set_pin_handler(self._pin_window)   # §12.1 pin-to-local
        self._timeline_win.show()
        self._timeline_win.raise_()
        self._timeline_win.activateWindow()

    def _open_devices(self):
        """The Devices manager (Available + Active, add/remove/configure) as a window."""
        if getattr(self, "_devices_win", None) is None:
            win = DevicesWindow(self.manager, self._open_config,
                                hub=getattr(self, "hub", None),
                                interactions=self.interactions, parent=self)
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
            list_cameras=self._doc_list_cameras,
            camera_shot=self._doc_camera_shot,
            list_media=self._doc_list_media,
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
                                     self._run_meta_markdown,
                                     self._doc_list_cameras,
                                     self._doc_camera_shot,
                                     self._doc_list_media)

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
        if not connected:
            self._retire_remote_prompts()       # relay gone → drop un-answerable remote cards
        if self.reads is not None:
            self.reads.invalidate()             # hub tier joined/left → coverage moved
        # surface (or retire) the hub's historic catalog as routable ports
        self.dashboard.refresh_ports()
        self._refresh_explorer()                # enable/disable the “On the hub…” item
        self._refresh_doc_collab()              # offer/retire the Collaborate toggle
        if getattr(self, "_timeline_win", None) is not None:   # (dis)connect → (un)wire
            self._timeline_win.set_video_backfill(self.hub.video_backfill)   # video backfill
        self._reconcile_prefetcher(connected)   # playback prefetch follows the hub (§12.1)

    def _reconcile_prefetcher(self, connected: bool) -> None:
        """Start the playback prefetcher on hub connect, stop it on disconnect. It
        pulls hub history into the local cache ahead of the head so replay never
        blocks and never skips hub data — the head HOLDS at the buffer edge (§12.1)."""
        tier = self.hub.read_tier if getattr(self, "hub", None) is not None else None
        want = bool(connected and tier is not None and self.resolver is not None
                    and self.time_context is not None and self._prefetch_cache is not None)
        if want and self._prefetcher is None:
            from ..store import PlaybackPrefetcher
            self._prefetcher = PlaybackPrefetcher(
                resolver=self.resolver, hub=tier, cache=self._prefetch_cache,
                tc=self.time_context, sources_fn=self.dashboard.source_keys,
                store=self.store_writer.store if self.store_writer is not None else None,
                deliver=self._gui_bridge.post, on_filled=self._on_prefetch_filled)
            self._prefetcher.start()
            self.time_context.set_buffer_gate(self._prefetcher.buffered_until)
        elif not want and self._prefetcher is not None:
            self.time_context.set_buffer_gate(None)
            self._prefetcher.stop()
            self._prefetcher = None
            if self._prefetch_cache is not None:
                self._prefetch_cache.clear()

    def _pin_window(self, t0, t1) -> None:
        """§12.1 Phase 3: promote the hub's data over [t0,t1] into the durable local
        store, so it survives a restart (the prefetch cache is RAM-only)."""
        if self._prefetcher is None:
            self.statusBar().showMessage("Pin needs a hub connection", 4000)
            return
        self.statusBar().showMessage("📌 Pinning this window into the local store…", 0)
        self._prefetcher.pin(t0, t1, on_done=lambda n: self.statusBar().showMessage(
            f"📌 Pinned {n} source(s) to the local store", 6000))

    def _on_prefetch_filled(self) -> None:
        """A prefetched range landed (GUI thread) → re-read the now-fuller LOCAL tiers.
        Hit back-to-back during a big backfill, so the redraws are COALESCED: while
        PLAYING the chart is left to advance() (it draws the head area incrementally as
        the head reaches each newly-cached slice) — a full-window reconcile per fill
        here just saturates the GUI and stalls replay; while PARKED one reconcile is
        scheduled per ~150 ms. The Timeline preview throttles itself (on_data_prefetched)."""
        if self.reads is not None:
            self.reads.invalidate()             # local coverage grew
        tl = getattr(self, "_timeline_win", None)
        if tl is not None:
            tl.on_data_prefetched()
        if self.chart_feed is None:
            return
        tc = self.time_context
        if tc is not None and tc.playing:
            return                              # play: advance() draws it — don't re-query
        if not self._prefetch_redraw_pending:   # parked: coalesce the full-window redraw
            self._prefetch_redraw_pending = True
            QTimer.singleShot(150, self._flush_prefetch_redraw)

    def _flush_prefetch_redraw(self) -> None:
        self._prefetch_redraw_pending = False
        tc = self.time_context
        if self.chart_feed is not None and not (tc is not None and tc.playing):
            self.chart_feed.reconcile(force=True)

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
            if agent:      # re-publish our still-open prompts so a joining viewer sees them
                for prompt in self.interactions.pending():
                    self.hub.publish_prompt(prompt)

    def _add_tag(self):
        dlg = _MarkerDialog(parent=self)
        if dlg.exec():
            label, comment = dlg.values()
            self.dashboard.markers.add(time.time(), label=label, comment=comment)
            self.events_dock.raise_()

    # -- media (DESIGN §9: snapshots) ------------------------------------------
    def _media_service(self):
        """Lazy MediaService against the ACTIVE project (it may switch)."""
        from ..core.media import MediaService
        return MediaService(
            latest=self.engine.latest,
            markers=self.dashboard.markers,
            media_dir=lambda: (self._project_mgr.active.media_dir
                               if self._project_mgr.active else ""),
            names=self.dashboard.source_names,
        )

    def _resolve_media(self, marker):
        """resolve(marker) → abs path|None against the ACTIVE project — the one
        provider the photo tile and the Events dock share."""
        from ..core.media import MediaService
        proj = self._project_mgr.active if getattr(self, "_project_mgr", None) else None
        if proj is None or marker is None:
            return None
        return MediaService.resolve(marker, proj.path)

    def _open_video_cleanup(self) -> None:
        """§9.3 manual cleanup: per-camera ambient-video usage + delete-older-than
        (the deliberate alternative to silent retention)."""
        if self._video_store is None:
            self.statusBar().showMessage("Ambient video is unavailable", 4000)
            return
        from .videocleanup import VideoCleanupDialog
        VideoCleanupDialog(self._video_store, self.dashboard.source_names(),
                           self).exec()

    def _open_media_tag(self, mid: str) -> None:
        """Events dock 🖼: open the photo in the system viewer."""
        from qtpy.QtCore import QUrl
        from qtpy.QtGui import QDesktopServices
        path = self._resolve_media(self.dashboard.markers.get(mid))
        if path is None:
            self.statusBar().showMessage(
                "Photo file not on this machine (media isn't synced in v1)", 5000)
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def _doc_list_cameras(self) -> list:
        """/cam macro: the online cameras as [{key, label}]."""
        return [{"key": k, "label": lbl}
                for k, lbl in sorted(self.dashboard.image_sources().items(),
                                     key=lambda kv: kv[1])]

    def _doc_list_media(self, limit: int = 50) -> list:
        """/cam macro: EXISTING project media to embed — the media tags whose files
        still exist, newest first, as [{name, abspath, kind: photo|clip}]. Lets a
        writer pick a prior photo/clip instead of only taking a fresh snapshot."""
        root = self._project_root()
        _VID = ("mp4", "mov", "mkv", "webm", "avi")
        out, seen = [], set()
        for m in reversed(self.dashboard.markers.of_kind("media")):   # newest first
            payload = m.payload or {}
            rel = payload.get("file")
            if not rel:
                continue
            ap = os.path.join(root, rel) if root else rel
            if ap in seen or not os.path.isfile(ap):
                continue
            seen.add(ap)
            kind = "clip" if (payload.get("format") or "").lower() in _VID else "photo"
            out.append({"name": m.label or os.path.basename(ap),
                        "abspath": ap, "kind": kind})
            if len(out) >= limit:
                break
        return out

    def _doc_camera_shot(self, key: str) -> dict:
        """/cam macro picked a camera: snapshot through the SAME path as the
        toolbar 📷 (file in media/ + a media tag), return {name, abspath} for the
        doc-relative embed — or {error} (shown in the editor status line)."""
        from ..core.media import MediaError
        try:
            res = self._media_service().snapshot(key)
        except MediaError as exc:
            return {"error": str(exc)}
        label = self.dashboard.image_sources().get(key, key)
        return {"name": f"📷 {label}", "abspath": res["path"]}

    def _take_photo(self):
        """Toolbar 📷: one camera → snap it; several → a menu of each plus
        'All cameras' (one tag per camera — "document the bench now")."""
        cams = self.dashboard.image_sources()
        if not cams:
            self.statusBar().showMessage("No camera source online", 4000)
            return
        if len(cams) == 1:
            self._snap([next(iter(cams))])
            return
        from qtpy.QtGui import QCursor
        from qtpy.QtWidgets import QMenu
        menu = QMenu(self)
        menu.addAction("All cameras", lambda: self._snap(list(cams)))
        menu.addSeparator()
        for key, label in sorted(cams.items(), key=lambda kv: kv[1]):
            menu.addAction(f"📷 {label}", lambda k=key: self._snap([k]))
        menu.exec(QCursor.pos())

    def _snap(self, keys: list) -> None:
        from ..core.media import MediaError
        try:
            results, errors = self._media_service().snapshot_all(keys)
        except Exception as exc:                    # noqa: BLE001 — never crash a photo
            self.statusBar().showMessage(f"Photo failed: {exc}", 6000)
            return
        if results:
            names = ", ".join(os.path.basename(r["relpath"]) for r in results)
            self.statusBar().showMessage(f"📷 saved {names}", 5000)
            self.events_dock.raise_()               # the tag is the reference
        for key, reason in errors:
            label = self.dashboard.image_sources().get(key, key)
            self.statusBar().showMessage(f"📷 {label}: {reason}", 6000)

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

    def _on_device_tag(self, marker) -> None:
        """A device raised a tag (alarm/event) → merge it into the shared TagStore, from
        which charts + the event log update. Runs on the GUI thread (QueuedConnection).
        upsert_LOCAL (not upsert): the device is on THIS box, so this is a LOCAL emission
        that must be announced (tag_changed) and thus published to the hub → reaches
        viewers. Plain upsert stays silent (it's for tags merged from a peer) and would
        leave device tags stranded on the owning box (they never reached other clients)."""
        self.dashboard.markers.upsert_local(marker)

    # -- device → app → device requests (core.interaction) -------------------
    def _device_name_for(self, device_id: str) -> str:
        """A friendly name for a prompt's raising device (by uuid or instance_id),
        falling back to the id — for the inbox/toast card header."""
        for d in self.manager.active_descriptors():
            if device_id in (getattr(d, "uuid", None), d.instance_id):
                return d.name
        return device_id

    def _on_device_prompt(self, prompt, on_response) -> None:
        """A device raised an operator REQUEST → file it in the shared store (which pops
        the toast + fills the inbox). Runs on the GUI thread (QueuedConnection); the
        driver's on_response is invoked here when the operator answers."""
        self.interactions.add(prompt, on_response)
        if self.interactions.get(prompt.id) is not None:   # not dropped by the flood guard
            self.hub.publish_prompt(prompt)                # mirror to the hub (no-op unless agent)

    def _on_device_prompt_withdrawn(self, prompt_id) -> None:
        """A device RESOLVED its own request (its front panel / another transport answered) →
        retire it from the inbox without this app answering it, so a handled-on-device modal
        doesn't linger as pending. GUI thread (QueuedConnection)."""
        self.interactions.withdraw_ids(prompt_id)
        self.hub.close_prompt(prompt_id, by="device")   # tell viewers it resolved on the device

    def _on_remote_prompt_opened(self, wire) -> None:
        """VIEWER: a device prompt raised on ANOTHER client (its owning agent) → surface it in
        OUR inbox; answering relays back to the owner over the hub. Idempotent by id (the store
        dedups a re-announce / snapshot)."""
        if self.interactions.get(wire.id) is not None:
            # already in our inbox → our OWN published prompt echoed back to us (we're also a
            # viewer on this hub), or a re-announced remote we already hold. Do NOT re-inject or
            # mark it 'remote' — a local prompt must keep its real driver callback + its tag/close.
            return
        from ..net.prompts import prompt_from_wire
        prompt = prompt_from_wire(wire)
        self._remote_prompt_ids.add(prompt.id)
        self.interactions.add(
            prompt,
            on_response=lambda answer, pid=prompt.id: self.hub.respond_remote_prompt(
                pid, answer, by=self.hub.actor))

    def _on_remote_prompt_closed(self, prompt_id) -> None:
        """VIEWER: a remote prompt resolved (answered anywhere / device / owner disconnect) →
        withdraw it from our inbox."""
        self.interactions.withdraw_ids(prompt_id)
        self._remote_prompt_ids.discard(prompt_id)

    def _retire_remote_prompts(self) -> None:
        """The hub relay is gone (disconnect / link drop) → retire any remote prompt cards, which
        can no longer be answered, rather than leave dead entries that silently no-op on a click."""
        if self._remote_prompt_ids:
            self.interactions.withdraw_ids(*self._remote_prompt_ids)
            self._remote_prompt_ids.clear()

    def _on_agent_prompt_answered(self, prompt_id, answer, by) -> None:
        """AGENT: a viewer answered a prompt WE own (over the hub) → resolve it in the local
        store, which fires the driver's on_response (→ RESPOND to the device) and broadcasts the
        resolution to every surface. first-responder-wins: a no-op if already answered."""
        self.interactions.resolve(prompt_id, answer, by=(f"hub:{by}" if by else "hub"))

    def _on_device_removed(self, ids) -> None:
        """A device was removed → withdraw its still-open requests (no answer, no callback
        into the now-dead driver). ids = (uuid, instance_id); a prompt carries whichever is
        its data_id, so match on both."""
        uuid, instance_id = ids
        idset = {uuid, instance_id}
        gone = [p.id for p in self.interactions.pending() if p.device_id in idset]
        self.interactions.withdraw(uuid, instance_id)
        for pid in gone:      # AGENT: close them on the hub too, else viewers keep ghost cards
            self.hub.close_prompt(pid, by="device removed")

    def _on_prompt_added(self, prompt) -> None:
        """A new request arrived → pop the non-blocking arrival toast and (for a critical
        one) surface the Requests inbox. Never a hard modal — the operator can keep working."""
        self._requests_toast.present(prompt)
        if prompt.is_critical:
            self.requests_dock.show()
            self.requests_dock.raise_()
        # a badge/hint in the status bar so the count is visible even with the dock hidden
        self.statusBar().showMessage(
            f"Device request: {self._device_name_for(prompt.device_id)} — "
            f"{prompt.title or prompt.question}", 8000)

    def _on_prompt_resolved(self, entry) -> None:
        """A request was answered (by ANY surface) → drop an origin=device provenance TAG
        recording the outcome + who answered, so the timeline carries the interaction as a
        fact (reuses the tag path, like an emitted device event)."""
        prompt = entry.prompt
        answer = entry.answer
        if prompt.id in self._remote_prompt_ids:
            # a hub-injected (remote) prompt answered on THIS viewer: the answer already relayed
            # to the OWNING agent (via on_response), which emits the provenance tag + broadcasts
            # the close. We must NOT double-record it here.
            self._remote_prompt_ids.discard(prompt.id)
            return
        if answer is True:
            shown = "Yes"
        elif answer is False:
            shown = "No"
        elif answer is None:
            shown = "(no answer)"
        else:
            shown = str(answer)
        ok = getattr(entry, "ok", True)
        # The tag must not claim the device acted on the answer if its callback threw
        # (e.g. the ack write failed on a flaky link) — the audit record stays honest.
        failed = "" if ok else "  ⚠ device ack FAILED"
        self.dashboard.markers.add(
            entry.answered_at or time.time(),
            label=f"↩ {shown}" + ("" if ok else " ⚠"),
            comment=(f"{prompt.title or prompt.question} — answered by "
                     f"{entry.answered_by}{failed}"),
            kind="interaction", origin_kind=ORIGIN_DEVICE, origin_id=prompt.device_id,
            scope=f"device:{prompt.device_id}", severity=prompt.severity,
            payload={"prompt_id": prompt.id, "kind": prompt.kind, "answer": answer,
                     "answered_by": entry.answered_by, "question": prompt.question,
                     "ok": ok},
            immutable=True)
        # AGENT: broadcast the resolution over the hub so every viewer withdraws it (no-op
        # unless we're an agent). This is the close that closes it everywhere.
        self.hub.close_prompt(prompt.id, shown, entry.answered_by)

    def _refresh_requests_badge(self) -> None:
        """Keep the Requests dock title showing the pending count (a persistent badge)."""
        n = self.interactions.count()
        self.requests_dock.setWindowTitle(f"Requests ({n})" if n else "Requests")

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
            desc = next((d for d in self.manager.active_descriptors()
                         if d.instance_id == iid), None)
            if desc is not None:
                self._curate_dev_id(getattr(desc, "uuid", "") or iid)
        # defer one tick so the dashboard has rebuilt its ports for the new device
        QTimer.singleShot(0, apply)

    def _curate_remote_device(self, uuid: str) -> None:
        """A hub device just appeared → auto-curate its channels too. Local devices get
        this via manager.device_added; remote ones are injected into the dashboard, so
        the Workspace.remote_added signal drives it. Its ports already exist here."""
        self._curate_dev_id(uuid)

    def _curate_dev_id(self, dev_id: str) -> None:
        """Append device `dev_id`'s channels to the active project's lens (so a curated
        project doesn't silently hide a just-arrived device). No-op for a show-all
        project or when every channel is already listed; never re-adds a curated-out one."""
        proj = self._project_mgr.active
        if proj is None:
            return
        existing = proj.source_keys()
        if not existing:
            return
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
        self._restore_pending_hub_project()               # land on the last-active hub project
        self._refresh_explorer()

    def _restore_pending_hub_project(self) -> None:
        """If a HUB project was the active one last session, it couldn't be restored during
        __init__ (the hub wasn't connected yet, so it wasn't in the registry) — the app fell
        back to a local project. Once the hub delivers that project, land on it and restore its
        full working session (layout + arrangement + devices). Fires once per launch."""
        if getattr(self, "_hub_restore_done", False):
            return
        mgr = getattr(self, "_project_mgr", None)
        if mgr is None:
            return
        pid = mgr.pending_active
        if not pid or mgr.get(pid) is None:               # nothing pending, or not synced yet
            return
        self._hub_restore_done = True                     # one-shot
        self._switch_project(pid)                         # full restore via the (fixed) switch path

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
        path = p.layout_path(names[0]) if names else p.layout_path("Default")
        if os.path.exists(p.working_path):
            # FULL restore — the target's live working session carries the panel ARRANGEMENT
            # and its saved DEVICES, not just the layout model. Switching a project (or
            # reopening a hub one) used to import only ["layout"], dropping both. geometry=False
            # keeps the window chrome put across a mid-session switch.
            self.open_session(p.working_path, geometry=False)
        elif names:                                # a named layout but no working session yet
            layout = {}
            try:
                with open(path, encoding="utf-8") as fh:
                    layout = json.load(fh).get("layout", {})
            except Exception:                      # noqa: BLE001
                layout = {}
            self.dashboard.import_layout(layout)
        else:                                      # brand-new project → a default layout
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

    def _retry_empty_sigma_timelines(self) -> None:
        """Evict only the σ-timeline entries that resolved EMPTY, so a model first
        logged after the key was seen still shows up — without re-reading resolved
        models from the store every 2 s on the draw path."""
        for k in [k for k, v in self._sigma_timelines.items() if not v]:
            self._sigma_timelines.pop(k, None)

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

    def _chart_coverage(self, key):
        """coverage(key) → merged [(t0,t1), …] for a chart's gap breaks — and the
        draw path must NEVER block: with a hub attached, resolver.coverage()
        reaches the remote tier's GetCoverage (a gRPC with a 4 s timeout), and the
        2 s cache clear made every live batch draw eligible for a network stall
        (watchdog: >700 ms on the GUI thread). Stale-while-revalidate: a cache
        miss returns the LAST KNOWN intervals immediately and refreshes through
        ReadService (worker pool + its own TTL); gap breaks change on ≥30 s
        scales, so one draw of staleness is invisible. Headless (no ReadService):
        LOCAL tiers synchronously — never the network."""
        resolver = getattr(self, "resolver", None)
        if resolver is None:
            return []
        cov = self._coverage_cache.get(key)
        if cov is not None:
            return cov
        reads = getattr(self, "reads", None)
        if reads is None:                      # headless/tests: local tiers only
            try:
                cov = list(resolver.coverage(key, local_only=True))
            except Exception:                  # noqa: BLE001 — never break a draw
                cov = []
            self._coverage_cache[key] = cov
            return cov

        def _store(res):
            got = list(res.get(key, ()))
            self._coverage_cache[key] = got
            self._coverage_stale[key] = got
        reads.coverage_many([key], key=("chart-cov", key), on_result=_store)
        return self._coverage_stale.get(key, [])

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
                          store=self.store_writer.store if self.store_writer else None,
                          media_root=self._project_root())
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
        """Start/stop a recording. The scalar lifecycle lives in
        RecordingController; video rides along (§9.3): capture reconciles to the
        record state, and on stop the span's ambient segments are materialized
        into clips (after a grace delay so the tail segment has landed)."""
        rec = self._recording
        state = rec.toggle()
        self.record_action.setText("■ Stop" if state == "started" else "● Record")
        if self._video_capture is not None:
            self._video_capture.reconcile()           # While-recording mode gate
        if state == "stopped" and self._video_store is not None:
            mid = rec.last_closed_mid
            m = self.dashboard.markers.get(mid) if mid else None
            if m is not None:
                QTimer.singleShot(3000,
                                  lambda mid=mid, a=m.t, b=m.t_end:
                                  self._materialize_clips(mid, a, b))

    # -- ambient video clips (§9.3) -------------------------------------------
    def _project_root(self):
        p = self._project_mgr.active if getattr(self, "_project_mgr", None) else None
        return p.path if p is not None else None

    def _clip_materializer(self):
        from ..core.media import ClipMaterializer
        return ClipMaterializer(
            self._video_store,
            media_dir=lambda: (self._project_mgr.active.media_dir
                               if self._project_mgr.active else ""))

    def _materialize_clips(self, rec_mid: str, t0: float, t1: float) -> None:
        """Clip every camera with ambient video over [t0,t1] into the project +
        a MEDIA span tag linked to the recording marker (rec_mid) so a later
        marker MOVE re-materializes it. ffmpeg runs OFF the GUI thread; the tags
        are added back on it."""
        if self._video_store is None or t0 is None or t1 is None:
            return
        mat = self._clip_materializer()
        names = self.dashboard.source_names()
        jobs = [(cam, f"{cam}/frame", names.get(f"{cam}/frame") or cam)
                for cam in self._video_store.cameras()]
        if not jobs:
            return

        def work(_ctx):
            out = []
            for cam, key, label in jobs:
                try:
                    res = mat.materialize(cam, t0, t1, label)
                except Exception:                      # noqa: BLE001
                    res = None
                if res is not None:
                    out.append((key, label, res))
            return out

        def done(out):
            new_tags = []
            for key, label, res in out:
                mid = self.dashboard.markers.add(
                    t0, t_end=t1, kind="media", label=f"🎬 {label}",
                    payload={"file": res["file"], "files": res["files"],
                             "source": key, "format": "mp4",
                             "clip": "documentation", "rec_mid": rec_mid})
                new_tags.append(self.dashboard.markers.get(mid))
            if out:
                self.statusBar().showMessage(
                    f"🎬 saved {len(out)} clip(s) to media/", 5000)
                self._bundle_clips_into_run(rec_mid, t0, t1, new_tags)

        run_task(work, title="Saving clips", exclusive=f"clip:{rec_mid}",
                 on_done=done)

    def _bundle_clips_into_run(self, rec_mid, t0, t1, tags) -> None:
        """Copy freshly-materialized clips into the recording's run bundle. The
        auto-export at Stop runs OFF the GUI thread and only sets the marker's
        run_dir when it COMPLETES — for a long/multi-source recording that can
        outlast clip materialization. So if the bundle isn't on disk yet we STASH
        the clips and let _on_recording_saved flush them once the export lands
        (previously a fixed 3 s timer raced the export and silently dropped the
        video from exactly those long recordings). Off the GUI thread; a no-op
        only if the recording is never exported to a run dir (e.g. export failed)."""
        root = self._project_root()
        if not root or not tags:
            return
        m = self.dashboard.markers.get(rec_mid)
        run_dir = getattr(m, "run_dir", None) if m is not None else None
        if not run_dir:                              # export still in flight → defer
            self.__dict__.setdefault("_pending_clip_bundles", {})[rec_mid] = \
                (t0, t1, tags)
            return
        self.__dict__.get("_pending_clip_bundles", {}).pop(rec_mid, None)
        from ..core.markers import marker_to_dict
        dicts = [marker_to_dict(t) for t in tags]

        def work(_ctx):
            from ..store import append_media_to_bundle
            try:
                return append_media_to_bundle(run_dir, dicts, t0, t1, root)
            except Exception:                        # noqa: BLE001
                return 0

        run_task(work, title="Adding clips to the run bundle",
                 exclusive=f"bundle-clip:{rec_mid}")

    def _rematerialize_clips_for(self, mid: str) -> None:
        """A RECORDING marker moved → re-slice its clips from ambient over the
        new span (the §9.3 promise: the clip follows the marker). Debounced per
        marker; ffmpeg runs off the GUI thread."""
        if self._video_store is None:
            return
        m = self.dashboard.markers.get(mid)
        if m is None or m.kind != "recording" or m.t_end is None:
            return
        pend = self.__dict__.setdefault("_clip_rematerialize_pending", {})
        t = pend.get(mid)
        if t is None:
            t = QTimer(self)
            t.setSingleShot(True)
            t.timeout.connect(lambda mid=mid: self._do_rematerialize(mid))
            pend[mid] = t
        t.start(700)                                   # coalesce a drag's edits

    def _do_rematerialize(self, mid: str) -> None:
        m = self.dashboard.markers.get(mid)
        if m is None or m.t_end is None or self._video_store is None:
            return
        mat = self._clip_materializer()
        jobs = []
        for tag in list(self.dashboard.markers.all()):
            if tag.kind == "media" and (tag.payload or {}).get("rec_mid") == mid:
                cam = (tag.payload.get("source", "") or "").rsplit("/", 1)[0]
                jobs.append((tag.id, cam, dict(tag.payload)))
        if not jobs:
            return
        a, b = m.t, m.t_end

        def work(_ctx):
            out = []
            for tag_id, cam, payload in jobs:
                try:
                    res = mat.materialize(cam, a, b, payload.get("source", cam))
                except Exception:                      # noqa: BLE001
                    res = None
                if res is not None:
                    out.append((tag_id, payload, res))
            return out

        def done(out):
            for tag_id, payload, res in out:
                self.dashboard.markers.update(
                    tag_id, t=a, t_end=b,
                    payload={**payload, "file": res["file"],
                             "files": res["files"]})

        run_task(work, title="Re-slicing clips", exclusive=f"reclip:{mid}",
                 on_done=done)

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
        # the run bundle now exists (run_dir was set just before this fired) — flush
        # any clips that materialized while the export was still running (§9.3).
        pending = self.__dict__.get("_pending_clip_bundles", {}).pop(mid, None)
        if pending is not None:
            t0, t1, tags = pending
            self._bundle_clips_into_run(mid, t0, t1, tags)

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
        root = self._project_root()          # media_root: clips+photos into the bundle

        def work(ctx):
            if writer is not None:
                try:
                    writer.flush_all()               # a clean stop loses nothing
                except Exception:                    # noqa: BLE001
                    pass
            return export_window(dest, sources, resolver, t0, t1, tags=tags,
                                 store=store, media_root=root)

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

    def _refresh_dirty_sinks(self) -> None:
        """A device polled back a changed control state (e.g. HV toggled on the front
        panel) → re-announce its descriptor so the config UIs (local + remote) reflect
        it. Checks ALL active devices so every dirty flag is cleared."""
        if self.manager is None:
            return
        dirty = [d for d in self.manager.active_devices()
                 if getattr(d, "take_sink_dirty", lambda: False)()]
        if dirty:
            self.manager.active_changed.emit()

    # -- external control surface (self-describing API for connectors) -------
    def _setup_control_api(self):
        """Build the control-surface registry + connector auth. The loopback server
        stays OFF until the user enables it in Connections — no port opens on startup."""
        self._control_surface = None
        self._connectors = None
        self._localapi = None
        self._companion = None           # phone companion (LAN) server
        self._phone_cid = None           # the phone's preshared connector id
        self._phone_win = None           # the "Connect a phone" dialog
        try:
            from ..core.connectors import ConnectorRegistry
            from .appcontrol import build_control_surface
            self._control_surface = build_control_surface(self)
            self._connectors = ConnectorRegistry()
            self._connectors.set_pairing_notifier(
                lambda p: self._pairing_request.emit(p))     # → GUI thread (queued)
            self._pairing_request.connect(self._on_pairing_request, Qt.QueuedConnection)
        except Exception:                    # noqa: BLE001 — never block startup on this
            logging.getLogger("ferrodac").warning("control API setup failed", exc_info=True)

    def _on_pairing_request(self, pairing):
        from .connections import PairingDialog
        if self._connectors is None:
            return
        dlg = PairingDialog(pairing, self)
        dlg.exec()
        decision = dlg.decision
        if decision and decision[0]:
            self._connectors.approve(pairing.id, scope=decision[1])
            self.statusBar().showMessage(
                f"Control connector “{pairing.name}” approved ({decision[1]})", 6000)
        else:
            self._connectors.deny(pairing.id)

    def _control_api_running(self) -> bool:
        return self._localapi is not None

    def _control_api_port(self) -> int:
        return self._localapi.port if self._localapi is not None else 0

    def _enable_control_api(self, on: bool):
        QSettings("ferroDAC", "ferroDAC").setValue("control/enabled", bool(on))
        if on and self._localapi is None and self._control_surface is not None:
            from .. import __version__
            from ..net.localapi import LocalApiServer
            want = self._configured_control_port()
            attempts = [want, 0] if want else [0]    # a busy pinned/default port → ephemeral
            last = None
            for p in attempts:
                srv = LocalApiServer(
                    self._control_surface, self._connectors, version=__version__,
                    port=p, on_audit=self._on_control_audit)
                try:
                    srv.start()
                    self._localapi = srv
                    self.statusBar().showMessage(
                        f"Local control API on 127.0.0.1:{srv.port}", 6000)
                    return
                except Exception as exc:     # noqa: BLE001 — try the next candidate port
                    last = exc
                    try:
                        srv.stop()
                    except Exception:
                        pass
            self.statusBar().showMessage(f"Control API failed to start: {last}", 8000)
        elif not on and self._localapi is not None:
            self._localapi.stop()
            self._localapi = None
            self.statusBar().showMessage("Local control API stopped", 4000)

    def autostart_control_api(self) -> None:
        """On by default: bring up the loopback control API on launch unless the user turned
        it off (persisted 'control/enabled'). Called from the entry point AFTER show() — never
        from __init__, so constructing a MainWindow (tests) never opens a port."""
        try:
            enabled = QSettings("ferroDAC", "ferroDAC").value(
                "control/enabled", True, type=bool)
        except Exception:                    # noqa: BLE001
            enabled = True
        if enabled:
            self._enable_control_api(True)

    @staticmethod
    def _configured_control_port() -> int:
        """The control-API port to bind. FERRODAC_CONTROL_PORT wins; else the persisted
        'control/port' setting; else DEFAULT_CONTROL_PORT. Set the env/setting to 0 to force
        an OS-assigned ephemeral port (carried in ~/.config/ferrodac/connector.json). A busy
        pinned/default port falls back to ephemeral at start."""
        env = os.environ.get("FERRODAC_CONTROL_PORT", "").strip()
        if env:
            try:
                return int(env)
            except ValueError:
                pass
        try:
            v = QSettings("ferroDAC", "ferroDAC").value("control/port", None)
            if v is not None and str(v).strip() != "":
                return int(v)
        except Exception:                    # noqa: BLE001 — a bad setting → the default
            pass
        return DEFAULT_CONTROL_PORT

    def _on_control_audit(self, name, verb, ok, detail):
        logging.getLogger("ferrodac.control").info(
            "connector %s: %s %s%s", name, verb, "ok" if ok else "FAIL",
            f" ({detail})" if detail else "")

    def _open_connections(self):
        if getattr(self, "_conn_win", None) is None:
            if self._connectors is None:
                self.statusBar().showMessage("Control API unavailable", 4000)
                return
            from .connections import ConnectionsWindow
            win = ConnectionsWindow(
                self._connectors, is_running=self._control_api_running,
                on_toggle=self._enable_control_api, get_port=self._control_api_port,
                parent=self)
            win.destroyed.connect(lambda: setattr(self, "_conn_win", None))
            self._conn_win = win
        self._conn_win.show()
        self._conn_win.raise_()
        self._conn_win.activateWindow()

    def _connect_phone(self):
        """Start the LAN phone-companion server + show a QR to pair a phone (uploads
        photos into the active project over the shared control surface)."""
        if getattr(self, "_phone_win", None) is not None:      # already open
            self._phone_win.show()
            self._phone_win.raise_()
            self._phone_win.activateWindow()
            return
        if self._control_surface is None or self._connectors is None:
            self.statusBar().showMessage("Control API unavailable", 4000)
            return
        from .. import __version__
        from ..net.companion import CompanionServer
        from .phonepair import PairPhoneDialog, lan_ip

        def _active_name():
            pm = getattr(self, "_project_mgr", None)
            return pm.active.name if pm is not None and pm.active else ""

        # mint the phone's preshared key AFTER we commit to starting — and revoke it
        # on any failure so a live control-scope credential never leaks un-torn-down.
        conn, psk = self._connectors.rotate_preshared("phone", scope="control")
        self._phone_cid = conn.id
        try:
            self._companion = CompanionServer(
                self._control_surface, self._connectors, host="0.0.0.0",
                version=__version__, get_project=_active_name)
            self._companion.start()
        except Exception as exc:              # noqa: BLE001
            self._connectors.revoke(self._phone_cid)
            self._phone_cid = None
            if getattr(self, "_companion", None) is not None:
                self._companion.stop()
            self._companion = None
            self.statusBar().showMessage(f"Phone companion failed to start: {exc}", 8000)
            return
        url = f"http://{lan_ip()}:{self._companion.port}"

        def _regen():
            c, p = self._connectors.rotate_preshared("phone", scope="control")
            self._phone_cid = c.id
            return p

        def _revoke():
            if self._phone_cid:
                self._connectors.revoke(self._phone_cid)
                self._phone_cid = None

        def _teardown(*_):
            if getattr(self, "_companion", None) is not None:
                self._companion.stop()
                self._companion = None
            if self._phone_cid:
                self._connectors.revoke(self._phone_cid)   # kill the link on close
                self._phone_cid = None
            self._phone_win = None

        dlg = PairPhoneDialog(self, url=url, psk=psk,
                              on_regenerate=_regen, on_revoke=_revoke)
        dlg.finished.connect(_teardown)
        self._phone_win = dlg
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()
        self.statusBar().showMessage(f"Phone companion on {url}", 6000)

    # -- python devices (user-authored virtual instruments) ------------------
    def _add_python_device(self):
        """Mint a new Python device, activate it, and open its config dialog so the
        user can edit the code that produces its channels."""
        try:
            from ..devices.python_device import PythonDevice, save_def
        except Exception as exc:              # noqa: BLE001
            self.statusBar().showMessage(f"Python device unavailable: {exc}", 6000)
            return
        dev = PythonDevice.new()
        save_def(dev.instance_id, dev.code)   # persist the starter so it survives restart
        self.manager.add_user_device(dev, user=True)
        self._open_config(dev.instance_id)    # open the code editor
        self.statusBar().showMessage(
            "Added a Python device — edit its code in the config dialog", 6000)

    def _restore_python_devices(self):
        """Re-mint every saved Python device at startup so a user's virtual instruments
        survive a restart. A bad def must never block launch."""
        try:
            from ..devices.python_device import PythonDevice
            for dev in PythonDevice.restore_all():
                self.manager.add_user_device(dev, user=False)   # silent restore, no curate
        except Exception:                     # noqa: BLE001
            logging.getLogger("ferrodac").warning(
                "python device restore failed", exc_info=True)

    def _open_source_config(self, source_key: str) -> None:
        """The ⚙ on a source card → open its owning device's config/control section.
        Local devices open the ConfigDialog; hub-remote devices open the
        RemoteControlDialog (control sinks over SendCommand, §5.3)."""
        did = source_key.split("/", 1)[0]                # device identity is the first seg
        kind = self.dashboard.source_origin(source_key)
        if kind == "remote":
            from .docks import RemoteControlDialog
            dlg = self._dialogs.get(source_key)
            if dlg is not None:
                dlg.raise_(); dlg.activateWindow(); return
            dlg = RemoteControlDialog(
                did, self.dashboard.remote_name(did),
                self.dashboard.remote_sinks(did), self.dashboard.remote_options(did),
                self.dashboard.send_command, self.dashboard.set_config,
                dashboard=self.dashboard, parent=self)
            dlg.setAttribute(Qt.WA_DeleteOnClose, True)
            dlg.destroyed.connect(lambda *_: self._dialogs.pop(source_key, None))
            self._dialogs[source_key] = dlg
            dlg.show()
        elif kind == "device":
            self._open_config(self.manager.instance_for_uuid(did) or did)

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
            QTimer.singleShot(0, self._slim_bottom_dock)   # fresh start: after show()

    def _restore_and_enable_autosave(self):
        try:
            self.open_session(self._working_path())
        except Exception as exc:                # a bad layout must never freeze the window
            logging.getLogger("ferrodac").warning("session restore failed: %s", exc)
        finally:
            self.setUpdatesEnabled(True)        # paint once, already assembled
        self._autosave_on = True
        self._recover_open_recordings()         # finalise any crash-interrupted REC
        self._slim_bottom_dock()                # keep the transport/log strip modest even
        #                                         over a saved-tall layout (restore ran above)

    def _slim_bottom_dock(self):
        """The Player is a one-row transport bar, so the bottom dock it shares (tabbed)
        with the Log only needs room for a few log lines — not a third of the window.
        Restored/default layouts tend to size that tabbed area to the log view's tall
        native hint, leaving the raised Player tab mostly empty; pull it back to the
        Log's modest sizeHint. The user can still drag it taller (it re-persists)."""
        if getattr(self, "player_dock", None) is None:
            return                              # no transport → the Log starts hidden
        try:
            h = self.log_panel.sizeHint().height()
            self.resizeDocks([self.log_dock], [h], Qt.Vertical)
        except Exception:
            pass                                # sizing is cosmetic; never break startup

    def open_session(self, path: str, *, geometry: bool = True) -> None:
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception as exc:
            self.statusBar().showMessage(f"Could not open layout: {exc}", 5000)
            return
        # rebuild the model first (so docks exist), then restore Qt state
        self.dashboard.import_layout(data.get("layout", {}))
        self.manager.request_devices(data.get("devices", []))
        dock = data.get("dock", {})
        if dock.get("workspace"):                   # the panel ARRANGEMENT — always restore it
            self.workspace.restoreState(QByteArray.fromBase64(dock["workspace"].encode()))
        # the main-window chrome (geometry + outer dock layout) only on a full/startup restore;
        # a mid-session project switch keeps the window put (geometry=False).
        if geometry and dock.get("geometry"):
            self.restoreGeometry(QByteArray.fromBase64(dock["geometry"].encode()))
        if geometry and dock.get("window"):
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
                info = self._srcinfo_cache.get(key)
                if info is None:                    # new key → one small zarr read;
                    info = resolve_source(key, store=st)   # cache cleared on
                    self._srcinfo_cache[key] = info        # provenance_changed
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
        if tc is None:
            return
        # This 50 ms timer runs whether or not we're playing, so reconcile EVERY
        # frame to keep the chart's display mode in lockstep with the transport.
        # reconcile() no-ops unless the mode flipped, so it cheaply catches BOTH
        # transitions that have no reset (nav) to ride on: PARKED→PLAYING (draw the
        # window envelope under the sweeping playhead) and PLAYING→PAUSE (redraw the
        # owned parked envelope). The pause redraw used to be MISSED — we returned
        # early below before reconciling — so the chart stayed blank after Play
        # cleared it (§22 I-8).
        self.chart_feed.reconcile()
        if not tc.playing:
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
        self._coverage_cache.clear()                 # re-read gaps for the new slice
        if self.time_context is not None:           # re-bin waterfalls to the new window
            self.dashboard.set_time_window(*self.time_context.window)
        self.chart_feed.reconcile(force=True)        # navigation → re-derive mode, redraw
        #                                              owned envelopes / go-live history

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        toast = getattr(self, "_requests_toast", None)
        if toast is not None and toast.isVisible():
            toast.reposition()                    # keep the request banner top-right

    def closeEvent(self, event):  # noqa: N802
        if getattr(self, "_localapi", None) is not None:
            self._localapi.stop()                 # close the control-API port cleanly
        if getattr(self, "_companion", None) is not None:
            self._companion.stop()                # close the phone-companion LAN port
        if getattr(self, "_video_capture", None) is not None:
            self._video_capture.stop()            # close + commit open segments
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
        if getattr(self, "_bench_dlg", None) is not None:
            self._bench_dlg.close()         # stop the benchmark worker thread cleanly
        if getattr(self, "reads", None) is not None:
            self.reads.shutdown()           # cancel in-flight timeline reads
        if getattr(self, "_prefetcher", None) is not None:
            self._prefetcher.stop()         # stop the playback prefetch worker
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


def _configure_video_encoding() -> None:
    """Choose the ambient-video (§9.3) H.264 encoder path. Qt's FFmpeg backend
    PREFERS hardware VAAPI, but many GPUs/drivers have no usable H.264 encode
    profile and Qt then fails ('No usable encoding profile found') WITHOUT falling
    back on its own. So: try hardware by default, and fall back to software the
    instant Qt's recorder actually reports it can't encode (camera._on_record_error
    persists a machine-global flag). Here we only honour that persisted verdict —
    Qt's real behaviour is the ground truth, not a proxy probe (a system-ffmpeg
    probe both false-positived on VAAPI and would false-negative on frozen builds).
    Must run BEFORE any Qt Multimedia object exists — Qt reads the var once. A user
    who set QT_FFMPEG_ENCODING_HW_DEVICE_TYPES themselves always wins; Linux only."""
    import logging
    import os
    import sys

    if sys.platform != "linux":                        # d3d11va/videotoolbox: trust Qt
        return
    if "QT_FFMPEG_ENCODING_HW_DEVICE_TYPES" in os.environ:
        return                                         # explicit user choice wins
    from ..core.videostore import prefer_software_encode
    if not prefer_software_encode():
        return                                         # try hardware; fall back on real failure
    os.environ["QT_FFMPEG_ENCODING_HW_DEVICE_TYPES"] = ""   # proven broken here → software
    logging.getLogger("ferrodac.video").info(
        "using software H.264 — a prior run found no usable hardware encoder here")


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

    # ambient video encoder selection MUST precede any Qt Multimedia init (Qt reads
    # the hw-encoder env var once): default to hardware, but honour a persisted
    # 'hardware H.264 is broken here' verdict from a prior run (§9.3).
    _configure_video_encoding()

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
