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

CONTRACT_VERSION = 1

try:
    import grpc  # noqa: F401
    GRPC_AVAILABLE = True
except Exception:
    GRPC_AVAILABLE = False


def _drain(loop) -> None:
    """Cancel + await any tasks still pending (grpc.aio's internal handlers)
    before closing a worker loop, so teardown doesn't log 'Task was destroyed'."""
    import asyncio
    try:
        pending = asyncio.all_tasks(loop)
    except RuntimeError:
        pending = set()
    for t in pending:
        t.cancel()
    if pending:
        loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
