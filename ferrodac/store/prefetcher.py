"""PlaybackPrefetcher — pull hub history into the local PrefetchCache ahead of the
head, so replay never blocks the GUI and never silently skips hub data (DESIGN
§12.1). The worked-out model:

  * A worker thread, woken by TimeContext notifications + a 0.25 s heartbeat,
    computes a TARGET range (the parked window, or [head, head+lookahead] while
    playing) and fetches the sub-ranges that are hub-covered but not yet local
    (`subtract(intersect(hub_cov, target), local_cov)`) from the hub into the
    cache, chunked and nearest-to-head first.
  * It publishes a WATERMARK = the nearest still-unfilled hub gap after the head
    (min over the played sources). `tick_play` gates on it: the head HOLDS at the
    watermark ("buffering…") rather than silently slowing — replay speed stays
    honest (a physicist calibrates rate intuition against it).
  * On fill it asks for a redraw (debounced), so the synchronous local display
    re-reads and completes as ranges land.
  * Supersession is by TimeContext.nav (bumps only on navigation, NOT on play/
    pause), so continuous play keeps filling while a scrub drops the old target.

Off-thread work may BLOCK on the hub freely (that is fine — waiting, not
freezing). Qt-free: `deliver` marshals the redraw to the consumer's thread.
"""

from __future__ import annotations

import logging
import threading
import time as _time

from .intervals import intersect as _intersect
from .intervals import subtract as _subtract

log = logging.getLogger("ferrodac.prefetch")


