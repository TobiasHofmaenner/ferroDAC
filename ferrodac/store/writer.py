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

import threading
import time

import numpy as np

from ..core.trace import Trace

_CHUNK = 4096            # samples buffered per source before a flush
_INTERVAL = 2.0         # …or this many seconds, whichever first (bounds crash loss:
#                         the durable store is now the sole crash-safe write path)
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
        self._device_records: dict = {}  # device_id -> merged provenance snapshot (pushed
        #                                  from the GUI thread; captured at flush time)
        self._device_written: dict = {}  # device_id -> last snapshot persisted (to diff)
        # `feed` runs on the writer's own bus pump thread (DESIGN §21.2), but
        # `flush_all` is also a cross-thread entry point — Stop-Recording calls it
        # from the GUI thread while the pump is live. This lock guards the pending
        # buffer so those never race (held only around buffer + append, never on
        # the per-reading hot path beyond a cheap append).
        self._buflock = threading.RLock()
        self._unsub = None
        # Rollup rebuilds are O(epoch) and grow all session — they must never run on
        # the pump thread (feed() can't drain during one → the store-writer backlog).
        # A dedicated worker drains this queue; the pump only enqueues.
        self._rollup_due: list = []
        self._rollup_cv = threading.Condition()
        self._rollup_stop = False
        self._rollup_thread = None
        # one epoch per app session, so a restart leaves a real coverage gap (the
        # resolver breaks the line there) instead of bridging stop→resume.
        self._epoch = "s%d" % int(time.time())

    # -- device provenance (snapshot pushed from the GUI thread) -------------
    def set_device_records(self, records: dict) -> None:
        """Latest merged provenance per device (id -> {fields}). Built on the GUI
        thread from descriptors + user metadata; persisted alongside the data on the
        engine thread at the next flush (so writes stay single-threaded). A single
        reference swap — safe to call concurrently with feed()."""
        self._device_records = {str(k): dict(v or {}) for k, v in (records or {}).items()}

    def _capture_device(self, key, t: float) -> None:
        """At a source's flush, persist/refresh its device's provenance record and
        append a change-log event for any field that changed. Best-effort: a
        persistence hiccup must never break acquisition. Skips non-device keys
        (virtual/derived/ui) — their prefix isn't a known device."""
        try:
            did = key.split("/", 1)[0]
            rec = self._device_records.get(did)
            if not rec:
                return
            prev = self._device_written.get(did)
            if prev == rec:
                return
            self.store.put_device(did, rec)
            for f, v in rec.items():
                if prev is None or prev.get(f) != v:
                    self.store.emit_device_meta(did, float(t), f, v)
            self._device_written[did] = dict(rec)
        except Exception:                            # noqa: BLE001 — never block writes
            pass

    # -- lifecycle -----------------------------------------------------------
    def attach(self, engine) -> None:
        # The durable write path runs on its OWN bus pump thread (DESIGN §21.2):
        # lossless + per-source ordered, and a slow disk can never stall the GUI.
        if self._unsub is None:
            self._unsub = engine.subscribe(self.feed, thread="worker",
                                           mode="lossless", name="store-writer")

    def stop(self) -> None:
        if self._unsub is not None:
            close = getattr(self._unsub, "close", None)
            if close is not None:
                close(timeout=10.0)     # deliver the backlog, then join the pump —
            else:                       # self-sufficient regardless of shutdown order
                self._unsub()
            self._unsub = None
        with self._rollup_cv:           # drain + retire the rollup worker
            self._rollup_stop = True
            self._rollup_cv.notify()
        if self._rollup_thread is not None:
            self._rollup_thread.join(timeout=10.0)
            self._rollup_thread = None
        self.flush_all()                # caller thread; race-free (pump is joined,
        for key in list(self._known):   # ZarrStore serializes behind its RLock)
            self._rollup(key)           # final rollups for fast historic query

    # -- ingest (the writer's own bus pump thread; flush_all may cross in) ----
    def feed(self, batch) -> None:
        now = time.monotonic()
        with self._buflock:
            for r in batch:
                if getattr(r, "partial", False):
                    continue                     # preview frame — only complete scans
                v = r.value
                if isinstance(v, Trace):
                    self._feed_trace(r.key, r.t, v)
                    continue
                if isinstance(v, bool):
                    v = 1.0 if v else 0.0        # persist bool as 0/1 scalar
                elif not isinstance(v, (int, float)):
                    continue
                if not np.isfinite(v):
                    continue                     # a failed read (devices emit NaN with
                #                                  status≠0) is NOT data: NaN in `v` poisons
                #                                  min/max rollup buckets, and the RAM tier
                #                                  already drops these — absence (a coverage
                #                                  gap) is the one representation of "no data"
                tb, vb = self._buf.setdefault(r.key, ([], []))
                tb.append(float(r.t))
                vb.append(float(v))
                if len(tb) >= self._chunk or \
                        now - self._last_flush.get(r.key, 0.0) > self._interval:
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
        self._capture_device(key, t)                 # provenance alongside the data
        self.store.append_trace(key, t, x, trace.y,
                                epoch=f"{self._epoch}__t{self._trace_gen[key]}")

    # -- internals (call with _buflock held) ---------------------------------
    def _flush(self, key) -> None:
        tb, vb = self._buf.get(key, ([], []))
        if not tb:
            return
        if key not in self._known:
            self.store.add_source(key, name=key)
            self._known.add(key)
        self._capture_device(key, tb[0])             # provenance alongside the data
        self.store.append(key, np.asarray(tb, dtype="f8"),
                          np.asarray(vb, dtype="f8"), epoch=self._epoch)
        n = len(tb)
        tb.clear()
        vb.clear()
        self._last_flush[key] = time.monotonic()
        self._since_rollup[key] = self._since_rollup.get(key, 0) + n
        if self._since_rollup[key] >= self._rollup_every:
            self._since_rollup[key] = 0
            self._queue_rollup(key)              # O(epoch) work → the rollup worker,
            #                                      NEVER inline on the pump thread

    def _queue_rollup(self, key) -> None:
        if self._rollup_thread is None:
            self._rollup_thread = threading.Thread(
                target=self._rollup_loop, name="fd-rollup", daemon=True)
            self._rollup_thread.start()
        with self._rollup_cv:
            if key not in self._rollup_due:
                self._rollup_due.append(key)
            self._rollup_cv.notify()

    def _rollup_loop(self) -> None:
        while True:
            with self._rollup_cv:
                while not self._rollup_due and not self._rollup_stop:
                    self._rollup_cv.wait()
                if self._rollup_due:
                    key = self._rollup_due.pop(0)
                else:                            # stopping and fully drained
                    return
            self._rollup(key)

    def _rollup(self, key) -> None:
        try:
            self.store.finalize_rollups(key, self._epoch)
            self._since_rollup[key] = 0
        except Exception:
            pass                                 # query falls back to raw-bucketing

    def flush_all(self) -> None:
        with self._buflock:
            for key in list(self._buf):
                self._flush(key)
