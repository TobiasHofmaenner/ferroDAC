"""ChartFeed — the single owner of chart curve-buffer writes (DESIGN §22, I-6).

Step 1 of the §22.2 migration: the three query-draw paths (parked window draw,
zoom re-query, route backfill) move here VERBATIM from the app shell, so every
store-query write into a ChartPanel's curves goes through one object instead of
being scattered across app.py. The live bus feed still reaches panels directly
(that seam moves in step 3), and mode is still read from TimeContext booleans
(named in step 2) — this step only establishes the ownership boundary.

Collaborators are injected as zero-arg accessors because they are created in
stages during app startup and are None in degraded modes (no durable store,
headless); accessors also keep ChartFeed constructible in Qt-free tests.
"""

from __future__ import annotations

import logging

from ..core.reading import Reading

_BACKFILL_POINTS = 4000

log = logging.getLogger("ferrodac")


class ChartFeed:
    def __init__(self, panels, resolver, reads, time_context, replay):
        self._panels = panels           # () -> iterable of dashboard panels
        self._resolver = resolver       # () -> Resolver | None
        self._reads = reads             # () -> ReadService | None
        self._tc = time_context         # () -> TimeContext | None
        self._replay = replay           # () -> ReplayController | None

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

    # -- parked window draw ------------------------------------------------------
    def draw_parked_windows(self) -> None:
        """Parked/historic: draw each chart's stored SCALAR curves from a pixel-budgeted store
        query (a min/max envelope) rather than the full-res re-stream fanned into the CurveBuffer
        — one uniform reduction, no old→new fidelity gradient, and zoom re-resolves (Phase 3). The
        re-stream still runs for PROCESSORS; derived / trace / not-yet-recorded curves stay on the
        feed path. No-op while following the live edge (the live tail is the buffer's job).

        The query is SYNCHRONOUS here on purpose: this runs from on_reset, which fires BEFORE the
        re-stream (_load) starts, so the bounded rollup query (~10 ms even over millions of samples)
        never contends with the re-stream for the store lock — the chart updates immediately on a
        select. An async read would be starved behind a huge re-stream and the chart wouldn't
        update until it finished. Zoom stays async (it can fire while a re-stream is in flight).

        Runs for LIVE too: dragging the tail back in grow mode extends the window into history while
        still following the live front. There the historic envelope is drawn UN-OWNED so feed()
        keeps appending the live tail — the historic part is the query, the tail is live, and the
        feed guard keeps them monotonic. Only a genuinely PARKED window is owned (feed skips it)."""
        tc, resolver, replay = self._tc(), self._resolver(), self._replay()
        if tc is None or resolver is None or replay is None:
            return
        following = tc.following
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
                if not following:
                    panel.enter_window(keys)         # parked → feed skips the re-stream entirely
                mp = max(400, int(panel.plot.width()) * 2)
                for key in keys:
                    try:
                        x, y = resolver.query(key, t0, t1, mp)
                        panel.set_window_curve(key, x, y, own=not following)
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
            wk = getattr(p, "_windowed", None)
            if not wk:
                continue                             # not a parked chart with windowed curves
            mp = max(400, int(p.plot.width()) * 2)
            for key in list(wk):
                self._query_zoom_curve(p, key, lo, hi, mp)

    def _query_zoom_curve(self, panel, key, t0, t1, mp) -> None:
        def draw(res):
            # still windowed (not gone live / re-parked) → paint the finer sub-window
            if key in getattr(panel, "_windowed", ()):
                panel.set_window_curve(key, res[0], res[1])
        self._reads().query(key, t0, t1, mp, key=("chart-win", id(panel), key), on_result=draw)
