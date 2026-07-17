"""ChartFeed.reconcile — the named-mode display state machine (DESIGN §22 I-6/I-8).

Drives the real ChartFeed + TimeContext + store/resolver through the full
transition cycle against a fake panel, Qt-free. Every assertion here encodes a
guard that used to be distributed across app.py/panels.py (the `_windowed` set,
clear-on-Play, the `own` flag) and is now a reconcile transition. This is the
seed of the §22.2 step-6 selftest harness.
"""
import os
import tempfile

import numpy as np

from ferrodac.store import Mode, Resolver, TimeContext, ZarrStore
from ferrodac.ui.chartfeed import ChartFeed

BASE = 1_000_000.0


class _FakePlot:
    def width(self):
        return 800


class _FakePanel:
    """Mimics ChartPanel's ownership/draw surface (set_query_owned semantics
    included: keys leaving the set are recorded as cleared)."""

    def __init__(self):
        self._query_owned = set()
        self.plot = _FakePlot()
        self.drawn = {}
        self.drawn_y = {}
        self.cleared = []

    def curve_keys(self):
        return ["dev/a", "derived/x"]          # one stored scalar + one derived

    def set_query_owned(self, keys):
        new = {k for k in keys if k in self.curve_keys()}
        for k in self._query_owned - new:
            self.cleared.append(k)
        self._query_owned = new

    def set_window_curve(self, key, x, y):
        self.drawn[key] = len(x)
        self.drawn_y[key] = np.asarray(y, dtype="f8")


class _FakePlayback:
    def _is_trace(self, k):
        return False


class _FakeReplay:
    playback = _FakePlayback()


def _rig():
    st = ZarrStore(os.path.join(tempfile.mkdtemp(), "s"))
    st.add_source("dev/a", name="a")
    t = BASE + np.arange(3000) * 0.1
    st.append("dev/a", t, np.sin(t), epoch="e0")
    st.finalize_rollups("dev/a")
    tc = TimeContext(width=100.0, now_fn=lambda: BASE + 300)
    panel = _FakePanel()
    cf = ChartFeed(panels=lambda: [panel], resolver=lambda: Resolver([st]),
                   reads=lambda: None, time_context=lambda: tc,
                   replay=lambda: _FakeReplay())
    return tc, panel, cf


def test_parked_owns_and_draws_stored_scalars_only():
    tc, panel, cf = _rig()
    assert tc.mode is Mode.LIVE
    cf.reconcile()                             # startup tick: LIVE, nothing owned
    assert panel._query_owned == set() and panel.drawn == {}
    tc.park(BASE + 150)
    cf.reconcile(force=True)                   # scrub → PARKED
    assert tc.mode is Mode.PARKED
    assert panel._query_owned == {"dev/a"}     # derived/x stays on the feed path
    assert panel.drawn.get("dev/a", 0) > 100   # pixel-budgeted envelope drawn


def test_idle_reconcile_is_a_noop():
    tc, panel, cf = _rig()
    tc.park(BASE + 150)
    cf.reconcile(force=True)
    before = dict(panel.drawn)
    cf.reconcile()
    cf.reconcile()                             # transport ticks, nothing changed
    assert panel.drawn == before               # no re-query per tick


def test_play_redraws_the_window_envelope_unowned():
    """Hitting Play releases query-ownership (so advance()/the live tail append) and
    REDRAWS the window's historic envelope UN-OWNED — the whole selected window stays
    visible with the playhead sweeping across it. Regression: since §22 step 4 the
    re-stream stopped refeeding raw scalars, so the old 'clear on Play and let the
    re-stream rebuild it' left the chart blank down to the swept sliver. The release
    still happens exactly once, not per play tick."""
    tc, panel, cf = _rig()
    tc.park(BASE + 150)
    cf.reconcile(force=True)
    tc.play()
    cf.reconcile()
    assert tc.mode is Mode.PLAYING
    assert panel._query_owned == set()         # un-owned: feed/advance append on top
    assert panel.cleared == ["dev/a"]          # ownership released once (buffer cleared)
    assert panel.drawn.get("dev/a", 0) > 100   # …then the envelope is REDRAWN (visible)
    drawn_before = panel.drawn["dev/a"]
    cf.reconcile()                             # later ticks: no-op, no re-clear/redraw
    assert panel.cleared == ["dev/a"] and panel.drawn["dev/a"] == drawn_before


