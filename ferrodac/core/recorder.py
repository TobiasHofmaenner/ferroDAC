"""Recorder — append-only capture + materialised CSV.

Hitting Record opens an **append-only** raw file (`_capture.csv`, long format:
one row per reading) plus a `_recording.json` sidecar. Append-only means a crash
leaves every recorded reading intact and nothing is ever rewritten. The two
record markers are a *selection*; the clean wide `data.csv` is materialised once
at Stop (with pre-roll backfill from the HistoryBuffer). On relaunch an
unfinalised capture can be recovered from its sidecar.
"""

from __future__ import annotations

import csv
import json
import os
import re
import time

from .trace import Trace


def _col(key: str, info: dict) -> str:
    name = info.get("name", key)
    unit = info.get("unit", "")
    return f"{name} [{unit}]" if unit else name


def _write_wide(out_path: str, sources: dict, rows: list) -> str:
    """rows = [(t, key, value)] (any order) → wide forward-filled CSV."""
    rows = sorted(rows, key=lambda x: x[0])
    keys = list(sources)
    t0 = rows[0][0] if rows else 0.0
    last = {k: "" for k in keys}
    with open(out_path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["time_s"] + [_col(k, sources[k]) for k in keys])
        cur_t = None
        for t, key, v in rows:
            if cur_t is None:
                cur_t = t
            if t != cur_t:
                w.writerow([f"{cur_t - t0:.6f}"] + [last[k] for k in keys])
                cur_t = t
            if key in last:
                last[key] = v
        if cur_t is not None:
            w.writerow([f"{cur_t - t0:.6f}"] + [last[k] for k in keys])
    return out_path


def materialize_capture(run_dir: str, sources: dict, t_start=None, t_stop=None,
                        history=None, cap_start=None, out_path=None) -> str:
    """Long capture (+ optional pre-roll from history) → wide forward-filled CSV."""
    rows: list[tuple] = []
    cap_start = cap_start if cap_start is not None else (t_start or 0.0)
    if history is not None and t_start is not None and t_start < cap_start:
        for key in sources:
            for (t, v, s) in history.slice(key, t_start, cap_start):
                if s == 0:
                    rows.append((t, key, v))
    cap_path = os.path.join(run_dir, "_capture.csv")
    if os.path.exists(cap_path):
        with open(cap_path, newline="") as fh:
            rd = csv.reader(fh)
            next(rd, None)
            for row in rd:
                if len(row) < 4:
                    continue
                try:
                    t = float(row[0])
                    v = float(row[2])
                    st = int(row[3]) if row[3] else 0
                except ValueError:
                    continue
                key = row[1]
                if st != 0 or key not in sources:
                    continue
                if (t_start is not None and t < t_start) or \
                   (t_stop is not None and t > t_stop):
                    continue
                rows.append((t, key, v))
    return _write_wide(out_path or os.path.join(run_dir, "data.csv"), sources, rows)


def materialize_from_history(out_path: str, sources: dict, history,
                             t_start=None, t_stop=None) -> str:
    """Export the in-memory history slice for these sources → wide CSV."""
    lo = t_start if t_start is not None else -1e18
    hi = t_stop if t_stop is not None else 1e18
    rows = [(t, key, v) for key in sources
            for (t, v, s) in history.slice(key, lo, hi) if s == 0]
    return _write_wide(out_path, sources, rows)


def run_sources(run_dir: str) -> dict:
    """The capture set of a finished run (from meta.json), for re-export."""
    try:
        with open(os.path.join(run_dir, "meta.json")) as fh:
            return json.load(fh).get("sources", {})
    except Exception:
        return {}


def find_unfinalized(base_dir: str) -> list[str]:
    """Run dirs under base_dir with an unfinalised capture (crash recovery)."""
    out = []
    if not os.path.isdir(base_dir):
        return out
    for name in os.listdir(base_dir):
        d = os.path.join(base_dir, name)
        if os.path.isfile(os.path.join(d, "_recording.json")):
            out.append(d)
    return out


