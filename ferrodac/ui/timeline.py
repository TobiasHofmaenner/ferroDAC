"""Timeline view — the video-editor history browser on real data (DESIGN §7.4).

An additive window (doesn't touch the live dashboard): a left source list, a
coverage **finder ribbon** with a draggable window + playhead, query-driven
charts, and a transport. Everything reads through the **resolver** (live RAM ring
+ durable store), so browse → play → live is one continuum on real acquired data.

Scalar sources for now; the prototype proved the rest of the modalities.
"""

from __future__ import annotations

import time

import numpy as np
import pyqtgraph as pg
from qtpy import QtCore, QtGui, QtWidgets

_PANEL = "#1e1e2a"
_MUTED = "#7f8a99"
_ACCENT = "#4dabf7"


def _label(key: str) -> str:
    return key.rsplit("/", 1)[-1]            # show the source id, not the full path


class Ribbon(pg.PlotWidget):
    """Per-source coverage tracks + a draggable window region + playhead."""

    windowChanged = QtCore.Signal(float, float)
    scrubbed = QtCore.Signal()
    recenter = QtCore.Signal(float)

    def __init__(self, sources, cover, t0, t1):
        super().__init__(axisItems={"bottom": pg.DateAxisItem(orientation="bottom")})
        self.setBackground(_PANEL)
        self.setMenuEnabled(False)
        self.setMouseEnabled(x=True, y=False)
        self.hideButtons()
        self.getAxis("left").setStyle(showValues=False)
        self.getAxis("left").setWidth(60)
        self._labels = []
        rows = list(sources)
        for i, key in enumerate(rows):
            y = len(rows) - 1 - i
            for (a, b) in cover.get(key, []):
                self.addItem(pg.BarGraphItem(x0=a, width=max(b - a, 1.0), y0=y + 0.15,
                             height=0.5, brush="#4dabf7", pen=None))
            lab = pg.TextItem(_label(key), color=_MUTED, anchor=(0, 0.5))
            self.addItem(lab)
            self._labels.append((lab, y + 0.4))
        self.setYRange(-0.5, max(1, len(rows)), padding=0)
        self.region = pg.LinearRegionItem(brush=(77, 171, 247, 40),
                                           hoverBrush=(77, 171, 247, 70))
        self.region.setZValue(10)
        self.addItem(self.region)
        self.region.sigRegionChanged.connect(self._on_region)
        self.head = pg.InfiniteLine(angle=90, movable=False,
                                    pen=pg.mkPen("#ff6b6b", width=2))
        self.head.setZValue(20)
        self.addItem(self.head)
        self.setXRange(t0, t1, padding=0.02)
        self.getPlotItem().getViewBox().setLimits(xMin=t0 - 1, xMax=t1 + 86400)
        self.set_window(t0, t1)
        self.scene().sigMouseClicked.connect(self._click)
        self.getPlotItem().getViewBox().sigXRangeChanged.connect(self._reflow)
        self._reflow()

    def _on_region(self):
        a, b = self.region.getRegion()
        self.head.setPos(b)
        self.scrubbed.emit()
        self.windowChanged.emit(a, b)

    def set_window(self, a, b):
        self.region.blockSignals(True)
        self.region.setRegion((a, b))
        self.region.blockSignals(False)
        self.head.setPos(b)

    def _click(self, ev):
        if ev.double():
            t = self.getPlotItem().getViewBox().mapSceneToView(ev.scenePos()).x()
            self.recenter.emit(float(t))
            ev.accept()

    def _reflow(self, *_):
        x0, x1 = self.getPlotItem().getViewBox().viewRange()[0]
        for lab, y in self._labels:
            lab.setPos(x0 + (x1 - x0) * 0.006, y)


