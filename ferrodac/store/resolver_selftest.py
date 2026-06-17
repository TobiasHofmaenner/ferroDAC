"""Self-test for the tiered resolver (DESIGN §7.4).
Run: python3 -m ferrodac.store.resolver_selftest

Composes a live HistoryBuffer (recent) + a ZarrStore (older recorded run) behind
one Resolver and checks: union coverage, seamless stitch across the local→RAM
handoff (no false NaN), nearest-wins in the overlap (RAM, the fresher tier), and
a NaN break at a genuine coverage gap.
"""

from __future__ import annotations

import os
import tempfile

import numpy as np

from ..core.history import HistoryBuffer
from . import RamTier, Resolver, ZarrStore


class _R:                       # a minimal Reading-like object for HistoryBuffer.feed
    __slots__ = ("key", "t", "value", "status", "partial")

    def __init__(self, key, t, v):
        self.key, self.t, self.value, self.status, self.partial = key, t, v, 0, False


def _feed(hist, key, t0, t1, value, hz=10):
    hist.feed([_R(key, t, value) for t in np.arange(t0, t1, 1.0 / hz)])


def main() -> int:
    now = 1_000_000.0
    d = tempfile.mkdtemp()
    store = ZarrStore(os.path.join(d, "run"))
    hist = HistoryBuffer(window_s=5000)
    res = Resolver([RamTier(hist), store])      # nearest → far: RAM, then local

    # series A — local [-1000,-400] (v=1) overlapping RAM [-500,0] (v=2): continuous
    store.add_source("A")
    ta = np.arange(now - 1000, now - 400, 0.1)
    store.append("A", ta, np.ones(len(ta)), epoch="e0")
    store.finalize_rollups("A")
    _feed(hist, "A", now - 500, now, 2.0)

    # series B — local [-1000,-700] (v=1), RAM [-300,0] (v=2): a gap in [-700,-300]
    store.add_source("B")
    tb = np.arange(now - 1000, now - 700, 0.1)
    store.append("B", tb, np.ones(len(tb)), epoch="e0")
    store.finalize_rollups("B")
    _feed(hist, "B", now - 300, now, 2.0)

    # coverage = merged union across tiers (RAM tail ends ~now-0.1)
    cov = res.coverage("A")
    assert len(cov) == 1 and abs(cov[0][0] - (now - 1000)) < 1 and abs(cov[0][1] - now) < 1, cov
    print("✓ coverage A = merged union [-1000, 0]")

    # full A query: continuous, NO false NaN at the local→RAM handoff
    x, y = res.query("A", now - 1000, now, max_points=2000)
    assert not np.isnan(y).any(), "false gap across a continuous handoff"
    assert x.min() <= now - 990 and x.max() >= now - 10
    print(f"✓ A stitched local→RAM seamlessly: {len(x)} pts, no NaN, spans the range")

    # nearest-wins: the overlap [-500,-400] is served by RAM (v≈2), not local (v=1)
    _, yo = res.query("A", now - 480, now - 420, max_points=2000)
    assert abs(np.nanmean(yo) - 2.0) < 0.01, np.nanmean(yo)
    # local-only region still reads local (v≈1)
    _, yl = res.query("A", now - 800, now - 700, max_points=2000)
    assert abs(np.nanmean(yl) - 1.0) < 0.01, np.nanmean(yl)
    print("✓ nearest-wins: overlap served by RAM (2.0), local-only by store (1.0)")

    # series B: a real gap → exactly one NaN break, values 1 then 2 around it
    xb, yb = res.query("B", now - 1000, now, max_points=2000)
    assert np.isnan(yb).any(), "gap not broken"
    assert abs(np.nanmean(yb[xb < now - 700]) - 1.0) < 0.01
    assert abs(np.nanmean(yb[xb > now - 300]) - 2.0) < 0.01
    print("✓ gap honored: NaN break in [-700,-300], v=1 before / v=2 after")

    print("\nRESOLVER SELFTEST PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
