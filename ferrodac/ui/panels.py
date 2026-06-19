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
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLCDNumber,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

import numpy as np
import pyqtgraph as pg

from ..core.markers import RECORDING
from ..core.trace import Trace
from ..analysis.library import DEFAULT_GASES, LIBRARY
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

    def clear_history(self) -> None:
        """Drop accumulated display data so the panel can re-experience a new
        slice from scratch — called by the replay reset when the head jumps
        (park / scrub / return to live). Default: nothing to clear."""

    def trim_to(self, x_min: float) -> None:
        """Drop accumulated data older than x_min (relative-time coords) so the
        live window slides instead of growing. Time-axis panels override."""

    def state(self) -> dict:
        """Per-panel state to persist in a saved session (override as needed)."""
        return {}

    def set_state(self, state: dict) -> None:
        """Restore per-panel state from a saved session."""

    # -- configuration (⚙) ---------------------------------------------------
    def config_fields(self) -> list:
        """Editable settings as ``[(key, label, kind, value, opts)]`` where kind
        is text / int / float / bool / choice. Every panel has a display name."""
        return [("name", "Display name", "text", self.title, {})]

    def apply_config(self, values: dict) -> None:
        if values.get("name"):
            self.set_display_name(values["name"])

    def set_display_name(self, name: str) -> None:
        """Set the panel's name (dock title + patch-bay). Plot panels override to
        also set the plot title so it appears on exported plots."""
        self.title = name


