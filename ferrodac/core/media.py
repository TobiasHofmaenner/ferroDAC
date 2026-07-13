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
import shutil
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
    def unique_path(mdir: str, base: str, ext: str = "png") -> tuple[str, str]:
        """Public variant of _unique for other media producers (clips)."""
        rel, n = f"{base}.{ext}", 1
        while os.path.exists(os.path.join(mdir, rel)):
            n += 1
            rel = f"{base}_{n}.{ext}"
        return os.path.join("media", rel), os.path.join(mdir, rel)

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


class ClipMaterializer:
    """Turn a recording SPAN into a clip file (DESIGN §9.3): a clip is a
    SELECTION over the ambient VideoStore's segments, re-materialized whenever
    its recording markers move. Qt-free — the VideoStore does the indexing, this
    concatenates the overlapping segments into the project's media/.

    Concatenation prefers the ffmpeg CLI (concat demuxer, STREAM COPY — lossless,
    fast, one clean .mp4). Without ffmpeg it degrades honestly: a single covering
    segment is copied as-is; multiple segments are copied as parts and the tag's
    payload lists them (`files`), `file` pointing at the first so single-file
    consumers still work. Segment-boundary slop (~2 min) at the edges is accepted
    (§9.3); the tag carries the exact requested t0/t1.
    """

    def __init__(self, store, media_dir):
        self._store = store              # VideoStore
        self._media_dir = media_dir      # () -> abs path (active project's media/)

    def materialize(self, cam_uuid: str, t0: float, t1: float,
                    label: str) -> "dict | None":
        """Concatenate the segments overlapping [t0,t1] into media/. Returns
        {"file", "files", "path"} or None (no ambient video for the span)."""
        segs = self._store.segments_overlapping(cam_uuid, t0, t1)
        segs = [e for e in segs
                if os.path.exists(e["path"]) and os.path.getsize(e["path"]) > 0]
        if not segs:
            return None
        mdir = self._media_dir()
        if not mdir:
            return None
        os.makedirs(mdir, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(t0))
        base = f"{stamp}_{_safe(label)}"

        if len(segs) == 1:               # one segment covers → copy it (§9.3 slop)
            rel, path = MediaService.unique_path(mdir, base, ext="mp4")
            shutil.copyfile(segs[0]["path"], path)
            return {"file": rel, "files": [rel], "path": path}

        if _have_ffmpeg():               # many → concat to one clean file
            rel, path = MediaService.unique_path(mdir, base, ext="mp4")
            if _ffmpeg_concat([e["path"] for e in segs], path):
                return {"file": rel, "files": [rel], "path": path}
            # ffmpeg hiccup → fall through to the parts fallback

        files, first_abs = [], None      # no ffmpeg: copy parts, list them
        for i, e in enumerate(segs, 1):
            rel, path = MediaService.unique_path(mdir, f"{base}.part{i}", ext="mp4")
            shutil.copyfile(e["path"], path)
            files.append(rel)
            first_abs = first_abs or path
        return {"file": files[0], "files": files, "path": first_abs}


def _have_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


def _ffmpeg_concat(parts: list, out_path: str) -> bool:
    """Lossless concat via the ffmpeg concat demuxer (stream copy). True on
    success. Best-effort: any failure returns False for the caller's fallback."""
    import subprocess
    import tempfile
    listf = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
            listf = fh.name
            for pth in parts:
                fh.write(f"file '{os.path.abspath(pth)}'\n")
        r = subprocess.run(
            ["ffmpeg", "-nostdin", "-y", "-f", "concat", "-safe", "0",
             "-i", listf, "-c", "copy", out_path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
        return r.returncode == 0 and os.path.exists(out_path) \
            and os.path.getsize(out_path) > 0
    except Exception:                    # noqa: BLE001
        return False
    finally:
        if listf and os.path.exists(listf):
            try:
                os.remove(listf)
            except OSError:
                pass
