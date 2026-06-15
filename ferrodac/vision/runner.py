"""CVRunner — drives all detectors off the GUI thread.

Each detector fires at its **own** ``rate_hz``, and the OCR work runs in a small
thread pool so the slow per-read Tesseract subprocess calls overlap instead of
serialising. At most one read per detector is in flight at a time.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

from .. import _qtbinding  # noqa: F401  selects QT_API before qtpy import
from qtpy.QtCore import QThread

from ..core.reading import Reading
from .ocr import qimage_to_rgb


class CVRunner(QThread):
    def __init__(self, engine, get_detectors, parent=None):
        super().__init__(parent)
        self._engine = engine
        self._get = get_detectors          # callable -> list[Detector]
        self._running = False

    def stop(self) -> None:
        self._running = False

    @staticmethod
    def _do_read(det, img):
        value, _text, status = det.read(qimage_to_rgb(img))
        return value, status

    def run(self) -> None:
        self._running = True
        pool = ThreadPoolExecutor(max_workers=4)
        last: dict = {}        # detector id -> last fire (monotonic)
        inflight: dict = {}    # detector id -> Future
        try:
            while self._running:
                now = time.monotonic()
                for det in self._get():
                    if det.id in inflight:
                        continue
                    rate = max(getattr(det, "rate_hz", 5.0), 0.05)
                    if now - last.get(det.id, 0.0) < 1.0 / rate:
                        continue
                    img = getattr(det.panel, "_last_img", None)
                    if img is None:
                        continue
                    last[det.id] = now
                    inflight[det.id] = pool.submit(self._do_read, det, img)
                for did, fut in list(inflight.items()):
                    if fut.done():
                        try:
                            value, status = fut.result()
                        except Exception:
                            value, status = float("nan"), 1
                        self._engine.publish(Reading("cv", did, time.time(), value, status))
                        del inflight[did]
                self.msleep(20)
        finally:
            pool.shutdown(wait=False)
