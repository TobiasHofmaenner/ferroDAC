"""PrefetchCache — a local, RAM-backed resolver tier the PlaybackPrefetcher fills
from the hub (DESIGN §12.1 / §7.4).

Why: GUI-thread reads (the parked-window redraw, the per-tick play advance) must
never wait on the hub socket, yet hub-only history must still be shown. So a
background prefetcher pulls hub ranges into THIS tier ahead of where you're
looking, and the synchronous reads find them locally. It sits AFTER the Zarr
store and BEFORE the hub, so `local_only` reads include it and still exclude the
socket — no new plumbing, the resolver's existing tier split does the work.

It advertises the RANGES it has FETCHED as its coverage (not merely where samples
landed), so the resolver never re-asks the hub for a range already pulled — a
fetched-but-empty span is "covered" and reads back empty. RAM-bounded: when over
a byte cap it trims the data farthest from a focus time (the play head).

Thread-safe (DESIGN §21.2): the prefetcher worker writes while resolver reads run
on the GUI thread and on worker threads. Qt-free.
"""

from __future__ import annotations

import threading

import numpy as np

from .decimate import downsample as _downsample
from .decimate import interleave as _interleave
from .intervals import intersect as _intersect
from .intervals import merge as _merge


class PrefetchCache:
    def __init__(self, max_bytes: int = 256 * 1024 * 1024, keep_s: float = 1800.0):
        self._max = int(max_bytes)
        self._keep = float(keep_s)          # trim to ±keep around focus when over budget
        self._lock = threading.Lock()
        self._sc: dict = {}                 # source -> (t, v) f8 arrays, sorted by t
        self._tr: dict = {}                 # source -> [(t, Y, x)] blocks, sorted by t[0]
        self._cov: dict = {}                # source -> merged [(a,b)] FETCHED ranges
        self._dt: dict = {}                 # source -> "scalar" | "trace"
        self._bytes = 0
        self._focus: "float | None" = None

    # -- tier protocol (same shape as RamTier / ZarrStore) -------------------
    def has(self, series) -> bool:
        with self._lock:
            return bool(self._cov.get(series))

    def coverage(self, series) -> list:
        with self._lock:
            return list(self._cov.get(series, ()))

    def source_dtype(self, series) -> str:
        with self._lock:
            return self._dt.get(series, "scalar")

    def read_raw(self, series, t0, t1):
        with self._lock:
            tv = self._sc.get(series)
            if tv is None:
                return np.array([]), np.array([])
            t, v = tv
            m = (t >= t0) & (t <= t1)
            return t[m].copy(), v[m].copy()

    def query(self, series, t0, t1, max_points=2000):
        t, v = self.read_raw(series, t0, t1)
        if len(t) > max_points * 2:                      # denser than asked → bucket
            f = max(2, len(t) // max_points)
            return _interleave(*_downsample(t, v, v, f))
        return t, v

    def read_raw_trace(self, series, t0, t1) -> list:
        with self._lock:
            out = []
            for (bt, by, bx) in self._tr.get(series, ()):
                m = (bt >= t0) & (bt <= t1)
                if m.any():
                    out.append((bt[m].copy(), by[m].copy(), bx.copy()))
            return out

    # -- ingest (the prefetcher worker writes) -------------------------------
    def add_scalar(self, series, t, v, a, b) -> None:
        """Merge fetched scalars + mark [a,b] fetched (even if t is empty, so an
        empty span isn't re-requested)."""
        t = np.asarray(t, dtype="f8")
        v = np.asarray(v, dtype="f8")
        with self._lock:
            self._dt[series] = "scalar"
            old = self._sc.get(series)
            if old is not None and len(old[0]):
                t = np.concatenate([old[0], t])
                v = np.concatenate([old[1], v])
                order = np.argsort(t, kind="stable")
                t, v = t[order], v[order]
                keep = np.concatenate(([True], np.diff(t) > 0))   # drop exact-t dups
                t, v = t[keep], v[keep]
            self._sc[series] = (t, v)
            self._mark_locked(series, a, b)

    def add_trace(self, series, blocks, a, b) -> None:
        with self._lock:
            self._dt[series] = "trace"
            lst = self._tr.setdefault(series, [])
            for (bt, by, bx) in blocks:
                bt = np.asarray(bt, dtype="f8")
                if len(bt):
                    lst.append((bt, np.asarray(by, dtype="f8"),
                                np.asarray(bx, dtype="f8")))
            lst.sort(key=lambda blk: float(blk[0][0]) if len(blk[0]) else 0.0)
            self._mark_locked(series, a, b)

    def set_focus(self, t) -> None:
        with self._lock:
            self._focus = float(t)

    def clear(self) -> None:
        with self._lock:
            self._sc.clear()
            self._tr.clear()
            self._cov.clear()
            self._dt.clear()
            self._bytes = 0

    # -- internal ------------------------------------------------------------
    def _mark_locked(self, series, a, b) -> None:
        self._cov[series] = _merge(list(self._cov.get(series, ()))
                                   + [(float(a), float(b))])
        self._recount_locked()
        self._evict_locked()

    def _recount_locked(self) -> None:
        n = 0
        for (t, v) in self._sc.values():
            n += t.nbytes + v.nbytes
        for blocks in self._tr.values():
            for (bt, by, bx) in blocks:
                n += bt.nbytes + by.nbytes + bx.nbytes
        self._bytes = n

    def _evict_locked(self) -> None:
        if self._bytes <= self._max or self._focus is None:
            return
        keep = self._keep
        while self._bytes > self._max and keep >= 30.0:  # shrink the kept window
            self._trim_locked(self._focus - keep, self._focus + keep)
            keep *= 0.5

    def _trim_locked(self, lo, hi) -> None:
        win = [(lo, hi)]
        for s in list(self._sc):
            t, v = self._sc[s]
            m = (t >= lo) & (t <= hi)
            self._sc[s] = (t[m], v[m])
            self._cov[s] = _intersect(self._cov.get(s, ()), win)
        for s in list(self._tr):
            self._tr[s] = [(bt, by, bx) for (bt, by, bx) in self._tr[s]
                           if len(bt) and bt[-1] >= lo and bt[0] <= hi]
            self._cov[s] = _intersect(self._cov.get(s, ()), win)
        self._recount_locked()
