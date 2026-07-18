"""Agent-side ambient-video store-and-forward over gRPC (DESIGN §9.3 phase 3).

The media-plane twin of net/sync.py: a gRPC `transport` for
`core.videosync.VideoSyncEngine`. `have()` calls GetVideoState (the
reconciliation truth), `push_segment()` streams a segment file up via
PushSegment, and `pull_segment()` streams one back down via PullSegment (the
on-demand scrub backfill). A **synchronous** channel on a **background thread** —
a separate consumer of the local video store, never blocking capture.

Degrades to a no-op if grpcio isn't importable.
"""

from __future__ import annotations

import logging

from . import GRPC_AVAILABLE, GRPC_CHANNEL_OPTIONS
from ..core.periodic import PeriodicWorker
from ..core.videostore import VideoStore
from ..core.videosync import VideoSyncEngine

log = logging.getLogger("ferrodac.videosync")

if GRPC_AVAILABLE:
    import grpc
    from ferrodac_contract.v1 import data_plane_pb2 as pb
    from ferrodac_contract.v1 import data_plane_pb2_grpc as rpc

_SLICE = 1 << 20        # 1 MiB per streamed message — well under the 64 MiB cap
_TIMEOUT = 8.0          # read side (pull/coverage) must not hang forever


class GrpcVideoTransport:
    """`have()` / `push_segment()` / `pull_segment()` over the hub's VideoStore."""

    def __init__(self, channel, token: str = "", timeout: float = _TIMEOUT):
        self.stub = rpc.VideoStoreStub(channel)
        self.token = token
        self.timeout = timeout

    def have(self) -> set:
        resp = self.stub.GetVideoState(pb.VideoStateRequest(token=self.token))
        return {(k.cam, int(k.key)) for k in resp.have}

    def push_segment(self, cam, t0, t1, data) -> None:
        def gen():
            first = True
            for i in range(0, len(data), _SLICE):
                sl = data[i:i + _SLICE]
                if first:                                # header rides the first slice
                    yield pb.VideoSegment(cam=cam, t0=float(t0), t1=float(t1),
                                          token=self.token, data=sl)
                    first = False
                else:
                    yield pb.VideoSegment(data=sl)
            if first:                                    # empty (shouldn't occur) — header only
                yield pb.VideoSegment(cam=cam, t0=float(t0), t1=float(t1),
                                      token=self.token)
        self.stub.PushSegment(gen())

    def pull_segment(self, cam, t) -> "tuple | None":
        stream = self.stub.PullSegment(
            pb.VideoPullRequest(cam=cam, t=float(t), token=self.token),
            timeout=self.timeout)
        t0 = t1 = None
        buf = bytearray()
        for msg in stream:                               # zero messages ⇒ hub has no footage
            if t0 is None:
                t0, t1 = msg.t0, msg.t1
            buf += msg.data
        if not buf:
            return None
        return (t0, t1, bytes(buf))

    # -- optional: surface the hub's coverage in the ribbon ----------------------
    def cameras(self) -> list:
        resp = self.stub.ListVideoCameras(
            pb.VideoCamerasRequest(token=self.token), timeout=self.timeout)
        return list(resp.cams)

    def coverage(self, cam) -> list:
        resp = self.stub.GetVideoCoverage(
            pb.CoverageRequest(source=str(cam), token=self.token),
            timeout=self.timeout)
        return [(iv.t0, iv.t1) for iv in resp.intervals]


def backfill_engine(local_store: VideoStore, channel,
                    token: str = "") -> VideoSyncEngine:
    """A VideoSyncEngine wired to the hub over an existing READ channel, for
    on-demand `backfill_at(cam, t)` pulls (the scrub-preview miss path). Cheap
    when the local store already has the instant — no network."""
    return VideoSyncEngine(local_store, GrpcVideoTransport(channel, token))


class VideoSyncRunner:
    """Runs `VideoSyncEngine.sync_once()` on a background thread every `interval`
    seconds (and once on start) until stopped. Reconnect-safe: a failed pass is
    logged and retried; the hub's reported state drives what re-uploads, so
    nothing is lost or duplicated. Mirrors net.sync.SyncRunner.

    The thread ("ferrodac-videosync") is the shared PeriodicWorker skeleton
    (§21.4); the channel is opened/closed ON that thread via on_start/on_stop."""

    def __init__(self, local_store: VideoStore, addr: str, interval: float = 10.0,
                 token: str = "", on_status=None):
        self.local_store = local_store
        self.addr = addr
        self.interval = interval
        self.token = token
        self._on_status = on_status
        self._channel = None
        self._engine: "VideoSyncEngine | None" = None
        self._worker = PeriodicWorker(self._pass, interval, "ferrodac-videosync",
                                      run_immediately=True,
                                      on_start=self._open, on_stop=self._close)

    def _report(self, state: str, detail: str = "") -> None:
        if self._on_status is not None:
            try:
                self._on_status(state, detail)
            except Exception:                            # a bad observer never breaks sync
                pass

    def start(self) -> bool:
        if not GRPC_AVAILABLE or self.local_store is None:
            return False
        return self._worker.start()

    def stop(self) -> None:
        self._worker.stop(timeout=2.0)

    # -- worker thread ---------------------------------------------------------
    def _open(self) -> None:
        self._channel = grpc.insecure_channel(self.addr, options=GRPC_CHANNEL_OPTIONS)
        self._engine = VideoSyncEngine(self.local_store,
                                       GrpcVideoTransport(self._channel, self.token))
        log.info("video sync started → %s", self.addr)
        self._report("connecting", f"→ {self.addr}")

    def _pass(self) -> None:
        try:
            n = self._engine.sync_once()
            if n:
                log.info("synced %d segment(s)", n)
                self._report("idle", f"synced {n} clip segment(s)")
            else:
                self._report("idle", "video up to date")
        except Exception as exc:                         # noqa: BLE001 (reconnect next tick)
            log.warning("video sync pass failed (retry in %.0fs): %s",
                        self.interval, exc)
            self._report("error", str(exc).splitlines()[0][:80])

    def _close(self) -> None:
        if self._channel is not None:
            self._channel.close()
            self._channel = None
        self._engine = None
        log.info("video sync stopped")
        self._report("offline", "")
