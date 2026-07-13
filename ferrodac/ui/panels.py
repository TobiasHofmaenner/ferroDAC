"""Dashboard panels.

Display panels are sinks (virtual): they subscribe to the engine and render the
Sources routed to them. Input panels are sources (virtual): they drive a device
Sink via ``manager.write``.
"""

from __future__ import annotations

from .. import _qtbinding  # noqa: F401  selects QT_API before qtpy import

from qtpy.QtCore import QRect, QRectF, Qt, QTimer
from qtpy.QtGui import QColor, QImage, QPainter, QPalette, QPen, QPixmap
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
    QToolButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

import time

import numpy as np
import pyqtgraph as pg

from ..core import units
from ..core.markers import MEDIA, RECORDING
from ..core.plotbuffer import CurveBuffer
from ..core.trace import Trace
from ..analysis.library import DEFAULT_GASES, LIBRARY
from ._common import color_for, fmt
from .widget import WIDGET_TYPES, Widget

pg.setConfigOptions(antialias=True, background="#11151c", foreground="#c7d0db")


class Panel(Widget):
    """Base class for a BUILT-IN display panel = the public `Widget` contract plus
    any ferroDAC-internal conveniences. Built-ins subclass this; third-party widget
    plugins subclass `Widget` directly (so internal additions here never leak into
    the plugin API). `kind` defaults to "panel" for legacy callers."""

    kind = "panel"


class PanelConfigDialog(QDialog):
    """A generic settings dialog built from a panel's config_fields()."""

    def __init__(self, title, fields, parent=None, on_export=None):
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
        if on_export is not None:                  # plot panels: a sub-dialog for exports
            btn = bb.addButton("Export…", QDialogButtonBox.ActionRole)
            btn.setToolTip("Configure how this panel renders to an image export")
            btn.clicked.connect(lambda: on_export())
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


class ExportConfigDialog(QDialog):
    """Configure plot-export render settings (pixels + DPI) with a LIVE preview.
    Generic: ``render_preview(spec)`` returns a QPixmap rendered at the given size
    (the caller knows how — ImageExporter on the panel), or None for a placeholder.
    Used for a per-panel override (``overridable=True``) or the project default."""

    def __init__(self, spec, render_preview, parent=None, title="Export settings",
                 overridable=False, overriding=False):
        super().__init__(parent)
        self.setWindowTitle(title)
        self._render = render_preview
        spec = spec or {}
        root = QHBoxLayout(self)

        left = QVBoxLayout()
        form = QFormLayout()
        self._w = QSpinBox(); self._w.setRange(64, 10000); self._w.setSingleStep(80)
        self._w.setSuffix(" px"); self._w.setValue(int(spec.get("width", 1600)))
        self._h = QSpinBox(); self._h.setRange(0, 10000); self._h.setSingleStep(80)
        self._h.setSuffix(" px"); self._h.setSpecialValueText("auto")
        self._h.setValue(int(spec.get("height", 0)))
        self._dpi = QSpinBox(); self._dpi.setRange(0, 1200); self._dpi.setSingleStep(10)
        self._dpi.setSuffix(" dpi"); self._dpi.setValue(int(spec.get("dpi", 96)))
        form.addRow("Width", self._w)
        form.addRow("Height", self._h)
        form.addRow("DPI", self._dpi)
        left.addLayout(form)

        self._override = None
        if overridable:
            self._override = QCheckBox("Override the project default")
            self._override.setChecked(bool(overriding))
            self._override.toggled.connect(self._on_override)
            left.addWidget(self._override)

        self._phys = QLabel("")
        self._phys.setStyleSheet("color:#8b95a4; font-size:11px;")
        self._phys.setWordWrap(True)
        left.addWidget(self._phys)
        left.addStretch(1)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        left.addWidget(bb)
        root.addLayout(left)

        self._preview = QLabel("preview")
        self._preview.setAlignment(Qt.AlignCenter)
        self._preview.setMinimumSize(380, 260)
        self._preview.setStyleSheet("background:#0c0f15; border:1px solid #222a36;")
        root.addWidget(self._preview, 1)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(180)              # debounce while the user types
        self._timer.timeout.connect(self._refresh)
        for sb in (self._w, self._h):
            sb.valueChanged.connect(lambda _=0: self._timer.start())
        self._dpi.valueChanged.connect(lambda _=0: self._update_readout())   # text only
        self._on_override()
        self._refresh()

    def _on_override(self, *_):
        on = self._override.isChecked() if self._override is not None else True
        for sb in (self._w, self._h, self._dpi):
            sb.setEnabled(on)
        self._timer.start()

    def _current(self) -> dict:
        return {"width": self._w.value(), "height": self._h.value(),
                "dpi": self._dpi.value()}

    def _update_readout(self):
        s = self._current()
        dpi = s["dpi"] or 96
        w_cm = s["width"] / dpi * 2.54
        if s["height"]:
            self._phys.setText(f"{s['width']}×{s['height']} px · {s['dpi']} dpi · "
                               f"≈ {w_cm:.1f}×{s['height'] / dpi * 2.54:.1f} cm")
        else:
            self._phys.setText(f"{s['width']} px wide · auto height · {s['dpi']} dpi · "
                               f"≈ {w_cm:.1f} cm wide")

    def _refresh(self):
        self._update_readout()
        pm = None
        try:
            pm = self._render(self._current()) if self._render else None
        except Exception:                          # noqa: BLE001
            pm = None
        if isinstance(pm, QPixmap) and not pm.isNull():
            self._preview.setPixmap(pm.scaled(self._preview.size(), Qt.KeepAspectRatio,
                                              Qt.SmoothTransformation))
        else:
            self._preview.setText("no preview")

    def result_spec(self):
        """The chosen spec dict, or None when a per-panel override is toggled OFF
        (meaning: fall back to the project default)."""
        if self._override is not None and not self._override.isChecked():
            return None
        return self._current()


_ARBITRARY = {"", "a.u.", "a_u", "au", "arb", "arbitrary_unit", "dimensionless"}


def _unit_label(unit: str):
    """Axis label for a unit — ``[mbar]`` — or None for an unlabelled/arbitrary unit
    (so a unitless channel doesn't render a meaningless ``[a_u]`` on its axis)."""
    u = (unit or "").strip()
    return f"[{u}]" if u.casefold() not in _ARBITRARY else None


