"""HubReadTier coverage must never block the GUI thread (DESIGN §21.2).

Regression for the watchdog freezes seen with a connected hub: the resolver
partitions a window (per play tick, per redraw) by asking every tier's
coverage() ON THE GUI THREAD, and HubReadTier.coverage() used to run a blocking
gRPC GetCoverage there. Now it serves cached/stale on the main thread and
refreshes in the background, while a worker thread still blocks for a fresh
value (export / the async read facade / analysis need accuracy)."""

import threading
import time

import pytest

pytest.importorskip("grpc")

from ferrodac.net.readtier import HubReadTier  # noqa: E402


class _Ivl:
    def __init__(self, a, b):
        self.t0, self.t1 = a, b


class _Resp:
    def __init__(self, ivs):
        self.intervals = [_Ivl(a, b) for a, b in ivs]


class _Stub:
    def __init__(self):
        self.calls = []

    def GetCoverage(self, req, timeout=None):   # noqa: N802 — mimics the gRPC stub
        self.calls.append(req.source)
        return _Resp([(1.0, 2.0)])


def _tier(reads=None):
    t = HubReadTier.__new__(HubReadTier)        # bypass __init__ (needs a real channel)
    t.stub = _Stub()
    t.token = ""
    t.timeout = 1.0
    t._cov_ttl = 3.0
    t._cov_cache = {}
    t._cov_inflight = set()
    t._cov_lock = threading.Lock()
    t._reads = reads                            # ReadService | None (thread fallback)
    return t


def test_coverage_is_nonblocking_on_the_gui_thread():
    t = _tier()
    # main (GUI) thread, cold cache → returns [] AT ONCE, refreshes in the background
    assert t.coverage("s1") == []
    for _ in range(400):                         # let the daemon refresh land
        if t._cov_cache.get("s1"):
            break
        time.sleep(0.005)
    assert t._cov_cache["s1"][1] == [(1.0, 2.0)]     # background refresh updated the cache
    assert t.coverage("s1") == [(1.0, 2.0)]          # warm → served from cache, still no block


def test_coverage_blocks_for_a_fresh_value_off_the_gui_thread():
    t = _tier()
    out = {}

    def worker():                                # a worker (export/analysis) → accuracy
        out["cov"] = t.coverage("s2")
    th = threading.Thread(target=worker)
    th.start()
    th.join(timeout=3)
    assert out.get("cov") == [(1.0, 2.0)]        # fetched synchronously off-thread
    assert t.stub.calls == ["s2"]


class _Reads:
    """ReadService stand-in that RECORDS call() submissions without running them —
    lets a test observe the queue deterministically, then run the job by hand."""

    def __init__(self):
        self.calls = []                          # (fn, key)

    def call(self, fn, *, key, on_result=None, on_error=None):
        self.calls.append((fn, key))


def test_gui_thread_refresh_rides_the_read_service_and_dedupes():
    """§21.4: with a ReadService, the GUI-thread coverage miss submits the refresh
    to it (namespaced key) instead of spawning a 'hub-cov-refresh' thread — and the
    per-series inflight gate still coalesces a burst to ONE submission."""
    reads = _Reads()
    t = _tier(reads)
    assert t.coverage("s1") == []                # cold cache → stale-now, refresh queued
    assert [k for _f, k in reads.calls] == ["hub-cov:s1"]
    assert t.stub.calls == []                    # nothing blocked the GUI thread
    assert t.coverage("s1") == []                # refresh inflight → deduped
    assert len(reads.calls) == 1                 # ONE submission for the burst
    fn, _key = reads.calls[0]
    fn()                                         # the pool worker runs the job
    assert t.stub.calls == ["s1"]
    assert t.coverage("s1") == [(1.0, 2.0)]      # cache update IS the delivery
    assert "s1" not in t._cov_inflight           # gate released for the next TTL expiry


def test_refresh_lands_through_a_real_read_service():
    """End-to-end through store.asyncread.ReadService: the background refresh runs
    on the pool and fills the cache (resolver unused — call()-kind jobs are
    self-contained; deliver defaults to inline)."""
    from ferrodac.store.asyncread import ReadService
    reads = ReadService(None)
    t = _tier(reads)
    try:
        assert t.coverage("s3") == []            # main thread, cold → [] at once
        for _ in range(400):                     # let the pool job land
            if t._cov_cache.get("s3"):
                break
            time.sleep(0.005)
        assert t._cov_cache["s3"][1] == [(1.0, 2.0)]
        assert t.coverage("s3") == [(1.0, 2.0)]
    finally:
        reads.shutdown()
