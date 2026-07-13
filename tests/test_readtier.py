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


def _tier():
    t = HubReadTier.__new__(HubReadTier)        # bypass __init__ (needs a real channel)
    t.stub = _Stub()
    t.token = ""
    t.timeout = 1.0
    t._cov_ttl = 3.0
    t._cov_cache = {}
    t._cov_inflight = set()
    t._cov_lock = threading.Lock()
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