class ChartPanel(Panel):
    kind = "chart"
    accepts = frozenset({"float", "bool"})

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.plot = pg.PlotWidget(
            axisItems={"bottom": pg.DateAxisItem(orientation="bottom")})  # absolute time
        self.plot.showGrid(x=True, y=True, alpha=0.25)
        self.plot.getAxis("bottom").enableAutoSIPrefix(False)
        self.plot.setLogMode(x=False, y=True)
        self._legend = self.plot.addLegend(offset=(-10, 10))
        item = self.plot.getPlotItem()
        # Full-res data is kept; pyqtgraph only downsamples for DISPLAY (zoom in →
        # full detail returns). 'peak' (min/max per pixel column) — NOT 'mean' — because
        # mean re-bins by sample index, so each appended sample slides the bins and the
        # averaged line JUMPS frame-to-frame on high-frequency signals (#10). peak is
        # stable and honestly shows the signal's range in each column; for a clean signal
        # min≈max so it renders as the same line.
        item.setDownsampling(auto=True, mode="peak")
        item.setClipToView(True)
        lay.addWidget(self.plot)
        self._pi = item
        self._curves: dict = {}
        self._buf: dict = {}
        self._conv_of: dict = {}              # key -> conv callable|None (source unit → display)
        self._unit_of: dict = {}              # key -> raw source unit (for dim bookkeeping)
        # ONE physical dimension per chart (Option B — docs/AXIS-DECISION-2026-07). The chart
        # adopts the dimension of its first REAL-unit source; same-dimension sources share the
        # axis (converted, e.g. mbar+Torr); a dimensionally-incompatible source is REFUSED at
        # routing time. This replaces the fragile per-dimension multi-viewbox model that
        # regressed 3× (leftover axes, reload mis-wiring, auto-range death).
        self._dim = None                      # the chart's dimension dimkey (None = none yet)
        self._display_unit = ""               # canonical display unit for the Y axis
        self.on_dim_changed = None            # callback(display_unit): the Dashboard mirrors it
        #                                       onto the SinkPort so the route menu gates by it
        self._t0 = None
        self._ylabel = ""
        self._logy = True
        self._logy_explicit = False           # True once the user toggles it → stops the
        #                                       per-dimension default from overriding
        self.clock = None
        self.markers = None
        self._marker_lines: dict = {}
        # Uncertainty bands (DESIGN §19.0): a shaded value ∓ k·σ per curve, σ from an
        # injected provider (the app wires reconstruct() with a cached model timeline).
        self._sigma_provider = None           # (key, times, values) -> σ array | None
        # Gap breaks (DESIGN §7.4): a coverage(key) -> [(t0,t1), …] provider lets the
        # chart insert a NaN at a recorded-data gap so the curve/band BREAK instead of
        # drawing a straight line across a span with no data. Display-only — the store,
        # the buffer, and the processor bus are never touched (a NaN on the bus would
        # poison a windowed processor); the NaN lives only in the array handed to setData.
        self._gap_provider = None             # key -> [(t0,t1), …] merged coverage
        # Parked/historic curves are drawn ONCE from a pixel-budgeted store query (a
        # min/max envelope — the same reduction the Timeline preview uses), NOT from the
        # full-res re-stream fanned into the CurveBuffer. That removes the second, fixed-cap
        # decimation (the old→new fidelity gradient + zoom starvation) from the parked path;
        # the buffer/peak path stays for the LIVE tail. A `_query_owned` key is owned by
        # the query draw, so feed() ignores the re-stream for it (the re-stream still feeds
        # PROCESSORS — only the chart's own curve changes source). Ownership is assigned
        # ONLY by ChartFeed.reconcile (DESIGN §22 I-6/I-8) via set_query_owned — the chart
        # never decides it. DESIGN §7.4 / §11 / §22.
        self._query_owned: set = set()        # keys drawn from a window query (not feed)
        self._bands: dict = {}                # key -> (lo, hi, fill)
        self._bands_on = False
        self._k = 1.0
        # Cross-panel X (time) link: broadcast a manual pan/zoom so sibling time-charts stay
        # aligned — recovers correlation-across-charts after the one-axis-per-chart change
        # (Option B). The Dashboard wires on_x_range and guards re-entrancy.
        self.on_x_range = None                # callback(t0, t1) — set by the Dashboard
        self._pi.vb.sigXRangeChanged.connect(self._emit_x_range)
        # Zoom re-resolves detail (DESIGN §7.4, "Fix B"): a manual pan/zoom on a PARKED
        # chart re-queries the visible sub-window at pixel resolution, so zooming in returns
        # real store detail instead of magnifying the full-window envelope. Uses
        # sigRangeChangedManually — fires ONLY on user interaction, never on programmatic
        # setData / autorange / the sibling X-link — so it can't loop with them. Debounced so
        # a drag re-queries once it settles.
        self.on_zoom = None                   # callback(t0, t1) — set by the Dashboard
        self._last_zoom_x = None              # last X range re-queried (skip Y-only zooms)
        self._zoom_timer = QTimer(self)
        self._zoom_timer.setSingleShot(True)
        self._zoom_timer.setInterval(150)
        self._zoom_timer.timeout.connect(self._fire_zoom)
        self._pi.vb.sigRangeChangedManually.connect(lambda *_: self._zoom_timer.start())

    def _fire_zoom(self):
        if self.on_zoom is None:
            return
        (t0, t1), _ = self._pi.vb.viewRange()
        if (self._last_zoom_x is not None                 # a Y-only zoom leaves X unchanged →
                and abs(t0 - self._last_zoom_x[0]) < 1e-9  # no re-query (would be wasted store
                and abs(t1 - self._last_zoom_x[1]) < 1e-9):  # reads + redraws for zero change)
            return
        self._last_zoom_x = (float(t0), float(t1))
        self.on_zoom(float(t0), float(t1))

    def config_fields(self):
        return super().config_fields() + [
            ("ylabel", "Y-axis label", "text", self._ylabel, {}),
            ("logy", "Logarithmic Y", "bool", self._logy, {}),
            ("show_sigma", "Show uncertainty band", "bool", self._bands_on, {}),
            ("sigma_2", "Uncertainty at 2σ (95%)", "bool", self._k >= 2.0, {}),
        ]

    def apply_config(self, values):
        super().apply_config(values)
        if "ylabel" in values:
            self._ylabel = values["ylabel"]
            self._apply_primary_label()       # manual label overrides the auto [unit]
        if "logy" in values:
            self._logy = bool(values["logy"])
            self._logy_explicit = True                           # user chose → keep it
            self.plot.setLogMode(x=False, y=self._logy)          # the single Y axis + curves
        if "show_sigma" in values:
            self._bands_on = bool(values["show_sigma"])
        if "sigma_2" in values:
            self._k = 2.0 if values["sigma_2"] else 1.0
        if any(f in values for f in ("logy", "show_sigma", "sigma_2")):
            self._refresh_bands()             # rebuild bands for the new log mode / k / toggle

    def set_display_name(self, name):
        super().set_display_name(name)
        self.plot.setTitle(name or None)

    def state(self):
        d = {"ylabel": self._ylabel,
             "show_sigma": self._bands_on, "sigma_k": self._k}
        if self._logy_explicit:              # only persist a log choice the user MADE, so a
            d["logy"] = self._logy           # restored auto chart keeps the per-dimension default
        return d

    def set_state(self, st):
        cfg = {"ylabel": st.get("ylabel", ""),
               "show_sigma": st.get("show_sigma", False),
               "sigma_2": float(st.get("sigma_k", 1.0)) >= 2.0}
        if "logy" in st:                     # a saved explicit choice (older layouts too)
            cfg["logy"] = bool(st["logy"])
        self.apply_config(cfg)
        self.set_display_name(self.title)    # apply the restored name as plot title

    # -- shared session time base + markers ----------------------------------
    def attach_session(self, clock, markers):
        self.clock = clock
        self.markers = markers
        markers.changed.connect(self._sync_markers)
        self._sync_markers()

    def _x(self, t):
        # ABSOLUTE epoch seconds — plotted on a DateAxis (so a parked window shows
        # real timestamps, identical to the Timeline preview, and there's no
        # origin-rebasing to drift between platforms / live↔replay).
        return t

    def _sync_markers(self):
        if self.markers is None:
            return
        current = {m.id: m for m in self.markers.visible()}   # active project lens
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
            # ignoreBounds: a tag is an annotation, not data — keep it out of "A"
            # auto-range so a tag off to the side can't drag the time axis open.
            self.plot.addItem(line, ignoreBounds=True)
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
            # ignoreBounds: a recording span is an annotation too — exclude it from
            # "A" auto-range (else the whole span is forced on screen).
            self.plot.addItem(reg, ignoreBounds=True)
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
        self.markers.move(mid, entry[0].value())      # x is the absolute timestamp

    def _on_region_drag(self, mid):
        entry = self._marker_lines.get(mid)
        if entry is None or self.markers is None:
            return
        x0, x1 = entry[0].getRegion()                  # absolute timestamps
        self.markers.update(mid, t=min(x0, x1), t_end=max(x0, x1))

    # -- unit-aware axes (DESIGN §19.0): one Y axis per physical dimension ----
    @staticmethod
    def _dimkey(unit):
        """A hashable key for a unit's physical DIMENSION. Parseable units group by
        dimensionality (mbar & Torr → one axis); unparseable ones group by their raw
        label, so identical labels still share but unknown labels don't collide."""
        dim = units.dimensionality(unit)
        if dim is not None:
            return ("dim", str(dim))
        return ("raw", (unit or "").strip().casefold())

    @staticmethod
    def _conv(src, dst):
        """A callable converting magnitudes ``src`` → ``dst`` for display, or None for
        identity. A scalar factor when offset-free (the common case), else pint's full
        convert (handles affine °C↔K)."""
        if not dst or (src or "").strip() == (dst or "").strip():
            return None
        f = units.convert_factor(src, dst)
        if f is not None:
            return lambda y: y * f
        if units.convert(1.0, src, dst) is None:      # not actually convertible
            return None
        return lambda y: units.convert(y, src, dst)

    @staticmethod
    def _default_logy(unit: str) -> bool:
        """Log Y suits PRESSURE (vacuum spans many decades and is strictly positive);
        linear suits everything else (temperature/voltage/… sit in a narrow band and can
        be ≤0, where a log axis shows no ticks — its labels and unit collapse). The user
        can still force either via the config toggle."""
        return units.compatible(unit or "", "Pa")

    def _is_real_dim(self, unit) -> bool:
        """`unit` carries a REAL physical dimension (mbar, °C) vs dimensionless / unknown
        ('', 'a.u.') — which never claims or conflicts with the chart's axis dimension."""
        return self._dimkey(unit) != self._dimkey("")

    def accepts_unit(self, unit) -> bool:
        """Whether a source with `unit` may be routed here: yes if the chart has no dimension
        yet, or the unit is dimensionless, or it shares the chart's dimension. The Dashboard's
        routing gate calls this BEFORE add_source (docs/AXIS-DECISION-2026-07)."""
        return (self._dim is None or not self._is_real_dim(unit)
                or self._dimkey(unit) == self._dim)

    @property
    def display_unit(self) -> str:
        return self._display_unit

    def _adopt_dimension(self, unit) -> None:
        """The chart takes on this (real) dimension for its single Y axis: canonical display
        unit, the per-dimension log/linear default (pressure→log, else linear, until the user
        picks), the axis label, and a notify so the Dashboard mirrors the unit onto the
        SinkPort (the routing menu then greys out incompatible sources)."""
        self._dim = self._dimkey(unit)
        self._display_unit = units.canonical(unit) or unit
        if not self._logy_explicit:
            want = self._default_logy(unit)
            if want != self._logy:
                self._logy = want
                self.plot.setLogMode(x=False, y=self._logy)
        self._apply_primary_label()
        if self.on_dim_changed is not None:
            self.on_dim_changed(self._display_unit)

    def _reset_dimension(self) -> None:
        """The chart emptied → forget its dimension so it can adopt a new one + free the sink."""
        if self._dim is not None:
            self._dim = None
            self._display_unit = ""
            self._apply_primary_label()
            if self.on_dim_changed is not None:
                self.on_dim_changed("")

    def _apply_primary_label(self):
        if self._ylabel:
            self.plot.setLabel("left", self._ylabel)      # manual label wins
            return
        self.plot.setLabel("left", _unit_label(self._display_unit))

    def _set_curve_data(self, key):
        buf = self._buf[key]
        conv = self._conv_of.get(key)
        y = conv(buf.y) if conv is not None else buf.y
        x, y = self._gap_split(key, np.asarray(buf.x), y)   # break the line at real gaps
        self._curves[key].setData(x, y, connect="finite")
        if self._bands_on:
            self._update_band(key)

    # -- uncertainty bands (DESIGN §19.0) ------------------------------------
    def set_sigma_provider(self, fn) -> None:
        """Inject σ(key, times, values) → ndarray|None (the app wires reconstruct with a
        cached timeline). Charts stay decoupled from the store."""
        self._sigma_provider = fn
        self._refresh_bands()

    # -- gap breaks (DESIGN §7.4) --------------------------------------------
    def set_gap_provider(self, fn) -> None:
        """Inject coverage(key) → [(t0,t1), …] (the app wires a cached resolver.coverage).
        A NaN is then inserted at each recorded-data gap so the curve/band break instead
        of drawing a line across it — the same break the Timeline preview shows."""
        self._gap_provider = fn

    def _gap_split(self, key, x, *ys):
        """Return (x, *ys) with a NaN inserted at every recorded-coverage gap inside the
        drawn span, so ``connect="finite"`` breaks the line there. Operates on COPIES
        (``np.insert``) — the CurveBuffer and store are never mutated; recomputed each
        draw from live coverage. All arrays get the break at the SAME positions, so a
        curve and its σ band (and the fill's paired subpaths) stay aligned."""
        fn = self._gap_provider
        if fn is None or x.size < 2:
            return (x, *ys)
        try:
            cov = fn(key)
        except Exception:                              # coverage unavailable → no break
            return (x, *ys)
        if not cov or len(cov) < 2:
            return (x, *ys)
        from ..store.intervals import GAP_JOIN_EPS     # the ONE gap-join test (§22 I-10)
        mids, prev_b = [], cov[0][1]
        for a, b in cov[1:]:
            if a > prev_b + GAP_JOIN_EPS:              # a genuine gap (same test query() uses)
                mids.append(0.5 * (prev_b + a))
            prev_b = max(prev_b, b)
        if not mids:
            return (x, *ys)
        mids = np.asarray(mids, dtype="f8")
        mids = mids[(mids > x[0]) & (mids < x[-1])]    # only gaps within the drawn window
        if mids.size == 0:
            return (x, *ys)
        pos = np.searchsorted(x, mids)                 # insert points, shared across lanes
        nan = np.full(mids.size, np.nan)
        nx = np.insert(x, pos, mids)
        return (nx, *(np.insert(np.asarray(y, dtype="f8"), pos, nan) for y in ys))

    def _ensure_band(self, key):
        if key in self._bands or key not in self._curves:
            return
        lo, hi = pg.PlotDataItem([], []), pg.PlotDataItem([], [])
        lo.setLogMode(False, self._logy)
        hi.setLogMode(False, self._logy)
        c = pg.mkColor(color_for(key))
        c.setAlpha(45)
        fill = pg.FillBetweenItem(lo, hi, brush=pg.mkBrush(c))
        fill.setZValue(-20)                   # behind the curve + grid
        self.plot.addItem(fill)               # single Y axis → always the main viewbox
        self._bands[key] = (lo, hi, fill)

    def _update_band(self, key):
        entry = self._bands.get(key)
        buf = self._buf.get(key)
        if entry is None or buf is None:
            return
        lo, hi, _fill = entry
        if buf.y.size == 0:
            lo.setData([], [])
            hi.setData([], [])
            return
        # Inline σ (a processor output that CREATES uncertainty — the gas fit) wins;
        # otherwise the model provider (device channels reconstruct σ from their model).
        # Inline σ may be asymmetric (fit folded at a physical bound, §19.7); model σ
        # is always symmetric.
        if buf.has_sigma:
            s_lo, s_hi = buf.sigma_lo, buf.sigma_hi
        elif self._sigma_provider is not None:
            sig = self._sigma_provider(key, buf.x, buf.y)
            if sig is None:
                lo.setData([], [])
                hi.setData([], [])
                return
            s_lo = s_hi = np.asarray(sig, dtype=float)
        else:
            lo.setData([], [])
            hi.setData([], [])
            return
        lo_y = buf.y - self._k * np.asarray(s_lo, dtype=float)
        hi_y = buf.y + self._k * np.asarray(s_hi, dtype=float)
        if buf.has_sigma:
            # Inline σ is a fit against a physical x≥0 bound (§19.7): its 1σ edge
            # is pre-clamped at 0, but k>1 would scale straight past the floor.
            lo_y = np.maximum(lo_y, 0.0)
        conv = self._conv_of.get(key)
        if conv is not None:
            lo_y, hi_y = conv(lo_y), conv(hi_y)
        if self._logy:
            # A log axis can't show a ≤0 lower edge — but NaN-ing ONLY the lower
            # curve desynchronises FillBetweenItem's subpath pairing (the band
            # vanishes or bridges gaps — §19.7). Clamp to a floor safely under
            # the drawn data instead, and gap BOTH curves where the upper edge
            # itself is unplottable.
            pos = hi_y > 0
            if not pos.any():
                lo.setData([], [])
                hi.setData([], [])
                return
            floor = float(np.min(hi_y[pos])) * 1e-2
            lo_y = np.where(lo_y > 0, lo_y, floor)
            lo_y = np.where(pos, lo_y, np.nan)
            hi_y = np.where(pos, hi_y, np.nan)
        bad = ~(np.isfinite(lo_y) & np.isfinite(hi_y))    # σ gaps: break BOTH curves
        if bad.any():                                     # at the same samples so the
            lo_y = np.where(bad, np.nan, lo_y)            # fill pairs its subpaths
            hi_y = np.where(bad, np.nan, hi_y)
        # break the band at recorded-data gaps too (same insert positions as the curve,
        # from the same buf.x → the fill's two subpaths stay paired across the gap)
        gx, lo_y, hi_y = self._gap_split(key, np.asarray(buf.x), lo_y, hi_y)
        lo.setData(gx, lo_y, connect="finite")
        hi.setData(gx, hi_y, connect="finite")

    def _remove_band(self, key):
        entry = self._bands.pop(key, None)
        if entry is not None:
            try:
                self.plot.removeItem(entry[2])    # the FillBetweenItem (main viewbox)
            except Exception:
                pass

    def _clear_bands(self):
        for key in list(self._bands):
            self._remove_band(key)

    def _refresh_bands(self):
        self._clear_bands()                   # rebuild (picks up log mode + k changes)
        if self._bands_on:
            for key in list(self._curves):
                self._ensure_band(key)
                self._update_band(key)

    def add_source(self, key, source) -> bool:
        """Route a source onto the chart's single Y axis. Returns True if adopted (or
        already shown), False if REFUSED (a different physical dimension than the chart's —
        the caller drops the route). A dimensionless source is always accepted; the first
        real-unit source claims the chart's dimension; and if a source that bound
        dimensionless later gets a real unit (the reload case), it's adopted in place — no
        viewbox surgery, which is what made the old per-dimension model regress."""
        unit = getattr(source, "unit", "") or ""
        if key in self._curves:                       # already shown — maybe a late unit
            if self._is_real_dim(unit) and self._dim is None:
                self._adopt_dimension(unit)           # reload: real unit arrived after ""
            if self._dim is not None and self._dimkey(unit) == self._dim:
                self._unit_of[key] = unit
                self._conv_of[key] = self._conv(unit, self._display_unit)
                self._set_curve_data(key)             # re-draw with the conversion
            return True
        if not self.accepts_unit(unit):
            return False                              # incompatible dimension → REFUSE
        if self._dim is None and self._is_real_dim(unit):
            self._adopt_dimension(unit)               # first real dimension claims the axis
        name = getattr(source, "label", source.name)
        curve = self.plot.plot([], [], pen=pg.mkPen(color_for(key), width=2), name=name)
        self._curves[key] = curve
        self._buf[key] = CurveBuffer()
        self._unit_of[key] = unit
        self._conv_of[key] = self._conv(unit, self._display_unit)
        if self._bands_on:
            self._ensure_band(key)
        return True

    def remove_source(self, key):
        self._remove_band(key)
        curve = self._curves.pop(key, None)
        self._conv_of.pop(key, None)
        self._unit_of.pop(key, None)
        self._buf.pop(key, None)
        self._query_owned.discard(key)            # else a re-route while parked stays blank
        #                                           (feed() would keep ignoring the stale key)
        if curve is None:
            return
        self.plot.removeItem(curve)
        if self._legend is not None:
            try:
                self._legend.removeItem(curve)
            except Exception:
                pass
        # No real-dimension source left → forget the dimension so the chart can adopt a new
        # one and the Dashboard frees the SinkPort's unit gate.
        if not any(self._is_real_dim(u) for u in self._unit_of.values()):
            self._reset_dimension()

    def feed(self, batch):
        # Accumulate the batch per source, then setData ONCE per source (not per
        # reading) into a bounded numpy buffer — the Tier-1 fix for the week-long
        # slowdown (DESIGN §21). Grow-mode "whole session" stays bounded because
        # the buffer decimates in place at its cap.
        touched: dict = {}
        for r in batch:
            if (r.key not in self._buf or r.key in self._query_owned
                    or not isinstance(r.value, (int, float))):
                continue                          # windowed → the query draw owns this curve
            # log Y needs strictly-positive values; a linear axis accepts any finite one
            ok = (r.status == 0 and r.value == r.value
                  and (r.value > 0 or not self._logy))
            tx, ty, tlo, thi = touched.setdefault(r.key, ([], [], [], []))
            tx.append(self._x(r.t))
            ty.append(r.value if ok else float("nan"))
            s = getattr(r, "sigma", None)         # inline σ from an uncertainty-creating
            if ok and s is not None:              # processor: scalar or (σ_lo, σ_hi)
                s_lo, s_hi = (s if isinstance(s, (tuple, list)) and len(s) == 2
                              else (s, s))
                tlo.append(float(s_lo) if s_lo == s_lo else float("nan"))
                thi.append(float(s_hi) if s_hi == s_hi else float("nan"))
            else:
                tlo.append(float("nan"))
                thi.append(float("nan"))
        for key, (tx, ty, tlo, thi) in touched.items():   # e.g. the gas fit
            buf = self._buf[key]
            # A live time-series display must never step BACKWARD in time: a stray older reading
            # (a device wall-clock correction, or a leftover window envelope) would make the buffer
            # non-monotonic and connect="finite" would draw a diagonal to the out-of-order point.
            # Keep only samples that advance past the running max (buffer end + this batch so far).
            tx_a = np.asarray(tx, dtype="f8")
            floor = float(buf.x[-1]) if len(buf) else float("-inf")
            runmax = np.maximum.accumulate(np.concatenate([[floor], tx_a[:-1]]))
            keep = tx_a > runmax
            if not keep.all():
                if not keep.any():
                    continue
                tx = tx_a[keep]
                ty = np.asarray(ty, dtype="f8")[keep]
                tlo = np.asarray(tlo, dtype="f8")[keep]
                thi = np.asarray(thi, dtype="f8")[keep]
            buf.append(tx, ty, (tlo, thi))
            self._set_curve_data(key)

    def curve_keys(self):
        """The source keys this chart draws (routed curves) — the app queries each
        stored one for the parked window draw."""
        return list(self._curves)

    def set_query_owned(self, keys) -> None:
        """Assign which curves the window QUERY owns — called ONLY by ChartFeed.reconcile
        (DESIGN §22 I-6/I-8); the chart never decides ownership itself. feed() skips owned
        keys, so the re-stream / live tail can't fight the envelope draw. Set BEFORE the
        query draws land (no gradient-decimated flash before the clean envelope).

        Keys LEAVING the set are CLEARED (the old clear-on-Play, now a transition): the
        query envelope spans the whole parked window, and the feed that resumes
        re-experiences that span from its start — appending onto the envelope would make
        the buffer go BACKWARD in time (connect='finite' then draws a diagonal to the
        out-of-order point). Clearing lets feed rebuild monotonically; the curve refills
        within a frame or two. Non-curve keys are ignored."""
        new = {k for k in keys if k in self._curves}
        for key in self._query_owned - new:        # released → clear for the feed
            buf = self._buf.get(key)
            if buf is not None:
                buf.clear()
            if key in self._curves:
                self._curves[key].setData([], [])
        self._query_owned = new

    def set_window_curve(self, key, x, y):
        """Draw a curve from a pre-reduced store-query envelope (min/max polyline, pixel-budgeted).
        Called ONLY by ChartFeed (DESIGN §22 I-6) — never wire another writer to this.
        Fed through the (now non-decimating) buffer so conversion, the σ band, and the coverage
        gap-break all apply unchanged — the buffer just holds the ~2·width envelope points without
        ever hitting its cap. The resolver's own NaN gap markers are stripped here; _set_curve_data
        re-inserts breaks from live coverage.

        Draws only — ownership is assigned separately by set_query_owned. A PARKED chart
        owns its stored curves (feed skips them); the grow-mode extended-back-while-LIVE
        envelope is drawn with the key simply NOT in the owned set, so feed() keeps
        appending the live tail — the feed monotonicity guard drops the redundant older
        re-stream (≤ the envelope's last time) and keeps the forward live points."""
        buf = self._buf.get(key)
        if buf is None:
            return
        x = np.asarray(x, dtype="f8")
        y = np.asarray(y, dtype="f8")
        finite = np.isfinite(x)                    # drop resolver gap markers (gap_split re-adds)
        buf.clear()
        buf.append(x[finite], y[finite])
        self._set_curve_data(key)

    def clear_history(self):
        self._zoom_timer.stop()                   # cancel a pending zoom re-query for the OLD
        self._last_zoom_x = None                  #   window (else it fires _fire_zoom on the
        #                                           stale viewRange and paints the wrong slice,
        #                                           and its shared key kills the fresh park query)
        self._query_owned = set()                 # go-live / new window → feed drives again
        for key, buf in self._buf.items():
            buf.clear()
            self._curves[key].setData([], [])
        for lo, hi, _fill in self._bands.values():
            lo.setData([], [])                # clear σ bands too, else a source with no
            hi.setData([], [])                # data in the new window keeps a stale band
        #                                       drawn as a horizontal span (the re-stream
        #                                       only repaints bands for sources it feeds)
        self._sync_markers()                  # reposition tags at the new time base
        self.plot.enableAutoRange()           # a freshly-loaded slice auto-fits once;
        #                                       then the user's zoom/pan is respected

    def _emit_x_range(self, _vb, rng):
        """The X (time) range changed (a pan/zoom) → tell the Dashboard so it aligns the
        sibling time-charts. Re-entrancy is guarded on the Dashboard side."""
        if self.on_x_range is not None and rng is not None:
            self.on_x_range(float(rng[0]), float(rng[1]))

    def set_x_range(self, t0, t1):
        """Adopt a sibling chart's X (time) range (cross-panel link) — exact, no padding."""
        self.plot.setXRange(t0, t1, padding=0)
        self._last_zoom_x = (float(t0), float(t1))    # stamp so a later Y-only zoom on THIS
        #                                               panel is still recognised as X-unchanged

    def zoom_time(self, t0, t1):
        self.plot.setXRange(t0, t1, padding=0.05)     # time is the X axis here

    def trim_to(self, x_min):
        """Drop buffered points older than x_min so the live window slides instead
        of growing (slide mode). In grow mode the buffer's own cap bounds it."""
        for key, buf in self._buf.items():
            if buf.trim(x_min):
                self._set_curve_data(key)


