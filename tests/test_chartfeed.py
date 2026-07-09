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


def test_play_releases_ownership_and_clears_exactly_once():
    """The old clear-on-Play (exit_window per tick, guarded internally) is now a
    single PARKED→PLAYING transition: released envelopes clear so the resuming
    re-stream rebuilds monotonically — and only once, not per play tick."""
    tc, panel, cf = _rig()
    tc.park(BASE + 150)
    cf.reconcile(force=True)
    tc.play()
    cf.reconcile()
    assert tc.mode is Mode.PLAYING
    assert panel._query_owned == set() and panel.cleared == ["dev/a"]
    cf.reconcile()                             # later ticks
    assert panel.cleared == ["dev/a"]          # not cleared again


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
