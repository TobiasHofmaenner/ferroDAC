"""In-app Benchmark dialog — run the real paths in the LIVE process for faithful
numbers (DESIGN §21). The Qt-free data-plane suite (ferrodac.bench) runs on a
worker thread so the app stays responsive; a GUI curve-render measurement runs on
the paint thread (that IS the thing being measured). Results stream into a table
and can be saved to JSON for before/after comparison.
"""

from __future__ import annotations

import json
import time

import numpy as np
import pyqtgraph as pg
from qtpy import QtCore, QtWidgets


class _BenchWorker(QtCore.QObject):
    row = QtCore.Signal(dict)
    finished = QtCore.Signal()

    def __init__(self, scalar_sizes, trace_sizes, rounds):
        super().__init__()
        self._ss, self._ts, self._rounds = scalar_sizes, trace_sizes, rounds
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    @QtCore.Slot()
    def run(self) -> None:
        try:
            from ..bench import run_all
            run_all(self._ss, self._ts, self._rounds,
                    on_progress=lambda k, r: self.row.emit(r),
                    cancel=lambda: self._cancel)
        finally:
            self.finished.emit()


class BenchmarkDialog(QtWidgets.QDialog):
    _COLS = ("Path", "Size", "Median (ms)", "Rate")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Benchmark — data-plane + render paths")
        self.resize(720, 460)
        self._rows: list = []
        self._thread = None
        self._worker = None

        lay = QtWidgets.QVBoxLayout(self)
        note = QtWidgets.QLabel(
            "Times the REAL paths at controlled load sizes (synthetic data in a "
            "scratch store; the app's own data is untouched). Numbers are the "
            "median of several rounds; lower ms is better.")
        note.setWordWrap(True)
        note.setStyleSheet("color:#7f8a99;")
        lay.addWidget(note)

        self.table = QtWidgets.QTableWidget(0, len(self._COLS))
        self.table.setHorizontalHeaderLabels(self._COLS)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setColumnWidth(0, 300)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        lay.addWidget(self.table, 1)

        self._status = QtWidgets.QLabel("Ready.")
        self._status.setStyleSheet("color:#7f8a99;")
        lay.addWidget(self._status)

        row = QtWidgets.QHBoxLayout()
        self._run_btn = QtWidgets.QPushButton("▶ Run benchmark")
        self._run_btn.clicked.connect(self._run)
        row.addWidget(self._run_btn)
        self._quick = QtWidgets.QCheckBox("Quick (skip 1 M)")
        self._quick.setChecked(True)
        row.addWidget(self._quick)
        row.addStretch(1)
        self._save_btn = QtWidgets.QPushButton("Save JSON…")
        self._save_btn.clicked.connect(self._save)
        self._save_btn.setEnabled(False)
        row.addWidget(self._save_btn)
        lay.addLayout(row)

    # -- run -----------------------------------------------------------------
    def _run(self) -> None:
        if self._thread is not None:
            return
        self.table.setRowCount(0)
        self._rows = []
        self._save_btn.setEnabled(False)
        self._run_btn.setEnabled(False)
        scalar = (10_000, 100_000) if self._quick.isChecked() else (10_000, 100_000, 1_000_000)
        trace = (200, 1_000) if self._quick.isChecked() else (200, 1_000, 5_000)

        # GUI curve render — measured HERE on the paint thread (that's the point)
        self._status.setText("Measuring curve render…")
        QtWidgets.QApplication.processEvents()
        for n in scalar:
            self._add_row(self._render_bench(n))

        # data-plane suite off the GUI thread so the app stays responsive
        self._status.setText("Running data-plane paths (off the GUI thread)…")
        self._worker = _BenchWorker(scalar, trace, rounds=5)
        self._thread = QtCore.QThread()          # NOT parented to the dialog: closing the
        #                                          dialog must never destroy a running thread
        self._worker.moveToThread(self._thread)
        self._worker.row.connect(self._add_row)          # queued: worker → GUI
        self._worker.finished.connect(self._finished)
        self._thread.started.connect(self._worker.run)
        self._thread.start()

    def _render_bench(self, n) -> dict:
        """Faithful GUI cost: build a real pyqtgraph curve of `n` points and force a
        synchronous render (grab paints the widget regardless of visibility)."""
        pw = pg.PlotWidget()
        pw.resize(900, 450)
        curve = pw.plot(pen="y")
        x = np.arange(n, dtype="f8")
        y = 100.0 + np.cumsum(np.random.default_rng(0).standard_normal(n)) * 0.01
        rounds, ts = 3, []
        for _ in range(rounds):
            a = time.perf_counter()
            curve.setData(x, y)
            pw.grab()                                    # force the paint synchronously
            ts.append(time.perf_counter() - a)
        pw.deleteLater()
        med = sorted(ts)[len(ts) // 2]
        return {"path": "pyqtgraph curve render (full-res)", "size": n,
                "unit": "points", "median_ms": med * 1000.0,
                "min_ms": min(ts) * 1000.0, "rate": (n / med) if med else 0.0}

    def _add_row(self, r: dict) -> None:
        self._rows.append(r)
        i = self.table.rowCount()
        self.table.insertRow(i)
        size = f"{r['size']:,} {r.get('unit', '')}".strip()
        rate = r.get("rate", 0.0)
        rate_s = (f"{rate / 1e6:.1f} M {r.get('unit', 'items')}/s" if rate >= 1e6
                  else f"{rate / 1e3:.0f} k {r.get('unit', 'items')}/s")
        for c, val in enumerate((r["path"], size, f"{r['median_ms']:.1f}", rate_s)):
            item = QtWidgets.QTableWidgetItem(val)
            if c >= 2:
                item.setTextAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
            self.table.setItem(i, c, item)
        self.table.scrollToBottom()
        self._status.setText(f"{len(self._rows)} measurements…")

    def _finished(self) -> None:
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(2000)
        self._thread = self._worker = None
        self._run_btn.setEnabled(True)
        self._save_btn.setEnabled(bool(self._rows))
        self._status.setText(f"Done — {len(self._rows)} measurements.")

    def _save(self) -> None:
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save benchmark results", "benchmark.json", "JSON (*.json)")
        if not path:
            return
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self._rows, fh, indent=2)
        self._status.setText(f"Saved {len(self._rows)} rows → {path}")

    def closeEvent(self, ev):  # noqa: N802
        # cancel is polled BETWEEN measurements (bench._tick), so the worker returns
        # within one measurement; wait generously so the thread is fully stopped
        # before it (and the dialog) are torn down — never destroyed mid-run.
        if self._worker is not None:
            self._worker.cancel()
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(15000)
            self._thread = self._worker = None
        super().closeEvent(ev)
