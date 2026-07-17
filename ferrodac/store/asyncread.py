"""ReadService — the one async entry point for UI-initiated resolver reads
(DESIGN §21.3).

The audit's critical Timeline finding: `Resolver.query`/`coverage` ran on the
GUI thread on every 500 ms tick (and per scrub), so a slow hub RPC or a
full-history zarr scan froze the paint thread. This moves all of that onto a
small worker pool and delivers results back via a `deliver` marshal (the GUI
thread in the app; inline in headless tests). Two mechanisms keep it cheap and
correct:

- **coverage TTL cache** — coverage grows slowly at the live edge, so the
  Timeline's 500 ms tick reads a ~2 s cache instead of rescanning every epoch.
- **key coalescing / supersession** — a newer request for the same key cancels
  the in-flight one (its result is discarded at delivery), so a fast scrub can
  never pile up work: at most one in-flight + the latest wins.

The tier protocol itself stays synchronous; the resolver + ZarrStore are made
thread-safe by their own locks (DESIGN §21.2), so reads here run concurrently
with the store-writer pump. Qt-free: `deliver` is the only seam to the GUI.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor


class Ticket:
    """A handle to a submitted read. `cancel()` is cooperative: a not-yet-run job
    is skipped, an in-flight one has its result discarded at delivery."""

    __slots__ = ("_cancelled",)

    def __init__(self):
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    @property
    def cancelled(self) -> bool:
        return self._cancelled


class ReadService:
    def __init__(self, resolver, deliver=None, max_workers: int = 2):
        # deliver(fn): run fn on the consumer's thread. The GUI app passes a
        # queued-signal marshal; headless/tests leave it None (call inline). Two
        # workers so a hub RPC stuck at its timeout can't starve local reads.
        self.resolver = resolver
        self._deliver = deliver or (lambda fn: fn())
        self._pool = ThreadPoolExecutor(max_workers=max_workers,
                                        thread_name_prefix="fd-read")
        self._lock = threading.Lock()
        self._pending: dict = {}          # key -> Ticket (supersession)
        self._cov_cache: dict = {}        # series -> (monotonic_ts, intervals)

    # -- windowed reads ------------------------------------------------------
    def query(self, series, t0, t1, max_points=2000, *, key=None,
              on_result=None, on_error=None) -> Ticket:
        return self._submit("query", (series, t0, t1, max_points),
                            key or ("query", series), on_result, on_error)

    def query_trace(self, series, t0, t1, max_scans=400, *, key=None,
                    on_result=None, on_error=None) -> Ticket:
        return self._submit("query_trace", (series, t0, t1, max_scans),
                            key or ("query_trace", series), on_result, on_error)

    def read_raw(self, series, t0, t1, *, key=None,
                 on_result=None, on_error=None) -> Ticket:
        return self._submit("read_raw", (series, t0, t1),
                            key or ("read_raw", series), on_result, on_error)

    def call(self, fn, *, key, on_result=None, on_error=None) -> Ticket:
        """Run an arbitrary store/resolver READ on the pool — §21.4: UI-initiated
        reads ride this service (supersession by key, result marshalled to the
        GUI), never an ad-hoc thread and never inline on the GUI thread. For
        reads the typed wrappers above don't cover (e.g. the startup source-info
        sweep). `fn` must be self-contained and thread-safe."""
        return self._submit("call", (fn,), key, on_result, on_error)

    def _call(self, kind, args):
        r = self.resolver
        if kind == "query":
            return r.query(*args)
        if kind == "query_trace":
            return r.query_trace(*args)
        if kind == "read_raw":
            return r.read_raw(*args)
        if kind == "call":
            return args[0]()
        raise ValueError(kind)

    def _submit(self, kind, args, key, on_result, on_error) -> Ticket:
        ticket = Ticket()
        with self._lock:
            old = self._pending.get(key)
            if old is not None:
                old.cancel()              # supersede: a newer read wins this key
            self._pending[key] = ticket

        def job():
            try:
                if ticket.cancelled:
                    return
                res = self._call(kind, args)
            except Exception as exc:       # noqa: BLE001 — reported, never crashes
                err = exc                  # bind: `exc` is cleared after the block,
                if not ticket.cancelled and on_error is not None:   # a deferred
                    self._deliver(lambda: on_error(err))            # lambda needs it
                return
            finally:
                with self._lock:
                    if self._pending.get(key) is ticket:
                        self._pending.pop(key, None)
            if not ticket.cancelled and on_result is not None:
                self._deliver(lambda: on_result(res))

        self._pool.submit(job)
        return ticket

    # -- coverage (TTL-cached; the Timeline tick's hot path) -----------------
    def coverage_many(self, series_list, *, ttl=2.0, key="coverage-many",
                      on_result=None) -> Ticket:
        """Coverage for many sources at once, honouring a per-series TTL cache.
        Delivers ``{series: [(t0,t1), ...]}``. All-cached → delivered without
        touching a worker; any stale → one background pass refreshes them."""
        series_list = list(series_list)
        now = time.monotonic()
        cached, stale = {}, []
        with self._lock:
            for s in series_list:
                ent = self._cov_cache.get(s)
                if ent is not None and now - ent[0] < ttl:
                    cached[s] = ent[1]
                else:
                    stale.append(s)
        ticket = Ticket()
        if not stale:
            if on_result is not None:
                self._deliver(lambda: on_result(cached))
            return ticket
        with self._lock:
            old = self._pending.get(key)
            if old is not None:
                old.cancel()
            self._pending[key] = ticket

        def job():
            result = dict(cached)
            try:
                for s in stale:
                    if ticket.cancelled:
                        return
                    iv = self.resolver.coverage(s)
                    result[s] = iv
                    with self._lock:
                        self._cov_cache[s] = (time.monotonic(), iv)
            finally:
                with self._lock:
                    if self._pending.get(key) is ticket:
                        self._pending.pop(key, None)
            if not ticket.cancelled and on_result is not None:
                self._deliver(lambda: on_result(result))

        self._pool.submit(job)
        return ticket

    def invalidate(self, series=None) -> None:
        """Drop cached coverage (hub connect/disconnect, epoch roll)."""
        with self._lock:
            if series is None:
                self._cov_cache.clear()
            else:
                self._cov_cache.pop(series, None)

    def shutdown(self) -> None:
        with self._lock:
            for t in self._pending.values():
                t.cancel()
            self._pending.clear()
        self._pool.shutdown(wait=False)
