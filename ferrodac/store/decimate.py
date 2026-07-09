"""The ONE display-decimation policy (DESIGN §22 I-10, step 5).

Envelope representation, NaN semantics, and bucket timing used to be scattered
across the store pyramid, the RAM tier, the Timeline preview, and the route
backfill — each computing a slightly different approximation of the same data,
which is exactly how "the same spike has three different heights depending on
which path drew it" happened. This module is the only implementation:

- **Envelope end-to-end**: min/max buckets, drawn as an interleaved
  (t,min),(t,max) polyline. Peaks survive by construction, and min/max of
  min/max composes, so re-folding a coarser view from a finer one is lossless.
- **NaN policy** (matches the write boundary: the writer never persists a
  non-finite value): a bucket keeps the extrema of its FINITE members (legacy
  stores hold NaN for failed reads); only an all-NaN bucket stays NaN — a real
  break, never fabricated data.
- **Bucket time = mean of member times.** Window edges must therefore be
  handled by the reader (see ZarrStore._query_epoch: pad one bucket, clip to
  the window) or up to half a coarse bucket is shaved off each edge.

`CurveBuffer._decimate` (core/plotbuffer.py) is the in-place buffer variant of
the same policy — same envelope + NaN semantics, plus span pinning and gap
anchoring, documented there. Qt-free, numpy-only.
"""

from __future__ import annotations

import math

import numpy as np


def downsample(t, mn, mx, factor):
    """One pyramid level: min/max over groups of `factor`, bucket time = mean.
    NaN-robust per the module policy (finite extrema win; all-NaN stays a break)."""
    n = len(mn)
    nb = math.ceil(n / factor)
    pad = nb * factor - n
    if pad:
        t = np.concatenate([t, np.full(pad, t[-1])])
        mn = np.concatenate([mn, np.full(pad, mn[-1])])
        mx = np.concatenate([mx, np.full(pad, mx[-1])])
    t = t.reshape(nb, factor).mean(axis=1)
    mnb = mn.reshape(nb, factor)
    mxb = mx.reshape(nb, factor)
    mask = np.isnan(mnb)
    mn_out = np.where(mask, np.inf, mnb).min(axis=1)
    mx_out = np.where(np.isnan(mxb), -np.inf, mxb).max(axis=1)
    all_nan = mask.all(axis=1)
    if all_nan.any():
        mn_out[all_nan] = np.nan
        mx_out[all_nan] = np.nan
    return t, mn_out, mx_out


def interleave(t, mn, mx):
    """A min/max envelope as a single polyline: (t,min),(t,max) per bucket."""
    if len(t) == 0:
        return np.array([]), np.array([])
    x = np.repeat(t, 2)
    y = np.empty(len(t) * 2)
    y[0::2], y[1::2] = mn, mx
    return x, y


def envelope_midline(x, y):
    """Collapse an interleaved envelope's duplicate-x pairs to their mid value.

    DISPLAY CHOICE for the Timeline's ~30 px navigation ribbon ONLY, where the
    full zigzag reads as noise: the midline HALVES every spike by construction
    ((min+max)/2), so it must never be used for a data display — charts draw the
    envelope itself everywhere (parked draw, zoom, route backfill), which is what
    keeps one amplitude across every path (§22 I-10). Singletons (already-raw)
    and NaN gap-markers pass through unchanged."""
    x = np.asarray(x, float)
    y = np.asarray(y, float)
    n = len(x)
    if n < 2:
        return x, y
    out_x, out_y = [], []
    i = 0
    while i < n:
        if i + 1 < n and not np.isnan(x[i]) and x[i] == x[i + 1]:
            out_x.append(x[i]); out_y.append(0.5 * (y[i] + y[i + 1])); i += 2
        else:
            out_x.append(x[i]); out_y.append(y[i]); i += 1
    return np.asarray(out_x), np.asarray(out_y)