class _Readout(QFrame):
    def __init__(self, source, color: str, parent=None):
        super().__init__(parent)
        self.unit = source.unit or ""
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(2)
        name = QLabel(getattr(source, 'label', source.name))
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
            [], [], pen=pg.mkPen(color_for(key), width=1.5),
            name=getattr(source, 'label', source.name))

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


def _time_binned(scans, t0, t1, rows, hold=True):
    """Build a `rows`×m spectrogram image over the time window [t0,t1] from
    (timestamp, log-intensity-row) `scans`, mapped to their real TIME bin.

    `hold=True` (sample-and-hold): each scan FILLS the bins from its time until
    the next scan — a continuous waterfall where each band's height = the gap to
    the next measurement (honest: it's the last-known spectrum, no interpolation).
    `hold=False`: one thin row per scan, blank gaps — the bare data points.
    Empty bins are NaN (transparent). Returns (img, m) or (None, 0)."""
    if not scans or t1 <= t0:
        return None, 0
    m = len(scans[-1][1])
    rows_in = sorted(((t, y) for (t, y) in scans if len(y) == m and t0 <= t <= t1),
                     key=lambda s: s[0])
    if not rows_in:
        return None, 0
    # Vectorised (DESIGN §21 Tier-1): the old per-scan Python loop did a sliding
    # np.median AND a full m-wide row copy per scan — 60–70 ms at the 8000-scan cap,
    # re-run every 500 ms tick. Here the median is one strided pass and the row copy
    # is a single gather; only the cheap integer owner-fill stays a loop (it fixes the
    # last-scan-wins overwrite order exactly, so the image is byte-identical).
    n = len(rows_in)
    span = t1 - t0
    ts = np.fromiter((t for t, _ in rows_in), dtype="f8", count=n)
    Y = np.asarray([y for _, y in rows_in], dtype=np.float32)      # (n, m)
    img = np.full((rows, m), np.nan, np.float32)
    i0 = np.clip(((ts - t0) / span * rows).astype(np.int64), 0, rows - 1)
    if not hold:
        img[i0] = Y                                # one row per scan (last wins on dup)
        return img, m
    _K = 8          # half-window of neighbouring gaps for the LOCAL cadence
    t_next = np.empty(n, dtype="f8")
    t_next[-1] = t1
    if n > 1:
        t_next[:-1] = ts[1:]                       # hold until the next scan…
        diffs = np.diff(ts)
        # …but cap the hold at 3× the LOCAL scan cadence (a bigger gap = a real
        # outage → leave it blank). Cadence = median of the ±_K nearby gaps,
        # computed as one strided pass (edge-padded ≈ the old truncated window).
        win = np.lib.stride_tricks.sliding_window_view(
            np.pad(diffs, (_K, _K), mode="edge"), 2 * _K)          # (n, 2K)
        cad = np.median(win, axis=1)
        # interior windows are exact; the padded ≤2K edge scans used replication
        # instead of truncation — recompute just those exactly (cheap, keeps the
        # image byte-identical to the per-scan version).
        for j in [*range(min(_K, n)), *range(max(_K, n - _K), n)]:
            cad[j] = np.median(diffs[max(0, j - _K):min(diffs.size, j + _K)])
        t_next = np.minimum(t_next, ts + np.maximum(3.0 * cad, 1e-9))
    i1 = np.clip(((t_next - t0) / span * rows).astype(np.int64), i0 + 1, rows)
    owner = np.full(rows, -1, dtype=np.int64)
    for a, b, j in zip(i0.tolist(), i1.tolist(), range(n)):
        owner[a:b] = j                             # later scans overwrite (sample-hold)
    covered = owner >= 0
    img[covered] = Y[owner[covered]]               # one gather for the m-wide copy
    return img, m


