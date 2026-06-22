"""In-app document view (slice 1: render-only).

The **`.md` file is the source of truth**. This view never writes disk — it renders
the file and re-renders whenever *anything* changes it (so editing the raw file in
your own editor, e.g. Neovim, Just Works: the watcher notices the save). QtWebEngine
hosts the bundled web renderer (``ferrodac/ui/web/dist``), built offline — no CDN.

Later slices add in-app editing (CodeMirror), git history, and a live-collab overlay;
all keep this same file-as-truth contract (the live layer materialises to the file).
"""

from __future__ import annotations

import os

# Chromium flags must be set BEFORE QtWebEngine initialises (first QWebEngineView).
# Software rendering avoids the most common Linux GPU-process crashes for a
# text renderer; the sandbox is only disabled when running as root (e.g. a dev
# box) — a normal-user desktop keeps it on.
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu")
if getattr(os, "geteuid", lambda: 1)() == 0:
    os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")

from qtpy.QtCore import QFileSystemWatcher, QObject, Qt, QUrl, Signal, Slot
from qtpy.QtWebChannel import QWebChannel
from qtpy.QtWebEngineWidgets import QWebEngineView
try:                                                 # Qt6: settings live in QtWebEngineCore
    from qtpy.QtWebEngineCore import QWebEngineSettings
except Exception:                                    # pragma: no cover — binding variance
    QWebEngineSettings = None
from qtpy.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

_DIST = os.path.join(os.path.dirname(__file__), "web", "dist")


def editor_command_args(command: str, path: str) -> list:
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


def launch_external_editor(path: str) -> str:
    """Open `path` in the user's CONFIGURED editor command (QSettings
    ``editor/command``), else the OS default handler. Returns '' on success or an
    error string. The built-in default behind a DocView's ``↗ Open externally``."""
    from qtpy.QtCore import QSettings
    from qtpy.QtGui import QDesktopServices
    cmd = (QSettings("ferroDAC", "ferroDAC").value(
        "editor/command", "", type=str) or "").strip()
    if cmd:
        import subprocess
        try:
            subprocess.Popen(editor_command_args(cmd, path), start_new_session=True)
            return ""
        except Exception as exc:                       # noqa: BLE001
            return str(exc)
    QDesktopServices.openUrl(QUrl.fromLocalFile(path))
    return ""


def configure_editor_command(parent=None) -> None:
    """Prompt for the external-editor command and store it (QSettings). The built-in
    behind a DocView's ``⚙``."""
    from qtpy.QtCore import QSettings
    from qtpy.QtWidgets import QInputDialog
    s = QSettings("ferroDAC", "ferroDAC")
    cur = s.value("editor/command", "", type=str) or ""
    text, ok = QInputDialog.getText(
        parent, "External editor command",
        "Command to open a file (use {file} for the path; blank = OS default).\n"
        "e.g.   konsole -e nvim {file}",
        text=cur)
    if ok:
        s.setValue("editor/command", text.strip())


