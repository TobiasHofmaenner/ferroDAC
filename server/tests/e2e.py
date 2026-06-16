"""End-to-end Milestone-1 test: agent → hub → viewer, no mocks.

Spins up a real hub in-process, connects a fake **agent** (announces a device and
streams readings over Ingest.Session) and a fake **viewer** (reads the catalog
and subscribes), and asserts the device appears transparently, its readings flow,
and it disappears when the agent disconnects. Run inside the hub image:

    docker compose run --rm hub python tests/e2e.py
"""

from __future__ import annotations

import asyncio
import os
import sys

os.environ.setdefault("GRPC_VERBOSITY", "NONE")   # quiet the C-core shutdown log

import grpc
from ferrodac_contract.v1 import data_plane_pb2 as pb
from ferrodac_contract.v1 import data_plane_pb2_grpc as rpc

from hub.main import build_server

DEV = "11111111-2222-3333-4444-555555555555"


async def run_agent(addr, announced, got_welcome, stop):
    async with grpc.aio.insecure_channel(addr) as ch:
        stub = rpc.IngestStub(ch)

        async def up():
            yield pb.AgentMessage(hello=pb.Hello(
                agent_id="test-agent", contract_version=1))
            yield pb.AgentMessage(announce=pb.DeviceDescriptor(
                uuid=DEV, name="Sim gauge", driver="sim",
                hardware_id="sim:0", online=True,
                sources=[pb.SourcePort(
                    id="p", name="Pressure", dtype=pb.SCALAR, unit="mbar")]))
            announced.set()
            i = 0
            while not stop.is_set():
                yield pb.AgentMessage(readings=pb.ReadingBatch(readings=[
                    pb.Reading(device_uuid=DEV, source_id="p", t=float(i),
                               status=pb.OK, scalar=1e-6 * (i + 1))]))
                i += 1
                await asyncio.sleep(0.02)

        call = stub.Session(up())
        async for resp in call:                 # ends when the server closes the
            if resp.WhichOneof("msg") == "welcome":   # stream after up() finishes
                got_welcome.set()


async def main() -> int:
    server, _hub = build_server()
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    addr = f"127.0.0.1:{port}"

    announced, got_welcome, stop = (asyncio.Event() for _ in range(3))
    agent = asyncio.create_task(run_agent(addr, announced, got_welcome, stop))

    await asyncio.wait_for(announced.wait(), 5)
    await asyncio.wait_for(got_welcome.wait(), 5)
    print("✓ handshake: agent connected, Welcome received")
    await asyncio.sleep(0.1)                     # let the announce settle

    async with grpc.aio.insecure_channel(addr) as ch:
        v = rpc.ViewerStub(ch)

        info = await v.GetInfo(pb.GetInfoRequest())
        assert info.contract_version == 1, info
        print(f"✓ GetInfo: hub {info.hub_version}, contract v{info.contract_version}")

        cat = await v.GetCatalog(pb.CatalogRequest())
        dev = next((d for d in cat.devices if d.uuid == DEV), None)
        assert dev is not None, "device not in catalog"
        assert dev.online and dev.name == "Sim gauge"
        assert dev.sources and dev.sources[0].id == "p"
        assert dev.sources[0].dtype == pb.SCALAR
        print(f"✓ catalog: '{dev.name}' visible with its original uuid + source 'p'")

        got: list = []
        sub_call = v.Subscribe(pb.SubscribeRequest())

        async def collect():
            async for batch in sub_call:
                got.extend(batch.readings)
                if len(got) >= 3:
                    return

        await asyncio.wait_for(collect(), 5)
        sub_call.cancel()                       # tear the stream down promptly
        assert len(got) >= 3, got
        r = got[0]
        assert r.device_uuid == DEV and r.source_id == "p"
        assert r.WhichOneof("payload") == "scalar"
        print(f"✓ subscribe: received {len(got)} live readings "
              f"(first scalar={r.scalar:.2e} mbar)")

    # agent leaves → device must disappear from the catalog (→ placeholder, §6.1)
    stop.set()
    await asyncio.wait_for(agent, 5)
    await asyncio.sleep(0.1)
    async with grpc.aio.insecure_channel(addr) as ch:
        v = rpc.ViewerStub(ch)
        cat = await v.GetCatalog(pb.CatalogRequest())
        assert not any(d.uuid == DEV for d in cat.devices), "device not retired"
        print("✓ retire: device removed from catalog after agent disconnect")

    await server.stop(grace=0.5)
    print("\nE2E PASS — transparent remote device, live subscribe, clean retire")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
