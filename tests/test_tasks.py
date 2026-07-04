"""TaskRunner + async park/scrub replay (DESIGN §21.3).

Pins: worker fn runs off the GUI thread; progress/finished/failed are delivered
ON the GUI thread; cancellation is cooperative and emits neither finished nor
failed; exclusive keys supersede/reject; and the ReplayController's async load
delivers the window's readings to a GUI-lane sink via the GUI-marshalled pump
(headless byte-identical to the synchronous path).
"""
import threading
import time

import pytest

pytest.importorskip("qtpy")

pytestmark = pytest.mark.ui         # touches Qt (qapp) — runs in the UI CI job


def _process(qapp, cond, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if cond():
            return True
        time.sleep(0.005)
    qapp.processEvents()
    return cond()


def test_worker_runs_off_gui_delivers_on_gui(qapp):
    from ferrodac.ui.tasks import TaskRunner
    main = threading.get_ident()
    runner = TaskRunner()
    seen = {}

    def work(ctx):
        seen["work_thread"] = threading.get_ident()
        ctx.progress(0.5, "half")
        return 42

    def done(res):
        seen["done_thread"] = threading.get_ident()
        seen["result"] = res

    runner.run(work, title="t", on_done=done)
    assert _process(qapp, lambda: "result" in seen)
    assert seen["work_thread"] != main            # fn ran off the GUI thread
    assert seen["done_thread"] == main            # callback on the GUI thread
    assert seen["result"] == 42
    runner.shutdown()


def test_progress_carries_eta_on_gui(qapp):
    from ferrodac.ui.tasks import TaskRunner
    runner = TaskRunner()
    updates = []
    gate = threading.Event()

    def work(ctx):
        ctx.progress(0.1, "start")
        gate.wait(5)
        ctx.progress(0.5, "mid")
        return None

    task = runner.run(work, title="t")
    task.progress.connect(lambda f, d, eta: updates.append((f, d, eta)))
    assert _process(qapp, lambda: len(updates) >= 1)
    time.sleep(1.1)                               # let elapsed exceed the ETA floor
    gate.set()
    assert _process(qapp, lambda: len(updates) >= 2)
    assert updates[0][0] == 0.1 and updates[0][1] == "start"
    assert any(u[2] is not None for u in updates)  # an ETA was extrapolated
    runner.shutdown()


def test_cancel_is_cooperative_no_finished(qapp):
    from ferrodac.ui.tasks import TaskRunner
    runner = TaskRunner()
    flags = {"finished": False, "ended": False, "ran": 0}

    def work(ctx):
        for _ in range(1000):
            ctx.check()                           # raises TaskCancelled when cancelled
            flags["ran"] += 1
            time.sleep(0.005)
        return "done"

    task = runner.run(work, title="t", cancellable=True,
                      on_done=lambda _r: flags.__setitem__("finished", True))
    runner.task_ended.connect(lambda _t: flags.__setitem__("ended", True))
    assert _process(qapp, lambda: flags["ran"] > 0)
    task.cancel()
    assert _process(qapp, lambda: flags["ended"])
    assert not flags["finished"]                  # cancelled → no finished callback
    runner.shutdown()


def test_exclusive_supersede_and_reject(qapp):
    from ferrodac.ui.tasks import TaskRunner
    runner = TaskRunner()
    gate = threading.Event()
    cancelled_first = threading.Event()

    def slow(ctx):
        while not ctx.cancelled:
            if gate.wait(0.02):
                return "a"
        cancelled_first.set()
        raise _cancel()

    def _cancel():
        from ferrodac.ui.tasks import TaskCancelled
        return TaskCancelled()

    first = runner.run(slow, title="first", cancellable=True, exclusive="k")
    assert first is not None
    # reject: a second with the same key while busy returns None
    rejected = runner.run(lambda c: "b", title="second", exclusive="k",
                          on_busy="reject")
    assert rejected is None
    # supersede: cancels the first, starts a third
    third = runner.run(lambda c: "c", title="third", exclusive="k",
                       on_busy="supersede")
    assert third is not None
    assert _process(qapp, lambda: cancelled_first.is_set())
    runner.shutdown()


def test_failure_delivers_error_not_finished(qapp):
    from ferrodac.ui.tasks import TaskRunner
    runner = TaskRunner()
    out = {}

    def boom(ctx):
        raise ValueError("nope")

    runner.run(boom, title="t",
               on_done=lambda r: out.__setitem__("done", r),
               on_error=lambda m: out.__setitem__("err", m))
    assert _process(qapp, lambda: "err" in out)
    assert "nope" in out["err"] and "done" not in out
    runner.shutdown()


def test_replay_async_load_matches_sync(qapp, tmp_path):
    """The async park path delivers exactly the window's readings to a GUI-lane
    sink — same set the synchronous path produces — via the GUI-marshalled pump."""
    from ferrodac.core.engine import Engine
    from ferrodac.store import ReplayController, TimeContext, ZarrStore
    from ferrodac.ui.tasks import GuiBridge, TaskRunner

    st = ZarrStore(str(tmp_path / "s.zarr"))
    st.add_source("dev/ch", name="ch")
    import numpy as np
    t = 1000.0 + np.arange(500) * 0.1
    st.append("dev/ch", t, np.arange(500, dtype="f8"), epoch="e1")

    engine = Engine()
    runner = TaskRunner()
    bridge = GuiBridge()
    got = []
    rc = ReplayController(
        engine, st, TimeContext(),
        sources=lambda: ["dev/ch"], reader=st,
        runner=runner, gui_pump=bridge.post_and_wait)
    rc.bus.subscribe(lambda batch: got.extend(r.value for r in batch))

    rc.tc.park_window(1000.0, 1050.0)             # park → async re-stream of [t0,t1]
    assert _process(qapp, lambda: len(got) >= 500, timeout=10.0)
    assert sorted(got) == list(range(500))        # every sample, once
    rc.stop()
    runner.shutdown()
    engine.shutdown()


def test_playback_advances_incrementally(qapp, tmp_path):
    """Pressing play must NOT re-stream (and re-clear) the whole window every frame
    — each play-step streams only the newly-entered slice. Regression: play used to
    flash a 'Loading history' task 20×/s and reload all the data."""
    import numpy as np

    from ferrodac.core.engine import Engine
    from ferrodac.store import ReplayController, TimeContext, ZarrStore

    st = ZarrStore(str(tmp_path / "s.zarr"))
    st.add_source("dev/ch", name="ch")
    t = 1000.0 + np.arange(2000) * 0.1            # 200 s of 10 Hz data
    st.append("dev/ch", t, np.arange(2000, dtype="f8"), epoch="e1")

    engine = Engine()
    resets = {"n": 0}
    got = []
    tc = TimeContext(width=10.0, now_fn=lambda: 1200.0)   # "now" past the data
    rc = ReplayController(
        engine, st, tc, sources=lambda: ["dev/ch"], reader=st,
        on_reset=lambda: resets.__setitem__("n", resets["n"] + 1))
    rc.bus.subscribe(lambda batch: got.extend(r.value for r in batch))

    rc.tc.park_window(1000.0, 1010.0)             # park a 10 s window (1 full render)
    assert resets["n"] == 1
    got.clear()
    rc.tc.playing = True                           # start playing
    for _ in range(20):                            # 20 play frames
        rc.tc.tick_play(0.5)                        # head walks +0.5 s each frame
    # incremental: NO extra clears, and only the newly-entered ~10 s of data streamed
    assert resets["n"] == 1, "playback re-cleared the panels (not incremental)"
    assert 0 < len(got) < 400, f"streamed too much — not incremental ({len(got)})"
    assert got == sorted(got)                      # forward, in time order
    rc.stop()
    engine.shutdown()
