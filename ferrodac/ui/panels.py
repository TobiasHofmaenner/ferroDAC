"""Dashboard panels.

Display panels are sinks (virtual): they subscribe to the engine and render the
Sources routed to them. Input panels are sources (virtual): they drive a device
Sink via ``manager.write``.
"""

from __future__ import annotations

from .. import _qtbinding  # noqa: F401  selects QT_API before qtpy import

from qtpy.QtCore import QRect, QRectF, Qt, Signal
from qtpy.QtGui import QColor, QImage, QPainter, QPalette, QPen
from qtpy.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLCDNumber,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

import numpy as np
import pyqtgraph as pg

from ..core.markers import RECORDING
from ..core.trace import Trace
from ..analysis.gas import GasAnalyzer
from ._common import color_for, fmt

pg.setConfigOptions(antialias=True, background="#11151c", foreground="#c7d0db")


class Panel(QWidget):
    """Base class: a display panel that shows a routed set of Sources."""

    kind = "panel"
    is_input = False

    def __init__(self, parent=None):
        super().__init__(parent)
        self.panel_id = ""
        self.title = ""
        self._unsub = None

    def add_source(self, key: str, source) -> None: ...
    def remove_source(self, key: str) -> None: ...
    def feed(self, batch: list) -> None: ...

    def state(self) -> dict:
        """Per-panel state to persist in a saved session (override as needed)."""
        return {}

    def set_state(self, state: dict) -> None:
        """Restore per-panel state from a saved session."""


class ChartPanel(Panel):
    kind = "chart"
    accepts = frozenset({"float", "bool"})

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.plot = pg.PlotWidget()
        self.plot.showGrid(x=True, y=True, alpha=0.25)
        self.plot.setLabel("bottom", "Time", units="s")
        self.plot.getAxis("bottom").enableAutoSIPrefix(False)
        self.plot.setLogMode(x=False, y=True)
        self.plot.addLegend(offset=(-10, 10))
        item = self.plot.getPlotItem()
        item.setDownsampling(auto=True, mode="peak")
        item.setClipToView(True)
        lay.addWidget(self.plot)
        self._curves: dict = {}
        self._buf: dict = {}
        self._t0 = None
        self.clock = None
        self.markers = None
        self._marker_lines: dict = {}

    # -- shared session time base + markers ----------------------------------
    def attach_session(self, clock, markers):
        self.clock = clock
        self.markers = markers
        markers.changed.connect(self._sync_markers)
        self._sync_markers()

    def _x(self, t):
        if self.clock is not None:
            return self.clock.rel(t)
        if self._t0 is None:
            self._t0 = t
        return t - self._t0

    def _sync_markers(self):
        if self.markers is None:
            return
        current = {m.id: m for m in self.markers.all()}
        for mid in list(self._marker_lines):
            if mid not in current:
                self.plot.removeItem(self._marker_lines.pop(mid)[0])
        for mid, m in current.items():
            want = "region" if (m.kind == RECORDING and m.t_end is not None) else "line"
            entry = self._marker_lines.get(mid)
            if entry is not None and entry[1] != want:    # type changed (live→region)
                self.plot.removeItem(entry[0])
                self._marker_lines.pop(mid, None)
                entry = None
            if want == "region":
                self._sync_region(mid, m, entry)
            else:
                self._sync_line(mid, m, entry)

    def _sync_line(self, mid, m, entry):
        x = self._x(m.t)
        if entry is None:
            line = pg.InfiniteLine(
                pos=x, angle=90, movable=True,
                pen=pg.mkPen(m.color, width=1.2, style=Qt.DashLine),
                label=m.label,
                labelOpts={"position": 0.92, "color": m.color,
                           "fill": (10, 14, 19, 180)})
            line.sigPositionChangeFinished.connect(
                lambda _=None, mid=mid: self._on_marker_drag(mid))
            self.plot.addItem(line)
            self._marker_lines[mid] = (line, "line")
        else:
            line = entry[0]
            if abs(line.value() - x) > 1e-9:
                line.blockSignals(True)
                line.setValue(x)
                line.blockSignals(False)
            try:
                line.label.setFormat(m.label)
            except Exception:
                pass

    def _sync_region(self, mid, m, entry):
        x0, x1 = self._x(m.t), self._x(m.t_end)
        if entry is None:
            reg = pg.LinearRegionItem(
                values=[x0, x1], movable=True,
                brush=pg.mkBrush(255, 107, 107, 38),
                pen=pg.mkPen(m.color, width=1, style=Qt.DashLine))
            reg.setZValue(-10)
            reg.sigRegionChangeFinished.connect(
                lambda _=None, mid=mid: self._on_region_drag(mid))
            self.plot.addItem(reg)
            self._marker_lines[mid] = (reg, "region")
        else:
            reg = entry[0]
            cur = reg.getRegion()
            if abs(cur[0] - x0) > 1e-9 or abs(cur[1] - x1) > 1e-9:
                reg.blockSignals(True)
                reg.setRegion([x0, x1])
                reg.blockSignals(False)

    def set_regions_visible(self, visible: bool) -> None:
        """Hide/show recording-region overlays (used to keep them out of exports)."""
        for item, kind in self._marker_lines.values():
            if kind == "region":
                item.setVisible(visible)

    def _on_marker_drag(self, mid):
        entry = self._marker_lines.get(mid)
        if entry is None or self.markers is None:
            return
        t = self.clock.abs(entry[0].value()) if self.clock else entry[0].value()
        self.markers.move(mid, t)

    def _on_region_drag(self, mid):
        entry = self._marker_lines.get(mid)
        if entry is None or self.markers is None:
            return
        x0, x1 = entry[0].getRegion()
        t0 = self.clock.abs(x0) if self.clock else x0
        t1 = self.clock.abs(x1) if self.clock else x1
        self.markers.update(mid, t=min(t0, t1), t_end=max(t0, t1))

    def add_source(self, key, source):
        if key in self._curves:
            return
        self._curves[key] = self.plot.plot(
            [], [], pen=pg.mkPen(color_for(key), width=2), name=source.name
        )
        self._buf[key] = ([], [])

    def remove_source(self, key):
        curve = self._curves.pop(key, None)
        if curve is not None:
            self.plot.removeItem(curve)
        self._buf.pop(key, None)

    def feed(self, batch):
        for r in batch:
            buf = self._buf.get(r.key)
            if buf is None:
                continue
            if not isinstance(r.value, (int, float)):
                continue
            xs, ys = buf
            xs.append(self._x(r.t))
            ok = r.status == 0 and r.value == r.value and r.value > 0
            ys.append(r.value if ok else float("nan"))
            self._curves[r.key].setData(xs, ys, connect="finite")


