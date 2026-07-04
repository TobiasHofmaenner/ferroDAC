"""The execution-model contract (DESIGN §21) — Bus lanes, locks, writer QoS.

Regressions for the audit's root finding: the data plane had no threading
contract, so zarr writes/rollups ran on the GUI thread and one slow sink could
stall everything. These pin the guarantees the app (and later the plugin SDK)
now relies on. Qt-free throughout — the Bus must stay headless-drivable.
"""

import subprocess
import sys
import threading
import time

from ferrodac.core.bus import Bus
from ferrodac.core.reading import Reading


def _r(i, dev="d"):
    return Reading(dev, "ch", float(i), float(i))


def _wait(cond, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.005)
    return False


def test_inline_default_is_todays_drain():
    """Default subscribe == the old behavior: sink runs inside drain() on the
    caller's thread, latest() updates, the returned handle unsubscribes, and a
    raising sink never breaks the others."""
    bus = Bus()
    got, thread_ids = [], []

    def sink(batch):
        got.extend(batch)
        thread_ids.append(threading.get_ident())

    def bad(_batch):
        raise RuntimeError("boom")

    unsub_bad = bus.subscribe(bad)
    unsub = bus.subscribe(sink)
    for i in range(5):
        bus.publish(_r(i))
    batch = bus.drain()
    assert len(batch) == 5 and len(got) == 5
    assert thread_ids == [threading.get_ident()]
    assert bus.latest()["d/ch"].t == 4.0
    unsub()                                   # callable handle — back-compat
    unsub_bad()
    bus.publish(_r(9))
    bus.drain()
    assert len(got) == 5                      # unsubscribed → no more deliveries


def test_worker_lane_runs_off_thread_in_order():
    bus = Bus()
    got, threads = [], set()

    def sink(batch):
        got.extend(r.t for r in batch)
        threads.add(threading.get_ident())

    sub = bus.subscribe(sink, thread="worker", name="t")
    for i in range(1000):
        bus.publish(_r(i))
        if i % 100 == 0:
            bus.drain(max_batch=64)
    while bus.drain(max_batch=64):
        pass
    assert _wait(lambda: len(got) == 1000)
    assert got == [float(i) for i in range(1000)]     # lossless, in order
    assert threads and threading.get_ident() not in threads
    assert len(threads) == 1                          # one stable pump thread
    sub.close()


def test_drain_cap_delays_but_never_drops():
    bus = Bus()
    got = []
    bus.subscribe(lambda b: got.extend(r.t for r in b))
    for i in range(12_000):
        bus.publish(_r(i))
    n = len(bus.drain(5000))
    assert n == 5000                          # capped: the stall-amplifier fix
    while bus.drain(5000):
        pass
    assert got == [float(i) for i in range(12_000)]


def test_blocked_sink_cannot_stall_writer_or_drain():
    """Per-sink pumps: a wedged (plugin) sink blocks only itself — the lossless
    writer keeps flowing and drain() never waits on workers."""
    bus = Bus()
    gate = threading.Event()
    writer_got = []
    bus.subscribe(lambda b: gate.wait(30), thread="worker", name="wedged")
    sub = bus.subscribe(lambda b: writer_got.extend(b), thread="worker",
                        mode="lossless", name="writer")
    t0 = time.monotonic()
    for i in range(100):
        bus.publish(_r(i))
        bus.drain()
    assert time.monotonic() - t0 < 5.0        # drain never waited on the wedge
    assert _wait(lambda: len(writer_got) == 100)
    gate.set()
    sub.close()


def test_conflate_merges_backlog_and_bounds_memory():
    bus = Bus()
    gate = threading.Event()
    batches = []

    def sink(batch):
        if not gate.wait(30):
            return
        batches.append(list(batch))

    sub = bus.subscribe(sink, thread="worker", mode="conflate", name="c",
                        conflate_max=500)
    # the sink blocks on the gate, so the backlog piles up and conflates
    for i in range(2000):
        bus.publish(_r(i))
        if i % 50 == 0:
            bus.drain()
    bus.drain()
    time.sleep(0.1)                            # let put() conflate the backlog
    gate.set()
    # converged = every published reading was either delivered or conflated away
    # (depth()==0 alone races: a popped batch can still be inside the sink)
    assert _wait(lambda: sub.pump.delivered + sub.pump.dropped == 2000)
    delivered = [r.t for b in batches for r in b]
    assert delivered == sorted(delivered)      # order preserved through merges
    assert delivered and delivered[-1] == 1999.0   # freshest data survives
    assert sub.pump.dropped > 0                # oldest conflated away beyond the
    assert len(delivered) < 2000               # cap — memory stayed bounded
    sub.close()