class TimelineWindow(QtWidgets.QMainWindow):
    def __init__(self, resolver, store, parent=None):
        super().__init__(parent)
        self.setWindowTitle("ferroDAC — Timeline")
        self.resize(1100, 720)
        self.resolver = resolver
        self.store = store
        self.live = False
        self.playing = False
        self.speed = 30.0
        self._charts: dict = {}

        self._sources = list(store.sources())
        self._cover = {k: resolver.coverage(k) for k in self._sources}
        now = time.time()
        lo = min((c[0][0] for c in self._cover.values() if c), default=now - 600)
        self.now = now
        self.t0, self.t1 = max(lo, now - 600), now      # open on the last 10 min

        self._build_ui()
        for k in self._sources[:3]:                     # show the first few by default
            self._src_list.findItems(_label(k), QtCore.Qt.MatchExactly)[0] \
                .setCheckState(QtCore.Qt.Checked)
        self._refresh()

        self._live_timer = QtCore.QTimer(self, interval=500)
        self._live_timer.timeout.connect(self._tick_live)
        self._live_timer.start()
        self._play_timer = QtCore.QTimer(self, interval=50)
        self._play_timer.timeout.connect(self._tick_play)

    # -- layout --
    def _build_ui(self):
        split = QtWidgets.QSplitter()
        self.setCentralWidget(split)
        left = QtWidgets.QListWidget()
        left.setFixedWidth(180)
        for k in self._sources:
            it = QtWidgets.QListWidgetItem(_label(k))
            it.setData(QtCore.Qt.UserRole, k)
            it.setFlags(it.flags() | QtCore.Qt.ItemIsUserCheckable)
            it.setCheckState(QtCore.Qt.Unchecked)
            left.addItem(it)
        left.itemChanged.connect(self._toggle)
        self._src_list = left
        split.addWidget(left)

        right = QtWidgets.QWidget()
        rv = QtWidgets.QVBoxLayout(right)
        rv.setContentsMargins(4, 4, 4, 4)
        self._charts_box = QtWidgets.QVBoxLayout()
        cw = QtWidgets.QWidget(); cw.setLayout(self._charts_box)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True); scroll.setWidget(cw)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.ribbon = Ribbon(self._sources, self._cover, self.t0 - 0, self.now)
        self.ribbon.setMinimumHeight(130)
        self.ribbon.windowChanged.connect(self._on_window)
        self.ribbon.scrubbed.connect(lambda: self._set_live(False))
        self.ribbon.recenter.connect(self._recenter)
        vsplit = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        vsplit.addWidget(scroll); vsplit.addWidget(self.ribbon)
        vsplit.setSizes([480, 150])
        rv.addWidget(vsplit, 1)
        rv.addLayout(self._transport())
        self.ribbon.set_window(self.t0, self.t1)
        split.addWidget(right)
        split.setStretchFactor(1, 1)

    def _transport(self):
        bar = QtWidgets.QHBoxLayout()
        mk = lambda t, fn: (b := QtWidgets.QToolButton(text=t), b.clicked.connect(fn), b)[0]
        self._play_btn = mk("▶ Play", self._toggle_play)
        bar.addWidget(self._play_btn)
        self._live_btn = QtWidgets.QToolButton(text="● Now", checkable=True)
        self._live_btn.clicked.connect(lambda: self._set_live(self._live_btn.isChecked()))
        bar.addWidget(self._live_btn)
        bar.addStretch(1)
        self._clock = QtWidgets.QLabel("")
        self._clock.setStyleSheet(f"color:{_MUTED};")
        bar.addWidget(self._clock)
        return bar

    # -- interactions --
    def _toggle(self, it):
        key = it.data(QtCore.Qt.UserRole)
        on = it.checkState() == QtCore.Qt.Checked
        if on and key not in self._charts:
            p = pg.PlotWidget(axisItems={"bottom": pg.DateAxisItem(orientation="bottom")})
            p.setBackground(_PANEL)
            p.setMinimumHeight(150)
            p.showGrid(x=True, y=True, alpha=0.15)
            p.setLabel("left", _label(key))
            p.setMouseEnabled(y=False)
            if self._charts:
                p.setXLink(next(iter(self._charts.values())))
            p._curve = p.plot(pen=pg.mkPen(_ACCENT, width=2), connect="finite")
            self._charts[key] = p
            self._charts_box.addWidget(p)
            self._refresh_one(key)
        elif not on and key in self._charts:
            self._charts.pop(key).setParent(None)

    def _on_window(self, a, b):
        self.t0, self.t1 = a, b
        self._refresh()

    def _recenter(self, t):
        w = self.t1 - self.t0
        self._set_live(False)
        self._jump(t - w / 2, t + w / 2)

    def _jump(self, a, b):
        self.t0, self.t1 = a, b
        self.ribbon.set_window(a, b)
        self._refresh()

    def _toggle_play(self):
        self.playing = not self.playing
        self._play_btn.setText("⏸ Pause" if self.playing else "▶ Play")
        if self.playing:
            self._set_live(False)
            self._play_timer.start()
        else:
            self._play_timer.stop()

    def _set_live(self, on):
        self.live = on
        self._live_btn.setChecked(on)
        if on and self.playing:
            self._toggle_play()
        if on:
            w = self.t1 - self.t0
            self._jump(time.time() - w, time.time())

    def _tick_play(self):
        self.t1 += self.speed * 0.05
        if self.t1 >= time.time():
            self._toggle_play(); self._set_live(True); return
        self.ribbon.set_window(self.t0, self.t1)
        self._refresh()

    def _tick_live(self):
        self.now = time.time()
        if self.live:
            w = self.t1 - self.t0
            self.t1 = self.now; self.t0 = self.t1 - w
            self.ribbon.set_window(self.t0, self.t1)
            self._refresh()
        dt = self.now - self.t1
        tag = "● LIVE" if self.live else (f"-{dt/60:.1f} min" if dt > 1 else "now")
        self._clock.setText(time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self.t1))
                            + f"   {tag}")

    def _refresh(self):
        for key in self._charts:
            self._refresh_one(key)

    def _refresh_one(self, key):
        p = self._charts.get(key)
        if p is None:
            return
        x, y = self.resolver.query(key, self.t0, self.t1,
                                   max_points=max(400, p.width() * 2))
        p._curve.setData(x, y)
        p.setXRange(self.t0, self.t1, padding=0)
