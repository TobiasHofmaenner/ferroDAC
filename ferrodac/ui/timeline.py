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


# the ~30 px navigation ribbon is the ONE legitimate midline consumer — see the
# shared decimation policy (store/decimate.py, §22 I-10) for why charts never use it
from ..store.decimate import envelope_midline as _envelope_midline  # noqa: E402


def _alive(widget) -> bool:
    """False once a widget's underlying C++ object is gone. Async ReadService
    results deliver on the GUI thread but AFTER an arbitrary delay — the Timeline
    may have been torn down or its rows rebuilt meanwhile, and touching a dead
    QGraphics object raises 'Internal C++ object already deleted'. Every async
    callback here must check its target first and drop stale deliveries."""
    if widget is None:
        return False
    try:
        from shiboken6 import isValid                # PySide6
        return bool(isValid(widget))
    except ImportError:
        pass
    for mod in ("PyQt6.sip", "PyQt5.sip", "sip"):    # PyQt variants
        try:
            import importlib
            sip = importlib.import_module(mod)
            return not sip.isdeleted(widget)
        except Exception:                            # noqa: BLE001
            continue
    return True                                      # unknown binding → assume alive


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

    windowPreview = QtCore.Signal(float, float)         # live, during a drag (cheap)
    windowChanged = QtCore.Signal(float, float, str)    # release: mode front|back|move
    recenter = QtCore.Signal(float)

    # the window can't be dragged narrower than this fraction of the VISIBLE span,
    # so head & tail never collapse onto one line — but it's zoom-relative, so
    # zooming in shrinks the floor and you can always make a finer window.
    _MIN_WIN_FRAC = 0.03

    # ambient-video coverage lane style (§9.3 phase 2): one violet, translucent
    # hatch-like band per camera BELOW the scalar rows, visually distinct from the
    # source-coloured scalar data-coverage bars.
    _VIDEO_HUE = (151, 117, 250)         # violet — "video exists here"

    def __init__(self, sources, cover, t0, t1, names=None,
                 cameras=None, video_cover=None):
        super().__init__(axisItems={"bottom": pg.DateAxisItem(orientation="bottom")})
        self.setBackground(_PANEL)
        self.setMenuEnabled(False)
        self.setMouseEnabled(x=True, y=False)
        self.hideButtons()
        self.getAxis("left").setStyle(showValues=False)
        self.getAxis("left").setWidth(60)
        self._names = names or {}
        self._labels = []
        self._bars = []
        self._region_ref = (t0, t1)          # last-set region (for edge detection)
        self._rows = list(sources)
        # video lanes (cameras) live in a band at NEGATIVE y below the scalar rows
        self._video_rows = list(cameras or [])
        self._video_cover = dict(video_cover or {})
        self._video_bars = []
        self._video_labels = []
        for i, key in enumerate(self._rows):
            y = len(self._rows) - 1 - i
            lab = pg.TextItem(self._names.get(key) or _label(key),
                              color=color_for(key), anchor=(0, 0.5),
                              fill=pg.mkBrush(18, 20, 30, 220))  # readable over its bar
            self.addItem(lab)
            lab.setZValue(25)             # keep labels above the coverage bars
            self._labels.append((lab, y + 0.4))
        self._draw_bars(cover)
        self._draw_video_lanes()
        self._apply_yrange()
        self.region = pg.LinearRegionItem(brush=(77, 171, 247, 40),
                                           hoverBrush=(77, 171, 247, 70))
        self.region.setZValue(10)
        self.addItem(self.region)
        self.region.sigRegionChanged.connect(self._on_region)
        self.region.sigRegionChangeFinished.connect(self._on_region_done)
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

    # -- video lanes (§9.3 phase 2) -------------------------------------------
    def _video_y(self, j: int) -> float:
        """y of video lane j — a band directly below the scalar rows (negative)."""
        return -1.0 - j

    def _apply_yrange(self) -> None:
        lo = -(len(self._video_rows) + 0.5) if self._video_rows else -0.5
        self.setYRange(lo, max(1, len(self._rows)), padding=0)

    def _video_label(self, cam: str) -> str:
        return "📷 " + (self._names.get(f"{cam}/frame") or _label(cam))

    def _draw_video_lanes(self) -> None:
        for lab, _ in self._video_labels:
            self.removeItem(lab)
        self._video_labels = []
        col = pg.mkColor(*self._VIDEO_HUE)
        for j, cam in enumerate(self._video_rows):
            y = self._video_y(j)
            lab = pg.TextItem(self._video_label(cam), color=col, anchor=(0, 0.5),
                              fill=pg.mkBrush(18, 20, 30, 220))
            self.addItem(lab)
            lab.setZValue(25)
            self._video_labels.append((lab, y + 0.4))
        self._draw_video_bars()

    def _draw_video_bars(self) -> None:
        for b in self._video_bars:
            self.removeItem(b)
        self._video_bars = []
        r, g, b = self._VIDEO_HUE
        for j, cam in enumerate(self._video_rows):
            y = self._video_y(j)
            for (a, b2) in self._video_cover.get(cam, []):
                item = pg.BarGraphItem(x0=a, width=max(b2 - a, 1.0), y0=y + 0.15,
                                       height=0.5, brush=(r, g, b, 130),
                                       pen=pg.mkPen(r, g, b, 220))
                self.addItem(item)
                self._video_bars.append(item)

    def set_video_coverage(self, cover: dict) -> None:
        """Refresh the video lanes' bars (async delivery — the window guards
        _alive before calling). New cameras extend the lane band."""
        self._video_cover = dict(cover or {})
        cams = list(self._video_cover)
        if cams != self._video_rows:            # a camera appeared → relabel + resize
            self._video_rows = cams
            self._draw_video_lanes()
            self._apply_yrange()
            self._reflow()
        else:
            self._draw_video_bars()

    def set_sources(self, rows, cover, names=None):
        """Rebuild the track rows (labels + bars) for a new source set — used when
        sources appear live (e.g. a device joins the hub) while the Timeline is
        open. The region/head/now markers are independent and stay put."""
        if names is not None:
            self._names = names
        for lab, _ in self._labels:
            self.removeItem(lab)
        self._labels = []
        self._rows = list(rows)
        for i, key in enumerate(self._rows):
            y = len(self._rows) - 1 - i
            lab = pg.TextItem(self._names.get(key) or _label(key),
                              color=color_for(key), anchor=(0, 0.5),
                              fill=pg.mkBrush(18, 20, 30, 220))  # readable over its bar
            self.addItem(lab)
            lab.setZValue(25)             # keep labels above the coverage bars
            self._labels.append((lab, y + 0.4))
        self._apply_yrange()              # scalar + video band
        self._draw_bars(cover)
        self._reflow()

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

    def _min_window(self):
        """Floor on the window width — a fraction of the VISIBLE span (zoom level),
        not the data — so the gap is kept relative to what's on screen."""
        (x0, x1), _ = self.getPlotItem().getViewBox().viewRange()
        return self._MIN_WIN_FRAC * max(1e-12, x1 - x0)

    def _on_region(self):
        # continuous (dragging): cheap live preview only — never the heavy commit
        a, b = self.region.getRegion()
        min_w = self._min_window()
        if b - a < min_w:                  # collapsing past the floor → hold the moved edge
            pa, pb = self._region_ref      # the region at drag start
            if abs(a - pa) >= abs(b - pb): # the tail (left) is the one being dragged in
                a = b - min_w
            else:                          # the head (right) is being dragged in
                b = a + min_w
            self.region.blockSignals(True)
            self.region.setRegion((a, b))
            self.region.blockSignals(False)
        self.head.setPos(b)
        self.windowPreview.emit(a, b)

    def _on_region_done(self):
        # released: classify the drag so the commit is right. Dragging the WHOLE
        # region keeps its width (both edges move together) → "move" the window;
        # dragging one edge changes the width → "front" (head) or "back" (tail).
        a, b = self.region.getRegion()
        pa, pb = self._region_ref
        tol = 1e-6 * max(1.0, abs(pb - pa))
        moved = abs(a - pa) > tol or abs(b - pb) > tol
        if moved and abs((b - a) - (pb - pa)) <= tol:
            mode = "move"
        elif abs(b - pb) >= abs(a - pa):
            mode = "front"
        else:
            mode = "back"
        self._region_ref = (a, b)
        self.windowChanged.emit(a, b, mode)

    def set_window(self, a, b):
        self.region.blockSignals(True)
        self.region.setRegion((a, b))
        self.region.blockSignals(False)
        self.head.setPos(b)
        self._region_ref = (a, b)

    def _click(self, ev):
        if ev.double():
            t = self.getPlotItem().getViewBox().mapSceneToView(ev.scenePos()).x()
            self.recenter.emit(float(t))
            ev.accept()

    def _reflow(self, *_):
        x0, x1 = self.getPlotItem().getViewBox().viewRange()[0]
        px = x0 + (x1 - x0) * 0.006
        for lab, y in self._labels:
            lab.setPos(px, y)
        for lab, y in self._video_labels:
            lab.setPos(px, y)


