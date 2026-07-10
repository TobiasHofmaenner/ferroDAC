"""MediaService — camera snapshots into the project's media/ dir (DESIGN §9).

A media item is a FILE plus a TAG: the image is written lossless (PNG, §9.1 —
bit-exact as the frame Reading carried it) into ``<project>/media/``, and the
event is a ``kind="media"`` tag on the shared session clock whose payload
carries the project-relative path. The tag substrate then provides everything
else for free: chart markers, the Events dock (with jump), tags.json
persistence, project-lens filtering, and hub sync of the *reference* (the file
itself stays local in v1 — the blob plane is §9's later stage).

Capture source (decided 2026-07-10): the STREAM frame — the engine bus already
caches the latest Reading per key, so a snapshot is exactly the frame the
operator saw, needs no camera panel open, and works in any transport state.
Device-native high-res stills (QImageCapture) can become an opt-in later.

Qt dependency is QtGui only (QImage.save) — no widgets; headless-testable.
"""

from __future__ import annotations

import math
import os
import re
import time

from .tag import MEDIA

# A camera that hasn't produced a frame in this long is STALLED — a "snapshot"
# of it would silently document the past, which is a data-trust hazard. Wall
# clocks and frame stamping have slack; 10 s is generous at any real frame rate.
STALE_S = 10.0


class MediaError(Exception):
    """Snapshot refused — the .reason is UI-presentable."""


def _safe(name: str) -> str:
    return re.sub(r"[^\w.-]+", "-", str(name)).strip("-") or "camera"


class MediaService:
    """Turns "take a photo" into a lossless file + a media tag.

    Collaborators are injected as callables (the app wires them; tests fake
    them): ``latest`` → the engine's latest-Reading-per-key dict, ``markers`` →
    the MarkerModel, ``media_dir`` → the ACTIVE project's media directory
    (created on demand), ``names`` → {key: display label} for tag labels.
    """

    def __init__(self, latest, markers, media_dir, names=None):
        self._latest = latest            # () -> {key: Reading}
        self._markers = markers          # MarkerModel
        self._media_dir = media_dir      # () -> abs path (project's media/)
        self._names = names or (lambda: {})

    # -- the one operation ----------------------------------------------------
    def snapshot(self, source_key: str) -> dict:
        """Capture the latest frame of `source_key` → PNG + media tag.

        Returns {"tag_id", "path", "relpath", "t"}. Raises MediaError when
        there is nothing trustworthy to save (no frame yet / camera stalled /
        not an image source) — the caller shows the reason, nothing is written.
        """
        reading = self._latest().get(source_key)
        if reading is None:
            raise MediaError("no frame from this camera yet")
        img = reading.value
        # duck-typed QImage check (isNull/save) so this module stays import-safe
        # for Qt-free test collection; a scalar Reading lands here on a mis-route
        if not hasattr(img, "save") or not hasattr(img, "isNull") or img.isNull():
            raise MediaError("source did not deliver an image frame")
        t = float(reading.t)
        age = time.time() - t
        if not math.isfinite(t) or age > STALE_S:
            raise MediaError(f"camera stalled — newest frame is {age:.0f} s old")

        mdir = self._media_dir()
        if not mdir:
            raise MediaError("no active project to store media in")
        os.makedirs(mdir, exist_ok=True)     # the service owns its directory
        label = self._names().get(source_key) or source_key
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(t))
        base = f"{stamp}.{int((t % 1) * 1000):03d}_{_safe(label)}"
        relpath, path = self._unique(mdir, base)

        if not img.save(path, "PNG"):        # lossless, always (DESIGN §9.1)
            raise MediaError(f"could not write {relpath}")

        tag_id = self._markers.add(
            t, label=f"📷 {label}", kind=MEDIA,
            payload={"file": relpath, "source": source_key, "format": "png"})
        return {"tag_id": tag_id, "path": path, "relpath": relpath, "t": t}

    def snapshot_all(self, source_keys) -> tuple[list, list]:
        """One tag per camera, best-effort across the set ("document the bench
        now"): returns (results, errors) — a stalled camera never blocks the
        others' capture."""
        results, errors = [], []
        for key in source_keys:
            try:
                results.append(self.snapshot(key))
            except MediaError as exc:
                errors.append((key, str(exc)))
        return results, errors

    # -- helpers ---------------------------------------------------------------
    @staticmethod
    def _unique(mdir: str, base: str) -> tuple[str, str]:
        """A media/-relative + absolute path pair that doesn't collide (two
        snapshots of one camera within a millisecond, or an all-cameras burst)."""
        rel, n = f"{base}.png", 1
        while os.path.exists(os.path.join(mdir, rel)):
            n += 1
            rel = f"{base}_{n}.png"
        return os.path.join("media", rel), os.path.join(mdir, rel)

    @staticmethod
    def resolve(marker, project_path: str) -> str | None:
        """A media tag's absolute file path inside `project_path`, or None if
        the payload is foreign/absent (e.g. a hub-synced reference whose file
        lives on another box — v1 does not sync blobs)."""
        rel = (marker.payload or {}).get("file", "")
        if not rel:
            return None
        path = os.path.normpath(os.path.join(project_path, rel))
        # the file must stay INSIDE the project (a hostile/corrupt payload
        # must not escape via ../..)
        if not path.startswith(os.path.abspath(project_path) + os.sep):
            return None
        return path if os.path.exists(path) else None
