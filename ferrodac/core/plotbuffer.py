"""CurveBuffer — a bounded, decimating point buffer for a live chart curve
(DESIGN §11 / §21 Tier-1).

The audit's #1 week-long-slowdown mechanism: ChartPanel kept every reading of the
session in a Python list and called ``setData(list, list)`` **per reading**, so
each tick did an O(total-history) list→ndarray conversion and memory grew without
bound in the default grow mode.

This replaces that with a pre-allocated numpy buffer:

- **append is amortized O(new points)** — samples are written into reserved slots,
  never re-converting the whole history; the panel then calls ``setData`` once per
  *batch* (not per reading), handing pyqtgraph an ndarray it decimates for display.
- **bounded**: at ``cap`` points the buffer **decimates 2:1 in place** (keeps the
  full time span, coarser), so a grow-mode "whole session" view and a parked
  multi-day window both stay bounded in memory and per-append cost. The durable
  full-resolution data lives in the store — this is a *display* buffer (DESIGN §11:
  "display is always a decimated view").

Qt-free, so it is unit-tested without a GUI.
"""

from __future__ import annotations

import numpy as np

_INIT = 1024               # initial backing capacity (grows by doubling → cap)
_DEFAULT_CAP = 120_000     # ~2 MB/curve; setData of this is a few ms