def test_pause_redraws_the_owned_parked_envelope():
    """Pausing (PLAYING→PARKED) must restore the owned window envelope on the next
    reconcile — the per-frame play-tick reconcile catches this transition. Regression:
    the chart stayed blank after pause because Play had cleared it and no redraw ran."""
    tc, panel, cf = _rig()
    tc.park(BASE + 150)
    cf.reconcile(force=True)
    tc.play()
    cf.reconcile()                             # PLAYING: envelope drawn un-owned
    tc.pause()
    assert tc.mode is Mode.PARKED              # frozen (no nav → no reset path)
    cf.reconcile()                             # the play-tick's per-frame reconcile
    assert panel._query_owned == {"dev/a"}     # re-owned by the parked query
    assert panel.drawn.get("dev/a", 0) > 100   # …and redrawn — not left blank


def test_repark_after_pause_reowns_and_redraws():
    tc, panel, cf = _rig()
    tc.park(BASE + 150)
    cf.reconcile(force=True)
    tc.play()
    cf.reconcile()
    tc.pause()
    assert tc.mode is Mode.PARKED              # frozen; no reset fires (as before)
    tc.park(BASE + 100)
    cf.reconcile(force=True)                   # navigation → reset path
    assert panel._query_owned == {"dev/a"}
    assert panel.drawn.get("dev/a", 0) > 100


def test_golive_draws_historic_envelope_unowned():
    """Go-live / grow-extend-back: the window's history is drawn from the query
    with the key NOT owned, so feed() keeps appending the live tail on top."""
    tc, panel, cf = _rig()
    tc.park(BASE + 150)
    cf.reconcile(force=True)
    tc.follow_now()
    cf.reconcile(force=True)                   # the reset path fires on go-live
    assert tc.mode is Mode.LIVE
    assert panel._query_owned == set()
    assert panel.drawn.get("dev/a", 0) > 0     # history under the live tail


# -- §22 step 3: the two feed streams through one forwarding point ---------------

class _FeedPanel(_FakePanel):
    def __init__(self):
        super().__init__()
        self.fed = []

    def feed(self, batch):
        self.fed.extend(batch)


def _stream_rig():
    from ferrodac.core.bus import Bus
    from ferrodac.core.reading import Reading
    from ferrodac.store import ReplayController
    st = ZarrStore(os.path.join(tempfile.mkdtemp(), "s"))
    st.add_source("dev/a", name="a")
    t = BASE + np.arange(1000) * 0.1
    st.append("dev/a", t, np.sin(t), epoch="e0")
    st.finalize_rollups("dev/a")
    engine_bus = Bus()
    tc = TimeContext(width=100.0, now_fn=lambda: BASE + 300)
    ctl = ReplayController(engine_bus, st, tc, sources=lambda: ["dev/a"])
    panel = _FeedPanel()
    cf = ChartFeed(panels=lambda: [panel], resolver=lambda: Resolver([st]),
                   reads=lambda: None, time_context=lambda: tc,
                   replay=lambda: ctl)
    cf.attach(engine_bus, ctl.bus)
    return engine_bus, tc, ctl, panel, cf, Reading


def test_live_tail_reaches_panels_from_engine_exactly_once():
    """LIVE raw rides the engine bus straight to panels via ChartFeed._forward;
    the playback bus stays idle (the old mirror was a nested drain per tick)."""
    engine_bus, tc, ctl, panel, cf, Reading = _stream_rig()
    engine_bus.publish(Reading("dev", "a", BASE + 300, 42.0))
    engine_bus.drain()
    hits = [r for r in panel.fed if r.value == 42.0]
    assert len(hits) == 1                       # exactly once — no double-feed
    assert ctl.bus.drain() == []                # playback bus never saw raw live


def test_raw_restream_never_reaches_panels_but_drives_processors():
    """§22 step 4 (I-9): replay serves analysis, not pixels. The parked re-stream's
    raw scalars reach playback-bus subscribers (processors) but are filtered out of
    the panel forward — parked curves are query-drawn by reconcile instead."""
    engine_bus, tc, ctl, panel, cf, Reading = _stream_rig()
    processed = []
    ctl.bus.subscribe(lambda b: processed.extend(b))   # the processor-side sink
    tc.park(BASE + 50)                          # scrub → controller re-streams the
    #                                             window synchronously (no runner)
    assert len(processed) > 100                 # analysis re-experienced the slice
    assert panel.fed == []                      # pixels never saw raw historic data


def test_live_is_gated_out_of_parked_panels():
    """While parked/playing, live acquisition continues on the engine bus but must
    not pollute the historic view (the gate the old _on_live provided)."""
    engine_bus, tc, ctl, panel, cf, Reading = _stream_rig()
    tc.park(BASE + 50)
    panel.fed.clear()
    engine_bus.publish(Reading("dev", "a", BASE + 300, 42.0))
    engine_bus.drain()
    assert panel.fed == []                      # parked chart ignored the live tick
    tc.follow_now()
    engine_bus.publish(Reading("dev", "a", BASE + 301, 43.0))
    engine_bus.drain()
    assert [r.value for r in panel.fed if r.value == 43.0] == [43.0]   # live again


