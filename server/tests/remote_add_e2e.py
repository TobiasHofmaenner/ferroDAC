"""Remote device addition over the hub — advertise available devices + add them.

An agent advertises a discovered-but-unadded device; a viewer sees it in the catalog
(tagged `available` + the owning client's agent_id); the viewer asks the hub to add
it; the hub routes an AddDevice to that CLIENT by agent_id (an available device has
no uuid to route by); the client onboards it, and it re-announces as an ACTIVE remote
device with sources. No hardware.

Run (from server/):  PYTHONPATH=..:.:gen python tests/remote_add_e2e.py
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
from ferrodac.store import ZarrStore
from ferrodac.net.agent import HubAgent
from ferrodac.net import convert
from ferrodac.devices.fake import FakePowerSupply

AGENT_ID = "ferrodac@rig-1"


async def main() -> int:
    d = tempfile.mkdtemp()
    server, _hub = build_server(store=ZarrStore(os.path.join(d, "hub.zarr")))
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    addr = f"127.0.0.1:{port}"

    # the agent has ONE available (discovered, not-added) device — no uuid yet.
    psu = FakePowerSupply.discover()[0]                 # instance_id "sim:psu:1"
    available = {psu.instance_id: psu}
    active: dict = {}

    def advertise():
        agent.set_devices([x.describe() for x in active.values()],
                          available=[x.describe() for x in available.values()])

    def on_add_device(instance_id):                     # onboard on request (like manager.add)
        dev = available.pop(instance_id, None)
        if dev is None:
            return False, "no such available device"
        dev.set_uuid("psu-onboarded-1")                 # add-time uuid assignment
        active[instance_id] = dev
        advertise()                                     # available → active, re-announced
        return True, ""

    agent = HubAgent(addr, agent_id=AGENT_ID, on_add_device=on_add_device)
    agent.start()
    advertise()                                         # publish the available device

    channel = grpc.insecure_channel(addr)
    stub = rpc.ViewerStub(channel)

    async def catalog():
        c = await asyncio.to_thread(lambda: stub.GetCatalog(pb.CatalogRequest()))
        return list(c.devices)

    # 1) the available device shows in the catalog, tagged available + agent_id, and
    #    keyed by the collision-proof synthetic uuid (two clients can share sim:psu:1).
    av = None
    for _ in range(100):
        av = next((x for x in await catalog() if x.available), None)
        if av is not None:
            break
        await asyncio.sleep(0.05)
    assert av is not None, "available device never advertised"
    assert av.agent_id == AGENT_ID and av.instance_id == "sim:psu:1"
    assert av.uuid == convert.available_uuid(AGENT_ID, "sim:psu:1")
    print(f"✓ available device advertised: {av.name} (available on {av.agent_id})")

    # 2) AddRemoteDevice routes by agent_id → the client onboards it.
    ack = await asyncio.to_thread(lambda: stub.AddRemoteDevice(
        pb.AddDeviceRequest(agent_id=AGENT_ID, instance_id="sim:psu:1")))
    assert ack.ok, ack.detail
    print("✓ AddRemoteDevice acked; the owning client onboarded the device")

    # 3) it re-announces as an ACTIVE remote device (real uuid, sources), and the
    #    available entry is retired.
    act = None
    for _ in range(100):
        act = next((x for x in await catalog()
                    if not x.available and x.uuid == "psu-onboarded-1"), None)
        if act is not None:
            break
        await asyncio.sleep(0.05)
    assert act is not None, "device did not re-announce as active"
    assert {s.id for s in act.sources} >= {"voltage", "current"}, [s.id for s in act.sources]
    assert not any(x.available for x in await catalog()), "available entry not retired"
    print("✓ device re-announced ACTIVE with sources; available entry retired")

    # 4) add to a client that is not connected → rejected, no hang.
    bad = await asyncio.to_thread(lambda: stub.AddRemoteDevice(
        pb.AddDeviceRequest(agent_id="ferrodac@nobody", instance_id="x")))
    assert not bad.ok and "not connected" in bad.detail.lower(), bad.detail
    print("✓ add to an unknown client rejected (ok=false)")

    channel.close()
    agent.stop()
    await server.stop(grace=0)
    print("\nREMOTE DEVICE ADD E2E PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
