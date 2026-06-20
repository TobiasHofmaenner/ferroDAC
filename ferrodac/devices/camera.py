"""Webcam / UVC capture driver — a first-class, built-in Device.

It discovers the host's cameras (Qt Multimedia), exposes each camera's supported
``(resolution, frame-rate)`` formats as a selectable **option**, and streams
frames as a single ``image`` **Source** (the Reading value is a QImage,
normalised to RGB888 so the data plane is pixel-format agnostic and CV-ready).

Qt Multimedia objects are thread-affine — and `QMediaDevices.videoInputs()` brings
the whole backend up on the CALLING thread, so even *enumeration* must happen on the
GUI thread (we cache it via ``install_camera_enumeration``; ``discover()`` only reads
the cache). The live QCamera likewise runs on the GUI thread, inside a small
controller moved to the application thread; the Device object itself stays callable
from the manager's worker threads.
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
#  GUI-thread camera enumeration
# --------------------------------------------------------------------------- #
# `QMediaDevices.videoInputs()` INITIALISES the platform multimedia backend on the
# CALLING thread. Doing that from the manager's discovery worker pulls Qt Multimedia
# (FFmpeg) up off the GUI thread — fragile and flagged by the diagnostics harness as
# a likely segfault source. So we enumerate on the GUI thread via a QMediaDevices we
# own here, cache EVERYTHING discover() needs (the QCameraDevice handle + its formats,
# id and description are value reads, safe to pass to the worker), and refresh on
# `videoInputsChanged`. discover() (worker thread) then makes ZERO Qt Multimedia calls.
_devices_watcher = None          # QMediaDevices, GUI-thread-owned (kept alive here)
_video_inputs: list = []         # cached [(QCameraDevice, formats, instance_id, name)]


def _refresh_video_inputs() -> None:
    """Re-read the camera list — MUST run on the GUI thread."""
    global _video_inputs
    cached = []
    try:
        for dev in QMediaDevices.videoInputs():
            if dev.isNull():
                continue
            cid = bytes(dev.id()).decode("utf-8", "replace")
            cached.append((dev, _dedup_formats(dev), f"cam:{cid}", dev.description()))
    except Exception:            # noqa: BLE001 — backend hiccup → no cameras this pass
        cached = []
    _video_inputs = cached


def install_camera_enumeration() -> None:
    """Enumerate cameras on the GUI thread and keep the cache fresh. Idempotent;
    a no-op without Qt Multimedia. Call once on the GUI thread before discovery."""
    global _devices_watcher
    if not HAVE_QT_MULTIMEDIA or _devices_watcher is not None:
        return
    _devices_watcher = QMediaDevices()                  # lives on the GUI thread
    _devices_watcher.videoInputsChanged.connect(_refresh_video_inputs)
    _refresh_video_inputs()


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
            emit(Reading(self._device.data_id, "frame", time.time(), img, 0))


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
    def prepare_discovery(cls) -> None:
        """GUI-thread setup hook (called by the DeviceManager before scanning):
        bring Qt Multimedia up HERE, on the GUI thread, not on the worker."""
        install_camera_enumeration()

    @classmethod
    def discover(cls):
        # Reads ONLY the GUI-thread-populated cache — no Qt Multimedia calls here
        # (those would run on the manager's worker thread; see the note above).
        out = []
        for dev, formats, iid, name in list(_video_inputs):
            try:
                out.append(cls(dev, formats, iid, name))
            except Exception:        # noqa: BLE001
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