def recover(run_dir: str) -> str | None:
    """Materialise a crashed run's full capture from its sidecar, then finalise."""
    side = os.path.join(run_dir, "_recording.json")
    try:
        with open(side) as fh:
            meta = json.load(fh)
    except Exception:
        return None
    out = materialize_capture(run_dir, meta.get("sources", {}))
    try:
        os.remove(side)
    except OSError:
        pass
    return out


class Recorder:
    def __init__(self, engine, history=None, on_change=None):
        self.engine = engine
        self.history = history
        self.on_change = on_change
        self._active = False
        self._unsub = None
        self._raw = None
        self._writer = None
        self._dir = None
        self._sources: dict = {}
        self._trace_sources: dict = {}
        self._trace_files: dict = {}
        self._cap_start = None

    @property
    def active(self) -> bool:
        return self._active

    @property
    def run_dir(self):
        return self._dir

    def start(self, run_dir: str, sources: dict, trace_sources: dict = None) -> None:
        os.makedirs(run_dir, exist_ok=True)
        self._dir = run_dir
        self._sources = dict(sources)
        self._trace_sources = dict(trace_sources or {})
        self._trace_files = {}
        self._cap_start = time.time()
        self._raw = open(os.path.join(run_dir, "_capture.csv"), "a", newline="")
        self._writer = csv.writer(self._raw)
        if self._raw.tell() == 0:
            self._writer.writerow(["t", "key", "value", "status"])
        with open(os.path.join(run_dir, "_recording.json"), "w") as fh:
            json.dump({"started": self._cap_start, "sources": self._sources}, fh)
        self._unsub = self.engine.subscribe(self._on_batch)
        self._active = True
        if self.on_change:
            self.on_change()

    def _on_batch(self, batch) -> None:
        if not self._active:
            return
        w = self._writer
        wrote = False
        for r in batch:
            if r.key in self._sources and isinstance(r.value, (int, float)):
                w.writerow([f"{r.t:.6f}", r.key, r.value, r.status])
                wrote = True
            elif r.key in self._trace_sources and isinstance(r.value, Trace):
                self._write_trace_row(r)
        if wrote:
            self._raw.flush()

    def _write_trace_row(self, r) -> None:
        """Append a scan to a per-trace CSV (header = the m/z axis, written once).
        Each row is `time_s` + the intensities; an axis-length change is skipped."""
        info = self._trace_sources[r.key]
        fh = self._trace_files.get(r.key)
        if fh is None:
            safe = re.sub(r"[^\w.-]", "_", info.get("name", r.key)) or "trace"
            fh = open(os.path.join(self._dir, f"trace_{safe}.csv"), "a", newline="")
            self._trace_files[r.key] = fh
            info["n"] = len(r.value.x)
            if fh.tell() == 0:
                csv.writer(fh).writerow(["time_s"] + [f"{m:g}" for m in r.value.x])
        if len(r.value.x) != info.get("n"):
            return
        csv.writer(fh).writerow([f"{r.t - self._cap_start:.3f}"]
                                + [f"{v:.6E}" for v in r.value.y])
        fh.flush()

    def stop(self, t_start=None, t_stop=None) -> str | None:
        if not self._active:
            return None
        if self._unsub:
            self._unsub()
            self._unsub = None
        self._active = False
        if self._raw:
            self._raw.flush()
            self._raw.close()
            self._raw = None
        for fh in self._trace_files.values():
            try:
                fh.close()
            except Exception:
                pass
        self._trace_files = {}
        out = materialize_capture(self._dir, self._sources, t_start, t_stop,
                                  history=self.history, cap_start=self._cap_start)
        # persistent run metadata (kept) — lets the recording be re-exported later
        with open(os.path.join(self._dir, "meta.json"), "w") as fh:
            json.dump({"sources": self._sources, "cap_start": self._cap_start,
                       "t_start": t_start, "t_stop": t_stop}, fh, indent=2)
        try:
            os.remove(os.path.join(self._dir, "_recording.json"))
        except OSError:
            pass
        if self.on_change:
            self.on_change()
        return out
