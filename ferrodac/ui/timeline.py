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

from ._common import color_for

_BG = "#161620"
_PANEL = "#1e1e2a"
_FG = "#c7d0db"
_MUTED = "#7f8a99"
_ACCENT = "#4dabf7"


def _label(key: str) -> str:
    return key.rsplit("/", 1)[-1]            # show the source id, not the full path


def _wf_cmap():
    return pg.ColorMap([0.0, 0.5, 1.0],
                       [(12, 10, 40), (190, 50, 90), (255, 235, 130)])


class CpuBars(QtWidgets.QWidget):
    """One mini bar per logical core (green/amber/red by load)."""

    def __init__(self):
        super().__init__()
        self._vals = []
        self.setFixedSize(200, 22)

    def set_vals(self, vals):
        self._vals = list(vals)
        self.update()

    def paintEvent(self, _e):
        p = QtGui.QPainter(self)
        n = max(1, len(self._vals))
        w = self.width() / n
        for i, v in enumerate(self._vals):
            h = self.height() * min(100.0, v) / 100.0
            col = "#69db7c" if v < 60 else "#ffa94d" if v < 88 else "#ff6b6b"
            p.fillRect(QtCore.QRectF(i * w + 0.5, self.height() - h, w - 1, h),
                       QtGui.QColor(col))
        p.end()


class PerfStrip(QtWidgets.QWidget):
    """Always-on HUD: per-core CPU, RAM (+free), this app's own usage, and the
    live playback rate — requested vs *actually achieved* (the 'can I replay
    this in realtime?' readout that matters once tracks get dense)."""

    def __init__(self):
        super().__init__()
        self.setFixedHeight(28)
        self.setStyleSheet(f"background:{_PANEL};")
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(10, 2, 10, 2)
        lay.setSpacing(14)
        self._ps = None
        try:
            import psutil
            self._ps = psutil
            self._proc = psutil.Process()
            psutil.cpu_percent(percpu=True)          # prime the deltas
            self._proc.cpu_percent()
        except Exception:
            pass
        lay.addWidget(self._lbl("CPU"))
        self.bars = CpuBars()
        lay.addWidget(self.bars)
        self.ram = self._lbl("RAM —")
        lay.addWidget(self.ram)
        self.app = self._lbl("app —")
        lay.addWidget(self.app)
        lay.addStretch(1)
        self.play = self._lbl("● live")
        self.play.setStyleSheet(f"color:{_ACCENT}; font-weight:600;")
        lay.addWidget(self.play)
        self._timer = QtCore.QTimer(self, interval=1000)
        self._timer.timeout.connect(self.refresh_res)
        self._timer.start()
        self.refresh_res()

    def _lbl(self, t):
        l = QtWidgets.QLabel(t)
        l.setStyleSheet(f"color:{_MUTED};")
        return l

    def refresh_res(self):
        if self._ps is None:
            self.ram.setText("RAM — (pip install psutil)")
            return
        self.bars.set_vals(self._ps.cpu_percent(percpu=True))
        vm = self._ps.virtual_memory()
        self.ram.setText(f"RAM {vm.used/1e9:.1f}/{vm.total/1e9:.0f} GB "
                         f"({vm.percent:.0f}%) · {vm.available/1e9:.1f} free")
        self.app.setText(f"app {self._proc.cpu_percent():.0f}% cpu · "
                         f"{self._proc.memory_info().rss/1e6:.0f} MB")

    def set_play(self, text):
        self.play.setText(text)


