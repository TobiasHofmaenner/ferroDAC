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