class _PreviewPlot(pg.PlotWidget):
    """A read-only Timeline preview chart: the finder/ribbon owns the data window,
    so the plot itself never zooms/pans — and a wheel over it scrolls the preview
    LIST (it bubbles to the enclosing QScrollArea) instead of zooming the curve."""

    def wheelEvent(self, ev):
        ev.ignore()                          # don't consume → QScrollArea scrolls


class TimelineWindow(QtWidgets.QMainWindow):
    """The video-editor scrubber. Its playhead **is** the app's head: it drives
    the shared `TimeContext`, so parking it here re-streams the historic slice
    into the live dashboard (the ReplayController, subscribed to the same `tc`).
    Live is just the head at now. Its own charts are a preview of the resolver."""

    def __init__(self, resolver, store, time_context, parent=None, names=None,
                 sources_fn=None, lens_fn=None, reads=None, markers=None,
                 video_store=None):
        super().__init__(parent)
        self._markers = markers              # MarkerModel | None — media tags on
        #                                      the ribbon (scrub-to-photo, §9b)
        self._video_store = video_store      # VideoStore | None — §9.3 phase 2:
        #                                      per-camera coverage lane + scrub preview
        self._video_cover = {}
        self._reads = reads                  # async resolver facade (§21.3); the
        #                                      coverage tick + preview queries go
        #                                      through it so they never block paint.
        #                                      None → synchronous (headless/tests).
        self._names = dict(names or {})      # key -> human display name (accumulated)
        self._sources_fn = sources_fn        # callable → {key: name} of LIVE sources
        #                                      (so sources that join the hub appear)
        self._lens_fn = lens_fn              # callable → the active project's curated
        #                                      channel keys (None/empty = no curation)
        self._show_all = False               # off = the project's channel lens (mirrors
        #                                      the Sources panel's "All" toggle)
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

        self._sources, self._names = self._available()
        # Seed coverage from LOCAL tiers only — this runs on the GUI thread at
        # open, and the full resolver walks the hub tier's GetCoverage gRPC per
        # source (a cold cache on a hub project = N round-trips = a multi-second
        # freeze, watchdog 2026-07-10). The async refresh scheduled below fills
        # in hub coverage through ReadService moments after the window is up.
        self._cover = {k: resolver.coverage(k, local_only=True)
                       for k in self._sources}
        # video coverage seed — LOCAL disk (json/os), the video analog of the
        # local_only scalar seed; the periodic refresh runs off the paint thread
        if self._video_store is not None:
            try:
                self._video_cover = {c: self._video_store.coverage(c)
                                     for c in self._video_store.cameras()}
            except Exception:                # noqa: BLE001 — video optional
                self._video_cover = {}
        now = time.time()
        lo = min((c[0][0] for c in self._cover.values() if c), default=now - 600)
        self.now = now
        if self.tc.following:
            # opened LIVE → follow now with a sane live tail (capped to the session
            # if it's short), and frame the ribbon at ~3× the window so you land
            # zoomed in on recent history (not spanning the whole multi-day session,
            # which made the window a tiny unusable sliver). Scroll/Fit reaches back.
            self.tc.set_width(max(60.0, min(600.0, now - lo)))
            self.tc.follow_now()
            self.t0, self.t1 = self.tc.window
            w = max(60.0, self.t1 - self.t0)
            self._view0 = max(lo - 0.04 * w, now - 3.0 * w)   # 3× window, clamped to data
            self._view1 = now + 0.02 * w
        else:
            # opened while PARKED (e.g. after Zoom-to-recording) → keep that exact
            # window and frame the ribbon around it, so the Timeline lands where you
            # already are instead of snapping back to the live edge.
            self.t0, self.t1 = self.tc.window
            pad = max(1.0, (self.t1 - self.t0) * 0.1)
            self._view0, self._view1 = self.t0 - pad, self.t1 + pad

        self._build_ui()
        self._restore_state()                  # reopen as it was left (checked + speed)
        self._refresh()

        self._cov_ticks = 0
        self._dragging = False
        self._preview_win = None
        self._tc_unsub = self.tc.subscribe(self._on_tc)
        # view-refresh timer only (the app owns the clock heartbeat that ticks tc)
        self._live_timer = QtCore.QTimer(self, interval=500)
        self._live_timer.timeout.connect(self._live_tick)
        self._live_timer.start()
        # hub coverage arrives async (the init seed above was local-only)
        QtCore.QTimer.singleShot(0, self._refresh_coverage)
        self._marker_unsub = None
        if self._markers is not None:
            self._markers.changed.connect(self._sync_media_marks)
            self._marker_unsub = lambda: self._markers.changed.disconnect(
                self._sync_media_marks)
            self._sync_media_marks()
        # debounce the live PREVIEW during a drag (cheap downsampled query); the
        # heavy main re-stream only fires on release (windowChanged).
        self._preview_timer = QtCore.QTimer(self, interval=40, singleShot=True)
        self._preview_timer.timeout.connect(self._do_preview)

    def _sync_media_marks(self) -> None:
        """Media tags (kind="media") as thin photo marks on the ribbon, so a
        scrubbing user sees WHERE photos exist; the photo tile panel shows the
        image for the head position. Diff-free redraw — media tags are few."""
        if not _alive(self.ribbon):
            return
        from ..core.tag import MEDIA
        for ln in getattr(self.ribbon, "_media_lines", ()):
            try:
                self.ribbon.removeItem(ln)
            except Exception:                # noqa: BLE001 — teardown race
                pass
        lines = []
        for m in self._markers.visible():
            if m.kind != MEDIA:
                continue
            ln = pg.InfiniteLine(pos=m.t, angle=90, movable=False,
                                 pen=pg.mkPen("#4dabf7", width=1,
                                              style=QtCore.Qt.DotLine),
                                 label="📷", labelOpts={"position": 0.94,
                                                        "color": "#4dabf7"})
            ln.setZValue(15)
            self.ribbon.addItem(ln)
            lines.append(ln)
        self.ribbon._media_lines = lines

    def _name(self, key):
        return self._names.get(key) or _label(key)

    def _available(self):
        """The current source set + names: the durable store's sources unioned
        with the live ones (a viewer's hub devices may have no stored history yet).
        Names accumulate so a late-resolving label is never lost."""
        names = dict(self._names)
        if self._sources_fn is not None:
            try:
                names.update(self._sources_fn() or {})
            except Exception:                 # a flaky provider must not break the view
                pass
        keys = list(dict.fromkeys(list(self.store.sources()) + list(names)))
        return self._lensed(keys), names

    def _lensed(self, keys):
        """The project's curated channels only (unless "All", or no curation) —
        the Timeline's source list/ribbon mirror the Sources panel's lens."""
        if self._show_all or self._lens_fn is None:
            return keys
        try:
            lens = self._lens_fn()
        except Exception:                     # a flaky provider must not blank the view
            lens = None
        if not lens:                          # nothing curated → show everything
            return keys
        return [k for k in keys if k in lens]

    def _toggle_show_all(self, on):
        self._show_all = bool(on)
        self._rebuild_sources()               # re-filter the list, ribbon and charts

    def _sync_sources(self):
        """Pick up sources that appeared since the last tick (e.g. a device joined
        the hub) and fold them into the list, ribbon tracks and coverage — so the
        open Timeline updates without a close/reopen."""
        keys, names = self._available()          # once — this walks the catalog
        if set(keys) != set(self._sources):
            self._rebuild_sources()
        else:
            self._names = names                  # names may have resolved late

    def _rebuild_sources(self):
        """Rebuild the source list + ribbon from the (lens-filtered) current set,
        preserving which sources are checked. Handles both ADD (a source joined)
        and REMOVE (the lens narrowed) — dropping the preview chart of any source
        that's no longer shown."""
        keys, names = self._available()
        keyset = set(keys)
        checked = {self._src_list.item(i).data(QtCore.Qt.UserRole)
                   for i in range(self._src_list.count())
                   if self._src_list.item(i).checkState() == QtCore.Qt.Checked}
        self._sources, self._names = keys, names
        self._src_list.blockSignals(True)     # restoring checks must not re-restream
        self._src_list.clear()
        for k in keys:
            it = QtWidgets.QListWidgetItem(self._name(k))
            it.setData(QtCore.Qt.UserRole, k)
            it.setForeground(QtGui.QColor(color_for(k)))
            it.setFlags(it.flags() | QtCore.Qt.ItemIsUserCheckable)
            it.setCheckState(QtCore.Qt.Checked if k in checked else QtCore.Qt.Unchecked)
            self._src_list.addItem(it)
        self._src_list.blockSignals(False)
        for k in list(self._charts):          # a no-longer-shown source loses its chart
            if k not in keyset:
                self._charts.pop(k).setParent(None)
        self._cover = {k: self.resolver.coverage(k) for k in keys}
        self.ribbon.set_sources(self._sources, self._cover, self._names)

    # -- layout --
    def _build_ui(self):
        split = QtWidgets.QSplitter()
        self.setCentralWidget(split)
        # left column: a "channels" header (with an All toggle = the project lens,
        # mirroring the Sources panel) over the checkable source list.
        leftcol = QtWidgets.QWidget()
        leftcol.setFixedWidth(180)
        lv = QtWidgets.QVBoxLayout(leftcol)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.setSpacing(0)
        head = QtWidgets.QHBoxLayout()
        head.setContentsMargins(8, 6, 8, 4)
        hl = QtWidgets.QLabel("Channels")
        hl.setStyleSheet(f"color:{_MUTED}; font-weight:700; font-size:11px;")
        head.addWidget(hl)
        head.addStretch(1)
        self._all_chk = QtWidgets.QCheckBox("All")
        self._all_chk.setToolTip("Show every channel, not just this project's selection")
        self._all_chk.setChecked(self._show_all)
        self._all_chk.toggled.connect(self._toggle_show_all)
        head.addWidget(self._all_chk)
        lv.addLayout(head)
        left = QtWidgets.QListWidget()
        for k in self._sources:
            it = QtWidgets.QListWidgetItem(self._name(k))
            it.setData(QtCore.Qt.UserRole, k)
            it.setForeground(QtGui.QColor(color_for(k)))     # per-source colour
            it.setFlags(it.flags() | QtCore.Qt.ItemIsUserCheckable)
            it.setCheckState(QtCore.Qt.Unchecked)
            left.addItem(it)
        left.itemChanged.connect(self._toggle)
        self._src_list = left
        lv.addWidget(left, 1)
        split.addWidget(leftcol)

        right = QtWidgets.QWidget()
        rv = QtWidgets.QVBoxLayout(right)
        rv.setContentsMargins(4, 4, 4, 4)
        self._charts_box = QtWidgets.QVBoxLayout()
        cw = QtWidgets.QWidget(); cw.setLayout(self._charts_box)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True); scroll.setWidget(cw)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.ribbon = Ribbon(self._sources, self._cover, self._view0, self._view1,
                             names=self._names,
                             cameras=list(self._video_cover),
                             video_cover=self._video_cover)
        self.ribbon.setMinimumHeight(130)
        self.ribbon.windowPreview.connect(self._on_preview)  # dragging → live preview
        self.ribbon.windowChanged.connect(self._on_window)   # release → commit (heavy)
        self.ribbon.recenter.connect(self._recenter)
        vsplit = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        vsplit.addWidget(scroll)
        self.video_preview = None
        if self._video_store is not None:
            from .videopreview import VideoPreviewPanel
            self.video_preview = VideoPreviewPanel(
                self._video_store, names_fn=lambda: self._names)
            vsplit.addWidget(self.video_preview)
        vsplit.addWidget(self.ribbon)
        vsplit.setSizes([420, 200, 150] if self.video_preview is not None
                        else [480, 150])
        rv.addWidget(vsplit, 1)
        rv.addLayout(self._transport())
        self.perf = PerfStrip()                              # always-on resource HUD
        rv.addWidget(self.perf)
        self.ribbon.set_window(self.t0, self.t1)
        self.ribbon.set_now(self.now)            # live edge + xMax at real now (view ≠ now when parked)
        split.addWidget(right)
        split.setStretchFactor(1, 1)

    def _transport(self):
        bar = QtWidgets.QHBoxLayout()
        mk = lambda t, fn: (b := QtWidgets.QToolButton(text=t), b.clicked.connect(fn), b)[0]
        self._play_btn = mk("▶ Play", self._toggle_play)
        self._play_btn.setCheckable(True)              # lit while moving (live or replay)
        bar.addWidget(self._play_btn)
        self._live_btn = QtWidgets.QToolButton(text="⦿ Live", checkable=True)
        self._live_btn.setToolTip("Jump to the live edge and follow it")
        self._live_btn.clicked.connect(self._go_live)   # always goes live (never parks)
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
        self._slide_btn = QtWidgets.QToolButton(text="⇉ Slide", checkable=True)
        self._slide_btn.setChecked(not self.tc.grow)
        self._slide_btn.setToolTip("Fixed-width window that slides (on) vs grow "
                                   "from a pinned start (off)")
        self._slide_btn.clicked.connect(
            lambda: self.tc.set_grow(not self._slide_btn.isChecked()))
        bar.addWidget(self._slide_btn)
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
            p = _PreviewPlot(axisItems={"bottom": pg.DateAxisItem(orientation="bottom")})
            p.setBackground(_PANEL)
            p.setMinimumHeight(150)
            p.showGrid(x=True, y=True, alpha=0.15)
            p.setMouseEnabled(x=False, y=False)      # read-only; finder owns the window
            p.setMenuEnabled(False)
            p.hideButtons()
            if self._charts:
                p.setXLink(next(iter(self._charts.values())))   # shared time axis
            if self.resolver.source_dtype(key) == "trace":      # spectrogram track
                p.setLabel("left", "m/z")
                img = pg.ImageItem()
                img.setLookupTable(_wf_cmap().getLookupTable())
                p.addItem(img)
                p._img = img
            else:
                p.setLabel("left", self._name(key))
                p._curve = p.plot(pen=pg.mkPen(color_for(key), width=2),
                                  connect="finite")            # per-source colour
            self._charts[key] = p
            self._charts_box.addWidget(p)
            self._refresh_one(key)
        elif not on and key in self._charts:
            self._charts.pop(key).setParent(None)

    def _on_preview(self, a, b):
        """While dragging the ribbon: update only this window's PREVIEW charts
        (cheap downsampled query), debounced. The shared clock is untouched, so
        the main dashboard is undisturbed until release."""
        if self._syncing:
            return
        self._dragging = True
        self.t0, self.t1 = a, b
        self._preview_win = (a, b)
        self._preview_timer.start()

    def _do_preview(self):
        if self._preview_win is None:
            return
        self.t0, self.t1 = self._preview_win
        self._refresh()                       # downsampled query — cheap
        self._drive_video_preview()

    def _drive_video_preview(self) -> None:
        """Point the video preview at the head instant (self.t1). Paused unless the
        transport is actually playing/live — a parked scrub shows a still frame."""
        vp = getattr(self, "video_preview", None)
        if vp is None:
            return
        vp.set_head(self.t1, playing=(self.tc.playing or self.tc.following))

    def _on_window(self, a, b, mode):
        """Drag RELEASED → commit to the shared clock. Tail edge → resize the back
        (stay live/playing); head edge → park the head; whole window → translate it
        to [a,b]. The one heavy step: the main analysis re-streams the slice."""
        self._dragging = False
        if self._syncing:
            return
        if mode == "move":
            self.tc.park_window(a, b)         # translate: pin BOTH edges to [a,b]
            #                                   (park alone would keep the old anchor
            #                                   in grow mode → window collapses)
        elif mode == "back":
            self.tc.resize_back(a)            # tail resize
        else:                                 # "front"
            self.tc.width = max(1e-3, b - a)
            self.tc.park(b)                   # head jump → full-res re-stream

    def _live_tick(self):
        """500 ms VIEW refresh: move the live marker and grow the ribbon coverage
        bars as data lands. (The CLOCK heartbeat that ticks tc lives in the app,
        so the head advances even with the Timeline closed and the two views
        never double-drive it.)"""
        self.now = time.time()
        self.ribbon.set_now(self.now)
        self._sync_sources()                          # fold in sources that joined live
        self._cov_ticks += 1
        if self._cov_ticks % 4 == 0:                  # coverage changes slowly (~2s)
            self._refresh_coverage()
            self._refresh_video_coverage()
            self._sync_video_cameras()

    def _refresh_coverage(self) -> None:
        """Repull coverage for the ribbon bars. Off the GUI thread via the async
        facade when present (a big store's per-tick coverage scan is O(epochs));
        synchronous fallback keeps headless/tests unchanged."""
        srcs = list(self._sources)

        def apply(cov):
            if not _alive(self.ribbon):              # delivered after teardown → drop
                return
            cov = {k: cov[k] for k in srcs if k in cov}
            if cov != self._cover:                    # redraw bars only when changed
                self._cover = cov
                self.ribbon.set_coverage(cov)

        if self._reads is not None:
            self._reads.coverage_many(srcs, ttl=2.0, key="tl-coverage",
                                      on_result=apply)
        else:
            apply({k: self.resolver.coverage(k) for k in srcs})

    def _refresh_video_coverage(self) -> None:
        """Repull ambient-video coverage for the ribbon's video lanes — OFF the
        paint thread (per-camera index.json parse is O(segments); §9.3). Local
        disk only (no network), delivered under the _alive guard."""
        vs = self._video_store
        if vs is None:
            return

        def work(_ctx):
            try:
                return {c: vs.coverage(c) for c in vs.cameras()}
            except Exception:                # noqa: BLE001
                return {}

        def done(cov):
            if not _alive(self.ribbon):       # window torn down mid-flight → drop
                return
            if cov != self._video_cover:
                self._video_cover = cov
                self.ribbon.set_video_coverage(cov)

        try:
            from .tasks import run_task
            run_task(work, title="Video coverage", on_done=done)
        except Exception:                    # noqa: BLE001 — no runner (headless)
            done(work(None))

    def _recenter(self, t):
        # double-click → centre a window of the current width on t. Use park_window
        # (not park): in GROW mode park() only moves the head, leaving the anchor at
        # launch — so parking into the PAST made window = (min(anchor, head), head)
        # collapse to (head, head), a zero-width line. park_window sets the back edge
        # (the anchor when growing) so the window keeps its width.
        half = max(0.5, self.tc.width / 2)
        self.tc.park_window(t - half, t + half)

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
        # the app's timers do the ticking; here we only flip the motion state
        self.tc.pause() if self.tc.moving else self.tc.play()
        self._sync_transport()

    def _go_live(self):
        self.tc.follow_now()                  # ⦿ Live is an action: jump to + follow now
        self._sync_transport()

    def _sync_transport(self):
        moving = self.tc.moving                            # live counts as playing
        self._play_btn.setText("⏸ Pause" if moving else "▶ Play")
        self._play_btn.setChecked(moving)                  # lit while moving
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
        self._slide_btn.blockSignals(True)
        self._slide_btn.setChecked(not self.tc.grow)
        self._slide_btn.blockSignals(False)

    def _on_tc(self):
        """The shared head moved (us, a play/live tick, or elsewhere) → reflect it
        in the ribbon, preview charts, transport and clock readout."""
        if self._dragging:                    # mid-drag: the preview owns the view,
            self._sync_transport()            # don't let a live tick yank the region
            return
        self.now = time.time()
        self.t0, self.t1 = self.tc.window
        self._syncing = True                  # ribbon update must not re-park tc
        self.ribbon.set_window(self.t0, self.t1)
        if self.tc.following:
            self.ribbon.follow_view(self.t1)  # keep the live edge in view
        self._syncing = False
        self._refresh()
        self._drive_video_preview()
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

    # -- persistence (reopen as left) ----------------------------------------
    def _restore_state(self) -> None:
        """Reopen the Timeline the way it was left — which sources are shown +
        the playback speed. (The data window, slide/grow and head live on the
        shared TimeContext, so they already persist across open/close.)"""
        import json
        raw = QtCore.QSettings("ferroDAC", "ferroDAC").value("timeline/state", "")
        st = {}
        if raw:
            try:
                st = json.loads(raw)
            except Exception:
                st = {}
        checked = set(st.get("checked", []))
        restored = 0
        for i in range(self._src_list.count()):
            it = self._src_list.item(i)
            if it.data(QtCore.Qt.UserRole) in checked:
                it.setCheckState(QtCore.Qt.Checked)      # fires _toggle → builds chart
                restored += 1
        if restored == 0:                                # nothing saved/present → defaults
            for i in range(min(3, self._src_list.count())):
                self._src_list.item(i).setCheckState(QtCore.Qt.Checked)
        sp = st.get("speed")
        if sp and self._speed.findText(f"{int(sp)}×") >= 0:
            self._speed.setCurrentText(f"{int(sp)}×")    # fires → sets tc.speed

    def _save_state(self) -> None:
        import json
        checked = [self._src_list.item(i).data(QtCore.Qt.UserRole)
                   for i in range(self._src_list.count())
                   if self._src_list.item(i).checkState() == QtCore.Qt.Checked]
        QtCore.QSettings("ferroDAC", "ferroDAC").setValue(
            "timeline/state", json.dumps({"checked": checked, "speed": self.tc.speed}))

    def _sync_video_cameras(self) -> None:
        vp = getattr(self, "video_preview", None)
        if vp is not None:
            vp.refresh_cameras()

    def closeEvent(self, ev):
        # leave the head/view exactly as-is — the dockable Player controls the
        # head independently, so closing the scrubber changes nothing.
        self._save_state()                     # remember checked sources + speed
        self._live_timer.stop(); self._preview_timer.stop()
        if getattr(self, "video_preview", None) is not None:
            self.video_preview.stop()
        try:
            self._tc_unsub()
        except Exception:
            pass
        if self._marker_unsub is not None:
            try:
                self._marker_unsub()
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
        mp = max(400, p.width() * 2)
        t0, t1 = self.t0, self.t1

        def draw(res):
            p2 = self._charts.get(key)
            if p2 is None or not _alive(p2) or (t0, t1) != (self.t0, self.t1):
                return                               # stale result / dead widget → drop
            x, y = _envelope_midline(*res)           # clean line, not a zigzag band
            p2._curve.setData(x, y)
            p2.setXRange(t0, t1, padding=0)

        if self._reads is not None:
            # per-series key: a newer scrub supersedes the in-flight query
            self._reads.query(key, t0, t1, mp, key=("tl-query", key),
                              on_result=draw)
        else:
            draw(self.resolver.query(key, t0, t1, max_points=mp))

    def _refresh_waterfall(self, key, p):
        """Render a trace source as a spectrogram over the window (X = TIME, Y =
        swept axis, colour = log intensity). The block read goes off the GUI
        thread via the async facade; the binning + paint run in the callback."""
        t0, t1 = self.t0, self.t1

        def render(blocks):
            p2 = self._charts.get(key)
            if p2 is None or not _alive(p2) or (t0, t1) != (self.t0, self.t1):
                return                               # stale result / dead widget → drop
            self._draw_waterfall(p2, t0, t1, [b for b in blocks if len(b[0])])

        if self._reads is not None:
            self._reads.query_trace(key, t0, t1, 320, key=("tl-wf", key),
                                    on_result=render)
        else:
            render(self.resolver.query_trace(key, t0, t1, max_scans=320))

    def _draw_waterfall(self, p, t0, t1, blocks):
        """Bin the trace blocks by real time and paint the spectrogram — sparse
        scans (slow RGA) keep their true gaps, across ALL epochs in view.
        (t0/t1 == self.t0/self.t1 here: the caller guarded against a moved window.)"""
        from .panels import _time_binned
        x_ref = max(blocks, key=lambda b: b[0][-1])[2] if blocks else None
        scans = []
        for times, Y, x in blocks:
            if x_ref is None or len(x) != len(x_ref):
                continue                                  # different axis length → skip
            z = np.log10(np.clip(Y, 1e-12, None)).astype(np.float32)
            scans.extend((float(times[i]), z[i]) for i in range(len(times)))
        scans.sort(key=lambda s: s[0])
        img, _m = _time_binned(scans, self.t0, self.t1, 320)
        if img is None:
            p._img.clear()
            p.setXRange(self.t0, self.t1, padding=0)
            return
        finite = img[np.isfinite(img)]
        lvl = ([float(np.percentile(finite, 50)), float(finite.max())]
               if finite.size else [0.0, 1.0])
        p._img.setImage(img, autoLevels=False, levels=lvl)   # (time-rows, m) → X=time
        x0, x1 = float(x_ref[0]), float(x_ref[-1])
        p._img.setRect(QtCore.QRectF(self.t0, x0, self.t1 - self.t0, x1 - x0))
        p.setXRange(self.t0, self.t1, padding=0)
