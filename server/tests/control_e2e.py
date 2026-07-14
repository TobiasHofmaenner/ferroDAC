"""Control plane over REAL gRPC (DESIGN §5.3 / §7.5) — the reserved command bus, lit.

A viewer issues `SendCommand` to the hub; the hub routes it to the owning agent's
Ingest down-channel as a `Command`; the agent runs the local device write and
replies with an `Ack` up the same stream; the device's **readback** (its sink value,
which its data sources derive from — §7.5) reflects the command. No hardware — a
simulated bench power supply. This proves the command→ack transport, the catalog
carrying control sinks, range-clamp safety, and graceful rejection.

Run (from server/):  PYTHONPATH=..:.:gen python tests/control_e2e.py
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

UUID = "psu-uuid-1"


async def main() -> int:
    d = tempfile.mkdtemp()
    hub_store = ZarrStore(os.path.join(d, "hub.zarr"))
    server, _hub = build_server(store=hub_store)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    addr = f"127.0.0.1:{port}"

    # THE AGENT owns a controllable sim PSU. on_command runs the local write and
    # returns (ok, detail) — exactly what HubController wires to manager.write_sync.
    psu = FakePowerSupply.discover()[0]
    psu.set_uuid(UUID)

    def on_command(uuid, sink_id, value):
        if uuid != psu.uuid:
            return False, "unknown device"
        try:
            psu.write(sink_id, value)
        except Exception as exc:                        # noqa: BLE001
            return False, str(exc)
        agent.announce(psu.describe())                  # mirror active_changed → re-announce
        return True, ""

    agent = HubAgent(addr, agent_id="ctrl-agent", on_command=on_command)
    agent.start()
    agent.announce(psu.describe())

    channel = grpc.insecure_channel(addr)
    stub = rpc.ViewerStub(channel)

    # 1) the announce must reach the hub catalog WITH its control sinks (the viewer
    #    needs them to build controls — the leg that was structurally dropped).
    dv = None
    for _ in range(100):
        cat = await asyncio.to_thread(lambda: stub.GetCatalog(pb.CatalogRequest()))
        dv = next((x for x in cat.devices if x.uuid == UUID), None)
        if dv is not None:
            break
        await asyncio.sleep(0.05)
    assert dv is not None, "device never appeared in the hub catalog"
    assert {s.id for s in dv.sinks} >= {"enable", "voltage", "current_limit"}, \
        [s.id for s in dv.sinks]
    print(f"✓ catalog carries {len(dv.sinks)} control sink(s) over the wire")

    async def send(sink_id, **value):
        req = pb.CommandRequest(device_uuid=UUID, sink_id=sink_id, **value)
        return await asyncio.to_thread(lambda: stub.SendCommand(req))

    # 2) SETPOINT: viewer → hub → agent → device.write; readback reflects it (§7.5).
    ack = await send("voltage", scalar=12.0)
    assert ack.ok, ack.detail
    assert abs(psu._sink_values["voltage"] - 12.0) < 1e-9
    assert next(s.value for s in psu.describe().sinks if s.id == "voltage") == 12.0
    print("✓ SETPOINT command: voltage → 12 V, captured on the device's readback")

    # 3) TOGGLE — and the re-announced descriptor carries the sink's CURRENT value, so
    #    a viewer's config UI shows real state (a ticked toggle), not a default.
    ack = await send("enable", boolean=True)
    assert ack.ok and psu._sink_values["enable"] is True
    for _ in range(100):
        cat = await asyncio.to_thread(lambda: stub.GetCatalog(pb.CatalogRequest()))
        dv2 = next((x for x in cat.devices if x.uuid == UUID), None)
        en = next((s for s in dv2.sinks if s.id == "enable"), None) if dv2 else None
        if en is not None and en.WhichOneof("value") == "boolean" and en.boolean:
            break
        await asyncio.sleep(0.05)
    assert en is not None and en.boolean is True, "sink readback value not on the wire"
    print("✓ TOGGLE command: enable → on, current value visible on the descriptor")

    # 4) the setpoint is CLAMPED to the sink's declared range (0–30 V) — safety.
    ack = await send("voltage", scalar=100.0)
    assert ack.ok and abs(psu._sink_values["voltage"] - 30.0) < 1e-9
    print("✓ out-of-range setpoint clamped to 30 V (declared max)")

    # 5) graceful rejection: unknown device (no agent owns it) and unknown sink —
    #    ok=false with a reason, and the session survives for the next command.
    bad_dev = await asyncio.to_thread(lambda: stub.SendCommand(
        pb.CommandRequest(device_uuid="nope", sink_id="voltage", scalar=1.0)))
    assert not bad_dev.ok and "no agent" in bad_dev.detail.lower(), bad_dev.detail
    bad_sink = await send("does_not_exist", scalar=1.0)
    assert not bad_sink.ok and bad_sink.detail, bad_sink.detail
    ack = await send("voltage", scalar=7.0)             # session still works after
    assert ack.ok and abs(psu._sink_values["voltage"] - 7.0) < 1e-9
    print("✓ unknown device + unknown sink rejected (ok=false); session survives")

    channel.close()
    agent.stop()
    await server.stop(grace=0)
    print("\nCONTROL E2E PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
