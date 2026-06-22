"""Project history + sync — the project's git commits and push/pull (DESIGN §8.2)."""
import time

from qtpy.QtCore import Qt
from qtpy.QtWidgets import (QApplication, QDialog, QDialogButtonBox, QHBoxLayout,
                            QInputDialog, QLabel, QListWidget, QPushButton,
                            QVBoxLayout)


def _ago(ts: int) -> str:
    if not ts:
        return ""
    d = max(0, int(time.time() - ts))
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if d >= n:
            return f"{d // n}{unit} ago"
    return "just now"


class HistoryDialog(QDialog):
    def __init__(self, repo, project_name, parent=None):
        super().__init__(parent)
        self.repo = repo
        self.setWindowTitle(f"History · {project_name}")
        self.resize(620, 480)
        root = QVBoxLayout(self)
        intro = QLabel("Version history of this project's files — reports, layouts, docs, "
                       "exported CSVs, papers. Measurements live in the data store, not here.")
        intro.setWordWrap(True)
        intro.setStyleSheet("color:#8b95a4; font-size:11px;")
        root.addWidget(intro)

        # remote: push/pull to any git URL (your GitHub/GitLab — auth via your git setup)
        rrow = QHBoxLayout()
        self._remote_lbl = QLabel("")
        self._remote_lbl.setStyleSheet("font-size:11px; color:#c7d0db;")
        self._remote_lbl.setWordWrap(True)
        rrow.addWidget(self._remote_lbl, 1)
        setb = QPushButton("Set remote…")
        setb.clicked.connect(self._set_remote)
        pullb = QPushButton("Pull")
        pullb.clicked.connect(self._pull)
        pushb = QPushButton("Push")
        pushb.clicked.connect(self._push)
        for b in (setb, pullb, pushb):
            rrow.addWidget(b)
        root.addLayout(rrow)

        self._list = QListWidget()
        self._list.setStyleSheet("font-family: monospace; font-size: 12px;")
        root.addWidget(self._list, 1)

        self._result = QLabel("")
        self._result.setWordWrap(True)
        self._result.setStyleSheet("font-size:11px; color:#8b95a4;")
        root.addWidget(self._result)

        brow = QHBoxLayout()
        cp = QPushButton("Checkpoint now…")
        cp.clicked.connect(self._checkpoint)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh)
        brow.addWidget(cp)
        brow.addStretch(1)
        brow.addWidget(refresh)
        root.addLayout(brow)

        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.reject)
        bb.accepted.connect(self.accept)
        root.addWidget(bb)
        self.refresh()

    def refresh(self):
        url = self.repo.remote_url()
        self._remote_lbl.setText(f"Remote: {url}" if url else "Remote: none set")
        self._list.clear()
        hist = self.repo.log(200)
        if not hist:
            self._list.addItem("No history yet — commits appear after recordings, named "
                               "layouts, settled doc edits, or a manual checkpoint.")
            return
        for h in hist:
            self._list.addItem(f"{h['sha'][:8]}   {_ago(h['time']):>9}   {h['message']}")

    def _set_remote(self):
        url, ok = QInputDialog.getText(
            self, "Set remote",
            "Git URL — HTTPS (with a token) or SSH.\nCredentials use your own git setup; "
            "no secrets are stored here.", text=self.repo.remote_url())
        if ok:
            self.repo.set_remote(url.strip())
            self.refresh()

    def _push(self):
        self._sync(self.repo.push, "Pushing…")

    def _pull(self):
        self._sync(self.repo.pull, "Pulling…")

    def _sync(self, op, busy):
        self._result.setText(busy)
        QApplication.setOverrideCursor(Qt.WaitCursor)
        QApplication.processEvents()
        try:
            ok, msg = op()
        finally:
            QApplication.restoreOverrideCursor()
        self._result.setText(("✔ " if ok else "✕ ") + (msg or ""))
        self.refresh()

    def _checkpoint(self):
        msg, ok = QInputDialog.getText(self, "Checkpoint", "Describe this checkpoint:",
                                       text="Checkpoint")
        if ok:
            self.repo.commit(msg.strip() or "Checkpoint")
            self.refresh()
