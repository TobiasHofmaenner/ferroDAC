"""VideoCaptureService — ambient video segment recording (DESIGN §9.3).

Rotates ~2-min documentation-quality segments per active camera into the
VideoStore, gated by each camera's mode (Off / While-recording / Always /
Always+retention) and a low-disk floor that PAUSES video (never the data
plane). Clips are materialized from these segments elsewhere (ClipMaterializer);
this service only keeps the ambient store fed.

The camera device is duck-typed: `video_mode`, `video_retention`, `data_id`,
`start_segment(path)`, `stop_segment()`. Segment finalization is async
(QMediaRecorder), so a stopped segment is committed to the store after a grace
delay, verifying the file — a failed encode is dropped, never indexed.
"""

from __future__ import annotations

import logging
import os
import time

from qtpy.QtCore import QObject, QTimer

from ..core.videostore import (DISK_FLOOR_GB, DISK_RESUME_GB, SEGMENT_S,
                               VideoStore, mark_prefer_software_encode,
                               prefer_software_encode)

log = logging.getLogger("ferrodac.video")


class VideoCaptureService(QObject):
    def __init__(self, store: VideoStore, devices, is_recording, now,
                 on_status=None, parent=None):
        super().__init__(parent)
        self._store = store
        self._devices = devices          # () -> live camera device objects
        self._is_recording = is_recording  # () -> bool
        self._now = now                  # () -> wall-clock seconds
        self._on_status = on_status or (lambda msg, timeout=0: None)
        self._active: dict = {}          # data_id -> {"t0", "path", "dev", "label"}
        self._paused_disk = False
        self._encode_fail_streak = 0     # consecutive segments that never landed —
        #                                  self-corrects a fooled hw-encoder probe (§9.3)
        # rotation heartbeat: a segment is closed + a fresh one opened each tick
        self._timer = QTimer(self)
        self._timer.setInterval(int(SEGMENT_S * 1000))
        self._timer.timeout.connect(self._rotate)

    def start(self) -> None:
        self._timer.start()
        self.reconcile()

    def stop(self) -> None:
        """Orderly shutdown (closeEvent): close every open segment and commit it
        SYNCHRONOUSLY. The normal path defers each commit behind a 1500 ms QTimer
        grace period, but at exit the event loop dies before that timer can ever
        fire — the deferred commit would never run and the final segment (up to
        SEGMENT_S of footage per camera) would sit on disk unindexed, invisible
        forever. We are on the GUI thread with nothing left to pump, so a short
        BOUNDED wait for the recorder's file is acceptable here (shared deadline
        across all cameras — their recorders finalize in parallel)."""
        self._timer.stop()
        deadline = time.monotonic() + 2.0
        for data_id in list(self._active):
            self._close(data_id, deadline=deadline)

    # -- policy ---------------------------------------------------------------
    def _should_capture(self, dev) -> bool:
        mode = getattr(dev, "video_mode", 0)
        if mode == 0:
            return False
        if mode == 1:                    # while recording
            return bool(self._is_recording())
        return True                      # 2 always · 3 always+retention

    def reconcile(self) -> None:
        """Bring capture in line with each camera's mode + the record state +
        the disk floor. Called on record start/stop, option changes, and the
        active-device set changing."""
        free = self._store.free_gb()
        if not self._paused_disk and free < DISK_FLOOR_GB:
            self._paused_disk = True
            self._on_status(f"⏸ Video capture paused — {free:.1f} GB free "
                            f"(protecting the data store)", 0)
            for data_id in list(self._active):
                self._close(data_id)
            return
        if self._paused_disk:
            if free < DISK_RESUME_GB:
                return                   # still low → stay paused (hysteresis)
            self._paused_disk = False
            self._on_status("▶ Video capture resumed", 4000)

        want = {}
        for dev in self._devices():
            if self._should_capture(dev):
                want[getattr(dev, "data_id", None)] = dev
        for data_id in list(self._active):        # stop those that shouldn't run
            if data_id not in want:
                self._close(data_id)
        for data_id, dev in want.items():         # start those that should
            if data_id not in self._active:
                self._open(dev)

    # -- segment lifecycle -----------------------------------------------------
    def _open(self, dev) -> None:
        data_id = getattr(dev, "data_id", None)
        if data_id is None:
            return
        t0 = self._now()
        path = self._store.segment_path(data_id, t0)
        try:
            if not dev.start_segment(path):
                return                    # not streaming yet — retry next reconcile
        except Exception:                 # noqa: BLE001 — one camera ≠ no capture
            log.debug("segment start failed for %s", data_id, exc_info=True)
            return
        self._active[data_id] = {"t0": t0, "path": path, "dev": dev,
                                 "label": getattr(dev, "name", data_id)}

    def _close(self, data_id: str, deadline: float = None) -> None:
        """Close one segment. `deadline` is set on the EXIT path only (stop()):
        wait boundedly for the file, then commit right here; otherwise the commit
        rides a QTimer grace delay as always."""
        seg = self._active.pop(data_id, None)
        if seg is None:
            return
        try:
            seg["dev"].stop_segment()
        except Exception:                 # noqa: BLE001
            pass
        t1 = self._now()
        if deadline is not None:          # exit — no event loop left for a timer
            self._wait_landed(seg["path"], deadline)
            self._commit(data_id, seg, t1)   # same logic the timer would have run;
            return                           # a file that never landed is logged
            #                                  and skipped by _commit, not raised
        # QMediaRecorder finalizes the file asynchronously — commit after a grace
        # delay, verifying it landed (VideoStore.commit drops an empty/missing file)
        QTimer.singleShot(1500, lambda: self._commit(data_id, seg, t1))

    @staticmethod
    def _wait_landed(path: str, deadline: float) -> None:
        """Bounded blocking poll (exit path only) for the recorder's async
        finalization: return as soon as `path` is non-empty on disk — the same
        'it landed' predicate VideoStore.commit verifies — or when the deadline
        passes (the following commit then logs + skips the segment)."""
        while time.monotonic() < deadline:
            try:
                if os.path.getsize(path) > 0:
                    return
            except OSError:
                pass                      # not there yet
            time.sleep(0.05)
        log.debug("segment file still absent at exit deadline: %s", path)

    def _commit(self, data_id: str, seg: dict, t1: float) -> None:
        if not self._store.commit(data_id, seg["t0"], t1, seg["path"]):
            log.debug("segment never landed for %s (%s)", data_id, seg["path"])
            # A landed segment is the only proof the encoder works. If several in a
            # row produce nothing, the hardware H.264 path is broken here despite the
            # startup probe — remember it so the NEXT launch encodes in software, and
            # tell the user why their video is currently empty (§9.3).
            self._encode_fail_streak += 1
            if self._encode_fail_streak == 3 and not prefer_software_encode():
                mark_prefer_software_encode()
                self._on_status("⚠ Video isn't encoding (no usable hardware H.264 "
                                "encoder) — restart to switch to software encoding", 0)
            return
        self._encode_fail_streak = 0                   # a segment landed → encoder works
        dev = seg["dev"]
        if getattr(dev, "video_mode", 0) == 3:        # always + retention → prune
            try:
                self._store.prune_retention(data_id, dev.video_retention,
                                            now=self._now())
            except Exception:             # noqa: BLE001
                log.debug("retention prune failed for %s", data_id, exc_info=True)

    def _rotate(self) -> None:
        """Heartbeat: close every open segment (committing it) and immediately
        reopen — bounding each segment to ~SEGMENT_S and keeping the index fresh
        for materialization. reconcile() re-opens under the current policy."""
        for data_id in list(self._active):
            self._close(data_id)
        self.reconcile()
