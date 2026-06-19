"""StoreWriter — always-on durable persistence of the live stream (DESIGN §7.4).

Subscribes to the engine (like the RAM HistoryBuffer does) and **continuously**
flushes every scalar reading into the durable Zarr store, chunk-wise. This is the
*ambient durable* tier: it grows as data arrives so you can scroll back past the
RAM ring, survive a restart, and **retroactively record** a span you didn't hit
Record on — the data is already on disk. Recording stays a separate concern (it
pins a span + materialises CSV over the marked area); this just never loses the
raw.

Grows indefinitely for now (retention config arrives with the search UI). Rollups
are rebuilt on a coarse cadence so query stays fast without paying O(N) per flush.
Qt-free. Scalar only this slice (traces ride in with the trace-epoch work).
"""

from __future__ import annotations

import time

import numpy as np

from ..core.trace import Trace

_CHUNK = 4096            # samples buffered per source before a flush
_INTERVAL = 5.0         # …or this many seconds, whichever first
_ROLLUP_EVERY = 50_000  # rebuild a source's rollup pyramid every N new samples


class StoreWriter:
    def __init__(self, store, chunk=_CHUNK, flush_interval=_INTERVAL,
                 rollup_every=_ROLLUP_EVERY):
        self.store = store
        self._chunk = chunk
        self._interval = flush_interval
        self._rollup_every = rollup_every
        self._buf: dict = {}            # key -> ([t...], [v...])
        self._known: set = set()        # sources declared in the store
        self._last_flush: dict = {}     # key -> monotonic seconds
        self._since_rollup: dict = {}   # key -> samples appended since last rollup
        self._trace_x: dict = {}        # key -> last axis seen (for epoch rolling)
        self._trace_gen: dict = {}      # key -> axis generation (epoch suffix)
        self._unsub = None
        # one epoch per app session, so a restart leaves a real coverage gap (the
        # resolver breaks the line there) instead of bridging stop→resume.
        self._epoch = "s%d" % int(time.time())

    # -- lifecycle -----------------------------------------------------------
    def attach(self, engine) -> None:
        if self._unsub is None:
            self._unsub = engine.subscribe(self.feed)

    def stop(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None
        self.flush_all()
        for key in list(self._known):           # final rollups for fast historic query
            self._rollup(key)

    # -- ingest (engine thread) ----------------------------------------------
    def feed(self, batch) -> None:
        now = time.monotonic()
        for r in batch:
            if getattr(r, "partial", False):
                continue                         # preview frame — only complete scans
            v = r.value
            if isinstance(v, Trace):
                self._feed_trace(r.key, r.t, v)
                continue
            if isinstance(v, bool):
                v = 1.0 if v else 0.0            # persist bool as 0/1 scalar
            elif not isinstance(v, (int, float)):
                continue
            tb, vb = self._buf.setdefault(r.key, ([], []))
            tb.append(float(r.t)); vb.append(float(v))
            if len(tb) >= self._chunk or now - self._last_flush.get(r.key, 0.0) > self._interval:
                self._flush(r.key)

    def _feed_trace(self, key, t, trace) -> None:
        x = np.asarray(trace.x, dtype="f8")
        if len(x) == 0:
            return
        last = self._trace_x.get(key)
        # A new config-epoch ONLY on a MEANINGFUL axis change (shape, or values
        # beyond tolerance). Real instruments (RGA) jitter the swept axis by tiny
        # floats every scan — an exact compare would roll a fresh epoch per scan,
        # fragmenting the store into one-scan epochs (ribbon dots, empty waterfall).
        if last is None or last.shape != x.shape \
                or not np.allclose(last, x, rtol=1e-4, atol=1e-6):
            self._trace_gen[key] = self._trace_gen.get(key, -1) + 1   # axis change
            self._trace_x[key] = x
        if key not in self._known:
            self.store.add_source(key, name=key, dtype="trace")
            self._known.add(key)
        self.store.append_trace(key, t, x, trace.y,
                                epoch=f"{self._epoch}__t{self._trace_gen[key]}")

    # -- internals -----------------------------------------------------------
    def _flush(self, key) -> None:
        tb, vb = self._buf.get(key, ([], []))
        if not tb:
            return
        if key not in self._known:
            self.store.add_source(key, name=key)
            self._known.add(key)
        self.store.append(key, np.asarray(tb, dtype="f8"),
                          np.asarray(vb, dtype="f8"), epoch=self._epoch)
        n = len(tb)
        tb.clear(); vb.clear()
        self._last_flush[key] = time.monotonic()
        self._since_rollup[key] = self._since_rollup.get(key, 0) + n
        if self._since_rollup[key] >= self._rollup_every:
            self._rollup(key)

    def _rollup(self, key) -> None:
        try:
            self.store.finalize_rollups(key, self._epoch)
            self._since_rollup[key] = 0
        except Exception:
            pass                                 # query falls back to raw-bucketing

    def flush_all(self) -> None:
        for key in list(self._buf):
            self._flush(key)
