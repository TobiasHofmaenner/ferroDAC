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


def _wf_cmap():
    return pg.ColorMap([0.0, 0.5, 1.0],
                       [(12, 10, 40), (190, 50, 90), (255, 235, 130)])


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
        self._bars = []
        self._rows = list(sources)
        for i, key in enumerate(self._rows):
            y = len(self._rows) - 1 - i
            lab = pg.TextItem(_label(key), color=_MUTED, anchor=(0, 0.5))
            self.addItem(lab)
            self._labels.append((lab, y + 0.4))
        self._draw_bars(cover)
        self.setYRange(-0.5, max(1, len(self._rows)), padding=0)
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

    def _draw_bars(self, cover):
        """(Re)draw the per-source coverage bars — called on open and whenever
        live data extends coverage, so the tracks grow with the data."""
        for b in self._bars:
            self.removeItem(b)
        self._bars = []
        n = len(self._rows)
        for i, key in enumerate(self._rows):
            y = n - 1 - i
            for (a, b) in cover.get(key, []):
                item = pg.BarGraphItem(x0=a, width=max(b - a, 1.0), y0=y + 0.15,
                                       height=0.5, brush="#4dabf7", pen=None)
                self.addItem(item)
                self._bars.append(item)

    def set_coverage(self, cover):
        self._draw_bars(cover)

    def follow_view(self, head):
        """While following live, pan the view to keep the head near the right
        edge (preserving the user's zoom width); no-op if it's already in view."""
        vb = self.getPlotItem().getViewBox()
        (x0, x1), _ = vb.viewRange()
        w = max(1.0, x1 - x0)
        if head > x1 - w * 0.08 or head < x0:
            vb.setXRange(head - w * 0.9, head + w * 0.1, padding=0)

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
    """The video-editor scrubber. Its playhead **is** the app's head: it drives
    the shared `TimeContext`, so parking it here re-streams the historic slice
    into the live dashboard (the ReplayController, subscribed to the same `tc`).
    Live is just the head at now. Its own charts are a preview of the resolver."""

    def __init__(self, resolver, store, time_context, parent=None):
        super().__init__(parent)
        self.setWindowTitle("ferroDAC — Timeline")
        self.setAttribute(QtCore.Qt.WA_DeleteOnClose, True)   # fresh tc link per open
        self.resize(1100, 720)
        self.resolver = resolver
        self.store = store
        self.tc = time_context
        self.speed = 30.0
        self._charts: dict = {}
        self._syncing = False                           # guard tc⇄ribbon feedback

        self._sources = list(store.sources())
        self._cover = {k: resolver.coverage(k) for k in self._sources}
        now = time.time()
        lo = min((c[0][0] for c in self._cover.values() if c), default=now - 600)
        self.now = now
        # adopt the shared head; open on the last 10 min if it's stale/following
        self.tc.set_width(600.0)
        self.tc.follow_now()
        self.t0, self.t1 = self.tc.window
        self.t0 = max(self.t0, lo - 1)

        self._build_ui()
        for k in self._sources[:3]:                         # show the first few by default
            self._src_list.findItems(_label(k), QtCore.Qt.MatchExactly)[0] \
                .setCheckState(QtCore.Qt.Checked)
        self._refresh()

        self._tc_unsub = self.tc.subscribe(self._on_tc)
        self._live_timer = QtCore.QTimer(self, interval=500)
        self._live_timer.timeout.connect(self._live_tick)
        self._live_timer.start()
        self._play_timer = QtCore.QTimer(self, interval=50)
        self._play_timer.timeout.connect(lambda: self.tc.tick_play(0.05))

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
        self.ribbon.windowChanged.connect(self._on_window)   # drag → park the head
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
            p.setMouseEnabled(y=False)
            if self._charts:
                p.setXLink(next(iter(self._charts.values())))   # shared time axis
            if self.store.source_dtype(key) == "trace":         # spectrogram track
                p.setLabel("left", "m/z")
                img = pg.ImageItem()
                img.setLookupTable(_wf_cmap().getLookupTable())
                p.addItem(img)
                p._img = img
            else:
                p.setLabel("left", _label(key))
                p._curve = p.plot(pen=pg.mkPen(_ACCENT, width=2), connect="finite")
            self._charts[key] = p
            self._charts_box.addWidget(p)
            self._refresh_one(key)
        elif not on and key in self._charts:
            self._charts.pop(key).setParent(None)

    def _on_window(self, a, b):
        """User dragged the ribbon region → park the shared head there. This is
        what routes the selected slice into the live dashboard (the controller,
        on the same tc, clears the panels and re-streams the slice full-res)."""
        if self._syncing:
            return
        self.tc.width = max(1e-3, b - a)
        self.tc.park(b)                       # head = window end → fires the replay

    def _live_tick(self):
        """500 ms heartbeat: advance the head (if following) and grow the ribbon
        coverage bars as new data lands (the preview charts already grow via tc)."""
        self.tc.tick_live()
        self._cover = {k: self.resolver.coverage(k) for k in self._sources}
        self.ribbon.set_coverage(self._cover)

    def _recenter(self, t):
        self.tc.park(t + self.tc.width / 2)   # double-click → centre the head on t

    def _jump(self, a, b):
        self.t0, self.t1 = a, b
        self.ribbon.set_window(a, b)
        self._refresh()

    def _toggle_play(self):
        if self.tc.playing:
            self.tc.playing = False
            self._play_timer.stop()
        else:
            if self.tc.following:             # nothing ahead of now → park first
                self.tc.park(self.tc.head)
            self.tc.playing = True
            self._play_timer.start()
        self._sync_transport()

    def _set_live(self, on):
        if on:
            self.tc.follow_now()              # ● Now → head jumps to the live edge
        elif self.tc.following:
            self.tc.park(self.tc.head)        # leaving live → park where we are
        self._sync_transport()

    def _sync_transport(self):
        self._play_btn.setText("⏸ Pause" if self.tc.playing else "▶ Play")
        self._live_btn.blockSignals(True)
        self._live_btn.setChecked(self.tc.following)
        self._live_btn.blockSignals(False)

    def _on_tc(self):
        """The shared head moved (us, a play/live tick, or elsewhere) → reflect it
        in the ribbon, preview charts, transport and clock readout."""
        self.now = time.time()
        self.t0, self.t1 = self.tc.window
        self._syncing = True                  # ribbon update must not re-park tc
        self.ribbon.set_window(self.t0, self.t1)
        if self.tc.following:
            self.ribbon.follow_view(self.t1)  # keep the live edge in view
        self._syncing = False
        self._refresh()
        if not self.tc.playing and self._play_timer.isActive():
            self._play_timer.stop()
        self._sync_transport()
        dt = self.now - self.t1
        tag = ("● LIVE" if self.tc.following
               else (f"-{dt/60:.1f} min" if dt > 1 else "now"))
        self._clock.setText(time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self.t1))
                            + f"   {tag}")

    def closeEvent(self, ev):
        self._live_timer.stop(); self._play_timer.stop()
        try:
            self._tc_unsub()
        except Exception:
            pass
        self.tc.follow_now()                  # closing the scrubber returns to live
        super().closeEvent(ev)

    def _refresh(self):
        for key in self._charts:
            self._refresh_one(key)

    def _refresh_one(self, key):
        p = self._charts.get(key)
        if p is None:
            return
        if hasattr(p, "_img"):                       # trace source → waterfall track
            self._refresh_waterfall(key, p)
            return
        x, y = self.resolver.query(key, self.t0, self.t1,
                                   max_points=max(400, p.width() * 2))
        p._curve.setData(x, y)
        p.setXRange(self.t0, self.t1, padding=0)

    def _refresh_waterfall(self, key, p):
        """Render a trace source as a spectrogram over the window: X = time,
        Y = swept axis (m/z), colour = log intensity, via the display-decimated
        query_trace (never the analysis path)."""
        blocks = [b for b in self.store.query_trace(key, self.t0, self.t1, max_scans=320)
                  if len(b[0])]
        if not blocks:
            p._img.clear()
            p.setXRange(self.t0, self.t1, padding=0)
            return
        times, Y, x = max(blocks, key=lambda b: len(b[0]))    # densest epoch in view
        z = np.log10(np.clip(Y, 1e-12, None))                 # (n_time, n_mass)
        p._img.setImage(z, autoLevels=True)
        x0, x1 = float(x[0]), float(x[-1])
        p._img.setRect(QtCore.QRectF(float(times[0]), x0,
                                     max(1e-6, float(times[-1] - times[0])), x1 - x0))
        p.setXRange(self.t0, self.t1, padding=0)
