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
    saveRequested = Signal(str)          # (text) — JS → Qt (autosave from the editor)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._relpath = ""
        self._text = ""

    def set_doc(self, relpath: str, text: str) -> None:
        self._relpath, self._text = relpath, text
        self.docChanged.emit(relpath, text)

    @Slot()
    def ready(self) -> None:
        self.docChanged.emit(self._relpath, self._text)   # (re)send to a just-loaded page

    @Slot(str)
    def save(self, text: str) -> None:
        self.saveRequested.emit(text)


class DocView(QWidget):
    """Renders a project markdown file live; an external editor editing the raw file
    is just another writer the watcher catches."""

    def __init__(self, on_edit=None, on_configure=None, parent=None):
        super().__init__(parent)
        self._path = None                # absolute path of the open doc
        self._dir = None                 # its folder (watched too — atomic-save safe)
        self._mtime_seen = None
        self._on_edit = on_edit          # optional override callable(path)
        self._on_configure = on_configure  # optional override callable()
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        head = QHBoxLayout()
        head.setContentsMargins(8, 6, 8, 6)
        self._title = QLabel("Document")
        self._title.setStyleSheet("font-weight:700; color:#c7d0db;")
        head.addWidget(self._title)
        head.addStretch(1)

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
        self.bridge = DocBridge(self)
        self.bridge.saveRequested.connect(self._write)   # in-app edits → the file
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
        self.bridge.set_doc(os.path.basename(self._path or ""), text)
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
                      parent=self.window())
        win.setWindowFlag(Qt.Window, True)              # owned, but its own OS window
        win.setAttribute(Qt.WA_DeleteOnClose, True)
        win.setWindowTitle(f"{os.path.basename(self._path)} — ferroDAC")
        win.resize(860, 640)
        win.open(self._path)
        win.show()
        win.raise_()

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
