"""The ONE interval/gap algebra (DESIGN §22 I-10, step 5).

Coverage intervals, gap splitting, widening, and merging used to live in three
places that had to stay consistent by hand — `zarrstore._split_intervals`,
`RamTier.coverage`, and the resolver's `_merge` — and every divergence was a
bug class: a tier reporting one continuous interval across a gap another tier
split would union the split away (a line drawn across a real outage), and a
zero-width interval was silently unownable (its lone sample dropped). This
module is now the only implementation; the store, the RAM tier, and the
resolver all import from here. Qt-free, numpy-only.

Gap detection (DESIGN §7.4): there is no time-resolved "device online" record —
a reconnect appends into the SAME epoch, so a recording outage is just a large
dt in an otherwise dense series. `split_intervals` cuts at any dt FAR larger
than the local cadence, so the chart AND the Timeline preview break the line
across a real gap instead of drawing across it. Scale-free (K·median) with a
floor so a fast source's sub-minute jitter never false-splits; per-source
override via the source group's `gap_k` attr.
"""

from __future__ import annotations

import numpy as np

GAP_K = 8.0        # a gap = a dt at least this many × the local median cadence …
GAP_MIN_S = 30.0   # … but never below this many seconds (ignore sub-30 s jitter)
GAP_EPS = 1e-3     # min half-width for a point interval so a partition can own its sample
GAP_JOIN_EPS = 1e-9  # two abutting segments are "continuous" within this slack —
#                      the same test the resolver's stitch and the chart's gap-break use


def widen(s, e, eps=GAP_EPS):
    """A coverage interval MUST have real width, or the resolver's partition (which
    assigns an owner by testing a segment's MIDPOINT against lo<=mid<=hi) can never
    own a zero-width (t,t) interval → its lone sample is dropped from read_raw/query.
    Pad an isolated sample (a flap between two gaps, or a 1-sample epoch) to a tiny
    real span; eps << any real gap."""
    return (s - 0.5 * eps, e + 0.5 * eps) if e - s < eps else (s, e)


def split_intervals(t, gap_k=GAP_K):
    """Split a monotonic timestamp array into contiguous-recording (start, end)
    intervals at real gaps — a dt >> the local median cadence. Endpoints are the
    true first/last samples; a gapless series returns a single interval. A degenerate
    (isolated-sample) interval is widened so the resolver never drops its sample."""
    t = np.asarray(t, dtype="f8")
    if t.size == 0:
        return []
    if t.size < 3:
        return [widen(float(t[0]), float(t[-1]))]
    d = np.diff(t)
    pos = d[d > 0]
    med = float(np.median(pos)) if pos.size else 0.0
    thresh = max(GAP_MIN_S, gap_k * med) if med > 0 else GAP_MIN_S
    cut = np.nonzero(d > thresh)[0]              # gap sits between t[i] and t[i+1]
    if cut.size == 0:
        return [widen(float(t[0]), float(t[-1]))]   # 3+ all-equal ts → widen (no drop)
    starts = [float(t[0])] + [float(t[i + 1]) for i in cut]
    ends = [float(t[i]) for i in cut] + [float(t[-1])]
    # GAP_EPS (not a median-scaled pad): the partition only needs ANY positive width to
    # own the sample, and a fixed 1 ms keeps a widened point from over-reporting coverage
    # for very sparse sources (still << any real gap, so intervals stay disjoint).
    return [widen(s, e) for s, e in zip(starts, ends)]


def merge(intervals):
    """Union a list of (start, end) intervals into a sorted, disjoint list.
    Used by the resolver to combine per-tier coverage — safe ONLY because every
    tier splits on the same `split_intervals` rule (a tier reporting one span
    across a gap another split would union the gap away)."""
    out = []
    for a, b in sorted(intervals):
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out
