"""Ambient video segment sync + backfill over REAL gRPC (DESIGN §9.3 phase 3).

The media-plane twin of sync_e2e: an agent uploads the local video segment FILES
the hub lacks (GetVideoState + streamed PushSegment), the hub mirrors them
byte-exact, and a FRESH client pulls the segment covering a scrubbed instant back
down (streamed PullSegment). Also exercises the cameras/coverage read RPCs.

Run (from server/):  PYTHONPATH=..:.:gen python tests/video_sync_e2e.py
"""

from __future__ import annotations

import asyncio
import os
import tempfile

os.environ.setdefault("GRPC_VERBOSITY", "NONE")

import grpc

from ferrodac_contract.v1 import data_plane_pb2 as pb
from ferrodac_contract.v1 import data_plane_pb2_grpc as rpc

from hub.main import build_server
from ferrodac.core.videostore import VideoStore
from ferrodac.core.videosync import VideoSyncEngine
from ferrodac.net.videosync import GrpcVideoTransport

BASE = 1_700_000_000.0
SEG = 120.0


def _seg(store, cam, t0, t1, fill: bytes) -> None:
    path = store.segment_path(cam, t0)
    with open(path, "wb") as fh:
        fh.write(fill)
    assert store.commit(cam, t0, t1, path)


async def main() -> int:
    d = tempfile.mkdtemp()
    hub_video = VideoStore(os.path.join(d, "hub_video"))
    server, _hub = build_server(video_store=hub_video)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    addr = f"127.0.0.1:{port}"

    local = VideoStore(os.path.join(d, "local_video"))
    for i in range(3):                                    # distinct sizes + bytes/segment
        _seg(local, "camA", BASE + i * SEG, BASE + (i + 1) * SEG,
             bytes([65 + i]) * (4096 * (i + 1)))
    channel = grpc.insecure_channel(addr)
    engine = VideoSyncEngine(local, GrpcVideoTransport(channel))

    # 1) cold connect → upload every segment; the hub mirrors local byte-exact
    n = await asyncio.to_thread(engine.sync_once)
    assert n == 3, n
    assert hub_video.have() == local.have(), (hub_video.have(), local.have())
    for i in range(3):
        t0 = BASE + i * SEG
        assert hub_video.read_segment_bytes("camA", t0) == \
            local.read_segment_bytes("camA", t0), i
    assert all(e["synced"] for e in local.segments("camA"))   # marked hub-confirmed
    print(f"✓ cold sync over gRPC: {n} segments; hub mirrors local byte-exact")

    # 2) idempotent + live tail: one new segment → only it re-uploads
    assert await asyncio.to_thread(engine.sync_once) == 0
    _seg(local, "camA", BASE + 3 * SEG, BASE + 4 * SEG, b"Z" * 8192)
    assert await asyncio.to_thread(engine.sync_once) == 1
    print("✓ idempotent + live tail: only the new segment re-uploads")

    # 3) READ RPCs: cameras + merged coverage over the wire
    stub = rpc.VideoStoreStub(channel)
    cams = await asyncio.to_thread(lambda: stub.ListVideoCameras(pb.VideoCamerasRequest()))
    assert list(cams.cams) == ["camA"], cams.cams
    cov = await asyncio.to_thread(
        lambda: stub.GetVideoCoverage(pb.CoverageRequest(source="camA")))
    assert cov.intervals and cov.intervals[0].t0 <= BASE + 1
    print(f"✓ read RPCs: cameras={list(cams.cams)}, coverage spans={len(cov.intervals)}")

    # 4) BACKFILL: a fresh client with NO local video pulls the covering segment
    client = VideoStore(os.path.join(d, "client_video"))
    back = VideoSyncEngine(client, GrpcVideoTransport(channel))
    assert client.segment_entry_at("camA", BASE + 130) is None        # nothing local
    e = await asyncio.to_thread(lambda: back.backfill_at("camA", BASE + 130))
    assert e is not None and e["t0"] == BASE + SEG, e
    assert client.read_segment_bytes("camA", BASE + SEG) == \
        hub_video.read_segment_bytes("camA", BASE + SEG)              # byte-exact pull
    assert await asyncio.to_thread(lambda: back.backfill_at("camA", BASE + 1e6)) is None
    print("✓ on-demand backfill over gRPC: empty client pulls the covering segment byte-exact")

    channel.close()
    await server.stop(grace=0)
    print("\nVIDEO SYNC E2E PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
