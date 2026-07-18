"""PlaybackPrefetcher — pull hub history into the local PrefetchCache ahead of the
head, so replay never blocks the GUI and never silently skips hub data (DESIGN
§12.1). The worked-out model:

  * A worker thread, woken by TimeContext notifications + a 0.25 s heartbeat,
    fetches in TWO priorities: PHASE 1 fills the RUNWAY ahead of the head
    ([head, head+lookahead]) for every played source — this is the gate, so it
    runs first and unbudgeted (whether parked or playing, so play starts at once);
    PHASE 2 backfills the visible window BEHIND the head for the display, chunked
    NEAREST-HEAD-FIRST and bounded to a per-pass budget so the pass returns and the
    next one re-targets a moved head. Both fetch only the hub-covered-but-not-yet-
    local sub-ranges (`subtract(intersect(hub_cov, target), local_cov)`).
  * It publishes a WATERMARK = the nearest still-unfilled hub gap after the head
    (min over the played sources). `tick_play` gates on it: the head HOLDS at the
    watermark ("buffering…") rather than silently slowing — replay speed stays
    honest (a physicist calibrates rate intuition against it). Because the runway
    ahead is prefetched (phase 1), the watermark leads the head and play advances.
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

from ..core.periodic import PeriodicWorker
from .intervals import intersect as _intersect
from .intervals import subtract as _subtract

log = logging.getLogger("ferrodac.prefetch")


class PlaybackPrefetcher:
    def __init__(self, *, resolver, hub, cache, tc, sources_fn, store=None,
                 deliver=None, on_filled=None, now_fn=_time.time,
                 buffer_realtime_s: float = 3.0, chunk_s: float = 60.0,
                 per_pass: int = 32):
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
        self._chunk = float(chunk_s)      # fetch granularity: big enough that a wide
        #                                   backfill isn't thousands of tiny RPCs, small
        #                                   enough that the runway lands in one/few reads
        self._per_pass = int(per_pass)    # phase-2 backfill chunk budget per pass
        self._watermark: "float | None" = None
        self._wm_nav = -1                 # the tc.nav the watermark was computed for
        self._pin_seq = 0                 # unique epoch per pin (no cross-pin tail-append)
        self._pin_lock = threading.Lock()  # serialize pins (no two writers → no dup/tear)
        # the §21.4 shared loop skeleton: re-target on wake() (TimeContext
        # notifications), else a 0.25 s heartbeat; run_immediately stays False —
        # the first pass follows the first heartbeat/notify, as before.
        self._worker = PeriodicWorker(self._pass, 0.25, "fd-prefetch")
        self._unsub = None

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> bool:
        if self._worker.running or self.hub is None:
            return False
        self._unsub = self.tc.subscribe(self.wake)
        return self._worker.start()

    def stop(self) -> None:
        self._worker.stop(timeout=2.0)
        if self._unsub is not None:
            self._unsub()
            self._unsub = None
        self._watermark = None

    def wake(self) -> None:
        self._worker.wake()

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
    def _pass(self) -> None:
        nav = self.tc.nav                     # supersession token (navigation only)
        head = self.tc.head
        w0, w1 = self.tc.window
        self.cache.set_focus(head)
        now = self._now()
        lookahead = max(self._chunk, self.tc.speed * self._buffer_rt)
        lo = min(w0, head)
        front = min(head + lookahead, now)    # end of the play runway ahead of head
        sources = list(self.sources_fn() or [])
        if not sources or max(w1, front) <= lo:
            self._watermark = None            # nothing to prefetch → no gating
            self._wm_nav = nav
            return
        hub_covs = {}
        filled = False
        # PHASE 1 — the GATE: fill the runway AHEAD of the head [head, front] for
        # EVERY source. The watermark is the min over sources of the nearest unfilled
        # hub gap after the head, so play advances only once ALL played sources have
        # their runway cached — hence this runs FIRST and unbudgeted (it is small:
        # ~one chunk/source), or a single source's long history would starve the gate.
        # Filled even when PARKED, so hitting Play advances immediately.
        for src in sources:
            if self.tc.nav != nav:            # navigated → target stale; leave _wm_nav
                return                        # behind so the gate keeps holding
            hub_covs[src] = self._hub_cov(src)
            filled |= self._fill(src, hub_covs[src], head, [(head, front)], nav, None)
        if self.tc.nav != nav:
            return
        # Publish the watermark AS SOON AS the runway is cached — the gate opens now,
        # BEFORE the (slower) history backfill, so play starts within one chunk rather
        # than after a whole pass. The forward watermark depends only on coverage AHEAD
        # of the head, so phase 2 (all behind the head) never changes it.
        marks = [self._source_watermark(head, hub_covs.get(src, []),
                                        self.resolver.coverage(src, local_only=True))
                 for src in sources]
        marks = [m for m in marks if m is not None]   # non-hub sources don't gate
        self._watermark = min(marks) if marks else None
        self._wm_nav = nav                    # watermark is fresh for this position
        if filled:
            self._deliver(self._on_filled)    # runway landed → redraw near the head
        # PHASE 2 — the DISPLAY: backfill the visible window BEHIND the head, nearest-
        # head first, within a bounded budget so the pass returns promptly and the
        # NEXT pass re-targets a moved head (during play the runway must track it).
        budget = [self._per_pass]
        back = False
        for src in sources:
            if self.tc.nav != nav or budget[0] <= 0:
                break
            back |= self._fill(src, hub_covs[src], head, [(lo, head)], nav, budget)
        if back:
            self._deliver(self._on_filled)    # debounced: one redraw per backfill pass
            if budget[0] <= 0:                # backfill hit the budget → more to do:
                self._worker.wake()           # continue now, don't wait the heartbeat

    def _hub_cov(self, src) -> list:
        try:
            return list(self.hub.coverage(src))       # fresh (worker) — TTL-cached
        except Exception:                             # noqa: BLE001 — hub down → nothing to pull
            return []

    def _fill(self, src, hub_cov, head, target, nav, budget) -> bool:
        """Fetch the hub-covered-but-not-local chunks of `target` into the cache,
        NEAREST-HEAD first. `budget` (a 1-element list) caps the chunk count and is
        decremented; None fills all of `target`. Returns whether anything was fetched."""
        local_cov = self.resolver.coverage(src, local_only=True)
        need = _subtract(_intersect(hub_cov, target), local_cov)
        dtype = self._dtype(src)
        did = False
        for a, b in self._chunks_near(need, head):
            if self.tc.nav != nav or (budget is not None and budget[0] <= 0):
                break
            did = self._fetch_chunk(src, dtype, a, b) or did
            if budget is not None:
                budget[0] -= 1
        return did

    def _chunks_near(self, need, head) -> list:
        """Split `need` into ≤ chunk_s pieces ordered by distance from the head —
        AHEAD of the head first (the gate runway), then BEHIND (history), each
        nearest-first — so both the gate and the near-head display fill before far data."""
        chunks = []
        for a, b in need:
            c = a
            while c < b:
                d = min(c + self._chunk, b)
                chunks.append((c, d))
                c = d

        def _key(ab):
            a, b = ab
            if a >= head:                     # ahead of head → gate runway, nearest first
                return (0, a - head)
            if b <= head:                     # behind head → visible history, nearest first
                return (1, head - b)
            return (0, 0.0)                   # straddles the head
        chunks.sort(key=_key)
        return chunks

    def _source_watermark(self, head, hub_cov, local_cov):
        """How far play may advance for THIS source before it would outrun buffered
        hub data: the nearest point after `head` the HUB covers but the LOCAL tiers
        (incl. the cache) do not — an unfilled hub gap. Nothing ahead → free (now).

        A source with NO hub coverage returns None — it does NOT gate the hub buffer.
        The gate exists to hold play until HUB history is cached; a derived (processor
        output), local-only, offline, or image source has nothing on the hub to wait
        for, so letting it gate pins play at the head forever (the 'always buffering,
        never advancing' bug — dashboard.source_keys feeds ALL sources here, most not
        hub-backed). A transient coverage RPC failure is not conflated with this:
        `_fetch_coverage_blocking` returns the LAST-KNOWN intervals on failure, so a
        genuinely-hub-backed source keeps gating on its cached (non-empty) coverage."""
        if not hub_cov:
            return None                       # not hub-backed → does not gate the buffer
        unfilled = [iv for iv in _subtract(hub_cov, local_cov) if iv[1] > head]
        if unfilled:
            return max(head, min(a for a, _b in unfilled))
        return self._now()                    # confirmed nothing to fetch ahead → free

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
    def pin_sync(self, t0, t1, sources=None, epoch=None, progress=None,
                 should_stop=None) -> int:
        """Promote the hub's data over [t0,t1] into the local Zarr store so it
        survives a restart (the cache is RAM-only). SYNCHRONOUS on the caller's
        thread — the app runs it as a TaskRunner task (§21.4: a finite,
        user-triggered durable job gets progress/cancel/exit-gating; the old
        fire-and-forget fd-pin daemon thread was killed MID-ZARR-WRITE at exit).
        Returns the number of sources that gained data. ``progress(frac, detail)``
        and ``should_stop()`` are optional cooperative hooks; a stop between
        sources keeps every completed source durable."""
        if self.store is None or self.hub is None:
            return 0
        srcs = list(sources or self.sources_fn() or [])
        self._pin_seq += 1
        ep = epoch or f"pin-{int(t0)}-{self._pin_seq}"   # UNIQUE per pin: appends within
        #                                                  one epoch stay monotonic, and a
        #                                                  re-pin never tail-appends into an
        #                                                  older epoch's array (§7.4).
        with self._pin_lock:                        # serialize: one durable writer
            n = 0
            for i, src in enumerate(srcs):
                if should_stop is not None and should_stop():
                    break                           # completed sources stay durable
                if progress is not None:
                    progress(i / max(1, len(srcs)), src)
                try:
                    n += 1 if self._pin_source(src, float(t0), float(t1), ep) else 0
                except Exception:                   # noqa: BLE001
                    log.debug("pin %s failed", src, exc_info=True)
        return n

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