class WaterfallPanel(Panel):
    """A trace over time as a heatmap (spectrogram): x = swept axis, **y = time**,
    colour = log intensity. The Y range follows the shared timeline window, so
    sparse scans (e.g. a slow RGA sweep) show their real time gaps and record
    markers can overlay. Single-bind — one source per waterfall."""

    kind = "waterfall"
    accepts = frozenset({"trace"})
    single_bind = True

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.plot = pg.PlotWidget(
            axisItems={"left": pg.DateAxisItem(orientation="left")})  # Y = time
        self.plot.setLabel("bottom", "x")
        self.plot.getAxis("bottom").enableAutoSIPrefix(False)
        self.img = pg.ImageItem()
        self.plot.addItem(self.img)
        cmap = _trace_colormap()
        if cmap is not None:
            self.img.setLookupTable(cmap.getLookupTable(0.0, 1.0, 256))
        self._bar = None
        # colour-range lock: auto-fit the levels per slice, but once the user drags
        # the colour bar, HONOUR their range instead of snapping back every tick.
        self._levels_locked = False
        self._levels_user = None
        try:
            self._bar = pg.ColorBarItem(colorMap=cmap)
            self._bar.setImageItem(self.img, insert_in=self.plot.getPlotItem())
            self._bar.sigLevelsChanged.connect(self._on_levels_changed)
        except Exception:
            self._bar = None
        lay.addWidget(self.plot)
        self._src_key = None
        self._scans: list = []            # (timestamp, log-intensity row), absolute time
        self._win = None                  # (t0,t1) Y window from the shared timeline
        self._rows = 400                  # vertical render resolution (time bins)
        self._hold = True                 # sample-and-hold (continuous) vs discrete scans
        self._x0, self._x1 = 0.0, 1.0
        self.markers = None
        self._marker_lines: dict = {}

    def config_fields(self):
        return super().config_fields() + [
            ("hold", "Fill gaps (sample & hold)", "bool", self._hold, {}),
            ("rows", "Resolution (rows)", "int", self._rows, {"min": 64, "max": 2000}),
        ]

    def apply_config(self, values):
        super().apply_config(values)
        if "hold" in values:
            self._hold = bool(values["hold"])
        if values.get("rows"):
            self._rows = max(64, int(values["rows"]))
        self._render()

    def set_display_name(self, name):
        super().set_display_name(name)
        self.plot.setTitle(name or None)

    def state(self):
        return {"rows": self._rows, "hold": self._hold}

    def set_state(self, st):
        if st.get("rows"):
            self._rows = max(64, int(st["rows"]))
        if "hold" in st:
            self._hold = bool(st["hold"])
        self.set_display_name(self.title)

    # -- markers (record regions / tags) on the time (Y) axis ----------------
    def attach_session(self, clock, markers):
        self.markers = markers
        markers.changed.connect(self._sync_markers)
        self._sync_markers()

    def add_source(self, key, source):
        self._src_key = key
        self._scans = []

    def remove_source(self, key):
        if key == self._src_key:
            self._src_key = None
            self._scans = []
            self.img.clear()

    def _on_levels_changed(self):
        """The user dragged the colour bar → lock to their range (until a new slice
        via clear_history re-autos). Programmatic setLevels blocks this signal."""
        if self._bar is not None:
            self._levels_user = tuple(self._bar.levels())
            self._levels_locked = True

    def clear_history(self):
        self._scans = []                  # a fresh slice re-bins from scratch
        self._levels_locked = False       # a new slice deserves fresh auto-levels
        self.img.clear()
        self.plot.enableAutoRange()       # auto-fit the new slice ONCE; then the
        #                                   user's zoom/pan is respected (not forced)

    def set_window(self, t0, t1):
        win = (float(t0), float(t1))
        if win != self._win:
            self._win = win
            self._render()                # grow/scrub → Y follows the timeline window

    def zoom_time(self, t0, t1):
        self.plot.setYRange(t0, t1, padding=0.05)     # time is the Y axis here (m/z is X)

    def trim_to(self, x_min):
        self._scans = [(t, y) for (t, y) in self._scans if t >= x_min]

    def feed(self, batch):
        # EVERY complete scan in the batch (a replay batch carries many) — not
        # just the last, else a parked slice shows only one row.
        added = False
        for r in batch:
            if r.key != self._src_key or not isinstance(r.value, Trace) or r.partial:
                continue
            tr = r.value
            self._scans.append(
                (float(r.t), np.log10(np.clip(tr.y, 1e-12, None)).astype(np.float32)))
            self._x0, self._x1 = float(tr.x[0]), float(tr.x[-1])
            self.plot.setLabel("bottom", _axis_text(tr.x_label, tr.x_unit))
            added = True
        if not added:
            return
        if len(self._scans) > 8000:                  # bound memory on long sessions
            self._scans = self._scans[-8000:]
        self._render()

    def _render(self):
        if not self._scans:
            return
        t0, t1 = self._win if self._win else (self._scans[0][0], self._scans[-1][0])
        if t1 <= t0:
            t1 = t0 + 1.0
        img, _m = _time_binned(self._scans, t0, t1, self._rows, hold=self._hold)
        if img is None:                              # no scan in the window → blank
            self.img.clear()
        else:
            if self._levels_locked and self._levels_user is not None:
                lo, hi = self._levels_user           # honour the user's colour range
            else:
                finite = img[np.isfinite(img)]
                lo = float(np.percentile(finite, 50)) if finite.size else 0.0
                hi = float(finite.max()) if finite.size else lo + 1.0
                if hi <= lo:
                    hi = lo + 1.0
            self.img.setImage(img.T, autoLevels=False, levels=[lo, hi])
            # place the image at its real time/x; the VIEW is the user's (auto-range
            # fits the slice once, then their zoom is kept) — never force it per tick.
            self.img.setRect(QRectF(self._x0, t0, self._x1 - self._x0, t1 - t0))
            if self._bar is not None and not self._levels_locked:
                self._bar.blockSignals(True)         # auto-set must not look like a user drag
                self._bar.setLevels((lo, hi))
                self._bar.blockSignals(False)
        # markers are at absolute time on the Y axis (independent of the image) — they
        # update on markers.changed, NOT every data tick, so a drag isn't fought.

    def _sync_markers(self):
        """Draw markers as HORIZONTAL lines / a shaded region on the time (Y)
        axis — so a recording span or a tag shows where it sits in the scans."""
        if self.markers is None:
            return
        current = {m.id: m for m in self.markers.visible()}   # active project lens
        for mid in list(self._marker_lines):
            if mid not in current:
                self.plot.removeItem(self._marker_lines.pop(mid))
        for mid, m in current.items():
            item = self._marker_lines.get(mid)
            if m.kind == RECORDING and m.t_end is not None:
                if not isinstance(item, pg.LinearRegionItem):
                    if item is not None:
                        self.plot.removeItem(item)
                    item = pg.LinearRegionItem(
                        orientation="horizontal", movable=True,   # drag to retime the span
                        brush=pg.mkBrush(m.color[0], m.color[1], m.color[2], 40)
                        if isinstance(m.color, (tuple, list)) else pg.mkBrush(77, 171, 247, 40))
                    item.setZValue(10)
                    item.sigRegionChangeFinished.connect(
                        lambda _=None, mid=mid: self._on_region_drag(mid))
                    # ignoreBounds: annotation, not data — keep it out of "A"
                    # auto-range (else a far recording forces the time axis open).
                    self.plot.addItem(item, ignoreBounds=True)
                    self._marker_lines[mid] = item
                if item.getRegion() != (m.t, m.t_end):     # don't fight a live drag
                    item.blockSignals(True)
                    item.setRegion((m.t, m.t_end))
                    item.blockSignals(False)
            else:
                if not isinstance(item, pg.InfiniteLine):
                    if item is not None:
                        self.plot.removeItem(item)
                    item = pg.InfiniteLine(
                        angle=0, movable=True,                 # drag to retime the tag
                        pen=pg.mkPen(m.color, width=1.2, style=Qt.DashLine),
                        label=m.label, labelOpts={"color": m.color,
                                                  "fill": (10, 14, 19, 180)})
                    item.setZValue(11)
                    item.sigPositionChangeFinished.connect(
                        lambda _=None, mid=mid: self._on_marker_drag(mid))
                    self.plot.addItem(item, ignoreBounds=True)   # annotation, not data
                    self._marker_lines[mid] = item
                if abs(item.value() - m.t) > 1e-9:
                    item.blockSignals(True)
                    item.setPos(m.t)
                    item.blockSignals(False)

    def _on_marker_drag(self, mid):
        item = self._marker_lines.get(mid)
        if item is not None and self.markers is not None:
            self.markers.move(mid, float(item.value()))   # Y value IS the absolute time

    def _on_region_drag(self, mid):
        item = self._marker_lines.get(mid)
        if item is None or self.markers is None:
            return
        a, b = item.getRegion()
        self.markers.update(mid, t=min(a, b), t_end=max(a, b))


