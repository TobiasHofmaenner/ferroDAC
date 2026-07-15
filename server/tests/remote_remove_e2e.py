"""Remote device removal over the hub — retire an active remote device (reverse of add).

An agent has an ACTIVE device (a real uuid + sources); a viewer sees it in the catalog;
the viewer asks the hub to remove it; the hub routes a RemoveDevice to the owning CLIENT
by the device uuid (translated to its instance_id); the client retires it, and it drops
from the catalog. No hardware. The mirror of remote_add_e2e.py.

Run (from server/):  PYTHONPATH=..:.:gen python tests/remote_remove_e2e.py
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
from ferrodac.devices.fake import FakePowerSupply

AGENT_ID = "ferrodac@rig-1"


async def main() -> int:
    d = tempfile.mkdtemp()
    server, _hub = build_server(store=ZarrStore(os.path.join(d, "hub.zarr")))
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    addr = f"127.0.0.1:{port}"

    # the agent has ONE ACTIVE device — a real uuid + sources (as if already onboarded).
    psu = FakePowerSupply.discover()[0]                 # instance_id "sim:psu:1"
    psu.set_uuid("psu-1")
    active = {psu.instance_id: psu}

    def advertise():
        agent.set_devices([x.describe() for x in active.values()], available=[])

    def on_remove_device(instance_id):                  # retire on request (like manager.remove)
        dev = active.pop(instance_id, None)
        if dev is None:
            return False, "no such active device"
        advertise()                                     # dropped from the set → Retire sent up
        return True, ""

    agent = HubAgent(addr, agent_id=AGENT_ID, on_remove_device=on_remove_device)
    agent.start()
    advertise()

    channel = grpc.insecure_channel(addr)
    stub = rpc.ViewerStub(channel)

    async def catalog():
        c = await asyncio.to_thread(lambda: stub.GetCatalog(pb.CatalogRequest()))
        return list(c.devices)

    # 1) the active device shows in the catalog with its uuid + sources.
    act = None
    for _ in range(100):
        act = next((x for x in await catalog() if x.uuid == "psu-1"), None)
        if act is not None:
            break
        await asyncio.sleep(0.05)
    assert act is not None, "active device never advertised"
    assert {s.id for s in act.sources} >= {"voltage", "current"}, [s.id for s in act.sources]
    assert act.instance_id == "sim:psu:1"
    print(f"✓ active remote device advertised: {act.name} ({act.uuid})")

    # 2) RemoveRemoteDevice routes by uuid → the owning client retires it (the hub
    #    translated the uuid to the owner's instance_id for the down-message).
    ack = await asyncio.to_thread(lambda: stub.RemoveRemoteDevice(
        pb.RemoveDeviceRequest(device_uuid="psu-1")))
    assert ack.ok, ack.detail
    print("✓ RemoveRemoteDevice acked; the owning client retired the device")

    # 3) it drops from the catalog.
    gone = False
    for _ in range(100):
        if not any(x.uuid == "psu-1" for x in await catalog()):
            gone = True
            break
        await asyncio.sleep(0.05)
    assert gone, "device did not drop from the catalog after remove"
    print("✓ device retired; dropped from the catalog")

    # 4) remove a uuid no agent owns → rejected, no hang.
    bad = await asyncio.to_thread(lambda: stub.RemoveRemoteDevice(
        pb.RemoveDeviceRequest(device_uuid="nobody")))
    assert not bad.ok and "no agent" in bad.detail.lower(), bad.detail
    print("✓ remove of an unowned device rejected (ok=false)")

    channel.close()
    agent.stop()
    await server.stop(grace=0)
    print("\nREMOTE DEVICE REMOVE E2E PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