class _Readout(QFrame):
    def __init__(self, source, color: str, parent=None):
        super().__init__(parent)
        self.unit = source.unit or ""
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(2)
        name = QLabel(source.name)
        name.setStyleSheet(f"color:{color}; font-weight:700;")
        lay.addWidget(name)
        self.lcd = QLCDNumber()
        self.lcd.setDigitCount(9)
        self.lcd.setSegmentStyle(QLCDNumber.Flat)
        self.lcd.setMinimumHeight(48)
        self.lcd.display("----")
        from qtpy.QtGui import QColor
        pal = self.lcd.palette()
        pal.setColor(QPalette.WindowText, QColor(color))
        self.lcd.setPalette(pal)
        lay.addWidget(self.lcd)
        u = QLabel(self.unit)
        u.setStyleSheet("color:#7f8a99; font-size:10px;")
        lay.addWidget(u)

    def set_value(self, value, status):
        if status == 0 and isinstance(value, (int, float)) and value == value:
            self.lcd.display(f"{value:.4g}")
        else:
            self.lcd.display("----")


class NumericPanel(Panel):
    kind = "numeric"
    accepts = frozenset({"float", "bool"})

    def __init__(self, parent=None):
        super().__init__(parent)
        self._outer = QVBoxLayout(self)
        self._outer.setContentsMargins(6, 6, 6, 6)
        self._outer.setSpacing(6)
        self._readouts: dict = {}
        self._placeholder = QLabel("Route sources here.")
        self._placeholder.setStyleSheet("color:#7f8a99;")
        self._outer.addWidget(self._placeholder)
        self._outer.addStretch(1)

    def add_source(self, key, source):
        if key in self._readouts:
            return
        ro = _Readout(source, color_for(key))
        self._readouts[key] = ro
        self._outer.insertWidget(self._outer.count() - 1, ro)
        self._placeholder.setVisible(False)

    def remove_source(self, key):
        ro = self._readouts.pop(key, None)
        if ro is not None:
            ro.setParent(None)
            ro.deleteLater()
        self._placeholder.setVisible(not self._readouts)

    def feed(self, batch):
        for r in batch:
            ro = self._readouts.get(r.key)
            if ro is not None:
                ro.set_value(r.value, r.status)