def test_derived_readings_forward_from_playback_bus_in_any_mode():
    from ferrodac.core.bus import Bus
    from ferrodac.core.reading import Reading
    engine_bus, playback = Bus(), Bus()
    panel = _FeedPanel()
    cf = ChartFeed(panels=lambda: [panel], resolver=lambda: None,
                   reads=lambda: None, time_context=lambda: None,
                   replay=lambda: None)
    cf.attach(engine_bus, playback, is_derived=lambda k: k == "proc/out")
    playback.publish(Reading("proc", "out", BASE, 1.5))       # derived → forwarded
    playback.publish(Reading("dev", "a", BASE, 9.9))          # raw → filtered out
    playback.drain()
    assert [r.value for r in panel.fed] == [1.5]


def test_advance_appends_playing_slice_through_the_owner():
    """PLAYING: the newly-entered slice reaches chart curves via ChartFeed.advance
    (synthetic readings through feed), not the re-stream."""
    engine_bus, tc, ctl, panel, cf, Reading = _stream_rig()
    cf.advance(BASE + 10, BASE + 12)            # a 2 s play-step slice
    got = [r for r in panel.fed if BASE + 10 <= r.t <= BASE + 12]
    assert 15 <= len(got) <= 25                 # 10 Hz data → ~20 samples
    assert [r.t for r in got] == sorted(r.t for r in got)


def test_degraded_mode_single_bus_no_double_feed():
    """No durable store → no playback bus: attach(engine, None) must deliver
    exactly once (guard against a future double-subscription regression)."""
    from ferrodac.core.bus import Bus
    from ferrodac.core.reading import Reading
    bus = Bus()
    panel = _FeedPanel()
    cf = ChartFeed(panels=lambda: [panel], resolver=lambda: None,
                   reads=lambda: None, time_context=lambda: None,
                   replay=lambda: None)
    cf.attach(bus, None)
    bus.publish(Reading("dev", "a", BASE, 7.0))
    bus.drain()
    assert len([r for r in panel.fed if r.value == 7.0]) == 1


def test_forward_isolates_a_broken_panel():
    """One raising panel must not starve the others — same isolation the Bus
    gives its inline sinks."""
    from ferrodac.core.bus import Bus
    from ferrodac.core.reading import Reading

    class _Broken:
        def feed(self, batch):
            raise RuntimeError("boom")

    bus = Bus()
    good = _FeedPanel()
    cf = ChartFeed(panels=lambda: [_Broken(), good], resolver=lambda: None,
                   reads=lambda: None, time_context=lambda: None,
                   replay=lambda: None)
    cf.attach(bus, None)
    bus.publish(Reading("dev", "a", BASE, 5.0))
    bus.drain()
    assert len(good.fed) == 1                   # the broken sibling didn't block it


# -- §22 step 5: one envelope representation end-to-end (I-10) -------------------

def test_backfill_draws_the_envelope_not_a_halved_midline():
    """Regression (artifact E): the route backfill used to collapse the min/max
    envelope to its midline, HALVING every spike — the same recorded spike showed
    full height parked but half height when backfilled. A chart backfill now draws
    the same envelope polyline every other query path uses, owned iff PARKED."""
    st = ZarrStore(os.path.join(tempfile.mkdtemp(), "s"))
    st.add_source("dev/a", name="a")
    t = BASE + np.arange(100_000) * 0.1
    v = np.ones(100_000)
    v[50_000] = 42.0                              # one lone spike on a flat baseline
    st.append("dev/a", t, v, epoch="e0")
    st.finalize_rollups("dev/a")
    tc = TimeContext(width=t[-1] - BASE + 10, now_fn=lambda: t[-1] + 1)
    panel = _FeedPanel()
    cf = ChartFeed(panels=lambda: [panel], resolver=lambda: Resolver([st]),
                   reads=lambda: None, time_context=lambda: tc,
                   replay=lambda: _FakeReplay())
    tc.park(BASE + 5000)
    cf.backfill_route("dev/a", panel)
    assert panel.drawn_y["dev/a"].max() == 42.0   # midline would have shown 21.5
    assert "dev/a" in panel._query_owned          # parked → the query owns it
    assert panel.fed == []                        # drawn, not fed

    panel2 = _FeedPanel()
    cf2 = ChartFeed(panels=lambda: [panel2], resolver=lambda: Resolver([st]),
                    reads=lambda: None, time_context=lambda: tc,
                    replay=lambda: _FakeReplay())
    tc.follow_now()
    cf2.backfill_route("dev/a", panel2)
    assert panel2.drawn_y["dev/a"].max() == 42.0
    assert "dev/a" not in panel2._query_owned     # live → un-owned, tail appends