def test_lossless_close_flushes_backlog():
    bus = Bus()
    got = []

    def slow(batch):
        time.sleep(0.002)
        got.extend(batch)

    sub = bus.subscribe(slow, thread="worker", mode="lossless", name="w")
    for i in range(300):
        bus.publish(_r(i))
    bus.drain()
    assert sub.close(timeout=15.0)             # delivers EVERYTHING, then joins
    assert len(got) == 300


def test_bus_imports_qt_free():
    """The Bus must stay headless (server + selftests import it without Qt)."""
    import os
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = {**os.environ,
           "PYTHONPATH": repo + os.pathsep + os.environ.get("PYTHONPATH", "")}
    code = ("import sys; import ferrodac.core.bus; "
            "sys.exit(1 if any('qtpy' in m or 'PySide' in m "
            "for m in sys.modules) else 0)")
    assert subprocess.run([sys.executable, "-c", code],
                          cwd=repo, env=env).returncode == 0


def test_zarrstore_concurrent_append_and_read(tmp_path):
    """The RLock model: a writer thread appending while a reader hammers
    query/coverage/read_raw — no exception, and the reader converges on every
    appended sample (the happens-before pin)."""
    from ferrodac.store import ZarrStore
    st = ZarrStore(str(tmp_path / "s.zarr"))
    st.add_source("a/b", name="b")
    stop = threading.Event()
    errors = []

    def write():
        i = 0
        while not stop.is_set():
            st.append("a/b", [float(i), float(i) + 0.5],
                      [1.0, 2.0], epoch="e1")
            if i % 200 == 0:
                st.finalize_rollups("a/b", "e1")
            i += 2

    def read():
        while not stop.is_set():
            try:
                st.coverage("a/b")
                st.query("a/b", 0, 1e9, 200)
                t, _ = st.read_raw("a/b", 0, 1e9)
                assert list(t) == sorted(t)
            except Exception as e:              # noqa: BLE001
                errors.append(e)
                return

    w = threading.Thread(target=write)
    r = threading.Thread(target=read)
    w.start()
    r.start()
    time.sleep(1.5)
    stop.set()
    w.join(10)
    r.join(10)
    assert not errors, errors
    t, v = st.read_raw("a/b", 0, 1e9)
    assert len(t) == len(v) and len(t) > 0


def test_history_buffer_concurrent_feed_and_slice():
    from ferrodac.core.history import HistoryBuffer
    hb = HistoryBuffer(window_s=1e9)
    stop = threading.Event()
    errors = []

    def feeder():
        i = 0
        while not stop.is_set():
            hb.feed([_r(i), _r(i + 1)])
            i += 2

    def slicer():
        while not stop.is_set():
            try:
                hb.slice("d/ch", 0, 1e12)
                hb.span("d/ch")
                hb.keys()
            except Exception as e:              # noqa: BLE001
                errors.append(e)
                return

    f = threading.Thread(target=feeder)
    s = threading.Thread(target=slicer)
    f.start()
    s.start()
    time.sleep(1.0)
    stop.set()
    f.join(10)
    s.join(10)
    assert not errors, errors


def test_storewriter_offthread_is_lossless(tmp_path):
    """The durable path end-to-end on the worker lane: publish → drain → stop()
    → the store holds every sample (crash-safe guarantee, order-independent
    shutdown). Qt-free: the writer subscribes to a bare Bus."""
    from ferrodac.store import StoreWriter, ZarrStore

    class _Eng:                                  # duck-typed engine: subscribe only
        def __init__(self):
            self.bus = Bus()

        def subscribe(self, sink, **kw):
            return self.bus.subscribe(sink, **kw)

    eng = _Eng()
    st = ZarrStore(str(tmp_path / "s.zarr"))
    w = StoreWriter(st, chunk=64, flush_interval=0.05)
    w.attach(eng)
    n = 500
    for i in range(n):
        eng.bus.publish(_r(i, dev="dev"))
    eng.bus.drain()
    w.stop()                                     # joins ITS pump, then flushes
    t, v = st.read_raw("dev/ch", 0, 1e9)
    assert len(t) == n
    assert list(t) == [float(i) for i in range(n)]
