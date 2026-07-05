"""Networking: publish local devices to a hub (agent) and consume remote ones
(viewer).

Qt-free, like ``analysis/`` — it depends only on grpc + the generated contract +
the Qt-free core dataclasses, so it is headless/Docker-testable. grpcio is an
**optional** dependency: importing this package never requires it (the app runs
fine without networking); the agent/viewer submodules import grpc and are loaded
lazily by the Qt side, guarded by ``GRPC_AVAILABLE``.
"""

from __future__ import annotations

import os
import sys

# Make the generated contract stubs importable from the monorepo without an
# install (the dev host keeps Python locked down; the stubs live in server/gen).
_GEN = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "server", "gen"))
if os.path.isdir(_GEN) and _GEN not in sys.path:
    sys.path.insert(0, _GEN)

CONTRACT_VERSION = 1   # wire contract version; MIRRORED in server/hub/core.py + the .proto — keep equal

# gRPC's default 4 MiB message cap is too small for the data plane: a backlogged
# store-sync chunk or a full-res ReadRawTrace response can exceed it. Lift it on
# every client channel (the hub matches this server-side). The sync also splits its
# pushes to stay well under even the DEFAULT cap (store/sync.py); this is headroom +
# the read tier (large trace reads).
MAX_MESSAGE_BYTES = 64 * 1024 * 1024
GRPC_CHANNEL_OPTIONS = [
    ("grpc.max_send_message_length", MAX_MESSAGE_BYTES),
    ("grpc.max_receive_message_length", MAX_MESSAGE_BYTES),
    # HTTP/2 keepalive: without it, a black-holed link (hub host rebooted, VPN
    # dropped — no RST ever reaches us) leaves a streaming RPC blind for the
    # ~15 min TCP retransmission timeout while the UI still shows connected.
    # Ping every 30 s even without traffic; declare the link dead after 10 s of
    # ping silence. The hub permits this rate server-side (hub/main.py) — an
    # OLDER hub will GOAWAY these pings, so deploy the hub first.
    ("grpc.keepalive_time_ms", 30_000),
    ("grpc.keepalive_timeout_ms", 10_000),
    ("grpc.keepalive_permit_without_calls", 1),
    ("grpc.http2.max_pings_without_data", 0),
]

try:
    import grpc  # noqa: F401
    GRPC_AVAILABLE = True
except Exception:
    GRPC_AVAILABLE = False


def call_soon_safe(loop, fn, *args) -> None:
    """``loop.call_soon_threadsafe`` that tolerates a dead worker: when a sync
    controller's thread has exited, its (closed) loop lingers on the object, and
    a late caller — the GUI thread's disconnect(), the engine thread's feed() —
    got ``RuntimeError: Event loop is closed``, which aborted the reconnect
    half-way and wedged the whole hub link until an app restart."""
    if loop is None:
        return
    try:
        loop.call_soon_threadsafe(fn, *args)
    except RuntimeError:                    # loop already closed — worker is gone
        pass


def _drain(loop) -> None:
    """Cancel + await pending tasks (grpc.aio's internal handlers) AND shut down
    the loop's default thread-pool executor before closing it.

    Without the executor shutdown its worker threads ('asyncio_N') LEAK on every
    (re)connect and, being non-daemon, hang the whole process at exit — the
    interpreter joins them at shutdown and they never stop. That's the
    'won't terminate while connected to the hub' bug. shutdown_default_executor
    wakes + joins those workers, so a closed loop leaves nothing behind."""
    import asyncio
    try:
        pending = asyncio.all_tasks(loop)
    except RuntimeError:
        pending = set()
    for t in pending:
        t.cancel()
    if pending:
        loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
    try:
        loop.run_until_complete(loop.shutdown_default_executor())
    except Exception:                       # pragma: no cover — old loop / no executor
        pass


async def watch_connectivity(ch, addr, notify) -> None:
    """Report the REAL gRPC link from a channel's connectivity, so the UI reflects
    the actual connection (not the optimistic 'we opened a channel' assumption):
    READY → connected; TRANSIENT_FAILURE/SHUTDOWN → can't reach the hub. Runs as a
    task the caller cancels when the session ends. `notify(connected, detail)`."""
    import grpc
    ready = grpc.ChannelConnectivity.READY
    down = (grpc.ChannelConnectivity.TRANSIENT_FAILURE,
            grpc.ChannelConnectivity.SHUTDOWN)
    last = None
    ch.get_state(try_to_connect=True)            # nudge the lazy channel to connect
    while True:
        st = ch.get_state()
        if st != last:
            last = st
            if st == ready:
                notify(True, f"connected to {addr}")
            elif st in down:
                notify(False, f"cannot reach {addr}")
        await ch.wait_for_state_change(st)       # raises CancelledError on teardown
