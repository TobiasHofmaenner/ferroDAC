"""Dashboard panels.

Display panels are sinks (virtual): they subscribe to the engine and render the
Sources routed to them. Input panels are sources (virtual): they drive a device
Sink via ``manager.write``.
"""

from __future__ import annotations

from .. import _qtbinding  # noqa: F401  selects QT_API before qtpy import

from qtpy.QtCore import QRect, Qt, Signal
from qtpy.QtGui import QColor, QImage, QPainter, QPalette
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

import pyqtgraph as pg

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
        self.setMinimumSize(160, 120)

    def set_image(self, img) -> None:
        self._img = img
        self.update()

    def image_size(self):
        if self._img is None or self._img.isNull():
            return None
        return self._img.width(), self._img.height()

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


PANEL_TYPES = {
    "chart": ("Chart", ChartPanel),
    "numeric": ("7-seg display", NumericPanel),
    "image": ("Camera view", ImagePanel),
    "slider": ("Slider", SliderPanel),
    "button": ("Button", ButtonPanel),
    "toggle": ("Toggle", TogglePanel),
}