class SpectrumWaterfallPanel(Panel):
    """Spectrum stacked over a waterfall, **sharing one m/z axis**. The live
    line and the spectrogram of past scans line up column-for-column, so a peak
    in the spectrum sits directly above its streak in the waterfall. Single-bind
    (one trace source feeds both)."""

    kind = "specwf"
    accepts = frozenset({"trace"})
    single_bind = True

    _AXIS_W = 74        # equal left-axis width → the two ViewBoxes align in x
    #                     (wide enough for the waterfall's time labels)

    def export_item(self):
        # both stacked plots + the colour bar export as ONE figure (the layout item)
        return self.glw.ci

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

        # -- waterfall (bottom) — Y = time -----------------------------------
        self.p_wf = self.glw.addPlot(
            row=1, col=0, axisItems={"left": pg.DateAxisItem(orientation="left")})
        self.p_wf.setLabel("bottom", "m/z")
        self.p_wf.getAxis("left").setWidth(self._AXIS_W)
        self.p_wf.getAxis("bottom").enableAutoSIPrefix(False)
        self.img = pg.ImageItem()
        self.p_wf.addItem(self.img)
        cmap = _trace_colormap()
        if cmap is not None:
            self.img.setLookupTable(cmap.getLookupTable(0.0, 1.0, 256))
        self._levels_locked = False         # honour a user-dragged colour range (see WaterfallPanel)
        self._levels_user = None
        try:
            self._bar = pg.ColorBarItem(colorMap=cmap)
            self._bar.setImageItem(self.img)
            self.glw.addItem(self._bar, row=1, col=1)   # own column → x stays aligned
            self._bar.sigLevelsChanged.connect(self._on_levels_changed)
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
        self._scans: list = []             # (timestamp, log-intensity row), absolute time
        self._win = None                   # (t0,t1) Y window from the shared timeline
        self._rows = 400                   # vertical render resolution (time bins)
        self._hold = True                  # sample-and-hold (continuous) vs discrete
        self._x0, self._x1, self._xr = 0.0, 1.0, None
        self.markers = None
        self._marker_lines: dict = {}

    # -- configuration -------------------------------------------------------
    def config_fields(self):
        return super().config_fields() + [
            ("logy", "Logarithmic Y (spectrum)", "bool", self._logy, {}),
            ("hold", "Fill gaps (sample & hold)", "bool", self._hold, {}),
            ("rows", "Waterfall resolution (rows)", "int", self._rows,
             {"min": 64, "max": 2000}),
        ]

    def apply_config(self, values):
        super().apply_config(values)
        if "logy" in values:
            self._logy = bool(values["logy"])
            self.p_spec.setLogMode(x=False, y=self._logy)
        if "hold" in values:
            self._hold = bool(values["hold"])
        if values.get("rows"):
            self._rows = max(64, int(values["rows"]))
        self._render_wf()

    def set_display_name(self, name):
        super().set_display_name(name)
        self.p_spec.setTitle(name or None)

    def state(self):
        return {"logy": self._logy, "rows": self._rows, "hold": self._hold}

    def set_state(self, st):
        self.apply_config({"logy": st.get("logy", True)})
        if st.get("rows"):
            self._rows = max(64, int(st["rows"]))
        if "hold" in st:
            self._hold = bool(st["hold"])
        self.set_display_name(self.title)

    # -- markers (record regions / tags) on the waterfall time (Y) axis -------
    def attach_session(self, clock, markers):
        self.markers = markers
        markers.changed.connect(self._sync_markers)
        self._sync_markers()

    # -- data ----------------------------------------------------------------
    def add_source(self, key, source):
        if self._src_key is not None:        # single-bind
            return
        self._src_key = key
        self._prev_curve = self.p_spec.plot(
            [], [], pen=pg.mkPen((120, 130, 145), width=1.0), name="previous")
        self._curves[key] = self.p_spec.plot(
            [], [], pen=pg.mkPen(color_for(key), width=1.5),
            name=getattr(source, 'label', source.name))
        self._scans = []

    def remove_source(self, key):
        if key != self._src_key:
            return
        for curve in (self._curves.pop(key, None), self._prev_curve):
            if curve is not None:
                self.p_spec.removeItem(curve)
        self._prev_curve = None
        self._src_key = None
        self.img.clear()
        self._scans = []

    def _on_levels_changed(self):
        if self._bar is not None:
            self._levels_user = tuple(self._bar.levels())
            self._levels_locked = True

    def clear_history(self):
        self._scans = []                  # a fresh slice re-bins from scratch
        self._levels_locked = False       # a new slice deserves fresh auto-levels
        self.img.clear()
        self.p_wf.enableAutoRange(y=True)  # auto-fit the slice's time span ONCE; the
        #                                    user's zoom is then kept (X is m/z-linked)
        if self._prev_curve is not None:
            self._prev_curve.setData([], [])
        for c in self._curves.values():
            c.setData([], [])

    def set_window(self, t0, t1):
        win = (float(t0), float(t1))
        if win != self._win:
            self._win = win
            self._render_wf()

    def zoom_time(self, t0, t1):
        # time is the waterfall's Y axis; X (m/z) is shared/linked with the spectrum
        # and must be left alone, so frame only the waterfall subplot's Y.
        self.p_wf.setYRange(t0, t1, padding=0.05)

    def trim_to(self, x_min):
        self._scans = [(t, y) for (t, y) in self._scans if t >= x_min]

    def feed(self, batch):
        show = None
        completes = []
        for r in batch:
            if r.key == self._src_key and isinstance(r.value, Trace):
                show = r.value
                if not r.partial:
                    completes.append((float(r.t), r.value))   # (time, scan), replay-safe
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
        # completed scans → dim ghost (last) + one waterfall row per scan, at its time
        last = completes[-1][1]
        cy = np.where(last.y > 0, last.y, np.nan)
        if self._prev_curve is not None:
            self._prev_curve.setData(last.x, cy, connect="finite")
        self._x0, self._x1 = lo, hi
        for t, cscan in completes:
            self._scans.append(
                (t, np.log10(np.clip(cscan.y, 1e-12, None)).astype(np.float32)))
        if len(self._scans) > 8000:
            self._scans = self._scans[-8000:]
        self._render_wf()

    def _render_wf(self):
        if not self._scans:
            return
        t0, t1 = self._win if self._win else (self._scans[0][0], self._scans[-1][0])
        if t1 <= t0:
            t1 = t0 + 1.0
        img, _m = _time_binned(self._scans, t0, t1, self._rows, hold=self._hold)
        if img is None:
            self.img.clear()
        else:
            if self._levels_locked and self._levels_user is not None:
                loL, hiL = self._levels_user            # honour the user's colour range
            else:
                finite = img[np.isfinite(img)]
                loL = float(np.percentile(finite, 50)) if finite.size else 0.0
                hiL = float(finite.max()) if finite.size else loL + 1.0
                if hiL <= loL:
                    hiL = loL + 1.0
            self.img.setImage(img.T, autoLevels=False, levels=[loL, hiL])
            self.img.setRect(QRectF(self._x0, t0, self._x1 - self._x0, t1 - t0))
            if self._bar is not None and not self._levels_locked:
                self._bar.blockSignals(True)
                self._bar.setLevels((loL, hiL))
                self._bar.blockSignals(False)
        # markers update on markers.changed (Y = absolute time), not every data tick

    def _sync_markers(self):
        """Record spans / tags as horizontal lines or a shaded band on the time
        (Y) axis of the waterfall."""
        if self.markers is None:
            return
        current = {m.id: m for m in self.markers.visible()}   # active project lens
        for mid in list(self._marker_lines):
            if mid not in current:
                self.p_wf.removeItem(self._marker_lines.pop(mid))
        for mid, m in current.items():
            item = self._marker_lines.get(mid)
            if m.kind == RECORDING and m.t_end is not None:
                if not isinstance(item, pg.LinearRegionItem):
                    if item is not None:
                        self.p_wf.removeItem(item)
                    br = (m.color if isinstance(m.color, (tuple, list))
                          else (77, 171, 247))
                    item = pg.LinearRegionItem(
                        orientation="horizontal", movable=True,   # drag to retime the span
                        brush=pg.mkBrush(br[0], br[1], br[2], 40))
                    item.setZValue(10)
                    item.sigRegionChangeFinished.connect(
                        lambda _=None, mid=mid: self._on_region_drag(mid))
                    # ignoreBounds: annotation, not data — keep it out of "A"
                    # auto-range (else a far recording forces the time axis open).
                    self.p_wf.addItem(item, ignoreBounds=True)
                    self._marker_lines[mid] = item
                if item.getRegion() != (m.t, m.t_end):     # don't fight a live drag
                    item.blockSignals(True)
                    item.setRegion((m.t, m.t_end))
                    item.blockSignals(False)
            else:
                if not isinstance(item, pg.InfiniteLine):
                    if item is not None:
                        self.p_wf.removeItem(item)
                    item = pg.InfiniteLine(
                        angle=0, movable=True,                 # drag to retime the tag
                        pen=pg.mkPen(m.color, width=1.2, style=Qt.DashLine),
                        label=m.label, labelOpts={"color": m.color,
                                                  "fill": (10, 14, 19, 180)})
                    item.setZValue(11)
                    item.sigPositionChangeFinished.connect(
                        lambda _=None, mid=mid: self._on_marker_drag(mid))
                    self.p_wf.addItem(item, ignoreBounds=True)    # annotation, not data
                    self._marker_lines[mid] = item
                if abs(item.value() - m.t) > 1e-9:
                    item.blockSignals(True)
                    item.setPos(m.t)
                    item.blockSignals(False)

    def _on_marker_drag(self, mid):
        item = self._marker_lines.get(mid)
        if item is not None and self.markers is not None:
            self.markers.move(mid, float(item.value()))

    def _on_region_drag(self, mid):
        item = self._marker_lines.get(mid)
        if item is None or self.markers is None:
            return
        a, b = item.getRegion()
        self.markers.update(mid, t=min(a, b), t_end=max(a, b))

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
        self._placeholder_text = "no video — route a camera here"
        self.setMinimumSize(160, 120)

    def set_placeholder(self, text: str) -> None:
        self._placeholder_text = text
        self.update()

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
            p.drawText(self.rect(), Qt.AlignCenter, self._placeholder_text)
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
        self.on_snapshot = None            # fn(source_key) — the Dashboard wires
        #                                    the app's snapshot service (§9)
        self._snap_btn = QToolButton(self.view)
        self._snap_btn.setText("📷")
        self._snap_btn.setToolTip("Take a photo of this camera (into the "
                                  "project's media/ + a timeline tag)")
        self._snap_btn.setCursor(Qt.PointingHandCursor)
        self._snap_btn.setStyleSheet(
            "QToolButton{background:rgba(20,26,34,190);border:1px solid #3a4250;"
            "border-radius:5px;color:#cdd6e0;font-size:14px;padding:2px 7px;}"
            "QToolButton:hover{background:rgba(46,57,74,230);}")
        self._snap_btn.move(6, 6)          # fixed top-left corner (gear owns top-right)
        self._snap_btn.raise_()
        self._snap_btn.setVisible(False)   # only when a camera is routed
        self._snap_btn.clicked.connect(self._snap)

    def _snap(self):
        if self.on_snapshot is not None and self._src_key:
            self.on_snapshot(self._src_key)

    def add_source(self, key, source):
        self._src_key = key
        self._snap_btn.setVisible(True)
        self._snap_btn.raise_()

    def remove_source(self, key):
        if key == self._src_key:
            self._src_key = None
            self._last_img = None
            self.view.set_image(None)
            self._snap_btn.setVisible(False)

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
    # `emitted` is inherited from the Widget contract (declared once there).

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
        # The MoNA fetch+parse takes MINUTES — run it off the GUI thread so nothing
        # freezes (a status chip shows progress). Guard the callbacks: the dialog may
        # be closed by the time it finishes.
        from ..analysis import library as lib
        from .tasks import run_task

        def done(n):
            try:
                QMessageBox.information(self, "Download", f"Imported {n} compounds.")
                self._refresh_list()
            except RuntimeError:                 # dialog already closed
                pass

        def fail(msg):
            try:
                QMessageBox.warning(
                    self, "Download failed",
                    f"{msg}\n\nThe MoNA link may have changed — download the GC-MS "
                    "MSP from mona.fiehnlab.ucdavis.edu/downloads and use Import MSP….")
            except RuntimeError:
                pass

        run_task(lambda ctx: lib.download_library(),
                 title="Downloading gas library",
                 why="Fetching + parsing the GC-MS EI library from MoNA",
                 exclusive="mona-download", on_busy="reject",
                 on_done=done, on_error=fail)

    def values(self) -> dict:
        return {"mc": self._mc.currentData(),
                "sparsity": round(self._sp.value(), 3),
                "peak_fwhm": round(self._fw.value(), 3),
                "gases": sorted(self._selected) or list(DEFAULT_GASES)}


