"""Viewer-side Qt wiring, end to end: a real hub + a real agent + the app's
HubController injecting a remote device into a real Dashboard/Engine.

Host-run (needs Qt + grpcio), offscreen:

    QT_QPA_PLATFORM=offscreen PYTHONPATH=server:server/gen \
        python3 server/tests/hub_viewer_e2e.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("GRPC_VERBOSITY", "NONE")

from qtpy.QtWidgets import QApplication

from hub.main import build_server
from ferrodac.core.device import DeviceDescriptor, Interface, Source
from ferrodac.core.engine import Engine
from ferrodac.core.manager import DeviceManager
from ferrodac.core.reading import Reading
from ferrodac.net.agent import HubAgent
from ferrodac.ui.hubclient import HubController
from ferrodac.ui.workspace import Dashboard, WorkspaceArea

UUID = "uuid-rga-1"
KEY = f"{UUID}/p"


def _run_hub(addr_out, ready):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def go():
        server, _ = build_server()
        port = server.add_insecure_port("127.0.0.1:0")
        await server.start()
        addr_out["addr"] = f"127.0.0.1:{port}"
        ready.set()
        await asyncio.Event().wait()

    loop.run_until_complete(go())


def main() -> int:
    app = QApplication([])

    addr_out, ready = {}, threading.Event()
    threading.Thread(target=_run_hub, args=(addr_out, ready), daemon=True).start()
    assert ready.wait(5), "hub did not start"
    addr = addr_out["addr"]

    def pump(secs):
        t0 = time.time()
        while time.time() - t0 < secs:
            app.processEvents()
            time.sleep(0.02)

    def wait_for(pred, secs=5.0):
        t0 = time.time()
        while time.time() - t0 < secs:
            app.processEvents()
            if pred():
                return True
            time.sleep(0.02)
        return False

    # the viewer app: a real Dashboard/Engine + HubController as a viewer
    eng = Engine()
    dash = Dashboard(WorkspaceArea(), eng, DeviceManager([]))
    cpid = dash.add_panel("chart")
    hub = HubController(dash, eng, DeviceManager([]))
    hub.connect(addr, as_agent=False, as_viewer=True)

    # a separate agent publishes a device + a reading
    agent = HubAgent(addr, agent_id="bench")
    agent.start()
    agent.announce(DeviceDescriptor(
        instance_id="/dev/sim0", driver="sim", name="Sim RGA",
        interface=Interface(kind="sim"), uuid=UUID,
        sources=[Source(id="p", name="Pressure", unit="mbar", dtype="float")]))

    assert wait_for(lambda: KEY in dash._sources), "remote port not injected"
    port = dash._sources[KEY]
    assert port.kind == "remote" and port.online and port.origin == "Sim RGA"
    assert port.dtype == "float" and port.unit == "mbar"
    print("✓ viewer: hub device injected as a 'remote' port", KEY)

    # route the remote source onto the chart, then publish a reading
    dash.set_route(KEY, cpid, True)
    assert KEY in dash._panels[cpid]._curves, "route did not reach the panel"
    print("✓ route: remote source bound to a local chart")

    agent.feed([Reading(device=UUID, source="p", t=1.0, value=4.2e-6)])
    assert wait_for(lambda: KEY in eng.latest()), "remote reading never reached Engine"
    assert abs(eng.latest()[KEY].value - 4.2e-6) < 1e-12
    print(f"✓ readings: remote reading rendered through the Engine "
          f"({eng.latest()[KEY].value:.2e} mbar)")

    # agent leaves → remote device greys out (placeholder), routes kept
    agent.stop()
    assert wait_for(lambda: not dash._sources[KEY].online), "remote not greyed on leave"
    assert KEY in dash._sources and KEY in dash._panels[cpid]._curves
    print("✓ placeholder: remote device greyed but kept after the agent left")

    hub.disconnect()
    pump(0.2)
    eng.shutdown()
    print("\nHUB VIEWER E2E PASS — remote device renders like a local one")
    return 0


if __name__ == "__main__":
    sys.exit(main())
