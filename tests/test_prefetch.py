"""Playback prefetch (DESIGN §12.1): the RAM cache tier, the prefetcher that fills
it from the hub ahead of the head, and the tick_play buffer gate.

The whole point: GUI-thread reads stay LOCAL (never block on the socket) yet hub
history is still shown, because the prefetcher pulls it into the local cache; and
replay HOLDS at the buffer edge rather than silently slowing (speed stays honest).
"""

import types

import numpy as np

from ferrodac.store import Resolver, TimeContext
from ferrodac.store.intervals import intersect, subtract
from ferrodac.store.prefetch import PrefetchCache
from ferrodac.store.prefetcher import PlaybackPrefetcher


# -- interval algebra --------------------------------------------------------
def test_interval_intersect_and_subtract():
    assert intersect([(0, 10)], [(3, 5), (8, 12)]) == [(3, 5), (8, 10)]
    assert subtract([(0, 10)], [(3, 5)]) == [(0, 3), (5, 10)]
    assert subtract([(0, 10)], []) == [(0, 10)]
    assert subtract([(0, 10)], [(0, 10)]) == []
    assert subtract([(0, 10)], [(-5, 2), (8, 20)]) == [(2, 8)]


# -- the cache tier ----------------------------------------------------------
def test_prefetch_cache_tier_protocol():
    c = PrefetchCache()
    t = np.arange(100.0, 110.0, 1.0)
    c.add_scalar("s", t, t * 2, 100.0, 110.0)
    assert c.has("s")
    assert c.coverage("s") == [(100.0, 110.0)]        # the FETCHED range IS the coverage
    rt, rv = c.read_raw("s", 102.0, 105.0)
    assert list(rt) == [102, 103, 104, 105] and list(rv) == [204, 206, 208, 210]
    # a fetched-but-EMPTY span is still "covered", so it is never re-requested
    c.add_scalar("s", [], [], 110.0, 120.0)
    assert c.coverage("s") == [(100.0, 120.0)]
    assert len(c.read_raw("s", 110.0, 120.0)[0]) == 0


def test_prefetch_cache_evicts_far_from_focus():
    c = PrefetchCache(max_bytes=2000, keep_s=200.0)
    for base in (0.0, 1000.0, 2000.0):
        t = np.arange(base, base + 60, 1.0)
        c.add_scalar("s", t, t, base, base + 60)
    c.set_focus(2000.0)
    t = np.arange(2060.0, 2120.0, 1.0)
    c.add_scalar("s", t, t, 2060.0, 2120.0)           # push over the cap → evict far data
    assert len(c.read_raw("s", 0.0, 60.0)[0]) == 0    # dropped (far from focus)
    assert len(c.read_raw("s", 2000.0, 2120.0)[0]) > 0  # kept (near focus)


# -- the prefetcher ----------------------------------------------------------
class _FakeHub:
    """Stand-in HubReadTier: scalar data v = 10·t over [0, 1000] for source 's'."""

    def coverage(self, s):
        return [(0.0, 1000.0)]

    def source_dtype(self, s):
        return "scalar"

    def read_raw(self, s, t0, t1):
        t = np.arange(np.ceil(t0), np.floor(t1) + 1, 1.0)
        return t, t * 10.0

    def read_raw_trace(self, s, t0, t1):
        return []


def _fake_tc(head, playing=False, width=10.0, speed=1.0):
    return types.SimpleNamespace(
        nav=0, playing=playing, head=head, speed=speed,
        window=(head - width, head), subscribe=lambda cb: (lambda: None))


def _wired(head, **tc_kw):
    cache = PrefetchCache()
    resolver = Resolver([])
    resolver.set_prefetch(cache)
    hub = _FakeHub()
    resolver.set_remote(hub)                          # local_only drops hub, keeps cache
    tc = _fake_tc(head, **tc_kw)
    pf = PlaybackPrefetcher(resolver=resolver, hub=hub, cache=cache, tc=tc,
                            sources_fn=lambda: ["s"], now_fn=lambda: 1000.0)
    return pf, cache, resolver


def test_prefetcher_fills_the_parked_window_from_the_hub():
    pf, cache, resolver = _wired(head=500.0)          # parked, window [490,500]
    pf._pass()                                        # one synchronous pass (no thread)
    assert cache.has("s")
    lo, hi = cache.coverage("s")[0]
    assert lo <= 490.0 and hi >= 500.0
    # a LOCAL-ONLY read (the GUI-thread path) now returns the hub data via the cache
    t, v = resolver.read_raw("s", 492.0, 498.0, local_only=True)
    assert len(t) > 0 and np.allclose(v, t * 10.0)


def test_prefetcher_watermark_reaches_lookahead_when_filled():
    pf, cache, _ = _wired(head=500.0, playing=True, speed=1.0)
    pf._pass()
    wm = pf.buffered_until()
    assert wm is not None and wm >= 503.0             # head + realtime look-ahead, filled


def test_prefetcher_pin_writes_hub_data_into_the_durable_store(tmp_path):
    from ferrodac.store import ZarrStore
    store = ZarrStore(str(tmp_path / "pinned.zarr"))
    pf = PlaybackPrefetcher(resolver=Resolver([]), hub=_FakeHub(),
                            cache=PrefetchCache(), tc=_fake_tc(500.0),
                            sources_fn=lambda: ["s"], store=store)
    assert pf._pin_source("s", 100.0, 110.0, "pin-1")
    t, v = store.read_raw("s", 100.0, 110.0)
    assert len(t) > 0 and np.allclose(v, t * 10.0)    # durable now (survives restart)


# -- the tick gate -----------------------------------------------------------
def test_tick_play_holds_at_the_buffer_watermark_then_resumes():
    tc = TimeContext(now_fn=lambda: 1_000_000.0)
    tc.head, tc.width = 100.0, 10.0
    tc.playing, tc.following, tc.speed = True, False, 10.0
    tc.set_buffer_gate(lambda: 105.0)                 # buffered only to 105
    tc.tick_play(1.0)                                 # wants +10 → 110, gated
    assert tc.head == 105.0 and tc.buffering          # HELD at the edge, honest speed
    tc.tick_play(1.0)                                 # still gated at 105 → no further
    assert tc.head == 105.0 and tc.buffering
    tc.set_buffer_gate(lambda: 500.0)                 # buffer caught up
    tc.tick_play(1.0)                                 # +10 → 115, ungated
    assert tc.head == 115.0 and not tc.buffering


def test_no_gate_means_free_play():
    tc = TimeContext(now_fn=lambda: 1_000_000.0)
    tc.head, tc.playing, tc.following, tc.speed = 100.0, True, False, 5.0
    tc.tick_play(1.0)                                 # no gate installed → advances fully
    assert tc.head == 105.0 and not tc.buffering