# --------------------------------------------------------------------------- #
#  Trace displays — virtual SINKS for a "trace" source (RGA / RF / audio …)
# --------------------------------------------------------------------------- #
def _axis_text(label, unit):
    return f"{label} [{unit}]" if unit else label


def _trace_colormap():
    for name in ("inferno", "viridis", "CET-L17", "CET-L9", "CET-L4"):
        try:
            cm = pg.colormap.get(name)
            if cm is not None:
                return cm
        except Exception:
            continue
    return None


class SpectrumPanel(Panel):
    """A trace as a line — intensity vs its swept axis. Unlike a chart, each scan
    *replaces* the curve rather than scrolling. Log-y (values span decades)."""

    kind = "spectrum"
    accepts = frozenset({"trace"})

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.plot = pg.PlotWidget()
        self.plot.showGrid(x=True, y=True, alpha=0.25)
        self.plot.setLabel("bottom", "x")
        self.plot.setLabel("left", "Intensity")
        self.plot.getAxis("bottom").enableAutoSIPrefix(False)
        self.plot.setLogMode(x=False, y=True)
        # X is pinned to the scan range (set in feed); Y auto-ranges (log-aware,
        # so we don't do manual log math) to the data visible within that X.
        self.plot.enableAutoRange(x=False, y=True)
        self.plot.getViewBox().setAutoVisible(y=True)
        self.plot.addLegend(offset=(-10, 10))
        self.plot.getPlotItem().setClipToView(True)
        lay.addWidget(self.plot)
        self._curves: dict = {}            # current run (bright)
        self._prev_curves: dict = {}       # previous completed run (dim, overlay)
        self._last_complete: dict = {}     # key -> (x, y) of last complete scan
        self._xr = None                    # pinned X range (declared axis extent)
        self._cursor_lines: dict = {}      # trend cursors (id -> InfiniteLine)
        self.on_cursor_move = None          # set by the Dashboard

    def add_source(self, key, source):
        if key in self._curves:
            return
        # previous-run ghost drawn underneath, current run on top
        self._prev_curves[key] = self.plot.plot(
            [], [], pen=pg.mkPen((120, 130, 145), width=1.0), name="previous")
        self._curves[key] = self.plot.plot(
            [], [], pen=pg.mkPen(color_for(key), width=1.5), name=source.name)

    def remove_source(self, key):
        for store in (self._curves, self._prev_curves):
            curve = store.pop(key, None)
            if curve is not None:
                self.plot.removeItem(curve)
        self._last_complete.pop(key, None)

    def feed(self, batch):
        # latest[key] = [trace_to_show, complete_trace_or_None]
        latest: dict = {}
        for r in batch:
            if r.key in self._curves and isinstance(r.value, Trace):
                slot = latest.setdefault(r.key, [None, None])
                slot[0] = r.value
                if not r.partial:
                    slot[1] = r.value
        for key, (tr, complete) in latest.items():
            y = np.where(tr.y > 0, tr.y, np.nan)            # log-safe
            self._curves[key].setData(tr.x, y, connect="finite")   # current (bright)
            self.plot.setLabel("bottom", _axis_text(tr.x_label, tr.x_unit))
            self.plot.setLabel("left", _axis_text(tr.y_label, tr.y_unit))
            # Pin X to the trace's declared range so a partial fill or a stale
            # ghost from a different scan range can't stretch the axis past it.
            lo = tr.x_lo if tr.x_lo is not None else float(tr.x[0])
            hi = tr.x_hi if tr.x_hi is not None else float(tr.x[-1])
            if hi > lo and self._xr != (lo, hi):
                self.plot.setXRange(lo, hi, padding=0.01)
                self._xr = (lo, hi)
            if complete is not None:
                # The finished scan becomes the dim "previous" ghost that the next
                # live-filling run overlays. Redrawn only here (on a full scan).
                cy = np.where(complete.y > 0, complete.y, np.nan)
                prev = self._prev_curves.get(key)
                if prev is not None:
                    prev.setData(complete.x, cy, connect="finite")
                self._last_complete[key] = (complete.x, cy)

    def set_cursors(self, cursors):
        """Draw trend-cursor lines: cursors = [(id, name, mz, value, color)]."""
        current = {c[0]: c for c in cursors}
        for cid in list(self._cursor_lines):
            if cid not in current:
                self.plot.removeItem(self._cursor_lines.pop(cid))
        for cid, (name, mz, value, color) in {c[0]: c[1:] for c in cursors}.items():
            label = f"{name}: {fmt(value)}"
            line = self._cursor_lines.get(cid)
            if line is None:
                line = pg.InfiniteLine(
                    pos=mz, angle=90, movable=True,
                    pen=pg.mkPen(color, width=1, style=Qt.DashLine), label=label,
                    labelOpts={"position": 0.96, "color": color,
                               "fill": (10, 14, 19, 180)})
                line.sigPositionChangeFinished.connect(
                    lambda _=None, cid=cid: self._on_cursor_drag(cid))
                self.plot.addItem(line)
                self._cursor_lines[cid] = line
            else:
                if abs(line.value() - mz) > 1e-6:
                    line.blockSignals(True)
                    line.setValue(mz)
                    line.blockSignals(False)
                try:
                    line.label.setFormat(label)
                except Exception:
                    pass

    def _on_cursor_drag(self, cid):
        line = self._cursor_lines.get(cid)
        if line is not None and self.on_cursor_move is not None:
            self.on_cursor_move(cid, float(line.value()))


