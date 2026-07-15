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

    # CURRENT value (readback) travels so the remote config UI shows real state, not a
    # default — a TOGGLE that is ON must come back True (the reported-HV-off bug).
    on = pb.SinkPort(id="hv", name="HV", kind=pb.TOGGLE)
    on.boolean = True
    assert convert.sink_from_proto(on).value is True
    off = pb.SinkPort(id="hv", name="HV", kind=pb.TOGGLE)
    off.boolean = False
    assert convert.sink_from_proto(off).value is False    # off ≠ unknown
    setp = pb.SinkPort(id="v", name="V", kind=pb.SETPOINT)
    setp.scalar = 12.0
    assert convert.sink_from_proto(setp).value == 12.0
    assert convert.sink_from_proto(pb.SinkPort(id="v", name="V",
                                               kind=pb.SETPOINT)).value is None  # unset


def test_option_to_from_proto_and_secrets_are_stripped():
    from ferrodac.core.device import Option
    # choice + text round-trip
    o = Option(key="mode", name="Mode", kind="choice", value="slow",
               choices=(("fast", "Fast"), ("slow", "Slow")))
    p = convert._option_to_proto(o)
    assert p.value == "slow" and {c.value for c in p.choices} == {"fast", "slow"}
    back = convert.option_from_proto(p)
    assert back.kind == "choice" and back.value == "slow"
    assert back.choices == (("fast", "Fast"), ("slow", "Slow"))
    # a SECRET's current value NEVER goes on the wire (settable, not readable)
    sec = convert._option_to_proto(Option(key="auth", name="Key", kind="secret",
                                          value="hunter2"))
    assert sec.value == "" and convert.option_from_proto(sec).value is None


def test_remote_control_dialog_live_refreshes_on_reannounce(qapp):
    """An open remote config dialog reflects the device's new state when it re-announces
    (a command from elsewhere, or an external change the driver reads back) — the HV
    toggle ticks without the user reopening the dialog, and without re-sending."""
    from qtpy.QtCore import QObject, Signal
    from qtpy.QtWidgets import QCheckBox
    from ferrodac.core.device import Sink
    from ferrodac.ui.docks import RemoteControlDialog

    class _FakeDash(QObject):
        ports_changed = Signal()

        def __init__(self):
            super().__init__()
            self._sinks = []

        def remote_sinks(self, uuid):
            return list(self._sinks)

        def remote_options(self, uuid):
            return []

    dash = _FakeDash()
    sent = []
    dlg = RemoteControlDialog(
        "u", "LSA", [Sink(id="hv", name="HV", kind=SinkKind.TOGGLE, value=False)],
        [], send_command=lambda *a: sent.append(a), send_config=lambda *a, **k: None,
        dashboard=dash)
    chk = dlg._sink_widgets["hv"]
    assert isinstance(chk, QCheckBox) and not chk.isChecked()

    dash._sinks = [Sink(id="hv", name="HV", kind=SinkKind.TOGGLE, value=True)]
    dash.ports_changed.emit()                        # the device re-announced HV=on
    assert chk.isChecked()                           # reflected live…
    assert sent == []                                # …WITHOUT re-sending a command
    dlg.close()


