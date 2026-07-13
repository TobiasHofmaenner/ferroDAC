"""VideoPreviewPanel — scrub the Timeline, see the ambient footage (DESIGN §9.3).

A thin GUI-thread shell over QMediaPlayer → QVideoSink → the existing VideoView
(so the preview inherits VideoView's zoom/pan). All the selection math — WHICH
segment file and WHAT offset for a given head instant — is the Qt-free
VideoStore.segment_at; this widget only drives the player from it.

Hidden when there are no cameras. A camera picker appears when more than one
camera has ambient video.
"""

from __future__ import annotations

import logging

from qtpy.QtCore import QUrl
from qtpy.QtMultimedia import QMediaPlayer, QVideoSink
from qtpy.QtWidgets import QComboBox, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from .panels import VideoView

log = logging.getLogger("ferrodac.video")


class VideoPreviewPanel(QWidget):
    def __init__(self, video_store, names_fn=None, parent=None):
        super().__init__(parent)
        self._store = video_store
        self._names_fn = names_fn or (lambda: {})
        self._cam = None                 # selected camera uuid
        self._cur_path = None            # the segment file currently loaded
        self._cameras = []

        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.setSpacing(2)
        top = QHBoxLayout()
        top.addWidget(QLabel("📹"))
        self._combo = QComboBox()
        self._combo.currentIndexChanged.connect(self._on_pick)
        top.addWidget(self._combo, 1)
        lay.addLayout(top)
        self.view = VideoView()
        self.view.set_placeholder("scrub the timeline to preview video")
        lay.addWidget(self.view, 1)

        self._sink = QVideoSink(self)
        self._player = QMediaPlayer(self)
        self._player.setVideoOutput(self._sink)
        self._sink.videoFrameChanged.connect(self._on_frame)
        self.refresh_cameras()

    # -- camera set -----------------------------------------------------------
    def refresh_cameras(self) -> None:
        try:
            cams = self._store.cameras() if self._store is not None else []
        except Exception:                # noqa: BLE001
            cams = []
        if cams == self._cameras:
            self.setVisible(bool(cams))
            return
        self._cameras = cams
        names = self._names_fn()
        self._combo.blockSignals(True)
        self._combo.clear()
        for c in cams:
            self._combo.addItem(names.get(f"{c}/frame") or c, c)
        self._combo.blockSignals(False)
        self._combo.setVisible(len(cams) > 1)
        self.setVisible(bool(cams))
        if cams and self._cam not in cams:
            self._cam = cams[0]
            self._combo.setCurrentIndex(0)

    def _on_pick(self, idx: int) -> None:
        if 0 <= idx < len(self._cameras):
            self._cam = self._cameras[idx]
            self._cur_path = None        # force a reload for the new camera
            self._player.stop()

    # -- head-driven playback -------------------------------------------------
    def set_head(self, t: float, playing: bool = False) -> None:
        """Show the frame at instant `t` for the selected camera. `playing`
        follows the transport: paused → a still at the head; playing → run."""
        if self._store is None or self._cam is None:
            return
        try:
            seg = self._store.segment_at(self._cam, float(t))
        except Exception:                # noqa: BLE001
            seg = None
        if seg is None:                  # a gap / outside coverage → blank
            self._cur_path = None
            self._player.stop()
            self.view.set_image(None)
            self.view.set_placeholder("no video at this instant")
            return
        if seg["path"] != self._cur_path:
            self._cur_path = seg["path"]
            self._player.setSource(QUrl.fromLocalFile(seg["path"]))
        self._player.setPosition(int(seg["offset"] * 1000))
        if playing:
            self._player.play()
        else:
            self._player.pause()          # decode + hold the single frame

    def _on_frame(self, frame) -> None:
        try:
            img = frame.toImage()
        except Exception:                # noqa: BLE001
            return
        if img is not None and not img.isNull():
            self.view.set_image(img.copy())

    def stop(self) -> None:
        try:
            self._player.stop()
        except Exception:                # noqa: BLE001
            pass
