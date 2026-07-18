"""PeriodicWorker (DESIGN §21.4): the ONE shared periodic-service-loop skeleton
that net/sync.py, net/videosync.py and store/prefetcher.py run on.

What matters: the interval fires, wake() is INSTANT (condition-variable, not
sleep-polling — and a wake during a pass is never lost), stop() signals + joins
bounded, an exception in a pass never kills the loop, and the thread carries
exactly the fd-*/ferrodac-* name the watchdog attributes it by.
"""

import threading
import time

from ferrodac.core.periodic import PeriodicWorker


def _wait_for(pred, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.005)
    return pred()


def test_interval_fires_repeatedly_on_the_named_thread():
    names = []
    w = PeriodicWorker(lambda: names.append(threading.current_thread().name),
                       interval=0.02, name="fd-test-periodic")
    assert w.start() is True
    assert w.start() is False                     # already running → no second thread
    try:
        assert _wait_for(lambda: len(names) >= 3)  # keeps ticking, not a one-shot
    finally:
        w.stop(timeout=2.0)
    assert set(names) == {"fd-test-periodic"}     # exactly the given fd-* name


def test_run_immediately_runs_a_first_pass_at_start():
    ran = threading.Event()
    w = PeriodicWorker(ran.set, interval=3600.0, name="fd-test-immediate",
                       run_immediately=True)
    w.start()
    try:
        assert ran.wait(2.0)                      # interval is an hour — only
    finally:                                      # run_immediately explains this
        w.stop()


def test_wake_is_immediate_not_interval_polled():
    ran = threading.Event()
    w = PeriodicWorker(ran.set, interval=3600.0, name="fd-test-wake")
    w.start()
    try:
        assert not ran.wait(0.15)                 # nothing until the (huge) interval …
        t0 = time.monotonic()
        w.wake()
        assert ran.wait(2.0)                      # … unless woken — then the pass
        assert time.monotonic() - t0 < 1.0        # runs at once, no sleep-poll lag
    finally:
        w.stop()


def test_a_self_wake_during_a_pass_is_not_lost():
    """The prefetcher wakes ITSELF from inside a pass when the backfill budget is
    exhausted; that wake must survive into the next sleep (immediate re-pass),
    not vanish because nobody was waiting yet."""
    passes = []
    w = PeriodicWorker(lambda: (passes.append(1), len(passes) == 1 and w.wake()),
                       interval=3600.0, name="fd-test-selfwake",
                       run_immediately=True)
    w.start()
    try:
        assert _wait_for(lambda: len(passes) >= 2)   # second pass without the hour wait
    finally:
        w.stop()


def test_an_exception_in_fn_never_kills_the_loop():
    n = [0]

    def fn():
        n[0] += 1
        if n[0] == 1:
            raise RuntimeError("boom (must be swallowed + logged)")

    w = PeriodicWorker(fn, interval=0.02, name="fd-test-exc", run_immediately=True)
    w.start()
    try:
        assert _wait_for(lambda: n[0] >= 3)       # loop survived the pass-1 raise
    finally:
        w.stop()


def test_stop_signals_joins_and_is_idempotent():
    w = PeriodicWorker(lambda: None, interval=0.01, name="fd-test-stop")
    assert w.running is False
    w.start()
    assert w.running is True
    w.stop(timeout=2.0)
    assert w.running is False
    assert all(t.name != "fd-test-stop" for t in threading.enumerate())  # joined
    w.stop()                                      # second stop: harmless no-op
    w.stop()                                      # stop on a never-restarted worker too


def test_on_start_and_on_stop_run_on_the_worker_thread():
    """The sync runners open/close their gRPC channel via these hooks — both must
    run ON the loop thread (session affinity), on_stop after the loop exits."""
    order = []
    w = PeriodicWorker(lambda: order.append("pass"), interval=0.02,
                       name="fd-test-hooks", run_immediately=True,
                       on_start=lambda: order.append(
                           ("start", threading.current_thread().name)),
                       on_stop=lambda: order.append(
                           ("stop", threading.current_thread().name)))
    w.start()
    assert _wait_for(lambda: "pass" in order)
    w.stop(timeout=2.0)                           # join is bounded; hooks already ran
    assert order[0] == ("start", "fd-test-hooks")
    assert order[-1] == ("stop", "fd-test-hooks")
