"""HistoryBuffer — a bounded, always-on ring of recent readings.

The data plane's hot tier (in RAM): it lets a chart redraw on demand and lets
Record reach *backwards* (pre-roll) when the start marker is dragged before the
moment Record was pressed. Image/non-numeric values are skipped — this is the
scalar history, not a frame store.

Thread-safe (DESIGN §21.2): `feed` runs on the engine drain while `slice`/`span`
are read from resolver worker threads — a deque iterated during a cross-thread
append raises RuntimeError, so all access serializes behind one lock (per-batch
and per-query cost, never per-sample).
"""

from __future__ import annotations

import threading
from collections import deque


class HistoryBuffer:
    def __init__(self, window_s: float = 300.0):
        self._window = window_s
        self._data: dict[str, deque] = {}
        self._lock = threading.Lock()

    def feed(self, batch) -> None:
        with self._lock:
            for r in batch:
                if not isinstance(r.value, (int, float)):
                    continue
                dq = self._data.get(r.key)
                if dq is None:
                    dq = self._data[r.key] = deque()
                dq.append((r.t, r.value, r.status))
                cutoff = r.t - self._window
                while dq and dq[0][0] < cutoff:
                    dq.popleft()

    def slice(self, key: str, t0: float, t1: float) -> list:
        with self._lock:
            return [(t, v, s) for (t, v, s) in self._data.get(key, ())
                    if t0 <= t <= t1]

    def keys(self) -> list:
        with self._lock:
            return list(self._data)

    def span(self, key: str):
        """(oldest, newest) timestamp held for `key`, or None — the coverage
        this tier advertises to the resolver (DESIGN §7.4)."""
        with self._lock:
            dq = self._data.get(key)
            return (dq[0][0], dq[-1][0]) if dq else None
