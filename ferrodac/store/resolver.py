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

from .zarrstore import _downsample, _interleave


def _merge(intervals):
    out = []
    for a, b in sorted(intervals):
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


class RamTier:
    """Adapts the live in-RAM HistoryBuffer to the tier protocol."""

    def __init__(self, history):
        self.history = history

    def coverage(self, series):
        sp = self.history.span(series)
        return [sp] if sp else []

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
    def __init__(self, tiers):
        self.tiers = list(tiers)                         # nearest → far
        self._remote = None

    def set_remote(self, tier) -> None:
        """Attach the hub as the FARTHEST tier (local RAM/store win on overlap;
        the hub fills history we lack locally). Replaces any prior remote."""
        self.clear_remote()
        self._remote = tier
        self.tiers.append(tier)

    def clear_remote(self) -> None:
        if self._remote is not None and self._remote in self.tiers:
            self.tiers.remove(self._remote)
        self._remote = None

    def coverage(self, series):
        ivs = []
        for tier in self.tiers:
            ivs += list(tier.coverage(series))
        return _merge(ivs)

    def query(self, series, t0, t1, max_points=2000):
        segs = self._partition(series, t0, t1)
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
            if prev_b is not None and a > prev_b + 1e-9:  # a real gap was skipped
                xs.append(np.array([np.nan])); ys.append(np.array([np.nan]))
            xs.append(np.asarray(qx)); ys.append(np.asarray(qy))
            prev_b = b
        if not xs:
            return np.array([]), np.array([])
        return np.concatenate(xs), np.concatenate(ys)

    def read_raw(self, series, t0, t1):
        """FULL-RES scalar samples stitched across tiers (nearest-wins per
        sub-range), no decimation — the replay/analysis read. Tiers without a
        read_raw (or with no data) are skipped; the hub fills what's only remote."""
        ts, vs = [], []
        for a, b, tier in self._partition(series, t0, t1):
            rr = getattr(tier, "read_raw", None) if tier is not None else None
            if rr is None:
                continue
            t, v = rr(series, a, b)
            if len(t):
                ts.append(np.asarray(t, dtype="f8")); vs.append(np.asarray(v, dtype="f8"))
        if not ts:
            return np.array([]), np.array([])
        t = np.concatenate(ts); v = np.concatenate(vs)
        order = np.argsort(t, kind="stable")             # tier boundaries → re-order
        return t[order], v[order]

    def read_raw_trace(self, series, t0, t1) -> list:
        """FULL-RES trace blocks stitched across tiers that hold traces (local
        store / hub). list of (times[k], Y[k, m], x[m])."""
        out = []
        for a, b, tier in self._partition(series, t0, t1):
            rr = getattr(tier, "read_raw_trace", None) if tier is not None else None
            if rr is not None:
                out.extend(rr(series, a, b))
        return out

    def query_trace(self, series, t0, t1, max_scans=400) -> list:
        """Display-decimated trace blocks stitched across tiers (the waterfall
        preview path) — full-res read then ~max_scans representative scans/block."""
        out = []
        for (t, Y, x) in self.read_raw_trace(series, t0, t1):
            if len(t) > max_scans:
                idx = np.linspace(0, len(t) - 1, max_scans).astype(int)
                t, Y = t[idx], Y[idx]
            out.append((t, Y, x))
        return out

    def source_dtype(self, series) -> str:
        """First tier that actually knows the source's dtype ('trace'|'scalar');
        lets the replay pick read_raw vs read_raw_trace for hub-only sources too."""
        for tier in self.tiers:
            f = getattr(tier, "source_dtype", None)
            if f is None:
                continue
            dt = f(series)
            if dt and dt != "scalar":
                return dt
        return "scalar"

    def _partition(self, series, t0, t1):
        """Tile [t0,t1] into (a, b, tier|None) segments — nearest covering tier
        wins each segment; None = a true gap (no tier has it)."""
        covs = [list(tier.coverage(series)) for tier in self.tiers]
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
                    owner = self.tiers[i]                # nearest tier wins
                    break
            if segs and segs[-1][2] is owner:            # merge adjacent same-owner
                segs[-1] = (segs[-1][0], b, owner)
            else:
                segs.append((a, b, owner))
        return segs
