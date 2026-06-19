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
