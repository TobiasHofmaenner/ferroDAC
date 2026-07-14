"""Configure a device over the hub (DESIGN §5.3) — the Configure RPC.

A viewer sets a remote device's OPTION / NAME via Viewer.SetConfig; the hub routes
a Configure to the owning agent; the agent applies it and Acks; the new state comes
back on the re-announced catalog descriptor (the config readback). No hardware — a
minimal configurable sim device with a choice option + a text option.

Run (from server/):  PYTHONPATH=..:.:gen python tests/configure_e2e.py
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
from ferrodac.core.base import BaseDevice
from ferrodac.core.device import Interface, Option, Source

UUID = "cfg-uuid-1"


class _ConfigurableDev(BaseDevice):
    driver = "fake_cfg"

    def _connect(self) -> None:
        pass

    def _read(self, source):
        return 0.0, 0


def _make_dev() -> _ConfigurableDev:
    return _ConfigurableDev(
        instance_id="sim:cfg:1", name="Configurable",
        interface=Interface(kind="sim", params={}),
        sources=[Source(id="x", name="X", unit="")],
        options=[Option(key="mode", name="Mode", kind="choice", value="fast",
                        choices=(("fast", "Fast"), ("slow", "Slow"))),
                 Option(key="label", name="Label", kind="text", value="")])


async def main() -> int:
    d = tempfile.mkdtemp()
    server, _hub = build_server(store=ZarrStore(os.path.join(d, "hub.zarr")))
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    addr = f"127.0.0.1:{port}"

    dev = _make_dev()
    dev.set_uuid(UUID)

    def on_configure(uuid, action, *args):
        if uuid != dev.uuid:
            return False, "unknown device"
        try:
            if action == "option":
                dev.set_option(args[0], args[1])
            elif action == "rename":
                dev.set_name(args[0])
            else:
                return False, f"unsupported action: {action}"
        except Exception as exc:                        # noqa: BLE001
            return False, str(exc)
        agent.announce(dev.describe())                  # mirror active_changed → re-announce
        return True, ""

    agent = HubAgent(addr, agent_id="cfg-agent", on_configure=on_configure)
    agent.start()
    agent.announce(dev.describe())

    channel = grpc.insecure_channel(addr)
    stub = rpc.ViewerStub(channel)

    async def catalog_dev():
        c = await asyncio.to_thread(lambda: stub.GetCatalog(pb.CatalogRequest()))
        return next((x for x in c.devices if x.uuid == UUID), None)

    # 1) the catalog carries OPTIONS over the wire (the viewer builds the config form).
    dv = None
    for _ in range(100):
        dv = await catalog_dev()
        if dv is not None:
            break
        await asyncio.sleep(0.05)
    assert dv is not None, "device never appeared in the catalog"
    assert {o.key for o in dv.options} == {"mode", "label"}, [o.key for o in dv.options]
    mode = next(o for o in dv.options if o.key == "mode")
    assert mode.kind == "choice" and mode.value == "fast"
    assert {c.value for c in mode.choices} == {"fast", "slow"}
    print(f"✓ catalog carries {len(dv.options)} config option(s) + choices over the wire")

    async def configure(**kw):
        req = pb.ConfigureRequest(device_uuid=UUID, **kw)
        return await asyncio.to_thread(lambda: stub.SetConfig(req))

    # 2) SetConfig an option → agent applies it → device holds the new value.
    ack = await configure(option=pb.OptionSet(key="mode", value="slow"))
    assert ack.ok, ack.detail
    assert dev._option_values["mode"] == "slow"
    print("✓ SetConfig(option): mode → slow, applied on the device")

    # 3) rename over the hub.
    ack = await configure(rename="Renamed Device")
    assert ack.ok and dev.name == "Renamed Device", ack.detail
    print("✓ SetConfig(rename): device renamed over the hub")

    # 4) the re-announced descriptor carries the new state (the config readback, §5.3).
    for _ in range(100):
        dv = await catalog_dev()
        if dv is not None and dv.name == "Renamed Device" \
                and next(o.value for o in dv.options if o.key == "mode") == "slow":
            break
        await asyncio.sleep(0.05)
    assert dv.name == "Renamed Device"
    assert next(o.value for o in dv.options if o.key == "mode") == "slow"
    print("✓ re-announced descriptor reflects the new option + name (config readback)")

    # 5) graceful rejection: unknown device + a bad choice value.
    bad = await asyncio.to_thread(lambda: stub.SetConfig(
        pb.ConfigureRequest(device_uuid="nope", rename="x")))
    assert not bad.ok and "no agent" in bad.detail.lower(), bad.detail
    print("✓ unknown device rejected (ok=false)")

    channel.close()
    agent.stop()
    await server.stop(grace=0)
    print("\nCONFIGURE E2E PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
