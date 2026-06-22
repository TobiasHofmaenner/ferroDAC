"""The Extensions manager — see installed add-ons and add new ones from a git repo.

Adding a repo clones it, shows a trust gate listing exactly what it provides (so you
can review before trusting), then installs + loads it. Extensions are trusted code you
opt into: the gate is the safeguard, transparency (source + white papers) is the rest.
"""
import os

from qtpy.QtCore import Qt, QUrl
from qtpy.QtGui import QDesktopServices
from qtpy.QtWidgets import (QDialog, QDialogButtonBox, QHBoxLayout, QInputDialog,
                            QLabel, QListWidget, QListWidgetItem, QMessageBox,
                            QPlainTextEdit, QPushButton, QVBoxLayout)

from ..extensions import discover_extensions
from ..extensions.manager import entry_file


class _SourceViewer(QDialog):
    """Read-only view of a provider's source — 'see exactly what is happening'."""

    def __init__(self, title, text, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Source · {title}")
        self.resize(720, 560)
        lay = QVBoxLayout(self)
        view = QPlainTextEdit()
        view.setReadOnly(True)
        view.setPlainText(text or "(no source available)")
        view.setStyleSheet("font-family: monospace; font-size: 12px;")
        lay.addWidget(view)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.reject)
        bb.accepted.connect(self.accept)
        lay.addWidget(bb)


