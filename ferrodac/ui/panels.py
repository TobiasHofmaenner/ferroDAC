"""Dashboard panels — display sinks.

Each panel is a data-plane SINK: it subscribes to the engine (via the Dashboard)
and renders the channels routed to it. Panels are uniform: `add_channel` /
`remove_channel` to (un)assign a channel, and `feed(batch)` to consume readings.

v1 panel types: ChartPanel (live plot) and NumericPanel (7-segment readouts).
Input-source panels (sliders/buttons → controls) will land as more types.
"""

from __future__ import annotations

from .. import _qtbinding  # noqa: F401  selects QT_API before qtpy import

from qtpy.QtGui import QPalette
from qtpy.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLCDNumber,
    QVBoxLayout,
    QWidget,
)

import pyqtgraph as pg

from ._common import color_for

pg.setConfigOptions(antialias=True, background="#11151c", foreground="#c7d0db")


class Panel(QWidget):
    """Base class: a sink that shows a routed set of channels."""

    kind = "panel"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.panel_id = ""
        self.title = ""
        self._unsub = None     # set by the Dashboard

    def add_channel(self, key: str, channel) -> None: ...
    def remove_channel(self, key: str) -> None: ...
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

    def add_channel(self, key, channel):
        if key in self._curves:
            return
        self._curves[key] = self.plot.plot(
            [], [], pen=pg.mkPen(color_for(key), width=2), name=channel.name
        )
        self._buf[key] = ([], [])

    def remove_channel(self, key):
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
    """One 7-segment readout: coloured name + LCD + unit."""

    def __init__(self, channel, color: str, parent=None):
        super().__init__(parent)
        self.unit = channel.unit or ""
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(2)
        name = QLabel(channel.name)
        name.setStyleSheet(f"color:{color}; font-weight:700;")
        lay.addWidget(name)
        self.lcd = QLCDNumber()
        self.lcd.setDigitCount(9)
        self.lcd.setSegmentStyle(QLCDNumber.Flat)
        self.lcd.setMinimumHeight(48)
        self.lcd.display("----")
        pal = self.lcd.palette()
        from qtpy.QtGui import QColor
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
        self._placeholder = QLabel("Route channels here.")
        self._placeholder.setStyleSheet("color:#7f8a99;")
        self._outer.addWidget(self._placeholder)
        self._outer.addStretch(1)

    def add_channel(self, key, channel):
        if key in self._readouts:
            return
        ro = _Readout(channel, color_for(key))
        self._readouts[key] = ro
        self._outer.insertWidget(self._outer.count() - 1, ro)
        self._placeholder.setVisible(False)

    def remove_channel(self, key):
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


#: panel kinds available in the "Add" menu
PANEL_TYPES = {"chart": ("Chart", ChartPanel), "numeric": ("7-seg display", NumericPanel)}
