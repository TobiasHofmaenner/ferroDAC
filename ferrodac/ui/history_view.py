"""Project history — a read-only view of the project's git commits (DESIGN §8.2)."""
import time

from qtpy.QtWidgets import (QDialog, QDialogButtonBox, QHBoxLayout, QInputDialog,
                            QLabel, QListWidget, QPushButton, QVBoxLayout)


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
        self.resize(580, 440)
        root = QVBoxLayout(self)
        intro = QLabel("Version history of this project's files — reports, layouts, docs, "
                       "exported CSVs, papers. Measurements live in the data store, not here.")
        intro.setWordWrap(True)
        intro.setStyleSheet("color:#8b95a4; font-size:11px;")
        root.addWidget(intro)

        self._list = QListWidget()
        self._list.setStyleSheet("font-family: monospace; font-size: 12px;")
        root.addWidget(self._list, 1)

        row = QHBoxLayout()
        cp = QPushButton("Checkpoint now…")
        cp.clicked.connect(self._checkpoint)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh)
        row.addWidget(cp)
        row.addStretch(1)
        row.addWidget(refresh)
        root.addLayout(row)

        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.reject)
        bb.accepted.connect(self.accept)
        root.addWidget(bb)
        self.refresh()

    def refresh(self):
        self._list.clear()
        hist = self.repo.log(200)
        if not hist:
            self._list.addItem("No history yet — commits appear after recordings, named "
                               "layouts, settled doc edits, or a manual checkpoint.")
            return
        for h in hist:
            self._list.addItem(f"{h['sha'][:8]}   {_ago(h['time']):>9}   {h['message']}")

    def _checkpoint(self):
        msg, ok = QInputDialog.getText(self, "Checkpoint", "Describe this checkpoint:",
                                       text="Checkpoint")
        if ok:
            self.repo.commit(msg.strip() or "Checkpoint")
            self.refresh()