class _BarsView(QWidget):
    """A reusable labeled bar chart with optional error bars — display only. Fed by
    the generic BarsPanel (routed scalars) or CompositionPanel (gas outputs)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.plot = pg.PlotWidget(
            axisItems={"bottom": _VerticalAxis(orientation="bottom")})
        self.plot.setLabel("left", "value")
        self.plot.showGrid(y=True, alpha=0.2)
        self._bars = pg.BarGraphItem(x=[0], height=[0], width=0.6, brush="#4fc3f7")
        self.plot.addItem(self._bars)
        self._err = pg.ErrorBarItem(pen=pg.mkPen("#c7d0db"))
        self.plot.addItem(self._err)
        lay.addWidget(self.plot)

    def set_bars(self, labels, heights, errors=None, title="", ylabel=None):
        """``errors`` is a per-bar sequence for symmetric ±err, or an ``(lo, hi)``
        pair of sequences for asymmetric errors (e.g. the gas fit's folded
        bootstrap, §19.7). NaN (no information) draws no whisker."""
        n = len(labels)
        x = np.arange(n, dtype=float)
        h = np.asarray(heights, dtype=float) if n else np.array([])
        self._bars.setOpts(x=x, height=h, width=0.6)
        if errors is not None and len(errors):
            if isinstance(errors, tuple) and len(errors) == 2:
                e_lo = np.asarray(errors[0], dtype=float)
                e_hi = np.asarray(errors[1], dtype=float)
            else:
                e_lo = e_hi = np.asarray(errors, dtype=float)
            e_lo = np.nan_to_num(e_lo, nan=0.0)          # no info → no whisker
            e_hi = np.nan_to_num(e_hi, nan=0.0)
            self._err.setData(x=x, y=h, top=e_hi, bottom=np.minimum(e_lo, h),
                              beam=0.25)
        else:
            self._err.setData(x=np.array([]), y=np.array([]))
        short = [s if len(s) <= 18 else s[:17] + "…" for s in labels]
        self.plot.getAxis("bottom").setTicks([list(zip(x.tolist(), short))])
        if ylabel:
            self.plot.setLabel("left", ylabel)
        self.plot.setTitle(title)

    def clear(self):
        self._bars.setOpts(x=[0], height=[0])
        self._err.setData(x=np.array([]), y=np.array([]))
        self.plot.setTitle("")


class BarsPanel(Panel):
    """A generic bar chart: route any scalar (float/bool) sources and each becomes a
    labeled bar of its latest value. The gas-composition display is one specialisation
    of this — point it at a Gas processor's per-gas outputs and you get the same bars."""

    kind = "bars"
    accepts = frozenset({"float", "bool"})

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self._view = _BarsView()
        self.plot = self._view.plot          # so export_item() / exports find the plot
        lay.addWidget(self._view)
        self._labels: dict = {}              # key -> bar label
        self._vals: dict = {}                # key -> latest value
        self._order: list = []               # routed keys, in routing order

    def add_source(self, key, source):
        if key not in self._labels:
            self._order.append(key)
        self._labels[key] = (getattr(source, "label", None)
                             or getattr(source, "name", None) or key)
        self._vals.setdefault(key, 0.0)
        self._redraw()

    def remove_source(self, key):
        self._labels.pop(key, None)
        self._vals.pop(key, None)
        if key in self._order:
            self._order.remove(key)
        self._redraw()

    def feed(self, batch):
        changed = False
        for r in batch:
            if r.key in self._vals and isinstance(r.value, (int, float, bool)):
                self._vals[r.key] = float(r.value)
                changed = True
        if changed:
            self._redraw()

    def _redraw(self):
        labels = [self._labels[k] for k in self._order]
        heights = [self._vals.get(k, 0.0) for k in self._order]
        self._view.set_bars(labels, heights)


