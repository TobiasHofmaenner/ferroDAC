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
                if replay.playback._is_trace(key) or not resolver.knows(key):
                    continue                         # traces ride the re-stream
                try:
                    t, v = resolver.read_raw(key, seg0, seg1)
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
                readings = replay.playback.read_window([source_key], t0, t1)
            else:
                from .timeline import _envelope_midline
                x, y = resolver.query(source_key, t0, t1, max_points=_BACKFILL_POINTS)
                x, y = _envelope_midline(x, y)       # min/max envelope → one clean line
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
        - PLAYING: the feed owns everything again — releasing ownership CLEARS the
                   released envelope buffers so the resuming re-stream rebuilds
                   monotonically (the old clear-on-Play, now a single transition).

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
            if mode is Mode.LIVE and force:
                self._draw_windows(tc, owned=False)  # go-live / grow-extend-back:
                #                                      historic envelope under the live tail

    def _draw_windows(self, tc, owned: bool) -> None:
        """Draw each chart's stored SCALAR curves from a pixel-budgeted store query
        (a min/max envelope) rather than the full-res re-stream fanned into the
        CurveBuffer — one uniform reduction, no old→new fidelity gradient, and zoom
        re-resolves (Fix B). The re-stream still runs for PROCESSORS; derived /
        trace / not-yet-recorded curves stay on the feed path.

        The query is SYNCHRONOUS on purpose: this runs from on_reset, which fires
        BEFORE the re-stream (_load) starts, so the bounded rollup query (~10 ms
        even over millions of samples) never contends with the re-stream for the
        store lock — the chart updates immediately on a select. An async read would
        be starved behind a huge re-stream. Zoom stays async (it can fire while a
        re-stream is in flight)."""
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
                        and resolver.knows(k)]                     # in a tier (not derived); O(1)
                if not keys:
                    continue
                panel.set_query_owned(keys if owned else ())
                mp = max(400, int(panel.plot.width()) * 2)
                for key in keys:
                    try:
                        x, y = resolver.query(key, t0, t1, mp)
                        panel.set_window_curve(key, x, y)
                    except Exception:                # noqa: BLE001 — one bad curve ≠ blank chart
                        log.debug("window query failed for %s", key, exc_info=True)
            except Exception:                        # noqa: BLE001 — never break a park
                log.debug("parked window draw failed", exc_info=True)

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