class DocBridge(QObject):
    """The Qt↔JS API (registered as ``bridge`` on the QWebChannel). Slice 1 just
    hands the renderer the current document's text and pushes a fresh copy on every
    (external) change. The JS calls ``ready()`` once loaded so there's no race."""

    docChanged = Signal(str, str)        # (relpath, text) — Qt → JS (load / external edit)
    docContext = Signal(str)             # (doc_dir) — Qt → JS, for resolving local images
    saveRequested = Signal(str)          # (text) — JS → Qt (autosave from the editor)
    jsReady = Signal()                   # the JS bridge handshake completed (page ready)

    # Collaboration (Phase 2 relay). doc_id is IMPLICIT per DocView (Qt owns it);
    # only opaque base64 payloads cross the bridge. Qt → JS:
    collabSeed = Signal(bool, str, str)  # (should_seed, text, actor) — start a collab session
    collabUpdate = Signal(str)           # (update_b64) — an incoming Yjs update
    collabAwareness = Signal(str)        # (state_b64) — an incoming awareness update
    collabPresence = Signal(str)         # (actors_json) — room membership
    collabStopped = Signal()             # Qt left the session → JS tears down Yjs
    collabRequestState = Signal()        # dump full Yjs state (to seed a new local view)
    # JS → Qt (re-emitted by the slots below; DocView forwards to the HubController):
    joinRequested = Signal()
    leaveRequested = Signal()
    updateRequested = Signal(str, bool)  # (update_b64, compaction)
    awarenessRequested = Signal(str)     # (state_b64)
    snapshotRequested = Signal(str)      # (text) — leader materialises the .md

    # Editor macros (/rec): list recordings + export one on demand. Qt → JS:
    recordingsAvailable = Signal(str)    # (json [{id, label, t0, t1}])
    recordingExports = Signal(str, str)  # (rec_id, json) — files ALREADY exported
    recordingExported = Signal(str, str)  # (rec_id, json) — files from a fresh export-now
    # JS → Qt (re-emitted by the slots below; DocView forwards to the app):
    recordingsRequested = Signal()
    exportsRequested = Signal(str)       # (rec_id) — list existing
    exportRequested = Signal(str)        # (rec_id) — render fresh (export now)

    # Editor macro (/proc): cite a used processor's source (open science). Qt → JS:
    processorsAvailable = Signal(str)    # (json [{kind, label}])
    processorSource = Signal(str, str, str)  # (kind, source, whitepaper_relpath)
    processorsRequested = Signal()       # JS → Qt
    procSourceRequested = Signal(str)    # (kind)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._relpath = ""
        self._text = ""
        self._docdir = ""

    def set_doc(self, relpath: str, text: str, docdir: str = "") -> None:
        self._relpath, self._text, self._docdir = relpath, text, docdir
        self.docContext.emit(docdir)
        self.docChanged.emit(relpath, text)

    @Slot()
    def ready(self) -> None:
        self.jsReady.emit()                               # JS bridge wired (collab-safe now)
        self.docContext.emit(self._docdir)                # (re)send to a just-loaded page
        self.docChanged.emit(self._relpath, self._text)

    @Slot(str)
    def save(self, text: str) -> None:
        self.saveRequested.emit(text)

    # -- editor macros: JS → Qt --------------------------------------------
    @Slot()
    def requestRecordings(self) -> None:
        self.recordingsRequested.emit()

    @Slot(str)
    def requestRecordingExports(self, rec_id: str) -> None:
        self.exportsRequested.emit(rec_id)        # list already-exported files

    @Slot(str)
    def requestRecordingExport(self, rec_id: str) -> None:
        self.exportRequested.emit(rec_id)         # render a fresh export now

    @Slot()
    def requestProcessors(self) -> None:
        self.processorsRequested.emit()

    @Slot(str)
    def requestProcessorSource(self, kind: str) -> None:
        self.procSourceRequested.emit(kind)

    # -- collaboration: JS → Qt (thin re-emit; DocView forwards to the hub) --
    @Slot()
    def collabJoin(self) -> None:
        self.joinRequested.emit()

    @Slot()
    def collabLeave(self) -> None:
        self.leaveRequested.emit()

    @Slot(str, bool)
    def collabSendUpdate(self, update_b64: str, compaction: bool = False) -> None:
        self.updateRequested.emit(update_b64, compaction)

    @Slot(str)
    def collabSendAwareness(self, state_b64: str) -> None:
        self.awarenessRequested.emit(state_b64)

    @Slot(str)
    def collabSendSnapshot(self, text: str) -> None:
        self.snapshotRequested.emit(text)