class CompositionPanel(Panel):
    """Gas composition: hosts a Dashboard GasAnalyzer on the bound mass-spectrum
    and shows the partial pressures as bars (Monte-Carlo error bars + flagged
    unresolvable pairs) via the shared _BarsView. Because the analyzer is a real
    processor, it also emits a partial-pressure source and a reconstructed-spectrum
    source per gas — route a gas's "fit" source back onto the Spectrum panel to see
    the fit, or its partial-pressure source onto a generic Bars panel. Single-bind."""

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
        self._view = _BarsView()
        self._view.plot.setLabel("left", "partial pressure")
        self.plot = self._view.plot          # so export_item() / exports find the plot
        lay.addWidget(self._view)
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
            self._view.clear()

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
        if a is None:
            return
        # Redraw when the FIT RESULTS land (the derived gas/<id>/<n> readings) —
        # NOT when the raw spectrum does: the fit runs on an offload worker, so at
        # raw-trace time the analyzer still holds the PREVIOUS scan (§19.7: bars
        # lagged one scan; a session's last scan never displayed). Heights and
        # whiskers come from the readings themselves — value + inline σ travel
        # together, race-free; only the title reads analyzer state, which was
        # written before these readings were published.
        prefix = f"gas/{self._proc_id}/"
        got = {r.key[len(prefix):]: r for r in batch
               if r.key.startswith(prefix) and isinstance(r.value, (int, float))}
        if not got:
            return
        names = a.gas_names
        heights, e_lo, e_hi = [], [], []
        for n in names:
            r = got.get(n)
            v = r.value if r is not None else a.last_amounts.get(n, 0.0)
            heights.append(max(0.0, v))
            s = getattr(r, "sigma", None) if r is not None else a.last_sd.get(n)
            if isinstance(s, (tuple, list)) and len(s) == 2:
                e_lo.append(float(s[0]))
                e_hi.append(float(s[1]))
            elif isinstance(s, (int, float)):
                e_lo.append(float(s))
                e_hi.append(float(s))
            else:
                e_lo.append(float("nan"))     # no information → no whisker
                e_hi.append(float("nan"))
        errors = (e_lo, e_hi) if any(v == v for v in e_lo + e_hi) else None
        flags = "   ⚠ unresolved: " + ", ".join(f"{p[0]}↔{p[1]}"
                                                 for p in a.last_degenerate) \
            if a.last_degenerate else ""
        self._view.set_bars(
            names, heights, errors=errors,
            ylabel=f"partial pressure [{a.unit}]" if a.unit else None,
            title=f"fit residual {a.last_residual:.2f}{flags}")

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


