"""VideoStore — the ambient video segment store (DESIGN §9.3).

Rotating documentation-quality segments per camera, with a [t0,t1] → file
index, living in the APP's ambient area (next to store.zarr — never inside a
project). Clips are SELECTIONS over this store, re-materialized whenever their
recording markers move; the store itself is expendable-by-policy: no default
retention ("never silently delete"), a manual cleanup instead, and opt-in
per-camera retention for those who want it.

Qt-free; the Qt capture orchestration lives in ui/videocapture.py.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time

SEGMENT_S = 120.0          # rotation period — also the clip-edge granularity (§9.3)
DISK_FLOOR_GB = 5.0        # below this free space, video capture PAUSES (loud),
#                            protecting the zarr store; scalars keep flowing
DISK_RESUME_GB = 7.0       # hysteresis: resume only above this


def _safe(name: str) -> str:
    return re.sub(r"[^\w.-]+", "-", str(name)).strip("-") or "camera"


def seg_key(t0: float) -> int:
    """A segment's stable cross-peer identity: the integer ms of its start (the
    same value that names the file, seg_<key>.mp4). Two stores agree on it, so
    sync reconciles on it without float drift (§9.3 phase 3)."""
    return int(round(float(t0) * 1000))


# -- H.264 encoder preference (§9.3) ------------------------------------------
# A machine-global flag: hardware H.264 encoding was proven broken here (segments
# never landed), so steer Qt to SOFTWARE at the next launch. Self-correcting
# backstop for when the startup probe is fooled (probe says hw ok, Qt still fails).
def _config_dir() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config")
    return os.path.join(base, "ferroDAC")


def prefer_software_encode() -> bool:
    return os.path.exists(os.path.join(_config_dir(), "prefer_software_video_encode"))


def mark_prefer_software_encode() -> None:
    """Remember (best-effort) that hardware H.264 encode failed on this box."""
    try:
        os.makedirs(_config_dir(), exist_ok=True)
        with open(os.path.join(_config_dir(), "prefer_software_video_encode"),
                  "w", encoding="utf-8") as fh:
            fh.write("hardware H.264 encode produced no segments — using software\n")
    except OSError:
        pass


class VideoStore:
    def __init__(self, root: str):
        self.root = root                    # <app_dir>/video
        self._cache: dict = {}              # cam_uuid -> ((mtime_ns, size), entries):
        #                                     memo of index.json keyed by the file's
        #                                     signature, so the per-tick scrub point
        #                                     query (segment_at) doesn't re-parse an
        #                                     O(segments) index off a GUI timer (§9.3).

    # -- paths / index ---------------------------------------------------------
    def cam_dir(self, cam_uuid: str, create: bool = False) -> str:
        d = os.path.join(self.root, _safe(cam_uuid))
        if create:                          # WRITE paths only — reads must not touch
            os.makedirs(d, exist_ok=True)   #   the disk (segment_at is on the GUI thread)
        return d

    def _index_path(self, cam_uuid: str, create: bool = False) -> str:
        return os.path.join(self.cam_dir(cam_uuid, create=create), "index.json")

    def _load(self, cam_uuid: str) -> list:
        """The camera's index, memoized by (mtime, size). Returns a COPY: callers
        (commit/prune/delete) mutate the list, so the cached one stays pristine."""
        path = self._index_path(cam_uuid)
        try:
            st = os.stat(path)
            sig = (st.st_mtime_ns, st.st_size)
        except OSError:                     # fresh / missing → empty (drop any memo)
            self._cache.pop(cam_uuid, None)
            return []
        hit = self._cache.get(cam_uuid)
        if hit is None or hit[0] != sig:
            try:
                with open(path, encoding="utf-8") as fh:
                    entries = json.load(fh)
            except Exception:               # noqa: BLE001 — corrupt → empty
                entries = []
            self._cache[cam_uuid] = hit = (sig, entries)
        return list(hit[1])

    def _save(self, cam_uuid: str, entries: list) -> None:
        path = self._index_path(cam_uuid, create=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(entries, fh)
        os.replace(tmp, path)               # next _load sees the new mtime → re-parses

    # -- segment lifecycle -------------------------------------------------------
    def segment_path(self, cam_uuid: str, t0: float) -> str:
        """Where the recorder should write the segment starting at t0."""
        return os.path.join(self.cam_dir(cam_uuid, create=True), f"seg_{int(t0 * 1000)}.mp4")

    def commit(self, cam_uuid: str, t0: float, t1: float, path: str) -> bool:
        """Register a FINALIZED segment (the file must exist and be non-empty —
        QMediaRecorder finalizes asynchronously; the orchestrator verifies after
        a grace period). Returns False when the file never landed."""
        if not (os.path.exists(path) and os.path.getsize(path) > 0):
            return False
        entries = self._load(cam_uuid)
        entries.append({"t0": float(t0), "t1": float(t1),
                        "file": os.path.basename(path),
                        "size": os.path.getsize(path), "synced": False})
        entries.sort(key=lambda e: e["t0"])
        self._save(cam_uuid, entries)
        return True

    # -- queries -----------------------------------------------------------------
    def cameras(self) -> list:
        if not os.path.isdir(self.root):
            return []
        return sorted(d for d in os.listdir(self.root)
                      if os.path.isfile(os.path.join(self.root, d, "index.json")))

    def coverage(self, cam_uuid: str) -> list:
        """Merged [t0,t1] intervals of stored ambient video (segments abut at
        rotation boundaries; merge within a small join slack)."""
        out = []
        for e in self._load(cam_uuid):
            if out and e["t0"] <= out[-1][1] + 2.0:      # rotation gap ≈ sub-second
                out[-1][1] = max(out[-1][1], e["t1"])
            else:
                out.append([e["t0"], e["t1"]])
        return [(a, b) for a, b in out]

    def segments_overlapping(self, cam_uuid: str, t0: float, t1: float) -> list:
        """Index entries overlapping [t0,t1], time-ordered, with abs paths."""
        d = self.cam_dir(cam_uuid)
        return [{**e, "path": os.path.join(d, e["file"])}
                for e in self._load(cam_uuid)
                if e["t1"] > t0 and e["t0"] < t1]

    def segment_entry_at(self, cam_uuid: str, t: float) -> "dict | None":
        """The raw index entry (t0/t1/file/size/synced) whose [t0,t1] contains
        instant `t`; the later segment wins at a rotation boundary. None in a gap
        or outside coverage. The selection kernel behind segment_at + backfill."""
        best = None
        for e in self._load(cam_uuid):
            if e["t0"] <= t < e["t1"] and (best is None or e["t0"] >= best["t0"]):
                best = e
        return best

    def segment_at(self, cam_uuid: str, t: float) -> "dict | None":
        """POINT query for the scrub preview (§9.3 phase 2): the segment whose
        [t0,t1] contains instant `t`, as {"path", "offset"} where offset is the
        seek position within the file (seconds). None when `t` lands in a gap or
        outside coverage. If two segments touch at a rotation boundary the later
        one wins (its offset ≈ 0), matching 'newest at-or-covering the head'."""
        best = self.segment_entry_at(cam_uuid, t)
        if best is None:
            return None
        return {"path": os.path.join(self.cam_dir(cam_uuid), best["file"]),
                "offset": max(0.0, float(t) - float(best["t0"]))}

    # -- hub sync + backfill (§9.3 phase 3): store-and-forward for segments -------
    def segments(self, cam_uuid: str) -> list:
        """The camera's raw index entries, time-ordered — what a sync pass walks."""
        return self._load(cam_uuid)

    def have(self) -> set:
        """{(cam, seg_key)} for every stored segment — the reconciliation truth a
        peer syncs against (the video twin of ZarrStore.epoch_lengths, §12.1)."""
        return {(cam, seg_key(e["t0"]))
                for cam in self.cameras() for e in self._load(cam)}

    def mark_synced(self, cam_uuid: str, t0: float, synced: bool = True) -> bool:
        """Flag a segment hub-confirmed — enables the §9.3 'prune only synced'
        retention notch (never drop footage the hub hasn't archived). Returns
        whether a matching segment was found."""
        key = seg_key(t0)
        entries = self._load(cam_uuid)
        hit = False
        for e in entries:
            if seg_key(e["t0"]) == key:
                e["synced"] = bool(synced)
                hit = True
        if hit:
            self._save(cam_uuid, entries)
        return hit

    def read_segment_bytes(self, cam_uuid: str, t0: float) -> "bytes | None":
        """The raw mp4 bytes of the segment at t0 (for upload/serve). None if the
        file has vanished (skipped by the sync, not fatal)."""
        key = seg_key(t0)
        for e in self._load(cam_uuid):
            if seg_key(e["t0"]) == key:
                try:
                    with open(os.path.join(self.cam_dir(cam_uuid), e["file"]),
                              "rb") as fh:
                        return fh.read()
                except OSError:
                    return None
        return None

    def import_segment(self, cam_uuid: str, t0: float, t1: float,
                       data: bytes) -> int:
        """Write a received/pulled segment's bytes to disk + index it. Idempotent:
        a segment already held at t0 is left untouched (returns 0). Used by the hub
        (PushSegment) and by local on-demand backfill (PullSegment). Returns bytes
        written."""
        if not data:
            return 0
        key = seg_key(t0)
        if any(seg_key(e["t0"]) == key for e in self._load(cam_uuid)):
            return 0                                     # already have it
        path = self.segment_path(cam_uuid, t0)           # creates the camera dir
        with open(path, "wb") as fh:
            fh.write(data)
        if not self.commit(cam_uuid, t0, t1, path):      # index + keep time order
            return 0
        self.mark_synced(cam_uuid, t0)                   # it exists on the peer
        return len(data)

    def covers(self, cam_uuid: str, t0: float, t1: float,
               slack: float = None) -> bool:
        """Does ambient video cover [t0,t1] (within one segment of slack at each
        edge — the §9.3 granularity)?"""
        slack = SEGMENT_S if slack is None else slack
        for a, b in self.coverage(cam_uuid):
            if a <= t0 + slack and b >= t1 - slack:
                return True
        return False

    def usage(self, cam_uuid: str = None) -> int:
        """Bytes stored (one camera, or the whole ambient area)."""
        cams = [cam_uuid] if cam_uuid else self.cameras()
        return sum(e["size"] for c in cams for e in self._load(c))

    # -- deletion (manual cleanup + opt-in retention) ------------------------------
    def delete_older_than(self, cam_uuid: str, cutoff_t: float) -> int:
        """MANUAL cleanup: drop segments that END before cutoff_t. Returns bytes
        freed. (Materialized clips live in projects and are untouched.)"""
        entries = self._load(cam_uuid)
        keep, freed = [], 0
        d = self.cam_dir(cam_uuid)
        for e in entries:
            if e["t1"] < cutoff_t:
                freed += e["size"]
                try:
                    os.remove(os.path.join(d, e["file"]))
                except OSError:
                    pass
            else:
                keep.append(e)
        self._save(cam_uuid, keep)
        return freed

    def prune_retention(self, cam_uuid: str, policy: str,
                        now: float = None) -> int:
        """OPT-IN retention (§9.3 mode 4): '48h'/'7d' = time window; '20GB'/'500MB'
        = size cap (oldest first). Unknown policy prunes nothing. Returns bytes
        freed."""
        now = time.time() if now is None else now
        policy = (policy or "").strip().lower()
        m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(h|d|gb|mb)", policy)
        if not m:
            return 0
        val, unit = float(m.group(1)), m.group(2)
        if unit in ("h", "d"):
            window = val * (3600.0 if unit == "h" else 86400.0)
            return self.delete_older_than(cam_uuid, now - window)
        cap = val * (1e9 if unit == "gb" else 1e6)
        entries = self._load(cam_uuid)
        total = sum(e["size"] for e in entries)
        freed = 0
        d = self.cam_dir(cam_uuid)
        while entries and total > cap:
            e = entries.pop(0)                           # oldest first
            total -= e["size"]
            freed += e["size"]
            try:
                os.remove(os.path.join(d, e["file"]))
            except OSError:
                pass
        if freed:
            self._save(cam_uuid, entries)
        return freed

    # -- disk guard -----------------------------------------------------------------
    def free_gb(self) -> float:
        os.makedirs(self.root, exist_ok=True)
        return shutil.disk_usage(self.root).free / 1e9
