"""The tiered resolver (DESIGN §7.4) — one query() over many tiers.

A **tier** is anything implementing ``coverage(series) -> [(t0,t1), ...]`` and
``query(series, t0, t1, max_points) -> (x, y)``. ``ZarrStore`` already is one;
``RamTier`` adapts the live ``HistoryBuffer``; the remote hub becomes one later.

The resolver holds tiers **nearest → far** (RAM ring → local store → remote) and,
for a window, **partitions it into sub-ranges each served by the nearest tier
that covers it** (overlap → nearer wins: fresher + cheaper), then **stitches** the
pieces — seamlessly where data is continuous across a tier handoff, with a NaN
break only at a real coverage gap. Local-first: with no remote tier it's just
RAM + local. Qt-free.
"""

from __future__ import annotations

import numpy as np

from .decimate import downsample as _downsample
from .decimate import interleave as _interleave
from .intervals import GAP_JOIN_EPS
from .intervals import merge as _merge
from .intervals import split_intervals as _split_intervals


class RamTier:
    """Adapts the live in-RAM HistoryBuffer to the tier protocol."""

    def __init__(self, history):
        self.history = history

    def has(self, series) -> bool:
        return self.history.span(series) is not None

    def coverage(self, series):
        # Gap-aware: a plain (oldest, newest) span would advertise ONE continuous interval
        # over a 30 s–300 s outage that the store correctly split, and _merge would union the
        # store's split back into one → the chart draws a line across a real gap again. Split
        # the RAM slice on the same dt rule so RAM and store agree (still one interval when
        # the ring truly is continuous — no false breaks).
        sp = self.history.span(series)
        if not sp:
            return []
        ts = [t for (t, _v, s) in self.history.slice(series, sp[0], sp[1]) if s == 0]
        return _split_intervals(np.asarray(ts, dtype="f8")) if ts else [sp]

    def query(self, series, t0, t1, max_points=2000):
        pts = [(t, v) for (t, v, s) in self.history.slice(series, t0, t1) if s == 0]
        if not pts:
            return np.array([]), np.array([])
        t = np.fromiter((p[0] for p in pts), dtype="f8", count=len(pts))
        v = np.fromiter((p[1] for p in pts), dtype="f8", count=len(pts))
        if len(t) > max_points * 2:                      # denser than asked → bucket
            f = max(2, len(t) // max_points)
            return _interleave(*_downsample(t, v, v, f))
        return t, v

    def read_raw(self, series, t0, t1):                  # FULL-RES (replay/analysis)
        pts = [(t, v) for (t, v, s) in self.history.slice(series, t0, t1) if s == 0]
        if not pts:
            return np.array([]), np.array([])
        t = np.fromiter((p[0] for p in pts), dtype="f8", count=len(pts))
        v = np.fromiter((p[1] for p in pts), dtype="f8", count=len(pts))
        return t, v


class Resolver:
    # `tiers` is REBOUND atomically on mutation (never mutated in place) and
    # readers bind it locally first — so resolver reads running on worker
    # threads (DESIGN §21.3) can never see a half-edited list when the GUI
    # attaches/detaches the hub tier. No lock on the read path.
    def __init__(self, tiers):
        self.tiers = list(tiers)                         # nearest → far
        self._remote = None

    def set_remote(self, tier) -> None:
        """Attach the hub as the FARTHEST tier (local RAM/store win on overlap;
        the hub fills history we lack locally). Replaces any prior remote."""
        self.clear_remote()
        self._remote = tier
        self.tiers = [*self.tiers, tier]

    def clear_remote(self) -> None:
        if self._remote is not None:
            self.tiers = [t for t in self.tiers if t is not self._remote]
        self._remote = None

    def _tiers(self, local_only: bool = False) -> list:
        """Atomic tier snapshot; local_only drops the remote (hub) tier — for
        callers on the GUI thread or on a per-tick cadence, where a networked
        coverage/read (4 s timeout) must never be reachable."""
        tiers = list(self.tiers)
        if local_only and self._remote is not None:
            tiers = [t for t in tiers if t is not self._remote]
        return tiers

    def coverage(self, series, local_only: bool = False):
        ivs = []
        for tier in self._tiers(local_only):
            ivs += list(tier.coverage(series))
        return _merge(ivs)

    def knows(self, series, local_only: bool = False) -> bool:
        """Does ANY tier hold this series? An O(1) presence test (unlike coverage(), which now
        materializes the RAM slice) for callers that only need a stored-vs-derived filter — a
        near tier's cheap has() short-circuits before a remote tier's networked coverage()."""
        for tier in self._tiers(local_only):
            has = getattr(tier, "has", None)
            if has is not None:
                if has(series):
                    return True
            elif tier.coverage(series):          # tier without has() → fall back (e.g. hub)
                return True
        return False

    def query(self, series, t0, t1, max_points=2000, local_only: bool = False):
        segs = self._partition(series, t0, t1, local_only=local_only)
        owned = [(a, b, tier) for a, b, tier in segs if tier is not None]
        if not owned:
            return np.array([]), np.array([])
        total = sum(b - a for a, b, _ in owned) or 1.0
        xs, ys, prev_b = [], [], None
        for a, b, tier in owned:
            budget = max(50, int(max_points * (b - a) / total))
            qx, qy = tier.query(series, a, b, budget)
            if len(qx) == 0:
                continue
            if prev_b is not None and a > prev_b + GAP_JOIN_EPS:  # a real gap was skipped
                xs.append(np.array([np.nan])); ys.append(np.array([np.nan]))
            xs.append(np.asarray(qx)); ys.append(np.asarray(qy))
            prev_b = b
        if not xs:
            return np.array([]), np.array([])
        return np.concatenate(xs), np.concatenate(ys)

    def read_raw(self, series, t0, t1, local_only: bool = False):
        """FULL-RES scalar samples stitched across tiers (nearest-wins per
        sub-range), no decimation — the replay/analysis/EXPORT read. Tiers without
        a read_raw (or with no data) are skipped; the hub fills what's only remote
        (unless local_only — the per-tick play path must never wait on a socket).

        Segment reads are HALF-OPEN at interior handoffs: partition edges are
        coverage endpoints, i.e. real sample times, and every tier's read is
        inclusive at both ends — so a sample sitting exactly on a tier boundary
        was returned by BOTH tiers, duplicating one measurement per seam
        (invisible on a chart, a corrupt row in a CSV; the old exporter's
        {t: v} dict masked it). The boundary sample belongs to the segment that
        STARTS there — unless the next segment is an unowned gap, where this
        interval's true last sample must stay."""
        segs = self._partition(series, t0, t1, local_only=local_only)
        ts, vs = [], []
        for i, (a, b, tier) in enumerate(segs):
            rr = getattr(tier, "read_raw", None) if tier is not None else None
            if rr is None:
                continue
            t, v = rr(series, a, b)
            if len(t) and i + 1 < len(segs) and segs[i + 1][2] is not None:
                keep = np.asarray(t, dtype="f8") < b     # half-open: next owner serves b
                t, v = np.asarray(t)[keep], np.asarray(v)[keep]
            if len(t):
                ts.append(np.asarray(t, dtype="f8")); vs.append(np.asarray(v, dtype="f8"))
        if not ts:
            return np.array([]), np.array([])
        t = np.concatenate(ts); v = np.concatenate(vs)
        order = np.argsort(t, kind="stable")             # tier boundaries → re-order
        return t[order], v[order]

    def read_raw_trace(self, series, t0, t1, local_only: bool = False) -> list:
        """FULL-RES trace blocks stitched across tiers that hold traces (local
        store / hub). list of (times[k], Y[k, m], x[m]). Same half-open seam rule
        as read_raw: a scan exactly on a tier boundary must not appear twice.
        `local_only` skips the networked hub tier — the per-tick play/backfill path
        must never wait on a socket (mirrors read_raw; DESIGN §21.2)."""
        segs = self._partition(series, t0, t1, local_only=local_only)
        out = []
        for i, (a, b, tier) in enumerate(segs):
            rr = getattr(tier, "read_raw_trace", None) if tier is not None else None
            if rr is None:
                continue
            for (bt, by, bx) in rr(series, a, b):
                if len(bt) and i + 1 < len(segs) and segs[i + 1][2] is not None:
                    keep = np.asarray(bt, dtype="f8") < b
                    bt, by = np.asarray(bt)[keep], np.asarray(by)[keep]
                if len(bt):
                    out.append((bt, by, bx))
        return out

    def query_trace(self, series, t0, t1, max_scans=400, local_only: bool = False) -> list:
        """Display-decimated trace blocks stitched across tiers (the waterfall
        preview path) — full-res read then ~max_scans representative scans/block."""
        out = []
        for (t, Y, x) in self.read_raw_trace(series, t0, t1, local_only=local_only):
            if len(t) > max_scans:
                idx = np.linspace(0, len(t) - 1, max_scans).astype(int)
                t, Y = t[idx], Y[idx]
            out.append((t, Y, x))
        return out

    def source_dtype(self, series) -> str:
        """First tier that actually knows the source's dtype ('trace'|'scalar');
        lets the replay pick read_raw vs read_raw_trace for hub-only sources too."""
        for tier in list(self.tiers):
            f = getattr(tier, "source_dtype", None)
            if f is None:
                continue
            dt = f(series)
            if dt and dt != "scalar":
                return dt
        return "scalar"

    def _partition(self, series, t0, t1, local_only: bool = False):
        """Tile [t0,t1] into (a, b, tier|None) segments — nearest covering tier
        wins each segment; None = a true gap (no tier has it)."""
        tiers = self._tiers(local_only)                  # atomic snapshot (§21.2)
        covs = [list(tier.coverage(series)) for tier in tiers]
        edges = {t0, t1}
        for cov in covs:
            for a, b in cov:
                if t0 < a < t1:
                    edges.add(a)
                if t0 < b < t1:
                    edges.add(b)
        edges = sorted(edges)
        segs = []
        for a, b in zip(edges, edges[1:]):
            mid = 0.5 * (a + b)
            owner = None
            for i, cov in enumerate(covs):
                if any(lo <= mid <= hi for lo, hi in cov):
                    owner = tiers[i]                     # nearest tier wins
                    break
            if segs and segs[-1][2] is owner:            # merge adjacent same-owner
                segs[-1] = (segs[-1][0], b, owner)
            else:
                segs.append((a, b, owner))
        return segs