def test_devices_panel_lists_remote_available_and_add_routes(qapp):
    """The Devices window shows other clients' available devices grouped by client,
    and Add routes to that client (request_add_remote); the share toggle reflects/sets."""
    from qtpy.QtCore import QObject, Signal
    from qtpy.QtWidgets import QPushButton
    from ferrodac.core.engine import Engine
    from ferrodac.core.manager import DeviceManager
    from ferrodac.core.device import DeviceDescriptor, Interface, Status
    from ferrodac.ui.docks import DevicesPanel

    class _FakeHub(QObject):
        remote_available_changed = Signal()

        def __init__(self):
            super().__init__()
            self.share_devices = True
            self.added = []
            self._ra = {}

        def remote_available(self):
            return dict(self._ra)

        def set_share_devices(self, on):
            self.share_devices = bool(on)

        def request_add_remote(self, aid, iid):
            self.added.append((aid, iid))

    hub = _FakeHub()
    hub._ra = {"ferrodac@rig-1": [DeviceDescriptor(
        instance_id="sim:psu:1", driver="fake_psu", name="PSU",
        interface=Interface(kind="hub"), status=Status.DISCOVERED)]}
    mgr = DeviceManager([], engine=Engine(), registry=None)   # no local devices
    panel = DevicesPanel(mgr, on_configure=lambda iid: None, hub=hub)

    assert not panel._remote_label.isHidden()                 # section shown
    assert "(1)" in panel._remote_label.text()
    adds = [b for b in panel.findChildren(QPushButton) if b.text() == "Add"]
    assert adds, "no Add button for the remote device"
    adds[0].click()
    assert hub.added == [("ferrodac@rig-1", "sim:psu:1")]     # routed by agent_id

    assert panel._share.isChecked()                           # opt-out default on
    panel._share.setChecked(False)
    assert hub.share_devices is False

    hub._ra = {}                                              # client left → section hides
    panel._rebuild_remote()
    assert panel._remote_label.isHidden()


def test_remote_options_surface_for_the_config_dialog(qapp):
    from ferrodac.core.engine import Engine
    from ferrodac.core.manager import DeviceManager
    from ferrodac.core.device import Option
    from ferrodac.ui.workspace import Dashboard, WorkspaceArea

    dash = Dashboard(WorkspaceArea(), Engine(),
                     DeviceManager([], engine=Engine(), registry=None))
    opts = [Option(key="mode", name="Mode", kind="choice", value="fast",
                   choices=(("fast", "Fast"),))]
    dash.add_remote_device("dev-uuid", "Dev", [("x", "X", "float", "")],
                           sinks=(), options=opts)
    assert [o.key for o in dash.remote_options("dev-uuid")] == ["mode"]
    dash.remove_remote_device("dev-uuid")
    assert dash.remote_options("dev-uuid") == []


def test_curate_dialog_includes_video_sources(qapp):
    """Camera/video sources (dtype 'image'/'video') are tickable in the Curate dialog —
    the dtype allowlist previously dropped them, so a remote camera couldn't be curated
    (the remote-video-not-selectable bug)."""
    from ferrodac.core.engine import Engine
    from ferrodac.core.manager import DeviceManager
    from ferrodac.ui.workspace import Dashboard, WorkspaceArea
    from ferrodac.ui.docks import _SourceCurateDialog

    dash = Dashboard(WorkspaceArea(), Engine(),
                     DeviceManager([], engine=Engine(), registry=None))
    dash.add_remote_device("cam-uuid", "Rig Cam",
                           [("frame", "Video", "image", ""),
                            ("temp", "Temp", "float", "C")])
    dlg = _SourceCurateDialog(dash.source_ports(), [], None)
    assert "cam-uuid/frame" in dlg._checks           # the video source got a checkbox…
    assert "cam-uuid/temp" in dlg._checks            # …and the scalar still does
    dlg.deleteLater()


def test_remote_added_fires_once_for_a_new_hub_device(qapp):
    """Workspace.remote_added fires on a hub device's FIRST appearance (drives the app's
    remote auto-curate) — and not again when it merely re-announces/refreshes."""
    from ferrodac.core.engine import Engine
    from ferrodac.core.manager import DeviceManager
    from ferrodac.ui.workspace import Dashboard, WorkspaceArea

    dash = Dashboard(WorkspaceArea(), Engine(),
                     DeviceManager([], engine=Engine(), registry=None))
    seen = []
    dash.remote_added.connect(seen.append)
    dash.add_remote_device("cam-uuid", "Rig Cam", [("frame", "Video", "image", "")])
    dash.add_remote_device("cam-uuid", "Rig Cam",
                           [("frame", "Video", "image", "")])   # re-announce → no re-fire
    assert seen == ["cam-uuid"]                       # first appearance only


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