class CurveBuffer:
    def __init__(self, cap: int = _DEFAULT_CAP):
        self.cap = max(4, int(cap))
        c0 = min(_INIT, self.cap)
        self._x = np.empty(c0, dtype="f8")
        self._y = np.empty(c0, dtype="f8")
        # parallel inline σ lanes (NaN = none); lo == hi for symmetric sources,
        # they differ for a fit folded against a physical bound (DESIGN §19.7)
        self._slo = np.empty(c0, dtype="f8")
        self._shi = np.empty(c0, dtype="f8")
        self._n = 0

    def __len__(self) -> int:
        return self._n

    def append(self, xs, ys, ss=None) -> None:
        """Append parallel sequences of x (time), y (value), and optional σ (inline
        uncertainty; absent → NaN). ``ss`` is a 1-D sequence for symmetric ±σ or a
        ``(lo, hi)`` pair of sequences for asymmetric errors. Decimates first if the
        new points would exceed `cap`, so the buffer never grows past it."""
        xs = np.asarray(xs, dtype="f8").ravel()
        ys = np.asarray(ys, dtype="f8").ravel()
        k = min(xs.shape[0], ys.shape[0])
        if k == 0:
            return

        def _lane(a):
            a = np.full(k, np.nan) if a is None else np.asarray(a, dtype="f8").ravel()
            if a.shape[0] < k:                 # short/absent σ → pad with NaN
                a = np.concatenate([a, np.full(k - a.shape[0], np.nan)])
            return a

        if isinstance(ss, tuple) and len(ss) == 2:
            slo, shi = _lane(ss[0]), _lane(ss[1])
        else:
            slo = shi = _lane(ss)
        if k > self.cap:                       # a single batch bigger than the cap
            xs, ys, k = xs[-self.cap:], ys[-self.cap:], self.cap
            slo, shi = slo[-self.cap:], shi[-self.cap:]
            self._n = 0                        # …keep only its most recent tail
        while self._n + k > self.cap:          # would overflow → coarsen to fit
            self._decimate()
        self._reserve(self._n + k)
        self._x[self._n:self._n + k] = xs[:k]
        self._y[self._n:self._n + k] = ys[:k]
        self._slo[self._n:self._n + k] = slo[:k]
        self._shi[self._n:self._n + k] = shi[:k]
        self._n += k

    def _reserve(self, need: int) -> None:
        cur = self._x.shape[0]
        if need <= cur:
            return
        newcap = cur or _INIT
        while newcap < need:
            newcap *= 2
        newcap = min(newcap, self.cap)
        nx = np.empty(newcap, dtype="f8")
        ny = np.empty(newcap, dtype="f8")
        nlo = np.empty(newcap, dtype="f8")
        nhi = np.empty(newcap, dtype="f8")
        nx[:self._n] = self._x[:self._n]
        ny[:self._n] = self._y[:self._n]
        nlo[:self._n] = self._slo[:self._n]
        nhi[:self._n] = self._shi[:self._n]
        self._x, self._y, self._slo, self._shi = nx, ny, nlo, nhi

    def _decimate(self) -> None:
        """Halve the stored points with a min/max **envelope**, keeping the full
        span. Each group of 4 consecutive samples collapses to its two extrema
        (the argmin and argmax of y, emitted in time order) — so a lone spike is
        never dropped the way plain stride-2 would drop it, and it composes
        losslessly with pyqtgraph's ``mode="peak"`` (min/max of min/max = min/max).
        Coarsens the *display* only; the store keeps full resolution for the
        Timeline/analysis. NaN-aware: any bucket holding a NaN emits a break, so a
        buffered offline-gap marker survives decimation (a straight line must never
        be drawn across a real gap)."""
        n = self._n
        if n < 4:                              # too small to envelope → stride halve
            h = (n + 1) // 2
            for a in (self._x, self._y, self._slo, self._shi):
                a[:h] = a[:n:2]
            self._n = h
            return
        B = 4                                  # 4 samples → 2 extrema = 2:1, like stride
        full = n // B
        used = full * B
        tail = n - used                        # 0..3 newest points, kept exact
        m = 2 * full
        rows = np.arange(full)
        yb = self._y[:used].reshape(full, B)
        nan_bucket = np.isnan(yb).any(axis=1)
        imin = np.where(np.isnan(yb), np.inf, yb).argmin(axis=1)
        imax = np.where(np.isnan(yb), -np.inf, yb).argmax(axis=1)
        lo_i = np.minimum(imin, imax)          # earlier-in-time extremum first
        hi_i = np.maximum(imin, imax)

        def _fold(a):
            ab = a[:used].reshape(full, B)
            out = np.empty(m + tail, dtype="f8")
            out[0:m:2] = ab[rows, lo_i]
            out[1:m:2] = ab[rows, hi_i]
            if tail:
                out[m:] = a[used:n]            # newest tail verbatim (live cursor)
            return out

        nx, ny, nlo, nhi = (_fold(self._x), _fold(self._y),
                            _fold(self._slo), _fold(self._shi))
        if nan_bucket.any():                   # force a break where the gap lives
            ny[:m].reshape(full, 2)[nan_bucket] = np.nan
        self._x[:m + tail] = nx
        self._y[:m + tail] = ny
        self._slo[:m + tail] = nlo
        self._shi[:m + tail] = nhi
        self._n = m + tail

    def trim(self, x_min: float) -> bool:
        """Drop points older than x_min (slide mode). Returns True if it changed
        anything. x is time-ordered (live append), so binary-search the cut."""
        if self._n and self._x[0] < x_min:
            i = int(np.searchsorted(self._x[:self._n], x_min, side="left"))
            if i:
                m = self._n - i
                self._x[:m] = self._x[i:self._n]
                self._y[:m] = self._y[i:self._n]
                self._slo[:m] = self._slo[i:self._n]
                self._shi[:m] = self._shi[i:self._n]
                self._n = m
                return True
        return False

    def clear(self) -> None:
        self._n = 0

    @property
    def x(self):
        return self._x[:self._n]

    @property
    def y(self):
        return self._y[:self._n]

    @property
    def sigma_lo(self):
        return self._slo[:self._n]

    @property
    def sigma_hi(self):
        return self._shi[:self._n]

    @property
    def sigma(self):
        """The upper σ lane — identical to the lower one for symmetric sources
        (which is every source except a fit folded against a physical bound)."""
        return self._shi[:self._n]

    @property
    def has_sigma(self) -> bool:
        """True if any buffered sample carries an inline σ (a processor output that
        CREATES uncertainty). Device channels leave σ NaN and use the model path."""
        return self._n > 0 and bool(np.isfinite(self._shi[:self._n]).any())