class DateJumpDialog(QtWidgets.QDialog):
    """Pick a day or a From–To range; days with recordings are tinted (GitHub-
    contribution style). Apply → the caller jumps the head there."""

    def __init__(self, earliest, latest, densities, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Jump to date")
        import datetime as _dt
        lay = QtWidgets.QVBoxLayout(self)
        self.cal = QtWidgets.QCalendarWidget()
        self.cal.setGridVisible(True)
        y0 = _dt.date.fromtimestamp(earliest)
        today = _dt.date.fromtimestamp(latest)
        self.cal.setMinimumDate(QtCore.QDate(y0.year, y0.month, y0.day))
        self.cal.setMaximumDate(QtCore.QDate(today.year, today.month, today.day))
        for d, inten in densities.items():                   # tint recording-days
            fmt = QtGui.QTextCharFormat()
            fmt.setBackground(QtGui.QColor(40, int(70 + 150 * inten), 95))
            fmt.setForeground(QtGui.QColor("#ffffff"))
            self.cal.setDateTextFormat(QtCore.QDate(d.year, d.month, d.day), fmt)
        lay.addWidget(self.cal)
        row = QtWidgets.QHBoxLayout()
        self.frm = QtWidgets.QDateEdit(calendarPopup=True)
        self.to = QtWidgets.QDateEdit(calendarPopup=True)
        for e in (self.frm, self.to):
            e.setDisplayFormat("yyyy-MM-dd")
            e.setDateRange(self.cal.minimumDate(), self.cal.maximumDate())
        row.addWidget(QtWidgets.QLabel("From"))
        row.addWidget(self.frm)
        row.addWidget(QtWidgets.QLabel("To"))
        row.addWidget(self.to)
        row.addStretch(1)
        lay.addLayout(row)
        self.cal.clicked.connect(lambda d: (self.frm.setDate(d), self.to.setDate(d)))
        sel = self.cal.selectedDate()
        self.frm.setDate(sel); self.to.setDate(sel)
        bb = QtWidgets.QDialogButtonBox()
        bb.addButton("Apply", QtWidgets.QDialogButtonBox.AcceptRole)
        bb.addButton(QtWidgets.QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def epoch_range(self):
        d0, d1 = self.frm.date(), self.to.date()
        if d1 < d0:
            d0, d1 = d1, d0
        t0 = QtCore.QDateTime(d0, QtCore.QTime(0, 0)).toSecsSinceEpoch()
        t1 = QtCore.QDateTime(d1.addDays(1), QtCore.QTime(0, 0)).toSecsSinceEpoch()
        return float(t0), float(t1)


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
            lab = pg.TextItem(_label(key), color=color_for(key), anchor=(0, 0.5))
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
        self.now_line = pg.InfiniteLine(                  # the live edge
            angle=90, movable=False, pen=pg.mkPen("#69db7c", width=1,
            style=QtCore.Qt.DashLine), label="live",
            labelOpts={"position": 0.04, "color": "#69db7c"})
        self.now_line.setZValue(15)
        self.addItem(self.now_line)
        self._now_t = t1
        self.setXRange(t0, t1, padding=0.02)
        self.set_now(t1)
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
            brush = color_for(key)                            # track in its source colour
            for (a, b) in cover.get(key, []):
                item = pg.BarGraphItem(x0=a, width=max(b - a, 1.0), y0=y + 0.15,
                                       height=0.5, brush=brush, pen=None)
                self.addItem(item)
                self._bars.append(item)

    def set_coverage(self, cover):
        self._draw_bars(cover)

    def set_now(self, now):
        """Move the live marker to `now` and clamp the view so you can't pan/zoom
        into the future — leaving a small margin so the marker isn't flush right."""
        self._now_t = now
        self.now_line.setPos(now)
        vb = self.getPlotItem().getViewBox()
        (x0, x1), _ = vb.viewRange()
        margin = 0.12 * max(1.0, x1 - x0)
        vb.setLimits(xMin=None, xMax=now + margin)

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
        self.setStyleSheet(
            f"QMainWindow,QWidget{{background:{_BG};color:{_FG};}}"
            f"QListWidget{{background:{_PANEL};border:none;outline:0;}}"
            f"QListWidget::item{{padding:5px 8px;}}"
            f"QListWidget::item:selected{{background:{_ACCENT};color:#0b0b10;}}"
            f"QToolButton,QPushButton{{background:{_PANEL};border:1px solid #2c2c3a;"
            f"border-radius:6px;padding:5px 10px;}}"
            f"QToolButton:checked{{background:{_ACCENT};color:#0b0b10;}}"
            f"QComboBox{{background:{_PANEL};border:1px solid #2c2c3a;"
            f"border-radius:6px;padding:3px 8px;}}")
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
        # adopt the CURRENT shared head/window — opening the scrubber doesn't
        # change what the app is showing (live or parked); just ensure a width.
        if self.tc.width <= 0:
            self.tc.set_width(600.0)
        self.t0, self.t1 = self.tc.window
        self.t0 = max(self.t0, lo - 1)

        self._build_ui()
        for k in self._sources[:3]:                         # show the first few by default
            self._src_list.findItems(_label(k), QtCore.Qt.MatchExactly)[0] \
                .setCheckState(QtCore.Qt.Checked)
        self._refresh()

        self._cov_ticks = 0
        self._pending_park = None
        self._tc_unsub = self.tc.subscribe(self._on_tc)
        # view-refresh timer only (the app owns the clock heartbeat that ticks tc)
        self._live_timer = QtCore.QTimer(self, interval=500)
        self._live_timer.timeout.connect(self._live_tick)
        self._live_timer.start()
        # debounce scrub → park so a drag doesn't fire a full-res re-stream per
        # mouse event (that synchronous stream is what stutters the UI thread)
        self._park_timer = QtCore.QTimer(self, interval=70, singleShot=True)
        self._park_timer.timeout.connect(self._commit_park)

    # -- layout --
    def _build_ui(self):
        split = QtWidgets.QSplitter()
        self.setCentralWidget(split)
        left = QtWidgets.QListWidget()
        left.setFixedWidth(180)
        for k in self._sources:
            it = QtWidgets.QListWidgetItem(_label(k))
            it.setData(QtCore.Qt.UserRole, k)
            it.setForeground(QtGui.QColor(color_for(k)))     # per-source colour
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
        self.perf = PerfStrip()                              # always-on resource HUD
        rv.addWidget(self.perf)
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
        bar.addWidget(mk("📅 Date", self._open_calendar))
        bar.addWidget(mk("⤢ Fit", self._fit_to_view))
        bar.addWidget(mk("⊡ Frame", self._frame_slice))
        sp = QtWidgets.QLabel("  speed"); sp.setStyleSheet(f"color:{_MUTED};")
        bar.addWidget(sp)
        self._speed = QtWidgets.QComboBox()
        self._speed.addItems(["1×", "4×", "30×", "120×"])
        self._speed.setCurrentText("30×")
        self.tc.speed = 30.0
        self._speed.currentTextChanged.connect(
            lambda t: setattr(self.tc, "speed", float(t.rstrip("×"))))
        bar.addWidget(self._speed)
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
                p._curve = p.plot(pen=pg.mkPen(color_for(key), width=2),
                                  connect="finite")            # per-source colour
            self._charts[key] = p
            self._charts_box.addWidget(p)
            self._refresh_one(key)
        elif not on and key in self._charts:
            self._charts.pop(key).setParent(None)

    def _on_window(self, a, b):
        """User dragged the ribbon region → park the shared head there. The own
        preview updates immediately (cheap, decimated); the dashboard re-stream
        (full-res, the heavy bit) is debounced so a drag doesn't hammer the UI
        thread. The first event parks at once so we stop following (no live yank
        mid-drag); intermediate events only re-stream once the drag settles."""
        if self._syncing:
            return
        self.t0, self.t1 = a, b
        self._refresh()                       # immediate preview feedback
        self._pending_park = (a, b)
        if self.tc.following:
            self._commit_park()               # leave live now
        else:
            self._park_timer.start()          # debounce the dashboard re-stream

    def _commit_park(self):
        if self._pending_park is None:
            return
        a, b = self._pending_park
        self._pending_park = None
        self.tc.width = max(1e-3, b - a)
        self.tc.park(b)                       # head = window end → fires the replay

    def _live_tick(self):
        """500 ms VIEW refresh: move the live marker and grow the ribbon coverage
        bars as data lands. (The CLOCK heartbeat that ticks tc lives in the app,
        so the head advances even with the Timeline closed and the two views
        never double-drive it.)"""
        self.now = time.time()
        self.ribbon.set_now(self.now)
        self._cov_ticks += 1
        if self._cov_ticks % 4 == 0:                  # coverage changes slowly (~2s)
            cov = {k: self.resolver.coverage(k) for k in self._sources}
            if cov != self._cover:                    # redraw bars only when changed
                self._cover = cov
                self.ribbon.set_coverage(cov)

    def _recenter(self, t):
        self.tc.park(t + self.tc.width / 2)   # double-click → centre the head on t

    def _fit_to_view(self):
        """Snap the window/head to whatever the ribbon currently shows — navigate
        the finder (drag=pan, scroll=zoom) to frame a region, then Fit."""
        x0, x1 = self.ribbon.getPlotItem().getViewBox().viewRange()[0]
        if x1 - x0 < 1e-6:
            return
        self.tc.width = x1 - x0
        self.tc.park(x1)

    def _frame_slice(self):
        """Reverse of Fit: zoom the finder to frame the current window (with a
        margin so the handles sit inset). Moves only the ribbon view."""
        t0, t1 = self.tc.window
        if t1 - t0 <= 0:
            return
        pad = (t1 - t0) * 0.1
        self.ribbon.getPlotItem().getViewBox().setXRange(t0 - pad, t1 + pad, padding=0)

    def _day_densities(self):
        """{date: 0..1} fraction of each day covered by any source — for the
        calendar tinting (GitHub-contribution style)."""
        import datetime as dt
        secs: dict = {}
        for k in self._sources:
            for (a, b) in self.resolver.coverage(k):
                t = a
                while t < b:
                    day = dt.date.fromtimestamp(t)
                    day_end = dt.datetime.combine(
                        day + dt.timedelta(days=1), dt.time()).timestamp()
                    secs[day] = secs.get(day, 0.0) + (min(b, day_end) - t)
                    t = day_end
        return {d: min(1.0, s / 86400.0) for d, s in secs.items()}

    def _open_calendar(self):
        covs = [self.resolver.coverage(k) for k in self._sources]
        starts = [c[0][0] for c in covs if c]
        earliest = min(starts) if starts else time.time() - 86400
        dlg = DateJumpDialog(earliest, time.time(), self._day_densities(), self)
        if dlg.exec():
            t0, t1 = dlg.epoch_range()
            self.tc.width = max(1.0, t1 - t0)
            self.tc.park(t1)                  # jump the head to the selected range

    def _jump(self, a, b):
        self.t0, self.t1 = a, b
        self.ribbon.set_window(a, b)
        self._refresh()

    def _toggle_play(self):
        # the app's play timer does the ticking; here we only flip tc.playing
        if self.tc.playing:
            self.tc.playing = False
        else:
            if self.tc.following:             # nothing ahead of now → park first
                self.tc.park(self.tc.head)
            self.tc.playing = True
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
        txt = f"{self.tc.speed:.0f}×"                  # reflect speed (e.g. hit-live→1×)
        if self._speed.currentText() != txt:
            i = self._speed.findText(txt)
            if i >= 0:
                self._speed.blockSignals(True)
                self._speed.setCurrentIndex(i)
                self._speed.blockSignals(False)

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
        self._sync_transport()
        if self.tc.following:
            self.perf.set_play("● live · 1.0× realtime")
        elif self.tc.playing:
            self.perf.set_play(f"▶ {self.tc.speed:.0f}× req · {self.tc.rate:.1f}× actual")
        else:
            self.perf.set_play("⏸ parked")
        dt = self.now - self.t1
        tag = ("● LIVE" if self.tc.following
               else (f"-{dt/60:.1f} min" if dt > 1 else "now"))
        self._clock.setText(time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self.t1))
                            + f"   {tag}")

    def closeEvent(self, ev):
        # leave the head/view exactly as-is — the dockable Player controls the
        # head independently, so closing the scrubber changes nothing.
        self._live_timer.stop(); self._park_timer.stop()
        try:
            self._tc_unsub()
        except Exception:
            pass
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
        # the MOST RECENT epoch in view — live scans land in the current epoch,
        # which may have fewer scans than an older (denser) one; picking by count
        # would freeze the preview on stale history.
        times, Y, x = max(blocks, key=lambda b: b[0][-1])
        z = np.log10(np.clip(Y, 1e-12, None))                 # (n_time, n_mass)
        p._img.setImage(z, autoLevels=True)
        x0, x1 = float(x[0]), float(x[-1])
        p._img.setRect(QtCore.QRectF(float(times[0]), x0,
                                     max(1e-6, float(times[-1] - times[0])), x1 - x0))
        p.setXRange(self.t0, self.t1, padding=0)
