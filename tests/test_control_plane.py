"""Control plane (DESIGN §5.3) — the viewer's surfacing leg.

The transport (viewer→hub→agent Command/Ack + readback) is covered end-to-end over
real gRPC by server/tests/control_e2e.py. Here we test the VIEWER side in isolation:
a hub device's sinks convert to app Sinks, become writable ports on the Dashboard,
and their writes route over the wire via send_command — never the local manager.
"""

import pytest

pytest.importorskip("ferrodac_contract.v1.data_plane_pb2")

from ferrodac_contract.v1 import data_plane_pb2 as pb   # noqa: E402
from ferrodac.core.device import SinkKind                # noqa: E402
from ferrodac.net import convert                         # noqa: E402


def test_sink_from_proto_roundtrips_every_kind():
    sp = pb.SinkPort(id="voltage", name="Set Voltage", kind=pb.SETPOINT, unit="V")
    sp.min, sp.max = 0.0, 30.0
    s = convert.sink_from_proto(sp)
    assert s.id == "voltage" and s.kind is SinkKind.SETPOINT
    assert s.params[0].unit == "V"
    assert s.params[0].minimum == 0.0 and s.params[0].maximum == 30.0

    e = convert.sink_from_proto(pb.SinkPort(id="det", name="Detector", kind=pb.ENUM,
                                            options=["FC", "SEM"]))
    assert e.kind is SinkKind.ENUM and tuple(e.params[0].options) == ("FC", "SEM")

    t = convert.sink_from_proto(pb.SinkPort(id="en", name="Enable", kind=pb.TOGGLE))
    assert t.kind is SinkKind.TOGGLE and t.params == ()   # no range → no param

    a = convert.sink_from_proto(pb.SinkPort(id="zero", name="Zero", kind=pb.ACTION))
    assert a.kind is SinkKind.ACTION


def test_remote_sink_becomes_a_writable_port_routed_over_the_hub(qapp):
    from ferrodac.core.engine import Engine
    from ferrodac.core.manager import DeviceManager
    from ferrodac.core.device import Sink, Param
    from ferrodac.ui.workspace import Dashboard, WorkspaceArea

    dash = Dashboard(WorkspaceArea(), Engine(),
                     DeviceManager([], engine=Engine(), registry=None))
    sinks = [Sink(id="voltage", name="Set Voltage", kind=SinkKind.SETPOINT,
                  params=(Param("v", "float", "V", minimum=0.0, maximum=30.0),)),
             Sink(id="enable", name="Enable", kind=SinkKind.TOGGLE)]
    dash.add_remote_device("psu-uuid", "Sim PSU",
                           [("voltage", "Voltage", "float", "V")], sinks)

    sp = dash._sinks.get("psu-uuid#voltage")
    assert sp is not None and sp.remote                      # a writable REMOTE port
    assert sp.device_id == "psu-uuid" and sp.sink_id == "voltage"
    assert sp.smin == 0.0 and sp.smax == 30.0                # range carried for a slider

    # a write routes to send_command (the wire), NOT the local manager, keyed by uuid
    calls = []
    dash.send_command = lambda uuid, sid, val: calls.append((uuid, sid, val))
    dash._write_to_device(sp, 12.0)
    assert calls == [("psu-uuid", "voltage", 12.0)]

    # the config dialog's inputs: sinks + name + origin
    assert [s.id for s in dash.remote_sinks("psu-uuid")] == ["voltage", "enable"]
    assert dash.remote_name("psu-uuid") == "Sim PSU"
    assert dash.source_origin("psu-uuid/voltage") == "remote"

    # leaving the catalog drops the control ports and unbinds them
    dash.remove_remote_device("psu-uuid")
    assert "psu-uuid#voltage" not in dash._sinks
    assert dash.remote_sinks("psu-uuid") == []
