"""Webcam / UVC capture driver — a first-class, built-in Device.

It discovers the host's cameras (Qt Multimedia), exposes each camera's supported
``(resolution, frame-rate)`` formats as a selectable **option**, and streams
frames as a single ``image`` **Source** (the Reading value is a QImage,
normalised to RGB888 so the data plane is pixel-format agnostic and CV-ready).

Qt Multimedia objects are thread-affine. Discovery is safe off-thread, but the
live QCamera must run on the GUI thread, so all capture happens inside a small
controller that we move to the application thread; the Device object itself
stays callable from the manager's worker threads.
"""

from __future__ import annotations

import time

from .. import _qtbinding  # noqa: F401  selects QT_API before qtpy import

from qtpy.QtCore import QCoreApplication, QMetaObject, QObject, Qt, Slot
from qtpy.QtGui import QImage

from ..core.base import BaseDevice
from ..core.device import (
    Interface,
    Modality,
    Option,
    RateControl,
    RateMode,
    Source,
    Status,
)
from ..core.reading import Reading

try:
    from qtpy.QtMultimedia import (
        QCamera,
        QMediaCaptureSession,
        QMediaDevices,
        QVideoSink,
    )
    HAVE_QT_MULTIMEDIA = True
except Exception:  # pragma: no cover - exercised only where bindings are absent
    HAVE_QT_MULTIMEDIA = False


# --------------------------------------------------------------------------- #
#  Format helpers
# --------------------------------------------------------------------------- #
def _format_label(fmt) -> str:
    r = fmt.resolution()
    return f"{r.width()}×{r.height()} @ {fmt.maxFrameRate():.0f} fps"


def _dedup_formats(cam_device) -> list:
    """[(QCameraFormat, label)] deduped by (w, h, fps), sorted by pixels then fps."""
    best: dict = {}
    for f in cam_device.videoFormats():
        r = f.resolution()
        key = (r.width(), r.height(), round(f.maxFrameRate()))
        best.setdefault(key, f)
    ordered = sorted(best.items(), key=lambda kv: (kv[0][0] * kv[0][1], kv[0][2]))
    return [(f, _format_label(f)) for _k, f in ordered]


def _default_format_index(formats: list) -> int:
    """Prefer 1280×720 @ 30 fps as a good quality/throughput balance."""
    for i, (f, _l) in enumerate(formats):
        r = f.resolution()
        if r.width() == 1280 and r.height() == 720 and round(f.maxFrameRate()) == 30:
            return i
    return len(formats) // 2 if formats else 0


# --------------------------------------------------------------------------- #
#  GUI-thread capture controller
# --------------------------------------------------------------------------- #
class _CaptureController(QObject):
    """Owns the live QCamera. Lives on the GUI thread; driven via queued slots."""

    def __init__(self, device: "CameraDevice"):
        super().__init__()
        self._device = device
        self._cam = None
        self._session = None
        self._sink = None

    @Slot()
    def begin(self) -> None:
        dev = self._device._cam_device
        if dev is None:
            return
        self._cam = QCamera(dev)
        fmt = self._device.selected_format()
        if fmt is not None:
            self._cam.setCameraFormat(fmt)
        self._session = QMediaCaptureSession()
        self._sink = QVideoSink()
        self._session.setCamera(self._cam)
        self._session.setVideoOutput(self._sink)
        self._sink.videoFrameChanged.connect(self._on_frame)
        self._cam.errorOccurred.connect(self._on_error)
        self._cam.start()

    @Slot()
    def reconfigure(self) -> None:
        if self._cam is None:
            return
        self._cam.stop()
        fmt = self._device.selected_format()
        if fmt is not None:
            self._cam.setCameraFormat(fmt)
        self._cam.start()

    @Slot()
    def end(self) -> None:
        try:
            if self._sink is not None:
                self._sink.videoFrameChanged.disconnect(self._on_frame)
        except Exception:
            pass
        if self._cam is not None:
            self._cam.stop()
        self._cam = self._session = self._sink = None

    def _on_error(self, *_args) -> None:
        if self._cam is not None:
            self._device._set_error(self._cam.errorString() or "camera error")

    def _on_frame(self, frame) -> None:
        if not frame.isValid():
            return
        img = frame.toImage()
        if img.isNull():
            return
        if img.format() != QImage.Format.Format_RGB888:
            img = img.convertToFormat(QImage.Format.Format_RGB888)
        else:
            img = img.copy()
        emit = self._device._emit
        if emit is not None:
            emit(Reading(self._device.instance_id, "frame", time.time(), img, 0))


# --------------------------------------------------------------------------- #
#  Device
# --------------------------------------------------------------------------- #
class CameraDevice(BaseDevice):
    driver = "camera"
    discoverable = True

    def __init__(self, cam_device, formats: list, instance_id: str, name: str):
        self._cam_device = cam_device
        self._formats = formats
        idx = _default_format_index(formats)
        fps = formats[idx][0].maxFrameRate() if formats else None
        options = [
            Option(
                key="format",
                name="Format",
                choices=tuple((i, lbl) for i, (_f, lbl) in enumerate(formats)),
                value=idx,
            )
        ]
        super().__init__(
            instance_id=instance_id,
            name=name,
            interface=Interface(kind="camera", params={}),
            sources=[Source(id="frame", name="Video",
                            modality=Modality.VIDEO, dtype="image")],
            sinks=(),
            rate=RateControl(mode=RateMode.FIXED, native_hz=fps),
            primary_source="frame",
            hardware_id=instance_id.split("cam:", 1)[-1][:24],
            model="UVC Camera",
            options=options,
        )
        self._rate_hz = fps
        self._controller = None

    @classmethod
    def discover(cls):
        if not HAVE_QT_MULTIMEDIA:
            return []
        out = []
        for dev in QMediaDevices.videoInputs():
            try:
                if dev.isNull():
                    continue
                cid = bytes(dev.id()).decode("utf-8", "replace")
                formats = _dedup_formats(dev)
                out.append(cls(dev, formats, f"cam:{cid}", dev.description()))
            except Exception:
                continue
        return out

    # -- format option -------------------------------------------------------
    def selected_format(self):
        idx = int(self._option_values.get("format", 0) or 0)
        if 0 <= idx < len(self._formats):
            return self._formats[idx][0]
        return None

    def _on_option(self, key: str, value) -> None:
        if key != "format":
            return
        fmt = self.selected_format()
        if fmt is not None:
            self._rate_hz = fmt.maxFrameRate()
            self._rate = RateControl(mode=RateMode.FIXED, native_hz=self._rate_hz)
        if self._controller is not None:
            QMetaObject.invokeMethod(self._controller, "reconfigure", Qt.QueuedConnection)

    def _set_error(self, msg: str) -> None:
        self._status = Status.ERROR
        self._last_error = msg

    # -- lifecycle / data plane (QCamera runs on the GUI thread) -------------
    def _connect(self) -> None:
        if not HAVE_QT_MULTIMEDIA:
            raise RuntimeError("Qt Multimedia is not available")
        self._firmware = None

    def start(self, emit) -> None:
        self._emit = emit
        if self._controller is None:
            self._controller = _CaptureController(self)
            app = QCoreApplication.instance()
            if app is not None:
                self._controller.moveToThread(app.thread())
        QMetaObject.invokeMethod(self._controller, "begin", Qt.QueuedConnection)

    def stop(self) -> None:
        if self._controller is not None:
            QMetaObject.invokeMethod(self._controller, "end", Qt.QueuedConnection)
        self._emit = None