class PanelConfigDialog(QDialog):
    """A generic settings dialog built from a panel's config_fields()."""

    def __init__(self, title, fields, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Configure · {title}")
        self.setMinimumWidth(280)
        form = QFormLayout(self)
        self._w = {}
        for key, label, kind, value, opts in fields:
            w = self._make(kind, value, opts)
            self._w[key] = (kind, w)
            form.addRow(label, w)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)

    @staticmethod
    def _make(kind, value, opts):
        if kind == "bool":
            w = QCheckBox()
            w.setChecked(bool(value))
            return w
        if kind in ("int", "float"):
            w = QSpinBox() if kind == "int" else QDoubleSpinBox()
            w.setRange(opts.get("min", -1e12), opts.get("max", 1e12))
            if kind == "float":
                w.setDecimals(opts.get("decimals", 4))
                w.setSingleStep(opts.get("step", 0.1))
            else:
                w.setSingleStep(int(opts.get("step", 1)))
            if opts.get("suffix"):
                w.setSuffix(opts["suffix"])
            w.setValue(value if value is not None else 0)
            return w
        if kind == "choice":
            w = QComboBox()
            for v, lbl in opts.get("options", []):
                w.addItem(lbl, v)
            ix = w.findData(value)
            if ix >= 0:
                w.setCurrentIndex(ix)
            return w
        w = QLineEdit("" if value is None else str(value))
        return w

    def values(self) -> dict:
        out = {}
        for key, (kind, w) in self._w.items():
            if kind == "bool":
                out[key] = w.isChecked()
            elif kind in ("int", "float"):
                out[key] = w.value()
            elif kind == "choice":
                out[key] = w.currentData()
            else:
                out[key] = w.text()
        return out


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
        self._ylabel = ""
        self._logy = True
        self.clock = None
        self.markers = None
        self._marker_lines: dict = {}

    def config_fields(self):
        return super().config_fields() + [
            ("ylabel", "Y-axis label", "text", self._ylabel, {}),
            ("logy", "Logarithmic Y", "bool", self._logy, {}),
        ]

    def apply_config(self, values):
        super().apply_config(values)
        if "ylabel" in values:
            self._ylabel = values["ylabel"]
            self.plot.setLabel("left", self._ylabel or None)
        if "logy" in values:
            self._logy = bool(values["logy"])
            self.plot.setLogMode(x=False, y=self._logy)

    def set_display_name(self, name):
        super().set_display_name(name)
        self.plot.setTitle(name or None)

    def state(self):
        return {"ylabel": self._ylabel, "logy": self._logy}

    def set_state(self, st):
        self.apply_config({"ylabel": st.get("ylabel", ""),
                           "logy": st.get("logy", True)})
        self.set_display_name(self.title)    # apply the restored name as plot title

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

    def clear_history(self):
        for key, (xs, ys) in self._buf.items():
            xs.clear(); ys.clear()
            self._curves[key].setData([], [])
        self._sync_markers()                  # reposition tags at the new time base
        self.plot.enableAutoRange()           # a freshly-loaded slice auto-fits once;
        #                                       then the user's zoom/pan is respected

    def trim_to(self, x_min):
        """Drop buffered points older than x_min so the live window slides instead
        of growing. xs is time-ordered (live append) → bisect; auto-range follows."""
        import bisect
        for key, (xs, ys) in self._buf.items():
            if xs and xs[0] < x_min:
                i = bisect.bisect_left(xs, x_min)
                if i:
                    del xs[:i]; del ys[:i]
                    self._curves[key].setData(xs, ys, connect="finite")


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
        self._logy = True
        self._cursor_lines: dict = {}      # trend cursors (id -> InfiniteLine)
        self.on_cursor_move = None          # set by the Dashboard

    def config_fields(self):
        return super().config_fields() + [
            ("logy", "Logarithmic Y", "bool", self._logy, {}),
        ]

    def apply_config(self, values):
        super().apply_config(values)
        if "logy" in values:
            self._logy = bool(values["logy"])
            self.plot.setLogMode(x=False, y=self._logy)

    def set_display_name(self, name):
        super().set_display_name(name)
        self.plot.setTitle(name or None)

    def state(self):
        return {"logy": self._logy}

    def set_state(self, st):
        self.apply_config({"logy": st.get("logy", True)})
        self.set_display_name(self.title)

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

    def clear_history(self):
        for store in (self._curves, self._prev_curves):
            for c in store.values():
                c.setData([], [])
        self._last_complete.clear()
        self._xr = None

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

    def config_fields(self):
        return super().config_fields() + [
            ("rows", "History (scans)", "int", self._rows, {"min": 10, "max": 2000}),
        ]

    def apply_config(self, values):
        super().apply_config(values)
        if values.get("rows"):
            self._rows = max(10, int(values["rows"]))
            self._buf = None             # rebuilt at the new height on next scan

    def set_display_name(self, name):
        super().set_display_name(name)
        self.plot.setTitle(name or None)

    def state(self):
        return {"rows": self._rows}

    def set_state(self, st):
        if st.get("rows"):
            self._rows = max(10, int(st["rows"]))
            self._buf = None
        self.set_display_name(self.title)

    def add_source(self, key, source):
        self._src_key = key
        self._buf = None

    def remove_source(self, key):
        if key == self._src_key:
            self._src_key = None
            self._buf = None
            self.img.clear()

    def clear_history(self):
        self._buf = None                  # rebuilt blank on the next replayed scan
        self.img.clear()

    def _push(self, tr):
        """Roll one complete scan into the history buffer."""
        y = np.log10(np.clip(tr.y, 1e-12, None)).astype(np.float32)
        if self._buf is None or self._buf.shape[1] != len(y):
            self._buf = np.full((self._rows, len(y)), float(y.min()), np.float32)
            self._x0, self._x1 = float(tr.x[0]), float(tr.x[-1])
            self.plot.setLabel("bottom", _axis_text(tr.x_label, tr.x_unit))
        self._buf = np.roll(self._buf, -1, axis=0)
        self._buf[-1] = y

    def feed(self, batch):
        # EVERY complete scan in the batch (a replay batch carries many) — not
        # just the last, else a parked slice shows only one row.
        scans = [r.value for r in batch
                 if r.key == self._src_key and isinstance(r.value, Trace)
                 and not r.partial]
        if not scans:
            return
        for tr in scans:
            self._push(tr)
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


