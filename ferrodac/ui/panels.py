"""Dashboard panels.

Display panels are sinks (virtual): they subscribe to the engine and render the
Sources routed to them. Input panels are sources (virtual): they drive a device
Sink via ``manager.write``.
"""

from __future__ import annotations

from .. import _qtbinding  # noqa: F401  selects QT_API before qtpy import

from qtpy.QtCore import Qt
from qtpy.QtGui import QPalette
from qtpy.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLCDNumber,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

import pyqtgraph as pg

from ..core.device import SinkKind
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


class ChartPanel(Panel):
    kind = "chart"

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
            if self._t0 is None:
                self._t0 = r.t
            xs, ys = buf
            xs.append(r.t - self._t0)
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
        if status == 0 and value == value:
            self.lcd.display(f"{value:.4g}")
        else:
            self.lcd.display("----")


class NumericPanel(Panel):
    kind = "numeric"

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
#  Input panels — virtual sources that drive a device Sink
# --------------------------------------------------------------------------- #
class InputPanel(Panel):
    is_input = True
    sink_kind = None   # which SinkKind this input targets

    def __init__(self, manager, parent=None):
        super().__init__(parent)
        self.manager = manager
        self.target = None          # (device_id, sink_id)
        self._options: list = []    # (device_id, sink_id, sink, device_name)

        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(10, 8, 10, 8)
        self._lay.setSpacing(8)
        self._combo = QComboBox()
        self._combo.currentIndexChanged.connect(self._on_target_changed)
        self._lay.addWidget(self._combo)
        self._build_body()
        self._lay.addStretch(1)

    def _build_body(self) -> None: ...
    def _configure_for_target(self) -> None: ...

    def set_options(self, options: list) -> None:
        prev = self.target
        self._options = options
        self._combo.blockSignals(True)
        self._combo.clear()
        self._combo.addItem("— select target —", None)
        sel = 0
        for i, (did, sid, sink, dev) in enumerate(options, start=1):
            self._combo.addItem(f"{dev} · {sink.name}", (did, sid))
            if prev == (did, sid):
                sel = i
        self._combo.setCurrentIndex(sel)
        self._combo.blockSignals(False)
        self.target = self._combo.currentData()
        self._configure_for_target()

    def _on_target_changed(self, _idx):
        self.target = self._combo.currentData()
        self._configure_for_target()

    def _sink(self):
        for did, sid, sink, dev in self._options:
            if (did, sid) == self.target:
                return sink
        return None

    def _apply(self, value):
        if self.target is not None:
            self.manager.write(self.target[0], self.target[1], value)


class SliderPanel(InputPanel):
    kind = "slider"
    sink_kind = SinkKind.SETPOINT

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

    def _configure_for_target(self):
        sink = self._sink()
        self._slider.setEnabled(sink is not None)
        if sink is not None and sink.params:
            p = sink.params[0]
            self._min = p.minimum if p.minimum is not None else 0.0
            self._max = p.maximum if p.maximum is not None else 1.0
            self._unit = p.unit
            cur = sink.value if sink.value is not None else self._min
            self._slider.blockSignals(True)
            span = self._max - self._min
            self._slider.setValue(int((cur - self._min) / span * 1000) if span else 0)
            self._slider.blockSignals(False)
            self._val.setText(fmt(cur, self._unit))
        else:
            self._val.setText("—")

    def _on_slide(self, v):
        val = self._min + (v / 1000.0) * (self._max - self._min)
        self._val.setText(fmt(val, self._unit))
        self._apply(val)


class ButtonPanel(InputPanel):
    kind = "button"
    sink_kind = SinkKind.ACTION

    def _build_body(self):
        self._btn = QPushButton("Trigger")
        self._btn.setMinimumHeight(40)
        self._btn.clicked.connect(lambda: self._apply(None))
        self._lay.addWidget(self._btn)

    def _configure_for_target(self):
        sink = self._sink()
        self._btn.setEnabled(sink is not None)
        self._btn.setText(f"Trigger {sink.name}" if sink else "Trigger")


class TogglePanel(InputPanel):
    kind = "toggle"
    sink_kind = SinkKind.TOGGLE

    def _build_body(self):
        self._chk = QCheckBox("On")
        self._chk.toggled.connect(self._apply)
        self._lay.addWidget(self._chk)

    def _configure_for_target(self):
        sink = self._sink()
        self._chk.setEnabled(sink is not None)
        if sink is not None:
            self._chk.blockSignals(True)
            self._chk.setChecked(bool(sink.value))
            self._chk.blockSignals(False)


PANEL_TYPES = {
    "chart": ("Chart", ChartPanel),
    "numeric": ("7-seg display", NumericPanel),
    "slider": ("Slider", SliderPanel),
    "button": ("Button", ButtonPanel),
    "toggle": ("Toggle", TogglePanel),
}
