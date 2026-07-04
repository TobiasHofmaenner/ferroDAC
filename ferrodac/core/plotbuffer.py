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
        self._s = np.empty(c0, dtype="f8")   # parallel inline σ lane (NaN = none)
        self._n = 0

    def __len__(self) -> int:
        return self._n

    def append(self, xs, ys, ss=None) -> None:
        """Append parallel sequences of x (time), y (value), and optional σ (inline
        uncertainty; absent → NaN). Decimates first if the new points would exceed
        `cap`, so the buffer never grows past it."""
        xs = np.asarray(xs, dtype="f8").ravel()
        ys = np.asarray(ys, dtype="f8").ravel()
        k = min(xs.shape[0], ys.shape[0])
        if k == 0:
            return
        ss = (np.full(k, np.nan) if ss is None
              else np.asarray(ss, dtype="f8").ravel())
        if ss.shape[0] < k:                    # short/absent σ → pad with NaN
            ss = np.concatenate([ss, np.full(k - ss.shape[0], np.nan)])
        if k > self.cap:                       # a single batch bigger than the cap
            xs, ys, ss, k = xs[-self.cap:], ys[-self.cap:], ss[-self.cap:], self.cap
            self._n = 0                        # …keep only its most recent tail
        while self._n + k > self.cap:          # would overflow → coarsen to fit
            self._decimate()
        self._reserve(self._n + k)
        self._x[self._n:self._n + k] = xs[:k]
        self._y[self._n:self._n + k] = ys[:k]
        self._s[self._n:self._n + k] = ss[:k]
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
        ns = np.empty(newcap, dtype="f8")
        nx[:self._n] = self._x[:self._n]
        ny[:self._n] = self._y[:self._n]
        ns[:self._n] = self._s[:self._n]
        self._x, self._y, self._s = nx, ny, ns

    def _decimate(self) -> None:
        """Halve the stored points, keeping the full span (stride 2). Coarsens the
        display; the store keeps full resolution for the Timeline/analysis."""
        n = self._n
        h = (n + 1) // 2
        self._x[:h] = self._x[:n:2]
        self._y[:h] = self._y[:n:2]
        self._s[:h] = self._s[:n:2]
        self._n = h

    def trim(self, x_min: float) -> bool:
        """Drop points older than x_min (slide mode). Returns True if it changed
        anything. x is time-ordered (live append), so binary-search the cut."""
        if self._n and self._x[0] < x_min:
            i = int(np.searchsorted(self._x[:self._n], x_min, side="left"))
            if i:
                m = self._n - i
                self._x[:m] = self._x[i:self._n]
                self._y[:m] = self._y[i:self._n]
                self._s[:m] = self._s[i:self._n]
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
    def sigma(self):
        return self._s[:self._n]

    @property
    def has_sigma(self) -> bool:
        """True if any buffered sample carries an inline σ (a processor output that
        CREATES uncertainty). Device channels leave σ NaN and use the model path."""
        return self._n > 0 and bool(np.isfinite(self._s[:self._n]).any())
