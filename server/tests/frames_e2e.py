"""Live-video round-trip (§9): HubAgent → hub → HubViewer, frames on demand.

Drives the real net layer against an in-process hub and asserts the §9 wire
contract end to end, Qt-free (FramePayload is plain bytes):

- an image frame pushed WITHOUT a watcher never reaches the general Subscribe
  stream (no NaN leak, no bandwidth);
- opening WatchFrames signals DEMAND to the owning agent (demanded_frames);
- a raw rgb888 frame round-trips BIT-EXACT to an explicit watcher;
- frames stay OFF the general stream even while demanded;
- closing the watch drops demand back at the agent (0-watcher stop);
- an agent reconnect re-receives active demand (hub re-sends on announce).
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading

os.environ.setdefault("GRPC_VERBOSITY", "NONE")

import grpc.aio

from hub.main import build_server
from ferrodac_contract.v1 import data_plane_pb2 as pb
from ferrodac_contract.v1 import data_plane_pb2_grpc as rpc
from ferrodac.core.device import DeviceDescriptor, Interface, Source
from ferrodac.core.reading import Reading
from ferrodac.net.agent import HubAgent
from ferrodac.net.convert import FramePayload

UUID = "uuid-cam-1"


async def _until(pred, timeout=5.0, step=0.05):
    for _ in range(int(timeout / step)):
        if pred():
            return True
        await asyncio.sleep(step)
    return False


def _descriptor() -> DeviceDescriptor:
    return DeviceDescriptor(
        instance_id="cam:test", uuid=UUID, driver="camera", name="Bench cam",
        interface=Interface(kind="camera"),
        sources=[Source(id="frame", name="Video", dtype="image")], sinks=[])


async def main() -> int:
    server, _hub = build_server()
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    addr = f"127.0.0.1:{port}"

    agent = HubAgent(addr, agent_id="bench")
    agent.start()
    agent.announce(_descriptor())

    raw = bytes(range(256)) * 3                  # 768 B = a 16x16 rgb888 frame
    frame = FramePayload(raw, "rgb888", 16, 16)

    general: list = []
    frames: list = []
    lock = threading.Lock()

    async with grpc.aio.insecure_channel(addr) as ch:
        v = rpc.ViewerStub(ch)

        async def drain_general():
            async for batch in v.Subscribe(pb.SubscribeRequest()):
                with lock:
                    general.extend(batch.readings)

        gen_task = asyncio.ensure_future(drain_general())
        await asyncio.sleep(0.3)                  # let agent + streams settle

        # 1) no watcher → a pushed frame goes NOWHERE (and no empty payload)
        agent.feed([Reading(UUID, "frame", 1.0, frame)])
        agent.feed([Reading(UUID, "scalar", 1.0, 42.0)])   # control: scalars flow
        ok = await _until(lambda: any(r.source_id == "scalar" for r in general))
        assert ok, "control scalar never arrived on Subscribe"
        assert not any(r.source_id == "frame" for r in general), \
            "frame leaked onto the general stream with no watcher"
        assert not agent.demanded_frames, "demand set should start empty"

        # 2) open WatchFrames → demand reaches the agent
        async def drain_frames():
            req = pb.SubscribeRequest(sources=[
                pb.SourceRef(device_uuid=UUID, source_id="frame")])
            async for batch in v.WatchFrames(req):
                with lock:
                    frames.extend(batch.readings)

        f_task = asyncio.ensure_future(drain_frames())
        ok = await _until(lambda: (UUID, "frame") in agent.demanded_frames)
        assert ok, "demand never reached the agent"

        # 3) raw frame round-trips bit-exact to the watcher
        agent.feed([Reading(UUID, "frame", 2.0, frame)])
        ok = await _until(lambda: len(frames) >= 1)
        assert ok, "frame never reached the watcher"
        got = frames[0]
        assert got.WhichOneof("payload") == "frame"
        assert bytes(got.frame.data) == raw, "raw frame not bit-exact"
        assert got.frame.encoding == "rgb888"
        assert (got.frame.width, got.frame.height) == (16, 16)
        # ...and still nothing frame-shaped on the general stream
        assert not any(r.source_id == "frame" for r in general)

        # 4) close the watch → demand drops at the agent
        f_task.cancel()
        try:
            await f_task
        except (asyncio.CancelledError, Exception):   # noqa: BLE001
            pass
        ok = await _until(lambda: (UUID, "frame") not in agent.demanded_frames)
        assert ok, "demand never dropped after the last watcher left"

        # 5) agent reconnect while a NEW watcher is open → demand re-sent
        frames2: list = []

        async def drain_frames2():
            req = pb.SubscribeRequest(sources=[
                pb.SourceRef(device_uuid=UUID, source_id="frame")])
            async for batch in v.WatchFrames(req):
                with lock:
                    frames2.extend(batch.readings)

        f2_task = asyncio.ensure_future(drain_frames2())
        ok = await _until(lambda: (UUID, "frame") in agent.demanded_frames)
        assert ok
        agent.stop()                              # simulate the agent going away
        agent2 = HubAgent(addr, agent_id="bench-2")
        agent2.start()
        agent2.announce(_descriptor())            # re-announce → hub re-sends demand
        ok = await _until(lambda: (UUID, "frame") in agent2.demanded_frames)
        assert ok, "reconnected agent never re-received active demand"
        agent2.feed([Reading(UUID, "frame", 3.0, frame)])
        ok = await _until(lambda: len(frames2) >= 1)
        assert ok, "frame after reconnect never reached the watcher"

        f2_task.cancel()
        gen_task.cancel()
        for t in (f2_task, gen_task):
            try:
                await t
            except (asyncio.CancelledError, Exception):   # noqa: BLE001
                pass
        agent2.stop()

    await server.stop(grace=0.5)
    print("FRAMES E2E PASS")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
