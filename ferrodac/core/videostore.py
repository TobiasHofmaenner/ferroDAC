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


class VideoStore:
    def __init__(self, root: str):
        self.root = root                    # <app_dir>/video

    # -- paths / index ---------------------------------------------------------
    def cam_dir(self, cam_uuid: str) -> str:
        d = os.path.join(self.root, _safe(cam_uuid))
        os.makedirs(d, exist_ok=True)
        return d

    def _index_path(self, cam_uuid: str) -> str:
        return os.path.join(self.cam_dir(cam_uuid), "index.json")

    def _load(self, cam_uuid: str) -> list:
        try:
            with open(self._index_path(cam_uuid), encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:                   # noqa: BLE001 — fresh / corrupt → empty
            return []

    def _save(self, cam_uuid: str, entries: list) -> None:
        path = self._index_path(cam_uuid)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(entries, fh)
        os.replace(tmp, path)

    # -- segment lifecycle -------------------------------------------------------
    def segment_path(self, cam_uuid: str, t0: float) -> str:
        """Where the recorder should write the segment starting at t0."""
        return os.path.join(self.cam_dir(cam_uuid), f"seg_{int(t0 * 1000)}.mp4")

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

    def segment_at(self, cam_uuid: str, t: float) -> "dict | None":
        """POINT query for the scrub preview (§9.3 phase 2): the segment whose
        [t0,t1] contains instant `t`, as {"path", "offset"} where offset is the
        seek position within the file (seconds). None when `t` lands in a gap or
        outside coverage. If two segments touch at a rotation boundary the later
        one wins (its offset ≈ 0), matching 'newest at-or-covering the head'."""
        d = self.cam_dir(cam_uuid)
        best = None
        for e in self._load(cam_uuid):
            if e["t0"] <= t < e["t1"] and (best is None or e["t0"] >= best["t0"]):
                best = e
        if best is None:
            return None
        return {"path": os.path.join(d, best["file"]),
                "offset": max(0.0, float(t) - float(best["t0"]))}

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
