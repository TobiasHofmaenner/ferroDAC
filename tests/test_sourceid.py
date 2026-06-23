"""Unified source identity resolution (Phase 3) — one device-qualified label for
live AND historic sources, with graceful fallback for old stores."""
import os
import types

from ferrodac.core.sourceid import compose_label, resolve_source
from ferrodac.store import ZarrStore


def test_compose_label_rule():
    assert compose_label("ch1", "Sim Gauge A") == "ch1 · Sim Gauge A"
    assert compose_label("ch1", "") == "ch1"                 # unknown device → bare
    assert compose_label("PSU Voltage", "PSU") == "PSU Voltage"   # dev already in name


def test_resolve_live_port():
    port = types.SimpleNamespace(name="ch1", origin="Sim Gauge A", unit="mbar",
                                 dtype="float", kind="device", proc_id="")
    info = resolve_source("g/ch1", live_ports={"g/ch1": port})
    assert info.channel_name == "ch1" and info.device_name == "Sim Gauge A"
    assert info.label == "ch1 · Sim Gauge A" and not info.is_derived


def test_resolve_derived_port_flagged_and_unqualified():
    """A processor output (virtual, proc_id set) is flagged derived and — like
    SourcePort.label — is NOT device-qualified."""
    port = types.SimpleNamespace(name="model", origin="Gas 1", unit="", dtype="trace",
                                 kind="virtual", proc_id="gas1")
    info = resolve_source("model/gas1", live_ports={"model/gas1": port})
    assert info.is_derived and info.label == "model"


def test_resolve_historic_from_store_record(tmp_path):
    st = ZarrStore(os.path.join(str(tmp_path), "s.zarr"))
    st.add_source("sim:gauge:A/ch1", name="ch1", unit="mbar")
    st.put_device("sim:gauge:A", {"name": "Sim Gauge A"})
    st.emit_device_meta("sim:gauge:A", 0.0, "name", "Sim Gauge A")
    info = resolve_source("sim:gauge:A/ch1", store=st)
    assert info.kind == "historic" and info.label == "ch1 · Sim Gauge A"
    assert info.unit == "mbar" and info.dtype == "float"


def test_resolve_old_store_degrades_to_bare(tmp_path):
    st = ZarrStore(os.path.join(str(tmp_path), "s.zarr"))
    st.add_source("dev/ch1", name="ch1")                     # no device record
    info = resolve_source("dev/ch1", store=st)
    assert info.device_name == "" and info.label == "ch1"


def test_historic_sourceport_label_guard_includes_historic():
    """A historic SourcePort (kind='historic') with a device origin device-qualifies
    its label — the bare-'ch1' fix (the guard previously excluded 'historic')."""
    from ferrodac.ui.workspace import SourcePort
    p = SourcePort("sim:gauge:A/ch1", "ch1", "float", "mbar", "Sim Gauge A",
                   "historic", online=False)
    assert p.label == "ch1 · Sim Gauge A"
    bare = SourcePort("dev/ch1", "ch1", "float", "", "", "historic", online=False)
    assert bare.label == "ch1"                   # unknown device → bare, no crash
