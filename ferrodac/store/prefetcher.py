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

import numpy as np

from .intervals import intersect as _intersect
from .intervals import merge as _merge
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
        self._wm_nav = -1                 # the tc.nav the watermark was computed for
        self._pin_seq = 0                 # unique epoch per pin (no cross-pin tail-append)
        self._pin_lock = threading.Lock()  # serialize pins (no two writers → no dup/tear)
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
        """The gate reads this on the GUI thread — a plain field read, never blocks.
        If a navigation happened since the watermark was computed (a scrub) OR no
        pass has run yet, the old value is meaningless — return the CURRENT head so
        the gate HOLDS there (never free-runs into unbuffered hub data, §12.1)
        until the next pass recomputes a fresh watermark for this nav."""
        if self._wm_nav != self.tc.nav:
            return self.tc.head
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
            self._watermark = None            # nothing to prefetch → no gating
            self._wm_nav = nav
            return
        filled = False
        marks = []
        for src in sources:
            if self.tc.nav != nav:            # navigated → this target is stale; leave
                return                        # _wm_nav behind so the gate keeps holding
            got = self._fill_source(src, head, lo, hi, nav)
            filled = filled or got[0]
            marks.append(got[1])
        if self.tc.nav != nav:
            return
        self._watermark = min(marks) if marks else None
        self._wm_nav = nav                    # watermark is fresh for this position
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
                did = self._fetch_chunk(src, dtype, c, d) or did
                c = d
            if self.tc.nav != nav:
                break
        local2 = self.resolver.coverage(src, local_only=True)   # now incl. what we filled
        return did, self._source_watermark(head, hub_cov, local2)

    def _source_watermark(self, head, hub_cov, local_cov):
        """How far play may safely advance for this source: the nearest point after
        `head` that the HUB covers but the LOCAL tiers (incl. the cache) do not — an
        unfilled gap. None ahead → free (capped at now). But if hub_cov is EMPTY we
        can't confirm there's nothing ahead (a coverage-refresh failure looks the
        same as genuinely-no-data), so we hold at the LOCAL data edge rather than
        free-run — safe against the §12.1 silent-skip."""
        unfilled = [iv for iv in _subtract(hub_cov, local_cov) if iv[1] > head]
        if unfilled:
            return max(head, min(a for a, _b in unfilled))
        if not hub_cov:
            return self._local_end(local_cov, head)
        return self._now()                    # confirmed nothing to fetch ahead → free

    @staticmethod
    def _local_end(local_cov, head):
        """The end of the contiguous local coverage from `head` (how far local data
        runs without a gap)."""
        end = head
        for a, b in _merge(local_cov):
            if b <= end:
                continue
            if a <= end + 1e-6:               # contiguous with what we have
                end = max(end, b)
            else:
                break                         # a real gap between `end` and this interval
        return end

    def _fetch_chunk(self, src, dtype, a, b) -> bool:
        """Fetch [a,b] from the hub into the cache. Uses the STRICT hub read that
        RAISES on an RPC failure, so we distinguish two empties that must be handled
        oppositely (§12.1): a genuine no-data range (empty result, no error) is
        MARKED covered so play advances across it with a NaN break — not marking it
        would freeze playback forever at any real sub-30 s recording gap; a transient
        FAILURE raises → NOT marked → retried, so it can't mask real hub data.
        Returns whether the range is now cached (advance) vs must be retried."""
        try:
            if dtype == "trace":
                self.cache.add_trace(src, self._hub_read_trace(src, a, b), a, b)
            else:
                t, v = self._hub_read(src, a, b)
                self.cache.add_scalar(src, t, v, a, b)
            return True                               # success (incl. genuine empty)
        except Exception:                             # noqa: BLE001 — RPC failure → retry
            log.debug("prefetch %s[%.1f,%.1f] failed", src, a, b, exc_info=True)
            return False

    def _hub_read(self, src, a, b):
        return (getattr(self.hub, "read_raw_strict", None) or self.hub.read_raw)(src, a, b)

    def _hub_read_trace(self, src, a, b):
        return (getattr(self.hub, "read_raw_trace_strict", None)
                or self.hub.read_raw_trace)(src, a, b)

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
        self._pin_seq += 1
        ep = epoch or f"pin-{int(t0)}-{self._pin_seq}"   # UNIQUE per pin: appends within
        #                                                  one epoch stay monotonic, and a
        #                                                  re-pin never tail-appends into an
        #                                                  older epoch's array (§7.4).

        def work():
            with self._pin_lock:                        # serialize: one durable writer
                n = 0
                for src in srcs:
                    try:
                        n += 1 if self._pin_source(src, float(t0), float(t1), ep) else 0
                    except Exception:                   # noqa: BLE001
                        log.debug("pin %s failed", src, exc_info=True)
            if on_done is not None:
                self._deliver(lambda: on_done(n))

        threading.Thread(target=work, name="fd-pin", daemon=True).start()
        return True

    def _pin_source(self, src, t0, t1, epoch) -> bool:
        store, dtype = self.store, self._dtype(src)
        # only pin what the DURABLE store LACKS — idempotent (a re-pin finds it
        # already durable and fetches nothing) and never duplicates an overlapping
        # local epoch (ZarrStore.read_raw concatenates epochs with no dedup).
        try:
            have = store.coverage(src)
        except Exception:                             # noqa: BLE001 — can't dedup safely →
            return False                              # ABORT rather than re-append blindly
        need = _subtract([(float(t0), float(t1))], have)
        if not need:
            return False

        def _fresh(t):                                # samples NOT already in the store —
            keep = np.ones(len(t), dtype=bool)        # a gap endpoint IS a real sample the
            for lo, hi in have:                       # hub re-reads, so filter it out
                keep &= ~((t >= lo) & (t <= hi))
            return keep

        wrote = False
        if dtype == "trace":
            store.add_source(src, dtype="trace")
            # each hub block is one config-epoch (its own swept axis); write each under
            # an epoch keyed by that axis, so append_trace never drops a differing-bin
            # block or binds one axis's scans to another's (§7.4). gen/last_x run ACROSS
            # all need-ranges — a per-range reset would collide two axes on the same key.
            gen, last_x = 0, None
            for a, b in need:
                for (bt, by, bx) in self._hub_read_trace(src, a, b):
                    bt = np.asarray(bt, dtype="f8")
                    bx = np.asarray(bx, dtype="f8")
                    if last_x is None or last_x.shape != bx.shape \
                            or not np.allclose(last_x, bx, rtol=1e-4, atol=1e-6):
                        gen += 1
                        last_x = bx
                    ep = f"{epoch}__t{gen}"
                    keep = _fresh(bt)
                    for i in range(len(bt)):
                        if keep[i]:
                            store.append_trace(src, float(bt[i]), bx, by[i], epoch=ep)
                            wrote = True
            return wrote
        store.add_source(src)
        for a, b in need:
            t, v = self._hub_read(src, a, b)
            if len(t):
                m = _fresh(np.asarray(t, dtype="f8"))
                if m.any():
                    store.append(src, np.asarray(t)[m], np.asarray(v)[m], epoch=epoch)
                    wrote = True
        return wrote