class ExtensionsDialog(QDialog):
    def __init__(self, manager, parent=None):
        super().__init__(parent)
        self.mgr = manager
        self.setWindowTitle("Extensions")
        self.resize(580, 440)
        root = QVBoxLayout(self)

        intro = QLabel("Extensions add drivers, processors and widgets. They run code on "
                       "your machine — only add repos you trust. Every extension ships its "
                       "source and, where it matters, a white paper.")
        intro.setWordWrap(True)
        intro.setStyleSheet("color:#8b95a4;")
        root.addWidget(intro)

        self._list = QListWidget()
        self._list.currentItemChanged.connect(self._on_record_selected)
        root.addWidget(self._list, 2)

        row = QHBoxLayout()
        official = QPushButton("Browse official…")
        official.setToolTip("Add from the official ferroDAC extensions repo")
        official.clicked.connect(self._add_official)
        add = QPushButton("Add from git…")
        add.clicked.connect(lambda: self._add())
        row.addWidget(official)
        self._toggle_btn = QPushButton("Enable / disable")
        self._toggle_btn.clicked.connect(self._toggle)
        rm = QPushButton("Remove")
        rm.clicked.connect(self._remove)
        row.addWidget(add)
        row.addStretch(1)
        row.addWidget(self._toggle_btn)
        row.addWidget(rm)
        root.addLayout(row)

        prov_label = QLabel("Provides")
        prov_label.setStyleSheet("color:#c7d0db; font-weight:600; margin-top:4px;")
        root.addWidget(prov_label)
        self._providers = QListWidget()
        root.addWidget(self._providers, 1)
        prow = QHBoxLayout()
        self._src_btn = QPushButton("Show source")
        self._src_btn.clicked.connect(self._show_source)
        self._wp_btn = QPushButton("Show white paper")
        self._wp_btn.clicked.connect(self._show_whitepaper)
        prow.addStretch(1)
        prow.addWidget(self._src_btn)
        prow.addWidget(self._wp_btn)
        root.addLayout(prow)

        note = QLabel("Processors are available immediately; new widgets and drivers apply "
                      "after a restart.")
        note.setWordWrap(True)
        note.setStyleSheet("color:#6b7686; font-size:11px;")
        root.addWidget(note)

        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.reject)
        bb.accepted.connect(self.accept)
        root.addWidget(bb)
        self._refresh()

    # -- list ----------------------------------------------------------------
    def _refresh(self):
        self._list.clear()
        for r in self.mgr.records():
            src = r.get("source", "")
            commit = (r.get("commit") or "")[:8]
            on = r.get("enabled", True)
            names = r.get("names")
            label = f"{'●' if on else '○'}  {src}"
            if commit:
                label += f"   @{commit}"
            if names:
                label += f"   [{', '.join(names)}]"
            if not on:
                label += "   (disabled)"
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, src)
            self._list.addItem(item)

    def _selected(self):
        item = self._list.currentItem()
        return item.data(Qt.UserRole) if item else None

    def _on_record_selected(self, *_):
        """Show the providers of the selected extension (so you can inspect them)."""
        self._providers.clear()
        src = self._selected()
        rec = next((r for r in self.mgr.records() if r.get("source") == src), None)
        if not rec:
            return
        d = rec.get("clone") or rec.get("source")
        if not d or not os.path.exists(d):
            return
        for mf in discover_extensions(d):
            for p in mf.providers:
                paper = mf.whitepaper_path(p)
                item = QListWidgetItem(f"{p.role}: {p.entry}{'  📄' if paper else ''}")
                item.setData(Qt.UserRole, (mf.root, p.entry, paper))
                self._providers.addItem(item)

    def _show_source(self):
        item = self._providers.currentItem()
        if item is None:
            return
        root_dir, entry, _ = item.data(Qt.UserRole)
        path = entry_file(root_dir, entry)
        text = ""
        if path:
            try:
                with open(path, encoding="utf-8") as fh:
                    text = fh.read()
            except OSError as exc:
                text = f"(could not read {path}: {exc})"
        _SourceViewer(entry, text, self).exec()

    def _show_whitepaper(self):
        item = self._providers.currentItem()
        if item is None:
            return
        _root, _entry, paper = item.data(Qt.UserRole)
        if not paper or not os.path.exists(paper):
            QMessageBox.information(self, "No white paper",
                                    "This provider doesn't ship a white paper.")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(paper))

    # -- actions -------------------------------------------------------------
    def _add_official(self):
        from ..extensions import OFFICIAL_EXTENSIONS_URL
        self._add(OFFICIAL_EXTENSIONS_URL)

    def _add(self, url=None):
        if not url:
            url, ok = QInputDialog.getText(self, "Add extension",
                                           "Git repo URL (or a local path):")
            if not ok or not url.strip():
                return
        url = url.strip()
        ref, ok2 = QInputDialog.getText(
            self, "Pin (recommended)", "Commit / tag / branch (blank = default branch):")
        ref = (ref.strip() or None) if ok2 else None
        try:
            _dest, sha, manifests = self.mgr.prepare(url, ref)
        except Exception as exc:                       # noqa: BLE001
            QMessageBox.warning(self, "Clone failed", str(exc))
            return
        if not manifests:
            QMessageBox.warning(self, "Nothing to install",
                                "No ferrodac-extension.toml found in that repo.")
            return
        if not self._trust_gate(url, sha, manifests):
            return
        try:
            self.mgr.install_url(url, ref, names=[m.name for m in manifests], enabled=True)
        except Exception as exc:                       # noqa: BLE001
            QMessageBox.warning(self, "Install failed", str(exc))
            return
        self._refresh()
        QMessageBox.information(
            self, "Installed",
            "Extension added. Processors are available now; restart to use any new "
            "widgets or drivers it provides.")

    def _trust_gate(self, url, sha, manifests) -> bool:
        lines = [url, f"commit {sha[:12]}", ""]
        for m in manifests:
            tag = "" if m.is_compatible() else "  — INCOMPATIBLE api"
            lines.append(f"{m.name}  (api {m.api}){tag}")
            for p in m.providers:
                paper = "  📄 white paper" if p.whitepaper else ""
                lines.append(f"    • {p.role}: {p.entry}{paper}")
            if not m.providers:
                lines.append("    (declares no providers)")
            lines.append("")
        lines.append("This will run the extension's code on your machine. Only continue "
                     "if you trust the source.")
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("Review before installing")
        box.setText("\n".join(lines))
        box.setStandardButtons(QMessageBox.Cancel | QMessageBox.Ok)
        box.button(QMessageBox.Ok).setText("Install")
        box.setDefaultButton(QMessageBox.Cancel)
        return box.exec() == QMessageBox.Ok

    def _toggle(self):
        src = self._selected()
        if not src:
            return
        rec = next((r for r in self.mgr.records() if r.get("source") == src), None)
        if rec is not None:
            self.mgr.set_enabled(src, not rec.get("enabled", True))
            self._refresh()

    def _remove(self):
        src = self._selected()
        if not src:
            return
        if QMessageBox.question(self, "Remove extension",
                                f"Remove {src}?\n(The installed record is dropped; restart "
                                "to fully unload it.)") == QMessageBox.Yes:
            self.mgr.remove(src)
            self._refresh()