class SpectrumWaterfallPanel(Panel):
    """Spectrum stacked over a waterfall, **sharing one m/z axis**. The live
    line and the spectrogram of past scans line up column-for-column, so a peak
    in the spectrum sits directly above its streak in the waterfall. Single-bind
    (one trace source feeds both)."""

    kind = "specwf"
    accepts = frozenset({"trace"})
    single_bind = True

    _AXIS_W = 64        # equal left-axis width → the two ViewBoxes align in x

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.glw = pg.GraphicsLayoutWidget()
        lay.addWidget(self.glw)
        self._logy = True

        # -- spectrum (top) --------------------------------------------------
        self.p_spec = self.glw.addPlot(row=0, col=0)
        self.p_spec.showGrid(x=True, y=True, alpha=0.25)
        self.p_spec.setLabel("left", "Intensity")
        self.p_spec.setLogMode(x=False, y=self._logy)
        self.p_spec.getAxis("left").setWidth(self._AXIS_W)
        self.p_spec.getAxis("bottom").setStyle(showValues=False)   # m/z shown below
        self.p_spec.getAxis("bottom").enableAutoSIPrefix(False)
        self.p_spec.enableAutoRange(x=False, y=True)
        self.p_spec.getViewBox().setAutoVisible(y=True)
        self.p_spec.setClipToView(True)
        self.p_spec.addLegend(offset=(-10, 10))

        # -- waterfall (bottom) ----------------------------------------------
        self.p_wf = self.glw.addPlot(row=1, col=0)
        self.p_wf.setLabel("left", "scan")
        self.p_wf.setLabel("bottom", "m/z")
        self.p_wf.getAxis("left").setWidth(self._AXIS_W)
        self.p_wf.getAxis("bottom").enableAutoSIPrefix(False)
        self.img = pg.ImageItem()
        self.p_wf.addItem(self.img)
        cmap = _trace_colormap()
        if cmap is not None:
            self.img.setLookupTable(cmap.getLookupTable(0.0, 1.0, 256))
        try:
            self._bar = pg.ColorBarItem(colorMap=cmap)
            self._bar.setImageItem(self.img)
            self.glw.addItem(self._bar, row=1, col=1)   # own column → x stays aligned
        except Exception:
            self._bar = None

        self.p_wf.setXLink(self.p_spec)                 # one shared m/z axis
        self.glw.ci.layout.setRowStretchFactor(0, 1)    # spectrum ~⅓
        self.glw.ci.layout.setRowStretchFactor(1, 2)    # waterfall ~⅔

        self._curves: dict = {}            # key -> live spectrum curve (for cursors)
        self._prev_curve = None            # previous completed scan (dim ghost)
        self._cursor_lines: dict = {}
        self.on_cursor_move = None          # set by the Dashboard
        self._src_key = None
        self._buf = None
        self._rows = 240
        self._x0, self._x1, self._xr = 0.0, 1.0, None

    # -- configuration -------------------------------------------------------
    def config_fields(self):
        return super().config_fields() + [
            ("logy", "Logarithmic Y (spectrum)", "bool", self._logy, {}),
            ("rows", "Waterfall history (scans)", "int", self._rows,
             {"min": 10, "max": 2000}),
        ]

    def apply_config(self, values):
        super().apply_config(values)
        if "logy" in values:
            self._logy = bool(values["logy"])
            self.p_spec.setLogMode(x=False, y=self._logy)
        if values.get("rows"):
            self._rows = max(10, int(values["rows"]))
            self._buf = None               # rebuilt at the new height next scan

    def set_display_name(self, name):
        super().set_display_name(name)
        self.p_spec.setTitle(name or None)

    def state(self):
        return {"logy": self._logy, "rows": self._rows}

    def set_state(self, st):
        self.apply_config({"logy": st.get("logy", True)})
        if st.get("rows"):
            self._rows = max(10, int(st["rows"]))
            self._buf = None
        self.set_display_name(self.title)

    # -- data ----------------------------------------------------------------
    def add_source(self, key, source):
        if self._src_key is not None:        # single-bind
            return
        self._src_key = key
        self._prev_curve = self.p_spec.plot(
            [], [], pen=pg.mkPen((120, 130, 145), width=1.0), name="previous")
        self._curves[key] = self.p_spec.plot(
            [], [], pen=pg.mkPen(color_for(key), width=1.5), name=source.name)
        self._buf = None

    def remove_source(self, key):
        if key != self._src_key:
            return
        for curve in (self._curves.pop(key, None), self._prev_curve):
            if curve is not None:
                self.p_spec.removeItem(curve)
        self._prev_curve = None
        self._src_key = None
        self.img.clear()
        self._buf = None

    def clear_history(self):
        self._buf = None
        self.img.clear()
        if self._prev_curve is not None:
            self._prev_curve.setData([], [])
        for c in self._curves.values():
            c.setData([], [])

    def feed(self, batch):
        show = None
        completes = []
        for r in batch:
            if r.key == self._src_key and isinstance(r.value, Trace):
                show = r.value
                if not r.partial:
                    completes.append(r.value)     # EVERY complete scan (replay-safe)
        if show is None or self._src_key not in self._curves:
            return
        # spectrum — current run (bright), log-safe
        y = np.where(show.y > 0, show.y, np.nan)
        self._curves[self._src_key].setData(show.x, y, connect="finite")
        self.p_spec.setLabel("left", _axis_text(show.y_label, show.y_unit))
        self.p_wf.setLabel("bottom", _axis_text(show.x_label, show.x_unit))
        lo = show.x_lo if show.x_lo is not None else float(show.x[0])
        hi = show.x_hi if show.x_hi is not None else float(show.x[-1])
        if hi > lo and self._xr != (lo, hi):
            self.p_spec.setXRange(lo, hi, padding=0)    # waterfall follows via XLink
            self._xr = (lo, hi)
        if not completes:
            return
        # completed scans → dim ghost (last) + one waterfall row per scan
        last = completes[-1]
        cy = np.where(last.y > 0, last.y, np.nan)
        if self._prev_curve is not None:
            self._prev_curve.setData(last.x, cy, connect="finite")
        for cscan in completes:
            wy = np.log10(np.clip(cscan.y, 1e-12, None)).astype(np.float32)
            if self._buf is None or self._buf.shape[1] != len(wy):
                self._buf = np.full((self._rows, len(wy)), float(wy.min()), np.float32)
                self._x0, self._x1 = lo, hi
            self._buf = np.roll(self._buf, -1, axis=0)
            self._buf[-1] = wy
        loL = float(np.percentile(self._buf, 50))
        hiL = float(self._buf.max())
        if hiL <= loL:
            hiL = loL + 1.0
        self.img.setImage(self._buf.T, autoLevels=False, levels=[loL, hiL])
        self.img.setRect(QRectF(self._x0, 0.0, self._x1 - self._x0, float(self._rows)))
        self.p_wf.setYRange(0, self._rows, padding=0)
        if self._bar is not None:
            self._bar.setLevels((loL, hiL))

    # -- trend cursors (mirrors SpectrumPanel, on the spectrum subplot) ------
    def set_cursors(self, cursors):
        current = {c[0]: c for c in cursors}
        for cid in list(self._cursor_lines):
            if cid not in current:
                self.p_spec.removeItem(self._cursor_lines.pop(cid))
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
                self.p_spec.addItem(line)
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
        self._step = 0.001               # value increment per slider tick
        self._name = QLabel("")          # shows the configured display name
        self._name.setStyleSheet("font-weight:600; color:#cdd6e0;")
        self._name.setVisible(False)
        self._lay.addWidget(self._name)
        row = QHBoxLayout()
        self._slider = QSlider(Qt.Horizontal)
        self._slider.valueChanged.connect(self._on_slide)
        self._val = QLabel("—")
        self._val.setStyleSheet("font-family:monospace; font-size:14px;")
        self._val.setMinimumWidth(96)
        row.addWidget(self._slider, 1)
        row.addWidget(self._val)
        host = QWidget()
        host.setLayout(row)
        self._lay.addWidget(host)
        self._reconfigure()

    def set_display_name(self, name):
        super().set_display_name(name)
        self._name.setText(name or "")
        self._name.setVisible(bool(name))

    def config_fields(self):
        return super().config_fields() + [
            ("min", "Minimum", "float", self._min, {}),
            ("max", "Maximum", "float", self._max, {}),
            ("step", "Step", "float", self._step, {}),
            ("unit", "Unit", "text", self._unit, {}),
        ]

    def apply_config(self, values):
        super().apply_config(values)
        if "min" in values:
            self._min = float(values["min"])
        if "max" in values:
            self._max = float(values["max"])
        if values.get("step"):
            self._step = abs(float(values["step"])) or self._step
        if "unit" in values:
            self._unit = values["unit"]
        self._reconfigure()

    def _ticks(self):
        if not self._step:
            return 1000
        return max(1, int(round(abs(self._max - self._min) / self._step)))

    def _reconfigure(self):
        """Map [min, max] onto integer slider ticks of size `step`, preserving
        the current value across the change."""
        cur = self.current_value()
        span = self._max - self._min
        ticks = self._ticks()
        self._slider.blockSignals(True)
        self._slider.setRange(0, ticks)
        frac = (cur - self._min) / span if span else 0.0
        self._slider.setValue(int(round(min(1.0, max(0.0, frac)) * ticks)))
        self._slider.blockSignals(False)
        self._val.setText(fmt(self.current_value(), self._unit))

    def set_range(self, lo, hi, unit):
        # A device sink offers its range when a slider is first bound to it — but
        # only adopt it for a *pristine* (never-configured) slider. A user-set or
        # restored range must survive device rebinds (e.g. on session restore the
        # route re-applies once the device comes back online).
        if not self._is_pristine():
            return
        self._min, self._max, self._unit = lo, hi, unit
        self._step = (hi - lo) / 1000.0 or self._step
        self._reconfigure()

    def _is_pristine(self) -> bool:
        return (self._min == 0.0 and self._max == 1.0
                and abs(self._step - 0.001) < 1e-12 and not self._unit)

    def current_value(self):
        if self._slider.maximum() <= 0:
            return self._min
        return self._min + self._slider.value() * self._step

    def state(self):
        return {"pos": self._slider.value(), "min": self._min, "max": self._max,
                "step": self._step, "unit": self._unit}

    def set_state(self, state):
        # Restore silently: emitting here would push a value computed with the
        # not-yet-set range into the data plane. The route re-sync propagates it.
        self._min = float(state.get("min", self._min))
        self._max = float(state.get("max", self._max))
        self._step = float(state.get("step", self._step)) or self._step
        self._unit = state.get("unit", self._unit)
        self._slider.blockSignals(True)
        self._slider.setRange(0, self._ticks())
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

    def config_fields(self):
        return super().config_fields() + [
            ("label", "Button label", "text", self._btn.text(), {}),
        ]

    def apply_config(self, values):
        super().apply_config(values)
        if values.get("label"):
            self._btn.setText(values["label"])

    def state(self):
        return {"label": self._btn.text()}

    def set_state(self, state):
        if state.get("label"):
            self._btn.setText(state["label"])


