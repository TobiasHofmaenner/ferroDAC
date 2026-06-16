"""ferroDAC — history-timeline UX spike (THROWAWAY PROTOTYPE).

NOT part of the app. A standalone window to find the *feel* of the video-editor
data-history experience (DESIGN §7.2) before any storage decisions:

    left HISTORY browser  +  charts (the "eyepiece")  +  a transport with a playhead

The whole point is the one continuum on a single control:
    drag a slice in the ribbon  → load it instantly (scrub)
    press Play                  → the slice's END advances (replay)
    the head catches "now"      → it locks → you're Live

Fed by synthetic in-memory data (some tracks intermittent, with gaps), queried
through a min/max-bucket `query(src, t0, t1, max_points)` — the same windowed,
resolution-aware call the real charts would use. Run:

    QT_API=pyside6 python3 prototypes/timeline_spike.py
"""

from __future__ import annotations

import time

import numpy as np
import pyqtgraph as pg
from qtpy import QtCore, QtGui, QtWidgets

# ---- look -----------------------------------------------------------------
BG = "#161620"
PANEL = "#1e1e2a"
FG = "#c7d0db"
MUTED = "#7f8a99"
ACCENT = "#4dabf7"
pg.setConfigOption("background", BG)
pg.setConfigOption("foreground", FG)
pg.setConfigOptions(antialias=True)

NOW0 = time.time()
HIST = 2 * 3600.0           # 2 h of synthetic history
DT = 1.0                    # sample spacing (s)


# ---- synthetic store ------------------------------------------------------
class Store(QtCore.QObject):
    """A few sources over [now-2h, now]; some intermittent. `now` advances and
    continuous sources get fresh samples, so Live feels live."""

    def __init__(self):
        super().__init__()
        self.now = NOW0
        self.sources = {
            "ion": dict(name="Ion gauge", unit="mbar", color="#4dabf7"),
            "temp": dict(name="Chamber temp", unit="°C", color="#ffa94d"),
            "rga": dict(name="RGA total", unit="A", color="#69db7c"),
        }
        self.data: dict[str, list] = {}        # id -> [t(np), v(np)]
        self.cover: dict[str, list] = {}       # id -> [(t0,t1), ...] coverage
        self._build()
        self.tags = [(NOW0 - 5400, "Bakeout off"), (NOW0 - 2700, "Close GV"),
                     (NOW0 - 1500, "Open GV")]
        self.runs = [("run 1 — pumpdown", NOW0 - 5200, NOW0 - 4200, "run"),
                     ("run 2 — leak check", NOW0 - 2600, NOW0 - 1400, "run"),
                     ("export — water peak", NOW0 - 1300, NOW0 - 900, "export")]

    def _seg(self, src, t0, t1, fn):
        t = np.arange(t0, t1, DT)
        self.data.setdefault(src, [np.array([]), np.array([])])
        self.data[src][0] = np.concatenate([self.data[src][0], t])
        self.data[src][1] = np.concatenate([self.data[src][1], fn(t)])
        self.cover.setdefault(src, []).append((t0, t1))

    def _build(self):
        a = NOW0 - HIST
        rng = np.random.default_rng(7)
        # ion gauge — continuous decaying pressure + noise
        self._seg("ion", a, NOW0, lambda t: 10 ** (-6 - 3 * (t - a) / HIST
                  + 0.04 * rng.standard_normal(len(t))
                  + 0.3 * np.exp(-((t - (NOW0 - 2700)) / 200) ** 2)))   # GV bump
        # temp — continuous slow drift
        self._seg("temp", a, NOW0, lambda t: 24 + 6 * np.sin((t - a) / 1800)
                  + 0.1 * rng.standard_normal(len(t)))
        # RGA — INTERMITTENT: scan bursts with gaps (tracks can have gaps)
        s = a
        while s < NOW0:
            burst = min(180.0, NOW0 - s)
            self._seg("rga", s, s + burst, lambda t: 1e-9 * (1 + 0.5 * np.sin(t / 50))
                      * (1 + 0.1 * rng.standard_normal(len(t))))
            s += burst + rng.uniform(120, 360)        # gap

    def tick_live(self):
        """Advance now; append fresh samples to the continuous sources."""
        prev, self.now = self.now, time.time()
        for src, fn in (("ion", lambda t: 10 ** (-9 + 0.05 * np.sin(t / 7)
                         + 0.04 * np.random.standard_normal(len(t)))),
                        ("temp", lambda t: 24 + 6 * np.sin((t - (NOW0 - HIST)) / 1800)
                         + 0.1 * np.random.standard_normal(len(t)))):
            t = np.arange(prev + DT, self.now, DT)
            if len(t):
                self.data[src][0] = np.concatenate([self.data[src][0], t])
                self.data[src][1] = np.concatenate([self.data[src][1], fn(t)])
                self.cover[src][-1] = (self.cover[src][-1][0], self.now)

    def query(self, src, t0, t1, max_points=2000):
        """Windowed + resolution-aware: min/max envelope buckets so peaks
        survive downsampling. Returns x, y with NaN across coverage gaps."""
        t, v = self.data[src]
        i0, i1 = np.searchsorted(t, [t0, t1])
        ts, vs = t[i0:i1], v[i0:i1]
        if len(ts) == 0:
            return np.array([]), np.array([])
        if len(ts) > max_points:
            nb = max_points // 2
            edges = np.linspace(0, len(ts), nb + 1).astype(int)
            xs, ys = [], []
            for k in range(nb):
                lo, hi = edges[k], edges[k + 1]
                if hi <= lo:
                    continue
                seg = vs[lo:hi]
                j = np.argmin(seg)
                xs += [ts[lo + j], ts[lo + np.argmax(seg)]]
                ys += [seg[j], seg.max()]
            order = np.argsort(xs)
            ts, vs = np.array(xs)[order], np.array(ys)[order]
        # punch NaN gaps so the chart shows intermittency honestly
        gaps = np.where(np.diff(ts) > 5 * DT)[0]
        if len(gaps):
            ts = np.insert(ts, gaps + 1, np.nan)
            vs = np.insert(vs, gaps + 1, np.nan)
        return ts, vs


