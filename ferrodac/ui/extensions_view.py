"""The Extensions manager — see installed add-ons and add new ones from a git repo.

Adding a repo clones it, shows a trust gate listing exactly what it provides (so you
can review before trusting), then installs + loads it. Extensions are trusted code you
opt into: the gate is the safeguard, transparency (source + white papers) is the rest.
"""
from qtpy.QtCore import Qt
from qtpy.QtWidgets import (QDialog, QDialogButtonBox, QHBoxLayout, QInputDialog,
                            QLabel, QListWidget, QListWidgetItem, QMessageBox,
                            QPushButton, QVBoxLayout)


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
        root.addWidget(self._list, 1)

        row = QHBoxLayout()
        add = QPushButton("Add from git…")
        add.clicked.connect(self._add)
        self._toggle_btn = QPushButton("Enable / disable")
        self._toggle_btn.clicked.connect(self._toggle)
        rm = QPushButton("Remove")
        rm.clicked.connect(self._remove)
        row.addWidget(add)
        row.addStretch(1)
        row.addWidget(self._toggle_btn)
        row.addWidget(rm)
        root.addLayout(row)

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

    # -- actions -------------------------------------------------------------
    def _add(self):
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