class TogglePanel(InputPanel):
    kind = "toggle"
    source_dtype = "bool"

    def _build_body(self):
        self._chk = QCheckBox("On")
        self._chk.toggled.connect(lambda on: self.emitted.emit(on))
        self._lay.addWidget(self._chk)

    def config_fields(self):
        return super().config_fields() + [
            ("label", "Toggle label", "text", self._chk.text(), {}),
        ]

    def apply_config(self, values):
        super().apply_config(values)
        if "label" in values:
            self._chk.setText(values["label"])

    def current_value(self):
        return self._chk.isChecked()

    def state(self):
        return {"on": self._chk.isChecked(), "label": self._chk.text()}

    def set_state(self, state):
        if "label" in state:
            self._chk.setText(state["label"])
        self._chk.blockSignals(True)
        self._chk.setChecked(bool(state.get("on", False)))
        self._chk.blockSignals(False)


class _VerticalAxis(pg.AxisItem):
    """Bottom axis that draws its tick labels vertically — for category names
    (gas labels) that would otherwise collide horizontally."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setHeight(78)

    def drawPicture(self, p, axisSpec, tickSpecs, textSpecs):
        p.setRenderHint(p.RenderHint.Antialiasing, False)
        p.setRenderHint(p.RenderHint.TextAntialiasing, True)
        pen, p1, p2 = axisSpec
        p.setPen(pen)
        p.drawLine(p1, p2)
        for tpen, tp1, tp2 in tickSpecs:
            p.setPen(tpen)
            p.drawLine(tp1, tp2)
        p.setPen(self.textPen())
        for rect, flags, text in textSpecs:
            p.save()
            p.translate(rect.center().x(), rect.top())
            p.rotate(90)                          # read top→down, below the tick
            p.drawText(QRectF(2, -rect.height() / 2.0, 200, rect.height()),
                       int(Qt.AlignVCenter | Qt.AlignLeft), text)
            p.restore()


class GasConfigDialog(QDialog):
    """Configure a gas analysis: Monte-Carlo runs, sparsity, peak width, and
    which gases to fit (the candidate set)."""

    _MC = [("Off (single fit)", 1), ("16", 16), ("32", 32),
           ("64", 64), ("128", 128), ("256", 256)]

    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Gas analysis")
        self.setMinimumWidth(320)
        root = QVBoxLayout(self)
        form = QFormLayout()
        self._mc = QComboBox()
        for label, val in self._MC:
            self._mc.addItem(label, val)
        ix = self._mc.findData(int(cfg.get("mc", 64)) or 1)
        self._mc.setCurrentIndex(ix if ix >= 0 else 3)
        form.addRow("Monte-Carlo", self._mc)
        self._sp = QDoubleSpinBox()
        self._sp.setRange(0.0, 0.3)
        self._sp.setSingleStep(0.01)
        self._sp.setDecimals(2)
        self._sp.setValue(float(cfg.get("sparsity", 0.0)))
        form.addRow("Sparsity", self._sp)
        self._fw = QDoubleSpinBox()
        self._fw.setRange(0.2, 2.0)
        self._fw.setSingleStep(0.1)
        self._fw.setDecimals(2)
        self._fw.setSuffix(" u")
        self._fw.setValue(float(cfg.get("peak_fwhm", 0.7)))
        form.addRow("Peak width", self._fw)
        root.addLayout(form)
        root.addWidget(QLabel("Gases to fit:"))
        self._selected = set(cfg.get("gases") or DEFAULT_GASES)
        srow = QHBoxLayout()
        self._search = QLineEdit()
        self._search.setPlaceholderText("search compounds (name or formula)…")
        self._search.textChanged.connect(self._refresh_list)
        srow.addWidget(self._search, 1)
        imp = QPushButton("Import MSP…")
        imp.setToolTip("Import a NIST/MoNA EI .msp library file")
        imp.clicked.connect(self._import)
        srow.addWidget(imp)
        dl = QPushButton("Download")
        dl.setToolTip("Best-effort fetch of the MoNA GC-MS library")
        dl.clicked.connect(self._download)
        srow.addWidget(dl)
        root.addLayout(srow)
        self._list = QListWidget()
        self._list.setMaximumHeight(180)
        self._list.itemChanged.connect(self._on_item)
        root.addWidget(self._list)
        self._sel_lbl = QLabel()
        self._sel_lbl.setStyleSheet("color:#8b95a4; font-size:11px;")
        root.addWidget(self._sel_lbl)
        self._refresh_list()
        credit = QLabel(
            "Reference cracking patterns from the "
            "<a href='https://webbook.nist.gov/chemistry/'>NIST Chemistry WebBook</a> "
            "(SRD 69) — public-domain U.S. Government data; use here does not imply "
            "endorsement by NIST. Imported libraries from "
            "<a href='https://mona.fiehnlab.ucdavis.edu'>MassBank of North America</a> "
            "(<a href='https://creativecommons.org/licenses/by/4.0/'>CC BY 4.0</a>, "
            "adapted). ferroDAC is not affiliated with or endorsed by NIST or MoNA.")
        credit.setWordWrap(True)
        credit.setOpenExternalLinks(True)
        credit.setStyleSheet("color:#6b7686; font-size:10px;")
        root.addWidget(credit)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        root.addWidget(bb)

    def _refresh_list(self):
        from ..analysis import library as lib
        self._list.blockSignals(True)
        self._list.clear()
        q = self._search.text()
        names = sorted(self._selected) if not q.strip() else []
        seen = set(names)
        for g in lib.search(q, limit=200):
            if g.name not in seen:
                names.append(g.name)
                seen.add(g.name)
        for n in names:
            g = lib.LIBRARY.get(n)
            it = QListWidgetItem(f"{n}  ({g.formula})" if g and g.formula else n)
            it.setData(Qt.UserRole, n)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked if n in self._selected else Qt.Unchecked)
            self._list.addItem(it)
        self._list.blockSignals(False)
        self._sel_lbl.setText(f"{len(self._selected)} selected  ·  "
                              f"{len(lib.LIBRARY)} in library")

    def _on_item(self, it):
        n = it.data(Qt.UserRole)
        if it.checkState() == Qt.Checked:
            self._selected.add(n)
        else:
            self._selected.discard(n)
        self._sel_lbl.setText(f"{len(self._selected)} selected  ·  "
                              f"{len(LIBRARY)} in library")

    def _import(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Import MSP library", "", "MSP (*.msp *.txt);;All files (*)")
        if not path:
            return
        from ..analysis import library as lib
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            n = lib.import_msp(path)
        except Exception as exc:                 # noqa: BLE001 — surface to user
            QApplication.restoreOverrideCursor()
            QMessageBox.warning(self, "Import failed", str(exc))
            return
        QApplication.restoreOverrideCursor()
        QMessageBox.information(self, "Import", f"Imported {n} compounds.")
        self._refresh_list()

    def _download(self):
        from ..analysis import library as lib
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            n = lib.download_library()
        except Exception as exc:                 # noqa: BLE001
            QApplication.restoreOverrideCursor()
            QMessageBox.warning(
                self, "Download failed",
                f"{exc}\n\nThe MoNA link may have changed — download the GC-MS "
                "MSP from mona.fiehnlab.ucdavis.edu/downloads and use Import MSP….")
            return
        QApplication.restoreOverrideCursor()
        QMessageBox.information(self, "Download", f"Imported {n} compounds.")
        self._refresh_list()

    def values(self) -> dict:
        return {"mc": self._mc.currentData(),
                "sparsity": round(self._sp.value(), 3),
                "peak_fwhm": round(self._fw.value(), 3),
                "gases": sorted(self._selected) or list(DEFAULT_GASES)}


class CompositionPanel(Panel):
    """Gas composition: hosts a Dashboard GasAnalyzer on the bound mass-spectrum
    and shows the partial pressures as bars (Monte-Carlo error bars + flagged
    unresolvable pairs). Because the analyzer is a real processor, it also emits
    a partial-pressure source and a reconstructed-spectrum source per gas — route
    a gas's "fit" source back onto the Spectrum panel to see the fit. Single-bind."""

    kind = "composition"
    accepts = frozenset({"trace"})
    single_bind = True

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        hdr = QHBoxLayout()
        hdr.setContentsMargins(4, 2, 4, 0)
        hdr.addStretch(1)
        self._cfg_btn = QPushButton("⚙ Configure")
        self._cfg_btn.setStyleSheet(
            "QPushButton { color:#8b95a4; border:1px solid #2a3340; border-radius:4px;"
            " padding:1px 8px; font-size:11px; } QPushButton:hover { color:#c7d0db; }")
        self._cfg_btn.clicked.connect(self._open_config)
        hdr.addWidget(self._cfg_btn)
        lay.addLayout(hdr)
        self.plot = pg.PlotWidget(axisItems={"bottom": _VerticalAxis(orientation="bottom")})
        self.plot.setLabel("left", "partial pressure")
        self.plot.showGrid(y=True, alpha=0.2)
        self._bars = pg.BarGraphItem(x=[0], height=[0], width=0.6, brush="#4fc3f7")
        self.plot.addItem(self._bars)
        self._err = pg.ErrorBarItem(pen=pg.mkPen("#c7d0db"))
        self.plot.addItem(self._err)
        lay.addWidget(self.plot)
        self._src_key = None
        self._proc_id = None
        # creation config for the hosted analyzer (+ optional gases)
        self._cfg = {"mc": 64, "sparsity": 0.0, "peak_fwhm": 0.7}
        self._add = self._remove = self._get = self._for = None

    def set_processor_host(self, add, remove, get, procs_for):
        """Dashboard wires its processor methods in (called from add_panel)."""
        self._add, self._remove, self._get, self._for = add, remove, get, procs_for

    def add_source(self, key, source):
        self._src_key = key
        if self._for is not None:                 # adopt one restored on import
            existing = self._for(key, "gas")
            if existing:
                self._proc_id = existing[0].id
                return
        if self._add is not None:
            self._proc_id = self._add("gas", key, **self._cfg)

    def remove_source(self, key):
        if key == self._src_key:
            self._src_key = None
            self.cleanup()
            self._bars.setOpts(x=[0], height=[0])
            self.plot.setTitle("")

    def cleanup(self):
        if self._proc_id and self._remove is not None:
            self._remove(self._proc_id)
        self._proc_id = None

    def _current_cfg(self) -> dict:
        a = self._get(self._proc_id) if (self._get and self._proc_id) else None
        if a is not None:
            return {"mc": a.mc, "sparsity": a.sparsity, "peak_fwhm": a.peak_fwhm,
                    "gases": list(a.gas_names)}
        cfg = dict(self._cfg)
        cfg.setdefault("gases", list(DEFAULT_GASES))
        return cfg

    def _open_config(self):
        dlg = GasConfigDialog(self._current_cfg(), self)
        if dlg.exec():
            self._apply_config(dlg.values())

    def _apply_config(self, cfg):
        a = self._get(self._proc_id) if (self._get and self._proc_id) else None
        if a is None:                            # not bound yet — stash for create
            self._cfg = cfg
            return
        if list(cfg["gases"]) != list(a.gas_names):
            # the output set changed → recreate (reuse the id to keep unchanged
            # gases' source keys, so existing routes survive)
            old = self._proc_id
            if self._remove is not None:
                self._remove(old)
            self._cfg = cfg
            self._proc_id = self._add("gas", self._src_key, pid=old, **cfg) \
                if self._add is not None else None
        else:
            a.update(mc=cfg["mc"], sparsity=cfg["sparsity"],
                     peak_fwhm=cfg["peak_fwhm"])
            self._cfg.update(cfg)

    def feed(self, batch):
        if self._proc_id is None or self._get is None:
            return
        a = self._get(self._proc_id)
        if a is None or not any(
                r.key == self._src_key and isinstance(r.value, Trace)
                and not r.partial for r in batch):
            return
        names = a.gas_names
        x = np.arange(len(names), dtype=float)
        h = np.array([max(0.0, a.last_amounts.get(n, 0.0)) for n in names])
        self._bars.setOpts(x=x, height=h, width=0.6)
        if a.last_sd:
            e = np.array([a.last_sd.get(n, 0.0) for n in names])
            self._err.setData(x=x, y=h, top=e, bottom=np.minimum(e, h), beam=0.25)
        else:
            self._err.setData(x=np.array([]), y=np.array([]))
        labels = [n if len(n) <= 18 else n[:17] + "…" for n in names]
        self.plot.getAxis("bottom").setTicks([list(zip(x.tolist(), labels))])
        if a.unit:
            self.plot.setLabel("left", f"partial pressure [{a.unit}]")
        flags = "   ⚠ unresolved: " + ", ".join(f"{p[0]}↔{p[1]}"
                                                 for p in a.last_degenerate) \
            if a.last_degenerate else ""
        self.plot.setTitle(f"fit residual {a.last_residual:.2f}{flags}")

    def state(self):
        a = self._get(self._proc_id) if (self._get and self._proc_id) else None
        if a is not None:
            return {"mc": a.mc, "sparsity": a.sparsity, "gases": a.gas_names,
                    "peak_fwhm": a.peak_fwhm}
        return dict(self._cfg)

    def set_state(self, st):
        self._cfg = {"mc": int(st.get("mc", 64)),
                     "sparsity": float(st.get("sparsity", 0.0)),
                     "peak_fwhm": float(st.get("peak_fwhm", 0.7))}
        if st.get("gases"):
            self._cfg["gases"] = st["gases"]


PANEL_TYPES = {
    "chart": ("Chart", ChartPanel),
    "numeric": ("7-seg display", NumericPanel),
    "spectrum": ("Spectrum", SpectrumPanel),
    "waterfall": ("Waterfall", WaterfallPanel),
    "specwf": ("Spectrum + waterfall", SpectrumWaterfallPanel),
    "composition": ("Gas composition", CompositionPanel),
    "image": ("Camera view", ImagePanel),
    "slider": ("Slider", SliderPanel),
    "button": ("Button", ButtonPanel),
    "toggle": ("Toggle", TogglePanel),
}
