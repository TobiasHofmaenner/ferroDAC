"""ReadService — the async resolver facade (DESIGN §21.3). Qt-free: tests use an
inline `deliver`, so they pin the coalescing/caching logic without a GUI.

Pins: results delivered via `deliver`; same-key supersession delivers only the
newer result; the coverage TTL cache avoids re-hitting the tier; cancel prevents
delivery; reads run off the calling thread.
"""
import threading
import time

from ferrodac.store.asyncread import ReadService


class FakeResolver:
    """Counts calls and can sleep, so we can prove caching/coalescing/off-thread."""

    def __init__(self, delay=0.0):
        self.delay = delay
        self.calls = {"query": 0, "coverage": 0}
        self.threads = set()
        self._lock = threading.Lock()

    def query(self, series, t0, t1, max_points=2000):
        with self._lock:
            self.calls["query"] += 1
            self.threads.add(threading.get_ident())
        if self.delay:
            time.sleep(self.delay)
        return ([t0, t1], [1.0, 2.0])

    def coverage(self, series):
        with self._lock:
            self.calls["coverage"] += 1
        if self.delay:
            time.sleep(self.delay)
        return [(0.0, 100.0)]


def _wait(cond, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.005)
    return False


def test_query_runs_off_thread_and_delivers():
    r = FakeResolver()
    svc = ReadService(r)                       # inline deliver
    out = {}
    svc.query("a/b", 0, 10, on_result=lambda res: out.__setitem__("res", res))
    assert _wait(lambda: "res" in out)
    assert out["res"] == ([0, 10], [1.0, 2.0])
    assert threading.get_ident() not in r.threads   # ran on a pool thread
    svc.shutdown()


def test_same_key_supersedes_older():
    r = FakeResolver(delay=0.15)
    delivered = []
    svc = ReadService(r)
    # fire three queries on the same key rapidly; only the last should deliver
    for i in range(3):
        svc.query("a/b", 0, i, key=("k", "a/b"),
                  on_result=lambda res, i=i: delivered.append(i))
        time.sleep(0.02)
    assert _wait(lambda: delivered, timeout=5)
    time.sleep(0.3)                            # let any stragglers try to deliver
    assert delivered == [2], delivered         # only the newest survived
    svc.shutdown()


def test_coverage_ttl_cache_avoids_retier():
    r = FakeResolver()
    svc = ReadService(r)
    seen = []
    svc.coverage_many(["a", "b"], ttl=10.0, on_result=lambda c: seen.append(c))
    assert _wait(lambda: len(seen) == 1)
    assert r.calls["coverage"] == 2            # one per source, first pass
    # a second call within the TTL hits the cache — no new tier calls
    svc.coverage_many(["a", "b"], ttl=10.0, on_result=lambda c: seen.append(c))
    assert _wait(lambda: len(seen) == 2)
    assert r.calls["coverage"] == 2            # unchanged → served from cache
    assert seen[1] == {"a": [(0.0, 100.0)], "b": [(0.0, 100.0)]}
    # invalidate → the next call refetches
    svc.invalidate()
    svc.coverage_many(["a", "b"], ttl=10.0, on_result=lambda c: seen.append(c))
    assert _wait(lambda: len(seen) == 3)
    assert r.calls["coverage"] == 4
    svc.shutdown()


def test_cancel_prevents_delivery():
    r = FakeResolver(delay=0.2)
    svc = ReadService(r)
    got = []
    ticket = svc.query("a/b", 0, 10, on_result=lambda res: got.append(res))
    ticket.cancel()
    time.sleep(0.4)
    assert got == []                           # cancelled → never delivered
    svc.shutdown()


def test_error_is_delivered_not_swallowed():
    """A tier exception reaches on_error via `deliver` (regression: the except
    variable is cleared after the block, so a deferred lambda must bind it)."""
    class Boom:
        def query(self, *a):
            raise ValueError("kaboom")

    svc = ReadService(Boom())
    out = {}
    svc.query("a/b", 0, 10, on_result=lambda r: out.__setitem__("ok", r),
              on_error=lambda e: out.__setitem__("err", str(e)))
    assert _wait(lambda: "err" in out)
    assert out["err"] == "kaboom" and "ok" not in out
    svc.shutdown()


def test_deliver_marshal_is_used():
    """Every result goes through `deliver` (the GUI marshal in the app)."""
    r = FakeResolver()
    marshalled = []
    svc = ReadService(r, deliver=lambda fn: (marshalled.append(1), fn()))
    out = {}
    svc.query("a/b", 0, 10, on_result=lambda res: out.__setitem__("res", res))
    assert _wait(lambda: "res" in out)
    assert marshalled                          # deliver wrapped the callback
    svc.shutdown()
