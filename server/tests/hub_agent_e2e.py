"""Agent-side Qt wiring: a real Engine + HubController(agent) → hub → viewer.

Covers the path the other e2e tests skipped: Engine readings flowing through
HubController._feed_agent → HubAgent.feed → the wire. Host-run, offscreen:

    QT_QPA_PLATFORM=offscreen PYTHONPATH=.:server:server/gen \
        python3 server/tests/hub_agent_e2e.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("GRPC_VERBOSITY", "NONE")

import grpc
from qtpy.QtWidgets import QApplication

from hub.main import build_server
from ferrodac_contract.v1 import data_plane_pb2 as pb
from ferrodac_contract.v1 import data_plane_pb2_grpc as rpc
from ferrodac.core.device import DeviceDescriptor, Interface, Source
from ferrodac.core.engine import Engine
from ferrodac.core.manager import DeviceManager
from ferrodac.core.reading import Reading
from ferrodac.ui.hubclient import HubController
from ferrodac.ui.workspace import Dashboard, WorkspaceArea

UUID = "uuid-agent-1"
DESC = DeviceDescriptor(
    instance_id="/dev/sim0", driver="sim", name="Bench RGA",
    interface=Interface(kind="sim"), uuid=UUID,
    sources=[Source(id="p", name="Pressure", unit="mbar", dtype="float")])


def _run_hub(out, ready):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def go():
        server, _ = build_server()
        port = server.add_insecure_port("127.0.0.1:0")
        await server.start()
        out["addr"] = f"127.0.0.1:{port}"
        ready.set()
        await asyncio.Event().wait()

    loop.run_until_complete(go())


def main() -> int:
    app = QApplication([])
    out, ready = {}, threading.Event()
    threading.Thread(target=_run_hub, args=(out, ready), daemon=True).start()
    assert ready.wait(5), "hub did not start"
    addr = out["addr"]

    # an agent app with one "active" device
    engine = Engine()
    mgr = DeviceManager([])
    mgr.active_descriptors = lambda: [DESC]      # pretend this device is live
    dash = Dashboard(WorkspaceArea(), engine, mgr)
    hub = HubController(dash, engine, mgr)
    hub.connect(addr, as_agent=True, as_viewer=False)

    # a raw viewer stub (separate thread) collects what the agent publishes
    got, vlock, vstop = [], threading.Lock(), threading.Event()

    def run_viewer():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def go():
            async with grpc.aio.insecure_channel(addr) as ch:
                async for batch in rpc.ViewerStub(ch).Subscribe(pb.SubscribeRequest()):
                    with vlock:
                        got.extend(batch.readings)
                    if vstop.is_set():
                        break
        try:
            loop.run_until_complete(go())
        except Exception:
            pass

    threading.Thread(target=run_viewer, daemon=True).start()

    def pump(secs):
        t0 = time.time()
        while time.time() - t0 < secs:
            app.processEvents()
            time.sleep(0.02)

    pump(0.5)                                    # agent connects + announces

    # produce readings through the Engine exactly as a device would
    for i in range(5):
        engine.publish(Reading(device=UUID, source="p",
                               t=float(i), value=1e-6 * (i + 1)))
        pump(0.1)

    ok = False
    for _ in range(100):
        pump(0.05)
        with vlock:
            if any(r.device_uuid == UUID and r.source_id == "p" for r in got):
                ok = True
                break
    assert ok, "agent did not publish the Engine's device readings"
    with vlock:
        r = next(x for x in got if x.device_uuid == UUID)
    assert r.WhichOneof("payload") == "scalar"
    print(f"✓ agent path: Engine readings reach the hub (scalar={r.scalar:.2e} mbar)")

    vstop.set()
    hub.disconnect()
    pump(0.2)
    engine.shutdown()
    print("\nHUB AGENT E2E PASS — HubController publishes live Engine readings")
    return 0


if __name__ == "__main__":
    sys.exit(main())
