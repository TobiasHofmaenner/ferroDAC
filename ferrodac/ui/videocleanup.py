"""VideoCleanupDialog — manual ambient-video storage management (DESIGN §9.3).

The deliberate alternative to silent retention: shows each camera's stored
segment span + size, total usage, and free disk, and lets the user delete
segments older than a chosen age. Materialized clips live in projects and are
never touched here.
"""

from __future__ import annotations

import time

from qtpy.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QHBoxLayout,
                            QLabel, QPushButton, QTableWidget, QTableWidgetItem,
                            QVBoxLayout)


def _fmt_gb(n_bytes: float) -> str:
    return f"{n_bytes / 1e9:.2f} GB" if n_bytes >= 1e8 else f"{n_bytes / 1e6:.0f} MB"


class VideoCleanupDialog(QDialog):
    _AGES = [("Everything", 0.0), ("Older than 1 hour", 3600.0),
             ("Older than 6 hours", 6 * 3600.0), ("Older than 1 day", 86400.0),
             ("Older than 3 days", 3 * 86400.0), ("Older than 1 week", 7 * 86400.0)]

    def __init__(self, store, names: dict, parent=None):
        super().__init__(parent)
        self._store = store
        self._names = names or {}
        self.setWindowTitle("Manage video storage")
        self.resize(560, 380)
        lay = QVBoxLayout(self)

        self._summary = QLabel()
        lay.addWidget(self._summary)

        self._table = QTableWidget(0, 3, self)
        self._table.setHorizontalHeaderLabels(["Camera", "Stored span", "Size"])
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.setColumnWidth(0, 240)
        self._table.setColumnWidth(1, 200)
        self._table.setSelectionBehavior(self._table.SelectionBehavior.SelectRows)
        lay.addWidget(self._table, 1)

        row = QHBoxLayout()
        row.addWidget(QLabel("Delete:"))
        self._age = QComboBox()
        for label, _s in self._AGES:
            self._age.addItem(label)
        self._age.setCurrentIndex(3)                 # default: older than 1 day
        row.addWidget(self._age, 1)
        self._del = QPushButton("Delete for selected camera")
        self._del.clicked.connect(self._delete)
        row.addWidget(self._del)
        lay.addLayout(row)

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.reject)
        bb.accepted.connect(self.accept)
        lay.addWidget(bb)
        self._reload()

    def _label(self, cam_uuid: str) -> str:
        return self._names.get(f"{cam_uuid}/frame") or cam_uuid

    def _reload(self) -> None:
        cams = self._store.cameras()
        self._table.setRowCount(len(cams))
        for r, cam in enumerate(cams):
            cov = self._store.coverage(cam)
            span = "—"
            if cov:
                dur = sum(b - a for a, b in cov)
                span = (f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(cov[0][0]))}"
                        f" · {dur / 3600:.1f} h")
            it = QTableWidgetItem(self._label(cam))
            it.setData(0x0100, cam)                  # Qt.UserRole
            self._table.setItem(r, 0, it)
            self._table.setItem(r, 1, QTableWidgetItem(span))
            self._table.setItem(r, 2, QTableWidgetItem(_fmt_gb(self._store.usage(cam))))
        if cams:
            self._table.selectRow(0)
        self._summary.setText(
            f"Ambient video: {_fmt_gb(self._store.usage())} across "
            f"{len(cams)} camera(s) · {self._store.free_gb():.1f} GB free on disk")

    def _delete(self) -> None:
        items = self._table.selectedItems()
        if not items:
            return
        cam = self._table.item(items[0].row(), 0).data(0x0100)
        window = self._AGES[self._age.currentIndex()][1]
        cutoff = time.time() - window if window else time.time() + 1  # 0 = all
        freed = self._store.delete_older_than(cam, cutoff)
        self._reload()
        self._summary.setText(self._summary.text() + f"  —  freed {_fmt_gb(freed)}")