# ---- the history ribbon (coverage tracks + runs + tags + region + head) ---
class Ribbon(pg.PlotWidget):
    windowChanged = QtCore.Signal(float, float)
    scrubbed = QtCore.Signal()             # user grabbed the region → leave Live

    def __init__(self, store: Store):
        super().__init__()
        self.store = store
        self.setBackground(PANEL)
        self.setMenuEnabled(False)
        self.setMouseEnabled(x=True, y=False)
        self.hideButtons()
        self.getAxis("left").setStyle(showValues=False)
        self.getAxis("left").setWidth(70)
        self.setLabel("bottom", "")
        self._rows = list(store.sources)
        self._draw_static()
        self.region = pg.LinearRegionItem(brush=(77, 171, 247, 40),
                                          hoverBrush=(77, 171, 247, 70))
        self.region.setZValue(10)
        self.addItem(self.region)
        # set_window() blocks signals, so any sigRegionChanged that fires here
        # is a genuine user drag → treat it as a scrub (leaves Live).
        self.region.sigRegionChanged.connect(self._on_region)
        self.head = pg.InfiniteLine(angle=90, movable=False,
                                    pen=pg.mkPen("#ff6b6b", width=2))
        self.head.setZValue(20)
        self.addItem(self.head)
        self.setXRange(NOW0 - HIST, NOW0, padding=0.02)

    def _draw_static(self):
        rows = self._rows
        for i, src in enumerate(rows):
            y = len(rows) - 1 - i
            c = self.store.sources[src]["color"]
            for (t0, t1) in self.store.cover[src]:
                self.addItem(pg.BarGraphItem(x0=t0, width=t1 - t0, y0=y + 0.15,
                             height=0.5, brush=c, pen=None))
            lbl = pg.TextItem(self.store.sources[src]["name"], color=MUTED,
                              anchor=(0, 0.5))
            lbl.setPos(NOW0 - HIST, y + 0.4)
            lbl.setFlag(lbl.GraphicsItemFlag.ItemIgnoresTransformations, False)
            self.addItem(lbl)
        # runs/exports row at the bottom
        for (name, t0, t1, kind) in self.store.runs:
            col = "#845ef7" if kind == "run" else "#f783ac"
            self.addItem(pg.BarGraphItem(x0=t0, width=t1 - t0, y0=-0.85,
                         height=0.5, brush=col, pen=None))
        # tag pins
        for (t, label) in self.store.tags:
            self.addItem(pg.InfiniteLine(pos=t, angle=90,
                         pen=pg.mkPen("#ffd54f", width=1, style=QtCore.Qt.DashLine)))
        self.setYRange(-1.0, len(rows), padding=0)

    def _on_region(self):
        t0, t1 = self.region.getRegion()
        self.head.setPos(t1)
        self.scrubbed.emit()                  # user grabbed it → leave Live
        self.windowChanged.emit(t0, t1)

    def set_window(self, t0, t1):
        self.region.blockSignals(True)
        self.region.setRegion((t0, t1))
        self.region.blockSignals(False)
        self.head.setPos(t1)