class PlaybackPrefetcher:
    def __init__(self, *, resolver, hub, cache, tc, sources_fn, store=None,
                 deliver=None, on_filled=None, now_fn=_time.time,
                 buffer_realtime_s: float = 3.0, chunk_s: float = 4.0):
        self.resolver = resolver          # local coverage (local_only) + tier host
        self.hub = hub                    # HubReadTier: coverage + read_raw[_trace]
        self.cache = cache                # PrefetchCache to fill
        self.tc = tc                      # TimeContext (window/head/playing/nav)
        self.sources_fn = sources_fn      # () -> [source keys] on screen / replayed
        self.store = store                # local ZarrStore (Phase 3 pin), optional
        self._deliver = deliver or (lambda fn: fn())
        self._on_filled = on_filled or (lambda: None)
        self._now = now_fn
        self._buffer_rt = float(buffer_realtime_s)   # seconds of realtime runway ahead
        self._chunk = float(chunk_s)
        self._watermark: "float | None" = None
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: "threading.Thread | None" = None
        self._unsub = None

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> bool:
        if self._thread is not None or self.hub is None:
            return False
        self._unsub = self.tc.subscribe(self.wake)
        self._thread = threading.Thread(target=self._run, name="fd-prefetch",
                                        daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._unsub is not None:
            self._unsub()
            self._unsub = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        self._watermark = None

    def wake(self) -> None:
        self._wake.set()

    def buffered_until(self) -> "float | None":
        """The gate reads this on the GUI thread — a plain float read, never blocks."""
        return self._watermark

    # -- worker --------------------------------------------------------------
    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(timeout=0.25)     # re-target on notify, else heartbeat
            self._wake.clear()
            if self._stop.is_set():
                break
            try:
                self._pass()
            except Exception:                 # noqa: BLE001 — a bad pass never kills the loop
                log.debug("prefetch pass failed", exc_info=True)

    def _pass(self) -> None:
        nav = self.tc.nav                     # supersession token (navigation only)
        playing = self.tc.playing
        head = self.tc.head
        w0, w1 = self.tc.window
        self.cache.set_focus(head)
        lo = min(w0, head)
        hi = (head + max(self._chunk, self.tc.speed * self._buffer_rt)) if playing else w1
        hi = min(hi, self._now())
        sources = list(self.sources_fn() or [])
        if not sources or hi <= lo:
            self._watermark = None
            return
        filled = False
        marks = []
        for src in sources:
            if self.tc.nav != nav:            # navigated → this target is stale
                return
            got = self._fill_source(src, head, lo, hi, nav)
            filled = filled or got[0]
            marks.append(got[1])
        self._watermark = min(marks) if marks else None
        if filled:
            self._deliver(self._on_filled)    # debounced: one redraw per pass

    def _fill_source(self, src, head, lo, hi, nav) -> tuple:
        """Fetch this source's unfilled hub gaps in [lo,hi]; return (did_fetch,
        source_watermark)."""
        try:
            hub_cov = list(self.hub.coverage(src))    # fresh (worker) — TTL-cached
        except Exception:                             # noqa: BLE001 — hub down → nothing to pull
            hub_cov = []
        local_cov = self.resolver.coverage(src, local_only=True)
        need = _subtract(_intersect(hub_cov, [(lo, hi)]), local_cov)
        need = sorted((iv for iv in need if iv[1] > iv[0]),
                      key=lambda iv: abs(iv[0] - head))   # nearest the head first
        dtype = self._dtype(src)
        did = False
        for a, b in need:
            c = a
            while c < b:
                if self.tc.nav != nav:                 # scrubbed away mid-fill → stop
                    break
                d = min(c + self._chunk, b)
                self._fetch_chunk(src, dtype, c, d)
                did = True
                c = d
            if self.tc.nav != nav:
                break
        # watermark: nearest hub gap after the head that is STILL not local
        local2 = self.resolver.coverage(src, local_only=True)
        rem = [iv for iv in _subtract(_intersect(hub_cov, [(head, hi)]), local2)
               if iv[1] > head + 1e-6]
        return did, (hi if not rem else max(head, min(a for a, _b in rem)))

    def _fetch_chunk(self, src, dtype, a, b) -> None:
        try:
            if dtype == "trace":
                self.cache.add_trace(src, self.hub.read_raw_trace(src, a, b), a, b)
            else:
                t, v = self.hub.read_raw(src, a, b)
                self.cache.add_scalar(src, t, v, a, b)
        except Exception:                             # noqa: BLE001
            log.debug("prefetch %s[%.1f,%.1f] failed", src, a, b, exc_info=True)
            self.cache.add_scalar(src, [], [], a, b)  # mark fetched (empty) → don't respin

    def _dtype(self, src) -> str:
        f = getattr(self.hub, "source_dtype", None)
        try:
            return f(src) if f is not None else "scalar"
        except Exception:                             # noqa: BLE001
            return "scalar"

    # -- Phase 3: pin a window into the DURABLE store ------------------------
    def pin(self, t0, t1, sources=None, epoch=None, on_done=None) -> bool:
        """Promote the hub's data over [t0,t1] into the local Zarr store so it
        survives a restart (the cache is RAM-only). Off-thread; `on_done(n)` is
        delivered on the consumer thread with the source count written."""
        if self.store is None or self.hub is None:
            return False
        srcs = list(sources or self.sources_fn() or [])
        ep = epoch or f"pin-{int(t0)}"

        def work():
            n = 0
            for src in srcs:
                try:
                    n += 1 if self._pin_source(src, float(t0), float(t1), ep) else 0
                except Exception:                     # noqa: BLE001
                    log.debug("pin %s failed", src, exc_info=True)
            if on_done is not None:
                self._deliver(lambda: on_done(n))

        threading.Thread(target=work, name="fd-pin", daemon=True).start()
        return True

    def _pin_source(self, src, t0, t1, epoch) -> bool:
        store, dtype = self.store, self._dtype(src)
        if dtype == "trace":
            blocks = self.hub.read_raw_trace(src, t0, t1)
            if not blocks:
                return False
            store.add_source(src, dtype="trace")
            for (bt, by, bx) in blocks:
                for i in range(len(bt)):
                    store.append_trace(src, float(bt[i]), bx, by[i], epoch=epoch)
            return True
        t, v = self.hub.read_raw(src, t0, t1)
        if not len(t):
            return False
        store.add_source(src)
        store.append(src, t, v, epoch=epoch)
        return True
