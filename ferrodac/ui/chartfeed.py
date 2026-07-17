"""ChartFeed — the single owner of chart curve-buffer writes (DESIGN §22, I-6).

Steps 1+2 of the §22.2 migration: the three query-draw paths (parked window
draw, zoom re-query, route backfill) live here, and `reconcile()` is THE entry
point for display-state changes — it derives the named LIVE|PARKED|PLAYING mode
(TimeContext.mode, I-8) once and makes every chart's ownership + drawn content
match it. Charts never decide ownership; per-key sets, `own` flags, and the
per-tick clear-on-Play calls are gone. The live bus feed still reaches panels
directly (that seam moves in step 3).

Collaborators are injected as zero-arg accessors because they are created in
stages during app startup and are None in degraded modes (no durable store,
headless); accessors also keep ChartFeed constructible in Qt-free tests.
"""

from __future__ import annotations

import logging

from ..core.reading import Reading
from ..core.trace import Trace
from ..store.replay import Mode

_BACKFILL_POINTS = 4000

log = logging.getLogger("ferrodac")


class ChartFeed:
    def __init__(self, panels, resolver, reads, time_context, replay):
        self._panels = panels           # () -> iterable of dashboard panels
        self._resolver = resolver       # () -> Resolver | None
        self._reads = reads             # () -> ReadService | None
        self._tc = time_context         # () -> TimeContext | None
        self._replay = replay           # () -> ReplayController | None
        self._last_mode: Mode | None = None
        self._is_derived = lambda key: False         # set by attach()

    # -- feed streams (DESIGN §22 steps 3+4) ---------------------------------------
    def attach(self, live_bus, playback_bus=None, is_derived=None) -> None:
        """Wire the two feed streams through this single forwarding point (I-6/I-7/I-9):

        - ENGINE bus → panels, only while mode is LIVE (the live tail is a LIVE-mode
          optimization; a parked/playing chart shows its historic window and live
          acquisition must not pollute it — the gate the old _on_live provided).
        - Playback bus → panels, filtered to DERIVED readings and TRACES. Raw
          historic scalars never reach a panel: parked curves are query-drawn,
          playing curves append via advance() — the re-stream serves PROCESSORS
          (I-9) and waterfall/spectrum trace displays.

        In degraded mode (no durable store) there is no playback bus, no
        TimeContext, and the engine feeds everything, exactly as before."""
        self._is_derived = is_derived or (lambda key: False)
        live_bus.subscribe(self._forward_live)
        if playback_bus is not None and playback_bus is not live_bus:
            playback_bus.subscribe(self._forward_playback)

    def _forward_live(self, batch) -> None:
        tc = self._tc()
        if tc is not None and not tc.following:
            return                                   # parked/playing: live stays out
        self._deliver(batch)

    def _forward_playback(self, batch) -> None:
        filt = [r for r in batch
                if isinstance(r.value, Trace) or self._is_derived(r.key)]
        if filt:
            self._deliver(filt)

    def _deliver(self, batch) -> None:
        """Fan a batch to every display panel's feed — the same per-sink isolation
        the Bus gives inline subscribers (one bad panel never starves the rest)."""
        for panel in self._panels():
            feed = getattr(panel, "feed", None)
            if feed is None:
                continue                             # input widgets have no feed
            try:
                feed(batch)
            except Exception:                        # noqa: BLE001 — panels isolated
                log.debug("panel feed failed", exc_info=True)

    def advance(self, seg0: float, seg1: float) -> None:
        """PLAYING: the playhead walked prev→new front — append the newly-entered
        slice to each chart's stored scalar curves through the owner (§22 step 4:
        raw re-stream data no longer reaches panels; this is the ONE raw path into
        a playing chart). Slices are ≤ speed×tick wide, so a handful of samples;
        the resolver serves them from the RAM ring when playing near now. Synthetic
        Readings go through feed() so conversion, σ lanes, and the monotonic guard
        apply unchanged."""
        resolver, replay = self._resolver(), self._replay()
        if resolver is None or replay is None or seg1 <= seg0:
            return
        for panel in self._panels():
            if not hasattr(panel, "set_window_curve"):
                continue                             # not a scalar chart
            feed = getattr(panel, "feed", None)
            if feed is None:
                continue
            for key in panel.curve_keys():
                # LOCAL tiers only: this runs per play tick (20 Hz) — the hub
                # tier's networked coverage/read (4 s timeout) must be
                # unreachable from here. A hub-only curve simply doesn't
                # advance point-by-point; the next park/render draws it whole.
                if replay.playback._is_trace(key) \
                        or not resolver.knows(key, local_only=True):
                    continue                         # traces ride the re-stream
                try:
                    t, v = resolver.read_raw(key, seg0, seg1, local_only=True)
                except Exception:                    # noqa: BLE001 — one bad curve
                    continue
                if not len(t):
                    continue
                dev, _, src = key.rpartition("/")
                feed([Reading(dev, src, float(t[i]), float(v[i]))
                      for i in range(len(t))])

    # -- route backfill (#8) ---------------------------------------------------
    def backfill_route(self, source_key: str, panel) -> None:
        """A source was just routed onto a chart → backfill it from its recorded history
        over the CURRENT window, so the chart shows the existing data instead of starting
        live from the click moment (#8). Fed DIRECTLY to that panel (not the shared replay
        bus) so panels already showing this source aren't double-fed.

        Scalars come from a BOUNDED, DOWNSAMPLED query (the display only needs ~pixels of
        detail): rollup-backed, so it's ~10 ms even over a whole-session grow window and
        returns ~display-resolution points — NOT the full-res `read_raw`, which read
        millions of samples on the GUI thread (measured multi-second freezes on long
        sessions) only for the chart buffer to decimate them away. Because the query is
        cheap it stays SYNCHRONOUS on the GUI thread, so no live tick interleaves older
        history behind newer points. Traces (low volume) keep their full read. No-op with
        no store / no feed target."""
        replay, resolver, tc = self._replay(), self._resolver(), self._tc()
        if (replay is None or resolver is None
                or tc is None or not hasattr(panel, "feed")):
            return
        t0, t1 = tc.window
        try:
            if replay.playback._is_trace(source_key):
                # ASYNC (ReadService): the full trace read blocked the GUI for the
                # whole zarr read during a project load (watchdog: 1.8 s locally,
                # worse on the lab box). Off-thread it may also wait on the hub —
                # so a hub-only trace now backfills too (the old local_only existed
                # ONLY because this was synchronous). Scans are bounded (~400) and
                # the waterfall bins by absolute time, so late delivery interleaving
                # with live scans is order-safe.
                reads = self._reads()
                if reads is None:              # headless/tests: bounded sync read
                    readings = replay.playback.read_window([source_key], t0, t1,
                                                           local_only=True)
                    if readings:
                        panel.feed(readings)
                    return
                dev, _, src = source_key.rpartition("/")

                def _deliver(blocks, panel=panel, dev=dev, src=src):
                    readings = [Reading(dev, src, float(t[j]), Trace(x=x, y=Y[j]))
                                for (t, Y, x) in blocks for j in range(len(t))]
                    try:
                        if readings:
                            panel.feed(readings)
                    except RuntimeError:       # the panel was removed before the
                        pass                   # read landed — nothing to draw on
                reads.query_trace(source_key, t0, t1, max_scans=400,
                                  key=("backfill-trace", source_key),
                                  on_result=_deliver)
                return
            if hasattr(panel, "set_window_curve"):
                # ONE envelope representation end-to-end (§22 I-10): draw the same
                # min/max polyline the parked/zoom paths use. The old midline
                # collapse halved every spike, so the identical data showed a
                # different amplitude depending on which path drew it. Ownership
                # follows the mode, exactly like reconcile: PARKED → the query owns
                # the new curve (feed skips it); LIVE/PLAYING → un-owned, and the
                # tail / play slices append on top past the envelope's end.
                x, y = resolver.query(source_key, t0, t1, max_points=_BACKFILL_POINTS)
                if len(x) == 0:
                    return
                if tc.mode is Mode.PARKED:
                    panel.set_query_owned(panel._query_owned | {source_key})
                panel.set_window_curve(source_key, x, y)
                return
            # non-chart value displays (7-seg, bars): churn to the latest value
            from ..store.decimate import envelope_midline
            x, y = resolver.query(source_key, t0, t1, max_points=_BACKFILL_POINTS)
            x, y = envelope_midline(x, y)
            dev, _, src = source_key.rpartition("/")
            readings = [Reading(dev, src, float(x[i]), float(y[i]))
                        for i in range(len(x)) if x[i] == x[i]]   # drop NaN gap markers
        except Exception as exc:                    # noqa: BLE001 — never break a route
            log.debug("route backfill failed for %s: %s", source_key, exc)
            return
        if readings:
            panel.feed(readings)

    # -- reconcile: THE display-state entry point (DESIGN §22 I-8) ---------------
    def reconcile(self, force: bool = False) -> None:
        """Derive the named mode ONCE and make every chart match it.

        - LIVE:    the feed owns every curve. On a reset (force=True — go-live or
                   the grow tail dragged back), draw the window's historic envelope
                   UN-OWNED so feed() keeps appending the live tail on top (the
                   feed monotonicity guard drops the overlap).
        - PARKED:  the query owns each STORED SCALAR curve (derived / trace /
                   unrecorded keys stay on the feed path): assign ownership, then
                   draw pixel-budgeted envelopes.
        - PLAYING: the feed owns every curve again (release ownership so the play
                   slices from advance() append). The window's historic envelope is
                   REDRAWN un-owned so the whole selected window stays visible with
                   the playhead sweeping across it. This redraw is essential: since
                   §22 step 4 the re-stream no longer refeeds raw scalars, so nothing
                   else rebuilds the backdrop — without it, hitting Play blanked the
                   chart down to the sliver advance() had swept forward.

        Cheap when nothing changed: transport ticks call this every frame and it
        no-ops unless the mode flipped or force=True (navigation: the reset path —
        window or sources changed, so owned curves must redraw)."""
        tc = self._tc()
        if tc is None:
            return
        mode = tc.mode
        changed = mode is not self._last_mode
        self._last_mode = mode
        if not changed and not force:
            return
        if mode is Mode.PARKED:
            self._draw_windows(tc, owned=True)
        else:
            for panel in self._panels():
                if hasattr(panel, "set_query_owned"):
                    panel.set_query_owned(())        # release → clears released bufs
            if mode is Mode.PLAYING or force:
                # Historic envelope drawn UNDER the appending tail: on every
                # PARKED→PLAYING transition (so the window is visible from the first
                # play frame), and on the LIVE reset path (go-live / grow-extend-back).
                # Un-owned so feed()/advance() keep appending on top (the monotonicity
                # guard drops the overlap) — owned would make the feed skip it and the
                # curve would scroll out of view as the window slides forward.
                self._draw_windows(tc, owned=False)

    def _draw_windows(self, tc, owned: bool) -> None:
        """Draw each chart's stored SCALAR curves from a pixel-budgeted store query
        (a min/max envelope) rather than the full-res re-stream fanned into the
        CurveBuffer — one uniform reduction, no old→new fidelity gradient, and zoom
        re-resolves. The re-stream still runs for PROCESSORS; derived / trace /
        not-yet-recorded curves stay on the feed path.

        ASYNC via ReadService (§21.4): ownership flips synchronously (the feed gate
        must be correct immediately) but the queries ride the read pool and each
        envelope lands when ready — the old synchronous loop claimed it 'stays
        local+sync so it can never block on the hub', yet the field watchdog caught
        it blocking the GUI 3.5 s MEDIAN × 67 events (big dirty epochs + store-lock
        contention during prefetch backfill; the local store is not free either).
        Delivery is guarded by ownership and superseded per (panel, key) — the same
        key the zoom re-query uses, so the newest request wins."""
        resolver, replay = self._resolver(), self._replay()
        if resolver is None or replay is None:
            return
        t0, t1 = tc.window
        for panel in self._panels():
            if not hasattr(panel, "set_window_curve"):
                continue                             # not a chart
            try:
                keys = [k for k in panel.curve_keys()
                        if not replay.playback._is_trace(k)        # scalars only
                        and resolver.knows(k, local_only=True)]    # in a LOCAL tier; O(1)
                if not keys:
                    continue
                panel.set_query_owned(keys if owned else ())
                mp = max(400, int(panel.plot.width()) * 2)
                for key in keys:
                    self._query_window_curve(panel, key, t0, t1, mp, owned)
            except Exception:                        # noqa: BLE001 — never break a park
                log.debug("parked window draw failed", exc_info=True)

    def _query_window_curve(self, panel, key, t0, t1, mp, owned) -> None:
        reads = self._reads()
        if reads is None:                            # headless/tests: synchronous
            try:
                x, y = self._resolver().query(key, t0, t1, mp, local_only=True)
                panel.set_window_curve(key, x, y)
            except Exception:                        # noqa: BLE001 — one bad curve ≠ blank chart
                log.debug("window query failed for %s", key, exc_info=True)
            return

        def draw(res, panel=panel, key=key, owned=owned):
            if owned and key not in getattr(panel, "_query_owned", ()):
                return                               # re-parked / gone live since submit
            try:
                panel.set_window_curve(key, res[0], res[1])
            except RuntimeError:                     # panel removed before delivery
                pass
        reads.query(key, t0, t1, mp, key=("chart-win", id(panel), key), on_result=draw)

    # -- zoom re-query (Fix B) ---------------------------------------------------
    def on_chart_zoom(self, panel, t0, t1) -> None:
        """A manual pan/zoom settled on a parked chart → re-query the VISIBLE sub-window at
        pixel resolution so zooming in returns real store detail rather than magnifying the
        full-window envelope (Fix B). The X-link moves every time-chart to this range, so
        re-query them all for it. No-op live (the live tail is the buffer's job)."""
        tc, reads, resolver = self._tc(), self._reads(), self._resolver()
        if tc is None or tc.following or reads is None or resolver is None:
            return
        lo, hi = (t0, t1) if t0 <= t1 else (t1, t0)
        if hi - lo <= 0:
            return
        for p in self._panels():
            wk = getattr(p, "_query_owned", None)
            if not wk:
                continue                             # not a parked chart with owned curves
            mp = max(400, int(p.plot.width()) * 2)
            for key in list(wk):
                self._query_zoom_curve(p, key, lo, hi, mp)

    def _query_zoom_curve(self, panel, key, t0, t1, mp) -> None:
        def draw(res):
            # still query-owned (not gone live / re-parked) → paint the finer sub-window
            if key in getattr(panel, "_query_owned", ()):
                panel.set_window_curve(key, res[0], res[1])
        self._reads().query(key, t0, t1, mp, key=("chart-win", id(panel), key), on_result=draw)
