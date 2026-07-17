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
        self._recorder = None      # live QMediaRecorder while a clip runs
        self._pending_clip_path = None   # handed over from start_clip (any thread;
        #                                  atomic attr write, consumed on the GUI thread)

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
    def begin_clip(self) -> None:
        """Start recording a DOCUMENTATION clip (DESIGN §9.1 — compressed is
        fine, it is context, not data) alongside the live sink. Best-effort:
        any encoder/backend failure logs and leaves streaming untouched — the
        caller verifies the FILE exists before tagging, so a failed clip can
        never produce a tag pointing at nothing."""
        path, self._pending_clip_path = self._pending_clip_path, None
        if not path or self._session is None or self._recorder is not None:
            return
        try:
            from qtpy.QtCore import QUrl
            from qtpy.QtMultimedia import QMediaFormat, QMediaRecorder
            rec = QMediaRecorder()
            fmt = QMediaFormat(QMediaFormat.FileFormat.MPEG4)
            fmt.setVideoCodec(QMediaFormat.VideoCodec.H264)
            rec.setMediaFormat(fmt)
            rec.setQuality(QMediaRecorder.Quality.NormalQuality)
            rec.setOutputLocation(QUrl.fromLocalFile(path))
            rec.errorOccurred.connect(self._on_record_error)   # broken hw encoder → fall back
            self._session.setRecorder(rec)
            rec.record()
            self._recorder = rec
        except Exception:                              # noqa: BLE001 — no encoder /
            self._recorder = None                      # backend quirk → no clip
            import logging
            logging.getLogger("ferrodac").warning(
                "clip recording unavailable for %s", path, exc_info=True)

    def _on_record_error(self, error, msg) -> None:
        """Qt's recorder couldn't encode (typically hardware VAAPI H.264 with no
        usable profile — 'No usable encoding profile found'). This is the GROUND
        TRUTH a proxy probe can't give, so react to it: steer the FFmpeg backend
        to software for any subsequent recorder in THIS process (best-effort — Qt
        may have cached the choice), and PERSIST it so the next launch skips
        straight to software. Thread-safe (env + a file write, no Qt access) — the
        signal can arrive on a Qt encoder worker thread (§9.3)."""
        from qtpy.QtMultimedia import QMediaRecorder
        if error in (QMediaRecorder.Error.FormatError,
                     QMediaRecorder.Error.ResourceError):
            import logging
            import os
            from ..core.videostore import (mark_prefer_software_encode,
                                           prefer_software_encode)
            os.environ["QT_FFMPEG_ENCODING_HW_DEVICE_TYPES"] = ""   # → software next recorder
            if not prefer_software_encode():
                mark_prefer_software_encode()
                logging.getLogger("ferrodac.video").warning(
                    "hardware H.264 encode failed (%s) — switching ambient video to "
                    "software; restart if segments stay empty", msg)

    @Slot()
    def end_clip(self) -> None:
        rec, self._recorder = self._recorder, None
        if rec is None:
            return
        try:
            rec.stop()                                 # file finalises ASYNC —
        except Exception:                              # callers verify on disk
            pass                                       # after a grace period
        if self._session is not None:
            try:
                self._session.setRecorder(None)
            except Exception:
                pass

    @Slot()
    def end(self) -> None:
        self.end_clip()                                # never leave a recorder running
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
        # Rate-gate BEFORE the conversion: this handler runs on the GUI thread for
        # every native frame (Qt Multimedia affinity), and toImage+convertToFormat
        # is an ~MB-scale copy per frame — ~30/s of pure GUI-thread work. Display,
        # CV and the hub preview ride these Readings and none needs more than
        # ~10 fps; RECORDING is QMediaRecorder-side and untouched by this gate.
        now = time.time()
        if now - getattr(self, "_last_reading", 0.0) < 0.099:
            return
        self._last_reading = now
        img = frame.toImage()
        if img.isNull():
            return
        if img.format() != QImage.Format.Format_RGB888:
            img = img.convertToFormat(QImage.Format.Format_RGB888)
        else:
            img = img.copy()
        emit = self._device._emit
        if emit is not None:
            emit(Reading(self._device.data_id, "frame", now, img, 0))


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
            ),
            # §9.3 ambient video: capture rotating segments so a clip is a
            # SELECTION over always-on video (movable REC markers re-materialize
            # the clip). 0 off · 1 only while recording · 2 always · 3 always
            # with retention (opt-in pruning — set video_retention).
            Option(key="video", name="Video capture",
                   choices=((0, "Off"), (1, "While recording"),
                            (2, "Always"), (3, "Always (with retention)")),
                   value=0),
            # retention policy for mode 3 (e.g. "48h", "7d", "20GB") — blank = keep
            Option(key="video_retention", name="Video retention", kind="text"),
            # §9/§9.1 live video on the hub — encoding when a REMOTE viewer
            # watches this camera (demand-driven; idle = zero traffic):
            # Documentation = JPEG ≤960px ≤8fps (webcam at a gauge);
            # Raw = bit-exact frames at native rate/size (pixels are data —
            # the scientific-camera-on-10GbE case; plan the bandwidth).
            Option(key="hub_video", name="Hub video",
                   choices=((0, "Off"), (1, "Documentation (JPEG)"),
                            (2, "Raw (lossless)")), value=1),
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

    # -- ambient video (§9.3; driven by the VideoCaptureService) --------------
    @property
    def video_mode(self) -> int:
        """0 off · 1 while recording · 2 always · 3 always+retention."""
        return int(self._option_values.get("video", 0))

    @property
    def video_retention(self) -> str:
        return str(self._option_values.get("video_retention", "") or "")

    @property
    def hub_video_mode(self) -> int:
        """0 off · 1 documentation (JPEG, capped) · 2 raw (bit-exact) — §9.1."""
        return int(self._option_values.get("hub_video", 0))

    def start_segment(self, path: str) -> bool:
        """Queue video recording into `path` — one ambient segment (§9.3), or a
        legacy clip. Best-effort; False = not streaming right now."""
        if self._controller is None:
            return False
        self._controller._pending_clip_path = str(path)   # cross-binding safe
        QMetaObject.invokeMethod(self._controller, "begin_clip",
                                 Qt.QueuedConnection)     # (no Q_ARG in PySide6)
        return True

    def stop_segment(self) -> None:
        if self._controller is not None:
            QMetaObject.invokeMethod(self._controller, "end_clip",
                                     Qt.QueuedConnection)
