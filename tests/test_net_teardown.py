"""Worker-loop teardown (ferrodac.net._drain).

The hub sync controllers each run a grpc.aio loop in a thread; on (re)connect the
loop's default ThreadPoolExecutor spins up NON-daemon worker threads. If _drain
doesn't shut that executor down they leak per reconnect and hang the whole
process at exit (the interpreter joins them and they never stop) — the
"won't terminate while connected to the hub" bug. This pins the shutdown.
"""

import asyncio
import threading

from ferrodac.net import _drain


def test_drain_shuts_down_default_executor():
    loop = asyncio.new_event_loop()
    grabbed = {}
    # use the loop's default executor → it spins up a (non-daemon) worker thread
    loop.run_until_complete(
        loop.run_in_executor(None, lambda: grabbed.setdefault(
            "t", threading.current_thread())))
    worker = grabbed["t"]
    assert worker.is_alive() and not worker.daemon     # the leak-prone kind

    _drain(loop)                                        # …must wake + join it
    loop.close()
    worker.join(timeout=5)
    assert not worker.is_alive(), "default-executor worker leaked (blocks exit)"


def test_agent_reports_failure_for_a_dead_hub():
    """The agent's link state reflects the REAL channel: connecting to a dead port
    reports connected=False (it must NOT optimistically say connected). Regression for
    the 'button always green' bug — the optimism was in the agent itself."""
    import time

    import pytest
    from ferrodac.net import GRPC_AVAILABLE
    if not GRPC_AVAILABLE:
        pytest.skip("grpcio unavailable")
    from ferrodac.net.agent import HubAgent

    states = []
    ag = HubAgent("127.0.0.1:1", agent_id="t",          # port 1 → nothing listening
                  on_state=lambda connected, _d: states.append(connected))
    ag.start()
    try:
        t = time.time()
        while time.time() - t < 8 and False not in states:
            time.sleep(0.05)
        assert False in states, f"never reported failure: {states}"
        assert True not in states, f"falsely reported connected: {states}"
    finally:
        ag.stop()