class DocPanel(Panel):
    """A document (markdown/LaTeX) view as a dashboard panel — render + edit a file.

    Unlike every other panel it carries NO data routing (``routable = False``): no
    patch-bay port, no data-bus subscription. That lets several coexist, each on its
    own file, and each can be popped into its own window. Needs QtWebEngine; if it's
    absent the panel degrades to a one-line note instead of crashing a layout load.
    """

    kind = "doc"
    routable = False

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self._path = None
        try:
            from .docs import DocView          # lazy: only this panel pulls QtWebEngine
            self._view = DocView()
            lay.addWidget(self._view)
        except Exception as exc:                # noqa: BLE001 — WebEngine not installed
            self._view = None
            note = QLabel("Document view needs QtWebEngine.\n\nInstall:\n"
                          "python3-pyside6.qtwebenginewidgets")
            note.setAlignment(Qt.AlignCenter)
            note.setWordWrap(True)
            note.setStyleSheet("color:#7f8a99; padding:24px;")
            note.setToolTip(str(exc))
            lay.addWidget(note)

    def open(self, path: str) -> None:
        self._path = path
        if self._view is not None:
            self._view.open(path)

    def set_doc_macros(self, on_list_recordings, on_export_recording,
                       on_list_recording_exports=None, on_list_processors=None,
                       on_processor_source=None, on_device_table=None,
                       on_run_meta=None, on_list_cameras=None,
                       on_camera_shot=None) -> None:
        """Wire the editor macros (/rec, /proc, /dev, /meta, /cam) to the app."""
        if self._view is not None:
            self._view.set_macros(on_list_recordings, on_export_recording,
                                  on_list_recording_exports, on_list_processors,
                                  on_processor_source, on_device_table, on_run_meta,
                                  on_list_cameras, on_camera_shot)

    def state(self) -> dict:
        return {"path": self._path} if self._path else {}

    def set_state(self, state: dict) -> None:
        path = state.get("path")
        if path:
            self.open(path)


class PhotoTilePanel(Widget):
    """The photo tile (DESIGN §9 stage b): shows the newest media snapshot AT OR
    BEFORE the shared time window's head — so a parked/scrubbing timeline
    surfaces the time-correlated photo, and LIVE simply shows the latest one.
    Fed by the tag substrate (kind="media" markers), never the data bus: it has
    no data port. The file is resolved through the injected media provider
    (None → the reference is from another box; show the label only)."""

    kind = "imagetile"
    routable = False

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.setSpacing(2)
        self.view = VideoView()
        self.view.set_placeholder("no photo yet — 📷 takes one")
        self._caption = QLabel("")
        self._caption.setStyleSheet("color:#7f8a99; font-size:10px;")
        self._caption.setAlignment(Qt.AlignHCenter)
        lay.addWidget(self.view, 1)
        lay.addWidget(self._caption)
        self._markers = None
        self._resolve = None                 # fn(marker) -> abs path | None
        self._head = None                    # shared window head (None = live/latest)
        self._shown = None                   # marker id currently displayed

    # -- wiring (Widget contract) ---------------------------------------------
    def attach_session(self, clock, markers) -> None:
        self._markers = markers
        markers.changed.connect(self._refresh)
        self._refresh()

    def set_media_provider(self, fn) -> None:
        self._resolve = fn
        self._shown = None                   # a new project → re-resolve
        self._refresh()

    def set_window(self, t0: float, t1: float) -> None:
        """The shared timeline window moved (live tick / scrub / park): follow
        its head so scrubbing surfaces the photo of that moment."""
        self._head = float(t1)
        self._refresh()

    # -- selection + render ------------------------------------------------------
    def _pick(self):
        """The newest media marker at-or-before the head (ties → newest id wins
        deterministically via sort stability)."""
        if self._markers is None:
            return None
        best = None
        for m in self._markers.visible():
            if m.kind != MEDIA:
                continue
            if self._head is not None and m.t > self._head + 1e-9:
                continue
            if best is None or m.t >= best.t:
                best = m
        return best

    def _refresh(self) -> None:
        m = self._pick()
        if m is None:
            if self._shown is not None:
                self._shown = None
                self.view.set_image(None)
                self._caption.setText("")
            return
        if m.id == self._shown:
            return                            # already on screen — no disk I/O
        path = self._resolve(m) if self._resolve is not None else None
        img = QImage(path) if path else QImage()
        self.view.set_image(None if img.isNull() else img)
        when = time.strftime("%H:%M:%S", time.localtime(m.t))
        self._caption.setText(f"{m.label} · {when}" if not img.isNull()
                              else f"{m.label} · {when} (file on another box)")
        self._shown = m.id


# Built-ins register into the shared WIDGET_TYPES registry; PANEL_TYPES is that same
# dict, so plugin widgets (which call register_widget) appear in the Add menu too.
WIDGET_TYPES.update({
    "chart": ("Chart", ChartPanel),
    "numeric": ("7-seg display", NumericPanel),
    "spectrum": ("Spectrum", SpectrumPanel),
    "waterfall": ("Waterfall", WaterfallPanel),
    "specwf": ("Spectrum + waterfall", SpectrumWaterfallPanel),
    "bars": ("Bars", BarsPanel),
    "composition": ("Gas composition", CompositionPanel),
    "image": ("Camera view", ImagePanel),
    "slider": ("Slider", SliderPanel),
    "button": ("Button", ButtonPanel),
    "toggle": ("Toggle", TogglePanel),
    "doc": ("Document", DocPanel),
    "imagetile": ("Photo tile", PhotoTilePanel),
})
PANEL_TYPES = WIDGET_TYPES
