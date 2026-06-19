"""Agent-side store-and-forward sync over gRPC (DESIGN §12.1).

A thin gRPC `transport` for `ferrodac.store.SyncEngine`: `state()` calls the
hub's GetSyncState (the reconciliation truth), `push()` calls PushChunk. Uses a
**synchronous** gRPC channel and runs in a **background thread** — so the sync is
a separate consumer of the local store and never blocks acquisition (headless).

The whole feature degrades to a no-op if grpcio isn't importable.
"""

from __future__ import annotations

import logging
import threading
import time

import numpy as np

from . import GRPC_AVAILABLE
from ..store import SyncEngine

log = logging.getLogger("ferrodac.sync")

if GRPC_AVAILABLE:
    import grpc
    from ferrodac_contract.v1 import data_plane_pb2 as pb
    from ferrodac_contract.v1 import data_plane_pb2_grpc as rpc


def _chunk_to_pb(source, epoch, chunk):
    if chunk["dtype"] == "trace":
        y = np.asarray(chunk["y"], dtype="f8")
        return pb.Chunk(source=source, epoch=epoch, dtype="trace",
                        t=[float(x) for x in chunk["t"]],
                        y=y.reshape(-1).tolist(), x=[float(x) for x in chunk["x"]],
                        m=int(y.shape[1]) if y.ndim == 2 else 0)
    return pb.Chunk(source=source, epoch=epoch, dtype="scalar",
                    t=[float(x) for x in chunk["t"]],
                    v=[float(x) for x in chunk["v"]])


class GrpcSyncTransport:
    """`state()` / `push()` over the hub's Store service (sync stub)."""

    def __init__(self, channel, token: str = ""):
        self.stub = rpc.StoreStub(channel)
        self.token = token

    def state(self) -> dict:
        resp = self.stub.GetSyncState(pb.SyncStateRequest(token=self.token))
        return {(e.source, e.epoch): e.n for e in resp.epochs}

    def push(self, source, epoch, chunk) -> None:
        msg = _chunk_to_pb(source, epoch, chunk)
        msg.token = self.token
        self.stub.PushChunk(msg)


class SyncRunner:
    """Runs `SyncEngine.sync_once()` on a background thread every `interval`
    seconds (and once immediately on start) until stopped. Reconnect-safe: a
    failed pass is logged and retried next tick; the hub's reported state always
    drives what's (re-)uploaded, so nothing is lost or duplicated."""

    def __init__(self, local_store, addr: str, interval: float = 5.0, token: str = "",
                 on_status=None):
        self.local_store = local_store
        self.addr = addr
        self.interval = interval
        self.token = token
        self._on_status = on_status      # callback(state, detail); fired off-thread
        self._stop = threading.Event()
        self._thread: "threading.Thread | None" = None

    def _report(self, state: str, detail: str = "") -> None:
        if self._on_status is not None:
            try:
                self._on_status(state, detail)
            except Exception:            # a bad observer must never break sync
                pass

    def start(self) -> bool:
        if not GRPC_AVAILABLE or self._thread is not None:
            return False
        self._thread = threading.Thread(target=self._run, name="ferrodac-sync",
                                        daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def _run(self) -> None:
        channel = grpc.insecure_channel(self.addr)
        engine = SyncEngine(self.local_store, GrpcSyncTransport(channel, self.token))
        log.info("sync started → %s", self.addr)
        self._report("connecting", f"→ {self.addr}")
        while not self._stop.is_set():
            try:
                self._report("syncing")
                n = engine.sync_once()
                if n:
                    log.info("synced %d samples", n)
                    self._report("idle", f"synced {n} samples")
                else:
                    self._report("idle", "up to date")
            except Exception as exc:                  # noqa: BLE001  (reconnect next tick)
                log.warning("sync pass failed (retry in %.0fs): %s", self.interval, exc)
                self._report("error", str(exc).splitlines()[0][:80])
            self._stop.wait(self.interval)
        channel.close()
        log.info("sync stopped")
        self._report("offline", "")