# -- §22 step 6: the full transition matrix + zoom supersession ------------------

class _FakeReads:
    """Mimics ReadService.query's signature: async, coalesced by ticket key —
    the test fires results by hand to model in-flight/stale deliveries."""

    def __init__(self):
        self.pending = []                       # (series, t0, t1, mp, on_result)

    def query(self, series, t0, t1, mp, key=None, on_result=None):
        self.pending.append((series, t0, t1, mp, on_result))


def test_zoom_requeries_owned_curves_and_discards_stale_results():
    """Fix B + the stale-zoom class (commits 558a488, 1303e36): a settled zoom on
    a parked chart re-queries the visible sub-window at pixel budget; a result
    landing AFTER go-live released ownership must be discarded, never painted."""
    st = ZarrStore(os.path.join(tempfile.mkdtemp(), "s"))
    st.add_source("dev/a", name="a")
    t = BASE + np.arange(3000) * 0.1
    st.append("dev/a", t, np.sin(t), epoch="e0")
    st.finalize_rollups("dev/a")
    tc = TimeContext(width=100.0, now_fn=lambda: BASE + 300)
    reads = _FakeReads()
    panel = _FeedPanel()
    cf = ChartFeed(panels=lambda: [panel], resolver=lambda: Resolver([st]),
                   reads=lambda: reads, time_context=lambda: tc,
                   replay=lambda: _FakeReplay())
    cf.on_chart_zoom(panel, BASE + 10, BASE + 20)
    assert reads.pending == []                  # LIVE → zoom is a no-op

    tc.park(BASE + 150)
    cf.reconcile(force=True)                    # parked → dev/a owned; the window
    assert len(reads.pending) == 1              # draw is ASYNC now (ReadService)
    series, q0, q1, mp, deliver = reads.pending[0]
    assert series == "dev/a"
    deliver(Resolver([st]).query(series, q0, q1, mp))
    full = panel.drawn["dev/a"]
    cf.on_chart_zoom(panel, BASE + 100, BASE + 110)
    assert len(reads.pending) == 2              # one owned curve → one re-query
    series, q0, q1, mp, deliver = reads.pending[1]
    assert series == "dev/a" and (q0, q1) == (BASE + 100, BASE + 110)
    deliver(Resolver([st]).query(series, q0, q1, mp))
    assert panel.drawn["dev/a"] != full         # finer sub-window painted

    cf.on_chart_zoom(panel, BASE + 120, BASE + 130)      # stale-delivery case:
    _, _, _, _, stale = reads.pending[2]
    tc.follow_now()
    cf.reconcile(force=True)                    # go-live releases ownership...
    before = dict(panel.drawn)
    stale(Resolver([st]).query("dev/a", BASE + 120, BASE + 130, 100))
    assert panel.drawn == before                # ...so the stale result is dropped


def test_full_transition_matrix_invariants():
    """The scripted walk (§22 step 6): every mode transition of the display state
    machine, with the two structural invariants checked after each step —
    (a) ownership == stored scalars iff PARKED, (b) live engine data reaches
    panels iff LIVE. Each leg encodes a 2026-06/07 regression class."""
    from ferrodac.core.reading import Reading
    engine_bus, tc, ctl, panel, cf, _ = _stream_rig()
    ctl.on_reset = lambda: cf.reconcile(force=True)      # what app._replay_reset does
    live_t = [BASE + 300]

    def check(step, mode):
        cf.reconcile()                          # what the transport ticks do
        assert tc.mode is mode, step
        want = {"dev/a"} if mode is Mode.PARKED else set()
        assert panel._query_owned == want, f"{step}: owned={panel._query_owned}"
        n0 = len(panel.fed)
        live_t[0] += 1.0
        engine_bus.publish(Reading("dev", "a", live_t[0], 1.23))
        engine_bus.drain()
        grew = len(panel.fed) > n0
        assert grew == (mode is Mode.LIVE), f"{step}: live leak/starve (grew={grew})"

    check("startup", Mode.LIVE)
    tc.park(BASE + 50);        check("scrub", Mode.PARKED)          # d23e1ff
    tc.play();                 check("play", Mode.PLAYING)          # 2b500ca
    tc.pause();                check("pause", Mode.PARKED)
    tc.park(BASE + 20);        check("re-scrub", Mode.PARKED)       # 7d56ea7 era
    tc.play();                 check("re-play", Mode.PLAYING)
    while tc.playing:                                                # catch up to now
        tc.tick_play(1e6)
    check("caught-up-to-live", Mode.LIVE)                           # 1303e36
    tc.park(BASE + 80);        check("park-again", Mode.PARKED)
    tc.follow_now();           check("go-live", Mode.LIVE)          # 0dd6072