class WaterfallPanel(Panel):
    """A trace over time as a heatmap (spectrogram): x = swept axis, y = scan,
    colour = log intensity. Single-bind — one source per waterfall."""

    kind = "waterfall"
    accepts = frozenset({"trace"})
    single_bind = True

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.plot = pg.PlotWidget()
        self.plot.setLabel("bottom", "x")
        self.plot.setLabel("left", "scan")
        self.plot.getAxis("bottom").enableAutoSIPrefix(False)
        self.img = pg.ImageItem()
        self.plot.addItem(self.img)
        cmap = _trace_colormap()
        if cmap is not None:
            self.img.setLookupTable(cmap.getLookupTable(0.0, 1.0, 256))
        self._bar = None
        try:
            self._bar = pg.ColorBarItem(colorMap=cmap)
            self._bar.setImageItem(self.img, insert_in=self.plot.getPlotItem())
        except Exception:
            self._bar = None
        lay.addWidget(self.plot)
        self._src_key = None
        self._buf = None
        self._rows = 240
        self._x0, self._x1 = 0.0, 1.0

    def add_source(self, key, source):
        self._src_key = key
        self._buf = None

    def remove_source(self, key):
        if key == self._src_key:
            self._src_key = None
            self._buf = None
            self.img.clear()

    def feed(self, batch):
        tr = None
        for r in batch:
            if r.key == self._src_key and isinstance(r.value, Trace) \
                    and not r.partial:           # one row per complete scan
                tr = r.value
        if tr is None:
            return
        y = np.log10(np.clip(tr.y, 1e-12, None)).astype(np.float32)
        if self._buf is None or self._buf.shape[1] != len(y):
            self._buf = np.full((self._rows, len(y)), float(y.min()), np.float32)
            self._x0, self._x1 = float(tr.x[0]), float(tr.x[-1])
            self.plot.setLabel("bottom", _axis_text(tr.x_label, tr.x_unit))
        self._buf = np.roll(self._buf, -1, axis=0)
        self._buf[-1] = y
        # levels span baseline → peak so the narrow peaks stay visible
        lo = float(np.percentile(self._buf, 50))
        hi = float(self._buf.max())
        if hi <= lo:
            hi = lo + 1.0
        self.img.setImage(self._buf.T, autoLevels=False, levels=[lo, hi])
        # setImage resets the rect — re-apply the m/z × scan mapping each frame
        self.img.setRect(QRectF(self._x0, 0.0, self._x1 - self._x0, float(self._rows)))
        self.plot.setXRange(self._x0, self._x1, padding=0)
        self.plot.setYRange(0, self._rows, padding=0)
        if self._bar is not None:
            self._bar.setLevels((lo, hi))


