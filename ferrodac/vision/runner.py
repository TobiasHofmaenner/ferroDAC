"""CVRunner — a worker thread that drives all detectors off the GUI thread.

Each cycle it grabs the latest frame from each detector's source panel, runs
OCR, and publishes the parsed value into the engine as a Reading. OCR is slow,
so it runs here (never on the GUI thread) at a throttled rate.
"""

from __future__ import annotations

import time

from .. import _qtbinding  # noqa: F401  selects QT_API before qtpy import
from qtpy.QtCore import QThread

from ..core.reading import Reading
from .ocr import qimage_to_rgb


class CVRunner(QThread):
    def __init__(self, engine, get_detectors, rate_hz: float = 5.0, parent=None):
        super().__init__(parent)
        self._engine = engine
        self._get = get_detectors          # callable -> list[Detector]
        self._rate = rate_hz
        self._running = False

    def stop(self) -> None:
        self._running = False

    def run(self) -> None:
        self._running = True
        while self._running:
            cycle = time.monotonic()
            for det in self._get():
                img = getattr(det.panel, "_last_img", None)
                if img is None:
                    continue
                try:
                    rgb = qimage_to_rgb(img)
                    value, _text, status = det.read(rgb)
                except Exception:
                    value, status = float("nan"), 1
                self._engine.publish(
                    Reading("cv", det.id, time.time(), value, status)
                )
            remaining = (1.0 / self._rate) - (time.monotonic() - cycle)
            while self._running and remaining > 0:
                self.msleep(20)
                remaining -= 0.02