class DocView(QWidget):
    """Renders a project markdown file live; an external editor editing the raw file
    is just another writer the watcher catches."""

    def __init__(self, on_edit=None, on_configure=None, parent=None,
                 on_list_recordings=None, on_export_recording=None,
                 on_list_recording_exports=None, on_list_processors=None,
                 on_processor_source=None, on_saved=None):
        super().__init__(parent)
        self._path = None                # absolute path of the open doc
        self._dir = None                 # its folder (watched too — atomic-save safe)
        self._mtime_seen = None
        self._on_saved = on_saved        # called after an in-app save (debounced git commit)
        self._on_edit = on_edit          # optional override callable(path)
        self._on_configure = on_configure  # optional override callable()
        self._on_list_recordings = on_list_recordings   # () -> [{id,label,t0,t1}]
        self._on_export_recording = on_export_recording  # (rec_id) -> fresh [{name,abspath,kind}]
        self._on_list_recording_exports = on_list_recording_exports  # (rec_id) -> existing
        self._on_list_processors = on_list_processors   # () -> [{kind,label}]
        self._on_processor_source = on_processor_source  # (kind) -> source str
        self._collab = None              # a HubController, when a hub+doc is available
        self._doc_id = None              # "<project_id>::<relpath>" for collaboration
        self._collab_on = False          # currently in a live session?
        self._js_ready = False           # has the JS bridge handshake completed?
        self._pending_collab = False     # asked to collaborate before the JS was ready
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        head = QHBoxLayout()
        head.setContentsMargins(8, 6, 8, 6)
        self._title = QLabel("Document")
        self._title.setStyleSheet("font-weight:700; color:#c7d0db;")
        head.addWidget(self._title)
        head.addStretch(1)

        self._collab_btn = QPushButton("👥 Collaborate")
        self._collab_btn.setCheckable(True)
        self._collab_btn.setToolTip("Edit this document together, live, with others "
                                    "on the hub")
        self._collab_btn.setVisible(False)        # shown once a hub + doc is available
        self._collab_btn.toggled.connect(self._on_collab_toggled)
        head.addWidget(self._collab_btn)

        self._open_btn = QPushButton("📂 Open…")
        self._open_btn.setToolTip("Open another markdown file in this view")
        self._open_btn.clicked.connect(self._open_picker)
        head.addWidget(self._open_btn)

        self._win_btn = QPushButton("⤢ Window")
        self._win_btn.setToolTip("Open this document in its own window")
        self._win_btn.clicked.connect(self._pop_out)
        head.addWidget(self._win_btn)

        self._edit_btn = QPushButton("↗ Open externally")
        self._edit_btn.setToolTip("Open this file in your configured editor")
        self._edit_btn.clicked.connect(self._edit_external)
        head.addWidget(self._edit_btn)

        gear = QPushButton("⚙")
        gear.setToolTip("Set the external editor command")
        gear.clicked.connect(self._configure_external)
        head.addWidget(gear)
        root.addLayout(head)

        self.view = QWebEngineView()
        # The preview page is served from dist/ (a file:// URL); let it load the
        # doc's LOCAL images (relative srcs are rewritten to file:// in the JS). This
        # is usually the default, but set it explicitly so embedded plots/figures load.
        try:
            st = self.view.settings()
            st.setAttribute(
                QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)
        except Exception:                                # noqa: BLE001 — enum/binding variance
            pass
        self.bridge = DocBridge(self)
        self.bridge.saveRequested.connect(self._write)   # in-app edits → the file
        self.bridge.jsReady.connect(self._on_js_ready)    # gate collab on the JS handshake
        # Collaboration: the bridge↔hub wiring is STATIC (connected once); the doc_id
        # and controller are dynamic state, so switching docs never re-wires signals.
        self.bridge.updateRequested.connect(self._collab_send_update)
        self.bridge.awarenessRequested.connect(self._collab_send_awareness)
        self.bridge.snapshotRequested.connect(self._collab_send_snapshot)
        self.bridge.leaveRequested.connect(self._stop_collab)
        self.bridge.recordingsRequested.connect(self._push_recordings)
        self.bridge.exportsRequested.connect(self._list_recording_exports)
        self.bridge.exportRequested.connect(self._export_recording)
        self.bridge.processorsRequested.connect(self._push_processors)
        self.bridge.procSourceRequested.connect(self._send_processor_source)
        self._channel = QWebChannel(self)
        self._channel.registerObject("bridge", self.bridge)
        self.view.page().setWebChannel(self._channel)
        self.view.load(QUrl.fromLocalFile(os.path.join(_DIST, "index.html")))
        root.addWidget(self.view, 1)

        self._watcher = QFileSystemWatcher(self)
        self._watcher.fileChanged.connect(self._reload)
        # …and the FOLDER: editors that save atomically (nvim, vim, VS Code) replace
        # the file's inode, which silently kills a file-only watch — a directory
        # watch still fires on the rename, so live reload survives any editor.
        self._watcher.directoryChanged.connect(self._on_dir_changed)

    @staticmethod
    def _file_mtime(path):
        try:
            return os.path.getmtime(path) if path else None
        except OSError:
            return None

    def open(self, path: str) -> None:
        self.set_collab_target(None, None)        # a new file → drop any collab target
        for w in self._watcher.files() + self._watcher.directories():
            self._watcher.removePath(w)
        self._path = os.path.abspath(path)
        self._dir = os.path.dirname(self._path)
        self._title.setText(os.path.basename(self._path))
        if os.path.exists(self._path):
            self._watcher.addPath(self._path)
        if os.path.isdir(self._dir):
            self._watcher.addPath(self._dir)
        self._reload()

    def _on_dir_changed(self, _dir) -> None:
        if self._file_mtime(self._path) != self._mtime_seen:   # OUR file changed
            self._reload()

    def _reload(self, _path=None) -> None:
        text = ""
        if self._path:
            try:
                with open(self._path, encoding="utf-8") as fh:
                    text = fh.read()
            except Exception:            # noqa: BLE001 — unreadable/just-removed
                pass
        self._mtime_seen = self._file_mtime(self._path)
        self.bridge.set_doc(os.path.basename(self._path or ""), text, self._dir or "")
        # re-arm watches — an atomic save drops the file watch (the dir watch persists)
        if (self._path and os.path.exists(self._path)
                and self._path not in self._watcher.files()):
            self._watcher.addPath(self._path)
        if (self._dir and os.path.isdir(self._dir)
                and self._dir not in self._watcher.directories()):
            self._watcher.addPath(self._dir)

    def _write(self, text: str) -> None:
        """Persist the in-app editor's text (the file stays truth). Records our own
        mtime so the watcher doesn't treat our save as an external change — and the
        JS ignores the echoed text anyway (it equals what it just sent)."""
        if not self._path:
            return
        try:
            tmp = self._path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(tmp, self._path)          # atomic
            self._mtime_seen = self._file_mtime(self._path)
            if self._path not in self._watcher.files():
                self._watcher.addPath(self._path)
            if (self._dir and os.path.isdir(self._dir)
                    and self._dir not in self._watcher.directories()):
                self._watcher.addPath(self._dir)
            if self._on_saved is not None:        # e.g. schedule a debounced git commit
                try:
                    self._on_saved()
                except Exception:                # noqa: BLE001
                    pass
        except Exception:                        # noqa: BLE001
            pass

    def _open_picker(self) -> None:
        start = self._dir or os.getcwd()
        path, _ = QFileDialog.getOpenFileName(
            self, "Open document", start,
            "Markdown (*.md *.markdown *.txt);;All files (*)")
        if path:
            self.open(path)

    def _pop_out(self) -> None:
        """Open this document in its OWN top-level window — a standalone DocView on
        the same file. The file stays truth, so the two views reconcile through it
        (edit in one → the watcher re-renders the other)."""
        if not self._path:
            return
        win = DocView(on_edit=self._on_edit, on_configure=self._on_configure,
                      parent=self.window(),
                      on_list_recordings=self._on_list_recordings,
                      on_export_recording=self._on_export_recording,
                      on_list_recording_exports=self._on_list_recording_exports,
                      on_list_processors=self._on_list_processors,
                      on_processor_source=self._on_processor_source,
                      on_saved=self._on_saved)
        win.setWindowFlag(Qt.Window, True)              # owned, but its own OS window
        win.setAttribute(Qt.WA_DeleteOnClose, True)
        win.setWindowTitle(f"{os.path.basename(self._path)} — ferroDAC")
        win.resize(860, 640)
        win.open(self._path)
        # If this view is collaborating, the pop-out joins the SAME live session (as a
        # local peer) instead of being a solo view that just fights over the file.
        if self._collab is not None and self._doc_id:
            win.set_collab_target(self._collab, self._doc_id)
            if self._collab_on:
                win.start_collab()
        win.show()
        win.raise_()

    def closeEvent(self, event):  # noqa: N802
        self._stop_collab()       # a closed pop-out leaves the room (locally; hub iff last)
        super().closeEvent(event)

    def _edit_external(self) -> None:
        if not self._path:
            return
        if self._on_edit is not None:                   # host override (status msgs)
            self._on_edit(self._path)
        else:
            launch_external_editor(self._path)          # built-in default

    def _configure_external(self) -> None:
        if self._on_configure is not None:
            self._on_configure()
        else:
            configure_editor_command(self)

    # -- collaboration -------------------------------------------------------
    def set_collab_target(self, controller, doc_id) -> None:
        """Declare which hub + doc_id this view COULD collaborate on (or (None, None)
        when none). Shows/hides the Collaborate toggle; leaves any live session whose
        target just went away (disconnected, switched to a non-hub doc, …)."""
        if self._collab_on and (controller is not self._collab or doc_id != self._doc_id):
            self._stop_collab()
        self._collab = controller
        self._doc_id = doc_id
        available = controller is not None and bool(doc_id)
        self._collab_btn.setVisible(available)
        if not available and self._collab_on:
            self._stop_collab()

    def _on_collab_toggled(self, checked: bool) -> None:
        if checked:
            self.start_collab()
        else:
            self._stop_collab()

    def _on_js_ready(self) -> None:
        self._js_ready = True
        self._push_recordings()               # seed the /rec macro's cache
        self._push_processors()               # …and /proc's
        if self._pending_collab:              # a pop-out asked to collab before JS was up
            self._pending_collab = False
            self._start_collab()

    # -- editor macros (/rec): list recordings + export one on demand --------
    def set_macros(self, on_list_recordings, on_export_recording,
                   on_list_recording_exports=None, on_list_processors=None,
                   on_processor_source=None) -> None:
        """Wire the editor macros to the app's services (used by doc panels created via
        the Add menu or a layout, which can't get the callbacks at construction)."""
        self._on_list_recordings = on_list_recordings
        self._on_export_recording = on_export_recording
        self._on_list_recording_exports = on_list_recording_exports
        self._on_list_processors = on_list_processors
        self._on_processor_source = on_processor_source
        if self._js_ready:                    # warm the caches if the page is already up
            self._push_recordings()
            self._push_processors()

    def _push_recordings(self) -> None:
        if self._on_list_recordings is None:
            return
        import json
        try:
            recs = self._on_list_recordings() or []
        except Exception:                     # noqa: BLE001
            recs = []
        self.bridge.recordingsAvailable.emit(json.dumps(recs))

    def _push_processors(self) -> None:
        if self._on_list_processors is None:
            return
        import json
        try:
            procs = self._on_list_processors() or []
        except Exception:                     # noqa: BLE001
            procs = []
        self.bridge.processorsAvailable.emit(json.dumps(procs))

    def _send_processor_source(self, kind: str) -> None:
        src, paper = "", None
        if self._on_processor_source is not None:
            try:
                res = self._on_processor_source(kind)
            except Exception:                 # noqa: BLE001
                res = None
            if isinstance(res, dict):         # {source, whitepaper-abspath}
                src, paper = res.get("source") or "", res.get("whitepaper")
            else:
                src = res or ""
        rel = ""
        if paper:                             # path relative to the open doc (portable)
            try:
                rel = os.path.relpath(paper, self._dir or os.getcwd()).replace(os.sep, "/")
            except Exception:                 # noqa: BLE001
                rel = ""
        self.bridge.processorSource.emit(kind, src, rel)

    def _emit_files(self, signal, rec_id: str, raw) -> None:
        """Hand the JS a recording's files with paths RELATIVE to the open doc
        (portable in-repo links)."""
        import json
        files = []
        base = self._dir or os.getcwd()
        for f in (raw or []):
            ap = f.get("abspath")
            if not ap:
                continue
            try:
                rel = os.path.relpath(ap, base)
            except Exception:                 # noqa: BLE001 — e.g. different drive (Windows)
                rel = ap
            files.append({"name": f.get("name") or os.path.basename(ap),
                          "relpath": rel.replace(os.sep, "/"),
                          "kind": f.get("kind", "plot")})
        signal.emit(rec_id, json.dumps(files))

    def _list_recording_exports(self, rec_id: str) -> None:
        """The recording's ALREADY-exported files (what the macro lists first)."""
        raw = []
        if self._on_list_recording_exports is not None:
            try:
                raw = self._on_list_recording_exports(rec_id) or []
            except Exception:                 # noqa: BLE001
                raw = []
        self._emit_files(self.bridge.recordingExports, rec_id, raw)

    def _export_recording(self, rec_id: str) -> None:
        """Render a FRESH export now (the macro's Export-now option)."""
        raw = []
        if self._on_export_recording is not None:
            try:
                raw = self._on_export_recording(rec_id) or []
            except Exception:                 # noqa: BLE001
                raw = []
        self._emit_files(self.bridge.recordingExported, rec_id, raw)

    def start_collab(self) -> None:
        """Join the collab session — DEFERRING until the JS bridge handshake completes
        (a freshly popped-out window's page is still loading, and a collabSeed emitted
        before its JS wires up would be lost)."""
        if self._collab is None or not self._doc_id:
            return
        if self._js_ready:
            self._start_collab()
        else:
            self._pending_collab = True

    def _start_collab(self) -> None:
        if self._collab_on or self._collab is None or not self._doc_id:
            return
        self._collab_on = True
        # doc_open joins the hub room (first local view) OR seeds this view from an
        # existing local peer (a 2nd view, e.g. a popped-out window of the same doc).
        self._collab.doc_open(self._doc_id, self.bridge)
        self._collab_btn.setChecked(True)
        self._collab_btn.setText("👥 Collaborating")

    def _stop_collab(self) -> None:
        if self._collab_on and self._collab is not None and self._doc_id:
            self._collab.doc_close(self._doc_id, self.bridge)   # leaves the hub iff last
            self.bridge.collabStopped.emit()       # JS tears down Yjs → back to solo
        self._collab_on = False
        self._collab_btn.setChecked(False)
        self._collab_btn.setText("👥 Collaborate")

    def _collab_send_update(self, update_b64: str, compaction: bool) -> None:
        if self._collab_on and self._collab is not None and self._doc_id:
            self._collab.doc_send_update(self._doc_id, update_b64, compaction,
                                         sender=self.bridge)

    def _collab_send_awareness(self, state_b64: str) -> None:
        if self._collab_on and self._collab is not None and self._doc_id:
            self._collab.doc_send_awareness(self._doc_id, state_b64, sender=self.bridge)

    def _collab_send_snapshot(self, text: str) -> None:
        if self._collab_on and self._collab is not None and self._doc_id:
            self._collab.doc_send_snapshot(self._doc_id, text)