# --------------------------------------------------------------------------- #
#  Image display — a virtual SINK for an "image" source (e.g. a camera)
# --------------------------------------------------------------------------- #
class VideoView(QWidget):
    """Paints the latest QImage, scaled to fit while keeping aspect ratio.

    Exposes ``content_rect()`` (the on-screen frame rectangle) and the source
    image size so an overlay can map widget coordinates to image pixels — the
    foundation the CV ROI editor builds on.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._img: QImage | None = None
        self._overlays: list = []     # (text, roi, color, ok) — detector regions
        self.setMinimumSize(160, 120)

    def set_image(self, img) -> None:
        self._img = img
        self.update()

    def set_overlays(self, overlays) -> None:
        self._overlays = overlays
        self.update()

    def image_size(self):
        if self._img is None or self._img.isNull():
            return None
        return self._img.width(), self._img.height()

    def _roi_to_widget(self, roi) -> QRect:
        cr = self.content_rect()
        sz = self.image_size()
        if sz is None:
            return QRect()
        iw, ih = sz
        x, y, w, h = roi
        sx, sy = cr.width() / iw, cr.height() / ih
        return QRect(int(cr.x() + x * sx), int(cr.y() + y * sy),
                     int(w * sx), int(h * sy))

    def content_rect(self) -> QRect:
        """The rectangle the image currently occupies (centred, aspect-fit)."""
        if self._img is None or self._img.isNull():
            return self.rect()
        iw, ih = self._img.width(), self._img.height()
        if iw == 0 or ih == 0:
            return self.rect()
        scale = min(self.width() / iw, self.height() / ih)
        w, h = int(iw * scale), int(ih * scale)
        return QRect((self.width() - w) // 2, (self.height() - h) // 2, w, h)

    def paintEvent(self, _ev):  # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#0b0e13"))
        if self._img is None or self._img.isNull():
            p.setPen(QColor("#5b6b7f"))
            p.drawText(self.rect(), Qt.AlignCenter, "no video — route a camera here")
            return
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        p.drawImage(self.content_rect(), self._img)
        for text, roi, color, ok in self._overlays:
            r = self._roi_to_widget(roi)
            col = QColor(color)
            pen = QPen(col)
            pen.setWidth(2)
            if not ok:
                pen.setStyle(Qt.DashLine)
            p.setPen(pen)
            p.drawRect(r)
            tw = p.fontMetrics().horizontalAdvance(text) + 8
            p.fillRect(QRect(r.x(), r.y() - 16, tw, 15),
                       col if ok else QColor("#3a2f24"))
            p.setPen(QColor("#0b0e13") if ok else QColor("#caa472"))
            p.drawText(r.x() + 4, r.y() - 4, text)


class ImagePanel(Panel):
    """A single-bind display sink: shows the frames of one routed image source."""

    kind = "image"
    accepts = frozenset({"image"})
    single_bind = True

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.view = VideoView()
        lay.addWidget(self.view)
        self._src_key = None
        self._last_img = None

    def add_source(self, key, source):
        self._src_key = key

    def remove_source(self, key):
        if key == self._src_key:
            self._src_key = None
            self._last_img = None
            self.view.set_image(None)

    def feed(self, batch):
        img = None
        for r in batch:
            if r.key == self._src_key and isinstance(r.value, QImage):
                img = r.value
        if img is not None:
            self._last_img = img
            self.view.set_image(img)


# --------------------------------------------------------------------------- #
#  Input panels — virtual SOURCES (emit a value; routed to sinks via the dock)
# --------------------------------------------------------------------------- #
class InputPanel(Panel):
    is_input = True
    source_dtype = "float"

    emitted = Signal(object)   # value (None = trigger, for actions)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(10, 8, 10, 8)
        self._lay.setSpacing(8)
        self._build_body()
        self._lay.addStretch(1)

    def _build_body(self) -> None: ...
    def current_value(self):
        return None


class SliderPanel(InputPanel):
    kind = "slider"
    source_dtype = "float"

    def _build_body(self):
        self._min, self._max, self._unit = 0.0, 1.0, ""
        row = QHBoxLayout()
        self._slider = QSlider(Qt.Horizontal)
        self._slider.setRange(0, 1000)
        self._slider.valueChanged.connect(self._on_slide)
        self._val = QLabel("—")
        self._val.setStyleSheet("font-family:monospace; font-size:14px;")
        self._val.setMinimumWidth(96)
        row.addWidget(self._slider, 1)
        row.addWidget(self._val)
        host = QWidget()
        host.setLayout(row)
        self._lay.addWidget(host)
        self._val.setText(fmt(self.current_value(), self._unit))

    def set_range(self, lo, hi, unit):
        self._min, self._max, self._unit = lo, hi, unit
        self._val.setText(fmt(self.current_value(), self._unit))

    def current_value(self):
        span = self._max - self._min
        return self._min + (self._slider.value() / 1000.0) * span

    def state(self):
        return {"pos": self._slider.value()}

    def set_state(self, state):
        # Restore silently: emitting here would push a value computed with the
        # not-yet-set range into the data plane. The route re-sync propagates it.
        self._slider.blockSignals(True)
        self._slider.setValue(int(state.get("pos", 0)))
        self._slider.blockSignals(False)
        self._val.setText(fmt(self.current_value(), self._unit))

    def _on_slide(self, _v):
        val = self.current_value()
        self._val.setText(fmt(val, self._unit))
        self.emitted.emit(val)


class ButtonPanel(InputPanel):
    kind = "button"
    source_dtype = "action"

    def _build_body(self):
        self._btn = QPushButton("Trigger")
        self._btn.setMinimumHeight(40)
        self._btn.clicked.connect(lambda: self.emitted.emit(None))
        self._lay.addWidget(self._btn)


class TogglePanel(InputPanel):
    kind = "toggle"
    source_dtype = "bool"

    def _build_body(self):
        self._chk = QCheckBox("On")
        self._chk.toggled.connect(lambda on: self.emitted.emit(on))
        self._lay.addWidget(self._chk)

    def current_value(self):
        return self._chk.isChecked()

    def state(self):
        return {"on": self._chk.isChecked()}

    def set_state(self, state):
        self._chk.blockSignals(True)
        self._chk.setChecked(bool(state.get("on", False)))
        self._chk.blockSignals(False)


class CompositionPanel(Panel):
    """Gas composition: deconvolves a bound mass-spectrum into partial pressures
    and shows them as bars. With Monte-Carlo on, each bar carries a 1-sigma error
    bar and unresolvable (anti-correlated) gas pairs are flagged. Single-bind."""

    kind = "composition"
    accepts = frozenset({"trace"})
    single_bind = True

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.plot = pg.PlotWidget()
        self.plot.setLabel("left", "partial pressure")
        self.plot.showGrid(y=True, alpha=0.2)
        self._bars = pg.BarGraphItem(x=[0], height=[0], width=0.6, brush="#4fc3f7")
        self.plot.addItem(self._bars)
        self._err = pg.ErrorBarItem(pen=pg.mkPen("#c7d0db"))
        self.plot.addItem(self._err)
        lay.addWidget(self.plot)
        self._src_key = None
        self._analyzer = GasAnalyzer("comp", "", mc=64)

    def add_source(self, key, source):
        self._src_key = key

    def remove_source(self, key):
        if key == self._src_key:
            self._src_key = None
            self._bars.setOpts(x=[0], height=[0])
            self.plot.setTitle("")

    def feed(self, batch):
        tr = None
        for r in batch:                          # complete scans only
            if r.key == self._src_key and isinstance(r.value, Trace) \
                    and not r.partial:
                tr = r.value
        if tr is None:
            return
        a = self._analyzer
        a.process(tr)
        names = a.gas_names
        x = np.arange(len(names), dtype=float)
        h = np.array([max(0.0, a.last_amounts.get(n, 0.0)) for n in names])
        self._bars.setOpts(x=x, height=h, width=0.6)
        if a.last_sd:
            e = np.array([a.last_sd.get(n, 0.0) for n in names])
            self._err.setData(x=x, y=h, top=e, bottom=np.minimum(e, h), beam=0.25)
        else:
            self._err.setData(x=np.array([]), y=np.array([]))
        self.plot.getAxis("bottom").setTicks([list(zip(x.tolist(), names))])
        if a.unit:
            self.plot.setLabel("left", f"partial pressure [{a.unit}]")
        flags = "   ⚠ unresolved: " + ", ".join(f"{p[0]}↔{p[1]}" for p in
                                                  a.last_degenerate) \
            if a.last_degenerate else ""
        self.plot.setTitle(f"fit residual {a.last_residual:.2f}{flags}")

    def state(self):
        return {"mc": self._analyzer.mc, "sparsity": self._analyzer.sparsity,
                "gases": self._analyzer.gas_names}

    def set_state(self, st):
        self._analyzer.mc = int(st.get("mc", 64))
        self._analyzer.sparsity = float(st.get("sparsity", 0.0))
        if st.get("gases"):
            self._analyzer.update(gases=st["gases"])


PANEL_TYPES = {
    "chart": ("Chart", ChartPanel),
    "numeric": ("7-seg display", NumericPanel),
    "spectrum": ("Spectrum", SpectrumPanel),
    "waterfall": ("Waterfall", WaterfallPanel),
    "composition": ("Gas composition", CompositionPanel),
    "image": ("Camera view", ImagePanel),
    "slider": ("Slider", SliderPanel),
    "button": ("Button", ButtonPanel),
    "toggle": ("Toggle", TogglePanel),
}