# ---- the prototype window -------------------------------------------------
class Spike(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.store = Store()
        self.setWindowTitle("ferroDAC — history timeline (UX spike)")
        self.resize(1180, 760)
        self.setStyleSheet(
            f"QMainWindow,QWidget{{background:{BG};color:{FG};}}"
            f"QListWidget{{background:{PANEL};border:none;outline:0;}}"
            f"QListWidget::item{{padding:5px 8px;}}"
            f"QListWidget::item:selected{{background:{ACCENT};color:#0b0b10;}}"
            f"QLabel#hdr{{color:{MUTED};font:600 10px;padding:8px 8px 2px;}}"
            f"QToolButton,QPushButton{{background:{PANEL};border:1px solid #2c2c3a;"
            f"border-radius:6px;padding:5px 10px;}}"
            f"QToolButton:checked{{background:{ACCENT};color:#0b0b10;}}")

        self.live = False
        self.playing = False
        self.speed = 1.0
        self._charts: dict[str, pg.PlotWidget] = {}

        self._build_ui()
        # initial window: the last 20 min
        self.t0, self.t1 = self.store.now - 1200, self.store.now
        self.ribbon.set_window(self.t0, self.t1)
        for s in ("ion", "temp"):
            self._browser_sources.findItems(self.store.sources[s]["name"],
                                            QtCore.Qt.MatchExactly)[0].setCheckState(
                QtCore.Qt.Checked)
        self._refresh()

        self._live_timer = QtCore.QTimer(self, interval=200)
        self._live_timer.timeout.connect(self._on_live_tick)
        self._live_timer.start()
        self._play_timer = QtCore.QTimer(self, interval=33)
        self._play_timer.timeout.connect(self._on_play_tick)

    # -- layout --
    def _build_ui(self):
        split = QtWidgets.QSplitter()
        self.setCentralWidget(split)

        # left: HISTORY browser
        left = QtWidgets.QWidget()
        left.setFixedWidth(220)
        lv = QtWidgets.QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.setSpacing(0)
        lv.addWidget(self._label("SOURCES"))
        self._browser_sources = QtWidgets.QListWidget()
        for src, meta in self.store.sources.items():
            it = QtWidgets.QListWidgetItem(meta["name"])
            it.setData(QtCore.Qt.UserRole, src)
            it.setFlags(it.flags() | QtCore.Qt.ItemIsUserCheckable)
            it.setCheckState(QtCore.Qt.Unchecked)
            it.setForeground(QtGui.QColor(meta["color"]))
            self._browser_sources.addItem(it)
        self._browser_sources.itemChanged.connect(self._on_source_toggle)
        lv.addWidget(self._browser_sources)
        lv.addWidget(self._label("DATASETS"))
        self._browser_runs = QtWidgets.QListWidget()
        for (name, t0, t1, kind) in self.store.runs:
            it = QtWidgets.QListWidgetItem(("▸ " if kind == "run" else "⇩ ") + name)
            it.setData(QtCore.Qt.UserRole, (t0, t1))
            self._browser_runs.addItem(it)
        self._browser_runs.itemClicked.connect(self._on_run_click)
        lv.addWidget(self._browser_runs)
        split.addWidget(left)

        # right: charts stack + ribbon + transport
        right = QtWidgets.QWidget()
        rv = QtWidgets.QVBoxLayout(right)
        rv.setContentsMargins(6, 6, 6, 6)
        self._charts_box = QtWidgets.QVBoxLayout()
        self._charts_box.setSpacing(4)
        cw = QtWidgets.QWidget()
        cw.setLayout(self._charts_box)
        rv.addWidget(cw, 1)
        self.ribbon = Ribbon(self.store)
        self.ribbon.setFixedHeight(140)
        self.ribbon.windowChanged.connect(self._on_window)
        self.ribbon.scrubbed.connect(lambda: self._set_live(False))
        rv.addWidget(self.ribbon)
        rv.addLayout(self._transport())
        split.addWidget(right)
        split.setStretchFactor(1, 1)

    def _label(self, text):
        lb = QtWidgets.QLabel(text)
        lb.setObjectName("hdr")
        return lb

    def _transport(self):
        bar = QtWidgets.QHBoxLayout()
        mk = lambda t, fn: (b := QtWidgets.QToolButton(text=t), b.clicked.connect(fn), b)[0]
        bar.addWidget(mk("⏮", lambda: self._jump(self.t0 - (self.t1 - self.t0))))
        self._play_btn = mk("▶  Play", self._toggle_play)
        bar.addWidget(self._play_btn)
        bar.addWidget(mk("⏭", lambda: self._jump(self.t1 + (self.t1 - self.t0))))
        self._live_btn = QtWidgets.QToolButton(text="● Now", checkable=True)
        self._live_btn.clicked.connect(lambda: self._set_live(self._live_btn.isChecked()))
        bar.addWidget(self._live_btn)
        bar.addStretch(1)
        self._clock = QtWidgets.QLabel("")
        self._clock.setStyleSheet(f"color:{MUTED};")
        bar.addWidget(self._clock)
        self._speed = QtWidgets.QComboBox()
        self._speed.addItems(["1×", "2×", "4×", "8×"])
        self._speed.currentTextChanged.connect(
            lambda t: setattr(self, "speed", float(t.rstrip("×"))))
        bar.addWidget(self._speed)
        return bar

    # -- interactions --
    def _on_source_toggle(self, it):
        src = it.data(QtCore.Qt.UserRole)
        on = it.checkState() == QtCore.Qt.Checked
        if on and src not in self._charts:
            p = pg.PlotWidget()
            p.setBackground(PANEL)
            p.showGrid(x=True, y=True, alpha=0.15)
            p.setLabel("left", self.store.sources[src]["name"],
                       units=self.store.sources[src]["unit"])
            p.setMouseEnabled(y=False)
            if self._charts:
                p.setXLink(next(iter(self._charts.values())))
            p._curve = p.plot(pen=pg.mkPen(self.store.sources[src]["color"], width=2),
                              connect="finite")
            self._charts[src] = p
            self._charts_box.addWidget(p)
            self._refresh_one(src)
        elif not on and src in self._charts:
            self._charts.pop(src).setParent(None)

    def _on_run_click(self, it):
        t0, t1 = it.data(QtCore.Qt.UserRole)
        pad = (t1 - t0) * 0.1
        self._set_live(False)
        self._jump_window(t0 - pad, t1 + pad)

    def _on_window(self, t0, t1):
        self.t0, self.t1 = t0, t1
        self._refresh()

    def _jump(self, center_t1):
        w = self.t1 - self.t0
        self._jump_window(center_t1 - w, center_t1)

    def _jump_window(self, t0, t1):
        self.t0, self.t1 = t0, t1
        self.ribbon.set_window(t0, t1)
        self._refresh()

    def _toggle_play(self):
        self.playing = not self.playing
        self._play_btn.setText("⏸  Pause" if self.playing else "▶  Play")
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
            self._jump_window(self.store.now - w, self.store.now)

    def _on_play_tick(self):
        # play = the slice's END advances (the user's keystone insight)
        self.t1 += self.speed * (self._play_timer.interval() / 1000.0) * 30
        if self.t1 >= self.store.now:
            self.t1 = self.store.now
            self._toggle_play()
            self._set_live(True)
        self.ribbon.set_window(self.t0, self.t1)
        self._refresh()

    def _on_live_tick(self):
        self.store.tick_live()
        if self.live:
            w = self.t1 - self.t0
            self.t1 = self.store.now
            self.t0 = self.t1 - w
            self.ribbon.set_window(self.t0, self.t1)
            self._refresh()
        dt = self.store.now - self.t1
        tag = "● LIVE" if self.live else f"-{dt/60:4.1f} min" if dt > 1 else "now"
        self._clock.setText(f"{time.strftime('%H:%M:%S', time.localtime(self.t1))}"
                            f"   {tag}")

    def _refresh(self):
        for src in self._charts:
            self._refresh_one(src)

    def _refresh_one(self, src):
        p = self._charts.get(src)
        if p is None:
            return
        x, y = self.store.query(src, self.t0, self.t1,
                                max_points=max(400, p.width() * 2))
        p._curve.setData(x, y)
        p.setXRange(self.t0, self.t1, padding=0)


def main():
    app = QtWidgets.QApplication([])
    w = Spike()
    w.show()
    app.exec()


if __name__ == "__main__":
    main()
