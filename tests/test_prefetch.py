"""Playback prefetch (DESIGN §12.1): the RAM cache tier, the prefetcher that fills
it from the hub ahead of the head, and the tick_play buffer gate.

The whole point: GUI-thread reads stay LOCAL (never block on the socket) yet hub
history is still shown, because the prefetcher pulls it into the local cache; and
replay HOLDS at the buffer edge rather than silently slowing (speed stays honest).
"""

import types

import numpy as np

from ferrodac.core.history import HistoryBuffer
from ferrodac.store import RamTier, Resolver, TimeContext
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


# -- review regressions: never silently skip hub data (§12.1) ----------------
def test_empty_hub_read_is_not_marked_covered_and_is_retried():
    """A hub read that returns empty (a timeout returns [] WITHOUT raising) must NOT
    mark the range fetched — else it masks real hub data on every path and the
    watermark skips it. It must be retried."""
    class _Flaky(_FakeHub):
        fail = True

        def read_raw(self, s, t0, t1):
            if self.fail:
                return np.array([]), np.array([])       # simulate a socket blip
            return super().read_raw(s, t0, t1)

    cache = PrefetchCache()
    resolver = Resolver([])
    resolver.set_prefetch(cache)
    hub = _Flaky()
    resolver.set_remote(hub)
    pf = PlaybackPrefetcher(resolver=resolver, hub=hub, cache=cache,
                            tc=_fake_tc(500.0), sources_fn=lambda: ["s"],
                            now_fn=lambda: 1000.0)
    pf._pass()
    assert not cache.has("s")                            # the blip is NOT cached as empty
    hub.fail = False
    pf._pass()
    assert cache.has("s")                                # recovers on retry
    assert len(resolver.read_raw("s", 492.0, 498.0, local_only=True)[0]) > 0


def test_watermark_holds_after_a_scrub_until_recomputed():
    """A nav bump (scrub) makes the old watermark meaningless; the gate must HOLD at
    the head, not gate against the pre-scrub value (which let play skip hub data)."""
    pf, _cache, _res = _wired(head=500.0, playing=True, speed=1.0)
    pf._pass()
    assert pf.buffered_until() is not None               # fresh for nav=0
    pf.tc.nav = 1                                         # a scrub happened
    assert pf.buffered_until() == pf.tc.head              # stale → HOLD at head
    pf._pass()                                            # recompute for nav=1
    assert pf.buffered_until() is not None                # fresh again


def test_watermark_holds_at_local_edge_when_hub_coverage_is_empty():
    """Hub coverage transiently returning [] (a refresh failure) must NOT free-run
    (it looks identical to genuinely-no-data) — hold at the local data edge."""
    class _NoCov(_FakeHub):
        def coverage(self, s):
            return []

    cache = PrefetchCache()
    resolver = Resolver([RamTier(HistoryBuffer())])
    resolver.set_prefetch(cache)
    hub = _NoCov()
    resolver.set_remote(hub)
    t = np.arange(400.0, 481.0)
    cache.add_scalar("s", t, t, 400.0, 480.0)            # local data runs to 480
    pf = PlaybackPrefetcher(resolver=resolver, hub=hub, cache=cache,
                            tc=_fake_tc(450.0, playing=True), sources_fn=lambda: ["s"],
                            now_fn=lambda: 1000.0)
    pf._pass()
    assert abs(pf.buffered_until() - 480.0) < 1.0        # held at the local edge, not now


def test_pin_only_fetches_what_the_store_lacks_no_duplication(tmp_path):
    """Pin must subtract the durable coverage (idempotent, no boundary dup) — else
    an overlap duplicates every sample in the permanent record."""
    from ferrodac.store import ZarrStore
    store = ZarrStore(str(tmp_path / "pin.zarr"))
    store.add_source("s")
    lt = np.arange(100.0, 151.0)
    store.append("s", lt, lt * 10.0, epoch="local")      # [100,150] already durable
    pf = PlaybackPrefetcher(resolver=Resolver([]), hub=_FakeHub(),
                            cache=PrefetchCache(), tc=_fake_tc(500.0),
                            sources_fn=lambda: ["s"], store=store)
    assert pf._pin_source("s", 100.0, 200.0, "pin-1")    # pins only the [150,200] gap
    t, _v = store.read_raw("s", 100.0, 200.0)
    assert len(t) == len(np.unique(t))                   # NO duplicate rows
    n = len(t)
    assert pf._pin_source("s", 100.0, 200.0, "pin-2") is False   # re-pin: idempotent no-op
    assert len(store.read_raw("s", 100.0, 200.0)[0]) == n


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
