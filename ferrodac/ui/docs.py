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

from qtpy.QtCore import QFileSystemWatcher, QObject, QUrl, Signal, Slot
from qtpy.QtWebChannel import QWebChannel
from qtpy.QtWebEngineWidgets import QWebEngineView
from qtpy.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

_DIST = os.path.join(os.path.dirname(__file__), "web", "dist")


class DocBridge(QObject):
    """The Qt↔JS API (registered as ``bridge`` on the QWebChannel). Slice 1 just
    hands the renderer the current document's text and pushes a fresh copy on every
    (external) change. The JS calls ``ready()`` once loaded so there's no race."""

    docChanged = Signal(str, str)        # (relpath, text)

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


class DocView(QWidget):
    """Renders a project markdown file live; an external editor editing the raw file
    is just another writer the watcher catches."""

    def __init__(self, on_edit=None, parent=None):
        super().__init__(parent)
        self._path = None                # absolute path of the open doc
        self._on_edit = on_edit          # callable(path) — launch the user's editor
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        head = QHBoxLayout()
        head.setContentsMargins(8, 6, 8, 6)
        self._title = QLabel("Document")
        self._title.setStyleSheet("font-weight:700; color:#c7d0db;")
        head.addWidget(self._title)
        head.addStretch(1)
        if on_edit is not None:
            self._edit_btn = QPushButton("✎ Edit in editor")
            self._edit_btn.setToolTip("Open this file in your external editor")
            self._edit_btn.clicked.connect(self._edit_external)
            head.addWidget(self._edit_btn)
        root.addLayout(head)

        self.view = QWebEngineView()
        self.bridge = DocBridge(self)
        self._channel = QWebChannel(self)
        self._channel.registerObject("bridge", self.bridge)
        self.view.page().setWebChannel(self._channel)
        self.view.load(QUrl.fromLocalFile(os.path.join(_DIST, "index.html")))
        root.addWidget(self.view, 1)

        self._watcher = QFileSystemWatcher(self)
        self._watcher.fileChanged.connect(self._reload)

    def open(self, path: str) -> None:
        if self._path and self._path in self._watcher.files():
            self._watcher.removePath(self._path)
        self._path = os.path.abspath(path)
        self._title.setText(os.path.basename(self._path))
        if os.path.exists(self._path):
            self._watcher.addPath(self._path)
        self._reload()

    def _reload(self, _path=None) -> None:
        text = ""
        if self._path:
            try:
                with open(self._path, encoding="utf-8") as fh:
                    text = fh.read()
            except Exception:            # noqa: BLE001 — unreadable/just-removed
                pass
        self.bridge.set_doc(os.path.basename(self._path or ""), text)
        # An atomic save (write tmp + rename) makes QFileSystemWatcher drop the path —
        # re-add it so we keep catching external edits.
        if (self._path and os.path.exists(self._path)
                and self._path not in self._watcher.files()):
            self._watcher.addPath(self._path)

    def _edit_external(self) -> None:
        if self._path and self._on_edit is not None:
            self._on_edit(self._path)
