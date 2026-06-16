"""App-side round-trip: HubAgent → hub → HubViewer, through the real net layer.

Unlike e2e.py (raw stubs), this drives the actual ferrodac.net agent/viewer and
the app's own dataclasses (DeviceDescriptor, Reading, Trace), asserting a device
and a scalar + a Trace survive the trip and come back as app objects. Qt-free —
runs in the hub image (with numpy) against an in-process hub.
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading

os.environ.setdefault("GRPC_VERBOSITY", "NONE")

import numpy as np

from hub.main import build_server
from ferrodac.core.device import (DeviceDescriptor, Interface, Sink, SinkKind,
                                   Source)
from ferrodac.core.reading import Reading
from ferrodac.core.trace import Trace
from ferrodac.net.agent import HubAgent
from ferrodac.net.viewer import HubViewer

UUID = "uuid-rga-1"


async def _until(pred, timeout=5.0, step=0.05):
    for _ in range(int(timeout / step)):
        if pred():
            return True
        await asyncio.sleep(step)
    return False


async def main() -> int:
    server, _hub = build_server()
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    addr = f"127.0.0.1:{port}"

    cat: dict = {}
    readings: list = []
    lock = threading.Lock()

    def on_catalog(etype, dev):
        with lock:
            if etype in ("ADDED", "UPDATED"):
                cat[dev.uuid] = dev
            elif etype == "REMOVED":
                cat.pop(dev.uuid, None)

    def on_readings(rs):
        with lock:
            readings.extend(rs)

    viewer = HubViewer(addr, on_catalog=on_catalog, on_readings=on_readings)
    viewer.start()

    desc = DeviceDescriptor(
        instance_id="/dev/sim0", driver="sim", name="Sim RGA",
        interface=Interface(kind="sim"), uuid=UUID,
        hardware_id="sim:rga", firmware="fw1",
        sources=[Source(id="p", name="Pressure", unit="mbar", dtype="float"),
                 Source(id="spec", name="Spectrum", unit="mbar", dtype="trace")],
        sinks=[Sink(id="emis", name="Emission", kind=SinkKind.TOGGLE)])
    agent = HubAgent(addr, agent_id="bench")
    agent.start()
    agent.announce(desc)

    assert await _until(lambda: UUID in cat), "device never reached the viewer"
    with lock:
        dev = cat[UUID]
    assert dev.name == "Sim RGA"
    assert {s.id for s in dev.sources} == {"p", "spec"}
    assert {s.id: s.dtype for s in dev.sources}["spec"]  # TRACE dtype set
    assert dev.sinks and dev.sinks[0].id == "emis"
    print("✓ catalog: remote device + sources + sink reached the viewer")

    x = np.linspace(1, 50, 50)
    y = np.exp(-0.5 * ((x - 28) / 0.5) ** 2)
    agent.feed([
        Reading(device=UUID, source="p", t=1.0, value=3.3e-6),
        Reading(device=UUID, source="spec", t=1.0,
                value=Trace(x=x, y=y, x_label="m/z", y_unit="mbar",
                            x_lo=1.0, x_hi=50.0)),
    ])

    assert await _until(lambda: len(readings) >= 2), "readings never arrived"
    with lock:
        by = {(r.device, r.source): r for r in readings}
    assert (UUID, "p") in by and (UUID, "spec") in by, list(by)
    assert abs(by[(UUID, "p")].value - 3.3e-6) < 1e-12
    tr = by[(UUID, "spec")].value
    assert isinstance(tr, Trace) and len(tr) == 50
    assert tr.x_label == "m/z" and tr.y_unit == "mbar"
    assert abs(tr.x_hi - 50.0) < 1e-9
    assert abs(tr.peak - 1.0) < 1e-6
    peak_mz = float(tr.x[int(tr.y.argmax())])
    print(f"✓ readings: scalar + Trace round-tripped (peak {tr.peak:.3f} "
          f"at m/z≈{peak_mz:.0f})")

    agent.stop()                               # disconnect ⇒ hub retires its devices
    assert await _until(lambda: UUID not in cat), "device not retired on disconnect"
    print("✓ retire: device left the catalog after the agent disconnected")

    viewer.stop()
    await server.stop(grace=0.5)
    print("\nNET E2E PASS — app ↔ contract round-trip via agent + viewer")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
